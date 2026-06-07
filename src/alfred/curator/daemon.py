"""Main daemon loop — orchestrates the full inbox processing pipeline.

Architecture: agent-writes-via-CLI. The agent uses ``alfred vault`` commands
(via Bash tool) to create/modify vault files. Curator orchestrates:
detect inbox → create session → invoke agent → read mutation log → mark processed → track state.

Claude is the only backend (post backend-abstraction-collapse 2026-05-25).
Vault changes are tracked via mutation-log JSONL injected via the
``ALFRED_VAULT_SESSION`` env var; no snapshot/diff fallback needed.
"""

from __future__ import annotations

import asyncio
import json
import os
import time as _time
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from alfred.email_classifier.config import EmailClassifierConfig

from alfred.common.heartbeat import Heartbeat
from alfred.preferences.loader import load_active_preferences
from alfred.vault.mutation_log import append_to_audit_log, cleanup_session_file, create_session_file, read_mutations

from .backends import BaseBackend
from .backends.cli import ClaudeBackend
from .config import CuratorConfig
from .context import build_vault_context, extract_sender_email, gather_sender_context
from .pipeline import _apply_inbox_preference_filter
from .state import StateManager
from .utils import get_logger
from .watcher import InboxWatcher
from .writer import mark_filtered, mark_processed

# Email classifier (per-instance, opt-in) — post-processor that adds
# ``priority`` + ``action_hint`` frontmatter fields to email-derived
# note records. Imported lazily inside ``_process_file`` so curator
# tests that don't touch the classifier still work without the module
# on the import path. See ``email_classifier/__init__.py``.

log = get_logger(__name__)

# Module-level idle-tick heartbeat — see ``alfred.common.heartbeat`` for
# the rationale ("intentionally left blank" pattern). Counter is bumped
# on each successful end-to-end inbox-file processing in
# :func:`_process_file`. The heartbeat task is spawned in :func:`run`
# only when ``config.idle_tick.enabled`` is True.
heartbeat: Heartbeat = Heartbeat(daemon_name="curator", log=log)


# --- P10 / Ship 3 (2026-06-07) inbox-stage preference filter ---
# Bucket of per-preference drop counts since the last daily summary.
# Reset to {} when ``_emit_daily_filter_summary`` fires. Keyed by
# preference slug; integer count.
#
# Module-level state on the same asyncio loop as ``_process_file`` and
# the main run loop — plain int/dict mutation is correct here, no lock
# needed. The bucket survives across ``_process_file`` calls so the
# daily summary aggregates per Halifax-local calendar day.
_inbox_filter_stats: dict[str, int] = {}

# Halifax-local calendar date of the LAST daily-summary emit. None on
# fresh daemon startup → seeded to today's date on the first tick
# (without firing a summary — see ``_maybe_emit_daily_filter_summary``).
_last_summary_emit: date | None = None

# Halifax timezone constant. Pinned at module load so the tz lookup
# doesn't fire on every poll iteration; zoneinfo objects are
# immutable and cheap to share.
_HALIFAX_TZ = ZoneInfo("America/Halifax")

# Active inbox-filter rule name — kept here as a module constant so
# tests + the daily-summary helper share the same source of truth.
# Mirrors ``alfred.curator.pipeline._INBOX_FILTER_RULE``; importing
# the underscored name from a sibling module is fine within the
# package but the constant is duplicated here for readability of the
# summary helper (single grep target for "which rule does the
# summary count").
_INBOX_FILTER_RULE_NAME: str = "skip_inbox_if_sender_matches"


def _filter_stats_bump(preference_slug: str) -> None:
    """Increment the per-pref drop counter by one.

    Called from ``_process_file`` when the inbox filter fires. Cheap
    by design — must add no measurable latency to the inbox path.
    Same loop as the main run loop = no lock.
    """
    _inbox_filter_stats[preference_slug] = (
        _inbox_filter_stats.get(preference_slug, 0) + 1
    )


def _halifax_today() -> date:
    """Return today's date in America/Halifax local time.

    Used by ``_maybe_emit_daily_filter_summary`` to decide if a new
    Halifax-local calendar day has begun since the last summary
    emit. Factored out for test injectability and so the timezone
    pin lives in one place.
    """
    return datetime.now(_HALIFAX_TZ).date()


