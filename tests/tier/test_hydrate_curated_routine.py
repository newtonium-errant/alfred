"""Curated ROUTINE-origin hydration — accept must not cost an item its ring.

THE REPORTED CASE (measured on the live box 2026-08-21). The operator accepted
``Hot Tub Chemistry`` into today's board and it landed in no ring at all::

    NAME: Hot Tub Chemistry
    tier=3  origin=routine_item  source=operator
    classify_slot -> slot=unslotted  rule=no_signal
    explicit_slot=None  self_care=False  has_due_pattern=False
    target_cadence_days=None  gap_escalated=False

while ``routine/Core Daily.md`` carried, on that very item, BOTH
``slot: rhythm`` (rule 1) and ``target_cadence_days: 1`` (rule 5). Neither
reached the classifier: ``_hydrate_curated_entries`` repaired task-origin rows
only, and the two curated routine converters build an entry with no classifier
inputs at all.

The fixtures below use the operator's real record and item names because a
fixture shaped for convenience tests a vault that does not exist — and because
the shape that made this bug invisible is specifically the FREE-TEXT one:
:class:`alfred.tier.daily_curation.T3Entry` has no record field, so an accepted
T3 card arrives back with ``routine_record=None`` and the anchor gone. A pin
built on the anchored (record+text) shape alone would pass against a build that
still leaves the reported row unslotted.

Two pins here are shaped to be uncheatable rather than merely green:

  * the keystone asserts the curated projection EQUALS the un-curated one on
    every hydrated field — a build that defaulted rows into a ring would fail
    it, and so would one that hydrated the wrong item;
  * every refusal pin asserts the logged REASON, not just the refusal. A
    denial for an unrelated cause (no record, no text, empty index) presents
    identically to the guard firing; only the reason field tells them apart.

Discipline per ``tests/tier/test_slot_classifier.py``: every classification pin
drives ``compute_today_view`` (or ``render_tier_section``) and asserts the
STAMPED result. None construct a TierEntry for the subject under test.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import structlog

from alfred.brief.tier_section import (
    T3_AUTO_DAYS_SINCE_NEVER_LABEL,
    render_tier_section,
)
from alfred.tier import slots
from alfred.tier.compute import compute_today_view

# Friday 2026-08-21 13:00 UTC — the day the bug was measured.
NOW = datetime(2026, 8, 21, 13, 0, 0, tzinfo=timezone.utc)
TODAY_ISO = "2026-08-21"

# The operator's item, transcribed from ``vault/routine/Core Daily.md`` rather
# than composed here — the fixture's literals are SAMPLED from the record that
# produced the bug (record name, item text, field values, spellings).
HOT_TUB_ITEM = (
    "  - priority: tracked\n"
    "    text: Hot Tub Chemistry\n"
    "    warn_after_gap_days: 3\n"
    "    target_cadence_days: 1\n"
    "    slot: rhythm\n"
)
# Last done 2 days before NOW: cadence 1 is overdue (so the auto lane surfaces
# it) AND days-since is a real number, which is what makes the "never done"
# render pin below able to fail.
HOT_TUB_LOG = "completion_log:\n  Hot Tub Chemistry:\n    - 2026-08-19\n"


def _vault(tmp_path: Path, name: str = "vault") -> Path:
    vault = tmp_path / name
    (vault / "task").mkdir(parents=True, exist_ok=True)
    (vault / "routine").mkdir(parents=True, exist_ok=True)
    (vault / "daily").mkdir(parents=True, exist_ok=True)
    return vault


def _write_routine(
    vault: Path, name: str, body: str, *, status: str = "active",
    extra_fm: str = "",
) -> None:
    """``body`` is raw frontmatter YAML below ``type``/``name``/``status``."""
    (vault / "routine" / f"{name}.md").write_text(
        f"---\ntype: routine\nname: {name}\nstatus: {status}\n"
        f"cadence:\n  type: daily\n{extra_fm}{body}---\n\n# {name}\n",
        encoding="utf-8",
    )


def _core_daily(vault: Path, *, items: str = HOT_TUB_ITEM, **kw) -> None:
    _write_routine(vault, "Core Daily", f"items:\n{items}{HOT_TUB_LOG}", **kw)


def _curate(vault: Path, *, t1: str = "[]", t2: str = "[]", t3: str = "[]") -> None:
    """Write today's ``tier_curation`` block. Lane arguments are raw YAML."""
    (vault / "daily" / f"{TODAY_ISO}.md").write_text(
        "---\ntype: daily\n"
        f"date: '{TODAY_ISO}'\n"
        "tier_curation:\n"
        f"  t1: {t1}\n"
        f"  t2: {t2}\n"
        f"  t3: {t3}\n"
        f"  curated_at: '{TODAY_ISO}T07:00:00-03:00'\n"
        "---\n\n# daily\n",
        encoding="utf-8",
    )


