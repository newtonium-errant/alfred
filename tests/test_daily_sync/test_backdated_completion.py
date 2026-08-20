"""Backdated completion — the operator's 2026-08-20 report, driven end-to-end.

His words: "I added this to my tasks yesterday but forgot to mark it complete.
Now it comes back as a Duty task today because it's due. There's no way of
saying I did it already... I don't want to add it and then complete it today
because I didn't do it today." His ruling: yesterday is the default
'previously done' option, further backdating as needed.

The pins here drive the REAL producer → REAL act router → REAL completion
writer → REAL projection (no writer mocks, the sibling file's discipline):

  * THE OPERATOR-SCENARIO PIN — a weekly duty due yesterday, unlogged,
    surfaces T1 (positive control); ``done_1d`` writes YESTERDAY's date, no
    today-dated completion exists anywhere, and ``compute_today_view``
    re-run shows the due-state CLEARED. Both sides of the projection driven.
  * Quick-complete regression — plain ``done`` still writes today, same
    detail string, same acted verb.
  * The bound at the door — a rung beyond the item's credit window is
    refused with the NAMED reason (``beyond_window``) and an untouched
    record, while a nearer rung on the SAME item lands (the exclusion pin's
    positive control). The serve side excludes the same rung it refuses.
  * Lane guard — task-lane backdates refuse ``unsupported_lane``.
  * Accept-then-backdate — the folded-state exception admits the whole done
    family, so the operator's literal two-step flow reaches the writer.
  * Undo aims at the CHOSEN date, not today.
  * Snooze-after-backdated-done refuses (family membership, not
    ``== "done"``).
  * The when-ruling capture — backdated acts append the correction row
    (proposed=today, chosen=the date); plain dones append nothing (v1
    capture decision, pinned so widening it is deliberate).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
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
    actions_for_item,
)
from alfred.daily_sync.config import DailySyncConfig
from alfred.feed import FeedStore
from alfred.feed.model import STATE_ACTED, STATE_OPEN, FeedItem
from alfred.routine.when_corrections import when_corrections_path_for
from alfred.tier.compute import compute_today_view

# Reference instant — Wednesday 2026-07-22 13:00 UTC (the sibling file's day).
# A weekly-Tuesday duty is then due YESTERDAY: the operator's shape exactly.
NOW = datetime(2026, 7, 22, 13, 0, 0, tzinfo=timezone.utc)
TODAY = date(2026, 7, 22)
TODAY_ISO = "2026-07-22"
YESTERDAY_ISO = "2026-07-21"


@pytest.fixture(autouse=True)
def _pin_router_today(monkeypatch):
    """Pin the router's completion date to the fixture day (the sibling
    file's seam — production derives both sides from one clock)."""
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


def _garbage_day(vault: Path) -> Path:
    """The screenshot card's shape: a routine item with a weekly due_pattern
    that was due YESTERDAY (Tuesday), escalate_at_days 0 → T1 duty today."""
    p = vault / "routine" / "Waste.md"
    p.write_text(
        "---\n"
        "type: routine\n"
        "status: active\n"
        "name: Waste\n"
        "cadence:\n  type: weekly\n  days: [Tue]\n"
        "items:\n"
        "- text: Garbage Day\n"
        "  due_pattern:\n    type: weekly\n    day: tue\n"
        "  escalate_at_days: 0\n"
        "---\n\n# Waste\n",
        encoding="utf-8",
    )
    return p


def _short_cycle_item(vault: Path) -> Path:
    """every_n_days n=2, due yesterday — half-cycle 1, so the credit window is
    [today-2 .. yesterday]: ``done_3d`` is beyond it while ``done_1d`` is
    inside. The narrowest real grammar, for the refusal pins."""
    anchor = (TODAY - timedelta(days=15)).isoformat()
    p = vault / "routine" / "Meds.md"
    p.write_text(
        "---\n"
        "type: routine\n"
        "status: active\n"
        "name: Meds\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Refill tray\n"
        "  due_pattern:\n    type: every_n_days\n    n: 2\n"
        f"    anchor: {anchor}\n"
        "  escalate_at_days: 0\n"
        "---\n\n# Meds\n",
        encoding="utf-8",
    )
    return p


