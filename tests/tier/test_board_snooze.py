"""Board snooze (R3) — store, breakthrough predicate, projection filter.

Operator screenshot 2026-08-03: board rows offered only ✓ DONE, so parking a
row the operator wasn't doing today meant claiming it was finished.

The constraint that shapes every pin here: **a snooze must never fake a
completion.** It writes nothing to the vault.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import structlog

from alfred.tier import snooze as sn


TODAY = date(2026, 8, 3)


class _Entry:
    """Minimal stand-in for a TierEntry (the projection only reads these)."""

    def __init__(self, *, origin="task", name="Pay Steph", path="task/Pay Steph.md",
                 routine_record=None, item_text=None, due_iso=""):
        self.origin = origin
        self.name = name
        self.path = path
        self.routine_record = routine_record
        self.item_text = item_text
        self.due_iso = due_iso


def _snoozed(store: Path, entry: _Entry, *, days=3, today=TODAY) -> sn.SnoozeEntry:
    return sn.add_snooze(
        store, sn.slot_stable_key(entry), days=days, today=today,
        lane="task", due_iso=entry.due_iso,
    )


# --- store round-trip ---------------------------------------------------------


def test_store_round_trips_and_is_schema_tolerant(tmp_path: Path) -> None:
    store = tmp_path / "snooze.json"
    e = _Entry(due_iso="2026-08-20")
    _snoozed(store, e)
    loaded = sn.load_snoozes(store)
    assert list(loaded) == ["task:task/Pay Steph.md"]
    assert loaded["task:task/Pay Steph.md"].snoozed_until == "2026-08-06"

    # An unknown field from a newer writer must not crash the loader.
    raw = json.loads(store.read_text(encoding="utf-8"))
    raw["task:task/Pay Steph.md"]["future_field"] = "ignored"
    store.write_text(json.dumps(raw), encoding="utf-8")
    assert sn.load_snoozes(store)["task:task/Pay Steph.md"].lane == "task"


def test_corrupt_store_degrades_to_unsnoozed_with_a_log(tmp_path: Path) -> None:
    """A broken store must never take the board down — it renders unsnoozed,
    and says so (ILB)."""
    store = tmp_path / "snooze.json"
    store.write_text("{ not json", encoding="utf-8")
    with structlog.testing.capture_logs() as cap:
        assert sn.load_snoozes(store) == {}
    assert [c for c in cap if c.get("event") == "board.snooze.store_read_failed"]


def test_absent_store_is_empty_not_an_error(tmp_path: Path) -> None:
    assert sn.load_snoozes(tmp_path / "nope.json") == {}
    assert sn.load_snoozes(None) == {}


# --- boundary -----------------------------------------------------------------


def test_snooze_holds_through_its_window_and_returns_on_the_boundary_day(
    tmp_path: Path,
) -> None:
    """3d on the 3rd hides the 3rd/4th/5th and the row is BACK on the 6th.

    The off-by-one is the likely bug in this feature, so both sides are pinned.
    Mutation: flip ``>=`` to ``>`` in is_snoozed → the boundary day fails."""
    store = tmp_path / "snooze.json"
    entry = _snoozed(store, _Entry(), days=3)
    for day, expected in (
        (date(2026, 8, 3), True),
        (date(2026, 8, 4), True),
        (date(2026, 8, 5), True),
        (date(2026, 8, 6), False),   # boundary — back on the board
        (date(2026, 8, 7), False),
    ):
        suppressed, _ = sn.is_snoozed(entry, today=day)
        assert suppressed is expected, f"{day} expected suppressed={expected}"


def test_the_three_v1_durations(tmp_path: Path) -> None:
    assert sn.SNOOZE_DURATIONS == {"snooze_1d": 1, "snooze_3d": 3, "snooze_7d": 7}
    store = tmp_path / "snooze.json"
    for action, days in sn.SNOOZE_DURATIONS.items():
        e = _Entry(name=action, path=f"task/{action}.md")
        stored = sn.add_snooze(
            store, sn.slot_stable_key(e), days=days, today=TODAY,
            duration_label=action.removeprefix("snooze_"),
        )
        assert stored.snoozed_until == (date(2026, 8, 3 + days)).isoformat()


# --- the breakthrough table (all four rows) -----------------------------------


def test_breakthrough_row1_nothing_changed_holds(tmp_path: Path) -> None:
    store = tmp_path / "snooze.json"
    e = _Entry(due_iso="2026-08-20")
    entry = _snoozed(store, e)
    suppressed, reason = sn.is_snoozed(
        entry, today=date(2026, 8, 4), current_due_iso="2026-08-20",
    )
    assert (suppressed, reason) == (True, None)


def test_breakthrough_row2_crossing_into_due_breaks_through(tmp_path: Path) -> None:
    """Not-due at snooze time, due/overdue later → genuinely new information."""
    store = tmp_path / "snooze.json"
    e = _Entry(due_iso="2026-08-05")
    entry = _snoozed(store, e, days=7)
    assert entry.overdue_at_snooze is False
    suppressed, reason = sn.is_snoozed(
        entry, today=date(2026, 8, 5), current_due_iso="2026-08-05",
    )
    assert (suppressed, reason) == (False, sn.REASON_CROSSED_DUE)


def test_breakthrough_row3_due_moving_earlier_breaks_through(tmp_path: Path) -> None:
    store = tmp_path / "snooze.json"
    e = _Entry(due_iso="2026-08-20")
    entry = _snoozed(store, e, days=7)
    suppressed, reason = sn.is_snoozed(
        entry, today=date(2026, 8, 4), current_due_iso="2026-08-11",
    )
    assert (suppressed, reason) == (False, sn.REASON_MOVED_EARLIER)


def test_breakthrough_row4_due_moving_later_holds(tmp_path: Path) -> None:
    store = tmp_path / "snooze.json"
    e = _Entry(due_iso="2026-08-20")
    entry = _snoozed(store, e, days=7)
    suppressed, reason = sn.is_snoozed(
        entry, today=date(2026, 8, 4), current_due_iso="2026-09-01",
    )
    assert (suppressed, reason) == (True, None)


def test_breakthrough_row5_already_overdue_at_snooze_HOLDS(tmp_path: Path) -> None:
    """THE MOTIVATING CARD. "T1 RRTS Invoicing overdue-by-1d" was already
    overdue when the operator wanted it gone. Under an ABSOLUTE "is it
    overdue?" predicate the snooze would be a no-op — tap 3d, back next
    projection — making the feature useless for the exact row that prompted it.

    Snoozing something overdue means "yes, I know it's late, not today."

    Mutation: drop ``and not entry.overdue_at_snooze`` from breakthrough_reason
    → this fails."""
    store = tmp_path / "snooze.json"
    e = _Entry(name="RRTS Invoicing", path="task/RRTS Invoicing.md",
               due_iso="2026-08-02")  # yesterday — overdue by 1d
    entry = _snoozed(store, e, days=3)
    assert entry.overdue_at_snooze is True
    for day in (date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)):
        suppressed, reason = sn.is_snoozed(
            entry, today=day, current_due_iso="2026-08-02",
        )
        assert (suppressed, reason) == (True, None), f"broke through on {day}"


def test_t3_free_text_snooze_is_absolute_by_construction(tmp_path: Path) -> None:
    """T3 has no due-ness, so no delta can fire — not a special case, an
    absence of input."""
    store = tmp_path / "snooze.json"
    e = _Entry(origin="", name="Call the bank", path="", due_iso="")
    entry = _snoozed(store, e, days=3)
    assert sn.slot_stable_key(e) == "text:Call the bank"
    suppressed, reason = sn.is_snoozed(entry, today=date(2026, 8, 4))
    assert (suppressed, reason) == (True, None)


def test_expiry_beats_breakthrough_reporting(tmp_path: Path) -> None:
    """An EXPIRED snooze is simply over — reporting a breakthrough would blame
    a delta for a return the clock caused."""
    store = tmp_path / "snooze.json"
    e = _Entry(due_iso="2026-08-20")
    entry = _snoozed(store, e, days=1)
    suppressed, reason = sn.is_snoozed(
        entry, today=date(2026, 8, 10), current_due_iso="2026-08-01",
    )
    assert (suppressed, reason) == (False, None)


# --- projection filter --------------------------------------------------------


def test_filter_drops_snoozed_and_keeps_the_rest(tmp_path: Path) -> None:
    store = tmp_path / "snooze.json"
    parked = _Entry(name="Pay Steph", path="task/Pay Steph.md")
    active = _Entry(name="File taxes", path="task/File taxes.md")
    _snoozed(store, parked)
    kept, stats = sn.filter_snoozed_entries(
        [parked, active], sn.load_snoozes(store), today=date(2026, 8, 4),
    )
    assert [k.name for k in kept] == ["File taxes"]
    assert stats.suppressed == 1
    assert stats.broke_through == []


def test_filter_reports_which_delta_broke_a_row_through(tmp_path: Path) -> None:
    store = tmp_path / "snooze.json"
    e = _Entry(due_iso="2026-08-20")
    _snoozed(store, e, days=7)
    e.due_iso = "2026-08-11"  # moved earlier
    kept, stats = sn.filter_snoozed_entries(
        [e], sn.load_snoozes(store), today=date(2026, 8, 4),
    )
    assert len(kept) == 1
    assert stats.suppressed == 0
    assert stats.broke_through == [("task:task/Pay Steph.md", sn.REASON_MOVED_EARLIER)]


def test_empty_store_is_a_pure_passthrough(tmp_path: Path) -> None:
    rows = [_Entry(name=f"t{i}", path=f"task/t{i}.md") for i in range(3)]
    kept, stats = sn.filter_snoozed_entries(rows, {}, today=TODAY)
    assert kept == rows
    assert stats.suppressed == 0


# --- unsnooze -----------------------------------------------------------------


def test_unsnooze_restores_immediately(tmp_path: Path) -> None:
    store = tmp_path / "snooze.json"
    e = _Entry()
    _snoozed(store, e)
    key = sn.slot_stable_key(e)
    assert sn.remove_snooze(store, key) is True
    assert sn.load_snoozes(store) == {}
    kept, stats = sn.filter_snoozed_entries(
        [e], sn.load_snoozes(store), today=date(2026, 8, 4),
    )
    assert kept == [e] and stats.suppressed == 0


def test_unsnooze_on_an_unsnoozed_row_is_a_noop(tmp_path: Path) -> None:
    store = tmp_path / "snooze.json"
    assert sn.remove_snooze(store, "task:task/Nothing.md") is False


# --- the never-fakes-completion constraint ------------------------------------


def test_snooze_writes_nothing_to_the_vault(tmp_path: Path) -> None:
    """THE CONSTRAINT. A snooze must leave every vault file byte-identical —
    no status, no completion date, no completion_log, no done_at.

    Guards against a future 'while we're here' vault write in the snooze path.
    """
    vault = tmp_path / "vault"
    (vault / "task").mkdir(parents=True)
    task = vault / "task" / "Pay Steph.md"
    task.write_text(
        "---\ntype: task\nstatus: todo\nname: Pay Steph\ndue: 2026-08-20\n---\n\n# Pay Steph\n",
        encoding="utf-8",
    )
    before = {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}

    _snoozed(tmp_path / "snooze.json", _Entry(due_iso="2026-08-20"))

    after = {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    assert after == before, "a snooze mutated the vault"
    assert "status: todo" in task.read_text(encoding="utf-8")


def test_slot_stable_key_matches_the_feed_producers_key(tmp_path: Path) -> None:
    """DRIFT PIN: the board card's feed id and the snooze store's key must be
    the same string. Two copies of this logic would drift, and a drifted key
    silently snoozes nothing (or the wrong row) — the same read/write key
    mismatch class that cost this round its first day.

    Mutation: give feed_producer its own copy again → this fails."""
    from alfred.brief.feed_producer import _slot_stable_key

    cases = [
        _Entry(origin="task", name="Pay Steph", path="task/Pay Steph.md"),
        _Entry(origin="routine_item", name="Dishes", path="",
               routine_record="Sundays", item_text="Wash dishes"),
        _Entry(origin="", name="Call the bank", path=""),
        _Entry(origin="task", name="No path", path=""),
    ]
    for entry in cases:
        assert sn.slot_stable_key(entry) == _slot_stable_key(entry)


# --- projection wiring (compute_today_view) -----------------------------------


def test_projection_is_inert_when_snooze_path_is_none(tmp_path: Path) -> None:
    """Opt-in: with no snooze_path the view is byte-identical to pre-R3 and the
    ILB count still reports 0 (ran, nothing parked)."""
    from alfred.tier.compute import compute_today_view
    from datetime import datetime, timezone

    vault = tmp_path / "vault"
    vault.mkdir()
    with structlog.testing.capture_logs() as cap:
        view = compute_today_view(vault, datetime(2026, 8, 3, 9, tzinfo=timezone.utc))
    assert view is not None
    computed = [c for c in cap if c.get("event") == "brief.today_view.computed"]
    assert len(computed) == 1
    assert computed[0]["snooze_suppressed"] == 0


def test_projection_reads_the_store_and_reports_it(tmp_path: Path) -> None:
    """The projection must actually LOAD the store it was given — a wiring pin,
    not a logic pin (the filter's own behaviour is covered above).

    Drives compute_today_view with a deliberately CORRUPT store: the read is
    the only thing that can emit ``board.snooze.store_read_failed``, so seeing
    that event proves the projection reached the loader. A snooze_path that was
    accepted and ignored would emit nothing.

    Mutation: drop the ``if snooze_path is not None`` block → this fails."""
    from datetime import datetime, timezone

    from alfred.tier.compute import compute_today_view

    vault = tmp_path / "vault"
    vault.mkdir()
    store = tmp_path / "snooze.json"
    store.write_text("{ not json", encoding="utf-8")

    with structlog.testing.capture_logs() as cap:
        compute_today_view(
            vault, datetime(2026, 8, 3, 9, tzinfo=timezone.utc),
            snooze_path=store,
        )

    assert [c for c in cap if c.get("event") == "board.snooze.store_read_failed"], (
        "compute_today_view never read the snooze store"
    )
    computed = [c for c in cap if c.get("event") == "brief.today_view.computed"]
    assert len(computed) == 1 and computed[0]["snooze_suppressed"] == 0


# --- dispatcher (never-fakes-completion, structurally) ------------------------


def test_snooze_dispatch_imports_no_completion_writer() -> None:
    """STRUCTURAL GUARANTEE: _dispatch_slot_snooze must be unable to reach a
    completion writer, not merely decline to call one.

    Reads the function's own bytecode names — a future edit that reaches for
    mark_task_done / a completion_log append / a done_at write from the snooze
    path trips this immediately.

    Mutation: call any completion writer inside _dispatch_slot_snooze → fails.
    """
    from alfred.daily_sync.action_router import _dispatch_slot_snooze

    names = set(_dispatch_slot_snooze.__code__.co_names)
    for consts in _dispatch_slot_snooze.__code__.co_consts:
        if isinstance(consts, tuple):
            names |= {str(c) for c in consts}
    forbidden = {
        "mark_task_done", "_slot_done", "_slot_undo", "_dispatch_slot_completion",
        "append_completion", "record_completion", "tier_done", "routine_done",
    }
    assert not (names & forbidden), f"snooze path can reach: {names & forbidden}"


def test_snooze_ceiling_admits_exactly_the_ratified_verbs() -> None:
    from alfred.daily_sync.action_router import FEED_ACTIONS

    assert set(FEED_ACTIONS["slot_suggestion"]) == {
        "done", "undo_done", "accept", "snooze_1d", "snooze_3d", "snooze_7d",
        "unsnooze",
    }
