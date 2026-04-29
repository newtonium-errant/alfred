"""JSONL-backed Pending Items queue (Phase 1).

Single-file source of truth. Append-only writes; reads filter by
status. State transitions (``pending`` → ``resolved`` / ``expired``)
rewrite the file in place via a temp + rename to keep the audit trail
intact while preserving line ordering.

Storage shape (one JSONL row per item)::

    {
      "id": "<uuid4>",
      "category": "outbound_failure | unanswered_clarification | "
                  "fuzzy_match | inference_review",
      "created_at": "<iso8601 UTC>",
      "created_by_instance": "salem | hypatia | kalle",
      "session_id": "<originating session id, may be null>",
      "context": "<≤500 char prose for Daily Sync display>",
      "resolution_options": [
        {
          "id": "<stable string>",
          "label": "<what Andrew sees>",
          "action_plan": <object | null>
        },
        ...
      ],
      "status": "pending | resolved | expired",
      "resolved_at": "<iso8601 or null>",
      "resolution": "<resolution_option_id or null>",
      "pushed_to_salem": <bool>,    # Phase 1: only set on non-Salem instances
      "pushed_at": "<iso8601 or null>"
    }

The ``id``, ``category``, ``created_by_instance``, ``session_id``, and
``created_at`` fields are immutable once written. Mutation happens to
``status``, ``resolved_at``, ``resolution``, ``pushed_to_salem``, and
``pushed_at`` only.

Designed after :mod:`alfred.transport.canonical_proposals` —
deliberately the same shape so the Daily Sync section renderer +
reply dispatcher patterns transfer cleanly.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Status values. Phase 1 uses pending + resolved + expired; Phase 2
# may add a "stale" intermediate state, but for now stale is computed
# at read time from ``created_at`` so the JSONL stays simple.
STATUS_PENDING = "pending"
STATUS_RESOLVED = "resolved"
STATUS_EXPIRED = "expired"

_VALID_STATUSES = {STATUS_PENDING, STATUS_RESOLVED, STATUS_EXPIRED}


# Phase 1 categories. Phase 2 adds ``unanswered_clarification`` +
# ``fuzzy_match``; Phase 4 adds ``inference_review``. We don't gate on
# the value here — the queue stores arbitrary strings — but downstream
# renderers / executors only know about these.
CATEGORY_OUTBOUND_FAILURE = "outbound_failure"
CATEGORY_UNANSWERED_CLARIFICATION = "unanswered_clarification"
CATEGORY_FUZZY_MATCH = "fuzzy_match"
CATEGORY_INFERENCE_REVIEW = "inference_review"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ActionPlan:
    """Structured executor instruction for a resolution option.

    Phase 1 implements ``noop`` (no-op, e.g. "Noted") and
    ``deliver_text`` (re-send the failed outbound text via the
    transport). Phase 3 will add ``merge_records``,
    ``rewrite_wikilinks``, ``delete_record``, ``edit_frontmatter``.

    The ``type`` field is the dispatch key. Type-specific fields live
    flat on the dataclass via the ``params`` dict so Phase 3 doesn't
    need a schema change.
    """

    type: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        # Inline params at top level so the on-disk shape matches the
        # spec ("type": "deliver_text", "source": "session_record",
        # ...). Reserved keys can't be overridden.
        for k, v in self.params.items():
            if k == "type":
                continue
            out[k] = v
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionPlan":
        type_ = str(data.get("type") or "noop")
        params = {k: v for k, v in data.items() if k != "type"}
        return cls(type=type_, params=params)


@dataclass
class ResolutionOption:
    """One resolution choice attached to a pending item.

    ``action_plan`` may be ``None`` for noted-only options (no executor
    runs; the resolver just flips the status). The Phase 1 spec keeps
    the structured ``deliver_text`` plan as the default for the
    ``show_me`` resolution on outbound_failure items.
    """

    id: str
    label: str
    action_plan: ActionPlan | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "action_plan": self.action_plan.to_dict() if self.action_plan else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResolutionOption":
        plan_raw = data.get("action_plan")
        plan = ActionPlan.from_dict(plan_raw) if isinstance(plan_raw, dict) else None
        return cls(
            id=str(data.get("id") or ""),
            label=str(data.get("label") or ""),
            action_plan=plan,
        )


@dataclass
class PendingItem:
    """One row in the pending-items queue.

    ``pushed_to_salem`` is the cross-instance push state — irrelevant
    on Salem itself (the aggregator), present-and-True on peer
    instances after a successful push to Salem. Items with
    ``pushed_to_salem: False`` retry on every periodic flush so a
    Salem-down window doesn't silently drop items.
    """

    id: str
    category: str
    created_at: str
    created_by_instance: str
    session_id: str | None = None
    context: str = ""
    resolution_options: list[ResolutionOption] = field(default_factory=list)
    status: str = STATUS_PENDING
    resolved_at: str | None = None
    resolution: str | None = None
    pushed_to_salem: bool = False
    pushed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "created_at": self.created_at,
            "created_by_instance": self.created_by_instance,
            "session_id": self.session_id,
            "context": self.context,
            "resolution_options": [o.to_dict() for o in self.resolution_options],
            "status": self.status,
            "resolved_at": self.resolved_at,
            "resolution": self.resolution,
            "pushed_to_salem": self.pushed_to_salem,
            "pushed_at": self.pushed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingItem":
        options_raw = data.get("resolution_options") or []
        options: list[ResolutionOption] = []
        if isinstance(options_raw, list):
            for o in options_raw:
                if isinstance(o, dict):
                    options.append(ResolutionOption.from_dict(o))
        return cls(
            id=str(data.get("id") or ""),
            category=str(data.get("category") or ""),
            created_at=str(data.get("created_at") or ""),
            created_by_instance=str(data.get("created_by_instance") or ""),
            session_id=(
                str(data["session_id"]) if data.get("session_id") else None
            ),
            context=str(data.get("context") or ""),
            resolution_options=options,
            status=str(data.get("status") or STATUS_PENDING),
            resolved_at=(
                str(data["resolved_at"]) if data.get("resolved_at") else None
            ),
            resolution=(
                str(data["resolution"]) if data.get("resolution") else None
            ),
            pushed_to_salem=bool(data.get("pushed_to_salem", False)),
            pushed_at=(
                str(data["pushed_at"]) if data.get("pushed_at") else None
            ),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Wall-clock UTC ISO-8601. Wrapped so tests can monkeypatch."""
    return datetime.now(timezone.utc).isoformat()