def _free_text_t3(*texts: str) -> str:
    """The accepted-card shape: ``tier_confirm`` writes T3 as bare free text
    with ``source: operator`` and NO record anchor (see ``_COMMITTED_SOURCE``
    / :class:`T3Entry`), which is exactly why the row loses its inputs."""
    return "\n" + "".join(
        f"    - item: {t}\n      source: operator\n" for t in texts
    )


def _anchored_t1(record: str, text: str, *, source: str = "auto-due-routine") -> str:
    """The anchored shape: ``tier_confirm`` preserves the ORIGINAL auto source
    on a confirmed T1, which is why a curated row can carry ``auto-*`` and
    still be a curated (blind) construction."""
    return (
        "\n    - routine_item:\n"
        f"        record: {record}\n"
        f"        text: {text}\n"
        f"      source: {source}\n"
        "      confirmed: true\n"
    )


def _entries(view) -> dict[str, object]:
    out = {}
    for lane in (view.t1, view.t2, view.t3):
        for e in lane:
            out[e.name] = e
    return out


def _entry(view, name: str):
    e = _entries(view).get(name)
    assert e is not None, (
        f"{name!r} never surfaced; visible: {sorted(_entries(view))}"
    )
    return e


def _events(captured, event: str) -> list[dict]:
    return [c for c in captured if c.get("event") == event]


# ---------------------------------------------------------------------------
# 1. The reported case
# ---------------------------------------------------------------------------


def test_accepted_free_text_t3_keeps_its_ring(tmp_path: Path) -> None:
    """THE ACCEPTANCE TEST. ``slot: rhythm`` sat inert on the operator's box.

    Asserted as an EQUALITY against the un-accepted projection of the same
    vault, not as a bare ``== rhythm``. Accept is supposed to be
    slot-preserving; stating it that way makes the pin fail for a build that
    defaults rows into a ring (the equality would hold but the control's own
    value is checked too) AND for a build that hydrates the wrong item.
    """
    accepted = _vault(tmp_path, "accepted")
    _core_daily(accepted)
    _curate(accepted, t3=_free_text_t3("Hot Tub Chemistry"))

    untouched = _vault(tmp_path, "untouched")
    _core_daily(untouched)  # same record, no curation block at all

    got = _entry(compute_today_view(accepted, NOW), "Hot Tub Chemistry")
    control = _entry(compute_today_view(untouched, NOW), "Hot Tub Chemistry")

    # The control is the item the operator was looking at BEFORE he accepted it.
    assert control.source == "auto-cadence-routine"
    assert control.slot == slots.SLOT_RHYTHM
    assert control.slot_rule == slots.RULE_EXPLICIT

    # The accepted copy is the same item; accepting it changed its provenance
    # and nothing else about where it belongs in the day.
    assert got.source == "operator"
    assert got.slot == slots.SLOT_RHYTHM
    assert got.slot_rule == slots.RULE_EXPLICIT
    assert (got.slot, got.slot_rule, got.explicit_slot,
            got.target_cadence_days, got.days_since_last_completed) == (
        control.slot, control.slot_rule, control.explicit_slot,
        control.target_cadence_days, control.days_since_last_completed,
    )