def _task_due_today(vault: Path, *, name: str = "Interview") -> Path:
    p = vault / "task" / f"{name}.md"
    p.write_text(
        f"---\ntype: task\nstatus: todo\nname: {name}\ndue: {TODAY_ISO}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return p


def _publish(store: FeedStore, vault: Path, *, item_text: str | None = None,
             origin: str | None = None) -> FeedItem:
    """Run the REAL producer and upsert the wanted slot item."""
    items = slot_suggestion_feed_items(vault, NOW, None, instance="salem") or []
    for it in items:
        ev = it.evidence
        if item_text is not None and ev.get("item_text") == item_text:
            store.upsert(it)
            return it
        if origin == "task" and ev.get("origin") == "task":
            store.upsert(it)
            return it
    raise AssertionError(
        f"no slot item for {item_text or origin!r} in {[i.evidence for i in items]}"
    )


def _act(store: FeedStore, cfg: DailySyncConfig, vault: Path, fid: str, action: str) -> Any:
    return act(
        fid, action,
        feed_store=store, config=cfg, vault_path=vault,
        instance_name="salem", instance_scope="talker", raw_config=None,
    )


def _completion_log(record: Path) -> dict:
    return dict(frontmatter.load(str(record)).metadata.get("completion_log") or {})


def _view_names(vault: Path) -> list[str]:
    """Every entry name across all three lanes of the CURRENT projection."""
    view = compute_today_view(vault, NOW, None)
    return [e.name for e in (*view.t1, *view.t2, *view.t3)]


# ---------------------------------------------------------------------------
# THE OPERATOR-SCENARIO PIN — both sides of the projection, driven
# ---------------------------------------------------------------------------


def test_operator_scenario_backdate_pays_yesterdays_window_and_today_clears(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    record = _garbage_day(vault)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)

    # BEFORE — the debt is real: the duty surfaces T1 (the positive control
    # that this projection can produce the operator's card at all).
    assert "Garbage Day" in _view_names(vault)

    item = _publish(store, vault, item_text="Garbage Day")
    assert item.evidence["backdate_limit_days"] == 4  # weekly: eff-3 .. eff, eff=yesterday

    with structlog.testing.capture_logs() as cap:
        res = _act(store, cfg, vault, item.id, "done_1d")

    assert res.ok and res.status == STATUS_ACTED
    # ILB: the confirmation NAMES the chosen day.
    assert res.detail == "marked done: Garbage Day — yesterday"

    # The record carries the CHOSEN date — and NO completion dated today
    # exists anywhere (the record's whole log + the daily curation file).
    assert _completion_log(record)["Garbage Day"] == [YESTERDAY_ISO]
    for dates in _completion_log(record).values():
        assert TODAY_ISO not in dates
    daily = vault / "daily" / f"{TODAY_ISO}.md"
    assert (not daily.exists()) or TODAY_ISO not in (daily.read_text(encoding="utf-8"))

    # AFTER — the debt is paid: today's projection no longer surfaces it.
    assert "Garbage Day" not in _view_names(vault)

    # The feed item is decided, stamped with the TRUE verb (undo derives from it).
    stored = store.load()[item.id]
    assert stored.state == STATE_ACTED and stored.acted_action == "done_1d"

    # The named act log carries the date (grep-able outcome).
    done_logs = [c for c in cap if c.get("event") == "feed.act.slot.done"]
    assert len(done_logs) == 1 and done_logs[0]["date"] == YESTERDAY_ISO


def test_quick_complete_is_byte_unchanged_today_semantics(tmp_path: Path) -> None:
    """The regression pin: the plain ✓ still means done TODAY — same date,
    same detail string, same acted verb as before this lane."""
    vault = _vault(tmp_path)
    record = _garbage_day(vault)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, item_text="Garbage Day")

    res = _act(store, cfg, vault, item.id, "done")

    assert res.ok and res.status == STATUS_ACTED
    assert res.detail == "marked done: Garbage Day"
    assert _completion_log(record)["Garbage Day"] == [TODAY_ISO]
    assert store.load()[item.id].acted_action == "done"


# ---------------------------------------------------------------------------
# The bound — served honestly, enforced at the door
# ---------------------------------------------------------------------------


def test_served_when_family_is_grouped_labelled_and_gesture_free(tmp_path: Path) -> None:
    """The wire-shape pin. The when-family arrives grouped + labelled like
    the sort alternates; NO member carries a gesture (a gestured ``done``
    would hijack the deck's slot affirm from Take-it — verbsFromActions takes
    the first gesture match, and ``done`` precedes ``accept`` in ceiling
    order). Positive control: accept KEEPS its affirm gesture."""
    vault = _vault(tmp_path)
    _garbage_day(vault)
    store = _store(tmp_path)
    item = _publish(store, vault, item_text="Garbage Day")

    served = {a["verb"]: a for a in actions_for_item(item.to_dict())}
    family = {v: a for v, a in served.items() if a.get("group") == "when"}
    assert set(family) == {"done", "done_1d", "done_2d", "done_3d"}
    assert family["done"]["label"] == "Today"
    assert family["done_1d"]["label"] == "Yesterday"
    assert family["done_2d"]["label"] == "2 days ago"
    assert family["done_3d"]["label"] == "3 days ago"
    assert all("gesture" not in a for a in family.values())
    assert served["accept"]["gesture"] == "affirm"


