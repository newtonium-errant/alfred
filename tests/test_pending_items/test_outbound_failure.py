"""Tests for the outbound-failure session scanner.

Uses the canonical Hypatia 2026-04-28 fixture from the spec
(session d145d57c, turn 7, length 4852, error 'Message is too long')
to exercise the full path: read session frontmatter → emit queue
row → idempotent re-scan.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest
import yaml

from alfred.pending_items.outbound_failure import scan_and_emit
from alfred.pending_items.queue import (
    CATEGORY_OUTBOUND_FAILURE,
    STATUS_PENDING,
    iter_items,
)


def _write_session_record(
    vault: Path,
    *,
    session_id: str,
    name: str,
    outbound_failures: list[dict] | None,
) -> Path:
    """Write a session record with optional outbound_failures."""
    fm = {
        "type": "session",
        "name": name,
        "telegram": {
            "session_id": session_id,
            "chat_id": 12345,
        },
    }
    if outbound_failures is not None:
        fm["outbound_failures"] = outbound_failures
    body = "# Transcript\n\n**Andrew** (16:00): hi\n\n**Alfred** (16:00): hello"
    record = frontmatter.Post(body, **fm)
    path = vault / "session" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(record), encoding="utf-8")
    return path


def test_scan_emits_queue_row_for_canonical_fixture(tmp_path: Path) -> None:
    """The Hypatia 2026-04-28 fixture lands as one queue row."""
    vault = tmp_path / "vault"
    queue_path = tmp_path / "pending_items.jsonl"
    state_path = tmp_path / "state.json"
    _write_session_record(
        vault,
        session_id="d145d57c-canonical-fixture",
        name="Conversation - 2026-04-28 i-want-to-talk",
        outbound_failures=[
            {
                "turn_index": 7,
                "timestamp": "2026-04-28T16:00:57+00:00",
                "error": "Message is too long",
                "length": 4852,
                "chunks_attempted": 1,
                "chunks_sent": 0,
                "delivered": False,
            }
        ],
    )
    summary = scan_and_emit(
        vault_path=vault,
        queue_path=queue_path,
        state_path=state_path,
        instance_name="hypatia",
    )
    assert summary["scanned_records"] == 1
    assert summary["emitted"] == 1
    assert summary["errors"] == []
    items = iter_items(queue_path)
    assert len(items) == 1
    item = items[0]
    assert item.category == CATEGORY_OUTBOUND_FAILURE
    assert item.status == STATUS_PENDING
    assert item.created_by_instance == "hypatia"
    assert item.session_id == "d145d57c-canonical-fixture"
    assert "4852 chars" in item.context
    assert "Message is too long" in item.context
    # Resolution options: noted (no plan) + show_me (deliver_text plan).
    option_ids = [o.id for o in item.resolution_options]
    assert option_ids == ["noted", "show_me"]
    assert item.resolution_options[0].action_plan is None
    show_plan = item.resolution_options[1].action_plan
    assert show_plan is not None
    assert show_plan.type == "deliver_text"
    assert show_plan.params["session_id"] == "d145d57c-canonical-fixture"
    assert show_plan.params["turn_index"] == 7


def test_scan_idempotent(tmp_path: Path) -> None:
    """Re-running the scan over the same vault state emits zero new rows."""
    vault = tmp_path / "vault"
    queue_path = tmp_path / "pending_items.jsonl"
    state_path = tmp_path / "state.json"
    _write_session_record(
        vault,
        session_id="abc123",
        name="Conversation - 2026-04-28 example",
        outbound_failures=[
            {
                "turn_index": 3,
                "timestamp": "2026-04-28T12:00:00+00:00",
                "error": "Network",
                "length": 100,
                "chunks_attempted": 1,
                "chunks_sent": 0,
                "delivered": False,
            }
        ],
    )
    s1 = scan_and_emit(
        vault_path=vault, queue_path=queue_path,
        state_path=state_path, instance_name="hypatia",
    )
    assert s1["emitted"] == 1
    s2 = scan_and_emit(
        vault_path=vault, queue_path=queue_path,
        state_path=state_path, instance_name="hypatia",
    )
    assert s2["emitted"] == 0
    assert s2["skipped_already_emitted"] == 1
    assert len(iter_items(queue_path)) == 1


def test_scan_skips_records_without_outbound_failures(tmp_path: Path) -> None:
    """Healthy sessions with no failures don't emit anything."""
    vault = tmp_path / "vault"
    queue_path = tmp_path / "pending_items.jsonl"
    state_path = tmp_path / "state.json"
    _write_session_record(
        vault,
        session_id="healthy",
        name="Conversation - healthy",
        outbound_failures=None,
    )
    summary = scan_and_emit(
        vault_path=vault, queue_path=queue_path,
        state_path=state_path, instance_name="hypatia",
    )
    assert summary["scanned_records"] == 1
    assert summary["emitted"] == 0
    assert iter_items(queue_path) == []


def test_scan_emits_one_row_per_failure(tmp_path: Path) -> None:
    """A session with multiple failures emits one row per failure."""
    vault = tmp_path / "vault"
    queue_path = tmp_path / "pending_items.jsonl"
    state_path = tmp_path / "state.json"
    _write_session_record(
        vault,
        session_id="multi",
        name="Conversation - multi-failure",
        outbound_failures=[
            {
                "turn_index": 3,
                "timestamp": "2026-04-28T12:00:00+00:00",
                "error": "First",
                "length": 100,
                "chunks_attempted": 1,
                "chunks_sent": 0,
                "delivered": False,
            },
            {
                "turn_index": 5,
                "timestamp": "2026-04-28T12:01:00+00:00",
                "error": "Second",
                "length": 200,
                "chunks_attempted": 1,
                "chunks_sent": 0,
                "delivered": False,
            },
        ],
    )
    summary = scan_and_emit(
        vault_path=vault, queue_path=queue_path,
        state_path=state_path, instance_name="hypatia",
    )
    assert summary["emitted"] == 2
    items = iter_items(queue_path)
    assert len(items) == 2
    # Different turn_index → different items.
    plans = [i.resolution_options[1].action_plan for i in items]
    turn_indexes = sorted(p.params["turn_index"] for p in plans)
    assert turn_indexes == [3, 5]
