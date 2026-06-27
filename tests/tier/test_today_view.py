"""compute_today_view — the unified today view (Step 2b, 2026-06-26).

Pins the keystone of the routine-systems consolidation: ONE computed
read (``compute_today_view``) over the substrate (tasks + routine items
+ curation) that the brief's two sections render as two slices.

Test surface:
  * **Partition correctness** — tasks/routine-items land in the right
    T1/T2/T3 lane via the single ``classify_routine_item`` predicate.
  * **Structural-dedup invariant** — a routine item is in EXACTLY ONE
    of {t1, t2, t3, routine_today}. ``routine_today`` is the complement
    (fired today, no tier handoff).
  * **Daily goal** — the one-of-each-tier balanced-day computation +
    all-T1-done ideal + empty-tier edges.
  * **Curated merge** — operator-curated entries win over auto
    candidates for the same key; auto candidates not curated are
    appended.
  * **Log-emission pin** (``feedback_log_emission_test_pattern`` /
    discipline #9) — ``compute_today_view`` emits exactly one
    ``brief.today_view.computed`` log with the per-lane counts + the
    daily-goal rollup, even on an empty day. Asserts key fields, not
    just the event.

Per ``feedback_regression_pin_unconditional`` — no module-level
``pytest.importorskip``. ``frontmatter`` + ``structlog`` are base deps.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import structlog

from alfred.tier.compute import (
    DailyGoalState,
    RoutineLine,
    TierEntry,
    TodayView,
    compute_today_view,
)

# Reference instant — 2026-05-28 13:00 UTC (Thursday). "Today" =
# 2026-05-28, "tomorrow" = 2026-05-29. Matches tests/tier/test_compute.py.
NOW = datetime(2026, 5, 28, 13, 0, 0, tzinfo=timezone.utc)


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "task").mkdir(parents=True, exist_ok=True)
    (vault / "routine").mkdir(parents=True, exist_ok=True)
    return vault


def _write_task(vault: Path, name: str, fm_body: str) -> None:
    (vault / "task" / f"{name}.md").write_text(
        f"---\n{fm_body}---\n\n# {name}\n", encoding="utf-8",
    )


def _write_routine(vault: Path, name: str, fm_body: str) -> None:
    (vault / "routine" / f"{name}.md").write_text(
        f"---\n{fm_body}---\n\n# {name}\n", encoding="utf-8",
    )


def _write_daily_curation(vault: Path, iso: str, fm_body: str) -> None:
    (vault / "daily").mkdir(parents=True, exist_ok=True)
    (vault / "daily" / f"{iso}.md").write_text(
        f"---\n{fm_body}---\n\n# daily\n", encoding="utf-8",
    )


# --- Dataclass shape -------------------------------------------------------


def test_today_view_is_dataclass_with_lanes() -> None:
    view = TodayView()
    assert view.t1 == []
    assert view.t2 == []
    assert view.t3 == []
    assert view.routine_today == []
    assert isinstance(view.daily_goal, DailyGoalState)


def test_empty_vault_returns_empty_view(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    view = compute_today_view(vault, NOW)
    assert view.t1 == []
    assert view.t2 == []
    assert view.t3 == []
    assert view.routine_today == []
    assert view.daily_goal.balanced_day is False
    # No T1 items → all_t1_done is vacuously True.
    assert view.daily_goal.all_t1_done is True


# --- Partition correctness -------------------------------------------------


def test_task_due_today_lands_in_t1(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _write_task(
        vault, "Pay Steph",
        "type: task\nstatus: todo\nname: Pay Steph\ndue: 2026-05-28\n",
    )
    view = compute_today_view(vault, NOW)
    assert len(view.t1) == 1
    e = view.t1[0]
    assert e.origin == "task"
    assert e.name == "Pay Steph"
    assert e.surface_reason == "due today"
    assert e.source == "auto-due"
    assert e.confirmed is False


def test_routine_item_escalate_window_lands_in_t1(tmp_path: Path) -> None:
    # monthly day=1 → due June 1 = 4 days out; escalate_at_days=5 → T1.
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Bills",
        "type: routine\nstatus: active\nname: Bills\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Pay Rent\n  priority: tracked\n"
        "  due_pattern:\n    type: monthly\n    day: 1\n"
        "  escalate_at_days: 5\n",
    )
    view = compute_today_view(vault, NOW)
    assert len(view.t1) == 1
    e = view.t1[0]
    assert e.origin == "routine_item"
    assert e.routine_record == "Bills"
    assert e.item_text == "Pay Rent"
    assert e.surface_reason == "escalate window (5d before due)"
    assert e.source == "auto-due-routine"


def test_routine_item_surface_window_lands_in_t2(tmp_path: Path) -> None:
    # monthly day=1 → 4 days out; escalate_at_days=0, surface_at_days=5
    # → T2 surface window.
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Bills",
        "type: routine\nstatus: active\nname: Bills\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Pay Rent\n  priority: tracked\n"
        "  due_pattern:\n    type: monthly\n    day: 1\n"
        "  escalate_at_days: 0\n  surface_at_days: 5\n",
    )
    view = compute_today_view(vault, NOW)
    assert view.t1 == []
    assert len(view.t2) == 1
    e = view.t2[0]
    assert e.tier == 2
    assert e.item_text == "Pay Rent"
    assert e.source == "auto-surface-routine"
    assert e.escalation_state is not None  # climb hint present


def test_soft_cadence_overdue_lands_in_t3(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Self Care",
        "type: routine\nstatus: active\nname: Self Care\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n  Walk Fergus:\n  - '2026-05-20'\n"
        "items:\n"
        "- text: Walk Fergus\n  priority: aspirational\n"
        "  target_cadence_days: 3\n",
    )
    view = compute_today_view(vault, NOW)
    assert len(view.t3) == 1
    e = view.t3[0]
    assert e.tier == 3
    assert e.item_text == "Walk Fergus"
    assert e.source == "auto-cadence-routine"


# --- Structural-dedup invariant --------------------------------------------


def test_routine_today_is_complement_of_tier_lanes(tmp_path: Path) -> None:
    """The keystone invariant: a routine item is in EXACTLY ONE of
    {t1, t2, t3, routine_today}. The overdue soft-cadence item goes to
    T3; the plain daily item (no handoff) goes to routine_today; neither
    appears in both."""
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Daily",
        "type: routine\nstatus: active\nname: Daily\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n  Walk Fergus:\n  - '2026-05-20'\n"
        "items:\n"
        "- text: Walk Fergus\n  priority: aspirational\n"
        "  target_cadence_days: 3\n"
        "- text: Do Dishes\n  priority: tracked\n",
    )
    view = compute_today_view(vault, NOW)

    tier_item_texts = {
        e.item_text for e in (view.t1 + view.t2 + view.t3)
        if e.origin == "routine_item"
    }
    routine_today_texts = {r.text for r in view.routine_today}

    # Walk Fergus surfaced to T3 (overdue), Do Dishes stayed in routines.
    assert "Walk Fergus" in tier_item_texts
    assert "Do Dishes" in routine_today_texts
    # No overlap — the complement invariant.
    assert tier_item_texts.isdisjoint(routine_today_texts)


def test_handed_off_item_not_in_routine_today(tmp_path: Path) -> None:
    """A deadline-bearing routine item that classifies into T1 must NOT
    also appear in routine_today (it's handed off, not double-rendered)."""
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Bills",
        "type: routine\nstatus: active\nname: Bills\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Pay Rent\n  priority: tracked\n"
        "  due_pattern:\n    type: weekly\n    day: thu\n"  # due today
        "  escalate_at_days: 1\n",
    )
    view = compute_today_view(vault, NOW)
    assert any(e.item_text == "Pay Rent" for e in view.t1)
    assert all(r.text != "Pay Rent" for r in view.routine_today)


