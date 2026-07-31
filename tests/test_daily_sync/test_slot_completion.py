"""Board DONE path — slot_suggestion completion router (Phase C slice 1).

Heavy-gate pins for the board's ``done`` / ``undo_done`` on rings/slot cards.
The slice writes to the vault-adjacent completion stores from the WEB path and
reuses the SAME writers the talker uses (single writer per lane) — so the gates
here run against the REAL writers (no writer mocks), per design §5:

  * Per-lane act → REAL writer:
      - routine-item lane → ``completion.mark_routine_item_done`` appends the
        record's completion_log; the feed item flips ``acted``.
      - free-text T3 lane → ``daily_curation.mark_t3_done`` stamps ``done_at``;
        the feed item flips ``acted``.
      - task-origin → ``unsupported_item`` (v1: no talker task writer exists),
        vault + feed state UNTOUCHED.
      - unknown origin → ``unsupported_item``.
  * Idempotent re-done (one write; second is an ok-noop).
  * Undo cycle (done → undo_done → back to ``open``; the vault flip reverses).
  * Producer emits ``evidence.done`` (from the tier layer's OWN predicate).
  * Optimistic-then-reconciled: after a board done, the two lanes reconcile
    differently — routine self-care stays ``acted`` (suppressed, retained);
    curated T3 revives to ``open`` with ``done=true`` (always re-emitted). Both
    stay undoable (the undo gate is ``acted OR evidence.done``).
  * Identity pins — the CLI (talker) + the board call the SAME writer object,
    and the daily-goal + producer call the SAME done-predicate. Fork any →
    the pin reddens.
  * Concurrency — two DONE taps on one item → one completion, second ok-noop.

Contract-first, no dep-gated skips (``feedback_regression_pin_unconditional``).
"""

from __future__ import annotations

import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter
import pytest
import structlog

from alfred.brief.feed_producer import slot_suggestion_feed_items
from alfred.daily_sync import action_router as arouter
from alfred.daily_sync.action_router import (
    STATUS_ACTED,
    STATUS_ALREADY_ACTED,
    STATUS_INVALID_ACTION,
    STATUS_UNDONE,
    STATUS_UNSUPPORTED_ITEM,
    act,
)
from alfred.daily_sync.config import DailySyncConfig
from alfred.feed import FeedStore, try_feed_reconcile
from alfred.feed.model import STATE_ACTED, STATE_OPEN, FeedItem
from alfred.tier.daily_curation import (
    DailyCuration,
    T3Entry,
    load_daily_curation,
    save_tier_curation,
)

# Reference instant — 2026-07-22 13:00 UTC. ds_config timezone is pinned to UTC
# so the router's ``_today_for`` and the producer's ``now.date()`` agree.
NOW = datetime(2026, 7, 22, 13, 0, 0, tzinfo=timezone.utc)
TODAY = date(2026, 7, 22)
TODAY_ISO = "2026-07-22"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pin_router_today(monkeypatch):
    """Pin the router's completion date to the fixture day (``TODAY``) so it
    aligns with the producer's ``NOW`` — in production both derive from the same
    real clock; the tests decouple them, so we clamp the router side to keep the
    dates deterministic. ``_today_for`` is the seam that exists for exactly this
    (its own tz behaviour is unit-covered separately)."""
    monkeypatch.setattr(arouter, "_today_for", lambda config: TODAY)


def _ds_config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True)
    cfg.state.path = str(tmp_path / "state.json")
    cfg.schedule.timezone = "UTC"
    return cfg


def _store(tmp_path: Path) -> FeedStore:
    return FeedStore(str(tmp_path / "feed.jsonl"))


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "routine").mkdir(parents=True, exist_ok=True)
    (vault / "daily").mkdir(parents=True, exist_ok=True)
    (vault / "task").mkdir(parents=True, exist_ok=True)
    return vault


def _routine_selfcare(vault: Path, *, item: str = "Meditate") -> Path:
    """A self-care routine item — surfaces T3 every day until completed (a
    reliable routine-lane slot regardless of the reference date)."""
    p = vault / "routine" / "Self Care.md"
    p.write_text(
        "---\n"
        "type: routine\n"
        "status: active\n"
        "name: Self Care\n"
        "cadence:\n  type: daily\n"
        f"items:\n- text: {item}\n  priority: aspirational\n  self_care: true\n"
        "---\n\n# Self Care\n",
        encoding="utf-8",
    )
    return p


def _curate_t3(vault: Path, *, item: str = "Rake leaves") -> None:
    save_tier_curation(
        vault, TODAY, DailyCuration(t3=[T3Entry(item=item, source="operator-adhoc")]),
    )


