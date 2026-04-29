"""Pending Items Queue — Phase 1.

Per-instance JSONL queue for items only Andrew can answer (outbound
failures today; clarifications + fuzzy matches in Phase 2). Salem
aggregates cross-instance queues, surfaces them in the Daily Sync, and
dispatches resolutions back to the originating instance via the
``pending_items_resolve`` peer endpoint.

Architecture:

* ``data/pending_items.jsonl`` — append-only source of truth (per
  instance).
* ``vault/process/Pending Items.md`` — debounced regenerated read-only
  view for Obsidian visibility.
* ``transport.peer_handlers._handle_peer_pending_items_push`` — Salem
  inbound (peer → Salem aggregate JSONL).
* ``transport.peer_handlers._handle_peer_pending_items_resolve`` —
  Salem outbound (Salem → peer; first Salem→peer consumer on the
  substrate).
* ``daily_sync.pending_items_section`` — provider that renders the
  aggregated queue in Salem's morning prompt.

Phase 1 ships ``outbound_failure`` as the only live category. Action
plans store all shapes; only ``noop`` and ``deliver_text`` execute.
Phase 2-4 add categories + executors per
``project_pending_items_queue.md``.
"""

from .config import PendingItemsConfig, load_from_unified
from .queue import (
    ActionPlan,
    PendingItem,
    ResolutionOption,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_RESOLVED,
    append_item,
    find_by_id,
    iter_items,
    list_pending,
    list_stale,
    mark_expired,
    mark_pushed_to_salem,
    mark_resolved,
)

__all__ = [
    "ActionPlan",
    "PendingItem",
    "PendingItemsConfig",
    "ResolutionOption",
    "STATUS_EXPIRED",
    "STATUS_PENDING",
    "STATUS_RESOLVED",
    "append_item",
    "find_by_id",
    "iter_items",
    "list_pending",
    "list_stale",
    "load_from_unified",
    "mark_expired",
    "mark_pushed_to_salem",
    "mark_resolved",
]
