"""Tests for the action plan executor (Phase 1).

Covers:
* ``noop`` action plan flips status to resolved.
* ``deliver_text`` action plan stub (transport mocked) routes the
  text to the outbound batch sender.
* Phase 3 action types return ``executed=False`` with the
  ``phase_3_not_yet_implemented`` sentinel.
* Atomicity: failed action plan leaves item pending.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import frontmatter
import pytest

from alfred.pending_items.executor import (
    _extract_assistant_turn_text,
    execute_action_plan,
    resolve_local_item,
)
from alfred.pending_items.queue import (
    ActionPlan,
    PendingItem,
    ResolutionOption,
    STATUS_PENDING,
    STATUS_RESOLVED,
    append_item,
    find_by_id,
    new_item_id,
)


def _make_item_noted_only(item_id: str = "") -> PendingItem:
    return PendingItem(
        id=item_id or new_item_id(),
        category="outbound_failure",
        created_at="2026-04-28T16:00:00+00:00",
        created_by_instance="hypatia",
        session_id="abc",
        context="test",
        resolution_options=[
            ResolutionOption(id="noted", label="Noted, no action", action_plan=None),
        ],
    )


def _make_item_with_deliver_text(turn_index: int = 7) -> PendingItem:
    return PendingItem(
        id=new_item_id(),
        category="outbound_failure",
        created_at="2026-04-28T16:00:00+00:00",
        created_by_instance="hypatia",
        session_id="d145d57c",
        context="test",
        resolution_options=[
            ResolutionOption(id="noted", label="Noted", action_plan=None),
            ResolutionOption(
                id="show_me",
                label="Show me",
                action_plan=ActionPlan(
                    type="deliver_text",
                    params={
                        "source": "session_record",
                        "session_id": "d145d57c",
                        "turn_index": turn_index,
                    },
                ),
            ),
        ],
    )


@pytest.mark.asyncio
async def test_execute_noop_plan() -> None:
    plan = ActionPlan(type="noop")
    result = await execute_action_plan(
        plan=plan, vault_path=Path("/tmp/nonexistent"), user_id=12345,
    )
    assert result["executed"] is True
    assert result["error"] is None


@pytest.mark.asyncio
async def test_execute_unsupported_plan_returns_phase_3_sentinel() -> None:
    plan = ActionPlan(type="merge_records")
    result = await execute_action_plan(
        plan=plan, vault_path=Path("/tmp/nonexistent"), user_id=12345,
    )
    assert result["executed"] is False
    assert result["error"] == "phase_3_not_yet_implemented"


@pytest.mark.asyncio
async def test_resolve_local_item_noted(tmp_path: Path) -> None:
    queue_path = tmp_path / "pending_items.jsonl"
    item = _make_item_noted_only()
    append_item(queue_path, item)

    result = await resolve_local_item(
        queue_path=queue_path,
        item_id=item.id,
        resolution_id="noted",
        vault_path=tmp_path,
        user_id=12345,
    )
    assert result["ok"] is True
    assert result["executed"] is True
    found = find_by_id(queue_path, item.id)
    assert found.status == STATUS_RESOLVED


@pytest.mark.asyncio
async def test_resolve_local_item_unknown_id(tmp_path: Path) -> None:
    queue_path = tmp_path / "pending_items.jsonl"
    item = _make_item_noted_only()
    append_item(queue_path, item)

    result = await resolve_local_item(
        queue_path=queue_path,
        item_id="never-existed",
        resolution_id="noted",
        vault_path=tmp_path,
        user_id=12345,
    )
    assert result["ok"] is False
    assert result["error"] == "item_not_found"


@pytest.mark.asyncio
async def test_resolve_local_item_unknown_resolution(tmp_path: Path) -> None:
    queue_path = tmp_path / "pending_items.jsonl"
    item = _make_item_noted_only()
    append_item(queue_path, item)

    result = await resolve_local_item(
        queue_path=queue_path,
        item_id=item.id,
        resolution_id="show_me",  # not in options for this item
        vault_path=tmp_path,
        user_id=12345,
    )
    assert result["ok"] is False
    assert result["error"] == "resolution_not_found"
    # Item stays pending (atomicity contract).
    found = find_by_id(queue_path, item.id)
    assert found.status == STATUS_PENDING


@pytest.mark.asyncio
async def test_resolve_local_item_deliver_text_calls_transport(
    tmp_path: Path,
) -> None:
    """Stub the transport client + verify deliver_text dispatches correctly."""
    queue_path = tmp_path / "pending_items.jsonl"
    item = _make_item_with_deliver_text(turn_index=1)
    append_item(queue_path, item)

    # Build a session record matching the ids in the action plan.
    vault = tmp_path / "vault"
    sess_dir = vault / "session"
    sess_dir.mkdir(parents=True)
    fm = {
        "type": "session",
        "name": "Conversation - 2026-04-28 fixture",
        "telegram": {
            "session_id": "d145d57c",
            "chat_id": 12345,
        },
    }
    body = (
        "# Transcript\n\n"
        "**Andrew** (16:00): hi\n\n"
        "**Alfred** (16:00): this is the failed assistant turn"
    )
    (sess_dir / "session1.md").write_text(
        frontmatter.dumps(frontmatter.Post(body, **fm)), encoding="utf-8",
    )

    sent_calls = []

    async def _stub_send(user_id, chunks, dedupe_key=None, client_name=None):
        sent_calls.append({
            "user_id": user_id,
            "chunks": list(chunks),
            "dedupe_key": dedupe_key,
            "client_name": client_name,
        })
        return {"id": "stub", "status": "queued"}

    with patch(
        "alfred.transport.client.send_outbound_batch",
        side_effect=_stub_send,
    ):
        result = await resolve_local_item(
            queue_path=queue_path,
            item_id=item.id,
            resolution_id="show_me",
            vault_path=vault,
            user_id=12345,
        )

    assert result["ok"] is True, result
    assert result["executed"] is True
    assert len(sent_calls) == 1
    assert sent_calls[0]["user_id"] == 12345
    assert "this is the failed assistant turn" in "".join(sent_calls[0]["chunks"])
    found = find_by_id(queue_path, item.id)
    assert found.status == STATUS_RESOLVED


def test_extract_assistant_turn_counts_tool_transcript_lines(
    tmp_path: Path,
) -> None:
    """Regression pin: tool_result transcript entries must not shift indices.

    Renderer↔executor contract, pinned end-to-end: the session body is
    built by the REAL ``_build_session_body`` (alfred.telegram.session),
    not a hand-written string. Since 71998aa a user-role transcript
    entry containing only ``tool_result`` blocks renders as a
    ``**Tool** (HH:MM): …`` line instead of an ``**Andrew**`` line. The
    executor's line-walk maps the global transcript index to a rendered
    line by counting speaker headers — if it stops counting ``**Tool**``
    lines (or the renderer's Tool-line shape changes so the walk no
    longer recognizes it), every assistant turn after a tool invocation
    reconstructs the wrong turn (or fails to reconstruct at all). This
    test fails in either regression direction.
    """
    from alfred.telegram.session import Session, _build_session_body

    now = datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc)
    sess = Session(
        session_id="d145d57c",
        chat_id=12345,
        started_at=now,
        last_message_at=now,
        model="claude-sonnet-4-6",
    )
    sess.transcript = [
        {
            "role": "user",
            "content": "hi",
            "_ts": "2026-06-11T10:00:00+00:00",
            "_kind": "text",
        },
        {
            "role": "assistant",
            "content": "earlier assistant turn",
            "_ts": "2026-06-11T10:01:00+00:00",
        },
        {
            # Pure tool_result entry — user-role per the Anthropic API,
            # rendered under the **Tool** speaker since 71998aa.
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_0123456789",
                    "content": "3 records found",
                },
            ],
            "_ts": "2026-06-11T10:02:00+00:00",
        },
        {
            "role": "assistant",
            "content": "this is the target turn",
            "_ts": "2026-06-11T10:03:00+00:00",
        },
    ]

    body = _build_session_body(sess)
    # Fixture validity: the rendered body must actually exercise the
    # Tool-line path — if the renderer stops emitting **Tool** headers
    # for pure-tool_result entries, this contract test must not pass
    # vacuously.
    assert "**Tool** (" in body

    record = tmp_path / "session1.md"
    record.write_text(
        frontmatter.dumps(frontmatter.Post(body, type="session")),
        encoding="utf-8",
    )

    # turn_index 3 = the final assistant entry, 0-based global
    # transcript index (recorded at failure time in bot.py as
    # ``len(session.transcript) - 1``).
    text = _extract_assistant_turn_text(record, 3)
    assert text == "this is the target turn"


@pytest.mark.asyncio
async def test_resolve_local_item_deliver_text_session_missing_leaves_pending(
    tmp_path: Path,
) -> None:
    """Action plan failure leaves the item pending (atomicity)."""
    queue_path = tmp_path / "pending_items.jsonl"
    item = _make_item_with_deliver_text()
    append_item(queue_path, item)

    # Vault has no session records at all → executor should fail.
    vault = tmp_path / "empty-vault"
    vault.mkdir()

    result = await resolve_local_item(
        queue_path=queue_path,
        item_id=item.id,
        resolution_id="show_me",
        vault_path=vault,
        user_id=12345,
    )
    assert result["ok"] is False
    assert result["executed"] is False
    found = find_by_id(queue_path, item.id)
    assert found.status == STATUS_PENDING