def test_curated_routine_item_reaches_rhythm_by_cadence_alone(
    tmp_path: Path,
) -> None:
    """THE CADENCE PATH, pinned separately from the explicit one.

    ``target_cadence_days`` with NO ``slot:`` must reach Rhythm via rule 5 —
    the ruling that this field IS hydratable from the item (unlike from a
    record, where it would borrow a sibling's cadence). Without a separate pin
    the explicit-slot test above would cover for it: rule 1 answers first and
    rule 5 could be dead.

    The same test proves the field is what carries it: strip the cadence from
    the record and the identical curated row goes honestly unslotted.
    """
    item = (
        "  - priority: tracked\n"
        "    text: Hot Tub Chemistry\n"
        "    target_cadence_days: 1\n"
    )
    with_cadence = _vault(tmp_path, "with")
    _core_daily(with_cadence, items=item)
    _curate(with_cadence, t3=_free_text_t3("Hot Tub Chemistry"))

    without = _vault(tmp_path, "without")
    _core_daily(
        without,
        items="  - priority: tracked\n    text: Hot Tub Chemistry\n",
    )
    _curate(without, t3=_free_text_t3("Hot Tub Chemistry"))

    hydrated = _entry(compute_today_view(with_cadence, NOW), "Hot Tub Chemistry")
    assert hydrated.target_cadence_days == 1
    assert hydrated.slot == slots.SLOT_RHYTHM
    assert hydrated.slot_rule == slots.RULE_CADENCE

    bare = _entry(compute_today_view(without, NOW), "Hot Tub Chemistry")
    assert bare.target_cadence_days is None
    assert bare.slot == slots.SLOT_UNSLOTTED
    assert bare.slot_rule == slots.RULE_NONE


# ---------------------------------------------------------------------------
# 2. The anchored (record + text) shape — the RRTS Payroll sibling
# ---------------------------------------------------------------------------


def test_curated_t1_routine_item_honours_an_explicit_slot(
    tmp_path: Path,
) -> None:
    """The anchored curated shape, which loses its inputs the same way.

    ``slot: fuel`` deliberately, NOT ``duty``: this item carries a
    ``due_pattern``, so rule 4 would reach Duty on its own and a ``duty`` pin
    would pass with rule 1 dead. Fuel can only come from the operator's word.

    ``source: auto-due-routine`` is the real shape too — a confirmed T1 keeps
    its original auto source (``_t12_source_confirmed``), so the source string
    is NOT a discriminator for "curated".
    """
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Core Daily",
        "items:\n"
        "  - priority: tracked\n"
        "    text: RRTS Payroll\n"
        "    slot: fuel\n"
        "    due_pattern:\n      type: monthly\n      day: 21\n"
        "    escalate_at_days: 1\n",
    )
    _curate(vault, t1=_anchored_t1("Core Daily", "RRTS Payroll"))

    e = _entry(compute_today_view(vault, NOW), "RRTS Payroll")
    assert e.source == "auto-due-routine"  # curated copy won dedup
    assert e.confirmed is True
    assert e.has_due_pattern is True
    assert e.slot == slots.SLOT_FUEL
    assert e.slot_rule == slots.RULE_EXPLICIT


def test_curated_t1_routine_item_reaches_duty_by_due_pattern(
    tmp_path: Path,
) -> None:
    """Rule 4 on the hydrated path — the structural half of the pin above.

    Same record, ``slot:`` removed: a recurring hard deadline is a scheduled
    obligation, and ``has_due_pattern`` is the field that has to survive
    curation for that to be true.
    """
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Core Daily",
        "items:\n"
        "  - priority: tracked\n"
        "    text: RRTS Payroll\n"
        "    due_pattern:\n      type: monthly\n      day: 21\n"
        "    escalate_at_days: 1\n",
    )
    _curate(vault, t1=_anchored_t1("Core Daily", "RRTS Payroll"))

    e = _entry(compute_today_view(vault, NOW), "RRTS Payroll")
    assert e.slot == slots.SLOT_DUTY
    assert e.slot_rule == slots.RULE_DUE_PATTERN