def _task_due_today(vault: Path, *, name: str = "Interview") -> None:
    (vault / "task" / f"{name}.md").write_text(
        f"---\ntype: task\nstatus: todo\nname: {name}\ndue: {TODAY_ISO}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def _slot_items(vault: Path) -> list[FeedItem]:
    return slot_suggestion_feed_items(vault, NOW, None, instance="salem") or []


def _publish(store: FeedStore, vault: Path, *, origin: str, tier: int | None = None) -> FeedItem:
    """Run the REAL producer, find the slot item for the wanted lane, upsert it
    into the store exactly as a fire would, and return it."""
    items = _slot_items(vault)
    for it in items:
        ev = it.evidence
        if origin == "task" and ev.get("origin") == "task":
            store.upsert(it)
            return it
        if origin == "routine" and ev.get("routine_record"):
            store.upsert(it)
            return it
        if origin == "tier" and ev.get("tier") == 3 and not ev.get("routine_record"):
            store.upsert(it)
            return it
    raise AssertionError(f"no slot item for lane {origin!r} in {[i.evidence for i in items]}")


def _act(store: FeedStore, cfg: DailySyncConfig, vault: Path, fid: str, action: str) -> Any:
    return act(
        fid, action,
        feed_store=store, config=cfg, vault_path=vault,
        instance_name="salem", instance_scope="talker", raw_config=None,
    )


def _completion_log(record: Path) -> dict:
    return dict(frontmatter.load(str(record)).metadata.get("completion_log") or {})


# ---------------------------------------------------------------------------
# Producer emits evidence.done
# ---------------------------------------------------------------------------


def test_producer_emits_done_false_when_open(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _routine_selfcare(vault)
    _curate_t3(vault)
    items = _slot_items(vault)
    assert items, "producer should surface the self-care + T3 items"
    for it in items:
        assert it.evidence["done"] is False


def test_producer_emits_done_true_after_vault_completion(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _curate_t3(vault, item="Rake leaves")
    # Complete it in the vault directly (as the talker's tier_done would).
    cur = load_daily_curation(vault, TODAY)
    cur.t3[0].done_at = TODAY_ISO
    save_tier_curation(vault, TODAY, cur)
    items = _slot_items(vault)
    t3 = [it for it in items if it.evidence.get("tier") == 3]
    assert len(t3) == 1
    assert t3[0].evidence["done"] is True  # the ring reads this → green


# ---------------------------------------------------------------------------
# Routine-item lane — REAL writer
# ---------------------------------------------------------------------------


def test_routine_lane_done_writes_completion_log_and_acts(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    record = _routine_selfcare(vault)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, origin="routine")

    res = _act(store, cfg, vault, item.id, "done")

    assert res.ok and res.status == STATUS_ACTED
    assert _completion_log(record)["Meditate"] == [TODAY_ISO]  # REAL write
    assert store.load()[item.id].state == STATE_ACTED  # optimistic green


def test_routine_lane_idempotent_redone(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    record = _routine_selfcare(vault)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, origin="routine")

    first = _act(store, cfg, vault, item.id, "done")
    second = _act(store, cfg, vault, item.id, "done")

    assert first.ok and second.ok
    # Second is the folded-state ok-noop (item already acted).
    assert second.status == STATUS_ALREADY_ACTED
    assert _completion_log(record)["Meditate"] == [TODAY_ISO]  # exactly ONE date


def test_routine_lane_undo_cycle(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    record = _routine_selfcare(vault)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, origin="routine")

    _act(store, cfg, vault, item.id, "done")
    undo = _act(store, cfg, vault, item.id, "undo_done")

    assert undo.ok and undo.status == STATUS_UNDONE
    assert _completion_log(record)["Meditate"] == []  # date removed (key kept)
    assert store.load()[item.id].state == STATE_OPEN  # ring back to amber


# ---------------------------------------------------------------------------
# Free-text T3 lane — REAL writer
# ---------------------------------------------------------------------------


def test_tier_lane_done_sets_done_at_and_acts(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _curate_t3(vault, item="Rake leaves")
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, origin="tier")

    res = _act(store, cfg, vault, item.id, "done")

    assert res.ok and res.status == STATUS_ACTED
    cur = load_daily_curation(vault, TODAY)
    assert cur.t3[0].done_at == TODAY_ISO  # REAL write
    assert store.load()[item.id].state == STATE_ACTED


def test_tier_lane_undo_clears_done_at(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _curate_t3(vault, item="Rake leaves")
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, origin="tier")

    _act(store, cfg, vault, item.id, "done")
    undo = _act(store, cfg, vault, item.id, "undo_done")

    assert undo.ok and undo.status == STATUS_UNDONE
    cur = load_daily_curation(vault, TODAY)
    assert cur.t3[0].done_at is None  # cleared
    assert store.load()[item.id].state == STATE_OPEN


# ---------------------------------------------------------------------------
# Unsupported lanes — honest disable, no guessed write
# ---------------------------------------------------------------------------


def test_task_origin_is_unsupported_and_leaves_vault_untouched(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _task_due_today(vault, name="Interview")
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, origin="task")
    task_before = (vault / "task" / "Interview.md").read_text(encoding="utf-8")

    res = _act(store, cfg, vault, item.id, "done")

    assert not res.ok and res.status == STATUS_UNSUPPORTED_ITEM
    assert (vault / "task" / "Interview.md").read_text(encoding="utf-8") == task_before
    assert store.load()[item.id].state == STATE_OPEN  # never acted


def test_unknown_origin_is_unsupported(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    # Craft a slot item with an origin the matrix doesn't route (no
    # routine_record, tier != 3) — the honest unsupported dead-end.
    crafted = FeedItem.create(
        kind="slot_suggestion",
        stable_key="text:Mystery",
        instance="salem",
        title="T1: Mystery",
        evidence={"tier": 1, "origin": "", "name": "Mystery", "item_text": "Mystery"},
        source_ref={"producer": "brief"},
    )
    store.upsert(crafted)

    res = _act(store, cfg, vault, crafted.id, "done")

    assert not res.ok and res.status == STATUS_UNSUPPORTED_ITEM
    assert store.load()[crafted.id].state == STATE_OPEN


def test_undo_on_not_done_item_is_invalid_action(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _routine_selfcare(vault)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, origin="routine")  # open, done=false, never acted

    res = _act(store, cfg, vault, item.id, "undo_done")

    assert not res.ok and res.status == STATUS_INVALID_ACTION


# ---------------------------------------------------------------------------
# Optimistic-then-reconciled: the two lanes reconcile differently, both undoable
# ---------------------------------------------------------------------------


def test_routine_done_survives_reconcile_as_acted_no_duplicate(tmp_path: Path) -> None:
    """A board-completed self-care routine item is SUPPRESSED by compute next
    fire — but the store retains it as ``acted`` (no vanish, no revive, no
    duplicate)."""
    vault = _vault(tmp_path)
    _routine_selfcare(vault)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, origin="routine")
    _act(store, cfg, vault, item.id, "done")

    # Next fire: producer no longer surfaces the completed item → reconcile.
    try_feed_reconcile(store, "slot_suggestion", _slot_items(vault))

    folded = store.load()
    slots = [i for i in folded.values() if i.kind == "slot_suggestion"]
    assert len(slots) == 1  # no duplicate episode
    assert folded[item.id].state == STATE_ACTED  # retained as done


def test_tier_done_revives_open_with_done_true_and_undoable(tmp_path: Path) -> None:
    """A board-completed curated T3 is ALWAYS re-emitted (done=true) — reconcile
    revives it open-with-done=true; undo is still allowed (evidence.done gate)."""
    vault = _vault(tmp_path)
    _curate_t3(vault, item="Rake leaves")
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, origin="tier")
    _act(store, cfg, vault, item.id, "done")

    # Next fire re-emits the done item → reconcile revives it to open+done=true.
    try_feed_reconcile(store, "slot_suggestion", _slot_items(vault))
    revived = store.load()[item.id]
    assert revived.state == STATE_OPEN
    assert revived.evidence["done"] is True  # ring stays green via done, not state

    # Undo still works off the evidence.done gate.
    undo = _act(store, cfg, vault, item.id, "undo_done")
    assert undo.ok and undo.status == STATUS_UNDONE
    assert load_daily_curation(vault, TODAY).t3[0].done_at is None


# ---------------------------------------------------------------------------
# Identity pins — the board and the talker share ONE writer / ONE predicate
# ---------------------------------------------------------------------------


def test_routine_writer_identity_pin_cli_and_board(tmp_path: Path, monkeypatch) -> None:
    """cmd_done (talker path) AND the board's router call the SAME
    ``completion.mark_routine_item_done`` object. Monkeypatch it once → BOTH
    callers route through the spy. Fork either write inline → the spy misses →
    this pin reddens."""
    from alfred.routine import completion as _completion
    from alfred.routine.cli import cmd_done
    from alfred.routine.config import RoutineConfig

    real = _completion.mark_routine_item_done
    seen: list[str] = []

    def _spy(record_path, item_text, date_):
        seen.append(item_text)
        return real(record_path, item_text, date_)

    monkeypatch.setattr(_completion, "mark_routine_item_done", _spy)

    vault = _vault(tmp_path)
    _routine_selfcare(vault)

    # (a) talker path — cmd_done. Complete on a PRIOR day so the self-care item
    # still surfaces for NOW (self-care suppresses only when done TODAY).
    rcfg = RoutineConfig(vault_path=str(vault), instance_name="salem")
    rcfg.state.path = str(tmp_path / "routine_state.json")
    cmd_done(rcfg, "Self Care", "Meditate", today_override="2026-07-20")
    assert seen == ["Meditate"], "cmd_done must route through the shared writer"

    # (b) board path — the router (completes TODAY via the pinned _today_for).
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, origin="routine")
    _act(store, cfg, vault, item.id, "done")
    assert seen == ["Meditate", "Meditate"], "board must route through the SAME writer"


def test_tier_writer_identity_pin_board(tmp_path: Path, monkeypatch) -> None:
    """The board's tier lane calls ``daily_curation.mark_t3_done`` — the SAME
    function ``tier_done`` uses. Monkeypatch the module attr → the board's lazy
    import binds to the spy at call time."""
    import alfred.tier.daily_curation as dc

    real = dc.mark_t3_done
    seen: list[str] = []

    def _spy(vault_path, item, *, completed_at, today):
        seen.append(item)
        return real(vault_path, item, completed_at=completed_at, today=today)

    monkeypatch.setattr(dc, "mark_t3_done", _spy)

    vault = _vault(tmp_path)
    _curate_t3(vault, item="Rake leaves")
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, origin="tier")
    _act(store, cfg, vault, item.id, "done")
    assert seen == ["Rake leaves"]


