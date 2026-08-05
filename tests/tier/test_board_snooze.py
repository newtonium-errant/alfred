"""Board snooze (R3) — store, breakthrough predicate, projection filter.

Operator screenshot 2026-08-03: board rows offered only ✓ DONE, so parking a
row the operator wasn't doing today meant claiming it was finished.

The constraint that shapes every pin here: **a snooze must never fake a
completion.** It writes nothing to the vault.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
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


def test_the_duration_ladder(tmp_path: Path) -> None:
    """CONTRACT PIN: three dated rungs + the indefinite one (#14's folded-in
    Park). Widening this ladder is intentional work — updating this pin in the
    same commit is how the widening stays deliberate."""
    assert sn.SNOOZE_DURATIONS == {
        "snooze_1d": 1, "snooze_3d": 3, "snooze_7d": 7,
        "snooze_until_i_say": None,
    }
    assert sn.SNOOZE_INDEFINITE_ACTION == "snooze_until_i_say"
    store = tmp_path / "snooze.json"
    for action, days in sn.SNOOZE_DURATIONS.items():
        e = _Entry(name=action, path=f"task/{action}.md")
        stored = sn.add_snooze(store, sn.slot_stable_key(e), days=days, today=TODAY)
        if days is None:
            assert stored.snoozed_until == ""
            assert stored.duration_label == sn.INDEFINITE_LABEL
        else:
            assert stored.snoozed_until == (date(2026, 8, 3 + days)).isoformat()
            assert stored.duration_label == f"{days}d"


# --- "until I say" (#14 — the indefinite rung) --------------------------------


def test_indefinite_snooze_stores_NO_snoozed_until_key(tmp_path: Path) -> None:
    """The ruling is specific: absence, not a null sentinel and not a far-future
    date. Read the JSON on disk, because that is the artifact the claim is about
    — a dataclass default would look identical here while writing ``null``.

    Mutation: drop the ``payload.pop`` in ``_serialize`` → the key is present
    (as "") and this fails."""
    store = tmp_path / "snooze.json"
    sn.add_snooze(store, "task:task/Pay Steph.md", days=None, today=TODAY)
    raw = json.loads(store.read_text(encoding="utf-8"))
    row = raw["task:task/Pay Steph.md"]
    assert "snoozed_until" not in row, f"expected the key absent, got {row!r}"
    # The other fields still round-trip — the omission is ONE key, not a
    # general falsy-drop (overdue_at_snooze=False is an answer, not an absence).
    assert row["overdue_at_snooze"] is False
    assert row["duration_label"] == sn.INDEFINITE_LABEL


def test_indefinite_entry_survives_the_loader(tmp_path: Path) -> None:
    """The absence must LOAD, not be skipped as malformed.

    ``load_snoozes`` swallows ``TypeError`` per row (a malformed row must not
    take the board down), so a required ``snoozed_until`` would turn every
    indefinite snooze into a silently-dropped row — the item would just come
    back and nothing would say why.

    Mutation: make ``snoozed_until`` required again → the row vanishes and this
    fails on the length assert."""
    store = tmp_path / "snooze.json"
    sn.add_snooze(store, "task:task/Pay Steph.md", days=None, today=TODAY)
    loaded = sn.load_snoozes(store)
    assert list(loaded) == ["task:task/Pay Steph.md"]
    assert loaded["task:task/Pay Steph.md"].snoozed_until == ""


def test_indefinite_snooze_never_expires(tmp_path: Path) -> None:
    """No end date means no clock. Ten years on it is still suppressed."""
    store = tmp_path / "snooze.json"
    entry = sn.add_snooze(store, "text:Write the thing", days=None, today=TODAY)
    for day in (date(2026, 8, 3), date(2026, 8, 4), date(2027, 1, 1), date(2036, 8, 3)):
        suppressed, reason = sn.is_snoozed(entry, today=day)
        assert suppressed is True, f"{day} should still be suppressed"
        assert reason is None


def test_indefinite_snooze_still_breaks_through_on_the_delta(tmp_path: Path) -> None:
    """RULING: breakthrough applies IDENTICALLY to dated and undated snoozes —
    urgency earns a return, not the calendar. An item snoozed "until I say"
    while not yet due comes back when it crosses into due.

    Mutation: return early for undated entries in ``is_snoozed`` (i.e. treat
    "until I say" as absolute) → this fails."""
    store = tmp_path / "snooze.json"
    entry = sn.add_snooze(
        store, "task:task/Pay Steph.md",
        days=None, today=TODAY, due_iso="2026-08-20",
    )
    # Nothing changed and it isn't due yet → holds, with no clock to expire it.
    # (Deliberately a date BEFORE the due date: an undated snooze on a row whose
    # due date is in the past has genuinely crossed into due, and holding it
    # then would be the absolute reading this whole module rejects.)
    suppressed, _ = sn.is_snoozed(
        entry, today=date(2026, 8, 19), current_due_iso="2026-08-20",
    )
    assert suppressed is True
    # Crossed into due → back, with the reason named.
    suppressed, reason = sn.is_snoozed(
        entry, today=date(2026, 8, 20), current_due_iso="2026-08-20",
    )
    assert (suppressed, reason) == (False, sn.REASON_CROSSED_DUE)
    # Moved earlier → back, with the OTHER reason named.
    suppressed, reason = sn.is_snoozed(
        entry, today=date(2026, 8, 10), current_due_iso="2026-08-12",
    )
    assert (suppressed, reason) == (False, sn.REASON_MOVED_EARLIER)


def test_indefinite_snooze_on_an_already_overdue_row_holds(tmp_path: Path) -> None:
    """The motivating card, at the indefinite rung: "yes, I know it's late, not
    today" must be honoured here exactly as it is for a dated snooze — the
    absolute reading would make the indefinite rung a no-op on the one card the
    operator most wants gone."""
    store = tmp_path / "snooze.json"
    entry = sn.add_snooze(
        store, "task:task/RRTS Invoicing.md",
        days=None, today=TODAY, due_iso="2026-08-02",  # already overdue
    )
    assert entry.overdue_at_snooze is True
    suppressed, reason = sn.is_snoozed(
        entry, today=date(2026, 9, 1), current_due_iso="2026-08-02",
    )
    assert (suppressed, reason) == (True, None)


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
        "snooze_until_i_say", "unsnooze",
    }


def test_every_ladder_rung_is_reachable_through_the_ceiling() -> None:
    """The ladder and the ceiling must not drift: a rung the store knows about
    but ``FEED_ACTIONS`` doesn't is a duration the FE can offer and the router
    will reject with ``invalid_action``.

    Derived from :data:`SNOOZE_DURATIONS` rather than re-listed, so adding a
    fifth rung without wiring it trips here instead of in the field."""
    from alfred.daily_sync.action_router import FEED_ACTIONS

    ceiling = set(FEED_ACTIONS["slot_suggestion"])
    missing = set(sn.SNOOZE_DURATIONS) - ceiling
    assert not missing, f"ladder rungs the router can't accept: {sorted(missing)}"
    assert sn.UNSNOOZE_ACTION in ceiling


# --- END-TO-END through a PRODUCTION entry point -------------------------------
#
# The R3 gate BLOCKed on exactly the gap these close: compute_today_view grew a
# snooze_path parameter that NO production caller threaded. Every unit pin was
# green, the write side was live, the read side was dead — a configured
# operator would be told "snoozed until X" and see the row return tomorrow.
# That is accepted-then-ignored, the disease this whole round set out to cure,
# reintroduced inside the cure.
#
# These drive a REAL vault through a REAL caller. A parameter that production
# doesn't thread cannot pass them.


PROD_NOW = datetime(2026, 5, 28, 13, 0, 0, tzinfo=timezone.utc)


def _prod_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "task").mkdir(parents=True, exist_ok=True)
    (vault / "routine").mkdir(parents=True, exist_ok=True)
    (vault / "task" / "Pay Steph.md").write_text(
        "---\ntype: task\nstatus: todo\nname: Pay Steph\ndue: 2026-05-28\n---\n\n# Pay Steph\n",
        encoding="utf-8",
    )
    return vault


def _tier_defaults_with(store: Path | None):
    """A real TierDefaultsConfig carrying the snooze path, exactly as
    brief.config.load_from_unified stamps it."""
    from alfred.routine.config import TierDefaultsConfig

    td = TierDefaultsConfig()
    td.snooze_path = str(store) if store is not None else ""
    return td


def test_e2e_slot_feed_omits_a_snoozed_row(tmp_path: Path) -> None:
    """PRODUCTION ENTRY POINT: slot_suggestion_feed_items — the board's own
    producer, called with the same 3 positionals production uses.

    Mutation: revert compute_today_view to ignore tier_defaults.snooze_path
    (i.e. the state that BLOCKed) → this fails."""
    from alfred.brief.feed_producer import slot_suggestion_feed_items

    vault = _prod_vault(tmp_path)
    store = tmp_path / "snooze.json"

    before = slot_suggestion_feed_items(
        vault, PROD_NOW, _tier_defaults_with(store), instance="salem",
    )
    assert before is not None
    assert "slot_suggestion:task:task/Pay Steph.md" in {i.id for i in before}

    sn.add_snooze(
        store, "task:task/Pay Steph.md",
        days=3, today=PROD_NOW.date(), lane="task", due_iso="2026-05-28",
    )

    after = slot_suggestion_feed_items(
        vault, PROD_NOW, _tier_defaults_with(store), instance="salem",
    )
    assert after is not None
    assert "slot_suggestion:task:task/Pay Steph.md" not in {i.id for i in after}


def test_e2e_expired_snooze_lets_the_row_back_through(tmp_path: Path) -> None:
    """The row comes BACK — a snooze that never expires is a delete.

    Driven with an ALREADY-EXPIRED snooze at the same ``now`` as the other e2e
    pins, rather than advancing the clock: a task due 2026-05-28 doesn't
    auto-surface three days later at all, so a clock-advance version would pass
    or fail for reasons that have nothing to do with snooze. (That was my first
    draft, and it failed for exactly that reason.)"""
    from alfred.brief.feed_producer import slot_suggestion_feed_items

    vault = _prod_vault(tmp_path)
    store = tmp_path / "snooze.json"
    # Snoozed three days BEFORE 'now' for 1 day → expired by the time we look.
    sn.add_snooze(
        store, "task:task/Pay Steph.md",
        days=1, today=date(2026, 5, 25), lane="task", due_iso="2026-05-28",
    )
    assert sn.load_snoozes(store)["task:task/Pay Steph.md"].snoozed_until == "2026-05-26"

    back = slot_suggestion_feed_items(
        vault, PROD_NOW, _tier_defaults_with(store), instance="salem",
    )
    assert back is not None
    assert "slot_suggestion:task:task/Pay Steph.md" in {i.id for i in back}


def test_e2e_indefinite_snooze_omits_the_row_and_unsnooze_restores_it(
    tmp_path: Path,
) -> None:
    """PRODUCTION ENTRY POINT for the indefinite rung. "Until I say" has no
    clock, so the ONLY thing that brings the row back on an unchanged vault is
    the operator saying so — which makes ``unsnooze`` the escape hatch this rung
    depends on rather than a nicety.

    Mutation: give the indefinite entry a far-future ``snoozed_until`` instead
    of an absent one → the omit half still passes (that's the trap), so the
    load-bearing assert here is the one on the stored key."""
    from alfred.brief.feed_producer import slot_suggestion_feed_items

    vault = _prod_vault(tmp_path)
    store = tmp_path / "snooze.json"
    feed_id = "slot_suggestion:task:task/Pay Steph.md"

    sn.add_snooze(
        store, "task:task/Pay Steph.md",
        days=None, today=PROD_NOW.date(), lane="task", due_iso="2026-05-28",
    )
    assert "snoozed_until" not in json.loads(store.read_text(encoding="utf-8"))[
        "task:task/Pay Steph.md"
    ]

    after = slot_suggestion_feed_items(
        vault, PROD_NOW, _tier_defaults_with(store), instance="salem",
    )
    assert after is not None and feed_id not in {i.id for i in after}

    assert sn.remove_snooze(store, "task:task/Pay Steph.md") is True
    back = slot_suggestion_feed_items(
        vault, PROD_NOW, _tier_defaults_with(store), instance="salem",
    )
    assert back is not None and feed_id in {i.id for i in back}


def test_e2e_breakthrough_reason_reaches_the_card_evidence(tmp_path: Path) -> None:
    """PRODUCTION ENTRY POINT: the WHY travels to the card, not just to a log.

    A row returning before its snooze ran out is the one event that provokes
    "why is this back?", and until #14 the answer died in a ``log.info`` the
    producer never saw. This drives the real producer and reads the real
    evidence key the card renders.

    Setup: snoozed 7d on the 25th while NOT yet due (due the 28th), looked at on
    the 28th — the snooze has 4 days left, and the row is back because it
    crossed into due. That is ``crossed_due``.

    Mutation: delete the stamping block in compute_today_view (leaving the
    log.info) → the row still returns, the log line still fires, and this fails
    on the evidence assert. The log alone cannot pass this."""
    from alfred.brief.feed_producer import slot_suggestion_feed_items

    vault = _prod_vault(tmp_path)
    store = tmp_path / "snooze.json"
    sn.add_snooze(
        store, "task:task/Pay Steph.md",
        days=7, today=date(2026, 5, 25), lane="task", due_iso="2026-05-28",
    )
    stored = sn.load_snoozes(store)["task:task/Pay Steph.md"]
    assert stored.snoozed_until == "2026-06-01"      # still snoozed on PROD_NOW
    assert stored.overdue_at_snooze is False         # wasn't overdue when parked

    items = slot_suggestion_feed_items(
        vault, PROD_NOW, _tier_defaults_with(store), instance="salem",
    )
    assert items is not None
    row = next(i for i in items if i.id == "slot_suggestion:task:task/Pay Steph.md")
    assert row.evidence["snooze_breakthrough"] == sn.REASON_CROSSED_DUE


def test_e2e_a_row_that_was_never_snoozed_carries_no_breakthrough_reason(
    tmp_path: Path,
) -> None:
    """The control for the pin above. Every ordinary row must carry an EMPTY
    reason — a stamp that leaks onto un-snoozed rows would put "why is this
    back?" on cards that never went anywhere."""
    from alfred.brief.feed_producer import slot_suggestion_feed_items

    vault = _prod_vault(tmp_path)
    items = slot_suggestion_feed_items(
        vault, PROD_NOW, _tier_defaults_with(tmp_path / "absent.json"),
        instance="salem",
    )
    assert items is not None and items
    assert all(i.evidence["snooze_breakthrough"] == "" for i in items)


def test_projection_logs_the_breakthrough_it_stamps(tmp_path: Path) -> None:
    """The stamp did not REPLACE the log line — an operator grepping
    ``board.snooze_breakthrough`` still finds it, with the reason and the lane.

    Both surfaces matter and they answer different people: the card answers the
    operator looking at it, the log answers the operator asking why the board
    moved last Tuesday."""
    from alfred.tier.compute import compute_today_view

    vault = _prod_vault(tmp_path)
    store = tmp_path / "snooze.json"
    sn.add_snooze(
        store, "task:task/Pay Steph.md",
        days=7, today=date(2026, 5, 25), lane="task", due_iso="2026-05-28",
    )
    with structlog.testing.capture_logs() as cap:
        compute_today_view(vault, PROD_NOW, _tier_defaults_with(store))

    broke = [c for c in cap if c.get("event") == "board.snooze_breakthrough"]
    assert len(broke) == 1
    assert broke[0]["reason"] == sn.REASON_CROSSED_DUE
    assert broke[0]["key"] == "task:task/Pay Steph.md"
    assert broke[0]["lane"] == "t1"


def test_e2e_unconfigured_snooze_path_is_inert(tmp_path: Path) -> None:
    """No tier.snooze.path → the producer is byte-identical to pre-R3."""
    from alfred.brief.feed_producer import slot_suggestion_feed_items

    vault = _prod_vault(tmp_path)
    items = slot_suggestion_feed_items(
        vault, PROD_NOW, _tier_defaults_with(None), instance="salem",
    )
    assert items is not None
    assert "slot_suggestion:task:task/Pay Steph.md" in {i.id for i in items}


def test_e2e_brief_tier_section_also_omits_a_snoozed_row(tmp_path: Path) -> None:
    """SECOND production surface. The board and the brief read ONE projection,
    so a row hidden from the deck must be absent from the rendered brief
    section too — otherwise the operator sees it in the morning brief after
    parking it on the board, which is the divergence the single-projection
    design exists to prevent."""
    from alfred.brief.tier_section import render_tier_section

    vault = _prod_vault(tmp_path)
    store = tmp_path / "snooze.json"

    before = render_tier_section(vault, PROD_NOW, _tier_defaults_with(store))
    assert "Pay Steph" in (before or "")

    sn.add_snooze(
        store, "task:task/Pay Steph.md",
        days=3, today=PROD_NOW.date(), lane="task", due_iso="2026-05-28",
    )
    after = render_tier_section(vault, PROD_NOW, _tier_defaults_with(store))
    assert "Pay Steph" not in (after or "")


def test_config_load_stamps_the_snooze_path_onto_tier_defaults(tmp_path: Path) -> None:
    """THE WIRING PIN: brief config load is the single read-side resolution
    point, and it must actually stamp the path where the projection reads it.

    Mutation: drop the stamp line in brief/config.py → this fails, and so does
    every e2e pin above."""
    from alfred.brief.config import load_from_unified

    cfg = load_from_unified({
        "vault": {"path": str(tmp_path / "vault")},
        "tier": {"snooze": {"path": "/x/board_snooze.json"}},
    })
    assert cfg.tier_defaults.snooze_path == "/x/board_snooze.json"


def test_writer_and_reader_resolve_the_same_key(tmp_path: Path) -> None:
    """Single-source: the dispatcher's resolver and the projection's resolver
    are the same parse, so a snooze can never be written where nothing reads.

    Mutation: give either side its own parse → this fails when they diverge."""
    import yaml as _yaml

    from alfred.daily_sync.action_router import _snooze_store_path

    raw = {"tier": {"snooze": {"path": "/x/board_snooze.json"}}}
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(_yaml.safe_dump(raw), encoding="utf-8")

    class _Cfg:
        config_path = str(cfg_file)

    writer_side = _snooze_store_path(_Cfg())
    reader_side = sn.resolve_snooze_path(raw)
    assert writer_side == reader_side == "/x/board_snooze.json"


def test_e2e_snoozed_task_is_not_offered_back_in_the_t2_selection_pool(
    tmp_path: Path,
) -> None:
    """FOUND BY THE E2E PIN, not by any unit pin.

    The T2 selection pool is a RENDER-ONLY material computed in tier_section,
    not sliced from today_view — so the projection's suppression never reached
    it. A snoozed task correctly left T1 and then reappeared two sections lower
    under "open tasks you might want to add": the system offering back the row
    the operator had just parked. Same broken promise, different section.

    Mutation: drop snoozed_names from _render_t2_selection_pool → this fails.
    """
    from alfred.brief.tier_section import render_tier_section

    vault = _prod_vault(tmp_path)
    store = tmp_path / "snooze.json"
    sn.add_snooze(
        store, "task:task/Pay Steph.md",
        days=3, today=PROD_NOW.date(), lane="task", due_iso="2026-05-28",
    )
    out = render_tier_section(vault, PROD_NOW, _tier_defaults_with(store)) or ""
    assert "### T2 selection pool" in out           # the section still renders
    assert "[[task/Pay Steph]]" not in out          # but not the parked row
    assert "selection pool is empty" in out         # ILB sentinel, not silence


def test_e2e_unsnoozed_task_is_still_offered_in_the_pool(tmp_path: Path) -> None:
    """The pool filter is surgical — only parked rows drop out."""
    from alfred.brief.tier_section import render_tier_section

    vault = _prod_vault(tmp_path)
    (vault / "task" / "File taxes.md").write_text(
        "---\ntype: task\nstatus: todo\nname: File taxes\n---\n\n# File taxes\n",
        encoding="utf-8",
    )
    store = tmp_path / "snooze.json"
    sn.add_snooze(
        store, "task:task/Pay Steph.md",
        days=3, today=PROD_NOW.date(), lane="task", due_iso="2026-05-28",
    )
    out = render_tier_section(vault, PROD_NOW, _tier_defaults_with(store)) or ""
    assert "[[task/File taxes]]" in out
    assert "[[task/Pay Steph]]" not in out


# --- dispatcher-level constraint (the COMPOSITION point) ----------------------


class _FakeStore:
    """Records set_state calls; never touches disk."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def set_state(self, item_id, state, *, action=None):  # noqa: ANN001
        self.calls.append((item_id, state, action))


class _FakeItem:
    def __init__(self, evidence: dict) -> None:
        self.evidence = evidence
        self.acted_action = None


def _dispatch_config(tmp_path: Path, store: Path):
    import yaml as _yaml

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        _yaml.safe_dump({"tier": {"snooze": {"path": str(store)}}}),
        encoding="utf-8",
    )

    class _Cfg:
        config_path = str(cfg_file)
        schedule = None

    return _Cfg()


def test_dispatcher_snooze_leaves_the_vault_byte_identical(tmp_path: Path) -> None:
    """THE COMPOSITION POINT. add_snooze's own byte-identical pin proves the
    STORE writer touches no vault; it does NOT prove the DISPATCHER doesn't —
    the dispatcher could route to a completion writer before or after calling
    it, and every store-level pin would stay green.

    This drives the real dispatcher end-to-end against a real vault. It is also
    the pin that would have caught the read-side BLOCK's sibling: a snooze that
    quietly wrote a completion would pass every unit pin and only show up here.

    Mutation: call any completion writer from _dispatch_slot_snooze → fails.
    """
    from alfred.daily_sync.action_router import _dispatch_slot_snooze

    vault = _prod_vault(tmp_path)
    (vault / "daily").mkdir(exist_ok=True)
    (vault / "daily" / "2026-05-28.md").write_text(
        "---\ntype: daily\n---\n\n# 2026-05-28\n", encoding="utf-8",
    )
    before = {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}

    store = tmp_path / "snooze.json"
    feed_store = _FakeStore()
    result = _dispatch_slot_snooze(
        "slot_suggestion:task:task/Pay Steph.md", "snooze_3d",
        _FakeItem({"origin": "task", "name": "Pay Steph",
                   "path": "task/Pay Steph.md", "due_iso": "2026-05-28"}),
        feed_store=feed_store,
        config=_dispatch_config(tmp_path, store),
    )

    assert result.ok and result.status == "acted"
    after = {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    assert after == before, "the snooze DISPATCHER mutated the vault"
    # It did do its actual job — sidecar written, verb stamped.
    assert "task:task/Pay Steph.md" in sn.load_snoozes(store)
    assert feed_store.calls == [
        ("slot_suggestion:task:task/Pay Steph.md", "acted", "snooze"),
    ]


def test_dispatcher_applies_the_indefinite_rung(tmp_path: Path) -> None:
    """The fourth rung through the REAL dispatcher — the composition the FE
    actually drives, not ``add_snooze`` in isolation.

    Three claims, and the third is the one a "does it write the row?" pin would
    miss: the store row carries no end date, the acted verb is the SAME
    ``snooze`` (so the staged list finds it alongside the dated ones rather than
    needing a second lookup), and the human detail line says what happened
    instead of trailing off after "until"."""
    from alfred.daily_sync.action_router import _dispatch_slot_snooze

    store = tmp_path / "snooze.json"
    feed_store = _FakeStore()
    with structlog.testing.capture_logs() as cap:
        result = _dispatch_slot_snooze(
            "slot_suggestion:task:task/Pay Steph.md", "snooze_until_i_say",
            _FakeItem({"origin": "task", "name": "Pay Steph",
                       "path": "task/Pay Steph.md", "due_iso": "2026-05-28"}),
            feed_store=feed_store,
            config=_dispatch_config(tmp_path, store),
        )

    assert result.ok and result.status == "acted"
    assert result.detail == "snoozed until you say otherwise"
    assert not result.detail.rstrip().endswith("until")
    stored = sn.load_snoozes(store)["task:task/Pay Steph.md"]
    assert stored.snoozed_until == ""
    assert stored.duration_label == sn.INDEFINITE_LABEL
    assert feed_store.calls == [
        ("slot_suggestion:task:task/Pay Steph.md", "acted", "snooze"),
    ]
    logged = [c for c in cap if c.get("event") == "board.snooze"]
    assert len(logged) == 1
    assert logged[0]["indefinite"] is True and logged[0]["until"] == ""


def test_dispatcher_refuses_a_snooze_on_a_done_row_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """Refusal is honest AND inert: no store row, no state change, no vault
    touch. An accepted-then-ignored refusal would be the same broken promise
    in the opposite direction."""
    from alfred.daily_sync.action_router import _dispatch_slot_snooze

    vault = _prod_vault(tmp_path)
    before = {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()}
    store = tmp_path / "snooze.json"
    feed_store = _FakeStore()

    with structlog.testing.capture_logs() as cap:
        result = _dispatch_slot_snooze(
            "slot_suggestion:task:task/Pay Steph.md", "snooze_3d",
            _FakeItem({"origin": "task", "path": "task/Pay Steph.md",
                       "done": True}),
            feed_store=feed_store,
            config=_dispatch_config(tmp_path, store),
        )

    assert result.ok is False
    assert [c for c in cap if c.get("event") == "board.snooze.refused_already_done"]
    assert sn.load_snoozes(store) == {}
    assert feed_store.calls == []
    assert {p: p.read_bytes() for p in vault.rglob("*") if p.is_file()} == before
