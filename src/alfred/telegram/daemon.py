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
from typing import Any, Callable

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


def _reconcile_left_collapse_group(
    *,
    client,
    config,
    vault_path,
    rel_path: str,
    pre_fm: dict,
    post_fm: dict,
    intended_on: bool,
    correlation_id: str = "",
) -> bool:
    """Reconcile the collapse group an event just LEFT (the pre-edit-fm fix).

    The update hook receives the POST-edit fm, so when an event's collapse-group
    identity ``(gcal_collapse_key, date)`` changes — key removed, key changed,
    or a date edit moving it to a different ``(key, date)`` bucket — the post-fm
    alone can't recompute the group it left (the old key/date is gone). With the
    pre-edit fm now plumbed through, recompute the OLD group IMMEDIATELY so the
    survivors get their fresh umbrella in the same edit. Replaces the NOTE-F
    deferred-reconcile WARN stopgap: the transient under-projection gap is GONE.

    Fires FIRST (before the caller re-scopes / re-syncs the leaver's own entry):
    survivors are reprojected before the leaver's entry sheds the umbrella, so
    the sub-second transient is a benign DOUBLE (two umbrellas briefly), never a
    GAP (vanished sessions on a live medical calendar).

    Group identity is compared on the NORMALIZED day (``_coerce_event_date``),
    not the raw ``date``/``start`` string, so a time-only edit within the same
    day does NOT count as a group change (no spurious cross-group reconcile).

    Own try/except: a GCal failure reconciling the OLD group must never abort
    the caller's NEW-state reconcile or the bubbled-up result — it's a logged
    side-effect. Returns True iff a group change was detected (the old group was
    reconciled). Module-level (not inline in the daemon closure) so the
    ``gcal.collapse_group_changed`` breadcrumb is unit-testable via capture_logs
    driving production code — per ``feedback_log_emission_test_pattern.md``.
    """
    from alfred.integrations.gcal_sync import (
        _coerce_event_date,
        resolve_collapse_key,
        sync_collapse_group,
    )

    old_key = resolve_collapse_key(pre_fm)
    new_key = resolve_collapse_key(post_fm)
    old_day = _coerce_event_date(pre_fm)
    new_day = _coerce_event_date(post_fm)
    # Only the group the member LEFT needs reconciling here — so an old key must
    # exist. A pure key-ADD (no old key) is handled by the caller's new-group
    # branch. Same (key, day) → no group change → nothing to do.
    if not old_key or (old_key, old_day) == (new_key, new_day):
        return False

    log.info(
        "gcal.collapse_group_changed",
        rel_path=rel_path,
        old_key=old_key,
        new_key=new_key,
        old_date=str(pre_fm.get("date") or pre_fm.get("start") or ""),
        new_date=str(post_fm.get("date") or post_fm.get("start") or ""),
        correlation_id=correlation_id,
    )
    try:
        sync_collapse_group(
            client=client,
            config=config,
            vault_path=vault_path,
            collapse_key=old_key,
            group_date=pre_fm.get("date") or pre_fm.get("start"),
            intended_on=intended_on,
            correlation_id=correlation_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "gcal.collapse_old_group_reconcile_failed",
            rel_path=rel_path,
            old_key=old_key,
            old_date=str(pre_fm.get("date") or pre_fm.get("start") or ""),
            error=str(exc),
            correlation_id=correlation_id,
        )
    return True


# --- Validation -----------------------------------------------------------