def test_done_predicate_identity_pin_daily_goal_and_producer(tmp_path: Path, monkeypatch) -> None:
    """The daily-goal count AND the feed producer's ``done`` flag call the SAME
    ``compute.entry_is_done``. Monkeypatch it once → BOTH surfaces reflect it.
    Fork either into a re-derived doneness → the spy misses that caller → red."""
    import alfred.tier.compute as compute

    real = compute.entry_is_done
    calls = {"n": 0}

    def _spy(entry, **kw):
        calls["n"] += 1
        return real(entry, **kw)

    monkeypatch.setattr(compute, "entry_is_done", _spy)

    vault = _vault(tmp_path)
    _routine_selfcare(vault)

    # (a) daily-goal count (via compute_today_view → _compute_daily_goal).
    compute.compute_today_view(vault, NOW, None)
    after_goal = calls["n"]
    assert after_goal >= 1, "daily-goal count must use the shared predicate"

    # (b) the feed producer's done flag.
    _slot_items(vault)
    assert calls["n"] > after_goal, "the producer must use the SAME predicate"


# ---------------------------------------------------------------------------
# Log-emission pins (feedback_log_emission_test_pattern)
# ---------------------------------------------------------------------------


def test_done_emits_named_log(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _routine_selfcare(vault)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, origin="routine")

    with structlog.testing.capture_logs() as cap:
        _act(store, cfg, vault, item.id, "done")

    done = [c for c in cap if c.get("event") == "feed.act.slot.done"]
    assert len(done) == 1
    assert done[0]["lane"] == "routine"
    assert done[0]["item"] == "Meditate"