def test_self_care_outranks_cadence_on_a_hydrated_row(tmp_path: Path) -> None:
    """Rule 3 over rule 5, on an item that hydrates BOTH.

    "Guitar, every 3 days" is Fuel that happens to be scheduled, not a practice
    that happens to be pleasant (slots.py's own words). Both fields have to
    arrive for the precedence to be exercised at all — a build that hydrated
    only the cadence would answer Rhythm here and look fine everywhere else.
    """
    vault = _vault(tmp_path)
    _write_routine(
        vault, "For Self Health",
        "items:\n"
        "  - priority: aspirational\n"
        "    text: Practice guitar\n"
        "    self_care: true\n"
        "    target_cadence_days: 3\n",
    )
    _curate(vault, t3=_free_text_t3("Practice guitar"))

    e = _entry(compute_today_view(vault, NOW), "Practice guitar")
    assert e.self_care is True
    assert e.target_cadence_days == 3
    assert e.slot == slots.SLOT_FUEL
    assert e.slot_rule == slots.RULE_SELF_CARE


def test_gap_escalation_still_outranks_a_hydrated_explicit_slot(
    tmp_path: Path,
) -> None:
    """Rule 0 over rule 1 — now that rule 1 is REACHABLE on this path.

    Before hydration a curated routine row had no ``explicit_slot``, so the
    precedence could not be tested from a projection at all. It can now, and
    the direction is load-bearing: the record's ``slot: fuel`` says what the
    item IS, the escalation says where a neglected one presses today.
    """
    from alfred.routine.config import TierDefaultsConfig

    vault = _vault(tmp_path)
    _write_routine(
        vault, "For Self Health",
        "items:\n"
        "  - priority: aspirational\n"
        "    text: Walk Fergus\n"
        "    slot: fuel\n"
        "    target_cadence_days: 1\n"
        "completion_log:\n  Walk Fergus:\n    - 2026-08-18\n",  # gap 3
    )
    _curate(
        vault,
        t1=_anchored_t1("For Self Health", "Walk Fergus",
                        source="auto-gap-escalated"),
    )

    e = _entry(
        compute_today_view(vault, NOW, TierDefaultsConfig(
            fuel_escalate_after_gap_days=3,
        )),
        "Walk Fergus",
    )
    assert e.explicit_slot == "fuel"  # hydration DID run
    assert e.gap_escalated is True
    assert e.slot == slots.SLOT_DUTY
    assert e.slot_rule == slots.RULE_GAP_ESCALATED


# ---------------------------------------------------------------------------
# 3. Refusals — each with its positive control and its logged reason
# ---------------------------------------------------------------------------


def test_hydration_does_not_invent_a_slot_for_a_routine_item(
    tmp_path: Path,
) -> None:
    """NEGATIVE CONTROL, with its positive control in the same projection.

    An item with a text and a priority and nothing else has NO signal, and the
    honest answer is ``unslotted``. "Hydrate the curated entry" must not be
    satisfiable by defaulting everything into a ring. The sibling item in the
    SAME record proves the read is live — without it this pin passes
    identically against a build where hydration does nothing at all.
    """
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Core Daily",
        "items:\n"
        "  - priority: tracked\n    text: Something vague\n"
        "  - priority: tracked\n    text: Hot Tub Chemistry\n"
        "    slot: rhythm\n",
    )
    _curate(vault, t3=_free_text_t3("Something vague", "Hot Tub Chemistry"))

    view = compute_today_view(vault, NOW)
    vague = _entry(view, "Something vague")
    assert vague.slot == slots.SLOT_UNSLOTTED
    assert vague.slot_rule == slots.RULE_NONE
    # POSITIVE CONTROL — same record, same projection, hydration demonstrably
    # working. The residue above is a fact about the item, not about the reader.
    assert _entry(view, "Hot Tub Chemistry").slot == slots.SLOT_RHYTHM