def new_item_id() -> str:
    """Mint a fresh UUID4-based id for a new queue item."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Append + read
# ---------------------------------------------------------------------------


def append_item(queue_path: str | Path, item: PendingItem) -> bool:
    """Append one item row to the JSONL queue. Returns True on success.

    Creates the parent directory when missing. Returns False rather
    than raising on disk errors so a failure to durably store an item
    can't crash the call site (e.g. a session-close hook); the call
    site logs and continues.
    """
    if not queue_path:
        return False
    path = Path(queue_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    line = json.dumps(item.to_dict(), default=str) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        return True
    except OSError:
        return False


def iter_items(queue_path: str | Path) -> list[PendingItem]:
    """Read every row (any status) from the queue.

    Returns ``[]`` when the file doesn't exist. Malformed rows are
    skipped silently — a malformed line shouldn't take the whole
    queue down.
    """
    path = Path(queue_path)
    if not path.exists():
        return []
    out: list[PendingItem] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            out.append(PendingItem.from_dict(data))
    return out


def list_pending(queue_path: str | Path) -> list[PendingItem]:
    """Return items whose status is ``pending``, oldest-first.

    Oldest-first preserves chronological order so a stale pending item
    leads today's batch in the Daily Sync.
    """
    return [i for i in iter_items(queue_path) if i.status == STATUS_PENDING]


def list_stale(
    queue_path: str | Path,
    *,
    threshold_days: int,
    now: datetime | None = None,
) -> list[PendingItem]:
    """Return pending items older than ``threshold_days``.

    Used by the Daily Sync renderer to flag stale items + by the
    expiry sweep to pick auto-expire candidates. ``now`` is settable
    for tests; defaults to ``datetime.now(timezone.utc)``.
    """
    if threshold_days <= 0:
        return []
    cutoff = (now or datetime.now(timezone.utc))
    out: list[PendingItem] = []
    for item in list_pending(queue_path):
        try:
            created = datetime.fromisoformat(item.created_at.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        age = (cutoff - created).total_seconds() / 86400.0
        if age >= threshold_days:
            out.append(item)
    return out


def find_by_id(
    queue_path: str | Path, item_id: str,
) -> PendingItem | None:
    """Return the item with ``id == item_id`` or ``None``."""
    if not item_id:
        return None
    for item in iter_items(queue_path):
        if item.id == item_id:
            return item
    return None


# ---------------------------------------------------------------------------
# State transitions (rewrite-in-place)
# ---------------------------------------------------------------------------


def _rewrite_in_place(
    queue_path: str | Path,
    items: list[PendingItem],
) -> bool:
    """Atomic rewrite of the full file (temp + rename). Returns True on success.

    Order-preserving — the Daily Sync section renderer's stable item
    ordering depends on it. Errors propagate to the caller — unlike
    :func:`append_item`, losing a state transition would re-surface a
    resolved item, which is observable confusion.
    """
    path = Path(queue_path)
    if not path.exists():
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item.to_dict(), default=str) + "\n")
        os.replace(tmp, path)
        return True
    except OSError:
        # Best-effort cleanup — failing to remove the partial temp is
        # not a hard error.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def mark_resolved(
    queue_path: str | Path,
    item_id: str,
    resolution_id: str,
    *,
    resolved_at: str | None = None,
) -> bool:
    """Flip one item's status to ``resolved`` in place.

    Returns False on any of:

      * ``item_id`` not found
      * file doesn't exist
      * already resolved with a different resolution (idempotent
        no-op when the same resolution is re-applied; True returned
        so the dispatcher's idempotency replay is silent)
    """
    if not item_id:
        return False
    items = iter_items(queue_path)
    if not items:
        return False
    found = False
    for item in items:
        if item.id != item_id:
            continue
        # Idempotent: re-applying the same resolution is a no-op.
        if item.status == STATUS_RESOLVED and item.resolution == resolution_id:
            return True
        item.status = STATUS_RESOLVED
        item.resolution = resolution_id
        item.resolved_at = resolved_at or _now_iso()
        found = True
    if not found:
        return False
    return _rewrite_in_place(queue_path, items)


def mark_expired(
    queue_path: str | Path,
    item_id: str,
    *,
    expired_at: str | None = None,
) -> bool:
    """Flip one item's status to ``expired`` in place.

    Used by the auto-expire sweep when an item exceeds the configured
    expiry days. Returns False when the item isn't found.
    """
    if not item_id:
        return False
    items = iter_items(queue_path)
    if not items:
        return False
    found = False
    for item in items:
        if item.id != item_id:
            continue
        if item.status == STATUS_EXPIRED:
            return True  # idempotent
        item.status = STATUS_EXPIRED
        item.resolved_at = expired_at or _now_iso()
        found = True
    if not found:
        return False
    return _rewrite_in_place(queue_path, items)


def mark_pushed_to_salem(
    queue_path: str | Path,
    item_id: str,
    *,
    pushed_at: str | None = None,
) -> bool:
    """Flip ``pushed_to_salem=True`` for one item.

    Called after a successful peer-push so the next periodic flush
    skips this item. Salem-down handling: items remain at False and
    retry every flush until the push succeeds.
    """
    if not item_id:
        return False
    items = iter_items(queue_path)
    if not items:
        return False
    found = False
    for item in items:
        if item.id != item_id:
            continue
        if item.pushed_to_salem:
            return True  # idempotent
        item.pushed_to_salem = True
        item.pushed_at = pushed_at or _now_iso()
        found = True
    if not found:
        return False
    return _rewrite_in_place(queue_path, items)


__all__ = [
    "ActionPlan",
    "CATEGORY_FUZZY_MATCH",
    "CATEGORY_INFERENCE_REVIEW",
    "CATEGORY_OUTBOUND_FAILURE",
    "CATEGORY_UNANSWERED_CLARIFICATION",
    "PendingItem",
    "ResolutionOption",
    "STATUS_EXPIRED",
    "STATUS_PENDING",
    "STATUS_RESOLVED",
    "append_item",
    "find_by_id",
    "iter_items",
    "list_pending",
    "list_stale",
    "mark_expired",
    "mark_pushed_to_salem",
    "mark_resolved",
    "new_item_id",
]
