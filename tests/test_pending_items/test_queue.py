"""Tests for the pending-items JSONL queue (Phase 1)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from alfred.pending_items.queue import (
    ActionPlan,
    CATEGORY_OUTBOUND_FAILURE,
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
    new_item_id,
)


def _make_item(
    *,
    item_id: str | None = None,
    created_at: str | None = None,
    instance: str = "salem",
    status: str = STATUS_PENDING,
    pushed: bool = False,
) -> PendingItem:
    return PendingItem(
        id=item_id or new_item_id(),
        category=CATEGORY_OUTBOUND_FAILURE,
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
        created_by_instance=instance,
        session_id="d145d57c",
        context="test failure context",
        resolution_options=[
            ResolutionOption(id="noted", label="Noted, no action needed"),
            ResolutionOption(
                id="show_me",
                label="Show me what was supposed to come",
                action_plan=ActionPlan(
                    type="deliver_text",
                    params={
                        "source": "session_record",
                        "session_id": "d145d57c",
                        "turn_index": 7,
                    },
                ),
            ),
        ],
        status=status,
        pushed_to_salem=pushed,
    )


def test_append_and_read_roundtrip(tmp_path: Path) -> None:
    queue_path = tmp_path / "pending_items.jsonl"
    item = _make_item()
    assert append_item(queue_path, item) is True
    items = iter_items(queue_path)
    assert len(items) == 1
    assert items[0].id == item.id
    assert items[0].category == CATEGORY_OUTBOUND_FAILURE
    assert items[0].resolution_options[1].action_plan is not None
    assert items[0].resolution_options[1].action_plan.type == "deliver_text"
    assert items[0].resolution_options[1].action_plan.params["turn_index"] == 7


def test_list_pending_filters_status(tmp_path: Path) -> None:
    queue_path = tmp_path / "pending_items.jsonl"
    item_a = _make_item(status=STATUS_PENDING)
    item_b = _make_item(status=STATUS_RESOLVED)
    item_c = _make_item(status=STATUS_PENDING)
    for item in (item_a, item_b, item_c):
        append_item(queue_path, item)
    pending = list_pending(queue_path)
    assert len(pending) == 2
    assert {p.id for p in pending} == {item_a.id, item_c.id}


def test_find_by_id(tmp_path: Path) -> None:
    queue_path = tmp_path / "pending_items.jsonl"
    item = _make_item()
    append_item(queue_path, item)
    found = find_by_id(queue_path, item.id)
    assert found is not None
    assert found.id == item.id
    assert find_by_id(queue_path, "nonexistent") is None


def test_mark_resolved_flips_status_and_records_resolution(tmp_path: Path) -> None:
    queue_path = tmp_path / "pending_items.jsonl"
    item = _make_item()
    append_item(queue_path, item)
    ok = mark_resolved(queue_path, item.id, "show_me")
    assert ok is True
    found = find_by_id(queue_path, item.id)
    assert found is not None
    assert found.status == STATUS_RESOLVED
    assert found.resolution == "show_me"
    assert found.resolved_at is not None


def test_mark_resolved_idempotent(tmp_path: Path) -> None:
    """Re-applying the same resolution is a no-op (returns True)."""
    queue_path = tmp_path / "pending_items.jsonl"
    item = _make_item()
    append_item(queue_path, item)
    assert mark_resolved(queue_path, item.id, "noted") is True
    first_resolved_at = find_by_id(queue_path, item.id).resolved_at
    # Replay produces the same result, no rewrite spam.
    assert mark_resolved(queue_path, item.id, "noted") is True
    second_resolved_at = find_by_id(queue_path, item.id).resolved_at
    # Idempotent path — resolved_at stays unchanged.
    assert first_resolved_at == second_resolved_at


def test_mark_expired(tmp_path: Path) -> None:
    queue_path = tmp_path / "pending_items.jsonl"
    item = _make_item()
    append_item(queue_path, item)
    assert mark_expired(queue_path, item.id) is True
    found = find_by_id(queue_path, item.id)
    assert found.status == STATUS_EXPIRED


def test_mark_pushed_to_salem(tmp_path: Path) -> None:
    queue_path = tmp_path / "pending_items.jsonl"
    item = _make_item(pushed=False)
    append_item(queue_path, item)
    assert mark_pushed_to_salem(queue_path, item.id) is True
    found = find_by_id(queue_path, item.id)
    assert found.pushed_to_salem is True
    assert found.pushed_at is not None


def test_list_stale_threshold(tmp_path: Path) -> None:
    queue_path = tmp_path / "pending_items.jsonl"
    now = datetime.now(timezone.utc)
    fresh = _make_item(
        created_at=(now - timedelta(days=1)).isoformat(),
    )
    stale = _make_item(
        created_at=(now - timedelta(days=10)).isoformat(),
    )
    append_item(queue_path, fresh)
    append_item(queue_path, stale)
    flagged = list_stale(queue_path, threshold_days=7, now=now)
    assert len(flagged) == 1
    assert flagged[0].id == stale.id


def test_action_plan_roundtrip() -> None:
    plan = ActionPlan(
        type="deliver_text",
        params={"session_id": "abc", "turn_index": 5},
    )
    out = plan.to_dict()
    assert out == {
        "type": "deliver_text",
        "session_id": "abc",
        "turn_index": 5,
    }
    plan2 = ActionPlan.from_dict(out)
    assert plan2.type == "deliver_text"
    assert plan2.params["session_id"] == "abc"
    assert plan2.params["turn_index"] == 5


def test_mark_resolved_returns_false_when_id_absent(tmp_path: Path) -> None:
    queue_path = tmp_path / "pending_items.jsonl"
    append_item(queue_path, _make_item())
    assert mark_resolved(queue_path, "nonexistent", "noted") is False