def test_a_renamed_item_announces_itself(tmp_path: Path) -> None:
    """THE NAMED FAILURE MODE. A renamed item hydrates from nothing.

    Silence here is indistinguishable from "the record genuinely says nothing",
    which is precisely the ambiguity intentionally-left-blank exists to kill.
    The pin asserts the logged REASON, not merely that the row is unslotted:
    an unslotted row is what a build with NO hydration produces too.
    """
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Core Daily",
        "items:\n"
        "  - priority: tracked\n    text: Hot Tub Chemistry\n"
        "    slot: rhythm\n",
    )
    # The curated row still names the OLD text; the record was renamed under it.
    _curate(
        vault,
        t1=_anchored_t1("Core Daily", "Hot tub chemicals"),
        t3=_free_text_t3("Hot Tub Chemistry"),
    )

    with structlog.testing.capture_logs() as captured:
        view = compute_today_view(vault, NOW)

    stale = _entry(view, "Hot tub chemicals")
    assert stale.slot == slots.SLOT_UNSLOTTED

    events = _events(captured, "tier.hydrate.routine_item_unresolved")
    assert len(events) == 1
    assert events[0]["reason"] == "no_such_item"
    assert events[0]["record"] == "Core Daily"
    assert events[0]["item_text"] == "Hot tub chemicals"
    assert events[0]["log_level"] == "warning"
    # POSITIVE CONTROL — the record IS readable and the live row hydrated.
    assert _entry(view, "Hot Tub Chemistry").slot == slots.SLOT_RHYTHM


def test_a_renamed_record_has_its_own_reason(tmp_path: Path) -> None:
    """``unknown_record`` vs ``no_such_item`` — different causes, different
    fixes, so the signal must not collapse them. A record renamed under a
    curated row is a different morning's work from an item renamed inside one.
    """
    vault = _vault(tmp_path)
    _core_daily(vault)
    _curate(vault, t1=_anchored_t1("Core Dailies", "Hot Tub Chemistry"))

    with structlog.testing.capture_logs() as captured:
        compute_today_view(vault, NOW)

    events = _events(captured, "tier.hydrate.routine_item_unresolved")
    assert len(events) == 1
    assert events[0]["reason"] == "unknown_record"
    assert events[0]["record"] == "Core Dailies"
    assert events[0]["records_scanned"] == 1


def test_ambiguous_free_text_refuses_to_guess_and_says_so(
    tmp_path: Path,
) -> None:
    """Two records claiming the same item text is not a resolvable question.

    A free-text row has no anchor, so "which record's cadence applies" has two
    answers here and picking one is the sibling-contamination failure the task
    path refuses by design. The row stays unslotted, the WARN names BOTH
    candidates, and an unambiguous row in the SAME projection still hydrates —
    the refusal is about this text, not a dead reader.
    """
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Core Daily",
        "items:\n"
        "  - priority: tracked\n    text: Hot Tub Chemistry\n"
        "    slot: rhythm\n"
        "  - priority: tracked\n    text: Sweep the deck\n"
        "    slot: duty\n",
    )
    _write_routine(
        vault, "Weekend Chores",
        "items:\n"
        "  - priority: tracked\n    text: Hot Tub Chemistry\n"
        "    slot: duty\n",
    )
    _curate(vault, t3=_free_text_t3("Hot Tub Chemistry", "Sweep the deck"))

    with structlog.testing.capture_logs() as captured:
        view = compute_today_view(vault, NOW)

    contested = _entry(view, "Hot Tub Chemistry")
    assert contested.slot == slots.SLOT_UNSLOTTED
    assert contested.explicit_slot is None  # refused, not coin-flipped

    events = _events(captured, "tier.hydrate.routine_item_unresolved")
    assert len(events) == 1
    assert events[0]["reason"] == "ambiguous_text"
    assert events[0]["candidates"] == ["Core Daily", "Weekend Chores"]
    assert events[0]["log_level"] == "warning"
    # POSITIVE CONTROL — an unambiguous free-text row in the same view.
    assert _entry(view, "Sweep the deck").slot == slots.SLOT_DUTY


