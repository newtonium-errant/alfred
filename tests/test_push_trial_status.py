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
        # Present-and-zero rather than absent: a reader that branches on these
        # keys must not KeyError on a trial nobody has ruled yet — nor on one
        # nobody has concluded, which is why `cancelled` is here too.
        "ruled_arrived": 0,
        "ruled_missed": 0,
        "cancelled": 0,
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


# ===========================================================================
# The reconcile writer — turning a remark into data
# ===========================================================================
#
# The trial's OUTPUT is the ruling data. If day-3 unknowns get settled in
# conversation, the envelope decision ends up resting on someone's recollection
# of chat messages rather than on the ledger — so the answer has to land in the
# file, under its own state, distinguishable from a measured tap.

from alfred.push_trial import (  # noqa: E402
    ROW_RULING,
    RULABLE_STATES,
    STATE_RULED_ARRIVED,
    STATE_RULED_MISSED,
    VERDICT_ARRIVED,
    VERDICT_MISSED,
    RulingError,
    record_ruling,
)


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    """A working copy of the fixture, so rulings can be appended to it."""
    p = tmp_path / "push_trial.jsonl"
    p.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    return p


def test_ruling_arrived_lands_under_its_own_state(ledger: Path) -> None:
    slot = record_ruling(ledger, "trial-d1-w2", VERDICT_ARRIVED, now=NOW)

    assert slot.state == STATE_RULED_ARRIVED
    # NOT `received` — a recollection must never be counted as a measurement.
    assert slot.state != STATE_RECEIVED
    assert slot.is_ruled is True
    assert slot.latency_s is None, "a ruling carries no timing"


def test_ruling_missed_is_the_only_genuine_delivery_failure(ledger: Path) -> None:
    record_ruling(ledger, "trial-d1-w2", VERDICT_MISSED, now=NOW)
    counts = build_status(ledger, now=NOW).counts

    assert counts[STATE_RULED_MISSED] == 1
    assert counts[STATE_UNKNOWN] == 0
    # The instrument-side states are untouched by a delivery ruling.
    assert counts[STATE_NOT_SENT] == 1
    assert counts[STATE_SEND_FAILED] == 1


def test_the_ruling_is_APPENDED_and_the_send_row_is_untouched(ledger: Path) -> None:
    """The audit trail is the point: the ledger must still show what was unknown
    and when it was settled."""
    before = ledger.read_text(encoding="utf-8")
    record_ruling(ledger, "trial-d1-w2", VERDICT_ARRIVED, now=NOW)
    after = ledger.read_text(encoding="utf-8")

    assert after.startswith(before), "an existing row was rewritten"
    added = [json.loads(l) for l in after[len(before):].strip().splitlines()]
    assert len(added) == 1
    assert added[0] == {
        "type": ROW_RULING,
        "push_id": "trial-d1-w2",
        "verdict": VERDICT_ARRIVED,
        "ruled_ts": NOW.isoformat(),
    }
    # The original sent row is still there, verbatim.
    assert '{"type":"sent","push_id":"trial-d1-w2"' in after


def test_a_later_ruling_wins_and_both_stay_on_disk(ledger: Path) -> None:
    record_ruling(ledger, "trial-d1-w2", VERDICT_MISSED, now=NOW)
    later = datetime(2026, 8, 13, 18, 0, 0, tzinfo=timezone.utc)
    slot = record_ruling(ledger, "trial-d1-w2", VERDICT_ARRIVED, now=later)

    assert slot.state == STATE_RULED_ARRIVED
    text = ledger.read_text(encoding="utf-8")
    assert text.count(f'"type": "{ROW_RULING}"') == 2, "the correction erased history"


def test_a_measurement_outranks_a_recollection(ledger: Path) -> None:
    """He tapped d1-w1. A ruling cannot demote a measured arrival — and the
    ruling row is still written, so the disagreement stays visible."""
    with pytest.raises(RulingError) as exc:
        record_ruling(ledger, "trial-d1-w1", VERDICT_MISSED, now=NOW)

    assert "tapped" in str(exc.value).lower()
    assert build_status(ledger, now=NOW).counts[STATE_RECEIVED] == 1


