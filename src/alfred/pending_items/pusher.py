"""Periodic peer-push daemon for the pending-items queue.

Runs on every NON-SALEM instance with a ``pending_items`` config
block (KAL-LE, Hypatia, etc.). Wakes every
``push.interval_seconds`` (default 5 minutes), scans the local queue
for items with ``pushed_to_salem=False AND status=pending``, pushes
them to Salem's ``/peer/pending_items_push`` endpoint, and marks them
``pushed_to_salem=True`` on success.

Salem itself omits the block (or sets ``push.target_peer=""``) — it
aggregates peer pushes via the inbound endpoint instead.

This daemon also runs the outbound-failure scanner on the same
cadence so new session-frontmatter ``outbound_failures`` entries get
emitted into the local queue and flushed in the same cycle. The
expiry sweep runs once per day at the start of the next cycle past
midnight UTC — kept out of the periodic flush so a busy minute
doesn't get bogged down in disk scans.

Ride the same Salem-down handling pattern as
:mod:`alfred.transport.client._peer_request`: 4xx is a hard fail
(don't retry on next flush; mark pushed_to_salem to avoid re-push).
5xx + connection errors leave the item at False so the next flush
retries.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from alfred.transport.config import TransportConfig

from .config import PendingItemsConfig
from .outbound_failure import scan_and_emit
from .queue import (
    PendingItem,
    iter_items,
    list_pending,
    mark_expired,
    mark_pushed_to_salem,
)
from .view import regenerate_view

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Single flush
# ---------------------------------------------------------------------------


async def flush_once(
    config: PendingItemsConfig,
    transport_config: TransportConfig,
    vault_path: Path,
    *,
    instance_name: str,
) -> dict[str, Any]:
    """Run one detection + push + view-regen cycle.

    Returns a summary dict::

        {
          "scanner": {<scan_and_emit summary>},
          "pushed": <int>,
          "failed_push": <int>,
          "expired": <int>,
          "view_regenerated": <bool>,
          "errors": [<str>, ...]
        }

    Failure is non-fatal at the daemon level — the loop logs and
    continues so the next cycle still runs.
    """
    summary: dict[str, Any] = {
        "scanner": {},
        "pushed": 0,
        "failed_push": 0,
        "expired": 0,
        "view_regenerated": False,
        "errors": [],
    }

    # 1. Scan session/ for new outbound_failures.
    if config.outbound_failure_detector.enabled:
        try:
            scan_summary = scan_and_emit(
                vault_path=vault_path,
                queue_path=config.queue_path,
                state_path=config.outbound_failure_detector.state_path,
                instance_name=instance_name,
                session_subpath=config.outbound_failure_detector.session_subpath,
            )
            summary["scanner"] = scan_summary
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(
                f"outbound_failure_scan: {exc.__class__.__name__}: {exc}"
            )
            log.exception("pending_items.pusher.scan_failed")

    # 2. Push pending items that haven't been pushed yet.
    if config.push.target_peer and config.push.self_name:
        push_summary = await _push_unflushed(
            config=config,
            transport_config=transport_config,
            instance_name=config.push.self_name,
            target_peer=config.push.target_peer,
        )
        summary["pushed"] = push_summary["pushed"]
        summary["failed_push"] = push_summary["failed"]
        summary["errors"].extend(push_summary["errors"])

    # 3. Auto-expire stale items.
    try:
        expired = _sweep_expired(config)
        summary["expired"] = expired
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(
            f"expire_sweep: {exc.__class__.__name__}: {exc}"
        )

    # 4. Regenerate the markdown view (debounced).
    try:
        view_target = vault_path / config.view_path
        wrote = regenerate_view(
            queue_path=config.queue_path,
            view_path=view_target,
            debounce_seconds=config.view_debounce_seconds,
        )
        summary["view_regenerated"] = wrote
    except Exception as exc:  # noqa: BLE001
        summary["errors"].append(
            f"view_regen: {exc.__class__.__name__}: {exc}"
        )

    log.info(
        "pending_items.pusher.flush_complete",
        scanned=summary["scanner"].get("scanned_records") if summary["scanner"] else 0,
        emitted=summary["scanner"].get("emitted") if summary["scanner"] else 0,
        pushed=summary["pushed"],
        failed_push=summary["failed_push"],
        expired=summary["expired"],
        view_regenerated=summary["view_regenerated"],
        instance=instance_name,
    )
    return summary


async def _push_unflushed(
    *,
    config: PendingItemsConfig,
    transport_config: TransportConfig,
    instance_name: str,
    target_peer: str,
) -> dict[str, Any]:
    """Push every pending item with ``pushed_to_salem=False``.

    Batches all unflushed items into a single push call. The Salem
    handler is idempotent (matches by item.id) so a partial-failure
    re-push is safe.
    """
    out = {"pushed": 0, "failed": 0, "errors": []}

    unflushed: list[PendingItem] = [
        item for item in list_pending(config.queue_path)
        if not item.pushed_to_salem
    ]
    if not unflushed:
        return out

    # Lazy import to keep the module importable even when the
    # transport client isn't wired (e.g. minimal test env).
    from alfred.transport.client import peer_push_pending_items
    from alfred.transport.exceptions import (
        TransportError,
        TransportRejected,
    )

    try:
        response = await peer_push_pending_items(
            target_peer,
            items=[item.to_dict() for item in unflushed],
            self_name=instance_name,
            config=transport_config,
        )
    except TransportRejected as exc:
        # 4xx — request is bad. Don't retry: log loudly + mark these
        # items pushed-to-salem so we don't spin re-pushing forever.
        # This is the conservative choice; a Phase 2 refinement could
        # capture the rejection on the item itself.
        log.warning(
            "pending_items.pusher.push_rejected_4xx",
            target_peer=target_peer,
            count=len(unflushed),
            status=exc.status_code,
            body=str(exc.body)[:300] if exc.body else "",
            response_summary=f"Status {exc.status_code}: {str(exc.body)[:200] if exc.body else '(no body)'}",
        )
        # Best-effort mark so we don't loop forever on unfixable input.
        for item in unflushed:
            try:
                mark_pushed_to_salem(config.queue_path, item.id)
            except Exception:  # noqa: BLE001
                pass
        out["failed"] = len(unflushed)
        out["errors"].append(f"4xx_rejected: {exc}")
        return out
    except TransportError as exc:
        # 5xx / connection / timeout — retry next cycle.
        log.warning(
            "pending_items.pusher.push_failed_transient",
            target_peer=target_peer,
            count=len(unflushed),
            error=str(exc),
            error_type=exc.__class__.__name__,
            response_summary=f"{exc.__class__.__name__}: {exc}",
        )
        out["failed"] = len(unflushed)
        out["errors"].append(f"{exc.__class__.__name__}: {exc}")
        return out
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "pending_items.pusher.push_unexpected_error",
            target_peer=target_peer,
            count=len(unflushed),
        )
        out["failed"] = len(unflushed)
        out["errors"].append(f"unexpected: {exc.__class__.__name__}: {exc}")
        return out

    received = (
        response.get("received") if isinstance(response, dict) else None
    )
    if received is None:
        received = len(unflushed)

    # Salem returned 200 → mark every successfully-received item as
    # flushed. Salem's handler echoes back any rejected ids in
    # ``response.errors`` so we leave those at False for retry.
    server_errors = (
        response.get("errors") if isinstance(response, dict) else []
    ) or []
    rejected_ids = {
        e.get("id") for e in server_errors
        if isinstance(e, dict) and e.get("id")
    }

    for item in unflushed:
        if item.id in rejected_ids:
            out["failed"] += 1
            continue
        try:
            mark_pushed_to_salem(config.queue_path, item.id)
            out["pushed"] += 1
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"mark_pushed_failed: {exc}")
            out["failed"] += 1

    log.info(
        "pending_items.pusher.push_succeeded",
        target_peer=target_peer,
        pushed=out["pushed"],
        failed=out["failed"],
        received_by_server=received,
    )
    return out


def _sweep_expired(config: PendingItemsConfig) -> int:
    """Mark items older than ``expiry.expire_days`` as expired.

    Called from the periodic flush. Cheap operation — full scan over
    the queue file, but the queue stays small in practice (≤ a few
    dozen open items). Returns the number of items expired this cycle.
    """
    if config.expiry.expire_days <= 0:
        return 0
    now = datetime.now(timezone.utc)
    expired_count = 0
    for item in iter_items(config.queue_path):
        if item.status != "pending":
            continue
        try:
            created = datetime.fromisoformat(
                item.created_at.replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            continue
        age_days = (now - created).total_seconds() / 86400.0
        if age_days >= config.expiry.expire_days:
            try:
                ok = mark_expired(config.queue_path, item.id)
                if ok:
                    expired_count += 1
                    log.info(
                        "pending_items.expired",
                        item_id=item.id,
                        age_days=round(age_days, 1),
                        category=item.category,
                    )
            except Exception:  # noqa: BLE001
                # Best-effort sweep — don't crash the daemon on a
                # transient disk error.
                pass
    return expired_count


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------


async def run_daemon(
    config: PendingItemsConfig,
    transport_config: TransportConfig,
    vault_path: Path,
    *,
    instance_name: str,
) -> None:
    """Periodic flush loop.

    Runs forever (until SIGTERM). Each cycle:
      1. Scan ``vault/session/`` for new outbound_failures.
      2. Push every unflushed pending item to Salem.
      3. Expire items past the hard window.
      4. Regenerate the vault markdown view.

    The interval is ``config.push.interval_seconds`` — defaults to
    300s. Production deployments may shorten to 60s once Phase 2
    adds time-sensitive categories.
    """
    interval = max(60, int(config.push.interval_seconds or 300))
    log.info(
        "pending_items.pusher.daemon.starting",
        instance=instance_name,
        target_peer=config.push.target_peer,
        interval_seconds=interval,
        queue_path=config.queue_path,
        vault_path=str(vault_path),
    )

    # Run an immediate flush at startup so a daemon that just came
    # back up doesn't sit on stale items for a full interval. Mirrors
    # the brief daemon's "fire on startup if missed window" pattern.
    try:
        await flush_once(
            config, transport_config, vault_path,
            instance_name=instance_name,
        )
    except Exception:  # noqa: BLE001
        log.exception("pending_items.pusher.daemon.initial_flush_error")

    while True:
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            log.info("pending_items.pusher.daemon.cancelled")
            return

        try:
            await flush_once(
                config, transport_config, vault_path,
                instance_name=instance_name,
            )
        except Exception:  # noqa: BLE001 — daemon-level safety net
            log.exception("pending_items.pusher.daemon.flush_error")


__all__ = [
    "flush_once",
    "run_daemon",
]
