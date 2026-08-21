"""Standing pins for the vault-talker SKILL's derivation contract.

WHY THIS FILE EXISTS. The ``Today's Plan`` sample block is a COPY. Its
population is exactly two surfaces: the ``alfred.brief.tier_section`` module
docstring, and the SKILL. The docstring gained a byte-pin on 2026-08-21; this
file is the SKILL's half, so neither copy can drift silently. Leaving exactly
one unpinned copy is the worst configuration available, because a reader who
knows "the sample is pinned" will generalise it to the copy that is not.

Two DIFFERENT failure modes are pinned here, and they are not interchangeable:

  1. CITATION ROT (``test_dotted_citations_resolve`` /
     ``test_unqualified_derivation_names_live_in_tier_section``). The SKILL
     tells a model to RE-DERIVE FROM THE FUNCTION. If a cited construct is
     renamed away, that instruction points at nothing and the model invents.
     This is not hypothetical: the SKILL carried ``_render_rollover_section``
     for months after it was split into ``compute_rollover`` +
     ``_render_unplaced_carryover``, and nothing caught it, because the only
     check anyone ran was on the LINE NUMBERS, which were being renumbered
     against a function that no longer existed.

  2. SAMPLE DRIFT (``test_sample_block_matches_emitters``). The block is
     asserted to be byte-identical to what the real emitters produce.

Citations in that note deliberately carry NO line numbers — a coordinate is a
fact with an expiry date and any parallel lane is when it expires. Names are
the durable key, which is precisely why the names need a pin: the whole scheme
rests on them resolving.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from alfred._data import get_skills_dir
from alfred.brief import tier_section as ts
from alfred.tier import day_plan as dp
from alfred.tier import slots as slotmod
from alfred.tier.compute import DailyGoalState, TierEntry

SKILL_PATH = get_skills_dir() / "vault-talker" / "SKILL.md"

#: Names the derivation note uses BARE, under its own standing sentence
#: ("every name below is in ``alfred.brief.tier_section`` unless qualified").
#: A bare name cannot be auto-resolved without knowing that default module, so
#: it is enumerated. Adding a bare name to the note means adding it here.
UNQUALIFIED_TIER_SECTION_NAMES = (
    "render_daily_goal_line",
    "_render_plan_row",
    "_row_head",
    "_row_annotation",
    "_row_affordance",
    "_format_due_date",
    "_render_routine_line",
    "_render_slot_group",
    "_render_day_plan",
    "_render_unplaced_carryover",
    "render_tier_section",
    "_render_t2_selection_pool",
    "compute_rollover",
    "ROLLOVER_HEADER",
    "T1_CONFIRM_PROMPT",
    "T2_EMPTY_PROMPT",
    "T3_EMPTY_PROMPT",
    "T2_POOL_HEADER",
    "T2_ROUTINE_CONFIRM_PROMPT",
    "T3_AUTO_ANNOTATION_TEMPLATE",
)

DOTTED = re.compile(r"`(alfred\.[A-Za-z0-9_.]+)`")


def _skill_text() -> str:
    assert SKILL_PATH.is_file(), (
        f"bundled talker SKILL not found at {SKILL_PATH} — the pin cannot "
        "silently pass when it cannot find its subject"
    )
    return SKILL_PATH.read_text(encoding="utf-8")


def _resolve(dotted: str) -> object:
    """Resolve ``alfred.a.b.C.d`` by importing the longest module prefix."""
    parts = dotted.split(".")
    module = None
    idx = 0
    for i in range(len(parts), 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:i]))
            idx = i
            break
        except ImportError:
            continue
    if module is None:
        raise AssertionError(f"no importable module prefix in {dotted!r}")
    obj = module
    for attr in parts[idx:]:
        obj = getattr(obj, attr)  # raises AttributeError -> test failure
    return obj


# --------------------------------------------------------------------------
# 1. citation rot
# --------------------------------------------------------------------------

def test_dotted_citations_resolve():
    """Every ``alfred.x.y`` the SKILL cites in backticks must resolve.

    The safety net that GROWS on its own: any citation a future edit adds in
    dotted form is covered without touching this file.
    """
    cited = sorted(set(DOTTED.findall(_skill_text())))
    assert cited, "found no dotted alfred.* citations — the scan is inert"
    broken = []
    for dotted in cited:
        try:
            _resolve(dotted)
        except (AssertionError, AttributeError) as exc:
            broken.append(f"{dotted} -> {type(exc).__name__}: {exc}")
    assert not broken, (
        "SKILL cites constructs that do not resolve. A model told to "
        "re-derive from these will find nothing and invent:\n  "
        + "\n  ".join(broken)
    )


def test_unqualified_derivation_names_live_in_tier_section():
    """The bare names in the derivation note must exist in tier_section.

    This is the pin that would have caught ``_render_rollover_section``.
    """
    missing = [n for n in UNQUALIFIED_TIER_SECTION_NAMES if not hasattr(ts, n)]
    assert not missing, (
        "the SKILL's derivation note names these as living in "
        f"alfred.brief.tier_section, but they do not: {missing}"
    )


def test_no_line_number_citations_into_tier_section():
    """The note's citations stay NAME-based.

    A line number reintroduced here would rot on the next parallel lane, which
    is the exact defect this contract was rewritten to remove.
    """
    text = _skill_text()
    offenders = re.findall(r"tier_section\.py:\d+", text)
    assert not offenders, (
        "line-number citations into tier_section.py reappeared in the talker "
        f"SKILL: {offenders}. Cite the CONSTRUCT, not the coordinate."
    )


# --------------------------------------------------------------------------
# 2. sample drift
# --------------------------------------------------------------------------

def _extract_sample_block() -> str:
    """Pull the fenced ``Today's Plan`` sample out of the SKILL.

    Anchored on the SECTION HEADING rather than on any line of the block, so
    the extractor cannot accidentally match a similarly-shaped block, and
    fails LOUDLY rather than returning nothing if the anchor moves.
    """
    lines = _skill_text().splitlines()
    try:
        anchor = next(i for i, l in enumerate(lines)
                      if l.startswith("#### The brief surface"))
        start = next(i for i in range(anchor, len(lines))
                     if lines[i].strip() == "```")
        end = next(i for i in range(start + 1, len(lines))
                   if lines[i].strip() == "```")
    except StopIteration:  # pragma: no cover - anchor-moved path
        pytest.fail(
            "could not locate the Today's Plan sample block in the talker "
            "SKILL. The anchor moved; re-point this extractor rather than "
            "deleting the pin."
        )
    return "\n".join(lines[start + 1:end]).rstrip("\n")


def _derive_sample_from_emitters() -> str:
    """Run the REAL emitters over a projection and compose as production does."""
    steph = TierEntry(
        tier=1, origin="task", name="Steph Yang ROE",
        path="task/Steph Yang ROE.md", due_iso="2026-08-12",
        surface_reason="due today", source="auto-due", confirmed=False,
    )
    qbo = TierEntry(
        tier=2, origin="task", name="Connect QBO API — RRTS",
        path="task/Connect QBO API — RRTS.md", due_iso="2026-08-24",
        source="operator",
    )
    plants = TierEntry(
        tier=2, origin="routine_item", name="Water the plants",
        path="routine/Weekly Chores.md", due_iso="2026-08-14",
        source="operator", routine_record="Weekly Chores",
        item_text="Water the plants", has_due_pattern=True,
    )
    # The ring is DERIVED by the real classifier, never asserted here — the
    # SKILL's sample is authority for RENDER SHAPE only, and this pin must not
    # quietly become a second (wrong) authority for ring assignment.
    for entry in (steph, qbo, plants):
        entry.slot = slotmod.classify_slot(
            entry, learned=slotmod.NoOverrides()
        ).slot

    morning_pages = SimpleNamespace(
        text="Morning pages", time="", priority="aspirational",
        annotation="*(3d since last; target every 5d)*", slot="rhythm",
    )
    view = SimpleNamespace(
        t1=[steph], t2=[qbo, plants], t3=[],
        routine_today=[morning_pages],
        daily_goal=DailyGoalState(
            t1_available=1, t2_available=2, t3_available=0,
            t1_done=0, t2_done=0, t3_done=0,
            balanced_day=False, all_t1_done=False,
        ),
    )
    rollover = [dp.RolloverRef(
        tier_label="T2", wikilink="[[task/Connect QBO API — RRTS]]",
        record_name="Connect QBO API — RRTS",
    )]
    plan = dp.build_day_plan(view, rollover=rollover, is_done=lambda e: False)

    goal_line = ts.render_daily_goal_line(view.daily_goal)
    day_plan_md = ts._render_day_plan(plan, list(rollover))
    pool = ts._render_t2_selection_pool(
        records=[
            (Path("task/RRTS Bug List — Burn Through.md"),
             {"status": "todo"}, "RRTS Bug List — Burn Through"),
            (Path("task/Set Up QuickBooks Online Developer Access for "
                  "RRTS Website.md"),
             {"status": "active"},
             "Set Up QuickBooks Online Developer Access for RRTS Website"),
        ],
        auto_t1_record_names={"Steph Yang ROE"},
        curated_t1_record_names=set(),
        curated_t2_record_names={"Connect QBO API — RRTS"},
    )
    # joined EXACTLY as render_tier_section joins them
    parts = [goal_line, "", day_plan_md, "---", "", pool]
    return "\n".join(parts).rstrip("\n")


def test_sample_block_matches_emitters():
    """The SKILL's sample is byte-identical to real emitter output.

    If this goes red after a deliberate formatter change, the fix is to
    RE-DERIVE the block (run the emitters, paste the output) — never to
    hand-patch the SKILL until the diff looks small, and never to copy the
    module docstring's block across, which is the copy-propagation this pin
    and the docstring's twin exist to stop.
    """
    assert _extract_sample_block() == _derive_sample_from_emitters()


def test_ring_assignments_in_the_sample_are_what_the_classifier_says():
    """The prose around the sample makes RING claims; drive them.

    Separate from the byte-pin on purpose: a block can be byte-perfect while
    the prose explaining WHY a row sits under a heading is wrong, and that
    prose is the part a model reasons from.
    """
    plants = TierEntry(
        tier=2, origin="routine_item", name="Water the plants",
        path="routine/Weekly Chores.md", due_iso="2026-08-14",
        routine_record="Weekly Chores", item_text="Water the plants",
        has_due_pattern=True,
    )
    verdict = slotmod.classify_slot(plants, learned=slotmod.NoOverrides())
    # The SKILL: "a routine with a due_pattern is a scheduled obligation" and
    # lands in Duty by rule 4 — NOT Rhythm.
    assert (verdict.slot, verdict.rule) == (slotmod.SLOT_DUTY,
                                            slotmod.RULE_DUE_PATTERN)

    dated_task = TierEntry(
        tier=1, origin="task", name="Steph Yang ROE",
        path="task/Steph Yang ROE.md", due_iso="2026-08-12",
    )
    v2 = slotmod.classify_slot(dated_task, learned=slotmod.NoOverrides())
    assert (v2.slot, v2.rule) == (slotmod.SLOT_DUTY, slotmod.RULE_DATED_TASK)