def _count_active_inbox_filter_prefs(vault_path: Path | str) -> int:
    """Count active curator-domain prefs with the inbox-filter rule.

    Used in the daily summary as the ``prefs_active`` field. Per
    operator decision 2026-06-07 (decision flag #4): the count
    reports ONLY the inbox-filter rule, not all curator-domain
    preferences — the summary is about THIS filter's activity.

    Defensive: any loader exception (e.g. malformed preference YAML)
    returns 0 with an info log. The daily summary should never crash
    the daemon — observability about other prefs is what the
    main loader log lines are for.
    """
    try:
        prefs = load_active_preferences(vault_path, shape="action")
    except Exception as exc:
        log.info(
            "curator.preference_filter_inbox_summary_load_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return 0
    count = 0
    for pref in prefs:
        matcher = pref.matcher or {}
        if matcher.get("rule") != _INBOX_FILTER_RULE_NAME:
            continue
        if matcher.get("domain") not in (None, "curator"):
            continue
        count += 1
    return count


def _emit_daily_filter_summary(
    summary_date: date,
    vault_path: Path | str,
) -> None:
    """Emit one ``curator.preference_filter_inbox_summary`` log line and reset.

    Drains ``_inbox_filter_stats`` into a single info-level log event
    (date, prefs_active, drops_today, drops_by_pref) and resets the
    stats bucket to empty. Per ``feedback_intentionally_left_blank.md``,
    a zero-drop day still emits — operator can distinguish "filter
    alive, nothing to drop" from "filter silently broken."

    Factored out from ``_maybe_emit_daily_filter_summary`` so test
    fixtures can drive the emit path directly without simulating a
    Halifax-midnight boundary.
    """
    global _inbox_filter_stats
    drops_by_pref = dict(_inbox_filter_stats)
    drops_today = sum(drops_by_pref.values())
    prefs_active = _count_active_inbox_filter_prefs(vault_path)
    log.info(
        "curator.preference_filter_inbox_summary",
        date=summary_date.isoformat(),
        prefs_active=prefs_active,
        drops_today=drops_today,
        drops_by_pref=drops_by_pref,
    )
    _inbox_filter_stats = {}


def _maybe_emit_daily_filter_summary(vault_path: Path | str) -> None:
    """Fire the daily inbox-filter summary if a Halifax day has rolled.

    Called from the main poll loop. Behaviour:

    1. First call after daemon start (``_last_summary_emit is None``):
       seed ``_last_summary_emit`` to today, do NOT emit. The startup
       case shouldn't replay an empty summary for whatever calendar
       day the daemon happens to boot on.
    2. Subsequent calls: if today's Halifax-local date is greater than
       ``_last_summary_emit``, emit the summary for the PREVIOUS day
       (the day the stats accumulated against) and update the marker
       to today. Today's drops start fresh.
    3. Same-day calls: no-op.

    The "summary covers the previous day" semantic matters: a drop at
    23:59 Halifax-local lands in the same summary as drops earlier in
    the day, NOT in the next day's summary. The boundary is the
    Halifax-midnight roll, not the emit-tick.
    """
    global _last_summary_emit
    today = _halifax_today()
    if _last_summary_emit is None:
        _last_summary_emit = today
        return
    if today > _last_summary_emit:
        _emit_daily_filter_summary(_last_summary_emit, vault_path)
        _last_summary_emit = today


def _load_skill(skills_dir: Path) -> str:
    """Load SKILL.md and all reference templates into a single text block."""
    skill_path = skills_dir / "vault-curator" / "SKILL.md"
    if not skill_path.exists():
        log.warning("daemon.skill_not_found", path=str(skill_path))
        return ""

    parts: list[str] = [skill_path.read_text(encoding="utf-8")]

    # Inline all reference templates so the agent has the full schema
    refs_dir = skills_dir / "vault-curator" / "references"
    if refs_dir.is_dir():
        for ref_file in sorted(refs_dir.glob("*.md")):
            content = ref_file.read_text(encoding="utf-8")
            parts.append(f"\n---\n### Reference Template: {ref_file.name}\n```\n{content}\n```")

    return "\n".join(parts)


def _create_backend(config: CuratorConfig) -> BaseBackend:
    """Instantiate the configured backend.

    Post backend-abstraction-collapse (2026-05-25): only the Claude CLI
    backend survives. The factory still takes a ``backend_name`` and
    fails loud on anything else so a config typo / stale yaml /
    re-introduced backend name fails at startup rather than silently
    defaulting. Per ``feedback_intentionally_left_blank.md``.

    BaseBackend + the factory pattern remain in place so re-introducing
    a backend (Q3 MCP, local Ollama, etc.) is a pure-extend: add a new
    branch + a sibling module in ``backends/`` — no architectural
    re-work.
    """
    backend_name = config.agent.backend
    if backend_name == "claude":
        return ClaudeBackend(config.agent.claude)
    raise ValueError(
        f"Unknown curator backend: {backend_name!r}. "
        f"Supported backends: 'claude'. "
        f"(zo / openclaw / hermes were removed in the backend-abstraction-"
        f"collapse arc 2026-05-25; update agent.backend in your config.yaml)"
    )


# ---------------------------------------------------------------------------
# Cross-process file locking — prevents two curator daemons from processing
# the same inbox file simultaneously (root cause of duplicate records).
# ---------------------------------------------------------------------------

_LOCK_STALE_SECONDS = 600  # 10 minutes


def _claim_file(inbox_file: Path, _retry: bool = True) -> bool:
    """Atomically claim an inbox file for processing.

    Creates ``{inbox_file}.lock`` containing our PID and timestamp.
    Returns True if the lock was acquired, False if another live process
    already holds it.
    """
    lock_path = inbox_file.with_suffix(inbox_file.suffix + ".lock")
    my_pid = os.getpid()
    payload = json.dumps({"pid": my_pid, "ts": _time.time()})

    # Fast path — try exclusive create (O_CREAT | O_EXCL)
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, payload.encode())
        os.close(fd)
        log.debug("daemon.lock_acquired", file=inbox_file.name, pid=my_pid)
        return True
    except FileExistsError:
        pass

    # Lock file exists — inspect it
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        lock_pid = int(data["pid"])
        lock_ts = float(data["ts"])
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        # Corrupt lock file — break it and retry (once)
        log.warning("daemon.lock_corrupt", file=inbox_file.name)
        _release_file(inbox_file)
        if _retry:
            return _claim_file(inbox_file, _retry=False)
        return False

    # Same process? (shouldn't happen, but be safe)
    if lock_pid == my_pid:
        return True

    # Is the holder still alive?
    try:
        os.kill(lock_pid, 0)
    except ProcessLookupError:
        # Dead process — break stale lock
        log.info("daemon.lock_stale_dead_pid", file=inbox_file.name, dead_pid=lock_pid)
        _release_file(inbox_file)
        if _retry:
            return _claim_file(inbox_file, _retry=False)
        return False
    except PermissionError:
        pass  # process exists but owned by another user — treat as alive

    # Alive but stale timestamp?
    if (_time.time() - lock_ts) > _LOCK_STALE_SECONDS:
        log.warning("daemon.lock_stale_timeout", file=inbox_file.name, holder_pid=lock_pid)
        _release_file(inbox_file)
        if _retry:
            return _claim_file(inbox_file, _retry=False)
        return False

    # Another live process legitimately holds the lock
    log.info("daemon.lock_held", file=inbox_file.name, holder_pid=lock_pid)
    return False