def test_rung_beyond_the_stamp_is_not_served(tmp_path: Path) -> None:
    """every_n_days n=2 stamps limit 2 → done_3d is NOT offered (a control
    that would refuse when pressed must not be served). Positive control in
    the same test: the nearer rungs ARE offered on the same item."""
    vault = _vault(tmp_path)
    _short_cycle_item(vault)
    store = _store(tmp_path)
    item = _publish(store, vault, item_text="Refill tray")
    assert item.evidence["backdate_limit_days"] == 2

    verbs = [a["verb"] for a in actions_for_item(item.to_dict())]
    assert "done_1d" in verbs and "done_2d" in verbs
    assert "done_3d" not in verbs


def test_beyond_window_backdate_refused_with_named_reason_and_untouched_record(tmp_path: Path) -> None:
    """The door itself (a stale client / hand-crafted POST bypasses the serve
    filter): the refusal names WHY (``beyond_window``), writes NOTHING, and
    the item stays open. Positive control: ``done_1d`` on the SAME item lands
    — the pin cannot be satisfied by a dead pipeline."""
    vault = _vault(tmp_path)
    record = _short_cycle_item(vault)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, item_text="Refill tray")
    before = record.read_bytes()

    with structlog.testing.capture_logs() as cap:
        res = _act(store, cfg, vault, item.id, "done_3d")

    assert res.ok is False and res.status == STATUS_INVALID_ACTION
    assert "outside the window" in res.detail
    assert record.read_bytes() == before  # untouched — bytes, not just fields
    assert store.load()[item.id].state == STATE_OPEN
    refusals = [c for c in cap if c.get("event") == "feed.act.slot.backdate_refused"]
    assert len(refusals) == 1 and refusals[0]["reason"] == "beyond_window"

    # Positive control — the nearest admissible neighbour is accepted.
    ok = _act(store, cfg, vault, item.id, "done_1d")
    assert ok.ok and _completion_log(record)["Refill tray"] == [YESTERDAY_ISO]


def test_task_lane_backdate_refused_as_unsupported(tmp_path: Path) -> None:
    """No recurrence grammar, no window, no guess: the task lane refuses the
    rung by NAME (``unsupported_lane``) and the task record is untouched.
    Positive control: the plain ``done`` on the same item still lands."""
    vault = _vault(tmp_path)
    task = _task_due_today(vault)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, origin="task")
    assert item.evidence["backdate_limit_days"] == 0
    before = task.read_bytes()

    with structlog.testing.capture_logs() as cap:
        res = _act(store, cfg, vault, item.id, "done_1d")

    assert res.ok is False and res.status == STATUS_UNSUPPORTED_ITEM
    assert task.read_bytes() == before
    refusals = [c for c in cap if c.get("event") == "feed.act.slot.backdate_refused"]
    assert len(refusals) == 1 and refusals[0]["reason"] == "unsupported_lane"

    ok = _act(store, cfg, vault, item.id, "done")
    assert ok.ok and ok.status == STATUS_ACTED


# ---------------------------------------------------------------------------
# The operator's literal flow — accept this morning, then "I did it yesterday"
# ---------------------------------------------------------------------------


def test_accept_then_backdate_reaches_the_writer(tmp_path: Path) -> None:
    """An accepted item is state=acted / acted_action=accept. The folded-state
    exception must admit the WHOLE done family — under the old
    ``== DONE_ACTION`` comparison this act would be swallowed as
    already_acted, re-opening the exact friction the exception was carved
    for, wearing a date."""
    vault = _vault(tmp_path)
    record = _garbage_day(vault)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, item_text="Garbage Day")

    accepted = _act(store, cfg, vault, item.id, "accept")
    assert accepted.ok, accepted.detail
    assert store.load()[item.id].acted_action == "accept"

    res = _act(store, cfg, vault, item.id, "done_1d")

    assert res.ok and res.status == STATUS_ACTED
    assert _completion_log(record)["Garbage Day"] == [YESTERDAY_ISO]
    assert store.load()[item.id].acted_action == "done_1d"


