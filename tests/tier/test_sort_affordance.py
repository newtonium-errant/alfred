"""The sort affordance — the operator's "no way of being sorted" report (2026-08-19).

His words, against a screenshot of four T2 tasks created in chat the night
before, due Friday, sitting under the home board's "NOT SORTED YET" heading with
DONE as their only control:

    "These 'not sorted' items have no way of being sorted. They are in the feed,
    haven't seen them in the deck. No option to sort."

Three defects sat behind that sentence and this file pins all three:

  1. **The classifier was blind on curated rows.** ``_curated_to_tier_entry``
     built a TierEntry from the day's curation block, which carries only the
     item's identity — so ``due_iso`` / ``explicit_slot`` / ``self_care`` /
     ``has_due_pattern`` were all empty and rule 1 ("the operator's word is
     final") was DEAD for every curated entry. A task with ``slot: duty``
     written on it reported ``no_signal``.
  2. **There was no writer.** Nothing anywhere could record a slot ruling from a
     surface; the only ``slot:`` write in the tree was the snooze-return path.
  3. **There was no verb.** ``slot_suggestion``'s ceiling had done / undo_done /
     accept / the snoozes, and nothing that said where an item belongs.

Test surface, and what each part would catch:

  * hydration, WITH its negative control — an undated slotless task must STILL
    classify ``unslotted``, or the fix would be "default everything to Duty",
    which is the lying-DONE-button disease in a new costume;
  * the writer's success paths END-TO-END through ``compute_today_view``, not
    just "a file changed" — the only claim worth making is that the BOARD moves;
  * every refusal asserted by its LOGGED REASON, each with a positive control,
    because an ``ok=False`` from a guard and an ``ok=False`` from an absent
    record are indistinguishable at the result;
  * a containment escape asserted as touching NOTHING, not merely as refused;
  * the act path driving the real writer, and NOT touching feed state;
  * the rotation's cap, its held-deferred band, and its ILB line.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import frontmatter
import structlog

from alfred.tier import slots
from alfred.tier.compute import compute_today_view
from alfred.tier.sort_writer import (
    SORT_KIND_IDEMPOTENT_NOOP,
    SORT_KIND_INVALID_SLOT,
    SORT_KIND_SUCCESS,
    SORT_KIND_THIN_EVIDENCE,
    SORT_KIND_UNKNOWN_ITEM,
    SORT_KIND_UNKNOWN_RECORD,
    assign_slot,
)

# Wednesday 2026-08-19 13:00 UTC — the day of the operator's report. His four
# tasks were due Friday, 2026-08-21: dated, and two days out.
NOW = datetime(2026, 8, 19, 13, 0, 0, tzinfo=timezone.utc)
FRIDAY = "2026-08-21"


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "task").mkdir(parents=True, exist_ok=True)
    (vault / "routine").mkdir(parents=True, exist_ok=True)
    (vault / "daily").mkdir(parents=True, exist_ok=True)
    return vault


def _task(vault: Path, name: str, fm: str) -> None:
    (vault / "task" / f"{name}.md").write_text(
        f"---\ntype: task\nstatus: todo\nname: {name}\n{fm}---\n\n# {name}\n",
        encoding="utf-8",
    )


def _routine(vault: Path, name: str, fm: str) -> None:
    (vault / "routine" / f"{name}.md").write_text(
        f"---\ntype: routine\nstatus: active\nname: {name}\n{fm}---\n\n# {name}\n",
        encoding="utf-8",
    )


def _curate_t2_tasks(vault: Path, *names: str) -> None:
    rows = "".join(f"  - task: '[[task/{n}]]'\n    source: operator\n" for n in names)
    (vault / "daily" / "2026-08-19.md").write_text(
        "---\ntype: daily\ndate: '2026-08-19'\n"
        "tier_curation:\n  t1: []\n  t2:\n"
        f"{rows}"
        "  t3: []\n  curated_at: '2026-08-19T07:00:00-03:00'\n"
        "---\n\n# daily\n",
        encoding="utf-8",
    )


def _entry(view, name: str):
    for lane in (view.t1, view.t2, view.t3):
        for e in lane:
            if e.name == name:
                return e
    raise AssertionError(f"{name!r} not in any lane")


# ---------------------------------------------------------------------------
# 1. Hydration — the operator's screenshot, reproduced and closed
# ---------------------------------------------------------------------------


def test_curated_dated_task_classifies_as_duty(tmp_path: Path) -> None:
    """THE REPORTED CASE. A task due Friday, curated into T2 on Wednesday.

    Rule 6 says a dated task is Duty. Before the hydration fix this reported
    ``unslotted / no_signal``, because the curated converter never read the due
    date off the record — which is what put four correctly-encoded items under
    "NOT SORTED YET" with nothing to do about it.
    """
    vault = _vault(tmp_path)
    _task(vault, "Call Carfax", f"due: {FRIDAY}\n")
    _curate_t2_tasks(vault, "Call Carfax")

    e = _entry(compute_today_view(vault, NOW), "Call Carfax")
    assert e.due_iso == FRIDAY
    assert e.slot == slots.SLOT_DUTY
    assert e.slot_rule == slots.RULE_DATED_TASK


def test_curated_task_honours_an_explicit_slot(tmp_path: Path) -> None:
    """Rule 1 — "the operator's word is final" — must survive curation.

    This is the half that makes the sort affordance work at all: the writer
    records the ruling as ``slot:`` on the record, and if curation drops it on
    the way back the operator taps a control that appears to do nothing.
    ``fuel`` deliberately, NOT ``duty``: a dated task would reach Duty anyway via
    rule 6, so pinning ``duty`` here would pass without rule 1 ever firing.
    """
    vault = _vault(tmp_path)
    _task(vault, "Practice guitar", f"due: {FRIDAY}\nslot: fuel\n")
    _curate_t2_tasks(vault, "Practice guitar")

    e = _entry(compute_today_view(vault, NOW), "Practice guitar")
    assert e.slot == slots.SLOT_FUEL
    assert e.slot_rule == slots.RULE_EXPLICIT


def test_hydration_does_not_invent_a_slot(tmp_path: Path) -> None:
    """NEGATIVE CONTROL, and the one that matters most.

    An undated task with no ``slot:`` and no ``self_care`` has NO signal, and
    the honest answer is still ``unslotted``. Without this, "hydrate the curated
    entry" could be satisfied by defaulting everything into Duty — a surface
    showing a confident answer it does not have, which is exactly what
    ``slots.py`` refuses to do and exactly the disease this whole lane is
    treating. It is also what keeps the sort rotation's population non-empty and
    therefore meaningful.
    """
    vault = _vault(tmp_path)
    _task(vault, "Someday thing", "")
    _curate_t2_tasks(vault, "Someday thing")

    e = _entry(compute_today_view(vault, NOW), "Someday thing")
    assert e.slot == slots.SLOT_UNSLOTTED
    assert e.slot_rule == slots.RULE_NONE


def test_hydration_survives_an_unreadable_record(tmp_path: Path) -> None:
    """A curated entry whose task file is gone must not take the board down.

    The entry keeps its pre-hydration shape (``unslotted``) and the projection
    still returns every lane. Silent by design — see ``_hydrate_curated_entries``
    — because a missing hint is never worth the operator's morning.
    """
    vault = _vault(tmp_path)
    _curate_t2_tasks(vault, "Never existed")

    view = compute_today_view(vault, NOW)
    e = _entry(view, "Never existed")
    assert e.slot == slots.SLOT_UNSLOTTED
    assert view.slot_coverage.total == 1


# ---------------------------------------------------------------------------
# 2. The writer — end to end, on the surface the operator reads
# ---------------------------------------------------------------------------


def test_sorting_a_task_moves_it_on_the_board(tmp_path: Path) -> None:
    """THE END-TO-END PIN. Not "a file changed" — the BOARD changed.

    Drives the whole arrangement the operator sees: an unslotted curated task,
    one ``assign_slot`` call, then a fresh projection. The pre-assertion is what
    stops this passing vacuously against a build where the item was already Duty.
    """
    vault = _vault(tmp_path)
    _task(vault, "Someday thing", "")
    _curate_t2_tasks(vault, "Someday thing")

    before = _entry(compute_today_view(vault, NOW), "Someday thing")
    assert before.slot == slots.SLOT_UNSLOTTED  # the state under repair

    result = assign_slot(
        vault, origin="task", slot="rhythm", path="task/Someday thing.md",
    )
    assert result.kind == SORT_KIND_SUCCESS
    assert result.changed is True

    after = _entry(compute_today_view(vault, NOW), "Someday thing")
    assert after.slot == slots.SLOT_RHYTHM
    assert after.slot_rule == slots.RULE_EXPLICIT


def test_sorting_preserves_the_rest_of_the_record(tmp_path: Path) -> None:
    """The write is one field. Frontmatter and body both survive it.

    ``due`` is compared through ``str()`` on purpose: PyYAML gives back a
    ``datetime.date`` for an UNQUOTED ISO date and a ``str`` for a quoted one,
    so both shapes reach this assertion in practice and the string form is the
    one the rest of the tier layer works in (``due.isoformat()`` everywhere).
    What is being pinned is that the value SURVIVES the round-trip — a writer
    that dropped or reformatted it would break the ``dated_task`` rule for every
    task the operator ever sorts.
    """
    vault = _vault(tmp_path)
    _task(vault, "Call Carfax", f"due: {FRIDAY}\nwaiting_on: Carfax\n")

    assign_slot(vault, origin="task", slot="duty", path="task/Call Carfax.md")

    post = frontmatter.load(str(vault / "task" / "Call Carfax.md"))
    assert post.metadata["slot"] == "duty"
    assert str(post.metadata["due"]) == FRIDAY
    assert post.metadata["waiting_on"] == "Carfax"
    assert post.metadata["status"] == "todo"
    assert "# Call Carfax" in post.content


def test_sorting_a_dated_task_leaves_it_reachable_by_rule_six(tmp_path: Path) -> None:
    """The round-trip's consequence, on the surface rather than in the file.

    A sort rewrites the whole frontmatter block through ``yaml.dump``. If that
    round-trip mangled ``due`` — reformatted it, stringified it into something
    ``date.fromisoformat`` chokes on — the task would still LOOK fine in the
    file and would silently stop being a dated task everywhere downstream. This
    drives the projection after the write to prove it did not.
    """
    vault = _vault(tmp_path)
    _task(vault, "Call Carfax", f"due: {FRIDAY}\n")
    _curate_t2_tasks(vault, "Call Carfax")

    assign_slot(vault, origin="task", slot="fuel", path="task/Call Carfax.md")

    e = _entry(compute_today_view(vault, NOW), "Call Carfax")
    assert e.due_iso == FRIDAY
    # Rule 3/1 outranks rule 6, so the explicit ruling wins — but the DATE is
    # still there and still read, which is the property under test.
    assert e.slot == slots.SLOT_FUEL


def test_sorting_a_routine_item_writes_that_item_only(tmp_path: Path) -> None:
    """The routine lane writes the per-ITEM field, not the record's.

    The sibling item is asserted UNTOUCHED — a writer that stamped the whole
    record would satisfy any assertion that only looked at the target.
    """
    vault = _vault(tmp_path)
    _routine(
        vault, "Household",
        "items:\n"
        "- text: Water the plants\n  priority: tracked\n"
        "- text: Sort the recycling\n  priority: tracked\n",
    )

    result = assign_slot(
        vault, origin="routine_item", slot="routine",  # operator's alias for rhythm
        routine_record="Household", item_text="Water the plants",
    )
    assert result.kind == SORT_KIND_SUCCESS
    assert result.slot == slots.SLOT_RHYTHM  # the alias normalised

    items = frontmatter.load(str(vault / "routine" / "Household.md")).metadata["items"]
    by_text = {i["text"]: i for i in items}
    assert by_text["Water the plants"]["slot"] == "rhythm"
    assert "slot" not in by_text["Sort the recycling"]


def test_re_sorting_is_allowed(tmp_path: Path) -> None:
    """Sorting is reversible, which is why the verbs are light and unarmed."""
    vault = _vault(tmp_path)
    _task(vault, "Call Carfax", f"due: {FRIDAY}\n")

    assign_slot(vault, origin="task", slot="duty", path="task/Call Carfax.md")
    again = assign_slot(vault, origin="task", slot="fuel", path="task/Call Carfax.md")

    assert again.kind == SORT_KIND_SUCCESS
    post = frontmatter.load(str(vault / "task" / "Call Carfax.md"))
    assert post.metadata["slot"] == "fuel"


def test_sorting_to_the_same_slot_is_an_idempotent_noop(tmp_path: Path) -> None:
    """A double-tap writes nothing and still reports an ok end-state."""
    vault = _vault(tmp_path)
    _task(vault, "Call Carfax", "slot: duty\n")
    before = (vault / "task" / "Call Carfax.md").read_text(encoding="utf-8")

    result = assign_slot(vault, origin="task", slot="duty", path="task/Call Carfax.md")

    assert result.kind == SORT_KIND_IDEMPOTENT_NOOP
    assert result.ok is True
    assert result.changed is False
    assert (vault / "task" / "Call Carfax.md").read_text(encoding="utf-8") == before


def test_an_operator_alias_is_idempotent_against_its_canonical(tmp_path: Path) -> None:
    """``slot: routine`` on the record IS Rhythm, so sorting it to Rhythm is a
    noop rather than a rewrite. Comparing raw strings would have written."""
    vault = _vault(tmp_path)
    _task(vault, "Call Carfax", "slot: routine\n")

    result = assign_slot(vault, origin="task", slot="rhythm", path="task/Call Carfax.md")

    assert result.kind == SORT_KIND_IDEMPOTENT_NOOP
    assert frontmatter.load(
        str(vault / "task" / "Call Carfax.md")
    ).metadata["slot"] == "routine"


# ---------------------------------------------------------------------------
# 3. Refusals — asserted by WHY, each with a positive control
# ---------------------------------------------------------------------------


def _events(captured, name: str) -> list[dict]:
    return [c for c in captured if c.get("event") == name]


def test_an_unrecognised_slot_is_refused_by_the_slot_guard(tmp_path: Path) -> None:
    """Asserts the GUARD fired, not merely that nothing happened.

    A bogus slot and a missing record both return ``ok=False`` with the record
    untouched, so the result alone cannot tell them apart — a pin that checked
    only the refusal would be green against a build with no slot guard at all.
    The logged event is the discriminator.

    Positive control in the same test: a canonical slot on the SAME record
    writes, so this is not passing because the writer is inert.
    """
    vault = _vault(tmp_path)
    _task(vault, "Call Carfax", "")

    with structlog.testing.capture_logs() as captured:
        bad = assign_slot(
            vault, origin="task", slot="urgent", path="task/Call Carfax.md",
        )
    refusals = _events(captured, "tier.sort.invalid_slot")
    assert len(refusals) == 1
    assert refusals[0]["slot"] == "urgent"
    assert bad.kind == SORT_KIND_INVALID_SLOT
    assert "slot" not in frontmatter.load(
        str(vault / "task" / "Call Carfax.md")
    ).metadata

    good = assign_slot(vault, origin="task", slot="duty", path="task/Call Carfax.md")
    assert good.kind == SORT_KIND_SUCCESS


def test_a_free_text_entry_is_refused_for_having_no_record(tmp_path: Path) -> None:
    """A curated free-text T3 intention has ``origin='routine_item'`` with no
    record and no item text. It is refused as THIN EVIDENCE — the reason names
    the absent record, not a bad slot.

    Positive control: the same call with a real record+text succeeds.
    """
    vault = _vault(tmp_path)
    _routine(vault, "Household", "items:\n- text: Water the plants\n")

    with structlog.testing.capture_logs() as captured:
        thin = assign_slot(
            vault, origin="routine_item", slot="fuel",
            routine_record=None, item_text=None,
        )
    events = _events(captured, "tier.sort.thin_evidence")
    assert len(events) == 1
    assert "no backing record" in events[0]["reason"]
    assert thin.kind == SORT_KIND_THIN_EVIDENCE

    ok = assign_slot(
        vault, origin="routine_item", slot="fuel",
        routine_record="Household", item_text="Water the plants",
    )
    assert ok.kind == SORT_KIND_SUCCESS


def test_a_renamed_routine_item_is_refused_as_unknown_item(tmp_path: Path) -> None:
    """Exact match only. A near-miss is a dead end, never a fuzzy write onto
    whichever item happened to look closest.

    Positive control: the verbatim text on the same record succeeds.
    """
    vault = _vault(tmp_path)
    _routine(vault, "Household", "items:\n- text: Water the plants\n")

    with structlog.testing.capture_logs() as captured:
        miss = assign_slot(
            vault, origin="routine_item", slot="rhythm",
            routine_record="Household", item_text="water the plants",  # cased differently
        )
    assert len(_events(captured, "tier.sort.unknown_item")) == 1
    assert miss.kind == SORT_KIND_UNKNOWN_ITEM

    hit = assign_slot(
        vault, origin="routine_item", slot="rhythm",
        routine_record="Household", item_text="Water the plants",
    )
    assert hit.kind == SORT_KIND_SUCCESS


def test_a_path_escape_is_refused_and_touches_nothing(tmp_path: Path) -> None:
    """The containment guard, asserted as DEBRIS-FREE.

    "Did it write the target file" is the wrong question — the right one is
    whether the refused call touched ANYTHING outside the vault. A refused write
    that still created a lock file out there would pass a file-contents
    assertion and fail this one.

    Positive control: an in-vault path on the same call shape writes.
    """
    vault = _vault(tmp_path)
    _task(vault, "Call Carfax", "")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Secret.md").write_text("---\ntype: task\n---\n\nsecret\n", encoding="utf-8")
    before = sorted(p.name for p in outside.iterdir())

    with structlog.testing.capture_logs() as captured:
        escaped = assign_slot(
            vault, origin="task", slot="duty", path="../outside/Secret.md",
        )
    assert len(_events(captured, "tier.sort.path_escape_denied")) == 1
    assert escaped.kind == SORT_KIND_UNKNOWN_RECORD
    assert sorted(p.name for p in outside.iterdir()) == before
    assert "slot" not in frontmatter.load(str(outside / "Secret.md")).metadata

    inside = assign_slot(vault, origin="task", slot="duty", path="task/Call Carfax.md")
    assert inside.kind == SORT_KIND_SUCCESS


def test_a_missing_task_record_is_refused_as_unknown_record(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    _task(vault, "Call Carfax", "")

    with structlog.testing.capture_logs() as captured:
        gone = assign_slot(vault, origin="task", slot="duty", path="task/Gone.md")
    assert len(_events(captured, "tier.sort.unknown_record")) == 1
    assert gone.kind == SORT_KIND_UNKNOWN_RECORD

    assert assign_slot(
        vault, origin="task", slot="duty", path="task/Call Carfax.md",
    ).kind == SORT_KIND_SUCCESS


# ---------------------------------------------------------------------------
# 4. The act path — the wire both surfaces fire
# ---------------------------------------------------------------------------


def _act(store, cfg, feed_id: str, action_id: str, vault_path: Path):
    from alfred.daily_sync.action_router import act

    return act(
        feed_id, action_id,
        feed_store=store, config=cfg, vault_path=vault_path,
        instance_name="salem", instance_scope="talker",
    )


def _slot_card(store, *, name: str = "Someday thing", state: str | None = None):
    from alfred.feed import FeedItem

    item = FeedItem.create(
        kind="slot_suggestion",
        stable_key=f"task:task/{name}.md",
        instance="salem",
        title=f"T2: {name}",
        evidence={
            "tier": 2, "origin": "task", "name": name,
            "path": f"task/{name}.md", "slot": "unslotted", "slot_rule": "no_signal",
        },
    )
    store.upsert(item)
    if state is not None:
        store.set_state(item.id, state)
    return item


def _ds_config(tmp_path: Path):
    from alfred.daily_sync.config import DailySyncConfig

    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    return cfg


def test_the_act_path_really_writes_the_slot(tmp_path: Path) -> None:
    """THE WIRE PIN — drive ``act``, not the writer.

    This is the call the board's Sort control and any future deck card both
    make, and it is the one that would go silently dead if the dispatcher were
    unwired (the interception deleted, the verb dropped from the ceiling). The
    assertion is on the RECORD and then on the PROJECTION, so a dispatcher that
    returned a cheerful ok without writing anything fails here.
    """
    from alfred.feed import FeedStore

    vault = _vault(tmp_path)
    _task(vault, "Someday thing", "")
    _curate_t2_tasks(vault, "Someday thing")
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path)
    card = _slot_card(store)

    assert _entry(compute_today_view(vault, NOW), "Someday thing").slot == "unslotted"

    result = _act(store, cfg, card.id, "sort_rhythm", vault)

    assert result.ok is True
    assert result.status == "sorted"
    assert result.render == {"slot": "rhythm", "sorted": True}
    assert _entry(compute_today_view(vault, NOW), "Someday thing").slot == "rhythm"


def test_sorting_does_not_decide_the_card(tmp_path: Path) -> None:
    """A sort leaves the feed item OPEN — it is not a decision about the card.

    If a sort marked the item ``acted`` it would drop off the board the moment
    the operator placed it, which reads as "you dealt with this" about something
    still sitting there undone. That is the same lying-affordance class the lane
    is closing, so it gets its own pin rather than riding on the wire test.
    """
    from alfred.feed import FeedStore
    from alfred.feed.model import STATE_OPEN

    vault = _vault(tmp_path)
    _task(vault, "Someday thing", "")
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path)
    card = _slot_card(store)

    assert _act(store, cfg, card.id, "sort_duty", vault).ok is True

    stored = store.load()[card.id]
    assert stored.state == STATE_OPEN
    assert getattr(stored, "acted_action", None) is None


def test_an_accepted_card_is_still_sortable(tmp_path: Path) -> None:
    """EXCEPTION 3 of the folded-state gate, driven.

    An accepted item is ``state=acted`` and is still ON the board today, so it
    can sit in the "Not sorted yet" stack exactly like an open one. Refusing to
    sort it would rebuild the dead end for the population most likely to hit it
    — the items the operator has already committed to.

    Positive control on the same card: a verb that IS finished for an acted item
    still answers ``already_acted``, so this is not passing because the gate
    stopped working altogether.
    """
    from alfred.feed import FeedStore
    from alfred.feed.model import STATE_ACTED

    vault = _vault(tmp_path)
    _task(vault, "Someday thing", "")
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path)
    card = _slot_card(store, state=STATE_ACTED)

    assert _act(store, cfg, card.id, "sort_fuel", vault).ok is True
    assert frontmatter.load(
        str(vault / "task" / "Someday thing.md")
    ).metadata["slot"] == "fuel"

    assert _act(store, cfg, card.id, "accept", vault).status == "already_acted"


def test_a_retired_card_is_not_sortable(tmp_path: Path) -> None:
    """RETIRED stays refused. The producer withdrew it, so the entry is not in
    today's projection at all and its record may be closed or gone — writing a
    slot onto that is a guess about something the system stopped tracking.

    Positive control: the same card, open, sorts.
    """
    from alfred.feed import FeedStore
    from alfred.feed.model import STATE_OPEN, STATE_RETIRED

    vault = _vault(tmp_path)
    _task(vault, "Someday thing", "")
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path)
    card = _slot_card(store, state=STATE_RETIRED)

    refused = _act(store, cfg, card.id, "sort_duty", vault)
    assert refused.ok is False
    assert refused.status == "retired"
    assert "slot" not in frontmatter.load(
        str(vault / "task" / "Someday thing.md")
    ).metadata

    store.set_state(card.id, STATE_OPEN)
    assert _act(store, cfg, card.id, "sort_duty", vault).ok is True


def test_a_sort_can_never_reach_a_completion_or_accept_writer() -> None:
    """STRUCTURAL, in the same shape as the snooze pin above it.

    "Sorting never fakes a commitment" is enforced by the dispatcher being
    separate, not by convention — so this reads the dispatcher's own code object
    and asserts none of the completion/accept writers is reachable from it.
    """
    from alfred.daily_sync.action_router import _dispatch_slot_sort

    names = set(_dispatch_slot_sort.__code__.co_names)
    for consts in _dispatch_slot_sort.__code__.co_consts:
        if isinstance(consts, tuple):
            names |= {str(c) for c in consts}
    forbidden = {
        "mark_task_done", "confirm_slot_candidate", "_dispatch_slot_completion",
        "_dispatch_slot_confirm", "_dispatch_slot_snooze", "set_state",
    }
    assert not (names & forbidden), f"sort path can reach: {names & forbidden}"


def test_the_free_text_refusal_reaches_the_operator_as_a_sentence(
    tmp_path: Path,
) -> None:
    """The unwritable shape, driven through ``act`` rather than the writer.

    A curated free-text T3 card carries no path and no routine record. The
    operator gets a sentence naming WHAT is missing, not a bare failure — and
    ``unsupported_item`` (422) rather than ``invalid_action`` (400), because the
    verb was fine and the item is the thing that cannot hold an answer.
    """
    from alfred.feed import FeedItem, FeedStore

    vault = _vault(tmp_path)
    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path)
    card = FeedItem.create(
        kind="slot_suggestion",
        stable_key="text:Read for an hour",
        instance="salem",
        title="T3: Read for an hour",
        evidence={"tier": 3, "origin": "routine_item", "name": "Read for an hour"},
    )
    store.upsert(card)

    result = _act(store, cfg, card.id, "sort_fuel", vault)

    assert result.ok is False
    assert result.status == "unsupported_item"
    assert "no record behind it" in result.detail


# ---------------------------------------------------------------------------
# 5. Cross-language drift — the client's copy of the verb map
# ---------------------------------------------------------------------------


def test_the_web_sort_map_matches_the_backend_ceiling() -> None:
    """CROSS-SURFACE DRIFT PIN, server half.

    The PWA hardcodes its own ``SORT_ACTION_BY_SLOT`` because TypeScript cannot
    import a Python dict AND because the tap has to name a verb before any
    response exists to derive it from — the same reason the snooze ladder keeps
    a copy. So the two can drift in silence, and neither side can notice alone:
    a verb here the client never sends is a capability nobody can reach, and a
    verb the client sends that the ceiling refuses 400s in the operator's hand.

    Deliberately PARSED from the other language's source rather than restated —
    writing the expected pairs here would create a third copy to drift. Both
    directions are asserted: same slots, same verbs, no extras either way.

    Mutation: rename a verb on either side alone → this reds and names it.
    """
    import re

    from alfred.daily_sync.action_router import SORT_ACTION_BY_SLOT

    ts = (
        Path(__file__).resolve().parents[2]
        / "web" / "lib" / "algernon" / "feedConstants.ts"
    ).read_text(encoding="utf-8")
    block = re.search(
        r"export const SORT_ACTION_BY_SLOT[^=]*=\s*\{(.*?)\}", ts, re.S,
    )
    assert block, "SORT_ACTION_BY_SLOT not found in feedConstants.ts"
    web_map = dict(re.findall(r"(\w+):\s*'([^']+)'", block.group(1)))

    # Positive control: the parse really found something, so an expression that
    # silently matched an empty block cannot make the comparison vacuous.
    assert len(web_map) == 3, web_map
    assert {v: k for k, v in SORT_ACTION_BY_SLOT.items()} == web_map
