"""Daily Sync daemon — fires the assembled message at 09:00 ADT.

Process model: one process per instance, started by the orchestrator
when ``daily_sync:`` is in ``config.yaml`` and ``enabled: true``. Mirrors
the brief daemon's shape (see ``src/alfred/brief/daemon.py``):

  1. Load config + state.
  2. Compute next fire time via ``compute_next_fire``.
  3. ``sleep_until`` (drift-bounded chunked sleep).
  4. Assemble message via the section-provider registry.
  5. Push to Telegram via the outbound transport.
  6. Persist the batch + message_ids to the state file so the reply
     parser can resolve "item 2" → record path.
  7. Loop.

The daemon does NOT consume replies — that's the talker bot's job (it
already owns the Telegram long-poll loop). This daemon is push-only;
replies feed back through the ``/calibrate``-aware reply parser in
:mod:`alfred.telegram.bot`.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from alfred.common.schedule import (
    compute_next_fire,
    should_catchup_today,
    sleep_until,
)

from . import (
    attribution_section,
    canonical_proposals_section,
    email_section,
    friction_section,
    pending_items_section,
    radar_section,
    routine_match_section,
    triage_section,
)
from .assembler import assemble_message
from .config import DailySyncConfig
from .confidence import (
    clear_last_error_on_state,
    load_state,
    record_error_on_state,
    save_state,
)

log = structlog.get_logger(__name__)


async def _push_via_transport(
    body: str,
    user_id: int,
    today_iso: str,
    *,
    dedupe_key: str | None = None,
) -> list[int]:
    """Dispatch the Daily Sync as Telegram chunks via the transport.

    ``dedupe_key`` is the server-side idempotency key (24h window). When
    omitted, falls back to ``daily-sync-{today_iso}`` — the natural
    auto-fire key, which deliberately collides on same-day re-fires so
    a scheduling glitch can't double-push. The ``/calibrate`` slash
    command passes a unique-per-invocation key instead so an explicit
    out-of-cycle fire isn't short-circuited by the auto-fire's prior
    entry in ``data/transport_state.json``.

    Returns the list of message IDs the server reports for the batch
    (the reply parser uses the FIRST id as the parent-thread anchor).
    Empty list on transport failure — failure is non-fatal because the
    state file is the source of truth for "did we fire today".
    """
    from alfred.transport.client import send_outbound_batch
    from alfred.transport.exceptions import TransportError
    from alfred.transport.utils import chunk_for_telegram

    if dedupe_key is None:
        dedupe_key = f"daily-sync-{today_iso}"

    try:
        chunks = chunk_for_telegram(body)
        if not chunks or not chunks[0]:
            return []
        response = await send_outbound_batch(
            user_id=user_id,
            chunks=chunks,
            dedupe_key=dedupe_key,
            client_name="daily_sync",
        )
        # The server's response shape includes ``telegram_message_ids`` —
        # a list of int Telegram message_ids the reply parser keys off.
        # Fall back to ``message_ids`` (older alias) defensively.
        ids = response.get("telegram_message_ids") or response.get("message_ids") or []
        if not isinstance(ids, list):
            ids = []
        out: list[int] = []
        for x in ids:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out
    except TransportError as exc:
        log.warning(
            "daily_sync.push_failed",
            date=today_iso,
            error_type=exc.__class__.__name__,
            error=str(exc),
            response_summary=f"{exc.__class__.__name__}: {exc}",
        )
        return []


def _build_state_payload(
    today_iso: str,
    items: list[Any],
    message_ids: list[int],
    *,
    attribution_items: list[Any] | None = None,
    proposal_items: list[Any] | None = None,
    pending_items: list[Any] | None = None,
    radar_items: list[Any] | None = None,
    friction_items: list[Any] | None = None,
    routine_match_items: list[Any] | None = None,
) -> dict[str, Any]:
    """Construct the per-fire batch payload persisted to the state file.

    ``items`` is the email-calibration batch (existing). ``attribution_items``
    (Phase 2) is the parallel attribution-audit batch — both lists are
    keyed off the same ``message_ids`` so the dispatcher routes a reply
    to whichever item_number matches. ``proposal_items`` (propose-person
    c2) is the parallel canonical-proposals batch. ``pending_items``
    (Pending Items Queue Phase 1) is the parallel cross-instance
    pending-items batch. ``radar_items`` (distiller-radar Phase 3b) is
    the parallel distiller-radar batch. ``friction_items`` (K3 c2) is
    the parallel KAL-LE friction-queue batch — informational items
    today; smart-routing dispatcher hooks deferred. ``routine_match_items``
    (self-correcting matcher Phase 2b) is the parallel low-confidence
    routine-match review batch — confirm/reject routes a verdict into the
    learned glossary corpus. The reply parser reads every list to resolve
    item numbers against a Telegram reply.
    """
    payload: dict[str, Any] = {
        "date": today_iso,
        "items": [item.to_dict() for item in items if hasattr(item, "to_dict")],
        "message_ids": message_ids,
        "fired_at": datetime.now(timezone.utc).isoformat(),
    }
    if attribution_items:
        payload["attribution_items"] = [
            item.to_dict() for item in attribution_items if hasattr(item, "to_dict")
        ]
    if proposal_items:
        payload["proposal_items"] = [
            item.to_dict() for item in proposal_items if hasattr(item, "to_dict")
        ]
    if pending_items:
        payload["pending_items"] = [
            item.to_dict() for item in pending_items if hasattr(item, "to_dict")
        ]
    if radar_items:
        payload["radar_items"] = [
            item.to_dict() for item in radar_items if hasattr(item, "to_dict")
        ]
    if friction_items:
        payload["friction_items"] = [
            item.to_dict() for item in friction_items if hasattr(item, "to_dict")
        ]
    if routine_match_items:
        payload["routine_match_items"] = [
            item.to_dict() for item in routine_match_items if hasattr(item, "to_dict")
        ]
    return payload


async def fire_once(
    config: DailySyncConfig,
    vault_path: Path,
    user_id: int,
    today: date | None = None,
    *,
    manual: bool = False,
    raw_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one assemble + push cycle. Returns a result summary dict.

    Used by both the daemon loop AND the ``/calibrate`` slash command
    (out-of-cycle fire). Two-path dedupe at the transport layer:

      * ``manual=False`` (default; daemon's natural 09:00 auto-fire)
        — uses ``daily-sync-{date}`` so a scheduling/restart glitch
        that fires the auto-loop twice in one day is server-deduped
        into a single Telegram push. The daemon-side
        ``last_fired_date`` guard already prevents most re-fires;
        this is the belt to that suspenders.
      * ``manual=True`` (``/calibrate`` slash command) — uses
        ``daily-sync-{date}-calibrate-{uuid8}`` so each explicit
        out-of-cycle fire gets its own unique idempotency key. Without
        this, the second /calibrate of a day collides with the first
        successful send (auto OR manual), the server returns the cached
        msg_ids without actually pushing, and Andrew sees the
        "firing now…" ack but no batch on Telegram. The calibrate-tagged
        prefix keeps ``data/transport_state.json`` greppable.

    The summary dict carries:
      - ``ok``: bool
      - ``items_count``: int (zero when empty-Daily-Sync header used)
      - ``message_ids``: list[int]
      - ``body``: str (the assembled message text — useful for testing)
      - ``dedupe_key``: str (the key actually sent to the transport —
        useful for tests and audit-trail correlation)
    """
    today = today or date.today()
    today_iso = today.isoformat()
    if manual:
        dedupe_key = f"daily-sync-{today_iso}-calibrate-{uuid.uuid4().hex[:8]}"
    else:
        dedupe_key = f"daily-sync-{today_iso}"

    # Make sure each section provider knows where the vault is, and
    # that all providers are registered (idempotent — re-firing
    # via ``/calibrate`` doesn't double-register).
    email_section.set_vault_path(vault_path)
    email_section.register()
    attribution_section.set_vault_path(vault_path)
    attribution_section.register()
    # canonical_proposals_section reads the transport config itself
    # (the queue path lives under transport.canonical.proposals_path),
    # so it doesn't need set_vault_path.
    canonical_proposals_section.register()
    # pending_items_section reads queue + aggregate paths from the
    # pending_items config block. Priority 5 puts it ABOVE email at
    # 10. When raw_config is plumbed through, stash it so the section
    # provider skips the per-fire ``open("config.yaml")`` round-trip
    # AND avoids the cwd-relative-path fragility.
    if raw_config is not None:
        pending_items_section.set_raw_config(raw_config)
    pending_items_section.register()
    # Radar section (Phase 3b): reads <vault>/digests/daily/YYYY-MM-DD.md
    # written by the distiller-radar Phase 3a CLI/daemon. Registered
    # unconditionally — when the daily file is missing, the section
    # provider returns None and the section is omitted, so instances
    # that don't run radar (Salem/Hypatia today) stay unaffected.
    radar_section.set_digests_dir(vault_path / "digests")
    radar_section.register()
    # Friction queue section (K3 c2): reads the friction-event JSONL
    # written by the K3 c1 analyzer. Registered unconditionally — the
    # provider returns None when set_friction_log_path was never
    # called, so instances without friction_analyzer (Salem/Hypatia
    # today) stay unaffected. KAL-LE's daemon sets the path from the
    # configured friction_analyzer.log_path; instances that disable
    # friction_analyzer leave the holder unset and the section omits.
    if config.friction_analyzer.enabled and config.friction_analyzer.log_path:
        friction_section.set_friction_log_path(
            Path(config.friction_analyzer.log_path).expanduser().resolve(),
        )
    friction_section.register()
    # Triage Queue section (Tier-V2 Ship 3, 2026-05-29): reads
    # vault/task/*.md and surfaces records flagged
    # ``alfred_triage: True``. Priority 24 — between friction (23) and
    # attribution (25). Registered unconditionally — when no triage
    # records exist, the provider emits the "no triage items today"
    # sentinel per intentionally-left-blank. Vault path is the same
    # one attribution_section uses, so we can call set_vault_path
    # with the already-resolved variable.
    triage_section.set_vault_path(vault_path)
    triage_section.register()
    # Self-correcting routine matcher surface (Phase 1, read-only): lists
    # low-confidence routine_done fuzzy matches from the routine capture sink.
    # Registered unconditionally — the provider returns None when
    # ``routine_match.enabled`` is False (Salem opts in via config), so other
    # instances stay unaffected. The pending path is read from config inside
    # the provider (no set_* holder needed).
    routine_match_section.register()

    body = assemble_message(config, today)
    items = email_section.consume_last_batch()
    attribution_items = attribution_section.consume_last_batch()
    proposal_items = canonical_proposals_section.consume_last_batch()
    pending_items = pending_items_section.consume_last_batch()
    radar_items = radar_section.consume_last_batch()
    friction_items = friction_section.consume_last_batch()
    triage_items = triage_section.consume_last_batch()
    routine_match_items = routine_match_section.consume_last_batch()

    log.info(
        "daily_sync.assembled",
        date=today_iso,
        items_count=len(items),
        attribution_items_count=len(attribution_items),
        proposal_items_count=len(proposal_items),
        pending_items_count=len(pending_items),
        radar_items_count=len(radar_items),
        friction_items_count=len(friction_items),
        triage_items_count=len(triage_items),
        routine_match_items_count=len(routine_match_items),
        body_length=len(body),
        manual=manual,
        dedupe_key=dedupe_key,
    )

    message_ids = await _push_via_transport(
        body, user_id, today_iso, dedupe_key=dedupe_key,
    )

    # Persist the batch to the state file ONLY when we actually have
    # items + message_ids. An empty-Daily-Sync push has no items to
    # match replies against — Andrew can still chat, but there's
    # nothing to calibrate. Any of email / attribution / proposals /
    # pending is enough to persist the batch (the dispatcher routes
    # per-item).
    state = load_state(config.state.path)
    if (
        items or attribution_items or proposal_items
        or pending_items or radar_items or friction_items
        or routine_match_items
    ) and message_ids:
        state["last_batch"] = _build_state_payload(
            today_iso,
            items,
            message_ids,
            attribution_items=attribution_items,
            proposal_items=proposal_items,
            pending_items=pending_items,
            radar_items=radar_items,
            friction_items=friction_items,
            routine_match_items=routine_match_items,
        )
    state["last_fired_date"] = today_iso
    # Clear-on-success: reaching this save point means the fire
    # completed without raising → wipe stale failure context. The
    # outer daemon-loop ``except Exception:`` at daemon.py owns the
    # failure-side write via record_error_on_state. Mirrors the
    # brief.State.add_run clear-on-success pattern (2026-05-14).
    clear_last_error_on_state(state)
    save_state(config.state.path, state)

    return {
        "ok": True,
        "items_count": len(items),
        "attribution_items_count": len(attribution_items),
        "proposal_items_count": len(proposal_items),
        "pending_items_count": len(pending_items),
        "radar_items_count": len(radar_items),
        "friction_items_count": len(friction_items),
        "routine_match_items_count": len(routine_match_items),
        "message_ids": message_ids,
        "body": body,
        "dedupe_key": dedupe_key,
    }


