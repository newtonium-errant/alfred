"""Brief daemon — scheduled daily brief generation."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from alfred.common.schedule import (
    compute_next_fire,
    should_catchup_today,
    sleep_until,
)

from .config import BriefConfig
from .health_section import render_health_section
from .peer_digests import render_peer_digests_section
from .renderer import (
    process_hub_name,
    render_brief,
    render_process_hub_record,
    serialize_record,
)
from .routine_section import render_routine_section
from .stayc_relay import RETENTION_SECTION_HEADER as STAYC_RETENTION_SECTION_HEADER
from .stayc_relay import SECTION_HEADER as STAYC_RELAY_SECTION_HEADER
from .stayc_relay import render_stayc_bug_relay_section, render_stayc_retention_relay_section
from .state import BriefRun, StateManager
from .tier_section import SECTION_HEADER as TIER_SECTION_HEADER
from .tier_section import render_tier_section
from .utils import get_logger
from .operations import format_operations_section
from .upcoming_events import render_upcoming_events_section
from .watches import check_and_format_watches
from .weather import fetch_and_format

log = get_logger(__name__)


def ensure_process_hub(
    vault_path: Path,
    config: BriefConfig,
    date_str: str,
) -> bool:
    """Create the brief's process hub note if it doesn't exist yet.

    Every brief run record carries ``process: [[process/<hub>]]`` —
    with no hub note, the janitor flags LINK001 daily and (having no
    create scope) can never self-heal it. The brief daemon already
    writes the vault directly by design, so the writer owns the hub's
    existence. Same defect-class and same fix-shape as the BIT hub
    (``alfred.bit.daemon.ensure_process_hub``, commit 02ff294).

    Returns True when the hub was created, False when it already
    existed or the create failed. The existing-hub path doesn't log —
    the hub's existence is vault-observable; only the CREATE and FAIL
    events are signal. Failure is loud (warning) but never fatal: the
    brief record write must still proceed, and the janitor's LINK001
    keeps flagging until the hub exists, so the failure is doubly
    visible.
    """
    hub_name = process_hub_name(config.output.name_template)
    hub_path = vault_path / "process" / f"{hub_name}.md"
    if hub_path.exists():
        return False
    frontmatter, body = render_process_hub_record(hub_name, date_str)
    content = serialize_record(frontmatter, body)
    try:
        hub_path.parent.mkdir(parents=True, exist_ok=True)
        hub_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        log.warning(
            "brief.process_hub_create_failed",
            path=str(hub_path),
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return False
    log.info("brief.process_hub_created", path=str(hub_path))
    return True


async def _push_brief_to_telegram(
    content: str,
    today: str,
    user_id: int,
) -> None:
    """Dispatch the rendered brief as Telegram chunks via the transport.

    Best-effort. Transport failures are logged and swallowed — the
    brief is already written to the vault, so the user's primary
    artifact is safe even if the push fails. Catches the
    ``transport-down`` case (common during talker daemon restarts) so
    brief generation doesn't become coupled to the talker's liveness.

    Single-user v1: ``user_id`` is the first entry in
    ``telegram.allowed_users``. Multi-user support arrives with the
    peer protocol in Stage 3.5.
    """
    # Local imports so the brief module doesn't drag transport into
    # tools that don't need it (e.g. when telegram isn't configured).
    from alfred.transport.client import send_outbound_batch
    from alfred.transport.exceptions import TransportError
    from alfred.transport.utils import chunk_for_telegram

    try:
        chunks = chunk_for_telegram(content)
        if not chunks or not chunks[0]:
            log.info("brief.push_skipped_empty", date=today)
            return
        await send_outbound_batch(
            user_id=user_id,
            chunks=chunks,
            dedupe_key=f"brief-{today}",
            client_name="brief",
        )
        log.info(
            "brief.pushed",
            date=today,
            chunks=len(chunks),
            user_id=user_id,
        )
    except TransportError as exc:
        log.warning(
            "brief.push_failed",
            date=today,
            error_type=exc.__class__.__name__,
            error=str(exc),
            response_summary=f"{exc.__class__.__name__}: {exc}",
        )


async def generate_brief(config: BriefConfig, state_mgr: StateManager, refresh: bool = False) -> str | None:
    """Generate a morning brief. Returns the vault-relative path, or None if skipped."""
    today = date.today().isoformat()

    if not refresh and state_mgr.state.has_brief_for_date(today):
        log.info("brief.already_exists", date=today)
        return None

    log.info("brief.generating", date=today, refresh=refresh)

    # Fetch weather section. fetch_and_format owns its own degradation
    # (every fetch/parse/format failure → explicit "unavailable" line),
    # but this last-resort guard means even a STRUCTURAL bug there can
    # never kill the run again — two morning briefs died to exactly
    # that propagation (2026-04-30 lost outright, 2026-05-10 delayed
    # ~9h; TypeError from mixed-type API fields reached
    # brief.daemon.error). Weather is one section of the brief; the
    # brief must out-rank it.
    try:
        weather_md = await fetch_and_format(config.weather)
    except Exception as exc:  # noqa: BLE001 — section never kills the run
        log.warning(
            "brief.weather_section_failed",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        weather_md = "*Weather unavailable.*"

    # Operations snapshot. quarantine_dir_name threads through from the
    # email_classifier YAML block via BriefConfig.load_from_unified so a
    # per-instance override on the classifier surfaces in the brief
    # without a separate brief config knob (2026-05-31 followup to
    # 164839a — code-reviewer WARN: previously hardcoded to default).
    data_dir = str(Path(config.state.path).parent)
    ops_md = format_operations_section(
        data_dir,
        config.vault_path,
        since=today,
        quarantine_dir_name=config.quarantine_dir_name,
    )

    # Health section — reads the latest BIT record from vault/run/
    # (vault/process/ for pre-2026-06-12 legacy records), falling back
    # to the BIT state file if no record is available.
    bit_state_path = Path(data_dir) / "bit_state.json"
    health_md = render_health_section(
        config.vault_path,
        state_path=bit_state_path,
        today=today,
    )

    # Watch Items — config-driven upstream checks (PRs, release
    # mentions) run LIVE by the brief, weather-style. Empty string when
    # no watches configured → section omitted entirely. Same section-
    # boundary containment idiom as weather (874c751): the module owns
    # per-item degradation, and this last-resort guard means even a
    # structural watches bug can never kill the run. CancelledError
    # derives from BaseException, so daemon-shutdown cancellation
    # propagates through the ``except Exception`` untouched.
    watches_md = ""
    if config.watches:
        try:
            watches_md = await check_and_format_watches(
                config.watches,
                state_path=Path(data_dir) / "brief_watches_state.json",
            )
        except Exception as exc:  # noqa: BLE001 — section never kills the run
            log.warning(
                "brief.watches_section_failed",
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            watches_md = "*Watch checks unavailable.*"

    # Upcoming Events — forward-looking calendar slice. Empty string
    # when the section is disabled in config; that signals "omit
    # entirely from the brief". A populated string (including the
    # "No upcoming events." sentinel) means render the header.
    today_local = datetime.now(ZoneInfo(config.schedule.timezone)).date()
    upcoming_md = render_upcoming_events_section(
        config.upcoming_events,
        config.vault_path,
        today_local,
    )

    # Peer Digests — V.E.R.A. content arc. One ``### {Peer} Update``
    # sub-section per expected peer (or per peer with a record today).
    # Empty string when the section is disabled OR no peers configured
    # AND nothing arrived. See peer_digests.py for the
    # intentionally-left-blank semantics.
    peer_digests_md = ""
    if config.peer_digests.enabled:
        peer_digests_md = render_peer_digests_section(
            config.vault_path,
            today_local.isoformat(),
            expected_peers=config.peer_digests.expected_peers,
            peer_canonical_names=config.peer_digests.peer_canonical_names,
        )

    # Open Tasks by Tier — Salem-only Phase 1 (2026-05-28). Live vault
    # scan over ``vault/task/*.md``, filters open tasks, computes
    # ``effective_tier`` per task (deadline-relative escalation), renders
    # three buckets T1/T2/T3. Unlike the routine section's filesystem
    # handoff, tier is a pure projection of current task records + now;
    # no aggregator daemon writes a derivative file. ``render_tier_section``
    # always returns a non-empty string per intentionally-left-blank, so
    # this stays in the section list unconditionally.
    now_local = datetime.now(ZoneInfo(config.schedule.timezone))
    tier_md = render_tier_section(
        Path(config.vault_path), now_local, config.tier_defaults,
    )

    # Today's Routines — Salem-only Phase 1. Renders the body of
    # ``vault/daily/<today>.md`` (written by the routine daemon at
    # 05:59 Halifax). High-attention slot per dispatch — time-anchored
    # actions for the day belong above the retrospective Operations
    # summary so the reader sees what to DO before what was. Empty
    # string when no salem instance / no daily note expected — but
    # render_routine_section always returns a non-empty sentinel string
    # per intentionally-left-blank, so this stays in the section list
    # unconditionally (no enable/disable gate yet; Phase 2 may add one
    # via brief config).
    routines_md = render_routine_section(Path(config.vault_path), today_local)

    # STAY-C Bug Relay — reads the box watcher's relay spool and renders one
    # PHI-free count line (never bug bodies — the brief transits Telegram and
    # STAY-C uses none). Empty string when disabled (Salem-only, opt-in);
    # when enabled ALWAYS returns a line (count, or an explicit no-data /
    # stale signal so a dead watcher is visible). ``now_utc`` drives the
    # staleness check against the spool's ``generated_at``.
    stayc_relay_md = render_stayc_bug_relay_section(
        config.stayc_bug_relay, datetime.now(timezone.utc),
    )
    # STAY-C Retention Review Relay (§4 / C3) — the same PHI-free, ILB discipline: the review_due
    # count + the oldest OPAQUE encounter_id, or an explicit no-data / stale signal. Empty when
    # disabled (Salem-only, opt-in).
    stayc_retention_md = render_stayc_retention_relay_section(
        config.stayc_retention_relay, datetime.now(timezone.utc),
    )

    # Section order is load-bearing: Health first (readers scan top-down;
    # critical status gets the highest priority real estate), Weather
    # second (time-sensitive but non-operational), Open Tasks by Tier
    # third (deadline-driven actionable queue — T1 tasks are the most
    # attention-critical line of the brief; ratified 2026-05-28 to sit
    # above Routines because a missed payroll deadline outranks today's
    # habit-anchor checklist), Today's Routines fourth (recurring
    # practice anchors — time-anchored but lower stakes than deadline-
    # driven tasks), Operations fifth (retrospective summary — less
    # time-sensitive than the others). Upcoming Events sixth because
    # it's forward-looking — useful context once the reader has absorbed
    # current state, but lower priority than health/weather/now-state
    # summaries. Peer Digests last (before signature) because they
    # represent OTHER instances' takes on their own state — informational,
    # not actionable by Salem's reader on their own. Sits AFTER Upcoming
    # Events so the principal's own forward calendar is the immediately-
    # actionable surface and peer chatter follows.
    sections = [
        ("Health", health_md),
        ("Weather", weather_md),
        (TIER_SECTION_HEADER, tier_md),
        ("Today's Routines", routines_md),
        ("Operations", ops_md),
    ]
    # Watch Items sits after Operations, before Upcoming Events:
    # upstream watches are forward-looking operational signals — more
    # actionable than the calendar slice when one flips (🚨 lines carry
    # the operator's own action note), less time-critical than today's
    # tasks/routines when stable. Rendered ONLY when ≥1 watch is
    # configured (the empty string from an unconfigured feature is the
    # one permitted silence; every CONFIGURED watch yields a line).
    if watches_md:
        sections.append(("Watch Items", watches_md))
    # STAY-C Bug Relay sits alongside Watch Items — both are upstream
    # operational signals (something outside Salem needs triage). Rendered
    # ONLY when the feature is enabled (the empty string from a disabled /
    # unconfigured-instance feature is the one permitted silence; an ENABLED
    # relay always yields a line, including the no-data / stale signal).
    if stayc_relay_md:
        sections.append((STAYC_RELAY_SECTION_HEADER, stayc_relay_md))
    # STAY-C Retention Review sits beside the Bug Relay — both are upstream operational signals from
    # the STAY-C box (something outside Salem needs triage). Rendered ONLY when enabled (the empty
    # string from a disabled instance is the one permitted silence; an ENABLED relay always yields a
    # line, incl. the no-data / stale signal).
    if stayc_retention_md:
        sections.append((STAYC_RETENTION_SECTION_HEADER, stayc_retention_md))
    if upcoming_md:
        sections.append(("Upcoming Events", upcoming_md))
    if peer_digests_md:
        sections.append(("Peer Digests", peer_digests_md))

    # Render
    frontmatter, body = render_brief(today, sections, config)
    content = serialize_record(frontmatter, body)

    # Write to vault
    vault_path = Path(config.vault_path)
    # Ensure the process hub the record's ``process`` field links to
    # exists — the janitor has no create scope, so it can never
    # self-heal the dangling link (LINK001) if we don't.
    ensure_process_hub(vault_path, config, today)
    name = config.output.name_template.replace("{date}", today)
    rel_path = f"{config.output.directory}/{name}.md"
    file_path = vault_path / rel_path

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")

    log.info("brief.written", path=rel_path)

    # Update state
    state_mgr.state.add_run(BriefRun(
        date=today,
        generated_at=datetime.now(timezone.utc).isoformat(),
        vault_path=rel_path,
        sections=["weather"],
        success=True,
    ))
    state_mgr.save()

    # Post-write push to Telegram — best-effort, swallows transport
    # errors so the brief stays in the vault even if the talker daemon
    # is restarting. ``primary_telegram_user_id`` is None when no
    # telegram section is configured — skip the push silently.
    if config.primary_telegram_user_id is not None:
        await _push_brief_to_telegram(
            content, today, config.primary_telegram_user_id,
        )

    return rel_path


async def update_weather(config: BriefConfig) -> str:
    """Update the weather section in today's brief. Creates the brief if it doesn't exist."""
    today = date.today().isoformat()
    vault_path = Path(config.vault_path)
    name = config.output.name_template.replace("{date}", today)
    rel_path = f"{config.output.directory}/{name}.md"
    file_path = vault_path / rel_path

    log.info("brief.updating_weather", date=today)

    # Fetch fresh weather
    weather_md = await fetch_and_format(config.weather)

    if not file_path.exists():
        # No brief yet — generate a full one. This branch writes a run
        # record carrying the ``process`` hub link too, so it ensures
        # the hub the same way generate_brief does.
        ensure_process_hub(vault_path, config, today)
        sections = [("Weather", weather_md)]
        frontmatter, body = render_brief(today, sections, config)
        content = serialize_record(frontmatter, body)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        log.info("brief.weather_created", path=rel_path)
        return rel_path

    # Brief exists — replace the weather section in-place
    existing = file_path.read_text(encoding="utf-8")

    # Find and replace the ## Weather section
    import re
    # Match from "## Weather\n" to the next "## " or "---" divider or end of file
    pattern = r"(## Weather\n)(.*?)(\n## |\n---|\Z)"
    replacement_body = f"## Weather\n\n{weather_md}\n"

    match = re.search(pattern, existing, re.DOTALL)
    if match:
        # Preserve what comes after
        suffix = match.group(3)
        updated = existing[:match.start()] + replacement_body + suffix + existing[match.end():]
        file_path.write_text(updated, encoding="utf-8")
        log.info("brief.weather_updated", path=rel_path)
    else:
        log.warning("brief.weather_section_not_found", path=rel_path)

    return rel_path


async def run_daemon(config: BriefConfig) -> None:
    """Daily brief generator daemon. Runs at configured time."""
    log.info("brief.daemon.starting",
             schedule_time=config.schedule.time,
             tz=config.schedule.timezone)

    state_mgr = StateManager(config.state.path)
    state_mgr.load()

    # Catch-up-on-startup (2026-05-28): if the daemon boots after
    # today's scheduled fire window has passed AND state shows no
    # successful fire today, fire immediately before entering the
    # normal sleep loop. Closes the false-FAIL class where a host
    # restart mid-day leaves the daemon sleeping until tomorrow
    # while probes correctly flag the missed window.
    #
    # Per ``feedback_intentionally_left_blank.md``: the catch-up path
    # is observability-load-bearing — emit ``brief.daemon.catchup_fired``
    # so operators can count incidents and characterise the lateness
    # distribution via grep on the log.
    try:
        tz_boot = ZoneInfo(config.schedule.timezone)
        now_boot = datetime.now(tz_boot)
        today_iso_boot = now_boot.date().isoformat()
        already_fired = state_mgr.state.has_brief_for_date(today_iso_boot)
        should_catch, intended_fire, delay_seconds = should_catchup_today(
            config.schedule, now_boot, already_fired,
        )
        if should_catch:
            log.info(
                "brief.daemon.catchup_fired",
                date=today_iso_boot,
                intended_fire_time=intended_fire.isoformat(),
                actual_fire_time=now_boot.isoformat(),
                delay_seconds=round(delay_seconds, 1),
            )
            try:
                path = await generate_brief(config, state_mgr)
                if path:
                    log.info("brief.daemon.catchup_generated", path=path)
            except Exception as exc:
                # Mirror the scheduled-fire failure capture pattern
                # (record_error + log + swallow). Daemons must not
                # crash; the BIT probe will surface the failure via
                # state.last_error.
                state_mgr.record_error(f"{type(exc).__name__}: {exc}")
                log.exception("brief.daemon.catchup_error")
    except Exception:  # noqa: BLE001
        # Defensive: the catch-up decision helper raising (e.g.
        # malformed schedule config) MUST NOT prevent the daemon
        # from entering its normal loop. Log + continue.
        log.exception("brief.daemon.catchup_decision_failed")

    while True:
        tz = ZoneInfo(config.schedule.timezone)
        now = datetime.now(tz)
        # Clock-aligned next-fire via shared helper (see
        # ``alfred.common.schedule``). Daily-only for brief; the weekly
        # gate there is used by distiller consolidation, not brief.
        target = compute_next_fire(config.schedule, now)
        sleep_seconds = (target - now).total_seconds()

        if sleep_seconds > 0:
            log.info("brief.daemon.sleeping",
                     next_run=target.isoformat(),
                     sleep_seconds=round(sleep_seconds, 1),
                     sleep_hours=round(sleep_seconds / 3600, 1))
            # Wall-clock-checked chunked sleep — defends against
            # monotonic clock drift during long sleeps (WSL2 host
            # suspend/resume, NTP adjustments). See
            # ``alfred.common.schedule.sleep_until`` for the rationale.
            actual_seconds = await sleep_until(target)
            log.info("brief.daemon.woke",
                     intended_seconds=round(sleep_seconds, 1),
                     actual_seconds=round(actual_seconds, 1),
                     drift_seconds=round(actual_seconds - sleep_seconds, 1))

        # Best-effort vault snapshot BEFORE brief — captures previous day's work
        try:
            from alfred.vault.snapshot import (
                build_snapshot_summary,
                get_status,
                take_snapshot,
            )

            vault_path = Path(config.vault_path)
            audit_log = Path(config.state.path).parent / "vault_audit.log"
            status = get_status(vault_path)
            since = status.get("last_commit_date")
            summary = build_snapshot_summary(audit_log, since=since)
            commit = take_snapshot(vault_path, message=summary)
            if commit:
                log.info("brief.daemon.snapshot", commit=commit)
            else:
                log.info("brief.daemon.snapshot_noop")
        except Exception:
            log.warning("brief.daemon.snapshot_failed", exc_info=True)

        # Generate the morning brief
        try:
            path = await generate_brief(config, state_mgr)
            if path:
                log.info("brief.daemon.generated", path=path)
        except Exception as exc:
            # Capture failure cause into state so the BIT
            # ``last-successful-brief`` probe surfaces the message on
            # its detail line. Keeps the swallow-the-exception
            # behaviour (daemons must not crash); just labels the
            # swallow. Added 2026-05-14 — closes the diagnostic gap
            # the 2026-04-30 → 05-10 incident exposed (BIT could
            # detect the silent fail but not say WHY).
            state_mgr.record_error(f"{type(exc).__name__}: {exc}")
            log.exception("brief.daemon.error")

        # Sleep 60s to avoid double-firing
        await asyncio.sleep(60)