def test_ad_hoc_free_text_is_reported_but_not_warned(tmp_path: Path) -> None:
    """"Read for an hour" is the ORDINARY shape of the free-text lane.

    It matches no routine item because it is not one. Warning on it would make
    the rename WARN fire every single morning on genuine intentions, and an
    alarm that cries wolf daily is worse than no alarm. It still emits — under
    its OWN event name, at INFO — so the lane is observable without diluting
    the anomaly grep.
    """
    vault = _vault(tmp_path)
    _core_daily(vault)
    _curate(vault, t3=_free_text_t3("Read for an hour"))

    with structlog.testing.capture_logs() as captured:
        view = compute_today_view(vault, NOW)

    assert _entry(view, "Read for an hour").slot == slots.SLOT_UNSLOTTED
    info = _events(captured, "tier.hydrate.free_text_no_routine_match")
    assert len(info) == 1
    assert info[0]["reason"] == "no_routine_match"
    assert info[0]["record"] == ""  # ILB: present-and-empty, never absent
    assert info[0]["item_text"] == "Read for an hour"
    assert info[0]["log_level"] == "info"
    assert _events(captured, "tier.hydrate.routine_item_unresolved") == []


def test_archived_record_does_not_supply_a_slot(tmp_path: Path) -> None:
    """Hydration reads the vault the PROJECTION reads, not the filesystem.

    An archived record is invisible to every auto lane; letting it hydrate a
    curated row would put a ring on an item no surface would ever have
    produced. Two projections over the same bytes, differing only in
    ``status:`` — which is what makes the exclusion a fact about the filter
    rather than about the fixture.
    """
    archived = _vault(tmp_path, "archived")
    _core_daily(archived, status="archived")
    _curate(archived, t3=_free_text_t3("Hot Tub Chemistry"))

    active = _vault(tmp_path, "active")
    _core_daily(active, status="active")
    _curate(active, t3=_free_text_t3("Hot Tub Chemistry"))

    assert _entry(
        compute_today_view(archived, NOW), "Hot Tub Chemistry",
    ).slot == slots.SLOT_UNSLOTTED
    assert _entry(
        compute_today_view(active, NOW), "Hot Tub Chemistry",
    ).slot == slots.SLOT_RHYTHM


def test_triage_record_does_not_supply_a_slot(tmp_path: Path) -> None:
    """The other record-level filter every auto lane applies.

    Same two-projection shape as the archived pin: ``alfred_triage: true`` is a
    janitor-generated record, out of scope for the tier surfaces, and so out of
    scope for hydration.
    """
    triaged = _vault(tmp_path, "triaged")
    _core_daily(triaged, extra_fm="alfred_triage: true\n")
    _curate(triaged, t3=_free_text_t3("Hot Tub Chemistry"))

    normal = _vault(tmp_path, "normal")
    _core_daily(normal)
    _curate(normal, t3=_free_text_t3("Hot Tub Chemistry"))

    assert _entry(
        compute_today_view(triaged, NOW), "Hot Tub Chemistry",
    ).slot == slots.SLOT_UNSLOTTED
    assert _entry(
        compute_today_view(normal, NOW), "Hot Tub Chemistry",
    ).slot == slots.SLOT_RHYTHM