def test_undo_of_a_backdated_completion_removes_the_chosen_date(tmp_path: Path) -> None:
    """Undo aims at the date the acted verb wrote — today's date would find
    nothing logged, answer ok, and leave the record still satisfied: an undo
    that silently undoes nothing."""
    vault = _vault(tmp_path)
    record = _garbage_day(vault)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, item_text="Garbage Day")

    _act(store, cfg, vault, item.id, "done_1d")
    undo = _act(store, cfg, vault, item.id, "undo_done")

    assert undo.ok and undo.status == STATUS_UNDONE
    assert undo.detail == f"undone: Garbage Day ({YESTERDAY_ISO})"
    assert _completion_log(record)["Garbage Day"] == []  # the chosen date is GONE
    assert store.load()[item.id].state == STATE_OPEN


def test_snooze_refuses_on_a_backdated_done_item(tmp_path: Path) -> None:
    """The snooze guard reads the FAMILY, not ``== "done"``: an item completed
    via a rung is exactly as finished, and parking it would be the
    accepted-then-ignored failure. Driven at the dispatcher with the fresh
    post-act shape (evidence.done still False — the producer hasn't re-emitted
    yet), which is precisely the shape only the acted-verb check can catch."""
    from alfred.daily_sync.action_router import STATUS_ALREADY_DONE, _dispatch_slot_snooze

    class _Item:
        evidence = {"origin": "routine_item", "routine_record": "Waste",
                    "item_text": "Garbage Day", "done": False}
        acted_action = "done_1d"

    class _FeedStore:
        def set_state(self, *a: Any, **k: Any) -> None:
            raise AssertionError("a refused snooze must write nothing")

    # A REAL snooze store config: the dispatcher resolves the store path
    # BEFORE the done refusal, so an unresolvable config would exit early with
    # not_configured and this pin would pass against a build with NO family
    # check at all (the refusal-for-the-wrong-reason trap).
    import yaml as _yaml

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        _yaml.safe_dump({"tier": {"snooze": {"path": str(tmp_path / "snooze.json")}}}),
        encoding="utf-8",
    )

    class _Cfg:
        config_path = str(cfg_file)

    with structlog.testing.capture_logs() as cap:
        res = _dispatch_slot_snooze(
            "slot_suggestion:routine:Waste::Garbage Day", "snooze_1d", _Item(),
            feed_store=_FeedStore(), config=_Cfg(),
        )

    assert res.ok is False and res.status == STATUS_ALREADY_DONE
    assert [c for c in cap if c.get("event") == "board.snooze.refused_already_done"]


# ---------------------------------------------------------------------------
# The when-ruling capture — the self-correcting signal
# ---------------------------------------------------------------------------


def test_backdated_act_appends_the_when_ruling_with_the_chosen_date(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _garbage_day(vault)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, item_text="Garbage Day")

    _act(store, cfg, vault, item.id, "done_1d")

    sidecar = when_corrections_path_for(store.path)
    rows = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    row = rows[0]
    assert row["proposed"] == TODAY_ISO and row["chosen"] == YESTERDAY_ISO
    assert row["confirmed"] is False
    assert row["item"] == "Garbage Day" and row["record"] == "Waste"
    assert row["shape"] == "weekly" and row["id"] == item.id


def test_plain_done_appends_no_when_ruling(tmp_path: Path) -> None:
    """The v1 capture decision, PINNED so widening it is a deliberate act:
    only a non-default when is a correction signal; the default answer is not
    recorded (the store's denominator question is a ledgered follow-up)."""
    vault = _vault(tmp_path)
    _garbage_day(vault)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, item_text="Garbage Day")

    _act(store, cfg, vault, item.id, "done")

    assert not when_corrections_path_for(store.path).exists()


def test_re_backdate_after_acted_is_folded_and_captures_nothing_new(tmp_path: Path) -> None:
    """A second ``done_1d`` on the decided item folds to already_acted — no
    duplicate log date, no duplicate when-ruling row."""
    vault = _vault(tmp_path)
    record = _garbage_day(vault)
    store, cfg = _store(tmp_path), _ds_config(tmp_path)
    item = _publish(store, vault, item_text="Garbage Day")

    first = _act(store, cfg, vault, item.id, "done_1d")
    second = _act(store, cfg, vault, item.id, "done_1d")

    assert first.ok and second.ok
    assert second.status == STATUS_ALREADY_ACTED
    assert _completion_log(record)["Garbage Day"] == [YESTERDAY_ISO]
    rows = when_corrections_path_for(store.path).read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
