"""PY-A — the consumers of a skipped send, over the HTTP transport.

The contract itself is pinned in ``test_telegram_send_contract.py``: the send
callable RAISES ``TelegramUnavailable`` instead of answering ``[]``, and the
transport server turns that into ``503 {"status": "skipped"}`` with no dedupe
row, which the client re-raises as the same narrow type.

THIS FILE PINS WHAT EACH CONSUMER DOES ABOUT IT. Each one used to read the
non-delivery as a delivery, and the four differ sharply in what that cost:
pending-items DESTROYED the content it was recovering, the email classifier
corrupted the calibration corpus with ``pushed_to_telegram: true``, and the
brief and the send-test CLI merely lied in the log and on the terminal.

EVERY REFUSAL PIN HERE CARRIES ITS POSITIVE CONTROL — "dark ⇒ nothing
recorded" is equally true of a build where the path never works at all, so
each skip assertion is paired with the same call against a live channel.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import frontmatter
import structlog

from alfred.pending_items.executor import resolve_local_item
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
from alfred.transport.exceptions import (
    TELEGRAM_UNAVAILABLE_REASON,
    TelegramUnavailable,
    TransportServerDown,
)


def _events(captured: list[dict], name: str) -> list[dict]:
    return [c for c in captured if c.get("event") == name]



def _item_with_deliver_text() -> PendingItem:
    return PendingItem(
        id=new_item_id(),
        category="outbound_failure",
        created_at="2026-04-28T16:00:00+00:00",
        created_by_instance="hypatia",
        session_id="d145d57c",
        context="test",
        resolution_options=[
            ResolutionOption(
                id="show_me",
                label="Show me",
                action_plan=ActionPlan(
                    type="deliver_text",
                    params={
                        "source": "session_record",
                        "session_id": "d145d57c",
                        "turn_index": 1,
                    },
                ),
            ),
        ],
    )


def _write_session(vault: Path) -> None:
    sess = vault / "session"
    sess.mkdir(parents=True, exist_ok=True)
    fm = {
        "type": "session",
        "name": "Conversation - 2026-04-28 fixture",
        "telegram": {"session_id": "d145d57c", "chat_id": 12345},
    }
    body = (
        "# Transcript\n\n"
        "**Andrew** (16:00): hi\n\n"
        "**Alfred** (16:00): the turn that already failed to deliver once"
    )
    (sess / "session1.md").write_text(
        frontmatter.dumps(frontmatter.Post(body, **fm)), encoding="utf-8",
    )


class TestPendingItemStaysPending:
    """WHY THIS ONE IS THE URGENT ONE. ``deliver_text`` is the RECOVERY path
    for content that already failed to deliver once; resolving destroys the
    item (``mark_resolved`` is irreversible) and there is no second delivery
    leg. Shipped behaviour on a dark instance: transport answers, executor
    reports "delivered N chars", item resolved, content gone."""

    async def _resolve(self, tmp_path: Path, sender):  # type: ignore[no-untyped-def]
        queue_path = tmp_path / "pending_items.jsonl"
        item = _item_with_deliver_text()
        append_item(queue_path, item)
        vault = tmp_path / "vault"
        _write_session(vault)
        with patch("alfred.transport.client.send_outbound_batch", sender):
            result = await resolve_local_item(
                queue_path=queue_path,
                item_id=item.id,
                resolution_id="show_me",
                vault_path=vault,
                user_id=12345,
            )
        return result, find_by_id(queue_path, item.id)

    async def test_a_dark_send_leaves_the_item_pending(self, tmp_path: Path) -> None:
        async def _dark(user_id, chunks, dedupe_key=None, client_name=None):
            raise TelegramUnavailable("no bot on this instance")

        result, found = await self._resolve(tmp_path, _dark)
        assert result["executed"] is False
        assert result["ok"] is False
        assert result["error"] == TELEGRAM_UNAVAILABLE_REASON
        assert found.status == STATUS_PENDING

    async def test_a_live_send_still_resolves_the_item(self, tmp_path: Path) -> None:
        """POSITIVE CONTROL. Without it, "stays pending" is equally true of a
        build where deliver_text never works at all."""

        async def _live(user_id, chunks, dedupe_key=None, client_name=None):
            return {"id": "abc", "sent_count": len(chunks)}

        result, found = await self._resolve(tmp_path, _live)
        assert result["executed"] is True
        assert found.status == STATUS_RESOLVED

    async def test_the_summary_does_not_claim_a_delivery(self, tmp_path: Path) -> None:
        """The operator-facing string. "delivered 4852 chars" for a message
        that never left is the sentence that made the incident invisible."""

        async def _dark(user_id, chunks, dedupe_key=None, client_name=None):
            raise TelegramUnavailable("no bot on this instance")

        result, _ = await self._resolve(tmp_path, _dark)
        summary = result["summary"].lower()
        # The shipped string was "delivered <N> chars in <M> chunk(s) for
        # session ... turn ...". Pin both halves of its shape, not the word
        # "delivered" alone — "not delivered" contains it.
        assert not summary.startswith("delivered")
        assert "chars in" not in summary
        assert "not delivered" in summary
        assert "stays pending" in summary

    async def test_a_transport_outage_is_still_its_own_error(
        self, tmp_path: Path,
    ) -> None:
        """Dark bot and downed talker both leave the item pending — correctly —
        but the operator needs to know which, because only one of them is
        fixable by waiting."""

        async def _down(user_id, chunks, dedupe_key=None, client_name=None):
            raise TransportServerDown("connection refused")

        result, found = await self._resolve(tmp_path, _down)
        assert result["executed"] is False
        assert result["error"] != TELEGRAM_UNAVAILABLE_REASON
        assert found.status == STATUS_PENDING


# ---------------------------------------------------------------------------
# CONSUMER 2 — the email classifier's c5 high-priority push
# ---------------------------------------------------------------------------


class TestEmailHighPush:
    """Shipped behaviour: ``_push_high_priority_email`` returned True and the
    record was stamped ``pushed_to_telegram: true`` for mail that never left.
    That field is what the calibration UI greps to find "records that triggered
    an active operator notification" — so the dead leg was also corrupting the
    calibration corpus.

    THE DOWNGRADE THIS FIX RESTS ON: severity stays moderate only because the
    ``email_urgent`` feed card is a live operator-facing leg on web-only
    instances. That leg is verified in :class:`TestTheLegsThisLaneLeansOn`."""

    def _config(self):  # type: ignore[no-untyped-def]
        from alfred.email_classifier.config import EmailClassifierConfig

        return EmailClassifierConfig(enabled=True, primary_telegram_user_id=12345)

    def _result(self):  # type: ignore[no-untyped-def]
        from alfred.email_classifier.classifier import ClassificationResult

        return ClassificationResult(
            priority="high",
            action_hint="reply today",
            reasoning="named contact, deadline",
            written_to="note/Urgent thing.md",
        )

    async def _push(self, sender):  # type: ignore[no-untyped-def]
        from alfred.email_classifier.classifier import _push_high_priority_email

        with patch("alfred.transport.client.send_outbound", sender):
            return await _push_high_priority_email(
                "note/Urgent thing.md",
                self._result(),
                {"email_message_id": "<abc@example.com>"},
                "Body of the email.",
                "From: Someone <someone@example.com>\nSubject: Urgent thing\n",
                self._config(),
            )

    async def test_a_dark_push_returns_false(self) -> None:
        async def _dark(user_id, text, scheduled_at=None, dedupe_key=None, client_name=None):
            raise TelegramUnavailable("no bot on this instance")

        assert await self._push(_dark) is False

    async def test_a_live_push_still_returns_true(self) -> None:
        """POSITIVE CONTROL — otherwise "returns False" is satisfied by a build
        whose push never works."""

        async def _live(user_id, text, scheduled_at=None, dedupe_key=None, client_name=None):
            return {"id": "abc", "status": "sent"}

        assert await self._push(_live) is True

    async def test_the_dark_push_logs_a_skip_not_a_send(self) -> None:
        async def _dark(user_id, text, scheduled_at=None, dedupe_key=None, client_name=None):
            raise TelegramUnavailable("no bot on this instance")

        with structlog.testing.capture_logs() as captured:
            await self._push(_dark)
        assert _events(captured, "email_classifier.high_push_sent") == []
        # ...and NOT the generic failure line either: a dark channel is not an
        # outage, and an operator triaging "high_push_failed" would go looking
        # for a transport fault that does not exist.
        assert _events(captured, "email_classifier.high_push_failed") == []
        matches = _events(captured, "email_classifier.high_push_skipped_no_bot")
        assert len(matches) == 1
        assert matches[0]["path"] == "note/Urgent thing.md"
        assert matches[0]["reason"] == TELEGRAM_UNAVAILABLE_REASON

    async def test_a_real_outage_still_logs_the_failure_line(self) -> None:
        """The sibling: ``high_push_failed`` must survive for the case it was
        written for, or this fix trades one blind spot for another."""

        async def _down(user_id, text, scheduled_at=None, dedupe_key=None, client_name=None):
            raise TransportServerDown("connection refused")

        with structlog.testing.capture_logs() as captured:
            assert await self._push(_down) is False
        assert len(_events(captured, "email_classifier.high_push_failed")) == 1
        assert _events(captured, "email_classifier.high_push_skipped_no_bot") == []


# ---------------------------------------------------------------------------
# CONSUMER 6a — brief.pushed
# ---------------------------------------------------------------------------


class TestBriefPush:
    async def _push(self, sender):  # type: ignore[no-untyped-def]
        from alfred.brief.daemon import _push_brief_to_telegram

        with patch("alfred.transport.client.send_outbound_batch", sender):
            with structlog.testing.capture_logs() as captured:
                await _push_brief_to_telegram("# Brief\n\nbody", "2026-08-15", 12345)
        return captured

    async def test_a_dark_push_does_not_log_pushed(self) -> None:
        async def _dark(user_id, chunks, dedupe_key=None, client_name=None):
            raise TelegramUnavailable("no bot on this instance")

        captured = await self._push(_dark)
        assert _events(captured, "brief.pushed") == []
        matches = _events(captured, "brief.push_skipped_no_telegram")
        assert len(matches) == 1
        assert matches[0]["date"] == "2026-08-15"
        assert matches[0]["reason"] == TELEGRAM_UNAVAILABLE_REASON

    async def test_a_live_push_still_logs_pushed(self) -> None:
        async def _live(user_id, chunks, dedupe_key=None, client_name=None):
            return {"id": "abc", "sent_count": 1}

        captured = await self._push(_live)
        assert len(_events(captured, "brief.pushed")) == 1
        assert _events(captured, "brief.push_skipped_no_telegram") == []


# ---------------------------------------------------------------------------
# CONSUMER 6b — `alfred transport send-test`
# ---------------------------------------------------------------------------


class TestSendTestCli:
    """A diagnostic that prints "Sent:" and exits 0 on a channel that delivered
    nothing is worse than no diagnostic: it is the command an operator runs to
    ANSWER the question this lane is about."""

    def _run(self, sender, wants_json=False):  # type: ignore[no-untyped-def]
        from alfred.transport.cli import cmd_send_test

        with patch("alfred.transport.client.send_outbound", sender):
            return cmd_send_test({}, 12345, "smoke", wants_json)

    def test_a_dark_send_exits_nonzero_and_says_skipped(self, capsys) -> None:  # type: ignore[no-untyped-def]
        async def _dark(user_id, text, scheduled_at=None, dedupe_key=None, client_name=None):
            raise TelegramUnavailable("no bot on this instance")

        code = self._run(_dark)
        out = capsys.readouterr().out
        assert code != 0
        assert "SKIPPED" in out
        assert "Sent:" not in out

    def test_a_live_send_still_exits_zero(self, capsys) -> None:  # type: ignore[no-untyped-def]
        async def _live(user_id, text, scheduled_at=None, dedupe_key=None, client_name=None):
            return {"id": "abc", "status": "sent", "telegram_message_id": 701}

        code = self._run(_live)
        out = capsys.readouterr().out
        assert code == 0
        assert "Sent:" in out

    def test_the_json_form_carries_the_skip_machine_readably(self, capsys) -> None:  # type: ignore[no-untyped-def]
        async def _dark(user_id, text, scheduled_at=None, dedupe_key=None, client_name=None):
            raise TelegramUnavailable("no bot on this instance")

        code = self._run(_dark, wants_json=True)
        payload = json.loads(capsys.readouterr().out)
        assert code != 0
        assert payload["status"] == "skipped"
        assert payload["reason"] == TELEGRAM_UNAVAILABLE_REASON
        assert payload["delivered"] is False