def test_hydration_survives_an_unreadable_routine_record(
    tmp_path: Path,
) -> None:
    """A record mid-write must not take the morning board down.

    The broken record is skipped and the READABLE one still hydrates in the
    same projection — the positive control that separates "tolerated the
    corruption" from "gave up on the whole scan".
    """
    vault = _vault(tmp_path)
    _core_daily(vault)
    (vault / "routine" / "Half Written.md").write_text(
        "---\ntype: routine\nname: Half Written\nitems:\n  - text: [unclosed\n",
        encoding="utf-8",
    )
    _curate(vault, t3=_free_text_t3("Hot Tub Chemistry"))

    view = compute_today_view(vault, NOW)
    assert _entry(view, "Hot Tub Chemistry").slot == slots.SLOT_RHYTHM
    assert view.slot_coverage.total == 1


# ---------------------------------------------------------------------------
# 4. Boundaries — what hydration must NOT do
# ---------------------------------------------------------------------------


def test_hydration_never_re_anchors_a_free_text_row(tmp_path: Path) -> None:
    """Classifier inputs only. Identity is not hydrated, and that is a rule.

    Stamping the resolved record onto a free-text row would move its dedup key
    AND move its done-state home from the entry's own ``done_at`` to that
    record's ``completion_log`` — and ``done_at`` is documented as the ONLY
    done-state home for a free-text T3 item. The slot fix must not quietly
    become a done-state change.
    """
    vault = _vault(tmp_path)
    _core_daily(vault)
    _curate(vault, t3=_free_text_t3("Hot Tub Chemistry"))

    e = _entry(compute_today_view(vault, NOW), "Hot Tub Chemistry")
    assert e.slot == slots.SLOT_RHYTHM  # hydration ran
    assert e.routine_record is None     # and did not re-anchor
    assert e.path == "routine/"
    assert e.done_at is None


def test_hydration_does_not_overwrite_a_value_the_converter_set(
    tmp_path: Path,
) -> None:
    """Fill-only-if-empty, on the one routine field a curated row can arrive
    carrying: ``gap_escalated`` is stamped from the auto map BEFORE hydration
    runs, and an unconditional hydrate would have to fight it. Here the record
    says ``slot: fuel`` and the item is NOT neglected, so the stamp is False and
    stays False — the pin fails if hydration starts writing that fact.
    """
    vault = _vault(tmp_path)
    _write_routine(
        vault, "For Self Health",
        "items:\n"
        "  - priority: aspirational\n    text: Walk Fergus\n"
        "    slot: fuel\n    target_cadence_days: 1\n"
        "completion_log:\n  Walk Fergus:\n    - 2026-08-21\n",  # done today
    )
    _curate(vault, t3=_free_text_t3("Walk Fergus"))

    e = _entry(compute_today_view(vault, NOW), "Walk Fergus")
    assert e.gap_escalated is False
    assert e.slot == slots.SLOT_FUEL
    assert e.slot_rule == slots.RULE_EXPLICIT


# ---------------------------------------------------------------------------
# 5. The render — the cadence pair travels together or the brief lies
# ---------------------------------------------------------------------------


def test_a_hydrated_cadence_row_does_not_claim_never_done(
    tmp_path: Path,
) -> None:
    """THE PAIRING PIN. ``target_cadence_days`` without days-since renders
    "never done".

    ``_row_annotation`` falls back to ``T3_AUTO_DAYS_SINCE_NEVER_LABEL`` when a
    cadence row carries no days-since — true for a genuinely never-completed
    item, and a confident lie for one done the day before yesterday. Hydrating
    the target alone would have put that lie on the operator's morning, so the
    two fields are hydrated together and this pin is why.
    """
    vault = _vault(tmp_path)
    _core_daily(vault)  # last done 2026-08-19, NOW is 2026-08-21
    _curate(vault, t3=_free_text_t3("Hot Tub Chemistry"))

    e = _entry(compute_today_view(vault, NOW), "Hot Tub Chemistry")
    assert e.target_cadence_days == 1
    assert e.days_since_last_completed == 2

    rendered = render_tier_section(vault, NOW)
    row = [ln for ln in rendered.splitlines() if "Hot Tub Chemistry" in ln]
    assert row, f"item not rendered at all:\n{rendered}"
    assert "2 days since last; target every 1d" in row[0]
    assert T3_AUTO_DAYS_SINCE_NEVER_LABEL not in row[0]