def test_unsupported_emits_named_log(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _task_due_today(vault)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, origin="task")

    with structlog.testing.capture_logs() as cap:
        _act(store, cfg, vault, item.id, "done")

    unsupported = [c for c in cap if c.get("event") == "feed.act.slot.unsupported_item"]
    assert len(unsupported) == 1
    assert unsupported[0]["origin"] == "task"


# ---------------------------------------------------------------------------
# Concurrency — two DONE taps, one completion, second ok-noop
# ---------------------------------------------------------------------------


def test_two_done_taps_serialize_to_one_write(tmp_path: Path) -> None:
    """Two concurrent DONE taps on the SAME item: the per-item mutex serializes
    them, so the second sees the first's committed ``acted`` state (ok-noop) and
    the completion_log carries exactly ONE date."""
    vault = _vault(tmp_path)
    record = _routine_selfcare(vault)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, origin="routine")

    barrier = threading.Barrier(2)
    results: list[Any] = []
    lock = threading.Lock()

    def _tap() -> None:
        barrier.wait()
        r = _act(store, cfg, vault, item.id, "done")
        with lock:
            results.append(r)

    t1 = threading.Thread(target=_tap)
    t2 = threading.Thread(target=_tap)
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive()

    assert _completion_log(record)["Meditate"] == [TODAY_ISO]  # exactly one date
    assert all(r.ok for r in results)
    assert sorted(r.status for r in results) == [STATUS_ACTED, STATUS_ALREADY_ACTED]