@pytest.mark.parametrize("push_id,state", [
    ("trial-d2-w1", STATE_NOT_SENT),
    ("trial-d1-w3", STATE_SEND_FAILED),
    ("trial-d2-w2", STATE_PENDING),
])
def test_only_a_sent_but_untapped_slot_is_rulable(
    ledger: Path, push_id: str, state: str,
) -> None:
    """Ruling a never-sent / failed / future slot asserts something about a
    delivery that never had a chance to happen."""
    with pytest.raises(RulingError) as exc:
        record_ruling(ledger, push_id, VERDICT_ARRIVED, now=NOW)

    assert state in str(exc.value)
    assert state not in RULABLE_STATES
    # Nothing was written.
    assert ROW_RULING not in ledger.read_text(encoding="utf-8")


def test_the_rulable_set_is_exactly_the_sent_untapped_family() -> None:
    """Named explicitly so a future widening is a deliberate edit here, not a
    side effect. `received` is the one that must never join: a recollection
    overwriting a measurement is the blend this whole state model prevents."""
    assert set(RULABLE_STATES) == {
        STATE_UNKNOWN, STATE_RULED_ARRIVED, STATE_RULED_MISSED,
    }
    for excluded in (STATE_RECEIVED, STATE_NOT_SENT, STATE_SEND_FAILED,
                     STATE_PENDING):
        assert excluded not in RULABLE_STATES


def test_an_unknown_slot_id_is_refused_with_help(ledger: Path) -> None:
    with pytest.raises(RulingError) as exc:
        record_ruling(ledger, "trial-d9-w9", VERDICT_ARRIVED, now=NOW)

    assert "no scheduled slot" in str(exc.value)
    assert "trial-d1-w1" in str(exc.value), "the refusal should name real slots"


@pytest.mark.parametrize("verdict", ["", "yes", "ARRIVED?", "maybe", "true"])
def test_a_junk_verdict_is_refused(ledger: Path, verdict: str) -> None:
    with pytest.raises(RulingError):
        record_ruling(ledger, "trial-d1-w2", verdict, now=NOW)
    assert ROW_RULING not in ledger.read_text(encoding="utf-8")


def test_verdict_is_case_and_space_tolerant(ledger: Path) -> None:
    slot = record_ruling(ledger, "trial-d1-w2", "  Arrived  ", now=NOW)
    assert slot.state == STATE_RULED_ARRIVED


def test_an_unreadable_ledger_refuses_the_append(tmp_path: Path, monkeypatch) -> None:
    """Never append to a store whose current contents are unknown — the slot
    might already be tapped."""
    p = tmp_path / "ledger.jsonl"
    p.write_text("{}\n", encoding="utf-8")

    def _boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", _boom)
    with pytest.raises(RulingError) as exc:
        record_ruling(p, "trial-d1-w2", VERDICT_ARRIVED, now=NOW)
    assert "could not be read" in str(exc.value)


def test_ruled_slots_render_as_rulings_not_as_taps(ledger: Path) -> None:
    """The two qualities of evidence must not look alike in the table the
    envelope decision is read off."""
    record_ruling(ledger, "trial-d1-w2", VERDICT_ARRIVED, now=NOW)
    body = render_status(build_status(ledger, now=NOW), "x.jsonl")

    assert "ruled arrived" in body
    assert "RECALLED" in body
    # d1-w1 was tapped; its evidence column shows the latency.
    assert "tapped +100s" in body
    # d1-w2 was ruled; its evidence column shows the ruling date, not a latency.
    ruled_line = [l for l in body.splitlines() if "trial-d1-w2" in l][0]
    assert "ruled 2026-08-13" in ruled_line
    assert "tapped" not in ruled_line


def test_the_ruling_prompt_disappears_once_everything_is_settled(
    ledger: Path,
) -> None:
    record_ruling(ledger, "trial-d1-w2", VERDICT_MISSED, now=NOW)
    body = render_status(build_status(ledger, now=NOW), "x.jsonl")

    assert "did it arrive?" not in body


def test_the_prompt_names_a_real_runnable_command(ledger: Path) -> None:
    body = render_status(build_status(ledger, now=NOW), "x.jsonl")

    assert "alfred push-trial rule trial-d1-w2 arrived" in body


def test_ruling_is_logged(ledger: Path) -> None:
    with structlog.testing.capture_logs() as captured:
        record_ruling(ledger, "trial-d1-w2", VERDICT_ARRIVED, now=NOW)

    events = [c for c in captured if c.get("event") == "push_trial.ruling_recorded"]
    assert len(events) == 1
    assert events[0]["push_id"] == "trial-d1-w2"
    assert events[0]["verdict"] == VERDICT_ARRIVED
