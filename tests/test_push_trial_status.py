"""#96 stage 1 — the push-delivery trial's read surface.

THE PROPERTY EVERY TEST HERE DEFENDS: a slot that was never SENT must never be
reported as a slot that failed to ARRIVE. The trial was commissioned because
"I didn't get a notification" is ambiguous — an instrument that collapses
instrument-downtime, send-failure and genuine-silence into one "not received"
number measures nothing and would retire the question with a wrong answer.

The ledger is written by the WEB app (TypeScript). ``tests/fixtures/
push_trial_ledger.jsonl`` is the cross-language agreement artifact: the vitest
side regenerates it byte-for-byte through the real writer, this side reads it
through the real reader. If either language's row shape drifts, its own half
goes red.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import structlog

from alfred.push_trial import (
    DEFAULT_TRIAL_PATH,
    STATE_NOT_SENT,
    STATE_PENDING,
    STATE_RECEIVED,
    STATE_SEND_FAILED,
    STATE_UNKNOWN,
    TRIAL_PATH_ENV,
    build_status,
    read_rows,
    render_status,
    trial_path,
)

FIXTURE = Path(__file__).parent / "fixtures" / "push_trial_ledger.jsonl"
# After d2-w1's 08:38 slot (so it reads as never-sent) and before d2-w2's 13:55.
NOW = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)


def _by_id(status) -> dict:
    return {s.push_id: s for s in status.slots}


# ===========================================================================
# The four states — the whole point of the instrument
# ===========================================================================

def test_the_four_states_are_distinguished() -> None:
    slots = _by_id(build_status(FIXTURE, now=NOW))

    assert slots["trial-d1-w1"].state == STATE_RECEIVED
    assert slots["trial-d1-w2"].state == STATE_UNKNOWN
    assert slots["trial-d1-w3"].state == STATE_SEND_FAILED
    assert slots["trial-d2-w1"].state == STATE_NOT_SENT
    assert slots["trial-d2-w2"].state == STATE_PENDING


def test_never_sent_is_not_counted_as_a_delivery_failure() -> None:
    """THE headline property. d2-w1's time passed with no send attempt — the
    instrument was down. Rolling that into the not-received bucket would invent
    a delivery failure that never happened."""
    counts = build_status(FIXTURE, now=NOW).counts

    assert counts[STATE_NOT_SENT] == 1
    assert counts[STATE_UNKNOWN] == 1
    # The two must be separately reported, never summed into one number.
    assert counts[STATE_NOT_SENT] != counts[STATE_UNKNOWN] + counts[STATE_NOT_SENT]


def test_a_send_failure_is_not_a_delivery_failure_either() -> None:
    slots = _by_id(build_status(FIXTURE, now=NOW))

    assert slots["trial-d1-w3"].state == STATE_SEND_FAILED
    assert slots["trial-d1-w3"].error == "no_subscriptions"
    assert build_status(FIXTURE, now=NOW).counts[STATE_SEND_FAILED] == 1


def test_full_counts() -> None:
    counts = build_status(FIXTURE, now=NOW).counts

    assert counts == {
        STATE_RECEIVED: 1,
        STATE_UNKNOWN: 1,
        STATE_SEND_FAILED: 1,
        STATE_NOT_SENT: 1,
        STATE_PENDING: 2,
    }


def test_latency_is_measured_only_when_both_ends_are_known() -> None:
    slots = _by_id(build_status(FIXTURE, now=NOW))

    assert slots["trial-d1-w1"].latency_s == pytest.approx(100.0)
    assert slots["trial-d1-w2"].latency_s is None
    assert slots["trial-d2-w1"].latency_s is None


def test_pending_flips_to_not_sent_once_its_time_passes() -> None:
    """The same ledger, read later, must reclassify — otherwise a stalled
    instrument stays invisible behind 'still pending'."""
    later = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)
    slots = _by_id(build_status(FIXTURE, now=later))

    assert slots["trial-d2-w2"].state == STATE_NOT_SENT
    assert slots["trial-d2-w3"].state == STATE_NOT_SENT


# ===========================================================================
# Robustness
# ===========================================================================

def test_orphan_rows_are_surfaced_not_dropped() -> None:
    """A sent row for a slot this ledger never scheduled means the trial was
    restarted against a different start date. Dropping it silently would
    under-report real deliveries."""
    with structlog.testing.capture_logs() as captured:
        status = build_status(FIXTURE, now=NOW)

    assert status.orphan_ids == ["trial-d9-w9"]
    events = [c for c in captured if c.get("event") == "push_trial.orphan_rows"]
    assert len(events) == 1
    assert events[0]["count"] == 1


def test_absent_ledger_is_not_started_not_empty_evidence(tmp_path: Path) -> None:
    status = build_status(tmp_path / "nope.jsonl", now=NOW)

    assert status.slots == []
    assert status.unreadable is False
    body = render_status(status, "nope.jsonl")
    assert "has not started" in body
    # It must NOT read as a delivery conclusion.
    assert "not received" not in body


def test_unreadable_ledger_refuses_to_conclude(tmp_path: Path, monkeypatch) -> None:
    bad = tmp_path / "ledger.jsonl"
    bad.write_text("{}\n", encoding="utf-8")

    def _boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", _boom)
    with structlog.testing.capture_logs() as captured:
        status = build_status(bad, now=NOW)

    assert status.unreadable is True
    assert len(
        [c for c in captured if c.get("event") == "push_trial.ledger_unreadable"]) == 1
    assert "no delivery" in render_status(status, str(bad))


def test_malformed_rows_are_skipped_and_counted(tmp_path: Path) -> None:
    p = tmp_path / "ledger.jsonl"
    p.write_text(
        '{"type":"scheduled","push_id":"a","due_ts":"2026-08-12T08:00:00Z"}\n'
        "not json\n"
        '"a bare string"\n'
        '{"type":"sent","push_id":"a","sent_ts":"2026-08-12T08:00:05Z"}\n',
        encoding="utf-8",
    )
    status = build_status(p, now=NOW)

    assert status.skipped_rows == 2
    assert _by_id(status)["a"].state == STATE_UNKNOWN


# ===========================================================================
# Render + path resolution
# ===========================================================================

def test_render_names_what_each_number_means() -> None:
    body = render_status(build_status(FIXTURE, now=NOW), "x.jsonl")

    # A bare count invites the exact misreading the trial exists to prevent.
    assert "instrument" in body.lower()
    assert "not sent" in body
    assert "unknown" in body
    for pid in ("trial-d1-w1", "trial-d2-w1"):
        assert pid in body


def test_render_asks_for_the_ruling_when_unknowns_exist() -> None:
    """`sent` is only collapsible by the operator — the human-in-the-loop half."""
    body = render_status(build_status(FIXTURE, now=NOW), "x.jsonl")
    assert "did it arrive?" in body


def test_render_omits_the_ruling_prompt_when_there_is_nothing_to_rule(
    tmp_path: Path,
) -> None:
    p = tmp_path / "ledger.jsonl"
    p.write_text(
        '{"type":"scheduled","push_id":"a","due_ts":"2026-08-12T08:00:00Z"}\n'
        '{"type":"sent","push_id":"a","sent_ts":"2026-08-12T08:00:05Z"}\n'
        '{"type":"receipt","push_id":"a","received_ts":"2026-08-12T08:00:20Z"}\n',
        encoding="utf-8",
    )
    body = render_status(build_status(p, now=NOW), "x.jsonl")
    assert "did it arrive?" not in body


def test_path_is_env_only_and_matches_the_senders_default(monkeypatch) -> None:
    """The sender resolves ALFRED_WEB_PUSH_TRIAL with this same default and
    reads no YAML; a config key here would point the reader at a file nothing
    writes."""
    monkeypatch.delenv(TRIAL_PATH_ENV, raising=False)
    assert trial_path() == DEFAULT_TRIAL_PATH
    monkeypatch.setenv(TRIAL_PATH_ENV, "/tmp/elsewhere.jsonl")
    assert trial_path() == "/tmp/elsewhere.jsonl"


# ===========================================================================
# Cross-language agreement
# ===========================================================================

def test_every_fixture_row_type_is_understood() -> None:
    """The row vocabulary this reader handles must cover everything the writer
    emits. An unknown type would be silently ignored — a whole class of ledger
    row invisible to the operator."""
    rows, skipped, unreadable = read_rows(FIXTURE)

    assert not unreadable and skipped == 0
    assert {r["type"] for r in rows} == {"scheduled", "sent", "send_failed", "receipt"}
    # Each type carries the timestamp field this reader reads off it.
    for r in rows:
        field = {
            "scheduled": "due_ts", "sent": "sent_ts",
            "send_failed": "sent_ts", "receipt": "received_ts",
        }[r["type"]]
        assert isinstance(r.get(field), str) and r[field]
