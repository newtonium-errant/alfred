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

from alfred.common.schedule import compute_next_fire, sleep_until

from . import attribution_section, canonical_proposals_section, email_section
from .assembler import assemble_message
from .config import DailySyncConfig
from .confidence import load_state, save_state

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
) -> dict[str, Any]:
    """Construct the per-fire batch payload persisted to the state file.

    ``items`` is the email-calibration batch (existing). ``attribution_items``
    (Phase 2) is the parallel attribution-audit batch — both lists are
    keyed off the same ``message_ids`` so the dispatcher routes a reply
    to whichever item_number matches. ``proposal_items`` (propose-person
    c2) is the parallel canonical-proposals batch. The reply parser
    reads every list to resolve item numbers against a Telegram reply.
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
    return payload


async def fire_once(
    config: DailySyncConfig,
    vault_path: Path,
    user_id: int,
    today: date | None = None,
    *,
    manual: bool = False,
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

    body = assemble_message(config, today)
    items = email_section.consume_last_batch()
    attribution_items = attribution_section.consume_last_batch()
    proposal_items = canonical_proposals_section.consume_last_batch()

    log.info(
        "daily_sync.assembled",
        date=today_iso,
        items_count=len(items),
        attribution_items_count=len(attribution_items),
        proposal_items_count=len(proposal_items),
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
    # nothing to calibrate. Any of email / attribution / proposals is
    # enough to persist the batch (the dispatcher routes per-item).
    state = load_state(config.state.path)
    if (items or attribution_items or proposal_items) and message_ids:
        state["last_batch"] = _build_state_payload(
            today_iso,
            items,
            message_ids,
            attribution_items=attribution_items,
            proposal_items=proposal_items,
        )
    state["last_fired_date"] = today_iso
    save_state(config.state.path, state)

    return {
        "ok": True,
        "items_count": len(items),
        "attribution_items_count": len(attribution_items),
        "proposal_items_count": len(proposal_items),
        "message_ids": message_ids,
        "body": body,
        "dedupe_key": dedupe_key,
    }


async def run_daemon(
    config: DailySyncConfig,
    vault_path: Path,
    user_id: int,
) -> None:
    """Daily Sync daemon — fires once per ``schedule.time`` ADT day.

    Loops until SIGTERM (the orchestrator handles that signal). The
    drift-bounded ``sleep_until`` keeps the fire wall-clock-aligned
    even on WSL2 with monotonic clock skew (same fix the brief daemon
    adopted in commit 9755ed7-ish).
    """
    log.info(
        "daily_sync.daemon.starting",
        schedule_time=config.schedule.time,
        tz=config.schedule.timezone,
        user_id=user_id,
        vault=str(vault_path),
    )

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
                result = await fire_once(config, vault_path, user_id, today=today)
                log.info(
                    "daily_sync.daemon.fired",
                    date=today.isoformat(),
                    items=result["items_count"],
                    message_ids=result["message_ids"],
                )
            except Exception:  # noqa: BLE001
                log.exception("daily_sync.daemon.fire_error")

        # Sleep 60s to avoid double-firing within the same minute.
        await asyncio.sleep(60)
