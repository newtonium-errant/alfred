"""Pins for the one-shot verbless-acted repair (``alfred feed repair``).

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.

The load-bearing pins here are the REFUSALS, and each is paired with a positive
control in the same test — an "excluded input produces nothing" assertion is
vacuous unless the same corpus proves an admissible neighbour DOES produce a
write. Without the control these pass identically against a repair that matches
nothing at all.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import structlog

from alfred.daily_sync.action_router import (
    ACCEPT_ACTION,
    ACK_ACTION,
    DONE_ACTION,
)
from alfred.feed.cli import cmd_repair
from alfred.feed.model import ACTION_RETIRED, STATE_ACTED, STATE_OPEN, FeedItem
from alfred.feed.repair import (
    MAX_JOIN_DELTA_SECONDS,
    VERB_ACCEPT,
    VERB_ACK,
    VERB_DONE,
    JoinPrecisionError,
    build_plan,
    collect_act_events,
    collect_reconcile_events,
    is_repair_candidate,
    parse_log_line,
)
from alfred.feed.store import FeedStore

BASE_TS = "2026-08-01T10:00:00"


def _line(ts: str, event: str, **fields: object) -> str:
    """One structlog ConsoleRenderer-shaped line."""
    rendered = " ".join(f"{k}={v}" for k, v in fields.items())
    return f"{ts}Z [info     ] {event:<30} {rendered}\n"


def _acted_item(item_id: str, *, kind: str = "task", acted_at: str = BASE_TS) -> FeedItem:
    """A LEGACY verbless-acted item: acted, no verb."""
    return FeedItem(
        id=item_id,
        kind=kind,
        state=STATE_ACTED,
        acted_at=f"{acted_at}+00:00",
        acted_action=None,
    )


def _write_store(path: Path, items: list[FeedItem]) -> FeedStore:
    store = FeedStore(path)
    for item in items:
        store.upsert(item)
    # ``upsert`` alone lands the item OPEN; drive it to the legacy acted shape
    # by appending a verbless state event, which is exactly what the old
    # writers produced.
    for item in items:
        if item.state == STATE_ACTED:
            store.set_state(item.id, STATE_ACTED)
    return store


def _raw(store_path: Path, log_dir: Path) -> dict:
    return {
        "feed": {"store_path": str(store_path)},
        "logging": {"dir": str(log_dir)},
    }


# --- parsing -----------------------------------------------------------------


def test_parse_log_line_reads_ts_event_and_fields():
    parsed = parse_log_line(_line(BASE_TS, "feed.act.acted", id="abc", action="done"))
    assert parsed is not None
    assert parsed.event == "feed.act.acted"
    assert parsed.fields["id"] == "abc"
    assert parsed.fields["action"] == "done"


def test_parse_log_line_returns_none_on_non_log_lines():
    # A repair that dies on a traceback line in a production log never runs.
    assert parse_log_line("Traceback (most recent call last):\n") is None
    assert parse_log_line("\n") is None


def test_all_four_act_shapes_yield_their_verb(tmp_path: Path):
    """The four act-success shapes, including the one whose verb is in the
    EVENT NAME rather than an ``action=`` field."""
    log_path = tmp_path / "talker.log"
    log_path.write_text(
        _line(BASE_TS, "feed.act.acted", id="i1", action="done")
        + _line(BASE_TS, "feed.act.slot.accepted", id="i2", tier="t")
        + _line(BASE_TS, "feed.act.slot.done", id="i3", lane="routine")
        # ``feed.act.acked`` carries NO action= field — verb from the name.
        + _line(BASE_TS, "feed.act.acked", id="i4", kind="task"),
        encoding="utf-8",
    )
    events = collect_act_events([log_path])
    assert events["i1"][0].verb == VERB_DONE
    assert events["i2"][0].verb == VERB_ACCEPT
    assert events["i3"][0].verb == VERB_DONE
    assert events["i4"][0].verb == VERB_ACK


def test_verb_constants_match_the_action_router():
    """Drift pin. ``feed`` cannot import ``daily_sync`` (that closes a cycle),
    so the verbs are copied by value — this asserts the copy has not rotted."""
    assert VERB_ACCEPT == ACCEPT_ACTION
    assert VERB_DONE == DONE_ACTION
    assert VERB_ACK == ACK_ACTION


# --- the join ----------------------------------------------------------------


def test_newest_verb_wins_when_one_id_has_several_acts(tmp_path: Path):
    """The case the precision assert exists for: accept THEN done on one id.
    The repair must write ``done`` — writing ``accept`` would be a false
    verdict, which is the failure class this whole command repairs."""
    log_path = tmp_path / "talker.log"
    log_path.write_text(
        _line("2026-08-01T09:30:00", "feed.act.slot.accepted", id="i1")
        + _line(BASE_TS, "feed.act.slot.done", id="i1"),
        encoding="utf-8",
    )
    items = {"i1": _acted_item("i1")}
    plan = build_plan(items, collect_act_events([log_path]), [])
    assert [(b.item_id, b.verb) for b in plan.backfills] == [("i1", VERB_DONE)]


def test_join_precision_refuses_the_WHOLE_plan(tmp_path: Path):
    """A single over-wide match discards every other backfill too.

    Positive control in the same corpus: ``near`` matches at 0.04s and IS
    planned when it stands alone, so the empty result below is the refusal
    firing rather than a repair that can never match anything.
    """
    log_path = tmp_path / "talker.log"
    log_path.write_text(
        _line("2026-08-01T10:00:00.040000", "feed.act.slot.done", id="near")
        + _line("2026-08-01T10:00:05.000000", "feed.act.slot.done", id="far"),
        encoding="utf-8",
    )
    act_events = collect_act_events([log_path])

    # Positive control: the admissible neighbour alone DOES produce a write.
    control = build_plan({"near": _acted_item("near")}, act_events, [])
    assert len(control.backfills) == 1
    assert control.max_delta_seconds < MAX_JOIN_DELTA_SECONDS

    # 5s is inside the 10s search window but far outside the 1s ceiling.
    with pytest.raises(JoinPrecisionError) as excinfo:
        build_plan(
            {"near": _acted_item("near"), "far": _acted_item("far")}, act_events, []
        )
    assert "over-matching" in str(excinfo.value)


def test_item_with_no_acted_at_is_skipped_not_guessed(tmp_path: Path):
    log_path = tmp_path / "talker.log"
    log_path.write_text(_line(BASE_TS, "feed.act.slot.done", id="i1"), encoding="utf-8")
    item = _acted_item("i1")
    item.acted_at = None
    plan = build_plan({"i1": item}, collect_act_events([log_path]), [])
    assert plan.backfills == []
    assert [(s.item_id, s.reason) for s in plan.skipped] == [("i1", "no_acted_at")]


# --- retirements -------------------------------------------------------------


def test_retirement_needs_a_corroborating_reconcile_line(tmp_path: Path):
    """ZERO act lines is not enough on its own.

    Both halves in one test: ``ret`` has a same-second reconcile of its kind
    and IS stamped; ``lonely`` has no corroboration and is left alone. Without
    the positive half, "lonely is skipped" would pass against a build that
    stamps nothing at all.
    """
    log_path = tmp_path / "daily_sync.log"
    log_path.write_text(
        _line(BASE_TS, "feed.reconcile", ok="True", kind="task", open=3, acted=1),
        encoding="utf-8",
    )
    reconcile_events = collect_reconcile_events([log_path])
    items = {
        "ret": _acted_item("ret", kind="task"),
        "lonely": _acted_item("lonely", kind="task", acted_at="2026-08-01T11:00:00"),
    }
    plan = build_plan(items, {}, reconcile_events)

    assert [(r.item_id, r.kind) for r in plan.retirements] == [("ret", "task")]
    assert ("lonely", "no_act_lines_no_reconcile") in [
        (s.item_id, s.reason) for s in plan.skipped
    ]


def test_reconcile_of_a_different_kind_does_not_corroborate(tmp_path: Path):
    log_path = tmp_path / "daily_sync.log"
    log_path.write_text(
        _line(BASE_TS, "feed.reconcile", ok="True", kind="email", open=1, acted=1),
        encoding="utf-8",
    )
    plan = build_plan(
        {"ret": _acted_item("ret", kind="task")},
        {},
        collect_reconcile_events([log_path]),
    )
    assert plan.retirements == []


# --- candidate selection -----------------------------------------------------


def test_only_legacy_verbless_acted_items_are_candidates():
    verbless = _acted_item("legacy")
    with_verb = _acted_item("already")
    with_verb.acted_action = VERB_DONE
    still_open = FeedItem(id="open", kind="task", state=STATE_OPEN)

    assert is_repair_candidate(verbless) is True
    assert is_repair_candidate(with_verb) is False
    assert is_repair_candidate(still_open) is False


# --- the CLI: dry-run, apply, idempotency, absent store ----------------------


def test_dry_run_writes_nothing(tmp_path: Path, capsys):
    """The mandatory-dry-run pin. Byte-identical store after the run."""
    store_path = tmp_path / "feed.jsonl"
    _write_store(store_path, [_acted_item("i1")])
    (tmp_path / "talker.log").write_text(
        _line(BASE_TS, "feed.act.slot.done", id="i1"), encoding="utf-8"
    )
    before = store_path.read_bytes()

    code = cmd_repair(_raw(store_path, tmp_path), apply=False)

    assert code == 0
    assert store_path.read_bytes() == before
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "Nothing was written" in out


def test_apply_stamps_the_verb_and_preserves_state_and_acted_at(tmp_path: Path):
    store_path = tmp_path / "feed.jsonl"
    _write_store(store_path, [_acted_item("i1")])
    (tmp_path / "talker.log").write_text(
        _line(BASE_TS, "feed.act.slot.done", id="i1"), encoding="utf-8"
    )
    before = FeedStore(store_path).load()["i1"]

    code = cmd_repair(_raw(store_path, tmp_path), apply=True)
    after = FeedStore(store_path).load()["i1"]

    assert code == 0
    assert after.acted_action == VERB_DONE
    assert after.state == STATE_ACTED == before.state
    assert after.acted_at == before.acted_at


def test_nothing_is_revived(tmp_path: Path):
    """No event this command writes may carry a non-terminal state."""
    store_path = tmp_path / "feed.jsonl"
    _write_store(store_path, [_acted_item("i1")])
    (tmp_path / "talker.log").write_text(
        _line(BASE_TS, "feed.act.slot.done", id="i1"), encoding="utf-8"
    )
    lines_before = store_path.read_text(encoding="utf-8").count("\n")

    cmd_repair(_raw(store_path, tmp_path), apply=True)

    appended = store_path.read_text(encoding="utf-8").splitlines()[lines_before:]
    assert appended, "the apply wrote nothing — this pin would be vacuous"
    for raw_line in appended:
        event = json.loads(raw_line)
        assert event["ev"] == "state"
        assert event["state"] == STATE_ACTED


def test_second_run_finds_nothing_to_do(tmp_path: Path, capsys):
    """Idempotency: the applied verbs satisfy the candidate predicate."""
    store_path = tmp_path / "feed.jsonl"
    _write_store(store_path, [_acted_item("i1")])
    (tmp_path / "talker.log").write_text(
        _line(BASE_TS, "feed.act.slot.done", id="i1"), encoding="utf-8"
    )
    cmd_repair(_raw(store_path, tmp_path), apply=True)
    capsys.readouterr()

    after_first = store_path.read_bytes()
    code = cmd_repair(_raw(store_path, tmp_path), apply=True)

    assert code == 0
    assert store_path.read_bytes() == after_first
    assert "Nothing to repair" in capsys.readouterr().out


def test_absent_store_says_so_and_exits_clean(tmp_path: Path, capsys):
    """ILB. Hypatia and VERA have no store file — steady state, not a fault."""
    store_path = tmp_path / "does_not_exist.jsonl"

    with structlog.testing.capture_logs() as captured:
        code = cmd_repair(_raw(store_path, tmp_path), apply=True)

    assert code == 0
    out = capsys.readouterr().out
    assert "No feed store on this instance" in out

    events = [c for c in captured if c.get("event") == "feed.repair.no_store"]
    assert len(events) == 1
    assert events[0]["path"] == str(store_path)


def test_plan_log_event_carries_its_counts(tmp_path: Path):
    """Log-emission pin: the operator's grep surface, with its fields."""
    store_path = tmp_path / "feed.jsonl"
    _write_store(store_path, [_acted_item("i1")])
    (tmp_path / "talker.log").write_text(
        _line(BASE_TS, "feed.act.slot.done", id="i1"), encoding="utf-8"
    )

    with structlog.testing.capture_logs() as captured:
        cmd_repair(_raw(store_path, tmp_path), apply=False)

    events = [c for c in captured if c.get("event") == "feed.repair.plan"]
    assert len(events) == 1
    assert events[0]["candidates"] == 1
    assert events[0]["backfills"] == 1
    assert events[0]["applied"] == 0
    assert events[0]["dry_run"] is True


