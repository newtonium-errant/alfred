"""Talker daemon — top-level entry point for ``alfred talker`` / orchestrator.

Wires together:
    * Config + logging setup
    * :class:`StateManager` + the startup stale-session sweep
    * An ``anthropic.AsyncAnthropic`` client
    * SKILL.md system prompt + vault context snapshot
    * The Telegram :class:`Application` (long-poll mode for wk1)
    * A background task that ticks every 60s and closes any sessions that
      have exceeded ``session.gap_timeout_seconds``.

PTB lifecycle note:
    :meth:`Application.run_polling` assumes it owns the event loop (it sets
    up SIGTERM handlers, closes the loop at exit). That collides with our
    own signal handler + sweeper task, so we use the manual lifecycle:
    ``initialize()`` → ``start()`` → ``updater.start_polling()`` → wait on a
    shutdown event → ``updater.stop() / stop() / shutdown()``. Clean
    integration, SIGTERM closes sessions, no orphaned polling task.

Returns a non-zero exit code (``_MISSING_CONFIG_EXIT``) when required
config is missing — matches orchestrator convention so commit 5 can route
it to the "don't retry" branch.
"""

from __future__ import annotations

import asyncio
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
from telegram import Update

from . import bot, session
from .config import TalkerConfig, load_from_unified
from .state import StateManager
from .utils import get_logger, setup_logging

log = get_logger(__name__)


# Matches ``orchestrator._MISSING_DEPS_EXIT``. Reusing the same code keeps
# commit 5's wiring one line: the orchestrator already maps 78 to "don't
# restart", which is the right behaviour for misconfiguration too.
_MISSING_CONFIG_EXIT = 78

# How often the sweeper checks for timed-out sessions.
_SWEEP_INTERVAL_SECONDS = 60


# --- Validation -----------------------------------------------------------


def _missing_config_reasons(config: TalkerConfig) -> list[str]:
    """Return human-readable reasons why config is incomplete (or []).

    Anything that would crash the daemon on first use counts — a missing
    bot token leaves us unable to build the Application; an empty allowlist
    means no one can talk to us; missing API keys make the first message
    500 the user.
    """
    reasons: list[str] = []
    if not config.bot_token:
        reasons.append("telegram.bot_token is empty")
    if not config.allowed_users:
        reasons.append("telegram.allowed_users is empty")
    if not config.anthropic.api_key:
        reasons.append("telegram.anthropic.api_key is empty")
    if not config.stt.api_key:
        reasons.append("telegram.stt.api_key is empty")
    if not config.vault.path:
        reasons.append("vault.path is empty")
    return reasons


# --- Prompt + context assembly --------------------------------------------


def _load_system_prompt(skills_dir: Path) -> str:
    """Read ``vault-talker/SKILL.md`` — placeholder content is fine for wk1."""
    skill_path = skills_dir / "vault-talker" / "SKILL.md"
    if not skill_path.exists():
        log.warning("talker.daemon.skill_missing", path=str(skill_path))
        return ""
    return skill_path.read_text(encoding="utf-8")


def _build_vault_context_str(config: TalkerConfig) -> str:
    """Build a compact vault-context snapshot string for the system blocks.

    Reuses curator's :func:`build_vault_context` when available — it already
    walks the vault and formats a type-grouped listing. If the curator
    module isn't importable (highly unlikely in practice), fall back to a
    minimal inline summary so the daemon still boots.
    """
    try:
        from alfred.curator.context import build_vault_context

        ctx = build_vault_context(
            Path(config.vault.path),
            ignore_dirs=config.vault.ignore_dirs,
        )
        return ctx.to_prompt_text()
    except Exception as exc:  # noqa: BLE001
        log.warning("talker.daemon.vault_context_fallback", error=str(exc))
        # Minimal inline fallback — a two-line summary is enough to unblock
        # the tool-use loop; prompt-tuner can enrich later.
        vault = Path(config.vault.path)
        if not vault.is_dir():
            return "Vault: (not yet initialised)"
        # Count top-level type directories as a cheap summary.
        type_counts: list[str] = []
        for sub in sorted(vault.iterdir()):
            if not sub.is_dir() or sub.name.startswith("."):
                continue
            md_count = sum(1 for _ in sub.glob("*.md"))
            if md_count:
                type_counts.append(f"{sub.name}={md_count}")
        return "Vault types: " + ", ".join(type_counts)


# --- Main entry -----------------------------------------------------------