def test_a_never_completed_hydrated_row_still_says_never_done(
    tmp_path: Path,
) -> None:
    """The other half — the label is CORRECT for an item with no completions,
    so the pin above must not be satisfiable by suppressing it. Same fixture
    minus the completion log."""
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Core Daily",
        "items:\n"
        "  - priority: tracked\n    text: Hot Tub Chemistry\n"
        "    target_cadence_days: 1\n",
    )
    _curate(vault, t3=_free_text_t3("Hot Tub Chemistry"))

    e = _entry(compute_today_view(vault, NOW), "Hot Tub Chemistry")
    assert e.days_since_last_completed is None

    rendered = render_tier_section(vault, NOW)
    row = [ln for ln in rendered.splitlines() if "Hot Tub Chemistry" in ln]
    assert row, f"item not rendered at all:\n{rendered}"
    assert T3_AUTO_DAYS_SINCE_NEVER_LABEL in row[0]


# ---------------------------------------------------------------------------
# 6. Intentionally left blank
# ---------------------------------------------------------------------------


def test_summary_is_emitted_on_a_board_with_nothing_to_hydrate(
    tmp_path: Path,
) -> None:
    """"The hydrator found nothing to do" and "the hydrator stopped running"
    are the same picture without this line — and the rename WARN above is only
    trustworthy if its ABSENCE means something. Emitted unconditionally, zeros
    included, the way ``slots.log_coverage`` is one caller up."""
    vault = _vault(tmp_path)
    _core_daily(vault)  # a record exists; nothing is curated

    with structlog.testing.capture_logs() as captured:
        compute_today_view(vault, NOW)

    summary = _events(captured, "tier.hydrate.routine_summary")
    assert len(summary) == 1
    assert summary[0]["considered"] == 0
    assert summary[0]["hydrated"] == 0
    assert summary[0]["unmatched"] == 0
    assert summary[0]["ambiguous"] == 0
    # No routine-origin row needed anything, so the index was never built —
    # reported as 0 scanned rather than as a number nobody measured.
    assert summary[0]["records_scanned"] == 0


def test_summary_counts_every_outcome(tmp_path: Path) -> None:
    """The rollup is a real count, not a constant. One row of each outcome in
    a single projection: hydrated / unmatched (rename) / ambiguous."""
    vault = _vault(tmp_path)
    _write_routine(
        vault, "Core Daily",
        "items:\n"
        "  - priority: tracked\n    text: Hot Tub Chemistry\n"
        "    slot: rhythm\n"
        "  - priority: tracked\n    text: Sweep the deck\n    slot: duty\n",
    )
    _write_routine(
        vault, "Weekend Chores",
        "items:\n  - priority: tracked\n    text: Sweep the deck\n"
        "    slot: fuel\n",
    )
    _curate(
        vault,
        t1=_anchored_t1("Core Daily", "Long gone"),
        t3=_free_text_t3("Hot Tub Chemistry", "Sweep the deck"),
    )

    with structlog.testing.capture_logs() as captured:
        compute_today_view(vault, NOW)

    summary = _events(captured, "tier.hydrate.routine_summary")[0]
    assert summary["considered"] == 3
    assert summary["hydrated"] == 1    # Hot Tub Chemistry
    assert summary["unmatched"] == 1   # Long gone
    assert summary["ambiguous"] == 1   # Sweep the deck, claimed by two records
    assert summary["records_scanned"] == 2