def _release_file(inbox_file: Path) -> None:
    """Remove the lock file for an inbox file (idempotent)."""
    lock_path = inbox_file.with_suffix(inbox_file.suffix + ".lock")
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


async def _process_file(
    inbox_file: Path,
    backend: BaseBackend,
    skill_text: str,
    config: CuratorConfig,
    state_mgr: StateManager,
    email_classifier_config: "EmailClassifierConfig | None" = None,
) -> None:
    """Process a single inbox file through the full pipeline.

    When ``email_classifier_config.enabled`` is True and the inbox file
    looks email-derived, the classifier post-processor runs after
    curation completes and writes ``priority`` + ``action_hint`` into
    each newly-created ``note/*.md`` record's frontmatter. Disabled
    or non-email files are skipped silently — the post-processor never
    raises into the curator pipeline.
    """
    filename = inbox_file.name
    log.info("daemon.processing", file=filename)

    # Always pass the file to the LLM — read as text if possible, otherwise point to the file
    try:
        inbox_content = inbox_file.read_text(encoding="utf-8")
    except (UnicodeDecodeError, ValueError):
        inbox_content = f"[File: {filename} — read it directly from: {inbox_file}]"

    # Extract sender FIRST so the inbox-stage preference filter (below)
    # can gate the whole file BEFORE the expensive vault-context scan.
    # Per Salem operator friction 2026-06-07: ~99% of recent inbox
    # volume is empty-body promotional; dropping at the inbox stage
    # saves both the LLM cost AND the vault-walk that builds context.
    sender_email = extract_sender_email(inbox_content)

    # P10 / Ship 3 (2026-06-07) — inbox-stage operator-preference
    # filter. Loads ``shape: action`` preferences from
    # ``<vault>/preference/`` and applies the
    # ``skip_inbox_if_sender_matches`` rule. Filtered files move to
    # ``processed/`` with ``status: filtered_by_preference`` sidecar
    # frontmatter (slug + reason + timestamp) so the operator-grep
    # workflow distinguishes filtered from LLM-processed files. State
    # row carries ``backend_used="preference_filter_inbox"`` for the
    # same reason.
    #
    # The filter is no-op (returns (False, None, None)) when:
    #   * the inbox file has no extractable sender (non-email path)
    #   * the vault has no active inbox-filter preferences
    #   * no preference's sender_patterns match this sender
    # Per ``feedback_intentionally_left_blank.md`` the filter always
    # emits ``curator.preference_filter_inbox_run`` so the empty-match
    # / no-prefs / no-sender cases stay distinguishable from a
    # silently-broken filter.
    prefs = load_active_preferences(config.vault.vault_path, shape="action")
    should_skip, filter_reason, matching_pref = _apply_inbox_preference_filter(
        sender_email, prefs,
    )
    if should_skip and matching_pref is not None:
        log.info(
            "curator.preference_filter_inbox_dropped",
            preference_slug=matching_pref.slug,
            preference_name=matching_pref.name,
            sender=sender_email,
            inbox_filename=filename,
            rule=_INBOX_FILTER_RULE_NAME,
            reason=filter_reason,
        )
        try:
            mark_filtered(
                inbox_file,
                config.vault.processed_path,
                preference_slug=matching_pref.slug,
                reason=filter_reason or "",
            )
        except Exception as exc:  # noqa: BLE001
            # Move failure: log and continue. State still records the
            # processed marker so the daemon doesn't retry this file
            # on next sweep. Per ``feedback_intentionally_left_blank.md``,
            # the failure must surface in logs — silent move failure
            # would leave the inbox file in place AND mark it
            # processed in state, creating a stuck-loop signature.
            log.warning(
                "curator.preference_filter_inbox_move_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                file=filename,
                preference_slug=matching_pref.slug,
            )
        # Update state: filtered files count as processed for
        # deduplication purposes. Sentinel ``backend_used`` value lets
        # operator grep distinguish filtered vs. LLM-processed.
        state_mgr.state.mark_processed(
            filename=filename,
            inbox_path=str(inbox_file),
            files_created=[],
            files_modified=[],
            backend_used="preference_filter_inbox",
        )
        state_mgr.save()
        # Bump daily-summary stats. The summary fires from the main
        # run loop's first-tick-after-Halifax-midnight.
        _filter_stats_bump(matching_pref.slug)
        return

    # Build vault context (post-filter — only paid for files that
    # actually reach the LLM).
    vault_context = build_vault_context(
        config.vault.vault_path,
        ignore_dirs=config.vault.ignore_dirs,
    )
    context_text = vault_context.to_prompt_text()

    # Inject sender-specific context for emails
    if sender_email:
        sender_ctx = gather_sender_context(
            config.vault.vault_path,
            sender_email,
            ignore_dirs=config.vault.ignore_dirs,
        )
        if sender_ctx:
            context_text = context_text + "\n\n" + sender_ctx

    vault_path_str = str(config.vault.vault_path)
    session_path = create_session_file()

    # Claude is the only surviving backend post backend-abstraction-collapse
    # (2026-05-25). It always supports mutation-log-based vault access via
    # env-var injection — no snapshot/diff fallback path needed. The
    # OpenClaw-only 4-stage pipeline (curator/pipeline.py) was retired in
    # the same arc; the legacy single-call path is now the only path.
    backend.env_overrides = {
        "ALFRED_VAULT_PATH": vault_path_str,
        "ALFRED_VAULT_SCOPE": "curator",
        "ALFRED_VAULT_SESSION": session_path,
    }

    result = await backend.process(
        inbox_content=inbox_content,
        skill_text=skill_text,
        vault_context=context_text,
        inbox_filename=filename,
        vault_path=vault_path_str,
    )

    mutations = read_mutations(session_path)
    files_created = mutations["files_created"]
    files_modified = mutations["files_modified"]
    cleanup_session_file(session_path)

    # Audit log
    audit_path = str(Path(config.state.path).parent / "vault_audit.log")
    append_to_audit_log(audit_path, "curator", mutations, detail=filename)

    if not result.success:
        log.error("daemon.agent_failed", file=filename, summary=result.summary[:500])

    if not files_created and not files_modified:
        log.warning("daemon.no_changes", file=filename)

    # Mark processed and move (skip if agent already moved the file)
    if inbox_file.exists():
        mark_processed(inbox_file, config.vault.processed_path)

    # Update state
    state_mgr.state.mark_processed(
        filename=filename,
        inbox_path=str(inbox_file),
        files_created=files_created,
        files_modified=files_modified,
        backend_used=config.agent.backend,
    )
    state_mgr.save()

    # Email-classifier post-processor (per-instance, opt-in). Runs in
    # a thread so the synchronous Anthropic SDK call doesn't block the
    # asyncio loop. Failures are logged + swallowed — classification
    # is a non-blocking post-pass; curation is already complete.
    if (
        email_classifier_config is not None
        and email_classifier_config.enabled
        and files_created
    ):
        try:
            from alfred.email_classifier import classify_records_for_inbox

            await asyncio.to_thread(
                classify_records_for_inbox,
                config.vault.vault_path,
                inbox_content,
                files_created,
                email_classifier_config,
            )
        except Exception:  # noqa: BLE001 — must never crash the daemon
            log.exception("daemon.email_classifier_error", file=filename)

    log.info(
        "daemon.completed",
        file=filename,
        created=len(files_created),
        modified=len(files_modified),
    )
    # Idle-tick counter — one inbox file processed end-to-end counts
    # as one event for the heartbeat's ``events_in_window``. Bumping
    # after the completed log keeps the call out of the hot path's
    # exception handling.
    heartbeat.record_event()


