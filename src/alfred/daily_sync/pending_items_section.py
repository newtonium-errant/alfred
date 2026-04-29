"""Pending Items section provider for the Daily Sync.

Renders the cross-instance pending-items aggregate (Salem's own
queue + every peer's pushed-to-Salem queue) as a numbered section
in the morning prompt.

Priority: 5 (renders BEFORE email at 10) — items in this queue are
"only Andrew can answer" by definition; we want them to lead the
morning conversation.

Read order:
  1. Salem's local queue file (``pending_items.queue_path``).
  2. The cross-instance aggregate file (``pending_items_aggregate.jsonl``,
     populated by ``/peer/pending_items_push``).
  3. Union by ``item.id`` (so a Salem-originated item that's ALSO in
     the aggregate doesn't double-render).
  4. Filter to ``status=pending``, sort by ``created_at`` oldest-first,
     group by category.

The Daily Sync renderer is read-only over the queue file — never
mutates. Resolutions arrive via the smart-routing reply parser,
which routes ``noted`` / ``show_me`` verbs through the dispatcher
into the executor (Salem's local items) or the
``pending_items_resolve`` peer call (peer-originated items).

Design ratified in ``project_pending_items_queue.md`` (2026-04-28).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from alfred.pending_items.queue import PendingItem, iter_items, list_pending

from .config import DailySyncConfig

log = structlog.get_logger(__name__)


@dataclass
class PendingItemEntry:
    """One pending-items item rendered in the Daily Sync.

    Persisted into the state file's ``last_batch.pending_items`` list
    so the reply dispatcher can resolve "item N" → queue item id +
    originating instance without re-reading every queue file.
    """

    item_number: int  # 1-indexed, GLOBAL across Daily Sync sections
    id: str
    category: str
    created_by_instance: str
    created_at: str
    session_id: str | None
    context: str
    is_stale: bool
    resolution_options: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_number": self.item_number,
            "id": self.id,
            "category": self.category,
            "created_by_instance": self.created_by_instance,
            "created_at": self.created_at,
            "session_id": self.session_id,
            "context": self.context,
            "is_stale": self.is_stale,
            "resolution_options": list(self.resolution_options or []),
        }


def _resolve_paths(
    config: DailySyncConfig,
) -> tuple[str | None, str | None, int]:
    """Return ``(queue_path, aggregate_path, stale_threshold_days)``.

    Reads from the Pending Items config block. Returns ``(None, None,
    0)`` when the block is absent or disabled — the section silently
    renders nothing in that case.
    """
    try:
        from alfred.pending_items.config import (
            load_from_unified as load_pending,
        )
    except ImportError:
        return None, None, 0
    try:
        # Daily Sync gets the raw config dict from a side channel —
        # the assembler doesn't pass it through. Re-read here from
        # the same config.yaml the rest of the system loads.
        import yaml
        # ``config.yaml`` is the production path; daily_sync.config
        # already implicitly relies on this convention via its own
        # ``load_config``. Tests can monkeypatch via the stash hook
        # below.
        if _TEST_STASH.get("queue_path"):
            return (
                _TEST_STASH.get("queue_path"),
                _TEST_STASH.get("aggregate_path"),
                int(_TEST_STASH.get("stale_threshold_days", 7)),
            )
        config_path = _TEST_STASH.get("config_path", "config.yaml")
        with open(str(config_path), "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        pi_config = load_pending(raw)
        if not pi_config.enabled:
            return None, None, 0
        # Aggregate path is queue_path's sibling, named
        # ``pending_items_aggregate.jsonl``. Mirror of the talker
        # daemon's startup wiring.
        from pathlib import Path as _P
        aggregate_path = str(
            _P(pi_config.queue_path).with_name(
                "pending_items_aggregate.jsonl"
            )
        )
        return (
            pi_config.queue_path,
            aggregate_path,
            int(pi_config.expiry.stale_days or 7),
        )
    except Exception as exc:  # noqa: BLE001
        log.info(
            "daily_sync.pending_items.config_unavailable",
            error=str(exc),
        )
        return None, None, 0


# Test hook — set via :func:`set_paths_for_tests` so tests don't
# need a real config.yaml on disk.
_TEST_STASH: dict[str, Any] = {}


def set_paths_for_tests(
    *,
    queue_path: str | None = None,
    aggregate_path: str | None = None,
    stale_threshold_days: int | None = None,
    config_path: str | None = None,
) -> None:
    """Stash explicit paths for test runs. Production callers don't use this."""
    if queue_path is not None:
        _TEST_STASH["queue_path"] = queue_path
    if aggregate_path is not None:
        _TEST_STASH["aggregate_path"] = aggregate_path
    if stale_threshold_days is not None:
        _TEST_STASH["stale_threshold_days"] = stale_threshold_days
    if config_path is not None:
        _TEST_STASH["config_path"] = config_path