def _missing_config_reasons(
    config: TalkerConfig, *, web_only: bool = False
) -> list[str]:
    """Return human-readable reasons why config is incomplete (or []).

    Anything that would crash the daemon on first use counts — a missing
    bot token leaves us unable to build the Application; an empty allowlist
    means no one can talk to us; missing API keys make the first message
    500 the user.

    ``web_only`` (bit d): a PWA-only instance mounts the web surface with NO
    Telegram bot. When set, the Telegram-specific prerequisites (``bot_token``
    / ``allowed_users`` / ``stt.api_key``) are NOT required — but ``vault.path``
    and ``anthropic.api_key`` (the agent needs both) stay required, and
    ``instance.name`` still fails loud upstream at ``load_from_unified``.
    Default ``False`` → today's five checks byte-for-byte (order preserved).
    """
    reasons: list[str] = []
    if not web_only and not config.bot_token:
        reasons.append("telegram.bot_token is empty")
    if not web_only and not config.allowed_users:
        reasons.append("telegram.allowed_users is empty")
    if not config.anthropic.api_key:
        reasons.append("telegram.anthropic.api_key is empty")
    if not web_only and not config.stt.api_key:
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

    Reads fresh from disk each call. The per-turn hot-reload wiring
    (see ``build_system_prompt_provider`` below) calls this on every
    Telegram update — i.e. every user message — so SKILL.md edits
    take effect without a daemon restart — closes the "same-cycle
    SKILL ship" gap from QA 2026-05-04 (Hypatia ran a conversation
    with the old SKILL after the new SKILL committed but before
    restart). The provider is invoked from ``bot.update_handler``
    which python-telegram-bot fires per inbound message, NOT once
    per logical conversation; the historical commit message
    ``be53673`` and earlier comments named this "per-conversation"
    inaccurately. The per-turn read is the actual behaviour and is
    fine performance-wise (see file-system cost note below).

    File-system cost: ~1-3 KB SKILL files, OS page cache hot — read
    is sub-millisecond. Negligible vs the per-turn Anthropic API
    round-trip.
    """
    skill_path = skills_dir / skill_bundle / "SKILL.md"
    if not skill_path.exists():
        log.warning(
            "talker.daemon.skill_missing",
            path=str(skill_path),
            skill_bundle=skill_bundle,
        )
        return ""
    text = skill_path.read_text(encoding="utf-8")
    # Per-load diagnostic so an operator can verify the hot-reload
    # IS happening on every user turn. Debug level so it doesn't
    # spam INFO-level logs in production; structlog filtering
    # decides the surface.
    log.debug(
        "talker.conversation.skill_md_loaded",
        path=str(skill_path),
        skill_bundle=skill_bundle,
        char_count=len(text),
    )
    return text


def build_system_prompt_provider(
    skills_dir: Path,
    config: TalkerConfig,
) -> Callable[[], str]:
    """Return a zero-arg callable that reads SKILL.md fresh + templates it.

    Used by ``bot.build_app`` to wire per-turn SKILL.md hot-reload —
    instead of stashing a static string in ``bot_data``, the bot
    stashes this provider and invokes it per turn (i.e. every
    inbound Telegram message). SKILL edits on disk take effect on
    the next user turn; no daemon restart needed.

    Closure captures ``skills_dir`` + ``config`` (both immutable for
    the daemon's lifetime). Templating reads ``config.instance.name``
    / ``canonical`` which don't change across the daemon's life
    either, so the only thing that varies between calls is the
    SKILL.md content itself.

    Errors degrade to empty string per ``_load_system_prompt``'s
    contract (file-missing → warning + empty); the bot's downstream
    Anthropic call still works on an empty system prompt (just with
    no instance-specific guidance — better than crashing the turn).
    """
    skill_bundle = config.instance.skill_bundle

    def _provider() -> str:
        return _apply_instance_templating(
            _load_system_prompt(skills_dir, skill_bundle=skill_bundle),
            config,
        )

    return _provider


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


async def _close_open_sessions_on_shutdown(
    state_mgr: StateManager,
    config: TalkerConfig,
    client: Any,
) -> list[str]:
    """Close every still-open session so the records land before exit.

    Extracted from ``run()``'s finally block so the call-site contract
    is regression-pinnable. Behaviour matches the timeout sweeper's
    close-call contract — including ``pushback_level=
    raw_sess.get("_pushback_level")``, which the shutdown path omitted
    pre-2026-06-12: shutdown-closed records got ``telegram.
    pushback_level: null`` while the other three close paths (bot
    ``/end``, timeout sweeper, startup sweep) all read the stash.

    Returns the vault-relative paths of the records written.
    """
    closed_paths: list[str] = []
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
        # active dict is popped during ``close_session``. The
        # helper encodes the fields the hook needs in one place.
        post_close_snap = session._snapshot_for_post_close(raw_sess)
        transcript_snap = post_close_snap["transcript"]
        session_id_snap = post_close_snap["session_id"]
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
                pushback_level=raw_sess.get("_pushback_level"),
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
        closed_paths.append(rel_path)
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
    return closed_paths


def _classify_transport_task_outcome(
    *,
    cancelled: bool,
    exc: BaseException | None,
    shutdown_requested: bool,
) -> str:
    """Pure decision for the transport-server task supervisor.

    Returns one of:
      * ``"silent"`` — expected completion (the task was CANCELLED on clean
        shutdown, OR ``run_server`` returned because a shutdown WAS requested).
        No log, no shutdown trigger.
      * ``"died_exception"`` — the task raised (``run_server`` hit the
        genuinely-fatal bind path: zero bound / loopback failed). Caller logs
        ``transport.server.task_died`` with the exception + triggers shutdown.
      * ``"died_returned"`` — the task returned WITHOUT an exception and WITHOUT
        a shutdown request → the transport silently stopped while the daemon
        believes it's healthy. Caller logs + triggers shutdown.

    Extracted from the supervisor closure so the four-branch decision is
    unit-testable (the "a dead transport never looks idle" guarantee). The two
    death branches drive systemd restart via the daemon's shutdown_event.
    """
    if cancelled:
        return "silent"
    if exc is not None:
        return "died_exception"
    if shutdown_requested:
        return "silent"
    return "died_returned"


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
    # Pull rotation kwargs from the unified ``logging`` block so the
    # talker honors the same per-file size cap as every other daemon.
    # Without this, ``data/talker.log`` would grow unbounded while
    # curator/janitor/distiller rotate.
    from alfred.common.logging_handler import extract_rotation_config
    _log_cfg = raw.get("logging", {}) if isinstance(raw, dict) else {}
    _max_bytes, _backup_count = extract_rotation_config(_log_cfg)
    setup_logging(
        level=config.logging.level,
        log_file=config.logging.file,
        suppress_stdout=suppress_stdout,
        max_bytes=_max_bytes,
        backup_count=_backup_count,
    )

    # Web-only mode (bit d): a PWA-only instance may run the web surface with
    # no Telegram bot. Opt-in + EXPLICIT — web.enabled AND web.web_only — so a
    # standard instance (either flag absent) keeps every Telegram prerequisite
    # required, byte-for-byte. Any web-config parse issue fails toward the
    # strict default (web_only stays False → today's behavior).
    web_only = False
    try:
        from alfred.web.config import load_from_unified as _load_web_cfg
        _wc_probe = _load_web_cfg(raw)
        web_only = bool(_wc_probe.enabled and _wc_probe.web_only)
    except Exception:  # noqa: BLE001 — never let a web-config issue block boot
        log.exception("talker.daemon.web_only_probe_failed")
        web_only = False

    reasons = _missing_config_reasons(config, web_only=web_only)
    if reasons:
        log.error("talker.daemon.missing_config", reasons=reasons)
        return _MISSING_CONFIG_EXIT

    if web_only and not config.bot_token:
        # Intentionally-left-blank: an explicit "running without Telegram"
        # signal so a bot-less daemon is distinguishable from a broken one.
        log.info(
            "talker.daemon.web_only_mode",
            detail=(
                "web.web_only=true and no bot_token — Telegram bot disabled; "
                "serving the web surface + transport only"
            ),
        )

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

    # Dangling-tool_use detector (P2 from QA 2026-05-04). Walks every
    # surviving active session's transcript; logs a warning per
    # assistant turn whose tool_use ids lack matching tool_result
    # blocks in the next user turn. Runs AFTER resolve_on_startup so
    # we don't waste a check on sessions we just closed; runs BEFORE
    # the bot starts handling new turns so the diagnostic log is
    # available to the operator before the LLM gets a chance to
    # parrot the heal's "interrupted before completing" wording.
    from .conversation import detect_dangling_tool_use_at_startup
    detect_dangling_tool_use_at_startup(state_mgr, now=now)

    client = anthropic.AsyncAnthropic(api_key=config.anthropic.api_key)
    # Build a system-prompt PROVIDER instead of a frozen string. The
    # provider is invoked per turn (i.e. every inbound Telegram
    # message in update_handler) so SKILL.md edits take effect on
    # the next user turn without a daemon restart — closes the
    # same-cycle SKILL ship gap from QA 2026-05-04.
    system_prompt_provider = build_system_prompt_provider(
        Path(skills_dir_str), config,
    )
    # One-shot boot-time invocation surfaces SKILL-missing warnings +
    # logs the initial char_count so the operator can confirm the
    # SKILL loaded cleanly at startup. The provider's per-turn
    # invocations carry the same diagnostic via debug-level logs.
    initial_prompt = system_prompt_provider()
    log.info(
        "talker.daemon.system_prompt_provider_ready",
        skill_bundle=config.instance.skill_bundle,
        initial_char_count=len(initial_prompt),
    )
    vault_context_str = _build_vault_context_str(config)

    # Web-only mode (bit d): with no bot_token there is no Telegram
    # Application to build — ``Application.builder().token("").build()`` raises
    # InvalidToken, so we skip the build entirely. Every ``app.*`` touchpoint
    # below is guarded on ``app is not None``. When a bot_token IS present (the
    # only path a standard instance ever takes, since an empty token without
    # web_only already early-returned above) this is byte-for-byte unchanged.
    app = None
    if config.bot_token:
        app = bot.build_app(
            config=config,
            state_mgr=state_mgr,
            anthropic_client=client,
            system_prompt_provider=system_prompt_provider,
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
        from alfred.transport.server import (
            build_app as build_transport_app,
            wire_transport_app,
        )
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
            if app is None:
                # Web-only mode (bit d) — no Telegram bot. Not reached in
                # practice (empty allowed_users → no scheduler; web-only
                # instances don't push to Telegram), but guard so a stray
                # call no-ops loudly instead of raising on ``app.bot``.
                log.warning(
                    "talker.daemon.telegram_send_skipped",
                    detail="web-only mode (no bot_token)",
                    user_id=user_id,
                )
                return []
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

        # Build the bare app — no resources wired yet.
        transport_app = build_transport_app(
            transport_config,
            transport_state,
        )

        # ---- Pending Items Queue (Phase 1) wiring inputs -------------
        # Aggregate path is needed on Salem (the receiver) so peer
        # pushes land in the right JSONL. Resolver callable is needed
        # on every instance with a ``pending_items`` block enabled so
        # Salem→peer dispatch can locate + execute. Both inputs default
        # to ``None`` on an instance whose ``pending_items`` block is
        # absent / disabled — wire_transport_app skips that registrar
        # and the inbound handler returns 501 (no resolver registered)
        # which the Daily Sync dispatcher treats as a terminal error
        # rather than a retry.
        pending_items_aggregate_path: str | None = None
        pending_items_resolver_fn = None
        try:
            from alfred.pending_items.config import (
                load_from_unified as load_pending_items,
            )
            pending_items_config = load_pending_items(raw)
        except Exception:  # noqa: BLE001
            pending_items_config = None
        if pending_items_config is not None and pending_items_config.enabled:
            try:
                # Aggregate path lives next to the local queue file —
                # Salem's queue + Salem's aggregate are different
                # files (the aggregate gets peer-pushed entries, the
                # queue gets Salem's own emissions); the Daily Sync
                # section provider reads BOTH and unions by id.
                pending_items_aggregate_path = str(
                    Path(pending_items_config.queue_path).with_name(
                        "pending_items_aggregate.jsonl"
                    )
                )

                # Resolver callable — closure over local queue + vault
                # + telegram user. Receives Salem→peer resolution
                # dispatches and runs the action plan locally.
                from alfred.pending_items.executor import resolve_local_item
                # VERA MVP: allowed_users entries are AllowedUser(id, role);
                # ``getattr(..., "id", entry)`` tolerates a bare-int entry
                # from a direct-construct fixture (falls back to the int).
                _first_user = (
                    config.allowed_users[0] if config.allowed_users else 0
                )
                resolver_user_id = getattr(_first_user, "id", _first_user)
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

                pending_items_resolver_fn = _pending_items_resolver
            except Exception:  # noqa: BLE001
                log.exception(
                    "talker.daemon.pending_items_setup_failed"
                )

        # ---- GCal integration (Phase A+) -----------------------------
        # The Google Calendar adapter is opt-in per instance via the
        # top-level ``gcal:`` config block. Default-disabled — Hypatia /
        # KAL-LE leave it off; Salem (or future V.E.R.A.) opt in.
        #
        # Both client + config must be wired together. Loading the
        # config is cheap; constructing the GCalClient is also cheap
        # (no network, no token-load until first API call). If the
        # google-* libs aren't installed, the client construction
        # itself succeeds — the failure surfaces on first list_events
        # / create_event call as GCalNotInstalled, which the conflict-
        # check + sync paths handle with graceful degradation.
        gcal_client = None
        gcal_config = None
        # P2-4: sentinel flag — flips True the moment we see
        # ``gcal.enabled: true`` in config, BEFORE client construction.
        # If construction then raises, the flag survives so the
        # transport handler can log warnings (not debug) at every skip
        # site, surfacing the silent feature-degradation to the operator.
        gcal_intended_on = False
        try:
            from alfred.integrations.gcal_config import (
                load_from_unified as load_gcal,
            )
            gcal_config_candidate = load_gcal(raw)
            if gcal_config_candidate.enabled:
                # Flag the intent FIRST. Client construction below can
                # still fail (missing creds, malformed scopes); the
                # sentinel persists so handlers know the operator opted
                # in even when the wiring is half-up.
                gcal_intended_on = True
                from alfred.integrations.gcal import GCalClient
                gcal_client = GCalClient(
                    credentials_path=gcal_config_candidate.credentials_path,
                    token_path=gcal_config_candidate.token_path,
                    scopes=gcal_config_candidate.scopes,
                )
                gcal_config = gcal_config_candidate
                log.info(
                    "talker.daemon.gcal_enabled",
                    alfred_calendar_id_set=bool(
                        gcal_config.alfred_calendar_id
                    ),
                    primary_calendar_id_set=bool(
                        gcal_config.primary_calendar_id
                    ),
                    calendar_label=gcal_config.alfred_calendar_label,
                    time_zone=gcal_config.default_time_zone or "(calendar default)",
                )

                # ---- Vault-ops event hooks (Phase A+ commit 2/3) -----
                # Register sync closures so vault_create / vault_edit /
                # vault_delete on event records mirror to GCal regardless
                # of caller (Telegram chat, instructor executor,
                # daily-sync dispatcher, future agents). The cross-
                # instance event-propose handler doesn't go through
                # vault_create — it writes files directly — so its sync
                # path is unchanged (still uses the shared
                # ``sync_event_create_to_gcal`` via the
                # ``_sync_event_to_gcal`` shim, no double-fire).
                from alfred.integrations.gcal_sync import (
                    resolve_collapse_key,
                    resolve_gcal_title,
                    resolve_sync_policy,
                    sync_collapse_group,
                    sync_event_cancellation_to_gcal,
                    sync_event_create_to_gcal,
                    sync_event_delete_to_gcal,
                    sync_event_update_to_gcal,
                )
                from alfred.vault.ops import (
                    register_event_create_hook,
                    register_event_delete_hook,
                    register_event_update_hook,
                )

                # Capture the daemon-bound client + config + sentinel
                # in closure scope so the hook signature stays
                # registry-friendly (no per-fire kwarg threading).
                _bound_client = gcal_client
                _bound_config = gcal_config
                _bound_intended_on = gcal_intended_on

                def _on_event_created(vault_path_, rel_path, fm):
                    """Vault-create hook → push event to GCal + writeback ID.

                    Returns the sync function's result dict so
                    ``_fire_create_hooks`` can bubble it up to
                    ``vault_create`` for ``gcal_sync`` surfacing in the
                    LLM tool_result. Early-bail branches (no datetimes,
                    unparseable datetimes) return ``None`` — those map
                    to "no GCal action attempted" in
                    :func:`alfred.vault.ops._extract_gcal_sync_status`,
                    which omits the ``gcal_sync`` key entirely.
                    """
                    from datetime import datetime as _dt
                    # §3 collapse: a keyed member routes to the group
                    # coordinator (ONE umbrella entry) instead of the plain
                    # per-event create. Absent key → plain path below.
                    collapse_key = resolve_collapse_key(fm)
                    if collapse_key:
                        return sync_collapse_group(
                            client=_bound_client,
                            config=_bound_config,
                            vault_path=vault_path_,
                            collapse_key=collapse_key,
                            group_date=fm.get("date") or fm.get("start"),
                            intended_on=_bound_intended_on,
                            correlation_id=str(fm.get("correlation_id") or ""),
                        )
                    start_raw = fm.get("start")
                    end_raw = fm.get("end")
                    if not start_raw or not end_raw:
                        # No times → can't push to GCal. The sync
                        # function would also reject this, but bail
                        # early to skip the import + mock-API roundtrip.
                        log.debug(
                            "talker.daemon.gcal_create_hook_skipped",
                            reason="no_start_or_end",
                            rel_path=rel_path,
                        )
                        return None
                    try:
                        start_dt = _dt.fromisoformat(str(start_raw))
                        end_dt = _dt.fromisoformat(str(end_raw))
                    except Exception:  # noqa: BLE001
                        log.warning(
                            "talker.daemon.gcal_create_hook_bad_time",
                            rel_path=rel_path,
                            start=str(start_raw)[:40],
                            end=str(end_raw)[:40],
                        )
                        return None
                    file_path = Path(vault_path_) / rel_path
                    resolved_title, title_source = resolve_gcal_title(fm)
                    return sync_event_create_to_gcal(
                        client=_bound_client,
                        config=_bound_config,
                        intended_on=_bound_intended_on,
                        file_path=file_path,
                        title=resolved_title,
                        description=str(fm.get("summary") or ""),
                        start_dt=start_dt,
                        end_dt=end_dt,
                        correlation_id=str(fm.get("correlation_id") or ""),
                        title_source=title_source,
                        sync_policy=resolve_sync_policy(fm),
                    )

                def _on_event_updated(
                    vault_path_, rel_path, fm, fields_changed, pre_fm,
                ):
                    """Vault-edit hook — branches:

                      * collapse-group identity ``(gcal_collapse_key,
                        date)`` changed pre→post → reconcile the OLD
                        group FIRST (pre-edit-fm fix): survivors of the
                        group the member LEFT get their fresh umbrella
                        immediately — no transient under-projection gap
                      * post-edit ``gcal_collapse_key`` present → route
                        to the group coordinator (NEW group: recompute /
                        adopt / create the umbrella)
                      * status newly set to ``cancelled`` AND
                        gcal_event_id present → CANCEL: delete the GCal
                        mirror (or patch status=cancelled if the record
                        carries ``gcal_keep_on_cancel: true``); on
                        delete, clear ``gcal_event_id`` from the vault
                        record so a future re-confirm starts fresh
                      * gcal_event_id absent BUT start+end present →
                        PROMOTE: push as fresh create, writeback ID
                        (the "first-sync via edit" case — common when
                        Salem back-fills datetimes onto an event that
                        predates Phase A+, or when a vault_create
                        landed a record without times that subsequently
                        got them via vault_edit)
                      * collapse-group ex-PRIMARY un-keyed to standalone
                        (old key, no new key, still holds the umbrella
                        id) → RE-SCOPE: PATCH the entry to this event's
                        OWN name+times, shedding the umbrella identity
                      * gcal_event_id present → PATCH (regular field
                        edit on an existing mirror)
                      * otherwise (no datetimes, no cancel) → no-op;
                        the GCal sync functions short-circuit on missing
                        fields too, but bailing here saves the import +
                        log noise

                    The promotion branch invokes
                    ``sync_event_create_to_gcal`` rather than the update
                    function. ``sync_event_create_to_gcal`` is safe to
                    call from a non-create context: the only side
                    effects are (1) ``client.create_event`` (which has
                    no risk of duplicate since by definition no
                    gcal_event_id was set, so no prior mirror exists)
                    and (2) frontmatter writeback of the new ID
                    (which is exactly what we want).

                    The cancel branch goes FIRST — a cancellation edit
                    on a synced event should never fall into the patch
                    path (which would try to PATCH the event title /
                    times after we've decided to delete it). The cancel
                    branch is also gated on ``"status" in fields_changed``
                    so an edit that doesn't touch status (but the record
                    was already cancelled from a prior edit) doesn't
                    re-fire the cancel sync.

                    Returns the sync function's result dict so
                    ``_fire_update_hooks`` can bubble it up to
                    ``vault_edit`` for ``gcal_sync`` surfacing in the
                    LLM tool_result. No-op branches (record had no
                    gcal_event_id and no datetimes; bad-time parse on
                    promote) return ``None`` — see
                    :func:`alfred.vault.ops._extract_gcal_sync_status`
                    for the contract.
                    """
                    from datetime import datetime as _dt
                    # Pre-edit-fm fix: reconcile the group the member LEFT
                    # FIRST (before re-scoping/re-syncing its own entry), so a
                    # collapse-group-identity change (key removed/changed, or a
                    # date edit moving it between (key,date) groups) reprojects
                    # the survivors immediately — no transient gap. Logged
                    # side-effect (own try/except in the helper); the bubbled-up
                    # result is the NEW-state action below.
                    _reconcile_left_collapse_group(
                        client=_bound_client,
                        config=_bound_config,
                        vault_path=vault_path_,
                        rel_path=rel_path,
                        pre_fm=pre_fm,
                        post_fm=fm,
                        intended_on=_bound_intended_on,
                        correlation_id=str(fm.get("correlation_id") or ""),
                    )
                    # §3 collapse: any edit of a keyed member (incl. a cancel,
                    # or the edit that ADDS the key) routes to the group
                    # coordinator — idempotent recompute of the NEW umbrella.
                    # Absent key → the plain cancel/promote/patch paths below.
                    collapse_key = resolve_collapse_key(fm)
                    if collapse_key:
                        return sync_collapse_group(
                            client=_bound_client,
                            config=_bound_config,
                            vault_path=vault_path_,
                            collapse_key=collapse_key,
                            group_date=fm.get("date") or fm.get("start"),
                            intended_on=_bound_intended_on,
                            correlation_id=str(fm.get("correlation_id") or ""),
                        )
                    # Un-key detection for the RE-SCOPE branch below: the event
                    # had a collapse key pre-edit and has none now. An ex-PRIMARY
                    # (still holds the umbrella ``gcal_event_id``) must shed the
                    # "<key> — N sessions" umbrella identity and become its own
                    # standalone entry. (Ex-SECONDARY un-key has no id → the
                    # PROMOTION path creates a fresh standalone entry instead.)
                    was_unkeyed = bool(resolve_collapse_key(pre_fm)) and not collapse_key
                    gcal_event_id = str(fm.get("gcal_event_id") or "")
                    start_raw = fm.get("start")
                    end_raw = fm.get("end")

                    # CANCEL path — vault_edit set status=cancelled this
                    # edit, AND we have a GCal mirror to act on.
                    status_now = str(fm.get("status") or "").strip().lower()
                    if (
                        "status" in fields_changed
                        and status_now == "cancelled"
                        and gcal_event_id
                    ):
                        keep_on_cancel = bool(fm.get("gcal_keep_on_cancel"))
                        file_path = Path(vault_path_) / rel_path
                        return sync_event_cancellation_to_gcal(
                            client=_bound_client,
                            config=_bound_config,
                            intended_on=_bound_intended_on,
                            file_path=file_path,
                            gcal_event_id=gcal_event_id,
                            keep_on_cancel=keep_on_cancel,
                            correlation_id=str(fm.get("correlation_id") or ""),
                            sync_policy=resolve_sync_policy(fm),
                        )

                    # Promotion path — first-sync via edit.
                    if not gcal_event_id and start_raw and end_raw:
                        try:
                            start_dt = _dt.fromisoformat(str(start_raw))
                            end_dt = _dt.fromisoformat(str(end_raw))
                        except Exception:  # noqa: BLE001
                            log.warning(
                                "talker.daemon.gcal_promote_hook_bad_time",
                                rel_path=rel_path,
                                start=str(start_raw)[:40],
                                end=str(end_raw)[:40],
                            )
                            return None
                        log.info(
                            "gcal.sync_promoted_to_create",
                            rel_path=rel_path,
                            reason=(
                                "vault_edit added start+end to a record "
                                "that had no gcal_event_id — first-sync "
                                "via edit"
                            ),
                            correlation_id=str(fm.get("correlation_id") or ""),
                        )
                        file_path = Path(vault_path_) / rel_path
                        resolved_title, title_source = resolve_gcal_title(fm)
                        return sync_event_create_to_gcal(
                            client=_bound_client,
                            config=_bound_config,
                            intended_on=_bound_intended_on,
                            file_path=file_path,
                            title=resolved_title,
                            description=str(fm.get("summary") or ""),
                            start_dt=start_dt,
                            end_dt=end_dt,
                            correlation_id=str(fm.get("correlation_id") or ""),
                            title_source=title_source,
                            # Thread the per-event policy — the PROMOTION path
                            # (date-only event later gains start/end) must honour
                            # gcal_sync:none like the other create-like branches.
                            # Was unthreaded since §2 (deeper indent missed the
                            # §2 replace_all); a remind-only event that gained a
                            # time would otherwise LEAK onto GCal.
                            sync_policy=resolve_sync_policy(fm),
                        )

                    # No-op path — never synced AND still no datetimes.
                    if not gcal_event_id:
                        log.debug(
                            "talker.daemon.gcal_update_hook_skipped",
                            reason="no_gcal_event_id_and_no_times",
                            rel_path=rel_path,
                        )
                        return None

                    # RE-SCOPE path — the group's ex-PRIMARY was un-keyed to
                    # standalone (had a key, has none now, still holds the
                    # umbrella id). The OLD-group reconcile above already gave
                    # the survivors a fresh umbrella; now shed THIS entry's
                    # "<key> — N sessions" umbrella identity by PATCHing it to
                    # the event's OWN name + times, so it stops representing the
                    # group. Force title/start/end even though only the key
                    # changed — that's the whole point of the re-scope. (A
                    # re-key keeps a new key → handled by the NEW-group branch
                    # and never reaches here.)
                    if was_unkeyed:
                        resolved_title, title_source = resolve_gcal_title(fm)
                        rescope_start = None
                        rescope_end = None
                        if start_raw:
                            try:
                                rescope_start = _dt.fromisoformat(str(start_raw))
                            except Exception:  # noqa: BLE001
                                pass
                        if end_raw:
                            try:
                                rescope_end = _dt.fromisoformat(str(end_raw))
                            except Exception:  # noqa: BLE001
                                pass
                        log.info(
                            "gcal.collapse_unkey_rescope",
                            rel_path=rel_path,
                            gcal_event_id=gcal_event_id,
                            correlation_id=str(fm.get("correlation_id") or ""),
                        )
                        return sync_event_update_to_gcal(
                            client=_bound_client,
                            config=_bound_config,
                            intended_on=_bound_intended_on,
                            gcal_event_id=gcal_event_id,
                            title=resolved_title,
                            description=str(fm.get("summary") or ""),
                            start_dt=rescope_start,
                            end_dt=rescope_end,
                            correlation_id=str(fm.get("correlation_id") or ""),
                            title_source=title_source,
                            sync_policy=resolve_sync_policy(fm),
                        )

                    # PATCH path — existing GCal mirror, normal update.
                    # Only patch fields that actually changed AND are
                    # GCal-relevant. ``fields_changed`` is a flat list
                    # of frontmatter keys + possibly "body" — the GCal
                    # patch surface is title / description / start / end.
                    #
                    # Title trigger: any of ``gcal_title`` / ``title`` /
                    # ``name`` in ``fields_changed`` re-resolves via
                    # ``resolve_gcal_title``. The decoupling means an
                    # operator setting ``gcal_title`` on a record that
                    # already has ``title``/``name`` should patch the
                    # GCal entry to the override even if title/name
                    # didn't change.
                    title_changed = (
                        "gcal_title" in fields_changed
                        or "title" in fields_changed
                        or "name" in fields_changed
                    )
                    if title_changed:
                        resolved_title, title_source = resolve_gcal_title(fm)
                        title = resolved_title
                    else:
                        title = None
                        title_source = None
                    description = (
                        str(fm.get("summary") or "")
                        if "summary" in fields_changed
                        else None
                    )
                    start_dt = None
                    end_dt = None
                    if "start" in fields_changed and fm.get("start"):
                        try:
                            start_dt = _dt.fromisoformat(str(fm["start"]))
                        except Exception:  # noqa: BLE001
                            pass
                    if "end" in fields_changed and fm.get("end"):
                        try:
                            end_dt = _dt.fromisoformat(str(fm["end"]))
                        except Exception:  # noqa: BLE001
                            pass
                    return sync_event_update_to_gcal(
                        client=_bound_client,
                        config=_bound_config,
                        intended_on=_bound_intended_on,
                        gcal_event_id=gcal_event_id,
                        title=title,
                        description=description,
                        start_dt=start_dt,
                        end_dt=end_dt,
                        correlation_id=str(fm.get("correlation_id") or ""),
                        title_source=title_source,
                        sync_policy=resolve_sync_policy(fm),
                    )

                def _on_event_deleted(vault_path_, rel_path, pre_delete_fm):
                    """Vault-delete hook → remove the GCal mirror.

                    Returns the sync function's result dict so
                    ``_fire_delete_hooks`` can bubble it up to
                    ``vault_delete`` for ``gcal_sync`` surfacing in the
                    LLM tool_result.
                    """
                    gcal_event_id = str(pre_delete_fm.get("gcal_event_id") or "")
                    # §3 collapse: deleting a keyed member recomputes the
                    # group (the file is already gone from the scan). If the
                    # deleted member was the PRIMARY, pass its id as
                    # orphan_event_id so the coordinator PROMOTES it onto a
                    # surviving member (or tears the entry down if last).
                    collapse_key = resolve_collapse_key(pre_delete_fm)
                    if collapse_key:
                        return sync_collapse_group(
                            client=_bound_client,
                            config=_bound_config,
                            vault_path=vault_path_,
                            collapse_key=collapse_key,
                            group_date=(
                                pre_delete_fm.get("date")
                                or pre_delete_fm.get("start")
                            ),
                            intended_on=_bound_intended_on,
                            correlation_id=str(
                                pre_delete_fm.get("correlation_id") or ""
                            ),
                            orphan_event_id=gcal_event_id,
                        )
                    return sync_event_delete_to_gcal(
                        client=_bound_client,
                        config=_bound_config,
                        intended_on=_bound_intended_on,
                        gcal_event_id=gcal_event_id,
                        correlation_id=str(
                            pre_delete_fm.get("correlation_id") or ""
                        ),
                        sync_policy=resolve_sync_policy(pre_delete_fm),
                    )

                register_event_create_hook(_on_event_created)
                register_event_update_hook(_on_event_updated)
                register_event_delete_hook(_on_event_deleted)
                log.info("talker.daemon.gcal_event_hooks_registered")
            else:
                log.info("talker.daemon.gcal_disabled")
        except Exception:  # noqa: BLE001
            # ``gcal_intended_on`` may already be True (failure happened
            # AFTER load_gcal returned enabled=True) — that's the case
            # the sentinel exists for. Don't reset it.
            log.exception("talker.daemon.gcal_setup_failed")

        # ---- Peer inbox callable (inter-instance messaging, P1 501 fix) --
        # Inbound /peer/send (kinds message | query_result | notice) was
        # defined-but-not-wired: wire_transport_app accepts a
        # ``peer_inbox_callable`` and registers it via register_peer_inbox,
        # but this daemon never passed one — so every inbound /peer/send
        # 501'd ``peer_inbox_not_configured``. Wiring it here completes the
        # foundation for the inter-instance query protocol (the deterministic
        # /peer/search path is synchronous and does NOT use this inbox; the
        # inbox carries query_result delivery for the future async lane +
        # message/notice relays + the R-precedence comms-test heartbeat).
        #
        # All instances run this daemon, so all instances now accept inbound
        # peer messages (intended — the comms-test BIT + query_result need
        # it everywhere, not just on Salem).
        async def _peer_inbox_handler(
            *,
            kind: str,
            payload: dict[str, Any],
            from_peer: str,
            correlation_id: str,
        ) -> dict[str, Any]:
            """Route an inbound /peer/send relay, precedence-aware (Z/O/P/R).

            * ``query_result`` → deliver to a waiting ``await_response``
              (else park in the orphan buffer). The P-lane back-fill
              substrate; no Telegram relay.
            * ``message`` / ``notice`` → relay to the operator via Telegram
              with a precedence-tagged prefix (``[<peer> · Immediate]`` /
              ``[<peer> · 🚨 Flash]`` etc., per the per-instance
              ``precedence_label_style``). Best-effort — a relay failure
              is logged but still acks the sender.

            Precedence lane mapping (MVP two lanes):
              * Z (Flash)  → immediate relay + 🚨 marker (NO turn-preempt)
              * O (Immediate) → immediate relay (today's behavior)
              * P (Priority)  → query_result → deliver_response (handled
                above); a P *message* relays with a [Priority] tag
              * R (Routine)   → DEFAULT; a Routine message still relays
                (report/digest R lands on the brief-digest path, NOT here)
            """
            from alfred.transport.peers import (
                deliver_response,
                normalize_precedence,
                render_precedence_prefix,
            )

            # Precedence — absent → R; unknown → R + log (Decision A). Emit
            # on every message so the later self-observation phase can
            # consume the dimension.
            precedence, prec_unknown = normalize_precedence(
                payload.get("precedence"),
            )
            if prec_unknown:
                log.info(
                    "talker.daemon.peer_inbox_precedence_unknown",
                    raw=str(payload.get("precedence")),
                    coerced_to=precedence,
                    kind=kind, from_peer=from_peer,
                    correlation_id=correlation_id,
                )
            log.info(
                "talker.daemon.peer_inbox_received",
                kind=kind, precedence=precedence, from_peer=from_peer,
                correlation_id=correlation_id,
            )

            if kind == "query_result":
                # P-lane back-fill substrate — wake the waiting requester
                # (or orphan-buffer). No relay; precedence is logged above.
                delivered = deliver_response(correlation_id, payload)
                return {
                    "delivered": delivered, "kind": kind,
                    "precedence": precedence,
                }

            # message | notice → Telegram relay to the primary operator,
            # with a precedence-tagged prefix in the configured label style.
            text = str(payload.get("text") or payload.get("body") or "")
            prefix = render_precedence_prefix(
                from_peer, precedence, config.precedence_label_style,
            )
            first = config.allowed_users[0] if config.allowed_users else None
            # Tolerate AllowedUser or a bare-int fixture entry.
            primary_user_id = getattr(first, "id", first) if first else 0
            if not primary_user_id or not text:
                log.info(
                    "talker.daemon.peer_inbox_relay_skipped",
                    kind=kind, precedence=precedence, from_peer=from_peer,
                    reason="no_primary_user" if not primary_user_id else "empty_text",
                    correlation_id=correlation_id,
                )
                return {"relayed": False, "kind": kind, "precedence": precedence}
            try:
                msg_ids = await _send_via_telegram(
                    int(primary_user_id), f"{prefix}{text}",
                )
                return {
                    "relayed": True, "kind": kind, "precedence": precedence,
                    "message_ids": msg_ids,
                }
            except Exception as exc:  # noqa: BLE001 — relay failure still acks the sender
                log.warning(
                    "talker.daemon.peer_inbox_relay_failed",
                    kind=kind, precedence=precedence, from_peer=from_peer,
                    error=str(exc), correlation_id=correlation_id,
                )
                return {
                    "relayed": False, "kind": kind, "precedence": precedence,
                    "error": str(exc),
                }

        # ---- NL-broker LLM callable (LLM-mediated opt-in lane) ----------
        # ``kind=query_nl`` needs a constrained one-shot completion for
        # its interpret + compose stages. The transport module stays free
        # of anthropic imports: the daemon (which owns the talker's
        # Anthropic config) builds an ASYNC closure here and registers it
        # via wire_transport_app — the peer_inbox_callable precedent.
        #
        # AsyncAnthropic is MANDATORY (not the sync client): the broker
        # runs inside this event loop, and a sync HTTP call would freeze
        # Telegram polling for the 5-25s LLM turns.
        #
        # Model resolution (ratified Decision D): nl_broker.model, with
        # "" inheriting the talker's anthropic.model — no per-instance
        # model literal in code. Failures here are non-fatal: the lane
        # fail-closes to ``nl_broker_unavailable`` (distinguishable from
        # "not opted in") and the talker keeps running.
        nl_llm_callable = None
        nl_llm_model_label = ""
        try:
            nl_broker_cfg = transport_config.canonical.nl_broker
            if nl_broker_cfg.enabled:
                from alfred.telegram._anthropic_compat import (
                    messages_create_kwargs as _nl_create_kwargs,
                )

                _nl_model = nl_broker_cfg.model or config.anthropic.model
                _nl_client = anthropic.AsyncAnthropic(
                    api_key=config.anthropic.api_key,
                    timeout=nl_broker_cfg.llm_timeout_seconds,
                    # The BROKER owns retry (its 2-attempt interpret /
                    # compose loops). The SDK default max_retries=2 would
                    # STACK with those: 2 SDK retries × 2 broker attempts
                    # × 2 stages × 30s timeout ≈ 6min worst case vs the
                    # requester's 90s mailbox await and the 300s orphan
                    # TTL (review NIT 3). Zero keeps worst-case holder
                    # latency inside the orphan window.
                    max_retries=0,
                )

                async def _nl_llm_complete(
                    *,
                    system: str,
                    user: str,
                    max_tokens: int,
                    output_schema: dict[str, Any] | None = None,
                ) -> tuple[str, dict[str, Any]]:
                    """The broker's injection contract — one-shot, no tools.

                    ``output_schema`` rides ``output_config.format`` when
                    given; a model that rejects structured outputs gets one
                    retry without it (the broker parses defensively either
                    way). ``temperature`` is omitted entirely (one-shot
                    translation/composition wants the default; also
                    sidesteps the Opus-family rejection quirk).
                    """
                    kwargs = _nl_create_kwargs(
                        model=_nl_model,
                        max_tokens=max_tokens,
                        system=system,
                        messages=[{"role": "user", "content": user}],
                    )
                    if output_schema is not None:
                        kwargs["output_config"] = {
                            "format": {
                                "type": "json_schema",
                                "schema": output_schema,
                            },
                        }
                    try:
                        resp = await _nl_client.messages.create(**kwargs)
                    except Exception as exc:
                        # Structured-output rejection fallback — older /
                        # other model families 400 on output_config; the
                        # plain completion + defensive parse still works.
                        rejected_output_config = (
                            output_schema is not None
                            and "output" in str(exc).lower()
                        )
                        if not rejected_output_config:
                            raise
                        log.info(
                            "talker.daemon.nl_broker_output_config_fallback",
                            model=_nl_model, error=str(exc)[:200],
                        )
                        kwargs.pop("output_config", None)
                        resp = await _nl_client.messages.create(**kwargs)
                    text = "".join(
                        block.text for block in resp.content
                        if getattr(block, "type", "") == "text"
                    )
                    usage = {
                        "input_tokens": getattr(resp.usage, "input_tokens", 0),
                        "output_tokens": getattr(resp.usage, "output_tokens", 0),
                    }
                    return text, usage

                nl_llm_callable = _nl_llm_complete
                nl_llm_model_label = _nl_model
                log.info(
                    "talker.daemon.nl_broker_enabled", model=_nl_model,
                )
            else:
                # ILB: opted-out reads differently from setup-failed.
                log.info("talker.daemon.nl_broker_disabled")
        except Exception:  # noqa: BLE001 — lane fail-closes; talker survives
            log.exception("talker.daemon.nl_broker_setup_failed")
            nl_llm_callable = None
            nl_llm_model_label = ""

        # ---- Ticket intake (VERA→KAL-LE→GitHub pipeline c3) ----------
        # KAL-LE-only in practice: the intake registers ONLY when both
        # the ``ticket_intake:`` section (present + enabled) AND a
        # successfully-built GitHub client exist. The fail-loud
        # build_github_client exceptions (GitHubOpsNotConfigured /
        # GitHubOpsWrongInstance / anything else) surface HERE at
        # daemon startup as a loud ``transport.ticket_intake.disabled``
        # warning — the daemon still starts, kind=ticket answers 501,
        # and there is never a silent half-registration.
        ticket_intake_config = None
        ticket_intake_github_client = None
        try:
            from alfred.transport.ticket_intake import (
                load_ticket_intake_config,
            )
            _ti_cfg = load_ticket_intake_config(raw)
            if _ti_cfg.enabled:
                try:
                    from alfred.integrations.github_ops import (
                        build_github_client,
                    )
                    _ti_client = build_github_client(
                        raw, config.instance.name,
                    )
                except Exception as exc:  # noqa: BLE001 — fail loud, start anyway
                    log.warning(
                        "transport.ticket_intake.disabled",
                        error=str(exc),
                        error_type=exc.__class__.__name__,
                        detail=(
                            "ticket_intake.enabled is true but the GitHub "
                            "client could not be built — kind=ticket will "
                            "501 until the github: config is fixed"
                        ),
                    )
                else:
                    ticket_intake_config = _ti_cfg
                    ticket_intake_github_client = _ti_client
                    log.info(
                        "talker.daemon.ticket_intake_enabled",
                        repo=_ti_client.config.repo,
                        state_path=_ti_cfg.state_path,
                    )
            else:
                # ILB: opted-out reads differently from setup-failed.
                log.info("talker.daemon.ticket_intake_not_configured")
        except Exception:  # noqa: BLE001 — intake is optional; talker survives
            log.exception("talker.daemon.ticket_intake_setup_failed")
            ticket_intake_config = None
            ticket_intake_github_client = None

        # Ticket-outcome resolver (pipeline c7) — the VERA-side receiver
        # for the KAL-LE→VERA outcome write-back. Wired only when this
        # instance opts in as a receiver (``ticket_outcome.receiver_
        # enabled: true``); the resolver closure flips the originating
        # ticket out of the open worklist under the narrow
        # ``vera_ticket_outcome`` scope. ILB: opted-out reads differently
        # from not-configured.
        ticket_outcome_resolver_fn = None
        try:
            from alfred.transport.ticket_intake import (
                load_ticket_outcome_config,
                resolve_ticket_outcome,
            )
            _to_cfg = load_ticket_outcome_config(raw)
            if _to_cfg.receiver_enabled:
                _to_vault_path = Path(config.vault.path)

                async def _ticket_outcome_resolver(
                    *,
                    ticket_uid: str,
                    status: str,
                    disposition: str,
                    pr_number: int | None = None,
                    resolved_at: str | None = None,
                    correlation_id: str = "",
                ) -> dict[str, Any]:
                    """Adapter: peer ticket_outcome → local write-back.

                    The transport handler hands us the parsed body; we
                    apply the resolution flip against this instance's
                    own ticket vault and return the resolver-contract
                    dict. Synchronous vault I/O is fine here — the write
                    is a single small frontmatter edit (matches the
                    pending-items resolver's posture).
                    """
                    return resolve_ticket_outcome(
                        _to_vault_path,
                        ticket_uid=ticket_uid,
                        status=status,
                        disposition=disposition,
                        pr_number=pr_number,
                        resolved_at=resolved_at,
                    )

                ticket_outcome_resolver_fn = _ticket_outcome_resolver
                log.info(
                    "talker.daemon.ticket_outcome_receiver_enabled",
                    vault_path=str(_to_vault_path),
                )
            else:
                log.info("talker.daemon.ticket_outcome_receiver_not_configured")
        except Exception:  # noqa: BLE001 — receiver is optional; talker survives
            log.exception("talker.daemon.ticket_outcome_setup_failed")
            ticket_outcome_resolver_fn = None

        # ---- Centralized wiring --------------------------------------
        # ``wire_transport_app`` calls every register_* helper
        # conditionally based on what we pass in. This is the single
        # call site for transport-app dependencies — adding a new
        # resource means adding a kwarg here AND in the helper, not
        # threading another register call through this daemon.
        #
        # Vault path is required by every /canonical/* handler, the
        # /peer/brief_digest handler, and the pending-items resolvers.
        # Without it, those handlers 500 with ``vault_not_configured``.
        # Salem hotfix 2026-05-01 (commit f0f8a03) plumbed
        # register_vault_path here after a 1-day production outage;
        # wire_transport_app is the structural fix that prevents the
        # next instance from re-discovering the same gap.
        wire_transport_app(
            transport_app,
            transport_config,
            instance_name=config.instance.name,
            peer_inbox_callable=_peer_inbox_handler,
            # instance_alias deliberately omitted — InstanceConfig.aliases is a
            # router accept-list (case-insensitive variant matching like
            # "Salem"→S.A.L.E.M., "Pat"→Hypatia), not a display alias.
            # Picking aliases[0] would advertise a router variant as a primary
            # name (e.g. "Kali" for KAL-LE). When a real consumer needs a
            # display alias, add an explicit InstanceConfig.display_alias
            # field rather than repurposing aliases[0].
            vault_path=Path(config.vault.path),
            send_fn=_send_via_telegram,
            pending_items_aggregate_path=pending_items_aggregate_path,
            pending_items_resolve_callable=pending_items_resolver_fn,
            gcal_client=gcal_client,
            gcal_config=gcal_config,
            gcal_intended_on=gcal_intended_on,
            nl_llm_callable=nl_llm_callable,
            nl_llm_model_label=nl_llm_model_label,
            ticket_intake_config=ticket_intake_config,
            ticket_intake_github_client=ticket_intake_github_client,
            ticket_outcome_resolve_callable=ticket_outcome_resolver_fn,
            # Cross-instance verbatim ingest route (2026-06-29). Opt-in via
            # transport.ingest.enabled (default False → route not mounted).
            ingest_enabled=transport_config.ingest.enabled,
            ingest_config=transport_config.ingest,
        )
        log.info(
            "talker.daemon.transport_configured",
            # host_display() renders single-host byte-identically and a
            # multi-bind list as "127.0.0.1, 10.99.0.1" (never a raw list).
            host=transport_config.server.host_display(),
            port=transport_config.server.port,
            peers=list(transport_config.auth.tokens.keys()),
        )

    except Exception:  # noqa: BLE001
        # Missing transport config is non-fatal — the talker still
        # handles Telegram chat even without the outbound push server.
        log.exception("talker.daemon.transport_setup_failed")
        transport_app = None

    # ---- Web chat surface (Frontend M1) ----------------------------------
    # The browser chat is a SECOND adapter onto ``run_turn``, mounted on the
    # transport app. The daemon wires it (not ``wire_transport_app``)
    # because the web handlers need talker runtime — the Anthropic client,
    # the StateManager, the TalkerConfig, the per-turn system-prompt
    # provider, and the boot-time vault-context snapshot — which live here,
    # not in the transport layer. Same "daemon wires the runtime closures"
    # shape as the GCal vault-ops hooks above.
    #
    # Opt-in: ``register_web_routes`` mounts NOTHING when ``web.enabled`` is
    # false / absent (the common case — M1 targets Salem only), so every
    # other instance's transport server is byte-unchanged. Must run BEFORE
    # ``run_server`` starts the app (this is still the pre-start window).
    try:
        from alfred.web.config import (
            load_from_unified as load_web_config,
            resolve_signing_secret,
        )
        from alfred.web.routes_chat import register_web_routes
        from alfred.web.state import WebAuthState

        web_config = load_web_config(raw)
        if transport_app is not None and web_config.enabled:
            # Boot-time fail-loud secret check (the reviewer-flagged trap:
            # without this, an enabled-but-unconfigured instance boots clean
            # and only errors lazily at FIRST login via the token codec).
            # Surfacing it HERE makes the failure happen at startup. Web is
            # an OPT-IN add-on, so a missing secret disables the web surface
            # loudly (fail-closed — never serve forgeable sessions) WITHOUT
            # taking down the core talker daemon (Telegram / transport stay
            # up). ``register_web_routes`` re-checks (defense-in-depth). A
            # flag (not a raise) keeps this path from hitting the generic
            # ``web_routes_setup_failed`` handler below — the dedicated
            # ``web_secret_unconfigured`` error is logged exactly once.
            # ``relay`` auth mode (cross-instance chat) mints no session
            # tokens, so it has no signing secret to check — skip the guard
            # so an enabled relay instance with no secret still mounts. The
            # Layer-1 ``web`` peer token is its authority instead.
            web_mode = getattr(web_config.auth, "mode", "session") or "session"
            secret_ok = True
            if web_mode != "relay":
                try:
                    resolve_signing_secret(web_config.auth)
                except ValueError as exc:
                    log.error(
                        "talker.daemon.web_secret_unconfigured",
                        error=str(exc),
                        detail=(
                            "web.enabled=true but session_secret is empty / "
                            "unresolved — web surface NOT mounted "
                            "(fail-closed). Set ALFRED_WEB_SESSION_SECRET to "
                            "enable web chat."
                        ),
                    )
                    secret_ok = False

            if secret_ok:
                allowed_user_ids = [
                    getattr(u, "id", u) for u in config.allowed_users
                ]
                # Single-use magic-link nonce store (persists across restart
                # within the link TTL window).
                web_auth_state = WebAuthState.create(web_config.state_path)
                web_auth_state.load()
                register_web_routes(
                    transport_app,
                    web_config=web_config,
                    web_auth_state=web_auth_state,
                    anthropic_client=client,
                    state_mgr=state_mgr,
                    talker_config=config,
                    system_prompt_provider=system_prompt_provider,
                    vault_context_str=vault_context_str,
                    allowed_user_ids=allowed_user_ids,
                )
        else:
            # Intentionally-left-blank: a disabled / unmountable web
            # surface is logged so "no web routes" is distinguishable from
            # "wiring silently skipped".
            log.info(
                "talker.daemon.web_routes_not_mounted",
                reason=(
                    "transport app unavailable"
                    if transport_app is None
                    else "web.enabled=false / web block absent"
                ),
            )
    except Exception:  # noqa: BLE001
        # Web surface is non-fatal — the talker still handles Telegram +
        # the outbound transport even if web wiring fails.
        log.exception("talker.daemon.web_routes_setup_failed")

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

    # ---- Voice/method training worker ------------------------------------
    # Async extraction worker that drains the JSONL queue, calls Opus on
    # each pending job, writes the structured voice / method profile,
    # and DMs the operator on completion. Per-instance opt-in via
    # ``telegram.voice_train.command_enabled`` — when disabled (default
    # for Salem / KAL-LE) the task is never created.
    voice_train_task: asyncio.Task | None = None
    if (
        config.voice_train is not None
        and config.voice_train.command_enabled
    ):
        from . import voice_train as _voice_train
        from . import bot as _bot_mod

        async def _voice_train_dm(chat_id: int, text: str) -> None:
            """Best-effort DM to the operator on extraction completion / failure.

            Wraps ``app.bot.send_message`` with the same per-chat lock
            shape the transport uses (avoids double-fire if a transport
            send raced with us). Failure is logged + swallowed — the
            structured record landed regardless of whether the DM made it.
            """
            if app is None:
                # Web-only mode (bit d) — no Telegram bot to DM through.
                return
            lock = send_lock_map.setdefault(chat_id, asyncio.Lock())
            async with lock:
                try:
                    await app.bot.send_message(chat_id=chat_id, text=text)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "talker.daemon.voice_train_dm_failed",
                        chat_id=chat_id,
                    )
                # 250ms inter-message floor mirrors the transport
                # rate-limit pattern. Cheap insurance against bursts
                # if the worker processes multiple jobs in one tick.
                await asyncio.sleep(0.25)

        queue_path = (
            Path(config.voice_train.queue_path)
            if config.voice_train.queue_path
            else _bot_mod._resolve_queue_path(config)
        )
        scope = _bot_mod._voice_train_scope_for(config)
        voice_train_task = asyncio.create_task(
            _voice_train.run_worker(
                queue_path=queue_path,
                vault_path=Path(config.vault.path),
                client=client,
                model=config.voice_train.extraction_model,
                scope=scope,
                instance=config.instance.name or "",
                poll_seconds=config.voice_train.worker_poll_seconds,
                dm_callback=_voice_train_dm,
                shutdown_event=shutdown_event,
            ),
            name="talker-voice-train-worker",
        )
        log.info(
            "talker.daemon.voice_train_worker_started",
            queue_path=str(queue_path),
            poll_seconds=config.voice_train.worker_poll_seconds,
            scope=scope,
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

        def _on_transport_server_done(task: asyncio.Task) -> None:
            """Supervise the transport server task.

            ``run_server`` only returns/raises on the genuinely-fatal path
            (zero bound, or loopback failed — see transport/server.py). A
            normal shutdown cancels the task. Any OTHER completion means the
            transport DIED while the daemon believes it's healthy — surface it
            LOUDLY (intentionally-left-blank: a dead transport must never look
            like idle) and trigger graceful daemon shutdown so systemd
            restarts and re-attempts the bind, rather than leaving a healthy
            daemon with a dead transport (silent orphan).
            """
            cancelled = task.cancelled()
            exc = None if cancelled else task.exception()
            outcome = _classify_transport_task_outcome(
                cancelled=cancelled,
                exc=exc,
                shutdown_requested=shutdown_event.is_set(),
            )
            if outcome == "silent":
                return  # expected: clean cancel, or shutdown-requested return
            if outcome == "died_exception":
                log.error(
                    "transport.server.task_died",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            else:  # "died_returned"
                log.error(
                    "transport.server.task_died",
                    detail="run_server returned without a shutdown request",
                )
            shutdown_event.set()

        transport_server_task.add_done_callback(_on_transport_server_done)
        # VERA MVP: tolerate AllowedUser or bare-int entry (see the
        # pending-items resolver site above for the getattr rationale).
        _sched_first_user = (
            config.allowed_users[0] if config.allowed_users else 0
        )
        scheduler_user_id = getattr(
            _sched_first_user, "id", _sched_first_user,
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
        if app is not None:
            await app.initialize()
            initialised = True
            await app.start()
            started = True
            await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
            polling = True
            log.info("talker.daemon.polling")
        else:
            # Web-only mode (bit d): no Telegram polling. The transport + web
            # server tasks started above serve until SIGTERM; the finally
            # block's PTB teardown is fully skipped (initialised/started/
            # polling all stay False). Intentionally-left-blank: an explicit
            # "serving without Telegram" signal.
            log.info(
                "talker.daemon.web_only_serving",
                detail="no Telegram polling — transport + web surface only",
            )
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

        # Stop the transport server + scheduler + heartbeat + voice_train
        # worker if they were started.
        for t in (
            transport_server_task,
            scheduler_task,
            heartbeat_task,
            voice_train_task,
        ):
            if t is None:
                continue
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        # Close any still-open sessions so the record lands before exit.
        try:
            await _close_open_sessions_on_shutdown(state_mgr, config, client)
        except Exception:  # noqa: BLE001
            log.exception("talker.daemon.shutdown_sweep_error")

        log.info("talker.daemon.stopped")

    return exit_code