async def run(
    raw: dict[str, Any],
    skills_dir_str: str,
    suppress_stdout: bool = False,
) -> int:
    """Run the talker daemon until SIGTERM. Returns an exit code.

    Args:
        raw: The unified config dict (output of ``yaml.safe_load(config.yaml)``).
        skills_dir_str: Path to the bundled skills directory (contains
            ``vault-talker/SKILL.md``).
        suppress_stdout: When True, log to file only — required by the
            orchestrator's live-mode TUI which owns stdout.

    Returns:
        ``0`` on clean shutdown, ``_MISSING_CONFIG_EXIT`` (78) when required
        config is missing. Matches the orchestrator's "don't retry" contract.
    """
    # Config + logging first — everything below assumes both.
    config = load_from_unified(raw)
    setup_logging(
        level=config.logging.level,
        log_file=config.logging.file,
        suppress_stdout=suppress_stdout,
    )

    reasons = _missing_config_reasons(config)
    if reasons:
        log.error("talker.daemon.missing_config", reasons=reasons)
        return _MISSING_CONFIG_EXIT

    log.info("talker.daemon.starting", model=config.anthropic.model)

    state_mgr = StateManager(config.session.state_path)
    state_mgr.load()

    # Startup sweep — close any sessions whose last_message_at is older than
    # the gap timeout. Covers daemon restarts where a session was mid-flight
    # when we went down.
    now = datetime.now(timezone.utc)
    gap = config.session.gap_timeout_seconds
    closed = session.resolve_on_startup(state_mgr, now, gap)
    if closed:
        log.info("talker.daemon.startup_sweep", closed=len(closed))

    client = anthropic.AsyncAnthropic(api_key=config.anthropic.api_key)
    system_prompt = _load_system_prompt(Path(skills_dir_str))
    vault_context_str = _build_vault_context_str(config)

    app = bot.build_app(
        config=config,
        state_mgr=state_mgr,
        anthropic_client=client,
        system_prompt=system_prompt,
        vault_context_str=vault_context_str,
    )

    # ---- Shutdown coordination -------------------------------------------
    # The event ties three things together: SIGTERM, the sweeper loop, and
    # the long-poll runner. Whichever fires first wins; the others observe
    # the set event and bail cleanly.
    shutdown_event = asyncio.Event()

    def _on_sigterm() -> None:
        log.info("talker.daemon.signal", signal="SIGTERM")
        shutdown_event.set()

    def _on_sigint() -> None:
        log.info("talker.daemon.signal", signal="SIGINT")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig, handler in ((signal.SIGTERM, _on_sigterm), (signal.SIGINT, _on_sigint)):
        try:
            loop.add_signal_handler(sig, handler)
        except (NotImplementedError, RuntimeError):
            # Platforms where signal handlers can't be installed on the
            # event loop (Windows, some child-process contexts). The
            # orchestrator SIGTERMs the process directly on those paths.
            pass

    # ---- Gap-timeout sweeper ---------------------------------------------
    async def _sweeper() -> None:
        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(), timeout=_SWEEP_INTERVAL_SECONDS
                )
                return  # event set → exit
            except asyncio.TimeoutError:
                pass
            try:
                closed_paths = session.check_timeouts(
                    state_mgr,
                    datetime.now(timezone.utc),
                    config.session.gap_timeout_seconds,
                )
                if closed_paths:
                    log.info(
                        "talker.daemon.sweep_closed",
                        count=len(closed_paths),
                    )
            except Exception:  # noqa: BLE001
                log.exception("talker.daemon.sweep_error")

    sweeper_task = asyncio.create_task(_sweeper(), name="talker-sweeper")

    # ---- PTB lifecycle ---------------------------------------------------
    # Manual initialize/start/start_polling to coexist with our own loop.
    # We track whether each stage completed so the finally block only
    # tears down what was actually set up — a bad token raises during
    # initialize() and leaves updater/start in an undefined state.
    initialised = False
    started = False
    polling = False
    exit_code = 0
    try:
        await app.initialize()
        initialised = True
        await app.start()
        started = True
        await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        polling = True
        log.info("talker.daemon.polling")
        await shutdown_event.wait()
    except Exception as exc:  # noqa: BLE001 — top-level: log and exit cleanly
        # InvalidToken / network failures during init are config-class
        # errors — return the missing-config exit code so the orchestrator
        # routes to "don't retry" rather than burning restart budget.
        log.exception("talker.daemon.lifecycle_error")
        exit_code = _MISSING_CONFIG_EXIT if "token" in str(exc).lower() else 1
    finally:
        log.info("talker.daemon.stopping")
        # Tear PTB down in reverse order — only what was set up.
        if polling:
            try:
                if app.updater and app.updater.running:
                    await app.updater.stop()
            except Exception:  # noqa: BLE001
                log.exception("talker.daemon.updater_stop_error")
        if started:
            try:
                if app.running:
                    await app.stop()
            except Exception:  # noqa: BLE001
                log.exception("talker.daemon.app_stop_error")
        if initialised:
            try:
                await app.shutdown()
            except Exception:  # noqa: BLE001
                log.exception("talker.daemon.app_shutdown_error")

        # Stop the sweeper.
        shutdown_event.set()
        sweeper_task.cancel()
        try:
            await sweeper_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

        # Close any still-open sessions so the record lands before exit.
        try:
            active_all = dict(state_mgr.state.get("active_sessions", {}) or {})
            for chat_id_str in list(active_all.keys()):
                raw_sess = active_all[chat_id_str]
                vault_root = raw_sess.get("_vault_path_root") or config.vault.path
                user_path = raw_sess.get("_user_vault_path") or (
                    config.primary_users[0] if config.primary_users else None
                )
                stt_model = raw_sess.get("_stt_model_used") or config.stt.model
                try:
                    session.close_session(
                        state_mgr,
                        vault_path_root=vault_root,
                        chat_id=int(chat_id_str),
                        reason="shutdown",
                        user_vault_path=user_path,
                        stt_model_used=stt_model,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "talker.daemon.shutdown_close_failed",
                        chat_id=chat_id_str,
                        error=str(exc),
                    )
        except Exception:  # noqa: BLE001
            log.exception("talker.daemon.shutdown_sweep_error")

        log.info("talker.daemon.stopped")

    return exit_code
