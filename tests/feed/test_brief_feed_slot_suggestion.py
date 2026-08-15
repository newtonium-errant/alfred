"""slot_suggestion feed extractor pins (Feed Phase A producer #2, section 4).

Fixtures are the TodayView projection scenarios the operator's ring spec renders
from in Phase B (B3 reuses these): empty day, auto-T1 overdue (task + routine),
T2 auto-surfaced, mixed done/undone, T3-only. Stable keys per the step-2 identity
table: task path / routine (record, text) / free-text T3 item text.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from alfred.brief.feed_producer import _slot_stable_key, slot_suggestion_feed_items

# Reference instant — 2026-05-28 13:00 UTC (matches tests/tier/test_today_view.py).
NOW = datetime(2026, 5, 28, 13, 0, 0, tzinfo=timezone.utc)


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "task").mkdir(parents=True, exist_ok=True)
    (vault / "routine").mkdir(parents=True, exist_ok=True)
    return vault


def _task(vault: Path, name: str, fm_body: str) -> None:
    (vault / "task" / f"{name}.md").write_text(f"---\n{fm_body}---\n\n# {name}\n", encoding="utf-8")


def _routine(vault: Path, name: str, fm_body: str) -> None:
    (vault / "routine" / f"{name}.md").write_text(f"---\n{fm_body}---\n\n# {name}\n", encoding="utf-8")


def _items(vault: Path):
    return slot_suggestion_feed_items(vault, NOW, None, instance="salem")


# --- empty day ---------------------------------------------------------------


def test_empty_day_no_slot_items(tmp_path: Path) -> None:
    assert _items(_vault(tmp_path)) == []


# --- auto-T1 overdue (task) --------------------------------------------------


def test_auto_t1_task_due_today(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _task(vault, "Pay Steph", "type: task\nstatus: todo\nname: Pay Steph\ndue: 2026-05-28\n")
    items = _items(vault)
    assert len(items) == 1
    it = items[0]
    assert it.id == "slot_suggestion:task:task/Pay Steph.md"
    assert it.kind == "slot_suggestion" and it.mode == "decide"  # decision kind
    assert it.evidence["tier"] == 1
    assert it.evidence["surface_reason"] == "due today"


# --- auto-T1 (routine) + stable key (record, text) ---------------------------


def test_auto_t1_routine_item_stable_key(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _routine(
        vault, "Bills",
        "type: routine\nstatus: active\nname: Bills\ncadence:\n  type: daily\n"
        "items:\n- text: Pay Rent\n  priority: tracked\n"
        "  due_pattern:\n    type: monthly\n    day: 1\n  escalate_at_days: 5\n",
    )
    items = _items(vault)
    assert len(items) == 1
    assert items[0].id == "slot_suggestion:routine:Bills::Pay Rent"
    assert items[0].evidence["tier"] == 1


# --- T2 auto-surfaced --------------------------------------------------------


def test_t2_auto_surface(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _routine(
        vault, "Bills",
        "type: routine\nstatus: active\nname: Bills\ncadence:\n  type: daily\n"
        "items:\n- text: Pay Rent\n  priority: tracked\n"
        "  due_pattern:\n    type: monthly\n    day: 1\n  escalate_at_days: 0\n  surface_at_days: 5\n",
    )
    items = _items(vault)
    assert len(items) == 1
    assert items[0].evidence["tier"] == 2


# --- mixed done/undone: the done task is not a slot item --------------------


def test_mixed_done_undone_only_open_items_surface(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _task(vault, "Open Task", "type: task\nstatus: todo\nname: Open Task\ndue: 2026-05-28\n")
    _task(vault, "Done Task", "type: task\nstatus: done\nname: Done Task\ndue: 2026-05-28\ncompleted: 2026-05-28\n")
    ids = {it.id for it in _items(vault)}
    assert ids == {"slot_suggestion:task:task/Open Task.md"}  # the done task never surfaces


# --- T3-only -----------------------------------------------------------------


def test_t3_only(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _routine(
        vault, "Self Care",
        "type: routine\nstatus: active\nname: Self Care\ncadence:\n  type: daily\n"
        "completion_log:\n  Walk Fergus:\n  - '2026-05-20'\n"
        "items:\n- text: Walk Fergus\n  priority: aspirational\n  target_cadence_days: 3\n",
    )
    items = _items(vault)
    assert len(items) == 1
    assert items[0].evidence["tier"] == 3


# --- done evidence (Phase C slice 1 — the board's ring greenness) -----------


def test_every_slot_item_carries_done_flag(tmp_path: Path) -> None:
    """Each emitted slot item carries ``evidence.done`` (the ring's green
    signal, from the tier layer's own predicate). Open items → done=False."""
    vault = _vault(tmp_path)
    _task(vault, "Pay Steph", "type: task\nstatus: todo\nname: Pay Steph\ndue: 2026-05-28\n")
    items = _items(vault)
    assert items
    for it in items:
        assert it.evidence["done"] is False


def test_done_true_for_completed_free_text_t3(tmp_path: Path) -> None:
    """A curated free-text T3 stamped ``done_at`` today re-emits with
    done=True (it does NOT vanish — planned/done/remaining)."""
    from datetime import date

    from alfred.tier.daily_curation import (
        DailyCuration,
        T3Entry,
        save_tier_curation,
    )

    vault = _vault(tmp_path)
    (vault / "daily").mkdir(parents=True, exist_ok=True)
    day = NOW.date()
    save_tier_curation(
        vault, day,
        DailyCuration(t3=[T3Entry(item="Meditate", source="operator-adhoc", done_at=day.isoformat())]),
    )
    items = [it for it in _items(vault) if it.evidence.get("tier") == 3]
    assert len(items) == 1
    assert items[0].evidence["done"] is True


# --- free-text T3 stable-key fallback (unit) --------------------------------


def test_slot_stable_key_free_text_fallback() -> None:
    class _Entry:
        origin = "curated"
        name = "Meditate 10 min"
        path = ""
        routine_record = None
        item_text = None

    assert _slot_stable_key(_Entry()) == "text:Meditate 10 min"


# --- accept candidate provenance (Phase C slice 2) --------------------------
# ``evidence.candidate`` is the flag the router's accept provenance-guard reads;
# ``evidence.confirmed`` is the verbatim T1 confirm flag. Pinned against the real
# producer output so the derivation can't silently drift.


def test_auto_t1_task_is_candidate_confirmed_false(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _task(vault, "Pay Steph", "type: task\nstatus: todo\nname: Pay Steph\ndue: 2026-05-28\n")
    it = _items(vault)[0]
    assert it.evidence["source"] == "auto-due"
    assert it.evidence["candidate"] is True  # auto-surfaced, not committed
    assert it.evidence["confirmed"] is False


def test_confirmed_curated_t1_is_not_candidate(tmp_path: Path) -> None:
    """A confirmed auto-T1 (operator already accepted it) preserves its auto
    ``source`` but ``confirmed=True`` flips ``candidate`` off."""
    from alfred.tier.daily_curation import (
        DailyCuration,
        T1T2Entry,
        save_tier_curation,
    )

    vault = _vault(tmp_path)
    (vault / "daily").mkdir(parents=True, exist_ok=True)
    _task(vault, "Pay Steph", "type: task\nstatus: todo\nname: Pay Steph\ndue: 2026-05-28\n")
    save_tier_curation(
        vault, NOW.date(),
        DailyCuration(t1=[T1T2Entry(task="[[task/Pay Steph]]", source="auto-due", confirmed=True)]),
    )
    t1 = [it for it in _items(vault) if it.evidence.get("tier") == 1]
    assert len(t1) == 1
    assert t1[0].evidence["confirmed"] is True
    assert t1[0].evidence["candidate"] is False  # committed → not accept-able


def test_t2_auto_surface_is_candidate(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _routine(
        vault, "Bills",
        "type: routine\nstatus: active\nname: Bills\ncadence:\n  type: daily\n"
        "items:\n- text: Pay Rent\n  priority: tracked\n"
        "  due_pattern:\n    type: monthly\n    day: 1\n  escalate_at_days: 0\n  surface_at_days: 5\n",
    )
    it = _items(vault)[0]
    assert it.evidence["tier"] == 2
    assert it.evidence["source"] == "auto-surface-routine"
    assert it.evidence["candidate"] is True
    assert it.evidence["confirmed"] is None  # T2 has no confirmed field


def test_operator_curated_t3_is_not_candidate(tmp_path: Path) -> None:
    """An operator-added free-text T3 (source ``operator-adhoc``) is committed —
    not an auto candidate."""
    from alfred.tier.daily_curation import (
        DailyCuration,
        T3Entry,
        save_tier_curation,
    )

    vault = _vault(tmp_path)
    (vault / "daily").mkdir(parents=True, exist_ok=True)
    save_tier_curation(
        vault, NOW.date(),
        DailyCuration(t3=[T3Entry(item="Rake leaves", source="operator-adhoc")]),
    )
    t3 = [it for it in _items(vault) if it.evidence.get("tier") == 3]
    assert len(t3) == 1
    assert t3[0].evidence["candidate"] is False


# --- snooze breakthrough reaches the CARD (#14) -------------------------------
# The reason was computed in the projection and then died in a log line, so a row
# that returned EARLY gave the operator no way to ask "why is this back?".
# Producer-driven per the #18 lesson: these drive the real
# `slot_suggestion_feed_items` over a real vault + a real snooze store, so they
# fail if the stamp stops travelling at ANY hop (compute → entry → evidence),
# not merely if the stamping helper regresses.


class _TierDefaults:
    """Minimal stand-in carrying only what the producer reads for snooze."""

    def __init__(self, snooze_path: str) -> None:
        self.snooze_path = snooze_path


def _snooze_store(tmp_path: Path, *, key: str, until: str, due_at_snooze: str,
                  overdue_at_snooze: bool) -> str:
    import json

    path = tmp_path / "board_snooze.json"
    path.write_text(
        json.dumps({
            key: {
                "key": key,
                "snoozed_until": until,
                "due_iso_at_snooze": due_at_snooze,
                "overdue_at_snooze": overdue_at_snooze,
                "lane": "task",
                "duration_label": "3d",
            }
        }),
        encoding="utf-8",
    )
    return str(path)


def test_moved_earlier_breakthrough_reaches_the_card(tmp_path: Path) -> None:
    """A snoozed row pulled EARLIER returns, and the card says why.

    Snoozed until the 31st when the row was ALREADY overdue and due 2026-06-10;
    the task now says 2026-05-28. Overdue-at-snooze on purpose — that is what
    closes the `crossed_due` branch, so `moved_earlier` is the only reason that
    can fire and the pin names one thing rather than whichever wins a race.
    """
    vault = _vault(tmp_path)
    _task(vault, "Pay Steph", "type: task\nstatus: todo\nname: Pay Steph\ndue: 2026-05-28\n")
    store = _snooze_store(
        tmp_path,
        key="task:task/Pay Steph.md",
        until="2026-05-31",          # still in the future at NOW
        due_at_snooze="2026-06-10",  # was later; the task now says the 28th
        overdue_at_snooze=True,      # closes the crossed_due branch
    )

    items = slot_suggestion_feed_items(vault, NOW, _TierDefaults(store), instance="salem")

    assert len(items) == 1, "the row must break through, not stay suppressed"
    assert items[0].evidence["snooze_breakthrough"] == "moved_earlier"


def test_crossed_due_breakthrough_reaches_the_card(tmp_path: Path) -> None:
    """The other reason, isolated: NOT overdue when snoozed, overdue now.

    Both reasons are pinned separately because the card renders the NAME — a fix
    that collapsed them to a single generic "it came back" would still satisfy a
    truthy check.
    """
    vault = _vault(tmp_path)
    _task(vault, "Pay Steph", "type: task\nstatus: todo\nname: Pay Steph\ndue: 2026-05-28\n")
    store = _snooze_store(
        tmp_path,
        key="task:task/Pay Steph.md",
        until="2026-05-31",
        due_at_snooze="2026-05-28",
        overdue_at_snooze=False,     # was not yet due; today it is
    )

    items = slot_suggestion_feed_items(vault, NOW, _TierDefaults(store), instance="salem")

    assert len(items) == 1
    assert items[0].evidence["snooze_breakthrough"] == "crossed_due"


def test_a_row_that_was_never_snoozed_carries_an_empty_reason(tmp_path: Path) -> None:
    """Intentionally-left-blank: the field is always present, and empty means
    'no early return to explain' — distinguishable from a missing key."""
    vault = _vault(tmp_path)
    _task(vault, "Pay Steph", "type: task\nstatus: todo\nname: Pay Steph\ndue: 2026-05-28\n")
    items = slot_suggestion_feed_items(vault, NOW, None, instance="salem")
    assert len(items) == 1
    assert items[0].evidence["snooze_breakthrough"] == ""


def test_a_held_snooze_emits_no_card_at_all(tmp_path: Path) -> None:
    """Control: with no delta the snooze HOLDS, so there is no card to explain.

    Guards against a fix that surfaced every snoozed row just to carry a reason.
    """
    vault = _vault(tmp_path)
    _task(vault, "Pay Steph", "type: task\nstatus: todo\nname: Pay Steph\ndue: 2026-06-10\n")
    store = _snooze_store(
        tmp_path,
        key="task:task/Pay Steph.md",
        until="2026-05-31",
        due_at_snooze="2026-06-10",  # unchanged → no delta
        overdue_at_snooze=False,
    )
    items = slot_suggestion_feed_items(vault, NOW, _TierDefaults(store), instance="salem")
    assert items == []


# --- PY-B item 1: the overdue task reaches the PRODUCER ----------------------
#
# The unit pins live in tests/tier/test_compute.py. This is the integration
# leg: a compute-layer reason that never reaches the feed producer is a fix
# nobody sees. Note the module docstring above has claimed "auto-T1 overdue
# (task + routine)" coverage since this file was written — the routine half
# existed, the task half did not, and no fixture here was actually overdue.


def test_overdue_task_reaches_the_slot_suggestion_producer(tmp_path: Path) -> None:
    """An open task 3 days past due becomes a T1 slot_suggestion card."""
    vault = _vault(tmp_path)
    _task(
        vault, "Submit Documents to Clutch",
        "type: task\nstatus: todo\nname: Submit Documents to Clutch\n"
        "due: 2026-05-25\n",
    )
    items = _items(vault)
    assert len(items) == 1
    it = items[0]
    assert it.id == "slot_suggestion:task:task/Submit Documents to Clutch.md"
    assert it.kind == "slot_suggestion"
    assert it.evidence["tier"] == 1
    assert it.evidence["surface_reason"] == "overdue by 3d"


def test_overdue_done_task_produces_no_card(tmp_path: Path) -> None:
    """The exclusion, driven through the producer rather than the computer —
    a closed task must not reappear as a card by aging."""
    vault = _vault(tmp_path)
    _task(
        vault, "Finished",
        "type: task\nstatus: done\nname: Finished\ndue: 2026-05-20\n",
    )
    assert _items(vault) == []


def test_a_due_today_task_still_produces_its_card(tmp_path: Path) -> None:
    """POSITIVE CONTROL for the two above: the producer is demonstrably
    capable of emitting, so "no card" means excluded, not broken."""
    vault = _vault(tmp_path)
    _task(
        vault, "Today",
        "type: task\nstatus: todo\nname: Today\ndue: 2026-05-28\n",
    )
    items = _items(vault)
    assert len(items) == 1
    assert items[0].evidence["surface_reason"] == "due today"