async def run_daemon(
    config: DailySyncConfig,
    vault_path: Path,
    user_id: int,
    raw_config: dict[str, Any] | None = None,
) -> None:
    """Daily Sync daemon — fires once per ``schedule.time`` ADT day.

    Loops until SIGTERM (the orchestrator handles that signal). The
    drift-bounded ``sleep_until`` keeps the fire wall-clock-aligned
    even on WSL2 with monotonic clock skew (same fix the brief daemon
    adopted in commit 9755ed7-ish).

    ``raw_config`` is the pre-loaded unified config dict (the
    orchestrator's ``_run_daily_sync`` already has it). Threaded
    through to ``fire_once`` → ``pending_items_section.set_raw_config``
    so the section provider skips the per-fire config-load round-trip.
    """
    log.info(
        "daily_sync.daemon.starting",
        schedule_time=config.schedule.time,
        tz=config.schedule.timezone,
        user_id=user_id,
        vault=str(vault_path),
    )

    # Catch-up-on-startup (2026-05-28): if the daemon boots after
    # today's scheduled fire window has passed AND state shows no
    # fire today, fire immediately before entering the normal sleep
    # loop. Closes the false-FAIL class where a host restart mid-day
    # leaves the daemon sleeping until tomorrow.
    #
    # Per ``feedback_intentionally_left_blank.md``: emit
    # ``daily_sync.daemon.catchup_fired`` so operators can count
    # incidents and characterise lateness distribution via grep.
    try:
        tz_boot = ZoneInfo(config.schedule.timezone)
        now_boot = datetime.now(tz_boot)
        today_boot = now_boot.date()
        today_iso_boot = today_boot.isoformat()
        state_boot = load_state(config.state.path)
        already_fired = (
            state_boot.get("last_fired_date") == today_iso_boot
        )
        should_catch, intended_fire, delay_seconds = should_catchup_today(
            config.schedule, now_boot, already_fired,
        )
        if should_catch:
            log.info(
                "daily_sync.daemon.catchup_fired",
                date=today_iso_boot,
                intended_fire_time=intended_fire.isoformat(),
                actual_fire_time=now_boot.isoformat(),
                delay_seconds=round(delay_seconds, 1),
            )
            try:
                result = await fire_once(
                    config, vault_path, user_id, today=today_boot,
                    raw_config=raw_config,
                )
                log.info(
                    "daily_sync.daemon.catchup_completed",
                    date=today_iso_boot,
                    items=result["items_count"],
                    message_ids=result["message_ids"],
                )
            except Exception as exc:  # noqa: BLE001
                # Mirror the scheduled-fire failure capture: record
                # the error into state so the BIT probe surfaces it;
                # swallow so the daemon enters the normal loop.
                record_error_on_state(
                    config.state.path,
                    f"{type(exc).__name__}: {exc}",
                )
                log.exception("daily_sync.daemon.catchup_error")
    except Exception:  # noqa: BLE001
        # Defensive: catch-up decision helper raising (e.g. malformed
        # schedule config) MUST NOT prevent the daemon from entering
        # its normal loop. Log + continue.
        log.exception("daily_sync.daemon.catchup_decision_failed")

    while True:
        tz = ZoneInfo(config.schedule.timezone)
        now = datetime.now(tz)
        target = compute_next_fire(config.schedule, now)
        sleep_seconds = (target - now).total_seconds()

        if sleep_seconds > 0:
            log.info(
                "daily_sync.daemon.sleeping",
                next_run=target.isoformat(),
                sleep_seconds=round(sleep_seconds, 1),
                sleep_hours=round(sleep_seconds / 3600, 1),
            )
            actual_seconds = await sleep_until(target)
            log.info(
                "daily_sync.daemon.woke",
                intended_seconds=round(sleep_seconds, 1),
                actual_seconds=round(actual_seconds, 1),
                drift_seconds=round(actual_seconds - sleep_seconds, 1),
            )

        # Fire — same-day dedup at the persistence layer.
        today = datetime.now(tz).date()
        state = load_state(config.state.path)
        if state.get("last_fired_date") == today.isoformat():
            log.info("daily_sync.daemon.already_fired_today", date=today.isoformat())
        else:
            try:
                result = await fire_once(
                    config, vault_path, user_id, today=today,
                    raw_config=raw_config,
                )
                log.info(
                    "daily_sync.daemon.fired",
                    date=today.isoformat(),
                    items=result["items_count"],
                    message_ids=result["message_ids"],
                )
            except Exception as exc:  # noqa: BLE001
                # Capture failure cause into state so the BIT
                # ``last-successful-fire`` probe surfaces the message
                # on its detail line. Keeps the swallow-the-exception
                # behaviour (daemons must not crash); just labels the
                # swallow. Added 2026-05-14 — mirrors the brief,
                # janitor, and distiller daemon captures (closes the
                # cross-daemon diagnostic-gap class per
                # ``project_cross_daemon_swallow_audit.md``).
                record_error_on_state(
                    config.state.path, f"{type(exc).__name__}: {exc}",
                )
                log.exception("daily_sync.daemon.fire_error")

        # Sleep 60s to avoid double-firing within the same minute.
        await asyncio.sleep(60)