async def run(
    config: CuratorConfig,
    skills_dir: Path,
    email_classifier_config: "EmailClassifierConfig | None" = None,
) -> None:
    """Main daemon entry point.

    ``email_classifier_config`` is an optional per-instance config block
    loaded by the orchestrator / CLI from the unified config dict
    (``email_classifier:`` section). When ``None`` or
    ``enabled=False``, the classifier post-processor is a no-op and
    curator behaves exactly as before this hook landed.
    """
    log.info(
        "daemon.starting",
        backend=config.agent.backend,
        email_classifier_enabled=bool(
            email_classifier_config and email_classifier_config.enabled
        ),
    )

    # Load skill text
    skill_text = _load_skill(skills_dir)
    if not skill_text:
        log.warning("daemon.no_skill", msg="Running without SKILL.md — agent may not produce correct output")

    # Init backend
    backend = _create_backend(config)

    # Init state
    state_mgr = StateManager(config.state.path)
    state_mgr.load()

    # Init watcher
    watcher = InboxWatcher(
        inbox_path=config.vault.inbox_path,
        debounce_seconds=config.watcher.debounce_seconds,
    )

    # Startup scan for unprocessed files — process concurrently up to
    # watcher.max_concurrent. Each file retains its own claim + release and
    # the mark_processed-on-failure fallback (Batch B item 1). One file's
    # failure does not cancel peers (return_exceptions=True is implicit in
    # the per-task exception handler). Ref upstream 163b7f9.
    max_concurrent = config.watcher.max_concurrent
    unprocessed = watcher.full_scan()
    if unprocessed:
        log.info(
            "daemon.startup_scan",
            files=len(unprocessed),
            max_concurrent=max_concurrent,
        )
        startup_sem = asyncio.Semaphore(max_concurrent)

        async def _process_startup(inbox_file: Path) -> None:
            async with startup_sem:
                if not _claim_file(inbox_file):
                    log.info("daemon.skip_locked", file=inbox_file.name)
                    return
                try:
                    await _process_file(
                        inbox_file,
                        backend,
                        skill_text,
                        config,
                        state_mgr,
                        email_classifier_config=email_classifier_config,
                    )
                except Exception:
                    log.exception("daemon.process_error", file=inbox_file.name)
                    # Always move to processed — even on failure — to prevent
                    # infinite reprocessing loops. The error is logged above.
                    if inbox_file.exists():
                        try:
                            mark_processed(inbox_file, config.vault.processed_path)
                        except Exception:
                            log.exception("daemon.mark_processed_fallback_failed", file=inbox_file.name)
                finally:
                    _release_file(inbox_file)

        await asyncio.gather(
            *[_process_startup(f) for f in unprocessed],
            return_exceptions=True,
        )

    # Start watching
    watcher.start()
    log.info("daemon.watching", inbox=str(config.vault.inbox_path))

    # Idle-tick heartbeat task — emits ``curator.idle_tick`` every
    # ``config.idle_tick.interval_seconds``. Default 60s, on by default.
    # See ``alfred.common.heartbeat`` for the "intentionally left blank"
    # rationale. Spawned only when enabled — disabled path is silent.
    heartbeat_shutdown = asyncio.Event()
    heartbeat_task: asyncio.Task | None = None
    if config.idle_tick.enabled:
        heartbeat_task = asyncio.create_task(
            heartbeat.run(
                interval_seconds=config.idle_tick.interval_seconds,
                shutdown_event=heartbeat_shutdown,
            ),
            name="curator-heartbeat",
        )
        log.info(
            "daemon.heartbeat_started",
            interval_seconds=config.idle_tick.interval_seconds,
        )

    import time
    last_rescan = time.monotonic()
    rescan_interval = config.watcher.rescan_interval
    _processing: set[str] = set()  # guard against concurrent processing

    try:
        while True:
            await asyncio.sleep(config.watcher.poll_interval)

            # P10 / Ship 3 (2026-06-07) — daily inbox-filter summary
            # tick. Cheap (date math + an in-memory dict snapshot
            # when fired), so calling on every poll iteration is
            # fine. Internal Halifax-midnight roll detection means
            # the actual log emit fires at most once per calendar
            # day. Per ``feedback_intentionally_left_blank.md``,
            # zero-drop days still emit so silence is distinguishable
            # from broken.
            _maybe_emit_daily_filter_summary(config.vault.vault_path)

            ready = watcher.collect_ready()

            # Periodic full_scan fallback (inotify may not work on all kernels/mounts)
            now = time.monotonic()
            if now - last_rescan >= rescan_interval:
                last_rescan = now
                rescan_hits = watcher.full_scan()
                for f in rescan_hits:
                    if f not in ready:
                        ready.append(f)

            # Filter to files not already being processed in this loop, and
            # claim them cross-process under the lock. Anything we claim must
            # hit the _release_file path, so we do the claim inside the task.
            to_process = [
                f for f in ready
                if f.exists() and str(f) not in _processing
            ]

            if to_process:
                for f in to_process:
                    _processing.add(str(f))

                sem = asyncio.Semaphore(max_concurrent)

                async def _watch_process(inbox_file: Path) -> None:
                    async with sem:
                        # Cross-process lock — prevents duplicate processing
                        # by zombie daemons. Must be INSIDE the semaphore so
                        # the in-memory _processing set stays consistent with
                        # the filesystem lock.
                        if not _claim_file(inbox_file):
                            log.info("daemon.skip_locked", file=inbox_file.name)
                            _processing.discard(str(inbox_file))
                            return
                        try:
                            await _process_file(
                                inbox_file,
                                backend,
                                skill_text,
                                config,
                                state_mgr,
                                email_classifier_config=email_classifier_config,
                            )
                        except Exception:
                            log.exception("daemon.process_error", file=inbox_file.name)
                            # Always move to processed — even on failure — to
                            # prevent infinite reprocessing loops.
                            if inbox_file.exists():
                                try:
                                    mark_processed(inbox_file, config.vault.processed_path)
                                except Exception:
                                    log.exception("daemon.mark_processed_fallback_failed", file=inbox_file.name)
                        finally:
                            _processing.discard(str(inbox_file))
                            _release_file(inbox_file)

                # return_exceptions=True: one file's failure must not cancel
                # the gather (per-task handler already logs + marks processed).
                await asyncio.gather(
                    *[_watch_process(f) for f in to_process],
                    return_exceptions=True,
                )
    finally:
        watcher.stop()
        # Tear down the heartbeat task if it was spawned.
        if heartbeat_task is not None:
            heartbeat_shutdown.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        log.info("daemon.stopped")
