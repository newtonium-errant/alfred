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

from . import bot, heartbeat, session
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


def _load_system_prompt(skills_dir: Path, skill_bundle: str = "vault-talker") -> str:
    """Read ``<skill_bundle>/SKILL.md`` — the per-instance prompt.

    Multi-instance (Stage 3.5): the bundle name comes from
    ``telegram.instance.skill_bundle`` in config.yaml. Salem keeps the
    legacy default ``"vault-talker"``; KAL-LE ships with ``"vault-kalle"``.
    """
    skill_path = skills_dir / skill_bundle / "SKILL.md"
    if not skill_path.exists():
        log.warning(
            "talker.daemon.skill_missing",
            path=str(skill_path),
            skill_bundle=skill_bundle,
        )
        return ""
    return skill_path.read_text(encoding="utf-8")


def _apply_instance_templating(prompt: str, config: TalkerConfig) -> str:
    """Substitute ``{{instance_name}}`` / ``{{instance_canonical}}`` in the SKILL.

    Plain ``str.replace`` — two calls, zero deps. See the comment at the
    top of ``vault-talker/SKILL.md`` for the contract. The persona-only
    tokens are templated; product/codebase references to ``Alfred``
    (wikilinks, framework names, other-instance names like "Knowledge
    Alfred") stay literal because they aren't placeholder tokens.
    """
    return (
        prompt
        .replace("{{instance_name}}", config.instance.name)
        .replace("{{instance_canonical}}", config.instance.canonical)
    )


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
    system_prompt = _apply_instance_templating(
        _load_system_prompt(
            Path(skills_dir_str),
            skill_bundle=config.instance.skill_bundle,
        ),
        config,
    )
    vault_context_str = _build_vault_context_str(config)

    app = bot.build_app(
        config=config,
        state_mgr=state_mgr,
        anthropic_client=client,
        system_prompt=system_prompt,
        vault_context_str=vault_context_str,
        raw_config=raw,
    )

    # ---- Outbound-push transport --------------------------------------
    # The transport server runs as a sibling asyncio task inside the
    # talker daemon so it shares the event loop and can invoke the
    # Telegram bot directly (no IPC hop). The scheduler is another
    # sibling task that fires task/ remind_at reminders and drains the
    # server's pending queue.
    transport_app = None
    transport_state = None
    transport_config = None
    send_lock_map: dict[int, asyncio.Lock] = {}
    try:
        from alfred.transport.config import load_from_unified as load_transport
        from alfred.transport.server import build_app as build_transport_app
        from alfred.transport.state import TransportState
        from alfred.transport import scheduler as transport_scheduler
        from alfred.transport import server as transport_server_mod

        transport_config = load_transport(raw)
        transport_state = TransportState.create(transport_config.state.path)
        transport_state.load()

        async def _send_via_telegram(
            user_id: int, text: str, dedupe_key: str | None = None,
        ) -> list[int]:
            """Dispatch one Telegram message, enforcing a 250ms per-chat floor.

            Telegram rate-limits per-chat at ~1 msg/sec. We enforce a
            250ms floor under an asyncio.Lock keyed by chat_id so
            bursts (batch sends, scheduler drains) don't trip 429.
            """
            lock = send_lock_map.setdefault(user_id, asyncio.Lock())
            async with lock:
                try:
                    msg = await app.bot.send_message(
                        chat_id=user_id, text=text,
                    )
                except Exception as exc:  # noqa: BLE001
                    # 429 / retry_after surfaces as a TelegramError.
                    log.warning(
                        "talker.daemon.telegram_send_failed",
                        user_id=user_id,
                        error=str(exc),
                        response_summary=(
                            f"{exc.__class__.__name__}: {exc}"
                        ),
                    )
                    raise
                # 250ms inter-message floor per chat.
                await asyncio.sleep(0.25)
                return [msg.message_id]

        transport_app = build_transport_app(
            transport_config,
            transport_state,
            send_fn=_send_via_telegram,
        )
        log.info(
            "talker.daemon.transport_configured",
            host=transport_config.server.host,
            port=transport_config.server.port,
            peers=list(transport_config.auth.tokens.keys()),
        )

        # ---- Pending Items Queue (Phase 1) wiring --------------------
        # Aggregate path is needed on Salem (the receiver) so peer
        # pushes land in the right JSONL. Resolver callable is needed
        # on every instance with a ``pending_items`` block enabled so
        # Salem→peer dispatch can locate + execute. Both registrations
        # are no-ops on an instance whose ``pending_items`` block is
        # absent / disabled — the inbound handler returns 501 (no
        # resolver registered) which the Daily Sync dispatcher treats
        # as a terminal error rather than a retry.
        try:
            from alfred.pending_items.config import (
                load_from_unified as load_pending_items,
            )
            pending_items_config = load_pending_items(raw)
        except Exception:  # noqa: BLE001
            pending_items_config = None
        if pending_items_config is not None and pending_items_config.enabled:
            try:
                from alfred.transport.peer_handlers import (
                    register_pending_items_aggregate_path,
                    register_pending_items_resolve_callable,
                )
                # Aggregate path lives next to the local queue file —
                # Salem's queue + Salem's aggregate are different
                # files (the aggregate gets peer-pushed entries, the
                # queue gets Salem's own emissions); the Daily Sync
                # section provider reads BOTH and unions by id.
                aggregate_path = str(
                    Path(pending_items_config.queue_path).with_name(
                        "pending_items_aggregate.jsonl"
                    )
                )
                register_pending_items_aggregate_path(transport_app, aggregate_path)
                log.info(
                    "talker.daemon.pending_items_aggregate_registered",
                    path=aggregate_path,
                )

                # Resolver callable — closure over local queue + vault
                # + telegram user. Receives Salem→peer resolution
                # dispatches and runs the action plan locally.
                from alfred.pending_items.executor import resolve_local_item
                resolver_user_id = (
                    config.allowed_users[0] if config.allowed_users else 0
                )
                _pending_queue_path = pending_items_config.queue_path
                _pending_vault_path = Path(config.vault.path)

                async def _pending_items_resolver(
                    *,
                    item_id: str,
                    resolution: str,
                    resolved_at: str | None = None,
                    correlation_id: str = "",
                ) -> dict[str, Any]:
                    """Adapter: peer resolve → local executor.

                    The transport handler hands us the parsed body;
                    we run :func:`resolve_local_item` against the
                    instance's own queue and return the executor's
                    result dict. ``resolved_at`` is unused in Phase 1
                    (the executor stamps "now") but retained for the
                    Phase 3 audit trail.
                    """
                    return await resolve_local_item(
                        queue_path=_pending_queue_path,
                        item_id=item_id,
                        resolution_id=resolution,
                        vault_path=_pending_vault_path,
                        user_id=resolver_user_id,
                    )

                register_pending_items_resolve_callable(
                    transport_app, _pending_items_resolver,
                )
                log.info(
                    "talker.daemon.pending_items_resolver_registered",
                    queue_path=_pending_queue_path,
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "talker.daemon.pending_items_setup_failed"
                )

    except Exception:  # noqa: BLE001
        # Missing transport config is non-fatal — the talker still
        # handles Telegram chat even without the outbound push server.
        log.exception("talker.daemon.transport_setup_failed")
        transport_app = None

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
                closed_meta = session.check_timeouts_with_meta(
                    state_mgr,
                    datetime.now(timezone.utc),
                    config.session.gap_timeout_seconds,
                )
                if closed_meta:
                    log.info(
                        "talker.daemon.sweep_closed",
                        count=len(closed_meta),
                    )
                # Phase 2 deferred-enhancement #1: opportunistically
                # rename each closed record to use a substance-derived
                # slug. Off by default (config knob); failure-isolated.
                for meta in closed_meta:
                    try:
                        await session.maybe_apply_substance_slug(
                            state_mgr,
                            enabled=config.session.derive_slug_from_substance,
                            client=client,
                            model=config.anthropic.model,
                            vault_path_root=meta["vault_path_root"],
                            rel_path=meta["rel_path"],
                            transcript=meta["transcript"],
                            session_id=meta["session_id"],
                        )
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "talker.daemon.sweep_substance_slug_failed",
                            chat_id=meta.get("chat_id"),
                        )
            except Exception:  # noqa: BLE001
                log.exception("talker.daemon.sweep_error")

    sweeper_task = asyncio.create_task(_sweeper(), name="talker-sweeper")

    # ---- Idle-tick heartbeat ----------------------------------------------
    # Periodic ``talker.idle_tick`` log event so observers can distinguish
    # an idle daemon from a broken one. Default cadence 60s — see
    # ``heartbeat.py`` for the cadence rationale and the
    # "intentionally left blank" pattern background. When
    # ``telegram.idle_tick.enabled = false`` we skip task creation entirely
    # — no background work, no log noise.
    heartbeat_task: asyncio.Task | None = None
    if config.idle_tick.enabled:
        heartbeat_task = asyncio.create_task(
            heartbeat.run(
                interval_seconds=config.idle_tick.interval_seconds,
                shutdown_event=shutdown_event,
            ),
            name="talker-heartbeat",
        )
        log.info(
            "talker.daemon.heartbeat_started",
            interval_seconds=config.idle_tick.interval_seconds,
        )

    # ---- Transport server + scheduler tasks ------------------------------
    transport_server_task: asyncio.Task | None = None
    scheduler_task: asyncio.Task | None = None
    if transport_app is not None and transport_config is not None:
        from alfred.transport.server import run_server as run_transport_server
        from alfred.transport.scheduler import run as run_scheduler

        transport_server_task = asyncio.create_task(
            run_transport_server(
                transport_app,
                transport_config,
                shutdown_event=shutdown_event,
            ),
            name="transport-server",
        )
        scheduler_user_id = (
            config.allowed_users[0] if config.allowed_users else 0
        )
        if scheduler_user_id:
            scheduler_task = asyncio.create_task(
                run_scheduler(
                    transport_config,
                    transport_state,
                    _send_via_telegram,
                    Path(config.vault.path),
                    scheduler_user_id,
                    shutdown_event=shutdown_event,
                ),
                name="transport-scheduler",
            )
        else:
            log.warning(
                "talker.daemon.scheduler_no_user",
                detail="allowed_users empty — scheduler not started",
            )

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

        # Stop the transport server + scheduler + heartbeat if they were
        # started.
        for t in (transport_server_task, scheduler_task, heartbeat_task):
            if t is None:
                continue
            t.cancel()
            try:
                await t
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
                # Snapshot for post-close substance-slug rename — same
                # pattern as ``check_timeouts_with_meta`` since the
                # active dict is popped during ``close_session``.
                transcript_snap = list(raw_sess.get("transcript") or [])
                session_id_snap = raw_sess.get("session_id", "")
                try:
                    rel_path = session.close_session(
                        state_mgr,
                        vault_path_root=vault_root,
                        chat_id=int(chat_id_str),
                        reason="shutdown",
                        user_vault_path=user_path,
                        stt_model_used=stt_model,
                        session_type=raw_sess.get("_session_type", "note"),
                        continues_from=raw_sess.get("_continues_from"),
                        # Per-instance session-save shape — prefer the
                        # stash, fall back to live config so a session
                        # opened before the field was stashed still
                        # closes with the correct shape on shutdown.
                        tool_set=(
                            raw_sess.get("_tool_set")
                            or config.instance.tool_set
                            or ""
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "talker.daemon.shutdown_close_failed",
                        chat_id=chat_id_str,
                        error=str(exc),
                    )
                    continue
                # Phase 2 deferred-enhancement #1 — same hook as the
                # in-flight sweeper. Best-effort; the session record is
                # already on disk by the time this runs.
                try:
                    await session.maybe_apply_substance_slug(
                        state_mgr,
                        enabled=config.session.derive_slug_from_substance,
                        client=client,
                        model=config.anthropic.model,
                        vault_path_root=vault_root,
                        rel_path=rel_path,
                        transcript=transcript_snap,
                        session_id=session_id_snap,
                    )
                except Exception:  # noqa: BLE001
                    log.exception(
                        "talker.daemon.shutdown_substance_slug_failed",
                        chat_id=chat_id_str,
                    )
        except Exception:  # noqa: BLE001
            log.exception("talker.daemon.shutdown_sweep_error")

        log.info("talker.daemon.stopped")

    return exit_code