# --- Curated merge ---------------------------------------------------------


def test_curated_t1_task_wins_over_auto(tmp_path: Path) -> None:
    """An operator-curated T1 task entry (confirmed) is authoritative;
    the same task auto-surfacing from its due date does NOT double."""
    vault = _vault(tmp_path)
    _write_task(
        vault, "Pay Steph",
        "type: task\nstatus: todo\nname: Pay Steph\ndue: 2026-05-28\n",
    )
    _write_daily_curation(
        vault, "2026-05-28",
        "type: daily\ndate: '2026-05-28'\n"
        "tier_curation:\n"
        "  t1:\n"
        "  - task: '[[task/Pay Steph]]'\n"
        "    source: operator\n"
        "    confirmed: true\n"
        "  t2: []\n  t3: []\n"
        "  curated_at: '2026-05-28T07:00:00-03:00'\n",
    )
    view = compute_today_view(vault, NOW)
    # Exactly one Pay Steph entry — the curated one (confirmed), not a
    # second auto copy.
    pay_steph = [e for e in view.t1 if e.name == "Pay Steph"]
    assert len(pay_steph) == 1
    assert pay_steph[0].confirmed is True
    assert pay_steph[0].source == "operator"


def test_curated_t3_freetext_appears(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _write_daily_curation(
        vault, "2026-05-28",
        "type: daily\ndate: '2026-05-28'\n"
        "tier_curation:\n"
        "  t1: []\n  t2: []\n"
        "  t3:\n"
        "  - item: Read for an hour\n    source: operator-adhoc\n"
        "  curated_at: '2026-05-28T07:00:00-03:00'\n",
    )
    view = compute_today_view(vault, NOW)
    assert any(e.name == "Read for an hour" for e in view.t3)


# --- Daily goal ------------------------------------------------------------


def test_daily_goal_counts_available_per_lane(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _write_task(
        vault, "Pay Steph",
        "type: task\nstatus: todo\nname: Pay Steph\ndue: 2026-05-28\n",
    )
    _write_routine(
        vault, "Self Care",
        "type: routine\nstatus: active\nname: Self Care\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n  Walk Fergus:\n  - '2026-05-20'\n"
        "items:\n"
        "- text: Walk Fergus\n  priority: aspirational\n"
        "  target_cadence_days: 3\n",
    )
    view = compute_today_view(vault, NOW)
    assert view.daily_goal.t1_available == 1
    assert view.daily_goal.t3_available == 1


def test_daily_goal_balanced_day_when_one_done_each(tmp_path: Path) -> None:
    """balanced_day fires when at least one item is done in EACH of
    T1/T2/T3. Built via curated entries the operator placed AND
    completed: a closed task in T1, a closed task in T2, and a routine
    item completed today in T3."""
    vault = _vault(tmp_path)
    # Closed-today tasks (count as done for the goal).
    _write_task(
        vault, "T1 Done",
        "type: task\nstatus: done\nname: T1 Done\ncompleted: 2026-05-28\n",
    )
    _write_task(
        vault, "T2 Done",
        "type: task\nstatus: done\nname: T2 Done\ncompleted: 2026-05-28\n",
    )
    # Routine item completed today.
    _write_routine(
        vault, "Care",
        "type: routine\nstatus: active\nname: Care\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n  Meditate:\n  - '2026-05-28'\n"
        "items:\n- text: Meditate\n  priority: aspirational\n"
        "  target_cadence_days: 1\n",
    )
    # Curate the two closed tasks into T1/T2 + the routine item into T3.
    _write_daily_curation(
        vault, "2026-05-28",
        "type: daily\ndate: '2026-05-28'\n"
        "tier_curation:\n"
        "  t1:\n  - task: '[[task/T1 Done]]'\n    source: operator\n"
        "    confirmed: true\n"
        "  t2:\n  - task: '[[task/T2 Done]]'\n    source: operator\n"
        "  t3:\n  - item: Meditate\n    source: operator\n"
        "  curated_at: '2026-05-28T07:00:00-03:00'\n",
    )
    view = compute_today_view(vault, NOW)
    assert view.daily_goal.t1_done >= 1
    assert view.daily_goal.t2_done >= 1
    assert view.daily_goal.t3_done >= 1
    assert view.daily_goal.balanced_day is True


def test_daily_goal_all_t1_done_vacuous_when_no_t1(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    view = compute_today_view(vault, NOW)
    assert view.daily_goal.t1_available == 0
    assert view.daily_goal.all_t1_done is True


def test_daily_goal_not_balanced_when_lane_empty(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _write_task(
        vault, "Only T1",
        "type: task\nstatus: todo\nname: Only T1\ndue: 2026-05-28\n",
    )
    view = compute_today_view(vault, NOW)
    # T2 + T3 empty → can't be balanced.
    assert view.daily_goal.balanced_day is False


# --- Log-emission pin (discipline #9) --------------------------------------


def test_compute_today_view_emits_signal_log(tmp_path: Path) -> None:
    """Per feedback_log_emission_test_pattern: compute_today_view emits
    exactly one brief.today_view.computed log carrying the per-lane
    counts + daily-goal rollup. Pins the event AND key fields so a
    refactor that drops the log OR renames a field fails."""
    vault = _vault(tmp_path)
    _write_task(
        vault, "Pay Steph",
        "type: task\nstatus: todo\nname: Pay Steph\ndue: 2026-05-28\n",
    )
    with structlog.testing.capture_logs() as captured:
        compute_today_view(vault, NOW)
    matches = [
        c for c in captured
        if c.get("event") == "brief.today_view.computed"
    ]
    assert len(matches) == 1, (
        "compute_today_view must emit exactly one "
        "brief.today_view.computed log (idle-distinguishable-from-broken)."
    )
    rec = matches[0]
    # Field-shape pin — catches renames/drops, not just full-event drop.
    for fld in (
        "t1_count", "t2_count", "t3_count", "routine_today_count",
        "balanced_day", "all_t1_done", "t1_done", "t2_done", "t3_done",
        "curation_loaded",
    ):
        assert fld in rec, f"log missing field {fld!r}"
    assert rec["t1_count"] == 1


def test_compute_today_view_emits_signal_on_empty_day(tmp_path: Path) -> None:
    """The signal fires even on an empty day — idle is distinguishable
    from broken (intentionally-left-blank)."""
    vault = _vault(tmp_path)
    with structlog.testing.capture_logs() as captured:
        compute_today_view(vault, NOW)
    matches = [
        c for c in captured
        if c.get("event") == "brief.today_view.computed"
    ]
    assert len(matches) == 1
    assert matches[0]["t1_count"] == 0
    assert matches[0]["routine_today_count"] == 0


# --- RoutineLine shape -----------------------------------------------------


def test_routine_line_carries_render_fields(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Daily",
        "type: routine\nstatus: active\nname: Daily\n"
        "cadence:\n  type: daily\n"
        "items:\n- text: Do Dishes\n  priority: tracked\n",
    )
    view = compute_today_view(vault, NOW)
    assert len(view.routine_today) == 1
    line = view.routine_today[0]
    assert isinstance(line, RoutineLine)
    assert line.text == "Do Dishes"
    assert line.priority == "tracked"


# --- self_care flag → T3 lane (Q2, 2026-06-26) -----------------------------


def test_self_care_item_lands_in_t3_lane(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Care",
        "type: routine\nstatus: active\nname: Care\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Meditate\n  priority: aspirational\n  self_care: true\n",
    )
    view = compute_today_view(vault, NOW)
    assert len(view.t3) == 1
    e = view.t3[0]
    assert e.item_text == "Meditate"
    assert e.source == "self-care"
    assert e.surface_reason == "self-care"


def test_self_care_item_not_in_routine_today(tmp_path: Path) -> None:
    """Structural-complement invariant: a self_care item surfaced to T3
    must NOT also appear in routine_today (it's handed off)."""
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Care",
        "type: routine\nstatus: active\nname: Care\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Meditate\n  priority: aspirational\n  self_care: true\n"
        "- text: Do Dishes\n  priority: tracked\n",
    )
    view = compute_today_view(vault, NOW)
    t3_texts = {e.item_text for e in view.t3}
    rt_texts = {r.text for r in view.routine_today}
    assert "Meditate" in t3_texts
    assert "Do Dishes" in rt_texts
    assert t3_texts.isdisjoint(rt_texts)


def test_self_care_done_today_not_surfaced(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Care",
        "type: routine\nstatus: active\nname: Care\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n  Meditate:\n  - '2026-05-28'\n"
        "items:\n"
        "- text: Meditate\n  priority: aspirational\n  self_care: true\n",
    )
    view = compute_today_view(vault, NOW)
    # Done today → not in T3; renders in routine_today instead (it still
    # fired as a daily item).
    assert all(e.item_text != "Meditate" for e in view.t3)


def test_self_care_with_cadence_uses_cadence_surface(tmp_path: Path) -> None:
    """A self_care item that ALSO has target_cadence_days goes through
    the cadence surface (compute_auto_t3_candidates), not the self-care-
    only surface — no double-render. Exactly one T3 entry."""
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Care",
        "type: routine\nstatus: active\nname: Care\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n  Walk Fergus:\n  - '2026-05-20'\n"
        "items:\n"
        "- text: Walk Fergus\n  priority: aspirational\n"
        "  self_care: true\n  target_cadence_days: 3\n",
    )
    view = compute_today_view(vault, NOW)
    walk = [e for e in view.t3 if e.item_text == "Walk Fergus"]
    assert len(walk) == 1  # exactly one — no double-render
    # Cadence surface owns it (source auto-cadence-routine, not self-care).
    assert walk[0].source == "auto-cadence-routine"


def test_self_care_task_no_deadline_lands_in_t3(tmp_path: Path) -> None:
    """A self_care task with no due date → the T3 self-care lane (the
    spec routes self_care on routines AND tasks to T3)."""
    vault = _vault(tmp_path)
    _write_task(
        vault, "Book Massage",
        "type: task\nstatus: todo\nname: Book Massage\nself_care: true\n",
    )
    view = compute_today_view(vault, NOW)
    sc = [e for e in view.t3 if e.name == "Book Massage"]
    assert len(sc) == 1
    assert sc[0].origin == "task"
    assert sc[0].source == "self-care"


def test_self_care_task_with_deadline_goes_t1_not_t3(tmp_path: Path) -> None:
    """A self_care task due today surfaces in T1 (deadline pressure wins)
    and is NOT double-rendered in the T3 self-care floor."""
    vault = _vault(tmp_path)
    _write_task(
        vault, "Urgent Care",
        "type: task\nstatus: todo\nname: Urgent Care\n"
        "self_care: true\ndue: 2026-05-28\n",
    )
    view = compute_today_view(vault, NOW)
    assert any(e.name == "Urgent Care" for e in view.t1)
    assert all(e.name != "Urgent Care" for e in view.t3)


# --- Reviewer carry-forwards (Step 2c NOTE-1 + NOTE-2, 2026-06-26) ---------


def test_view_emits_no_handed_off_to_tier_logs(tmp_path: Path) -> None:
    """NOTE-1: compute_today_view's internal _collect_items_for_today call
    must run quiet — the aggregate pass (05:59) owns the
    routine.aggregator.handed_off_to_tier log; the view (~06:00) is a
    derived read and must NOT re-emit them (duplicate-log fix)."""
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Bills",
        "type: routine\nstatus: active\nname: Bills\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Pay Rent\n  priority: tracked\n"
        "  due_pattern:\n    type: weekly\n    day: thu\n"  # due today → handoff
        "  escalate_at_days: 1\n",
    )
    with structlog.testing.capture_logs() as captured:
        compute_today_view(vault, NOW)
    handoff = [
        c for c in captured
        if c.get("event") == "routine.aggregator.handed_off_to_tier"
    ]
    assert handoff == [], (
        "compute_today_view must not emit handed_off_to_tier logs — the "
        "aggregate pass owns them; the view reads quiet (NOTE-1)."
    )


def test_curated_freetext_t3_dedups_auto_cadence_same_item(
    tmp_path: Path,
) -> None:
    """NOTE-2: a curated free-text T3 entry ("Walk Fergus") + an
    auto-cadence T3 entry for the SAME item must render ONCE, not twice
    (the free-text key and record-anchored key differ; text-match closes
    it)."""
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Care",
        "type: routine\nstatus: active\nname: Care\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n  Walk Fergus:\n  - '2026-05-20'\n"  # overdue → auto-T3
        "items:\n"
        "- text: Walk Fergus\n  priority: aspirational\n"
        "  target_cadence_days: 3\n",
    )
    _write_daily_curation(
        vault, "2026-05-28",
        "type: daily\ndate: '2026-05-28'\n"
        "tier_curation:\n"
        "  t1: []\n  t2: []\n"
        "  t3:\n  - item: Walk Fergus\n    source: operator\n"
        "  curated_at: '2026-05-28T07:00:00-03:00'\n",
    )
    view = compute_today_view(vault, NOW)
    walk = [e for e in view.t3 if (e.item_text or e.name) == "Walk Fergus"]
    assert len(walk) == 1, (
        f"Walk Fergus rendered {len(walk)}x in T3 — curated free-text + "
        "auto-cadence must dedup to one (NOTE-2)."
    )
    # The curated entry wins (operator-authoritative).
    assert walk[0].source == "operator"
