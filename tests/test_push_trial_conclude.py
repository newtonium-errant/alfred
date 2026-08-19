"""The trial's EARLY END, read from this side.

THE PROPERTY THESE TESTS DEFEND, and it is the same one the whole module exists
for: a slot that was deliberately retired must never be reported as a slot the
instrument failed to send. ``not_sent`` means INSTRUMENT DOWN. A trial the
operator ended on purpose would otherwise show a week of phantom outages — the
false signal this instrument was commissioned to eliminate, arriving through the
feature meant to end it cleanly.

Measured, not assumed: against the reader as it stood before this change, the
ledger below reported ``not_sent`` for both cancelled slots AND filed the
conclusion marker under ``orphan_ids`` (whose warning blames "a restarted trial
with a different start date"). Neither crashed — the misreport was the risk.

The rows here are written in exactly the shape ``pushTrial.ts`` emits; the web
side has its own pins on the writer.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from alfred.push_trial import (
    ROW_CANCELLED,
    ROW_CONCLUDED,
    RULABLE_STATES,
    STATE_CANCELLED,
    STATE_NOT_SENT,
    STATE_RECEIVED,
    STATE_UNKNOWN,
    RulingError,
    build_status,
    record_conclusion,
    record_ruling,
    render_status,
)

# Day 1's slots are past; day 2's last slot is still future.
NOW = datetime(2026, 8, 17, 12, 0, 0, tzinfo=timezone.utc)

SCHEDULED = [
    {"type": "scheduled", "push_id": "trial-d1-w1", "due_ts": "2026-08-16T11:07:00.000Z"},
    {"type": "scheduled", "push_id": "trial-d1-w2", "due_ts": "2026-08-16T16:24:00.000Z"},
    {"type": "scheduled", "push_id": "trial-d2-w1", "due_ts": "2026-08-17T11:38:00.000Z"},
    {"type": "scheduled", "push_id": "trial-d2-w2", "due_ts": "2026-08-18T16:55:00.000Z"},
]


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8",
    )
    return path


def _ledger(tmp_path: Path, extra: list[dict]) -> Path:
    return _write(tmp_path / "push_trial.jsonl", [*SCHEDULED, *extra])


def _by_id(status) -> dict:
    return {s.push_id: s for s in status.slots}


CONCLUDED_ROW = {
    "type": ROW_CONCLUDED,
    "push_id": "",
    "concluded_ts": "2026-08-16T18:00:00.000Z",
    "reason": "delivery confirmed",
}


def _cancelled(push_id: str) -> dict:
    return {
        "type": ROW_CANCELLED,
        "push_id": push_id,
        "cancelled_ts": "2026-08-16T18:01:00.000Z",
        "reason": "delivery confirmed",
    }


class TestCancelledIsNotAnOutage:
    def test_a_cancelled_slot_reads_cancelled_not_not_sent(self, tmp_path: Path) -> None:
        """THE PIN. Before this change both of these read ``not_sent``."""
        path = _ledger(tmp_path, [
            CONCLUDED_ROW, _cancelled("trial-d1-w2"), _cancelled("trial-d2-w1"),
        ])
        slots = _by_id(build_status(path, now=NOW))
        assert slots["trial-d1-w2"].state == STATE_CANCELLED
        assert slots["trial-d2-w1"].state == STATE_CANCELLED

    def test_positive_control_a_genuinely_unsent_slot_is_STILL_not_sent(
        self, tmp_path: Path,
    ) -> None:
        """Without this the pin above would pass against a reader that had
        stopped reporting outages at all — which would hide the very failure the
        ``not_sent`` state exists to surface."""
        path = _ledger(tmp_path, [])  # no conclusion, no cancellations
        slots = _by_id(build_status(path, now=NOW))
        assert slots["trial-d1-w1"].state == STATE_NOT_SENT
        assert slots["trial-d1-w2"].state == STATE_NOT_SENT

    def test_the_cancelled_slot_says_WHY(self, tmp_path: Path) -> None:
        path = _ledger(tmp_path, [CONCLUDED_ROW, _cancelled("trial-d1-w2")])
        slot = _by_id(build_status(path, now=NOW))["trial-d1-w2"]
        assert slot.cancel_reason == "delivery confirmed"
        assert slot.cancelled_ts

    def test_a_SENT_slot_that_was_later_cancelled_stays_sent(self, tmp_path: Path) -> None:
        """An observation is not retracted. The push left the server and may have
        arrived; a later cancellation cannot un-happen that."""
        path = _ledger(tmp_path, [
            {"type": "sent", "push_id": "trial-d1-w1", "sent_ts": "2026-08-16T11:07:03.000Z"},
            CONCLUDED_ROW,
            _cancelled("trial-d1-w1"),
        ])
        assert _by_id(build_status(path, now=NOW))["trial-d1-w1"].state == STATE_UNKNOWN

    def test_a_RECEIVED_slot_is_untouched_by_a_cancellation(self, tmp_path: Path) -> None:
        path = _ledger(tmp_path, [
            {"type": "sent", "push_id": "trial-d1-w1", "sent_ts": "2026-08-16T11:07:03.000Z"},
            {"type": "receipt", "push_id": "trial-d1-w1", "received_ts": "2026-08-16T11:07:41.000Z"},
            CONCLUDED_ROW,
            _cancelled("trial-d1-w1"),
        ])
        assert _by_id(build_status(path, now=NOW))["trial-d1-w1"].state == STATE_RECEIVED

    def test_a_cancelled_slot_is_NOT_rulable(self, tmp_path: Path) -> None:
        """Ruling it would assert something about a delivery that never had a
        chance to happen — the same reason ``not_sent`` is refused."""
        assert STATE_CANCELLED not in RULABLE_STATES
        path = _ledger(tmp_path, [CONCLUDED_ROW, _cancelled("trial-d1-w2")])
        with pytest.raises(RulingError):
            record_ruling(path, "trial-d1-w2", "missed")


class TestTheConclusionMarker:
    def test_is_not_mistaken_for_an_orphan_or_a_malformed_line(self, tmp_path: Path) -> None:
        """Its ``push_id`` is present-but-empty. Before this change it landed in
        ``orphan_ids`` and tripped a warning blaming a restarted trial; had it
        omitted the field entirely, ``read_rows`` would have counted it as
        corruption instead."""
        path = _ledger(tmp_path, [CONCLUDED_ROW, _cancelled("trial-d1-w2")])
        status = build_status(path, now=NOW)
        assert status.orphan_ids == []
        assert status.skipped_rows == 0

    def test_surfaces_when_and_why(self, tmp_path: Path) -> None:
        status = build_status(_ledger(tmp_path, [CONCLUDED_ROW]), now=NOW)
        assert status.is_concluded is True
        assert status.concluded_ts == "2026-08-16T18:00:00.000Z"
        assert status.concluded_reason == "delivery confirmed"

    def test_a_running_trial_reports_NOT_concluded_as_a_value(self, tmp_path: Path) -> None:
        status = build_status(_ledger(tmp_path, []), now=NOW)
        assert status.is_concluded is False
        assert status.concluded_ts == ""
        assert status.to_dict()["is_concluded"] is False

    def test_FIRST_conclusion_wins(self, tmp_path: Path) -> None:
        """A conclusion is an EVENT, not an opinion — unlike a ruling, which is
        later-wins because he is allowed to correct a recollection."""
        second = {**CONCLUDED_ROW, "concluded_ts": "2026-08-17T09:00:00.000Z", "reason": "changed my mind"}
        status = build_status(_ledger(tmp_path, [CONCLUDED_ROW, second]), now=NOW)
        assert status.concluded_ts == "2026-08-16T18:00:00.000Z"
        assert status.concluded_reason == "delivery confirmed"


class TestRecordConclusion:
    def test_writes_a_marker_the_sender_can_find(self, tmp_path: Path) -> None:
        path = _ledger(tmp_path, [])
        status = record_conclusion(path, "delivery confirmed", now=NOW)
        assert status.is_concluded is True
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        marker = [r for r in rows if r["type"] == ROW_CONCLUDED]
        assert len(marker) == 1
        # The shape the WEB sender reads: present-but-empty push_id, a timestamp,
        # and the reason it copies onto every cancelled slot.
        assert marker[0]["push_id"] == ""
        assert marker[0]["reason"] == "delivery confirmed"
        assert marker[0]["concluded_ts"]

    def test_is_idempotent_and_does_not_move_the_moment(self, tmp_path: Path) -> None:
        path = _ledger(tmp_path, [])
        record_conclusion(path, "first", now=NOW)
        later = datetime(2026, 8, 18, 9, 0, 0, tzinfo=timezone.utc)
        status = record_conclusion(path, "second", now=later)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len([r for r in rows if r["type"] == ROW_CONCLUDED]) == 1
        assert status.concluded_reason == "first"

    def test_refuses_when_the_ledger_cannot_be_read(self, tmp_path: Path) -> None:
        """Appending to a store whose current contents are unknown is how a
        second conclusion gets written over a first one nobody could see."""
        path = tmp_path / "unreadable"
        path.mkdir()  # a directory: exists, cannot be read as a file
        with pytest.raises(RulingError):
            record_conclusion(path, "x", now=NOW)

    def test_an_empty_reason_is_allowed_and_recorded_as_empty(self, tmp_path: Path) -> None:
        status = record_conclusion(_ledger(tmp_path, []), "", now=NOW)
        assert status.is_concluded is True
        assert status.concluded_reason == ""


class TestRenderStatus:
    def test_says_CONCLUDED_up_front_and_names_the_cancelled_count(self, tmp_path: Path) -> None:
        """Every count below the banner is a final tally, not a running one. A
        reader who missed that would treat cancelled slots as work still owed."""
        path = _ledger(tmp_path, [
            CONCLUDED_ROW, _cancelled("trial-d1-w2"), _cancelled("trial-d2-w1"),
            _cancelled("trial-d2-w2"),
        ])
        out = render_status(build_status(path, now=NOW), str(path))
        assert "CONCLUDED" in out
        assert "delivery confirmed" in out
        assert "cancelled        3" in out
        # And it must not be sold as an outage.
        assert "A DECISION, not an outage." in out

    def test_a_running_trial_shows_no_conclusion_banner(self, tmp_path: Path) -> None:
        out = render_status(build_status(_ledger(tmp_path, []), now=NOW), "p")
        assert "CONCLUDED" not in out
        assert "not sent" in out  # control: the normal table still renders