def test_join_refusal_exits_one_and_writes_nothing(tmp_path: Path, capsys):
    store_path = tmp_path / "feed.jsonl"
    _write_store(store_path, [_acted_item("far")])
    (tmp_path / "talker.log").write_text(
        _line("2026-08-01T10:00:05.000000", "feed.act.slot.done", id="far"),
        encoding="utf-8",
    )
    before = store_path.read_bytes()

    code = cmd_repair(_raw(store_path, tmp_path), apply=True)

    assert code == 1
    assert store_path.read_bytes() == before
    assert "REFUSED" in capsys.readouterr().out


def test_retirement_applies_as_legacy_acted_plus_verb(tmp_path: Path):
    """Post-PY-C the fold reads legacy ``acted`` + ``action=retired``. The
    repair records the REASON; it does not restate the history as
    ``state=retired``."""
    store_path = tmp_path / "feed.jsonl"
    _write_store(store_path, [_acted_item("ret", kind="task")])
    (tmp_path / "daily_sync.log").write_text(
        _line(BASE_TS, "feed.reconcile", ok="True", kind="task", open=2, acted=1),
        encoding="utf-8",
    )

    cmd_repair(_raw(store_path, tmp_path), apply=True)
    after = FeedStore(store_path).load()["ret"]

    assert after.state == STATE_ACTED
    assert after.acted_action == ACTION_RETIRED
