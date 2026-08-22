"""The CANCEL disposition — writer, router, and the suppression it must cause (#103).

The operator's friction, verbatim: *"There's no quick way to remove a card once
assigned without talking to Salem directly. For example, TheJamieClinic email
setup no longer needs to be done, so I don't want to mark it done, but I do want
it removed from the list as cancelled."*

WHAT THIS MODULE IS FOR, beyond the happy path. Three claims in this lane are
the kind that read as obviously-true and are exactly the kind that rot:

  1. *A cancel does not come back.* The card in the report surfaced via
     ``surface_reason: returned`` / ``source: auto-returned``, so a cancel that
     fails to suppress the RETURN is the operator's complaint with extra steps.
     Driven here against the real producer, never reasoned about.
  2. *A cancel is not a completion.* Measured at ``c552fa09``, a curated T1 whose
     record was cancelled reported ``t1_done=1 all_t1_done=True`` — a
     cancellation counted as an achievement. Pinned in both directions.
  3. *A refusal refuses for the reason it claims.* Every deny pin below asserts
     the LOGGED reason and the untouched record, not merely ``ok is False`` —
     a denial for an unrelated cause (missing record, wrong lane, unknown type)
     wears the identical ``ok=False`` and would pass against a build with no
     guard at all.

Every deny pin carries its POSITIVE CONTROL in the same test: the nearest
admissible neighbour must succeed against the same fixtures, or the pin proves
only that the path is dead.

Contract-first, no dep-gated skips (``feedback_regression_pin_unconditional``).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter
import pytest
import structlog

from alfred.daily_sync import action_router as arouter
from alfred.daily_sync.action_router import (
    CANCEL_ACTION,
    CANCEL_ACTED_VERB,
    STATUS_ALREADY_DONE,
    STATUS_CANCELLED,
    STATUS_INVALID_ACTION,
    STATUS_UNCANCELLED,
    STATUS_UNSUPPORTED_ITEM,
    UNDO_CANCEL_ACTION,
    UNDO_DONE_ACTION,
    act,
)
from alfred.daily_sync.config import DailySyncConfig
from alfred.feed import FeedStore
from alfred.feed.model import STATE_ACTED, STATE_OPEN
from alfred.tier.task_cancel import (
    CANCELLED_AT_FIELD,
    CANCELLED_FROM_FIELD,
    CANCELLED_STATUS,
    TASK_CANCEL_KIND_ALREADY_DONE,
    TASK_CANCEL_KIND_IDEMPOTENT_NOOP,
    TASK_CANCEL_KIND_INVALID_STATUS,
    TASK_CANCEL_KIND_SUCCESS,
    TASK_CANCEL_KIND_UNKNOWN_RECORD,
    TASK_RESTORE_KIND_NO_PRIOR_STATUS,
    TASK_RESTORE_KIND_NOT_CANCELLED,
    TASK_RESTORE_KIND_SUCCESS,
    mark_task_cancelled,
    restore_cancelled_task,
)

NOW = datetime(2026, 7, 22, 13, 0, 0, tzinfo=timezone.utc)
TODAY = date(2026, 7, 22)
TODAY_ISO = "2026-07-22"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _pin_router_today(monkeypatch):
    """Pin the router's date to the fixture day — the same seam
    ``test_slot_completion`` clamps, for the same determinism reason."""
    monkeypatch.setattr(arouter, "_today_for", lambda config: TODAY)


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for sub in ("task", "daily", "routine"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    return vault


def _task(
    vault: Path,
    name: str,
    *,
    status: str = "todo",
    extra: str = "",
    body: str = "Some notes worth keeping.",
) -> Path:
    p = vault / "task" / f"{name}.md"
    p.write_text(
        f"---\ntype: task\nstatus: {status}\nname: {name}\n{extra}---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return p


def _fm(p: Path) -> dict:
    return dict(frontmatter.load(str(p)).metadata or {})


def _ds_config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True)
    cfg.state.path = str(tmp_path / "state.json")
    cfg.schedule.timezone = "UTC"
    return cfg


def _store(tmp_path: Path) -> FeedStore:
    return FeedStore(str(tmp_path / "feed.jsonl"))


def _act(store, cfg, vault, fid: str, action: str) -> Any:
    return act(
        fid, action,
        feed_store=store, config=cfg, vault_path=vault,
        instance_name="salem", instance_scope="talker", raw_config=None,
    )


def _slot_item(store: FeedStore, *, origin: str, path: str, name: str, **ev):
    """Publish one slot card for a lane. Evidence mirrors what the real producer
    stamps for that lane (``origin``/``path`` for task, ``routine_record`` for
    routine, ``tier: 3`` for the free-text lane)."""
    from alfred.feed.model import FeedItem

    evidence = {"origin": origin, "path": path, "name": name, "item_text": name}
    evidence.update(ev)
    item = FeedItem.create(
        kind="slot_suggestion",
        stable_key=f"{origin}:{name}",
        instance="salem",
        title=name,
        evidence=evidence,
    )
    store.upsert(item)
    return item


# ---------------------------------------------------------------------------
# Premise pins — the facts this lane's arrangement depends on
# ---------------------------------------------------------------------------


def test_premise_cancelled_is_a_legal_task_status_per_schema() -> None:
    """``cancelled`` is legal on a ``task`` per the SCHEMA — the independent
    source, not this lane's own constant.

    A premise pin, not a tautology: ``task_cancel._TASK_STATUSES`` is DERIVED
    from ``STATUS_BY_TYPE``, so a fixture built from the writer's constant would
    move with any bug and score a mutation RED 0. Reading ``schema.py``
    directly is the only assertion the derivation cannot satisfy by
    construction."""
    from alfred.vault.schema import STATUS_BY_TYPE

    assert CANCELLED_STATUS in STATUS_BY_TYPE["task"]
    # And it is NOT open — the property every suppression below rests on.
    from alfred.tier.compute import OPEN_STATUSES

    assert CANCELLED_STATUS not in OPEN_STATUSES


def test_premise_cancelled_at_is_the_house_spelling_not_a_minted_one() -> None:
    """``cancelled_at`` is SAMPLED from the shipped migration scripts, not
    minted to rhyme with ``completed``.

    Provenance, not shape-realism: both one-shot migrations already stamp this
    exact key on task records, so a second spelling here would have created two
    vocabularies for one fact in a vault that already carries the first."""
    from alfred.scripts import migrate_routine_recurring_bills as mig

    doc = mig.__doc__ or ""
    assert f"{CANCELLED_AT_FIELD}:" in doc, (
        "the migration script's own docstring is the source this field name was "
        "taken from; if it no longer spells it this way, re-derive rather than "
        "assuming"
    )


# ---------------------------------------------------------------------------
# The writer — cancel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prior", ["todo", "active", "blocked"])
def test_cancel_writes_status_date_and_provenance(tmp_path: Path, prior: str) -> None:
    vault = _vault(tmp_path)
    p = _task(vault, "Setup TheJamieClinic Email", status=prior)

    res = mark_task_cancelled(vault, "task/Setup TheJamieClinic Email.md", TODAY_ISO)

    assert res.kind == TASK_CANCEL_KIND_SUCCESS
    assert res.changed is True
    fm = _fm(p)
    assert fm["status"] == CANCELLED_STATUS
    assert str(fm[CANCELLED_AT_FIELD]) == TODAY_ISO
    assert fm[CANCELLED_FROM_FIELD] == prior
    # STRUCK THROUGH AND KEPT, per the GCal precedent — the body survives.
    assert "Some notes worth keeping." in p.read_text(encoding="utf-8")
    # And it did NOT write a completion. The whole point of the verb.
    assert "completed" not in fm
    assert fm.get("status") != "done"


def test_cancel_is_idempotent(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    p = _task(vault, "Twice")
    mark_task_cancelled(vault, "task/Twice.md", TODAY_ISO)
    first = p.read_text(encoding="utf-8")

    res = mark_task_cancelled(vault, "task/Twice.md", "2026-08-01")

    assert res.kind == TASK_CANCEL_KIND_IDEMPOTENT_NOOP
    assert res.changed is False
    # Byte-identical: the second call must not restamp the date.
    assert p.read_text(encoding="utf-8") == first


def test_cancel_refuses_a_done_task_and_leaves_it_untouched(tmp_path: Path) -> None:
    """A completed task is refused with its OWN reason, and the neighbour that
    should succeed does — the positive control that makes this a guard pin
    rather than a proof the path is dead."""
    vault = _vault(tmp_path)
    done = _task(vault, "Finished", status="done", extra="completed: 2026-07-01\n")
    before = done.read_text(encoding="utf-8")
    open_task = _task(vault, "Still Open", status="todo")

    refused = mark_task_cancelled(vault, "task/Finished.md", TODAY_ISO)
    allowed = mark_task_cancelled(vault, "task/Still Open.md", TODAY_ISO)

    # The refusal names ALREADY_DONE specifically — not a generic error, which
    # would be indistinguishable from an unknown record or a bad status.
    assert refused.kind == TASK_CANCEL_KIND_ALREADY_DONE
    assert refused.ok is False
    assert refused.status == "done"
    assert done.read_text(encoding="utf-8") == before  # not one byte
    # POSITIVE CONTROL — the same call on the admissible neighbour succeeds.
    assert allowed.kind == TASK_CANCEL_KIND_SUCCESS
    assert _fm(open_task)["status"] == CANCELLED_STATUS


def test_cancel_fails_loud_on_unknown_status(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    weird = _task(vault, "Weird", status="marinating")
    before = weird.read_text(encoding="utf-8")
    ok_task = _task(vault, "Normal", status="active")

    refused = mark_task_cancelled(vault, "task/Weird.md", TODAY_ISO)
    allowed = mark_task_cancelled(vault, "task/Normal.md", TODAY_ISO)

    assert refused.kind == TASK_CANCEL_KIND_INVALID_STATUS
    assert refused.status == "marinating"
    assert weird.read_text(encoding="utf-8") == before
    assert allowed.kind == TASK_CANCEL_KIND_SUCCESS


def test_cancel_refuses_non_task_and_missing_records(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    note = vault / "task" / "NotATask.md"
    note.write_text("---\ntype: note\nstatus: active\nname: NotATask\n---\n\n# n\n", encoding="utf-8")
    before = note.read_text(encoding="utf-8")
    real = _task(vault, "Real")

    not_a_task = mark_task_cancelled(vault, "task/NotATask.md", TODAY_ISO)
    missing = mark_task_cancelled(vault, "task/Nope.md", TODAY_ISO)
    allowed = mark_task_cancelled(vault, "task/Real.md", TODAY_ISO)

    assert not_a_task.kind == TASK_CANCEL_KIND_UNKNOWN_RECORD
    assert note.read_text(encoding="utf-8") == before
    assert missing.kind == TASK_CANCEL_KIND_UNKNOWN_RECORD
    assert allowed.kind == TASK_CANCEL_KIND_SUCCESS


def test_cancel_refuses_a_path_escaping_the_vault_and_touches_nothing(
    tmp_path: Path,
) -> None:
    """Containment. The refusal must leave nothing OUTSIDE the vault either —
    'does it write the record' is the wrong question; 'does it touch anything
    out there' is the right one (a refused write once still created a lock file
    via ``mkdir(parents=True)``)."""
    vault = _vault(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "Secret.md"
    victim.write_text("---\ntype: task\nstatus: todo\nname: Secret\n---\n\n# s\n", encoding="utf-8")
    before = victim.read_text(encoding="utf-8")
    debris_before = sorted(p.name for p in outside.iterdir())
    inside = _task(vault, "Inside")

    refused = mark_task_cancelled(vault, "../outside/Secret.md", TODAY_ISO)
    allowed = mark_task_cancelled(vault, "task/Inside.md", TODAY_ISO)

    assert refused.kind == TASK_CANCEL_KIND_UNKNOWN_RECORD
    assert victim.read_text(encoding="utf-8") == before
    # No lock file, no .tmp, no directory — nothing new out there at all.
    assert sorted(p.name for p in outside.iterdir()) == debris_before
    assert allowed.kind == TASK_CANCEL_KIND_SUCCESS


def test_cancel_logs_its_success_with_the_prior_status(tmp_path: Path) -> None:
    """Log-emission pin — the provenance must be observable, not just stored."""
    vault = _vault(tmp_path)
    _task(vault, "Logged", status="blocked")

    with structlog.testing.capture_logs() as captured:
        mark_task_cancelled(vault, "task/Logged.md", TODAY_ISO)

    hits = [c for c in captured if c.get("event") == "tier.task_cancel.success"]
    assert len(hits) == 1
    assert hits[0]["prior_status"] == "blocked"
    assert hits[0]["date"] == TODAY_ISO


# ---------------------------------------------------------------------------
# The writer — restore (undo)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prior", ["todo", "active", "blocked"])
def test_restore_returns_the_exact_prior_status(tmp_path: Path, prior: str) -> None:
    """The round trip is EXACT, not a guess — which is the entire reason the
    cancel stamps ``cancelled_from``, and the reason this lane can offer an undo
    where ``undo_done`` still cannot."""
    vault = _vault(tmp_path)
    p = _task(vault, "RoundTrip", status=prior)
    original = p.read_text(encoding="utf-8")

    mark_task_cancelled(vault, "task/RoundTrip.md", TODAY_ISO)
    res = restore_cancelled_task(vault, "task/RoundTrip.md")

    assert res.kind == TASK_RESTORE_KIND_SUCCESS
    assert res.status == prior
    fm = _fm(p)
    assert fm["status"] == prior
    # The cancel's bookkeeping is GONE, not merely overwritten — a true inverse.
    assert CANCELLED_AT_FIELD not in fm
    assert CANCELLED_FROM_FIELD not in fm
    # Byte-for-byte back to where it started.
    assert p.read_text(encoding="utf-8") == original


def test_restore_refuses_a_cancel_with_no_recorded_provenance(tmp_path: Path) -> None:
    """A Salem-cancelled (or migration-cancelled) task carries no
    ``cancelled_from``, so its prior status is genuinely unknown. Refuse rather
    than guess ``todo`` — resurrecting a ``blocked`` task as actionable is the
    exact lifecycle-guess that keeps ``undo_done`` unsupported on this lane.

    Positive control: a BOARD-cancelled task in the same vault restores fine, so
    the pin proves the provenance gate fires, not that restore is broken."""
    vault = _vault(tmp_path)
    # Exactly what SKILL.md:2750's recipe writes: status only.
    salem = _task(vault, "Salem Cancelled", status="cancelled")
    before = salem.read_text(encoding="utf-8")
    board = _task(vault, "Board Cancelled", status="active")
    mark_task_cancelled(vault, "task/Board Cancelled.md", TODAY_ISO)

    refused = restore_cancelled_task(vault, "task/Salem Cancelled.md")
    allowed = restore_cancelled_task(vault, "task/Board Cancelled.md")

    assert refused.kind == TASK_RESTORE_KIND_NO_PRIOR_STATUS
    assert salem.read_text(encoding="utf-8") == before
    # POSITIVE CONTROL.
    assert allowed.kind == TASK_RESTORE_KIND_SUCCESS
    assert _fm(vault / "task" / "Board Cancelled.md")["status"] == "active"


def test_restore_refuses_a_task_that_is_not_cancelled(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    live = _task(vault, "Live", status="todo")
    before = live.read_text(encoding="utf-8")
    cancelled = _task(vault, "Gone", status="todo")
    mark_task_cancelled(vault, "task/Gone.md", TODAY_ISO)

    refused = restore_cancelled_task(vault, "task/Live.md")
    allowed = restore_cancelled_task(vault, "task/Gone.md")

    assert refused.kind == TASK_RESTORE_KIND_NOT_CANCELLED
    assert live.read_text(encoding="utf-8") == before
    assert allowed.kind == TASK_RESTORE_KIND_SUCCESS


def test_restore_logs_its_refusal_reason(tmp_path: Path) -> None:
    """The refusal must SAY why. A ``no_prior_status`` and a ``not_cancelled``
    are the same ``ok=False`` to a caller reading only the boolean."""
    vault = _vault(tmp_path)
    _task(vault, "Bare", status="cancelled")

    with structlog.testing.capture_logs() as captured:
        restore_cancelled_task(vault, "task/Bare.md")

    hits = [c for c in captured if c.get("event") == "tier.task_restore.no_prior_status"]
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# The router — lane scope, both gates
# ---------------------------------------------------------------------------


def test_board_cancel_writes_the_vault_and_acts_the_card(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    p = _task(vault, "Setup TheJamieClinic Email")
    item = _slot_item(
        store, origin="task", path="task/Setup TheJamieClinic Email.md",
        name="Setup TheJamieClinic Email",
    )

    res = _act(store, cfg, vault, item.id, CANCEL_ACTION)

    assert res.ok is True
    assert res.status == STATUS_CANCELLED
    # The operator's own word, and NOT "done".
    assert "cancelled" in res.detail.lower()
    assert "done" not in res.detail.lower()
    assert _fm(p)["status"] == CANCELLED_STATUS
    stored = store.load()[item.id]
    assert stored.state == STATE_ACTED
    assert stored.acted_action == CANCEL_ACTED_VERB


@pytest.mark.parametrize(
    "origin,extra",
    [
        ("routine_item", {"routine_record": "routine/Self Care.md"}),
        ("tier", {"tier": 3}),
        ("mystery", {}),
    ],
)
def test_board_cancel_refuses_every_lane_but_task(
    tmp_path: Path, origin: str, extra: dict,
) -> None:
    """Scope ruling: task-backed cards ONLY. The refusal names the lane, and the
    task lane succeeds against the same fixtures as the control."""
    vault = _vault(tmp_path)
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    _task(vault, "A Real Task")
    other = _slot_item(store, origin=origin, path="routine/Self Care.md", name="Meditate", **extra)
    task_card = _slot_item(
        store, origin="task", path="task/A Real Task.md", name="A Real Task",
    )

    with structlog.testing.capture_logs() as captured:
        refused = _act(store, cfg, vault, other.id, CANCEL_ACTION)
    allowed = _act(store, cfg, vault, task_card.id, CANCEL_ACTION)

    assert refused.ok is False
    assert refused.status == STATUS_UNSUPPORTED_ITEM
    # The logged reason distinguishes THIS guard from a missing-record refusal.
    hits = [c for c in captured if c.get("event") == "feed.act.slot.cancel_unsupported_lane"]
    assert len(hits) == 1
    assert store.load()[other.id].state == STATE_OPEN  # untouched
    # POSITIVE CONTROL.
    assert allowed.ok is True
    assert _fm(vault / "task" / "A Real Task.md")["status"] == CANCELLED_STATUS


def test_cancel_is_offered_on_the_task_lane_and_withheld_elsewhere() -> None:
    """The SERVE side. A verb offered where the router refuses it is a control
    that 400s in the operator's hand — the failure the served list exists to
    remove."""
    task_item = {"kind": "slot_suggestion", "evidence": {"origin": "task"}}
    routine_item = {
        "kind": "slot_suggestion",
        "evidence": {"origin": "routine_item", "routine_record": "routine/X.md"},
    }
    t3_item = {"kind": "slot_suggestion", "evidence": {"origin": "tier", "tier": 3}}

    served = {v["verb"] for v in arouter.actions_for_item(task_item)}
    assert CANCEL_ACTION in served
    assert UNDO_CANCEL_ACTION in served
    for other in (routine_item, t3_item):
        off = {v["verb"] for v in arouter.actions_for_item(other)}
        assert CANCEL_ACTION not in off
        assert UNDO_CANCEL_ACTION not in off
        # POSITIVE CONTROL — the lane still gets its own verbs, so the
        # assertion above is about cancel and not about an empty list.
        assert "done" in off


def test_cancel_ships_heavy_and_ungrouped() -> None:
    """Placement contract. HEAVY because it writes a status that clears the task
    from every list; UNGROUPED because ``group`` means co-equal alternatives of
    ONE decision and cancel answers a different question than the durations —
    grouping it would encode 'cancel is just another snooze', the exact #14
    conflation the ruling forbids."""
    served = arouter.actions_for_item({"kind": "slot_suggestion", "evidence": {"origin": "task"}})
    cancel = next(v for v in served if v["verb"] == CANCEL_ACTION)

    assert cancel["weight"] == "heavy"
    assert "group" not in cancel
    assert "gesture" not in cancel  # menu verb, never a swipe
    assert cancel["note"]
    # The when-family is still grouped — proving 'group' is live in this payload
    # and its absence on cancel is a decision, not a broken serializer.
    assert next(v for v in served if v["verb"] == "done")["group"] == "when"


def test_board_undo_cancel_round_trips(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    p = _task(vault, "Oops", status="active")
    item = _slot_item(store, origin="task", path="task/Oops.md", name="Oops")

    _act(store, cfg, vault, item.id, CANCEL_ACTION)
    res = _act(store, cfg, vault, item.id, UNDO_CANCEL_ACTION)

    assert res.ok is True
    assert res.status == STATUS_UNCANCELLED
    assert _fm(p)["status"] == "active"
    assert store.load()[item.id].state == STATE_OPEN


# ---------------------------------------------------------------------------
# The guarded family — every gate that asks "is this finished?"
# ---------------------------------------------------------------------------


def test_snooze_on_a_cancelled_item_folds_to_already_acted(tmp_path: Path) -> None:
    """PIN WRITTEN WITH ITS PREMISE REVERSED (#103).

    It was first written to assert a dedicated ``board.snooze.refused_cancelled``
    refusal, on the premise that ``_dispatch_slot_snooze``'s ``acted_action``
    guard was live and merely needed widening to cover cancel. MEASUREMENT
    DISPROVED THE PREMISE: that dispatcher is reachable only in ``STATE_OPEN``,
    and an OPEN item never carries an ``acted_action`` — ``FeedStore``'s fold
    clears the verb on every non-terminal transition and ``feed.sweep.as_open``
    nulls it on revival (both driven, not read). So the guard as designed could
    never have fired, and the ``DONE_FAMILY`` half already there is inert for the
    same reason.

    The honest behaviour is the folded-state gate's ``already_acted``, and that
    is what this now asserts. Kept rather than deleted, with the reversed premise
    recorded, so the next person to reach for that widening meets the
    measurement instead of repeating it — and so the assertion reddens if the
    fold ever does start carrying a verb into the open state."""
    vault = _vault(tmp_path)
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    _task(vault, "Moot")
    item = _slot_item(store, origin="task", path="task/Moot.md", name="Moot")
    _act(store, cfg, vault, item.id, CANCEL_ACTION)

    res = _act(store, cfg, vault, item.id, "snooze_1d")

    assert res.status == arouter.STATUS_ALREADY_ACTED
    # THE PREMISE ITSELF, pinned: the acted verb does not survive into open.
    stored = store.load()[item.id]
    assert stored.acted_action == CANCEL_ACTED_VERB  # terminal state keeps it
    store.set_state(item.id, STATE_OPEN)
    assert store.load()[item.id].acted_action is None  # open state does not
    # And the cancel stands — a refused snooze wrote nothing to the record.
    assert _fm(vault / "task" / "Moot.md")["status"] == CANCELLED_STATUS


def test_undo_done_on_a_cancelled_item_points_at_undo_cancel(tmp_path: Path) -> None:
    """The third member of the undo-guard family (accept / snooze / cancel). The
    generic task-lane answer would be 'undo isn't available for tasks' — the
    right colour by luck, and it would send the operator to chat for something
    the board can do."""
    vault = _vault(tmp_path)
    cfg = _ds_config(tmp_path)
    store = _store(tmp_path)
    _task(vault, "Misfire")
    item = _slot_item(store, origin="task", path="task/Misfire.md", name="Misfire")
    _act(store, cfg, vault, item.id, CANCEL_ACTION)

    with structlog.testing.capture_logs() as captured:
        res = _act(store, cfg, vault, item.id, UNDO_DONE_ACTION)

    assert res.ok is False
    assert res.status == STATUS_INVALID_ACTION
    assert "cancelled, not done" in res.detail
    hits = [c for c in captured if c.get("event") == "feed.act.slot.undo_on_cancelled"]
    assert len(hits) == 1
    # And the record is STILL cancelled — the refusal wrote nothing.
    assert _fm(vault / "task" / "Misfire.md")["status"] == CANCELLED_STATUS


def test_no_closed_family_constant_was_shipped() -> None:
    """The OMITTED-CONSTRUCT pin, with its rationale at the assertion.

    A ``CLOSED_FAMILY = (*DONE_FAMILY, CANCEL_ACTED_VERB)`` was written in this
    lane and then REMOVED, because the gates it would have widened are
    unreachable for any acted verb (see the snooze pin above for the
    measurement). Shipping it would have been an inert construct that reads as
    coverage — a name that resolves while its effect is nil.

    Pinned so its reintroduction is a decision rather than an accident: a future
    reader who wants it must first show the gate can see an ``acted_action``,
    which is exactly the check that was missing the first time."""
    assert not hasattr(arouter, "CLOSED_FAMILY")
    # POSITIVE CONTROL on the LIVE path: the verbs cancel genuinely reaches ARE
    # exported and do carry it, so this pin asserts an absence in a module that
    # is otherwise wired — not an absence because nothing shipped at all.
    assert arouter.CANCEL_ACTION in arouter.FEED_ACTIONS["slot_suggestion"]
    assert arouter.UNDO_CANCEL_ACTION in arouter.FEED_ACTIONS["slot_suggestion"]


def test_cancel_cannot_reach_a_completion_writer() -> None:
    """The structural guarantee, asserted the way its siblings are: the cancel
    dispatcher's own code names no completion writer, and the module it DOES
    import does not contain one.

    This is why ``task_cancel`` is a separate module from ``task_completion``.
    Had they shared a file, this pin could only ever have been a convention."""
    names = set(arouter._dispatch_slot_cancel.__code__.co_names)
    for forbidden in ("mark_task_done", "mark_routine_item_done", "mark_t3_done", "add_snooze"):
        assert forbidden not in names

    import alfred.tier.task_cancel as tc

    assert not hasattr(tc, "mark_task_done")


# ---------------------------------------------------------------------------
# Suppression — the operator's actual complaint
# ---------------------------------------------------------------------------


def test_cancel_stops_the_returned_card_coming_back(tmp_path: Path) -> None:
    """THE COMPLAINT. The card surfaced via ``surface_reason: returned`` /
    ``source: auto-returned``; a cancel that does not suppress THAT is the
    complaint with extra steps.

    Driven against the real producer, in both directions in one test: the task
    is a returned candidate BEFORE the cancel (the positive control that proves
    the producer can return 1 here) and is absent AFTER."""
    from alfred.tier.compute import compute_returned_task_candidates

    vault = _vault(tmp_path)
    _task(
        vault, "Setup TheJamieClinic Email",
        extra="reminded_at: '2026-07-20T09:00:00+00:00'\nreturn_slot: rhythm\n",
    )

    before = compute_returned_task_candidates(vault, NOW)
    assert [c.name for c in before] == ["Setup TheJamieClinic Email"], (
        "positive control: the task MUST be a returned candidate before the "
        "cancel, or the 'gone after' assertion proves nothing"
    )

    mark_task_cancelled(vault, "task/Setup TheJamieClinic Email.md", TODAY_ISO)

    after = compute_returned_task_candidates(vault, NOW)
    assert after == []


def test_cancel_drops_the_task_from_the_t1_pool(tmp_path: Path) -> None:
    """The deadline-driven producer, same both-directions shape."""
    from alfred.tier.compute import compute_auto_t1_candidates

    vault = _vault(tmp_path)
    _task(vault, "Due Today", extra=f"due: {TODAY_ISO}\n")

    before = compute_auto_t1_candidates(vault, NOW)
    assert "Due Today" in [c.name for c in before]

    mark_task_cancelled(vault, "task/Due Today.md", TODAY_ISO)

    assert "Due Today" not in [c.name for c in compute_auto_t1_candidates(vault, NOW)]


def test_a_cancelled_curated_task_is_neither_a_todo_nor_an_achievement(
    tmp_path: Path,
) -> None:
    """THE REGRESSION THIS LANE FOUND. Measured at ``c552fa09``: a curated T1
    whose record was cancelled reported ``t1_count=1 t1_done=1
    all_t1_done=True`` — the daily goal counted a cancellation as a completion,
    which is exactly what *"I don't want to mark it done"* refuses.

    Curated entries are the one shape the status filters miss, because curation
    is authoritative by design and nothing re-asks the record.

    Both directions, and the positive control is load-bearing: a LIVE curated
    task must still surface, or this pin would pass against a build that dropped
    the curated lane entirely."""
    from alfred.tier.compute import compute_today_view
    from alfred.tier.daily_curation import DailyCuration, T1T2Entry, save_tier_curation

    vault = _vault(tmp_path)
    _task(vault, "Setup TheJamieClinic Email")
    _task(vault, "Live One")
    save_tier_curation(vault, TODAY, DailyCuration(t1=[
        T1T2Entry(task="Setup TheJamieClinic Email", source="operator"),
        T1T2Entry(task="Live One", source="operator"),
    ]))

    before = compute_today_view(vault, NOW)
    assert {e.name for e in before.t1} == {"Setup TheJamieClinic Email", "Live One"}

    mark_task_cancelled(vault, "task/Setup TheJamieClinic Email.md", TODAY_ISO)
    after = compute_today_view(vault, NOW)

    # Gone from the lane — not a to-do.
    assert [e.name for e in after.t1] == ["Live One"]
    # And NOT counted as an achievement.
    assert after.daily_goal.t1_done == 0
    assert after.daily_goal.all_t1_done is False
    # POSITIVE CONTROL: the live curated task is still there and still countable.
    assert after.daily_goal.t1_available == 1


def test_cancelled_task_is_not_done_today(tmp_path: Path) -> None:
    """The shared predicate, corrected. ``entry_is_done`` is reused by the daily
    goal, ``day_plan`` and the feed producer — a predicate answering 'done' for
    a status nobody completed is a trap for whichever surface reaches it next."""
    from alfred.tier.compute import _task_is_done_today

    assert _task_is_done_today({"status": "done"}, TODAY) is True
    assert _task_is_done_today({"status": "cancelled"}, TODAY) is False
    assert _task_is_done_today(
        {"status": "cancelled", CANCELLED_AT_FIELD: TODAY_ISO}, TODAY,
    ) is False


def test_cancel_excluding_lanes_announces_itself(tmp_path: Path) -> None:
    """ILB: a silent drop is indistinguishable from a producer that never ran."""
    from alfred.tier.compute import compute_today_view

    vault = _vault(tmp_path)
    _task(vault, "Dropped", status="cancelled")

    with structlog.testing.capture_logs() as captured:
        compute_today_view(vault, NOW)

    hits = [c for c in captured if c.get("event") == "tier.today_view.cancelled_tasks_excluded"]
    assert len(hits) == 1
    assert hits[0]["count"] == 1