def clear_paths_for_tests() -> None:
    """Clear the stash. Test cleanup helper."""
    _TEST_STASH.clear()


def _pending_from_aggregate(aggregate_path: str | None) -> list[PendingItem]:
    """Read pending items from the aggregate file (peer-pushed).

    Status filter happens here so an item Salem already resolved
    (round-tripped via ``pending_items_resolve``) doesn't re-surface.
    Note: the aggregate file is append-only on Salem's side — the
    state transition lives on the originating peer's queue. So
    Salem's aggregate may contain stale-looking ``status=pending``
    rows for items that are actually resolved on the peer side.
    Phase 1 accepts this; Phase 2 may add a Salem-side aggregate
    state-transition path. For now, the smart-route dispatcher de-
    duplicates by re-checking status server-side via the resolve
    call.
    """
    if not aggregate_path:
        return []
    items = iter_items(aggregate_path)
    return [i for i in items if i.status == "pending"]


def _is_stale(
    item: PendingItem,
    *,
    stale_threshold_days: int,
    now: datetime,
) -> bool:
    """Return True when the item was created more than threshold days ago."""
    if stale_threshold_days <= 0:
        return False
    try:
        created = datetime.fromisoformat(
            item.created_at.replace("Z", "+00:00")
        )
    except (ValueError, TypeError):
        return False
    age_days = (now - created).total_seconds() / 86400.0
    return age_days >= stale_threshold_days


def build_batch(
    config: DailySyncConfig,
    today: date,
    *,
    start_index: int = 1,
) -> list[PendingItemEntry]:
    """Sample pending items from local + aggregate queues.

    Returns ``[]`` when nothing is pending (the steady state). Items
    are ordered: local queue first, then aggregate, then the union
    is filtered to status=pending and sorted by created_at oldest-
    first. Duplicates (same id in both files) are deduped; the local
    copy wins.

    ``start_index`` (1-based, GLOBAL across Daily Sync sections) lets
    the assembler keep numbering continuous across sections.
    """
    queue_path, aggregate_path, stale_days = _resolve_paths(config)
    if queue_path is None:
        return []

    seen_ids: set[str] = set()
    union: list[PendingItem] = []

    try:
        for item in list_pending(queue_path):
            if item.id and item.id not in seen_ids:
                seen_ids.add(item.id)
                union.append(item)
    except Exception as exc:  # noqa: BLE001
        log.info("daily_sync.pending_items.local_read_failed", error=str(exc))

    if aggregate_path:
        try:
            for item in _pending_from_aggregate(aggregate_path):
                if item.id and item.id not in seen_ids:
                    seen_ids.add(item.id)
                    union.append(item)
        except Exception as exc:  # noqa: BLE001
            log.info(
                "daily_sync.pending_items.aggregate_read_failed",
                error=str(exc),
            )

    if not union:
        return []

    # Oldest-first.
    def _sort_key(it: PendingItem) -> str:
        return it.created_at or ""
    union.sort(key=_sort_key)

    now = datetime.now(timezone.utc)
    entries: list[PendingItemEntry] = []
    for i, item in enumerate(union):
        entries.append(PendingItemEntry(
            item_number=start_index + i,
            id=item.id,
            category=item.category,
            created_by_instance=item.created_by_instance,
            created_at=item.created_at,
            session_id=item.session_id,
            context=item.context,
            is_stale=_is_stale(item, stale_threshold_days=stale_days, now=now),
            resolution_options=[
                {
                    "id": opt.id,
                    "label": opt.label,
                    "has_action": opt.action_plan is not None,
                }
                for opt in item.resolution_options
            ],
        ))
    return entries


