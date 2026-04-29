"""Tests for the Daily Sync pending-items section provider."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alfred.daily_sync.config import DailySyncConfig
from alfred.daily_sync.pending_items_section import (
    build_batch,
    clear_paths_for_tests,
    consume_last_batch,
    pending_items_section,
    render_batch,
    set_paths_for_tests,
)
from alfred.pending_items.queue import (
    ActionPlan,
    PendingItem,
    ResolutionOption,
    append_item,
    new_item_id,
)


@pytest.fixture(autouse=True)
def _reset_test_state():
    """Clear the test stash + last-batch holder between tests."""
    clear_paths_for_tests()
    consume_last_batch()
    yield
    clear_paths_for_tests()
    consume_last_batch()


def _make_item(
    *,
    instance: str = "hypatia",
    age_days: float = 1.0,
) -> PendingItem:
    when = datetime.now(timezone.utc) - timedelta(days=age_days)
    return PendingItem(
        id=new_item_id(),
        category="outbound_failure",
        created_at=when.isoformat(),
        created_by_instance=instance,
        session_id="abc",
        context=f"failure from {instance}, {age_days} days ago",
        resolution_options=[
            ResolutionOption(id="noted", label="Noted, no action needed"),
            ResolutionOption(
                id="show_me",
                label="Show me what was supposed to come",
                action_plan=ActionPlan(
                    type="deliver_text",
                    params={"session_id": "abc", "turn_index": 7},
                ),
            ),
        ],
    )


def test_build_batch_returns_empty_when_disabled(tmp_path: Path) -> None:
    """No paths registered → empty batch."""
    config = DailySyncConfig(enabled=True)
    items = build_batch(config, datetime.now().date())
    assert items == []


def test_build_batch_unions_local_and_aggregate(tmp_path: Path) -> None:
    queue_path = tmp_path / "pending_items.jsonl"
    aggregate_path = tmp_path / "pending_items_aggregate.jsonl"
    set_paths_for_tests(
        queue_path=str(queue_path),
        aggregate_path=str(aggregate_path),
        stale_threshold_days=7,
    )
    # Salem-local item.
    local_item = _make_item(instance="salem", age_days=1.0)
    append_item(queue_path, local_item)
    # Aggregate (peer-pushed) item.
    peer_item = _make_item(instance="hypatia", age_days=2.0)
    append_item(aggregate_path, peer_item)

    config = DailySyncConfig(enabled=True)
    entries = build_batch(config, datetime.now().date(), start_index=1)
    assert len(entries) == 2
    # Oldest first (peer at age 2 days, local at age 1 day).
    assert entries[0].created_by_instance == "hypatia"
    assert entries[1].created_by_instance == "salem"
    assert entries[0].item_number == 1
    assert entries[1].item_number == 2


def test_build_batch_dedupes_by_id(tmp_path: Path) -> None:
    queue_path = tmp_path / "pending_items.jsonl"
    aggregate_path = tmp_path / "pending_items_aggregate.jsonl"
    set_paths_for_tests(
        queue_path=str(queue_path),
        aggregate_path=str(aggregate_path),
    )
    item = _make_item()
    append_item(queue_path, item)
    append_item(aggregate_path, item)  # same id in both

    config = DailySyncConfig(enabled=True)
    entries = build_batch(config, datetime.now().date())
    assert len(entries) == 1


def test_build_batch_flags_stale_items(tmp_path: Path) -> None:
    queue_path = tmp_path / "pending_items.jsonl"
    set_paths_for_tests(queue_path=str(queue_path), stale_threshold_days=7)
    fresh = _make_item(age_days=1.0)
    stale = _make_item(age_days=10.0)
    append_item(queue_path, fresh)
    append_item(queue_path, stale)

    config = DailySyncConfig(enabled=True)
    entries = build_batch(config, datetime.now().date())
    by_id = {e.id: e for e in entries}
    assert by_id[fresh.id].is_stale is False
    assert by_id[stale.id].is_stale is True


def test_render_batch_empty_returns_none() -> None:
    assert render_batch([]) is None


def test_render_batch_includes_resolution_hints(tmp_path: Path) -> None:
    queue_path = tmp_path / "pending_items.jsonl"
    set_paths_for_tests(queue_path=str(queue_path))
    item = _make_item(instance="hypatia")
    append_item(queue_path, item)
    config = DailySyncConfig(enabled=True)
    entries = build_batch(config, datetime.now().date())
    rendered = render_batch(entries)
    assert rendered is not None
    assert "Pending items" in rendered
    assert "[hypatia]" in rendered
    assert "outbound_failure" in rendered
    assert "1 noted" in rendered
    assert "1 show_me" in rendered


def test_render_batch_includes_stale_tag(tmp_path: Path) -> None:
    queue_path = tmp_path / "pending_items.jsonl"
    set_paths_for_tests(queue_path=str(queue_path), stale_threshold_days=7)
    stale_item = _make_item(age_days=10.0)
    append_item(queue_path, stale_item)
    config = DailySyncConfig(enabled=True)
    entries = build_batch(config, datetime.now().date())
    rendered = render_batch(entries)
    assert rendered is not None
    assert "(stale)" in rendered


def test_section_provider_persists_batch_for_consume(tmp_path: Path) -> None:
    """The provider stashes the batch so consume_last_batch returns it."""
    queue_path = tmp_path / "pending_items.jsonl"
    set_paths_for_tests(queue_path=str(queue_path))
    item = _make_item()
    append_item(queue_path, item)
    config = DailySyncConfig(enabled=True)
    rendered = pending_items_section(config, datetime.now().date(), start_index=1)
    assert rendered is not None
    batch = consume_last_batch()
    assert len(batch) == 1
    assert batch[0].id == item.id
    # Subsequent consume returns empty (batch was consumed).
    assert consume_last_batch() == []