def render_batch(items: list[PendingItemEntry]) -> str | None:
    """Render the pending-items section as a Daily Sync block.

    Returns ``None`` when the batch is empty so the Daily Sync stays
    concise on the common case (no pending items).

    Format (group-by-category, with stale flag inline)::

        ## Pending items (3)

        1. [hypatia] outbound_failure (stale)
            on 2026-04-28 at 16:00 UTC, an outbound reply (4852 chars)
            failed to deliver via Telegram. Error: Message is too long.
            Full text in session/d145d57c.
            Reply: `1 noted` (no action) or `1 show me` (deliver text)

        2. [salem] outbound_failure
            ...

        Reply with `N noted` or `N show me` to resolve. Resolutions
        route to the originating instance.
    """
    if not items:
        return None
    plural = "s" if len(items) != 1 else ""
    lines = [f"## Pending items ({len(items)} item{plural})", ""]

    by_category: dict[str, list[PendingItemEntry]] = {}
    for item in items:
        by_category.setdefault(item.category, []).append(item)

    for category in sorted(by_category.keys()):
        bucket = by_category[category]
        for item in bucket:
            instance_label = (item.created_by_instance or "?").lower()
            stale_tag = " (stale)" if item.is_stale else ""
            header = (
                f"{item.item_number}. [{instance_label}] "
                f"{item.category}{stale_tag}"
            )
            lines.append(header)
            # Indent the context block — Telegram renders this as a
            # readable wrapped paragraph.
            context_clean = (item.context or "").strip()
            if context_clean:
                # 1-level indent so it visually attaches to the header.
                for ctx_line in context_clean.splitlines():
                    lines.append(f"    {ctx_line.rstrip()}")
            # One-line resolution hint — only the labels, not the
            # full action_plan shape.
            if item.resolution_options:
                hints: list[str] = []
                for opt in item.resolution_options:
                    opt_id = opt.get("id", "")
                    if opt_id:
                        hints.append(f"`{item.item_number} {opt_id}`")
                if hints:
                    lines.append(f"    Reply: {' or '.join(hints)}")
            lines.append("")

    lines.append(
        "Reply `N noted` to mark resolved without action, or "
        "`N show me` to trigger the action plan. Resolutions route "
        "to the originating instance."
    )
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Section provider entry point + registration
# ---------------------------------------------------------------------------


# Module-level holder so the daemon can read the batch back after
# the assembler runs. Mirrors the canonical-proposals pattern.
_LAST_BATCH_HOLDER: dict[str, list[PendingItemEntry]] = {"items": []}


def consume_last_batch() -> list[PendingItemEntry]:
    """Return and clear the most recently-built batch."""
    items = _LAST_BATCH_HOLDER.get("items", [])
    _LAST_BATCH_HOLDER["items"] = []
    return items


def peek_last_batch_count() -> int:
    """Non-destructive count for the assembler's ``item_count_after`` hook."""
    return len(_LAST_BATCH_HOLDER.get("items", []))


def pending_items_section(
    config: DailySyncConfig,
    today: date,
    *,
    start_index: int = 1,
) -> str | None:
    """Section provider — builds and renders the pending-items batch.

    Registered with priority 5 (renders BEFORE email at 10). Returns
    ``None`` when nothing is pending so the Daily Sync stays concise.
    """
    items = build_batch(config, today, start_index=start_index)
    _LAST_BATCH_HOLDER["items"] = items
    return render_batch(items)


def register() -> None:
    """Idempotent provider registration. Safe to call multiple times."""
    from . import assembler
    if "pending_items" in assembler.registered_providers():
        return
    assembler.register_provider(
        "pending_items",
        priority=5,
        provider=pending_items_section,
        item_count_after=peek_last_batch_count,
    )


__all__ = [
    "PendingItemEntry",
    "build_batch",
    "clear_paths_for_tests",
    "consume_last_batch",
    "peek_last_batch_count",
    "pending_items_section",
    "register",
    "render_batch",
    "set_paths_for_tests",
]
