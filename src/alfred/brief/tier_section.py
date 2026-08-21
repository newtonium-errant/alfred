"""Brief integration — render the "Today's Plan" section (V2).

Tier-V2 reframes tier as a **daily curation ritual** stored in
``vault/daily/<date>.md`` (the ``tier_curation`` frontmatter block —
see :mod:`alfred.tier.daily_curation`). This module reads that block
plus the open-task pool and composes a two-section render: **curated
shortlists** at the top + **materials** (T2 selection pool + rollover
from yesterday's incomplete) below.

The V1 surface (per-task ``base_tier``/``escalate_to`` projection
through ``compute_effective_tier``) is gone from this module and from
:mod:`alfred.tier.compute` itself (Ship 3 atomic drop, 2026-05-29 —
last-consumer-rewrite ratified pattern #22). The ``base_tier`` /
``escalate_to`` fields were removed from the schema surface 2026-06-25
(routine-systems consolidation Step 1); the ~24 stale records are
being stripped, not backfilled, so the once-deferred "Ship 5 backfill"
is moot. The migration script ``scripts/migrate_tier_phase1.py`` (which
populated those fields) is ARCHIVED as a completed one-time migration.

Phase C (2026-08-12) turned this section into the BOARD'S MORNING PROJECTION.
It is grouped by SLOT (Duty / Rhythm / Fuel) rather than by tier, carryover
renders first inside its slot, and the brief's former standalone "Today's
Routines" section dissolved into it — today's habit anchors render beside the
tier rows in the slot they belong to. Both this render and the briefing
player's spoken ``day_plan`` segment format ONE shared projection
(:mod:`alfred.tier.day_plan`), so the read and spoken plans cannot disagree.

The daily-goal line at the top is still TIER-based (``balanced_day`` counts
one-done in each of T1/T2/T3). Grouping by slot is an ARRANGEMENT of the same
rows, not a new target — which is why every row keeps a visible ``[Tn]`` tag.
See :mod:`alfred.tier.day_plan`: no copy anywhere may claim a slot-based goal
while the metric is tier-based.

Render shape (the section body — the brief renderer wraps it under
``## Today's Plan``: :mod:`alfred.brief.renderer` emits ``f"## {section_name}"``
over this module's :data:`SECTION_HEADER`):

WHAT THIS BLOCK IS AUTHORITY FOR — RENDER SHAPE, AND NOTHING ELSE. It is NOT
authority for RING ASSIGNMENT: which slot a row belongs to is decided by
:func:`alfred.tier.slots.classify_slot` and by nothing else, so these headers
are only where these particular example rows happen to land. Read the rule
ladder in ``slots.py`` before concluding anything about WHY a row sits under a
heading. TWO prompt lanes have lifted rows out of this block as if the headers
were the rule, and this is the THIRD round of defects found in it — two counts,
deliberately kept apart, because collapsing them is the same overstatement this
block keeps being fixed for. If you are reaching for a row, this is for you.

BYTE-VERIFIED OUTPUT, not a transcription. The block was produced by projecting
a :class:`~alfred.tier.compute.TodayView` (built over the four inputs named in
the enumeration) through :func:`alfred.tier.day_plan.build_day_plan`, rendering
it through :func:`render_daily_goal_line`, :func:`_render_day_plan` and
:func:`_render_t2_selection_pool`, joining those exactly as
:func:`render_tier_section` does (``parts = [goal_line, "", day_plan_md, "---",
"", pool]``), and byte-comparing the result against these lines. Every blank
line below is REAL: an earlier revision elided them "for compactness", which is
indistinguishable from never having verified them. If any formatter changes,
RE-DERIVE from the emitters — do not copy this block, and do not copy it INTO
another surface. Two mechanisms have produced defects here, and they are
different: READING a sample instead of driving it, and COPY-PROPAGATION between
this docstring and the vault-talker SKILL (the SKILL's own derivation note
records the second, having found a near-copy of this block carrying two of its
render-shape defects).

    **Daily goal — balanced day:** not yet · T1 0/1 · T2 0/2 · T3 0/0

    ### Duty

    - [T2] [[task/Connect QBO API — RRTS]] — due Tue Aug 11  *(carried from yesterday)*
    - [T2] Water the plants (from [[routine/Weekly Chores]]) — due Fri Aug 14
    - [T1] [[task/Steph Yang ROE]] — due Wed Aug 12 (due today)  *(confirm? reply "T1 confirm")*

    ### Rhythm

    *Today's rhythm items:*

    - Morning pages *(3d since last; target every 5d)*

    ### Fuel

    *(nothing in Fuel today)*

    ### Rollover from yesterday (incomplete)

    *(everything carried over is on today's plan above, marked where it sits)*

    *(empty — pick from Aspirational routines below or add new — reply "T3 add walk Fergus")*

    ---

    ### T2 selection pool
    (open `todo`/`active` tasks, NOT auto-T1, NOT alfred_triage)

    - [[task/RRTS Bug List — Burn Through]]

Enumeration — EVERY claim the block makes, and how each was determined. The
rows do not state their own shapes, which is why READING them has failed three
times; each claim below was DRIVEN instead. Note the shape of the failure: a
per-row enumeration that truthfully covered every ROW is still blind to a
property of the BLOCK, so the row drives come first and the block-level claims
that no row drive can see come after them.

INPUTS, and the ring each DERIVES (run through the real ``classify_slot`` with
the real ``NoOverrides``; verdicts quoted as ``slot/slot_rule``):

  * ``[T1] [[task/Steph Yang ROE]]`` — ``origin='task'`` (the ``task/``
    wikilink), ``due_iso='2026-08-12'``, ``surface_reason='due today'``,
    ``source='auto-due'``, unconfirmed. A deadline-bearing entry carries BOTH
    ``due_iso`` and ``surface_reason``, which is why the annotation takes the
    reason-and-due branch of :func:`_row_annotation` — ``— due Wed Aug 12 (due
    today)`` — and not a bare ``— due today``, which no formatter can emit.
    Unconfirmed + an ``auto-*`` source makes it a CANDIDATE, which is what
    earns it the :data:`T1_CONFIRM_PROMPT`. → **duty/dated_task** (rule 6).
  * ``[T2] [[task/Connect QBO API — RRTS]]`` — ``origin='task'``,
    ``due_iso='2026-08-11'``, ``source='operator'``, and present in the
    rollover by record name, so ``build_day_plan`` marks it ``carryover``.
    :func:`_row_annotation` ALWAYS emits an annotation when ``due_iso`` is set,
    so a row printed without one is a row with NO due date — and an undated
    task classifies **unslotted/no_signal** (rule 7), not Duty. It carries a
    due date for exactly that reason. → **duty/dated_task** (rule 6).
  * ``[T2] Water the plants (from [[routine/Weekly Chores]])`` — the
    ``(from [[routine/...]])`` head means ``origin='routine_item'`` with
    ``routine_record='Weekly Chores'``; ``has_due_pattern=True``, and
    ``source='operator'`` makes it COMMITTED rather than offered, which is why
    it carries no affordance. A routine with a hard recurring deadline is a
    scheduled obligation, so it sits under Duty, NOT under Rhythm. →
    **duty/due_pattern** (rule 4). The surprising one, and the reason it stays.
  * ``Morning pages`` — not a tier row at all but a
    :class:`~alfred.tier.compute.RoutineLine` habit anchor, rendered under
    :data:`ROUTINES_SUBHEADER` by :func:`_render_routine_line` off the routine
    aggregator's soft-cadence annotation. The aggregator's ``*(Nd since last;
    target every Nd)*`` SHORT form differs from the plan-row
    :data:`T3_AUTO_ANNOTATION_TEMPLATE`'s ``N days`` LONG form; they are not
    interchangeable. Cadence target, no due pattern → rule 5, emitting
    **rhythm/target_cadence_days** (the rule string is ``RULE_CADENCE``'s
    VALUE, not the constant's name).

  CONTROLS on those four verdicts, so "every row lands where it is printed" is
  distinguishable from a classifier that answers Duty to everything: strip the
  QBO row's ``due_iso`` and it returns **unslotted/no_signal**; strip the
  plants row's ``has_due_pattern`` and it returns **unslotted/no_signal** too
  (rule 6 cannot rescue it — a routine item is not a ``task``). Both controls
  move, and the four verdicts span two different slots.

BLOCK-LEVEL claims — the ones no per-row drive can see:

  * ROW ORDER inside a slot is **carryover → committed → suggestions**
    (``SlotGroup.rows``), NOT tier order and not authoring order. That is why
    the carried ``[T2]`` prints ABOVE the ``[T1]`` candidate: the thing that
    already cost a day leads, and the yes/no offers come last.

    The history here is stated in SHAs rather than in "rounds", because TWO
    successive corrections to this very bullet were each wrong about which
    round was which — the labels are the part that kept being false, so they
    are gone. Measured: at ``50f05b73^`` this slot held 2 rows (the plants row
    sat under Rhythm); ``50f05b73`` moved plants in, making it 3 in tier
    order; and ``c01d119d`` is byte-identical to ``50f05b73`` on this file
    (``git log 50f05b73..c01d119d -- src/alfred/brief/tier_section.py`` is
    empty). The ``[T1]`` candidate printed above the ``[T2]`` carryover at ALL
    THREE trees. So the order defect survived the very commit that ADDED a row
    to this slot — which is the strongest available evidence for the point:
    an ordering bug is invisible to a per-row check by construction, at any
    length.
  * THE GOAL LINE is :func:`render_daily_goal_line` over
    ``TodayView.daily_goal``; see that function's own docstring for the full
    shape enumeration. Its counts here are consistent with the rows shown BY
    CONSTRUCTION — one T1 row, two T2 rows, no T3 row, nothing done ⇒
    ``T1 0/1 · T2 0/2 · T3 0/0``. Earlier revisions printed a
    ``**Today's goal:** … ✓ … — one to go for a balanced day`` line, which is
    producible by NO emitter in this repo. The vault-talker SKILL had this
    right while this docstring was stale — the SECOND claim in this one block
    where the prompt layer held the correct copy, so do not assume this file
    wins a disagreement with it.
  * WHICH LINES EXIST AT ALL. The :data:`T3_EMPTY_PROMPT` line near the bottom
    is not decoration: :func:`_render_day_plan` appends it whenever the plan
    holds no T3 row, AFTER the rollover block, at the BOTTOM of the plan body
    — tiers no longer have headings to be empty under.
    :data:`T2_EMPTY_PROMPT` is absent because T2 has two rows.
  * HEADERS AND BLANK LINES. :func:`_render_slot_group` emits
    ``["### {label}", ""]`` unconditionally — hence a blank line after EVERY
    slot header, including the empty one — plus a trailing ``""`` closing each
    group. Fuel renders its :data:`SLOT_EMPTY_TEMPLATE` line because it is
    empty; Rhythm renders anchors with no tier rows because
    ``SlotGroup.is_empty`` is ``not rows and not routines``, so a slot holding
    only anchors is not empty. The ``unslotted`` residue group is absent
    because nothing landed in it.
  * THE ROLLOVER BLOCK sits ABOVE the ``---`` separator, between the last slot
    and the pool. It renders the "everything carried over is on today's plan
    above" sentinel rather than a list because the one rollover ref MATCHED a
    row on today's board, leaving ``plan.unplaced_carryover`` empty. Its three
    states are distinct — see :func:`_render_unplaced_carryover`.
  * THE POOL prints its parenthetical with SINGLE backticks and a blank line
    after it: ``_render_t2_selection_pool``'s ``out`` literal opens
    ``[T2_POOL_HEADER, "(open `todo`/`active` …)", ""]``. Those backticks are
    emitted bytes — do not "fix" them into RST double backticks, which an
    earlier revision did. This block is a verbatim sample, not prose.
  * TWO SPACES precede both the affordance and the carryover marker:
    :func:`_render_plan_row` joins its parts on ``" "`` while the affordance
    and marker parts are THEMSELVES ``f" {…}"``.

Read path (Step 2c, 2026-06-26 — the SINGLE computed view + materials):

  1. ``load_daily_curation(vault_path, today)`` — today's
     ``tier_curation`` block. ``None`` when un-curated yet
     (operator's "selection pool" mode); populated when talker has
     already curated.
  2. ``compute_today_view(vault_path, now)`` — THE single source of what
     surfaces / which lane (T1/T2/T3 lanes + the daily goal). This
     render layer no longer calls the ``compute_auto_*`` predicates
     directly; it slices the view's lanes (by origin + auto-source) into
     the candidate shapes the formatters consume, so no surface decision
     is re-derived here. The view merged curated + auto per lane via the
     single ``classify_routine_item`` predicate.
  3. ``load_daily_curation(vault_path, today - 1 day)`` — yesterday's
     curation, for rollover detection (:func:`compute_rollover`). Each
     yesterday-T1/T2 entry is checked against the current task record's
     status; incomplete entries either MARK their row on today's board
     (carryover, rendered first in its slot) or, when they are no longer on
     the board at all, list under :data:`ROLLOVER_HEADER`. (Render-only
     material — not a substrate lane assignment.)
  4. Open-task pool scan over ``vault/task/*.md`` for the T2 selection
     pool (status in OPEN_STATUSES, NOT ``alfred_triage``, NOT in
     today's auto-T1 set, NOT already-curated T1/T2). (Render-only
     material — not a substrate lane assignment.)

Cross-agent contract — operator-facing prompt phrases:

The :data:`T1_CONFIRM_PROMPT` / :data:`T2_EMPTY_PROMPT` /
:data:`T3_EMPTY_PROMPT` / :data:`ROLLOVER_HEADER` / :data:`T2_POOL_HEADER`
module-level constants are quoted verbatim by Ship 4's SKILL so the
talker recognises the operator-reply pattern. Renaming these here =
update SKILL in lockstep. Pinned via tests.

Read-side stability (CRITICAL for refresh): when the operator triggers
``/today`` or the brief regenerates mid-day, the curated shortlists
must be byte-stable as long as ``tier_curation`` hasn't changed. The
render is a pure projection over the block — no re-derivation, no
silent rewrites. The :func:`render_tier_section` signature stays the
same as V1 (``vault_path, now``) so the daemon + ``/today`` wiring
doesn't need to change.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import frontmatter  # type: ignore[import-untyped]
import structlog
import yaml

from alfred.tier.compute import (
    OPEN_STATUSES,
    AutoT1Candidate,
    AutoT3Candidate,
    DailyGoalState,
    TodayView,
    compute_today_view,
)
from alfred.tier.daily_curation import (
    DailyCuration,
    T1T2Entry,
    T3Entry,
    load_daily_curation,
)
from alfred.tier.day_plan import (
    DayPlan,
    PlanRow,
    RolloverRef,
    SlotGroup,
    build_day_plan_for_vault,
)

from .utils import SectionReadStatus, safe_read_section_file

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Section header — referenced by ``brief/daemon.py`` (``today_command.py``,
# its other consumer, died with the Telegram retirement — T5 2026-08-19).
# Single source of truth so a rename here propagates without grep-replace.
#
# Renamed from "Open Tasks by Tier" in Phase C (2026-08-12). Two reasons, both
# forced by the ratified content pass rather than chosen for taste:
#   * the section is no longer grouped BY TIER — it is the board's morning
#     projection, grouped by slot (Duty / Rhythm / Fuel), and a header that
#     names the wrong axis is the "comment lies about behaviour" trap in copy;
#   * it is no longer only TASKS — the standalone "Today's Routines" section
#     dissolved into it, so today's habit anchors render here too.
# Matches the narration's own ``day_plan`` slide title ("Today's plan"), so the
# read surface and the spoken surface name the same thing.
# ---------------------------------------------------------------------------

SECTION_HEADER = "Today's Plan"


# ---------------------------------------------------------------------------
# Operator-facing prompt phrases — CROSS-AGENT CONTRACT
#
# Ship 4 SKILL imports + quotes these verbatim so the talker recognises
# the canonical reply patterns ("T1 confirm", "T2 add ...", "T3 add ...").
# A rename here MUST be matched by a SKILL update in the same arc — code
# + prompt are two sides of the same contract.
# ---------------------------------------------------------------------------

T1_CONFIRM_PROMPT = '*(confirm? reply "T1 confirm")*'
T2_EMPTY_PROMPT = (
    '*(empty — reply "T2 add <items from selection pool below or anywhere>")*'
)
T3_EMPTY_PROMPT = (
    '*(empty — pick from Aspirational routines below or add new — '
    'reply "T3 add walk Fergus")*'
)
# ``ROLLOVER_HEADER``'s SCOPE NARROWED in Phase C, its STRING did not.
#
# Before: every one of yesterday's still-open commitments was listed under this
# header, in its own block at the bottom of the section. Now the ones that are
# also on today's board render INLINE in their slot, carryover-first — which is
# the whole point of a carryover-first morning. What stays under this header is
# the honest remainder: yesterday's commitments that are NOT on today's board at
# all, so there is no row to mark and no slot to place them in without guessing.
#
# The string is unchanged because the talker SKILL quotes it verbatim as a
# stable contract (see the cross-agent block above); the prompt-tuner pass that
# follows this merge is what re-voices it.
ROLLOVER_HEADER = "### Rollover from yesterday (incomplete)"
T2_POOL_HEADER = "### T2 selection pool"

# --- Phase C day-plan render strings ---------------------------------------
#
# The per-row carryover marker. Rendered on rows that ARE on today's board and
# were also open yesterday — the inline half of the rollover above.
CARRYOVER_MARKER = "*(carried from yesterday)*"

#: Header for the habit anchors inside a slot — what the dissolved "Today's
#: Routines" section used to hold. Rendered per-slot, only when that slot has
#: any (an empty slot's routine block would be noise, and the section-level ILB
#: sentinel already covers "nothing at all today").
ROUTINES_SUBHEADER = "*Today's rhythm items:*"

#: The section-level intentionally-left-blank line. Fires when the projection
#: arranged NOTHING — no rows, no routines, no rollover. Distinguishable from a
#: crash (no section at all) and from a quiet-but-live day (a slot renders its
#: own empty line).
PLAN_EMPTY_SENTINEL = (
    "*(nothing on today's plan yet — no tasks due, no routines firing, and "
    "nothing carried over)*"
)

#: Per-slot empty line. A slot with no rows is a real, reportable fact ("no
#: Fuel today" is exactly the imbalance the board exists to show), so it is
#: never silently omitted.
SLOT_EMPTY_TEMPLATE = "*(nothing in {label} today)*"

# Phase 2A Ship B (2026-05-29): routine-origin tier surfaces.
#
# T2 ramp items from routine due_patterns render in a subsection between
# the curated T2 bucket and the T2 selection pool. The auto-routine T2
# items aren't curated yet (operator hasn't confirmed) — the prompt
# names the canonical talker reply for confirmation.
T2_AUTO_ROUTINE_HEADER = "#### Auto-surfaced (from routines)"
T2_ROUTINE_CONFIRM_PROMPT = (
    '*(reply "T2 confirm" to keep on today\'s list)*'
)

# Phase 2A-soft-cadence (2026-05-30): T3 auto-suggest subsection
# constants — CROSS-AGENT CONTRACT (Phase 2B B1 SKILL quotes verbatim).
#
# T3 auto-suggestions surface routine items overdue against their soft
# cadence target (``target_cadence_days``). Distinct subsection inside
# the T3 bucket (after any curated T3 entries). Rendered ONLY when
# auto-T3 candidates exist — empty auto-T3 with populated curated T3
# is silently omitted (no spurious "auto-suggested: nothing" header).
# Empty curated T3 + empty auto-T3 falls through to the existing
# ``T3_EMPTY_PROMPT`` sentinel.
#
# ``T3_AUTO_CONFIRM_PROMPT`` is the talker reply pattern Phase 2B B1
# recognises (``T3 confirm <item text>``). The talker SKILL +
# ``routine_done`` tool path shipped 2026-05-30; the prompt is now
# actionable (pre-B1 the prompt was operator-axis only).
#
# ``T3_AUTO_TALKER_DEFERRED_NOTE`` is RETIRED as of Phase 2B B1.
# The constant is preserved for backwards-compat (downstream
# consumers may have grepped for it), but the brief render loop
# deliberately omits it from the output — the deferred-note copy
# is no longer accurate now that the talker companion has shipped.
# Per the ILB-acknowledgement-retirement pattern: when the deferred
# capability lands, retire the acknowledgement in the same ship.
#
# ``T3_AUTO_DAYS_SINCE_NEVER_LABEL`` is the per-item display string
# for items with empty completion_log (never completed). Keeps the
# render layer free of magic strings.
#
# ``T3_AUTO_ANNOTATION_TEMPLATE`` is the per-item annotation format
# string. ``{days_since}`` and ``{target}`` are the only fields;
# call site formats it via ``.format(...)``.
T3_AUTO_SECTION_HEADER = "#### Auto-suggested (from routine cadence)"
T3_AUTO_CONFIRM_PROMPT = (
    '*(reply "T3 confirm <item>" to add to today\'s T3)*'
)
T3_AUTO_DAYS_SINCE_NEVER_LABEL = "never done"
T3_AUTO_ANNOTATION_TEMPLATE = (
    "*({days_since} days since last; target every {target}d)*"
)
T3_AUTO_TALKER_DEFERRED_NOTE = (
    "*(talker T3 confirm grammar ships in Phase 2B B1; meanwhile use "
    '`alfred routine done "<item text>"` to mark complete, or '
    "edit the daily file directly.)*"
)


# ---------------------------------------------------------------------------
# YAML pre-validation — reused from V1 (python-frontmatter is lenient on
# bad YAML and silently returns empty metadata; we want the explicit raise
# so the parse-failed log line stays reachable).
# ---------------------------------------------------------------------------


def _validate_frontmatter_yaml(path: Path) -> str | None:
    """Pre-validate a record's YAML frontmatter block.

    Returns ``None`` when well-formed (or no frontmatter at all);
    returns a short error string on failure. ``python-frontmatter`` is
    lenient on invalid YAML — without this pre-pass, broken records
    would silently render as zero-fielded entries instead of triggering
    the parse_failed log line operators rely on. See V1's history at
    commit ``91504ea`` for the underlying gotcha.
    """
    # Defensive read via the shared helper — catches FileNotFoundError,
    # other OSError, AND UnicodeDecodeError uniformly (the last subclasses
    # ValueError, not OSError, so a bare ``except OSError`` misses it).
    read = safe_read_section_file(path)
    if read.status is SectionReadStatus.DECODE_ERROR:
        return f"not utf-8: {read.detail}"
    if read.status is not SectionReadStatus.OK:
        # NOT_FOUND + other OSError — same "read failed" message the prior
        # ``except OSError`` (which included FileNotFoundError) produced.
        return f"read failed: {read.detail}"
    text = read.text

    if not text.startswith("---"):
        return None

    lines = text.splitlines()
    if len(lines) < 2 or lines[0].strip() != "---":
        return "frontmatter opener malformed (no newline after leading ---)"

    close_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            close_idx = idx
            break

    if close_idx is None:
        return "frontmatter block not closed (no trailing --- found)"

    block = "\n".join(lines[1:close_idx])
    try:
        yaml.safe_load(block)
    except yaml.YAMLError as exc:
        first_line = (
            str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        )
        return f"yaml: {first_line}"

    return None


# ---------------------------------------------------------------------------
# Task-record iteration — yields (path, fm, name) tuples
# ---------------------------------------------------------------------------


def _iter_task_records(vault_path: Path) -> list[tuple[Path, dict, str]]:
    """Walk ``vault/task/*.md`` and yield non-broken task records.

    Filters at this layer:
      * Skip parse-failed records (logged at warning).
      * Skip non-task ``type:`` (logged at info — defensive against
        stray templates / janitor stubs).

    Does NOT filter by ``status`` here; callers filter at the
    bucket-population step so a future surface (e.g. ``alfred tier
    list``) could scan ALL tasks without re-walking.
    """
    task_dir = vault_path / "task"
    if not task_dir.is_dir():
        log.info(
            "brief.tier_section.no_task_dir",
            path=str(task_dir),
            detail=(
                "vault/task/ does not exist — selection pool will be empty."
            ),
        )
        return []

    out: list[tuple[Path, dict, str]] = []
    for path in sorted(task_dir.glob("*.md")):
        validation_error = _validate_frontmatter_yaml(path)
        if validation_error is not None:
            log.warning(
                "brief.tier_section.parse_failed",
                path=str(path),
                error=validation_error,
            )
            continue
        try:
            post = frontmatter.load(str(path))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "brief.tier_section.parse_failed",
                path=str(path),
                error=f"frontmatter.load: {exc}",
            )
            continue
        fm = dict(post.metadata or {})
        record_type = fm.get("type")
        if record_type != "task":
            log.info(
                "brief.tier_section.non_task_skipped",
                path=str(path),
                type=record_type,
            )
            continue
        name = str(fm.get("name") or path.stem)
        out.append((path, fm, name))
    return out


def _is_open(fm: dict[str, Any]) -> bool:
    """Return True if the task's status is in ``OPEN_STATUSES``.

    Missing ``status`` is treated as ``"todo"`` (forward-compat).
    """
    status = fm.get("status") or "todo"
    if not isinstance(status, str):
        return False
    return status.lower() in OPEN_STATUSES


def _format_due_date(due_iso: str) -> str:
    """Format an ISO date as ``"Mon Jun 1"``-style display string.

    Phase 2A Ship B (2026-05-29): routine-origin tier renders include
    the actual due date in the line head per the dispatch's worked
    example. Returns the raw ISO string on parse failure so the brief
    never silently swallows a date display.

    Example:
      ``_format_due_date("2026-06-01")`` → ``"Mon Jun 1"``
      ``_format_due_date("not-a-date")`` → ``"not-a-date"`` (fallback)
    """
    try:
        d = date.fromisoformat(due_iso)
    except (ValueError, TypeError):
        return due_iso
    # ``%a`` = abbreviated weekday (Mon), ``%b`` = abbreviated month
    # (Jun), ``%-d`` = day without leading zero. Use Python's
    # platform-neutral form (``%d`` then strip leading zero) since
    # ``%-d`` is Linux-only.
    weekday = d.strftime("%a")
    month = d.strftime("%b")
    day = str(d.day)  # no leading zero
    return f"{weekday} {month} {day}"


def _wikilink_to_record_name(wikilink: str) -> str | None:
    """Extract the record name from a ``[[task/Name]]`` wikilink.

    Returns ``None`` on malformed input (no ``[[…]]`` or no ``task/``
    prefix). Used to map curated T1/T2 ``task:`` strings back to the
    task pool for rollover-status checking + auto-T1 dedup.
    """
    if not isinstance(wikilink, str):
        return None
    s = wikilink.strip()
    if not (s.startswith("[[") and s.endswith("]]")):
        return None
    inner = s[2:-2].strip()
    if "/" not in inner:
        return None
    type_part, _, name_part = inner.partition("/")
    if type_part.strip() != "task":
        return None
    return name_part.strip()


# ---------------------------------------------------------------------------
# Curated-shortlist render
# ---------------------------------------------------------------------------


def _render_t2_entry(entry: T1T2Entry) -> str:
    """Render one curated T2 line — bare wikilink (or routine reference)
    with no confirm affordance.

    Origin discrimination matches :func:`_render_t1_entry` but without
    the confirm prompt (T2 entries are operator-curated; the add itself
    is the confirmation).
    """
    if entry.routine_item is not None:
        record = str(entry.routine_item.get("record", ""))
        text = str(entry.routine_item.get("text", ""))
        return f"- {text} (from [[routine/{record}]])"
    return f"- {entry.task or ''}"


def _render_t3_entry(entry: T3Entry, today_iso: str | None = None) -> str:
    """Render one T3 line — bare free-text item (no confirm affordance).

    Note T3 entries carry ``item:`` (free-text) not ``task:`` (wikilink).

    Arc #20: a checked-off ad-hoc item (``done_at`` set) renders ✓-struck
    (``- ~~item~~ ✓``). The morning brief is a day snapshot, so a done T3
    stays VISIBLE as progress toward the balanced-day goal — contrast
    ``/today`` (:func:`render_curated_tier_section_for_today`), which
    DROPS done T3 to keep the committed "what's on my plate" view clean.

    #20 P5 NOTE-2 (same-render-date guard): a daily file SHOULD only ever
    carry a same-day ``done_at`` (T3 never rolls over, and ``tier_done``
    back-dates into the completion date's OWN file). This render no longer
    TRUSTS that invariant — when the caller threads the render date
    (``today_iso``), an entry is ✓-struck ONLY when ``done_at ==
    today_iso`` (mirroring the ``/today`` drop-filter's date-equality),
    so a stale / mis-dated ``done_at`` renders PLAIN rather than falsely
    reading "done today". ``today_iso is None`` (a date-less caller — e.g.
    a shape preview) falls back to the prior presence-based ✓-strike. An
    unmarked item (``done_at`` None — the common case) is byte-identical
    to the pre-Arc-#20 ``- {item}`` render under both modes.
    """
    done_at = getattr(entry, "done_at", None)
    if done_at and (today_iso is None or done_at == today_iso):
        return f"- ~~{entry.item}~~ ✓"
    return f"- {entry.item}"


# ---------------------------------------------------------------------------
# T2 selection pool — open tasks NOT auto-T1, NOT alfred_triage, NOT curated
# ---------------------------------------------------------------------------


def _render_t2_selection_pool(
    records: list[tuple[Path, dict, str]],
    auto_t1_record_names: set[str],
    curated_t1_record_names: set[str],
    curated_t2_record_names: set[str],
    snoozed_names: set[str] | None = None,
) -> str:
    """Compose the ``### T2 selection pool`` subsection (materials).

    The pool surfaces tasks the operator might want to add to T2.
    Filters (in order):
      1. ``status`` in :data:`OPEN_STATUSES`
      2. NOT ``alfred_triage: True`` (logged per skip)
      3. NOT in today's auto-T1 set (already in T1 shortlist)
      4. NOT already in curated T1 (operator confirmed) or T2 (operator
         picked)
      5. NOT board-snoozed (R3) — offering back a row the operator just
         parked is the same broken promise as leaving it on the board

    Empty-pool path emits a sentinel line per intentionally-left-blank.
    """
    snoozed_names = snoozed_names or set()
    pool: list[tuple[str, Path]] = []  # (display_name, path) for sort
    alfred_triage_skipped = 0
    for path, fm, name in records:
        if not _is_open(fm):
            continue
        if fm.get("alfred_triage") is True:
            log.info(
                "brief.tier_section.alfred_triage_skipped",
                path=str(path),
                name=name,
                detail=(
                    "janitor-generated triage record is not "
                    "tier-rankable work; surfaces in Daily Sync "
                    "instead."
                ),
            )
            alfred_triage_skipped += 1
            continue
        if name in auto_t1_record_names:
            continue
        if name in curated_t1_record_names:
            continue
        if name in curated_t2_record_names:
            continue
        if name in snoozed_names:
            continue
        pool.append((name, path))

    pool.sort(key=lambda np: np[0].lower())

    out = [
        T2_POOL_HEADER,
        (
            "(open `todo`/`active` tasks, NOT auto-T1, NOT "
            "alfred_triage)"
        ),
        "",
    ]
    if not pool:
        out.append("*(selection pool is empty — no other open tasks)*")
        out.append("")
        return "\n".join(out)
    for name, _path in pool:
        out.append(f"- [[task/{name}]]")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Rollover from yesterday — incomplete T1 / T2 entries
# ---------------------------------------------------------------------------


def _build_status_lookup(
    records: list[tuple[Path, dict, str]],
) -> dict[str, str]:
    """Build a ``{record_name: status_lower}`` map from the task pool.

    Used by the rollover scan to check whether yesterday's T1/T2
    entries are still open today. Missing entries are NOT in the
    lookup (the task may have been deleted/moved; rollover treats
    those as "incomplete-and-missing" — surfaced with a note).
    """
    lookup: dict[str, str] = {}
    for _path, fm, name in records:
        status = fm.get("status") or "todo"
        if isinstance(status, str):
            lookup[name] = status.lower()
    return lookup


def compute_rollover(
    yesterday_curation: DailyCuration | None,
    status_by_name: dict[str, str],
) -> list[RolloverRef] | None:
    """THE server rollover: yesterday's commitments that are still open today.

    Factored out of the old ``_render_rollover_section`` so the day-plan
    projection can consume the same computation the brief has always used,
    rather than a second definition of "carried over". The word has two correct
    meanings on two surfaces — see ``tier/day_plan.py``'s module docstring for
    why the board's ``boardIsCarryover`` is a DIFFERENT and equally right rule,
    and why neither should be made to swallow the other.

    Logic (unchanged from the renderer this replaces):
      * ``yesterday_curation is None`` (no yesterday daily file OR no
        ``tier_curation`` block) → return ``None``. Rollover is opt-in by data
        existence, so "we have no idea" is a distinct answer from "we looked
        and everything was finished" (``[]``). The render keeps that
        distinction visible; collapsing the two to ``[]`` here would destroy it
        before the render could.
      * Walk yesterday's T1 + T2 entries: parse the wikilink to a record name,
        look up the current status, and treat MISSING or open as incomplete
        (missing = the task may have been moved or deleted, which the operator
        wants flagged rather than silently dropped).
      * Routine-origin entries never roll over — the next cycle resolves them
        through the routine's own ``due_pattern``.
      * T3 is deliberately excluded: self-care intentions are picked fresh each
        day, so "you didn't do yesterday's" is the wrong thing to say about one.
    """
    if yesterday_curation is None:
        # Per intentionally-left-blank we still emit the signal, so the
        # operator can grep the brief log for "did rollover run?" — the
        # absence here is a data fact, not a skipped step.
        log.info(
            "brief.tier_section.rollover_suppressed_no_yesterday",
            detail=(
                "yesterday's daily file is absent or has no "
                "tier_curation block; rollover material unavailable."
            ),
        )
        return None

    refs: list[RolloverRef] = []
    for tier_label, entries in (("T1", yesterday_curation.t1), ("T2", yesterday_curation.t2)):
        for entry in entries:
            if entry.routine_item is not None:
                continue
            if entry.task is None:
                continue
            rec_name = _wikilink_to_record_name(entry.task)
            if rec_name is None:
                continue
            status = status_by_name.get(rec_name)
            if status is None or status in OPEN_STATUSES:
                refs.append(RolloverRef(
                    tier_label=tier_label,
                    wikilink=entry.task,
                    record_name=rec_name,
                ))
    return refs


# ---------------------------------------------------------------------------
# The day-plan render — §3 as the board's morning projection
# ---------------------------------------------------------------------------


def _row_head(row: PlanRow) -> str:
    """The identifying half of a plan row: what the item IS and where it lives.

    Preserves the information the per-tier renderers carried before the slot
    regroup — the task wikilink, or the routine item's text plus a link to its
    owning routine record — so nothing an operator could previously click or
    grep for went away in the rearrangement.
    """
    entry = row.entry
    record = getattr(entry, "routine_record", None)
    if getattr(entry, "origin", "") == "routine_item" and record:
        text = getattr(entry, "item_text", None) or entry.name
        return f"{text} (from [[routine/{record}]])"
    if getattr(entry, "origin", "") == "task":
        return f"[[task/{entry.name}]]"
    return entry.name


def _row_annotation(row: PlanRow) -> str:
    """The WHY of a row — surface reason, due date, or cadence gap.

    One annotation per row, in precedence order, because a row carrying three
    of them reads as noise on a surface whose whole promise is a 30-second
    morning. Cadence rows keep the "never done" wording rather than "0 days
    since last", which would read as done-today.
    """
    entry = row.entry
    reason = getattr(entry, "surface_reason", None) or ""
    due_iso = getattr(entry, "due_iso", None) or ""
    target = getattr(entry, "target_cadence_days", None)
    if reason and due_iso:
        return f"— due {_format_due_date(due_iso)} ({reason})"
    if reason:
        return f"— {reason}"
    if due_iso:
        return f"— due {_format_due_date(due_iso)}"
    if target is not None:
        days_since = getattr(entry, "days_since_last_completed", None)
        if days_since is None:
            return f"*({T3_AUTO_DAYS_SINCE_NEVER_LABEL}; target every {target}d)*"
        return T3_AUTO_ANNOTATION_TEMPLATE.format(
            days_since=days_since, target=target,
        )
    return ""


def _row_affordance(row: PlanRow) -> str:
    """The reply affordance for a row, or empty.

    These strings are the talker's stable verbatim contract (the SKILL quotes
    them), so the regroup RELOCATES them and never rewords them. Which one
    fires is unchanged: an unconfirmed T1 gets the T1 confirm prompt, an
    auto-surfaced T2 routine candidate gets the T2 one. A committed row gets
    nothing — the commitment already happened.
    """
    entry = row.entry
    if not row.candidate:
        return ""
    if row.tier == 1:
        return T1_CONFIRM_PROMPT
    if row.tier == 2 and getattr(entry, "origin", "") == "routine_item":
        return T2_ROUTINE_CONFIRM_PROMPT
    if row.tier == 3 and getattr(entry, "target_cadence_days", None) is not None:
        return T3_AUTO_CONFIRM_PROMPT
    return ""


def _render_plan_row(row: PlanRow) -> str:
    """Render one row of a slot stack.

    Shape: ``- [T1] <what it is> <why> <affordance> <carryover marker>``.

    The ``[Tn]`` tag is load-bearing, not decoration. The board ARRANGES by
    slot while the daily goal MEASURES by tier, so a reader looking at a Duty
    stack still has to be able to see which of those rows the balanced-day line
    is counting. Dropping the tag is how the arrangement quietly becomes the
    target — see ``tier/day_plan.py``'s constraint.

    A row completed today renders struck-through with a ✓ and KEEPS its place:
    the morning brief is a snapshot of the day, and progress is part of it.
    """
    head = _row_head(row)
    if row.done:
        head = f"~~{head}~~ ✓"
    parts = [f"- [T{row.tier}]", head]
    annotation = _row_annotation(row)
    if annotation:
        parts.append(annotation)
    affordance = _row_affordance(row)
    if affordance:
        parts.append(f" {affordance}")
    if row.carryover:
        parts.append(f" {CARRYOVER_MARKER}")
    return " ".join(p for p in parts if p).rstrip()


def _render_routine_line(line: Any) -> str:
    """Render one habit anchor inside its slot (the dissolved §4).

    Byte-compatible with what the standalone routines section showed per item —
    text, an ``@ HH:MM`` for timed critical items, and the cadence annotation —
    so the dissolution moved the lines without rewriting them.
    """
    text = getattr(line, "text", "") or ""
    out = f"- {text}"
    time_str = getattr(line, "time", "") or ""
    if getattr(line, "priority", "") == "critical" and time_str:
        out += f" @ {time_str}"
    annotation = getattr(line, "annotation", "") or ""
    if annotation:
        out += f" {annotation}"
    return out


def _render_slot_group(group: SlotGroup) -> list[str]:
    """Render one slot's stack: header, then carryover → committed →
    suggestions → habit anchors, in that order.

    An empty slot still renders its header and an explicit empty line. "No Fuel
    today" is precisely the signal a balance board exists to surface; omitting
    the header would make an unbalanced day look like a shorter one.
    """
    out = [f"### {group.label}", ""]
    if group.is_empty:
        out.append(SLOT_EMPTY_TEMPLATE.format(label=group.label))
        out.append("")
        return out
    for row in group.rows:
        out.append(_render_plan_row(row))
    if group.routines:
        if group.rows:
            out.append("")
        out.append(ROUTINES_SUBHEADER)
        out.append("")
        for line in group.routines:
            out.append(_render_routine_line(line))
    out.append("")
    return out


def _render_unplaced_carryover(
    plan: DayPlan, rollover: list[RolloverRef] | None,
) -> list[str]:
    """Render the rollover remainder under :data:`ROLLOVER_HEADER`.

    Three distinct states, all of which the operator can tell apart — the
    distinction the section this replaces was careful to preserve, carried
    forward verbatim:

      * ``rollover is None`` — no yesterday file / no curation block. We do not
        KNOW what was carried, so the block is suppressed entirely (the log
        line above records that it ran).
      * ``rollover == []`` — we looked, and yesterday's commitments are all
        finished. Header plus the "all completed" sentinel, so "all clear" is
        never rendered as "no data".
      * everything on today's board already — every carried item is marked
        inline in its slot, so there is nothing left to list here; say that
        rather than falling through to a silent absence.
    """
    if rollover is None:
        return []
    out = [ROLLOVER_HEADER, ""]
    if not rollover:
        out.append(
            "*(yesterday's tracked items all completed — nothing to "
            "roll over)*"
        )
        out.append("")
        return out
    if not plan.unplaced_carryover:
        out.append(
            "*(everything carried over is on today's plan above, marked "
            "where it sits)*"
        )
        out.append("")
        return out
    for ref in plan.unplaced_carryover:
        out.append(
            f"- {ref.tier_label}: {ref.wikilink} *(uncompleted yesterday)*"
        )
    out.append("")
    return out


def _render_day_plan(
    plan: DayPlan, rollover: list[RolloverRef] | None,
) -> str:
    """Compose the whole slot-grouped plan body.

    ARRANGEMENT ONLY. This function groups the day by slot; it does not compute
    or claim any goal. The balanced-day line is rendered separately by
    :func:`render_daily_goal_line` off ``DailyGoalState``, which is tier-based —
    see ``tier/day_plan.py``: no copy anywhere may claim a slot-based goal while
    the metric is tier-based.
    """
    out: list[str] = []
    if plan.is_empty:
        out.append(PLAN_EMPTY_SENTINEL)
        out.append("")
    else:
        for group in plan.groups:
            out.extend(_render_slot_group(group))
    out.extend(_render_unplaced_carryover(plan, rollover))

    # Empty-bucket reply affordances. These strings fire under exactly the
    # condition they always did — the tier lane is empty — and are preserved
    # verbatim because the talker SKILL quotes them as stable contracts. They
    # sit here rather than inside a slot because they are about a TIER being
    # empty, and tiers no longer have their own headers to be empty under.
    prompts: list[str] = []
    if not plan.rows_in_tier(2):
        prompts.append(T2_EMPTY_PROMPT)
    if not plan.rows_in_tier(3):
        prompts.append(T3_EMPTY_PROMPT)
    if prompts:
        out.extend(prompts)
        out.append("")
    return "\n".join(out)


def render_daily_goal_line(goal: DailyGoalState) -> str:
    """Render the one-of-each-tier daily-goal status line (Q4, 2026-06-26).

    The PURPOSE of tiering per the spec: finish at least one item from
    each of T1/T2/T3 each day (a balanced day — urgent + medium +
    self-care), ideally all T1 done. This line surfaces that goal's
    progress at the top of the tier section so the view is rendered
    AROUND the goal, not just as three buckets.

    Minimal register per Q4 (voice polish — gentle/plain phrasing — is
    deferred to prompt-tuner). Per ``feedback_intentionally_left_blank``:
    ALWAYS emits a line, even on an empty day ("no tier items yet
    today"), so the goal signal is never a silent absence.

    Shape — the COMPLETE producible set. Every line below was emitted by
    this function and byte-compared against it, not written out:

        **Daily goal — balanced day:** ✓ achieved · T1 1/2 · T2 1/1 · T3 1/1
        **Daily goal — balanced day:** not yet · T1 0/2 · T2 0/1 · T3 0/1
        **Daily goal — balanced day:** ✓ achieved · T1 2/2 · T2 1/1 · T3 1/1 · all T1 done
        **Daily goal — balanced day:** not yet · T1 2/2 · T2 0/1 · T3 0/1 · all T1 done
        **Daily goal:** no tier items yet today.

    That is the whole space: the empty-day sentinel, then the cross-product
    of two INDEPENDENT branches — ``status`` (off ``balanced_day``) and
    ``ideal`` (off ``t1_available > 0 and all_t1_done``). Five shapes, five
    lines, no sixth.

    ``✓ achieved`` IS NOT FREE-STANDING COPY, and this is the trap that has
    already been sprung here. ``balanced_day`` is produced as ``t1_done >= 1
    and t2_done >= 1 and t3_done >= 1`` (in
    :func:`alfred.tier.compute.compute_today_view`, alongside the
    ``all_t1_done`` it ships with), so ``✓ achieved`` printed beside ANY
    ``0/n`` lane is a line this function CANNOT emit. An earlier revision of
    this very block printed ``✓ achieved · T1 1/2 · T2 1/1 · T3 0/1`` — a
    non-producible line sitting in the docstring that other surfaces read as
    the authority on this line's shapes. Pinned in
    ``tests/test_brief_tier_section.py`` (the shape-set pin drives all five
    states through this function and asserts the impossible pairing never
    appears).
    """
    total = (
        goal.t1_available + goal.t2_available + goal.t3_available
    )
    if total == 0:
        return "**Daily goal:** no tier items yet today."

    def _lane(label: str, done: int, avail: int) -> str:
        return f"{label} {done}/{avail}"

    status = "✓ achieved" if goal.balanced_day else "not yet"
    # Note the ideal (all T1 done) when it holds AND there are T1 items.
    ideal = ""
    if goal.t1_available > 0 and goal.all_t1_done:
        ideal = " · all T1 done"
    return (
        f"**Daily goal — balanced day:** {status}"
        f" · {_lane('T1', goal.t1_done, goal.t1_available)}"
        f" · {_lane('T2', goal.t2_done, goal.t2_available)}"
        f" · {_lane('T3', goal.t3_done, goal.t3_available)}"
        f"{ideal}"
    )


# ---------------------------------------------------------------------------
# View → formatter-input adapters (Step 2c, 2026-06-26)
# ---------------------------------------------------------------------------
#
# The render layer no longer calls the ``compute_auto_*`` predicates. It
# reads ``compute_today_view``'s lane assignments — the SINGLE source of
# what surfaces / which lane — and these adapters slice the view's lanes
# back into the candidate shapes the existing formatters consume. The
# "auto" subset of each lane is selected by ``source`` (the view marks
# auto candidates with ``auto-*`` sources; curated entries the formatters
# read separately from the curation block). This keeps the markdown
# byte-identical (the view's membership is equivalent to the prior direct
# compute — proven by the unchanged output pins) while making the view
# the only place a surface decision is made.

def _auto_t1_task_from_view(view: TodayView) -> list[AutoT1Candidate]:
    """Task-origin T1 entries carrying an auto reason, sliced from the
    view's T1 lane.

    Returns EVERY task-origin T1 entry that has a ``surface_reason`` —
    including a CURATED entry the operator confirmed that also
    auto-surfaces (the view annotates such entries with the auto
    reason/due). The downstream merge keys reason lookups off this list
    AND dedups appends against the curation block, so returning the
    curated-coinciding entry populates the reason map without
    double-rendering. Curated entries with NO auto reason (operator
    added a task that isn't deadline-near) carry no reason and are
    skipped — they render bare from the curation block."""
    out: list[AutoT1Candidate] = []
    for e in view.t1:
        if e.origin != "task" or not e.surface_reason:
            continue
        out.append(AutoT1Candidate(
            path=e.path,
            name=e.name,
            due_iso=e.due_iso or "",
            surface_reason=e.surface_reason,
            origin="task",
        ))
    return out


def _auto_t1_routine_from_view(view: TodayView) -> list[AutoT1Candidate]:
    """Routine-origin T1 entries carrying an auto reason, sliced from the
    view's T1 lane. Same curated-coinciding inclusion as the task
    variant (the view annotates curated routine_item entries that also
    auto-surface)."""
    out: list[AutoT1Candidate] = []
    for e in view.t1:
        if e.origin != "routine_item" or not e.surface_reason:
            continue
        out.append(AutoT1Candidate(
            path=e.path,
            name=e.name,
            due_iso=e.due_iso or "",
            surface_reason=e.surface_reason,
            origin="routine",
            routine_record=e.routine_record,
            item_text=e.item_text,
        ))
    return out


def _auto_t2_routine_from_view(view: TodayView) -> list[AutoT1Candidate]:
    """Routine-origin auto-T2 ramp candidates, sliced from the T2 lane."""
    out: list[AutoT1Candidate] = []
    for e in view.t2:
        if e.origin != "routine_item" or e.source != "auto-surface-routine":
            continue
        out.append(AutoT1Candidate(
            path=e.path,
            name=e.name,
            due_iso=e.due_iso or "",
            surface_reason=e.surface_reason or "",
            origin="routine",
            routine_record=e.routine_record,
            item_text=e.item_text,
        ))
    return out


def _auto_t3_routine_from_view(view: TodayView) -> list[AutoT3Candidate]:
    """Cadence-driven auto-T3 candidates, sliced from the T3 lane. The
    cadence metadata (target / days-since / ratio) is carried on the
    view's TierEntry so the annotation render is a pure read."""
    out: list[AutoT3Candidate] = []
    for e in view.t3:
        if e.origin != "routine_item" or e.source != "auto-cadence-routine":
            continue
        out.append(AutoT3Candidate(
            path=e.path,
            routine_record=e.routine_record or "",
            item_text=e.item_text or e.name,
            target_cadence_days=e.target_cadence_days or 0,
            days_since_last_completed=e.days_since_last_completed,
            overdue_ratio=(
                e.overdue_ratio if e.overdue_ratio is not None
                else float("inf")
            ),
        ))
    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def render_tier_section(
    vault_path: Path,
    now: datetime,
    tier_defaults: Any = None,
) -> str:
    """Render the brief's ``Today's Plan`` section body — the board's morning
    projection (V2; slot-grouped since Phase C).

    ``now`` is the reference instant — passed by the brief daemon at
    fire time + by ``/today`` at request time. ``now.date()`` is "today"
    for the curation lookup.

    ``tier_defaults`` (Q3 Option A, 2026-06-26): optional global tier-
    window defaults, passed straight through to ``compute_today_view`` so
    the 06:00 brief applies the SAME defaults the aggregator's 05:59 pass
    does. ``None`` → no defaults (opt-out semantics unchanged).

    Always returns a non-empty string per
    ``feedback_intentionally_left_blank``: even the cold-start case
    (no vault, no curation, no records) emits an explicit "ran,
    nothing to do" composition.

    Read-side stability: this function is a pure projection over the
    inputs (today's curation + auto-T1 candidates + yesterday's
    curation + task pool snapshot). Called twice with identical inputs
    it returns identical output — Ship 4 talker reads + writes
    curation separately; this render never mutates the block.
    """
    today = now.date()

    # --- 1. Read today's curation ---------------------------------
    curation = load_daily_curation(vault_path, today)

    # --- 2. Compute the unified today view ------------------------
    # Step 2c (Option B, 2026-06-26): the SINGLE source of what
    # surfaces / which lane. This render layer no longer calls the
    # ``compute_auto_*`` predicates directly — it reads the view's lane
    # assignments and re-presents them. The view already merged curated +
    # auto candidates per lane (via ``classify_routine_item``, the single
    # predicate); the auto-candidate lists below are SLICED from the
    # view's lanes (by origin + auto-source), not independently computed.
    # So the renderer makes NO surface decision of its own — it's a pure
    # formatter of the view's WHAT. (Membership is byte-equivalent to the
    # prior direct-compute path — proven by the unchanged
    # ``test_brief_tier_section`` output pins.) The selection pool +
    # rollover are render-only MATERIALS (not substrate lane assignment)
    # and stay computed here.
    today_view = compute_today_view(vault_path, now, tier_defaults)

    auto_t1_task_candidates = _auto_t1_task_from_view(today_view)
    auto_t1_routine_candidates = _auto_t1_routine_from_view(today_view)
    auto_t2_routine_candidates = _auto_t2_routine_from_view(today_view)
    auto_t3_routine_candidates = _auto_t3_routine_from_view(today_view)
    auto_t1_record_names = {c.name for c in auto_t1_task_candidates}

    # --- 3. Read yesterday's curation for rollover ----------------
    yesterday = today - timedelta(days=1)
    yesterday_curation = load_daily_curation(vault_path, yesterday)

    # --- 4. Scan task pool ----------------------------------------
    records = _iter_task_records(vault_path)
    status_by_name = _build_status_lookup(records)

    # Build the curated-name sets for the selection-pool exclusion.
    # Only task-origin curated entries pollute these sets — routine-
    # origin entries don't shadow task-pool entries.
    curated_t1_names: set[str] = set()
    curated_t2_names: set[str] = set()
    if curation is not None:
        for e in curation.t1:
            if e.task is not None:
                n = _wikilink_to_record_name(e.task)
                if n:
                    curated_t1_names.add(n)
        for e in curation.t2:
            if e.task is not None:
                n = _wikilink_to_record_name(e.task)
                if n:
                    curated_t2_names.add(n)

    # --- 5. Compose render --------------------------------------------
    # Phase C: the section is the board's MORNING PROJECTION — one shared slot
    # projection, grouped Duty / Rhythm / Fuel, carryover-first, with the
    # dissolved routines section's habit anchors inside their slot. The same
    # ``DayPlan`` object drives the briefing player's spoken day-plan segment,
    # so the read surface and the spoken surface cannot disagree about what is
    # on today's plan.
    rollover = compute_rollover(yesterday_curation, status_by_name)
    plan = build_day_plan_for_vault(
        vault_path, today_view, today, rollover=rollover or (),
    )
    day_plan_md = _render_day_plan(plan, rollover)
    # Snoozed rows must not reappear in the pool. The pool is a RENDER-ONLY
    # material computed here, not sliced from today_view, so the projection's
    # suppression does not reach it — a snoozed task dropped out of T1 and
    # showed up two sections lower under "tasks you might want to add",
    # i.e. the system offering back the thing just parked. The ratified matrix
    # is "hide-from-board AND suppress re-suggestion"; a pick-list IS the
    # re-suggestion surface. Found by the end-to-end pin, not by unit pins.
    from alfred.tier.snooze import load_snoozes, is_snoozed

    _snooze_path = getattr(tier_defaults, "snooze_path", "") or None
    _snoozed_task_names: set[str] = set()
    if _snooze_path is not None:
        _snoozes = load_snoozes(_snooze_path)
        for _path, _fm, _name in records:
            _stored = _snoozes.get(f"task:task/{_path.name}")
            if _stored is None:
                continue
            # Same predicate as the projection — a due-date delta that would
            # break the row through onto the board must also return it here.
            _suppressed, _ = is_snoozed(
                _stored, today=today,
                current_due_iso=str(_fm.get("due") or ""),
            )
            if _suppressed:
                _snoozed_task_names.add(_name)

    pool = _render_t2_selection_pool(
        records,
        auto_t1_record_names,
        curated_t1_names,
        curated_t2_names,
        snoozed_names=_snoozed_task_names,
    )
    # Daily-goal status line (Q4, 2026-06-26). Read from the SAME
    # ``today_view`` computed once at the top — no second compute. The
    # line renders first so the day is framed around the balanced-day goal.
    #
    # It is TIER-based and stays that way in this lane: the slot regroup below
    # it is an ARRANGEMENT of the same rows, not a new target. Flipping the
    # metric to the slot axis is a separate, separately-gated lane; until it
    # lands, this line and the stacks under it are deliberately on different
    # axes, and the ``[Tn]`` tag on each row is what keeps that legible.
    goal_line = render_daily_goal_line(today_view.daily_goal)

    # Compose: goal line, the slot-grouped plan (incl. rollover remainder),
    # separator, selection pool.
    parts = [goal_line, "", day_plan_md, "---", "", pool]

    body = "\n".join(parts)

    log.info(
        "brief.tier_section.rendered",
        scanned=len(records),
        curation_loaded=curation is not None,
        curated_t1=len(curation.t1) if curation else 0,
        curated_t2=len(curation.t2) if curation else 0,
        curated_t3=len(curation.t3) if curation else 0,
        auto_t1_task_count=len(auto_t1_task_candidates),
        auto_t1_routine_count=len(auto_t1_routine_candidates),
        auto_t2_routine_count=len(auto_t2_routine_candidates),
        # Phase 2A-soft-cadence (2026-05-30): T3 soft-cadence auto-
        # suggest count. ``feedback_log_emission_test_pattern`` pin:
        # test asserts this field is present in the log when
        # candidates exist, AND when bucket is empty.
        auto_t3_routine_count=len(auto_t3_routine_candidates),
        # ``rollover_present`` now means "yesterday's block existed AND held
        # something still open" — the tri-state (None / [] / non-empty) that
        # ``compute_rollover`` returns, flattened for the log. The suppressed
        # case keeps its own dedicated event above.
        rollover_present=bool(rollover),
        rollover_count=len(rollover) if rollover is not None else 0,
        # Phase C: how the day actually arranged, pinned per
        # ``feedback_log_emission_test_pattern`` — a slot board that silently
        # stopped grouping would otherwise be invisible in the logs.
        plan_rows=plan.total_rows,
        plan_carryover=sum(len(g.carryover) for g in plan.groups),
        plan_unplaced_carryover=len(plan.unplaced_carryover),
        plan_slots_occupied=[g.slot for g in plan.groups if not g.is_empty],
        yesterday_curation_loaded=yesterday_curation is not None,
        # Step 2c (2026-06-26): the daily-goal rollup, surfaced from the
        # unified compute_today_view, pinned per
        # ``feedback_log_emission_test_pattern``.
        balanced_day=today_view.daily_goal.balanced_day,
        all_t1_done=today_view.daily_goal.all_t1_done,
    )
    return body


def _build_task_status_map(vault_path: Path) -> dict[str, dict[str, Any]]:
    """Map each task record's NAME → its frontmatter, for status lookup.

    Built once per ``/today`` render from the existing
    :func:`_iter_task_records` walk (which already skips broken /
    non-task files). Keyed on the record name (matching what
    :func:`_wikilink_to_record_name` extracts from a ``[[task/Name]]``
    curated entry). On a duplicate name the first walked record wins
    (sorted glob order); name collisions in ``task/`` are a separate
    janitor concern.
    """
    status_map: dict[str, dict[str, Any]] = {}
    for _path, fm, name in _iter_task_records(vault_path):
        status_map.setdefault(name, fm)
    return status_map


def _curated_entry_is_closed(
    entry: T1T2Entry,
    status_map: dict[str, dict[str, Any]],
) -> bool:
    """Return True iff a curated T1/T2 entry references a CLOSED task.

    Only task-origin entries (``entry.task`` is a ``[[task/Name]]``
    wikilink) are status-checked. Routine-origin entries
    (``entry.routine_item``) have no task record and are NEVER closed
    by this gate (return False → kept).

    Fail-OPEN on a lookup miss: an entry whose task record is missing,
    unreadable, or absent from ``status_map`` returns False (kept), so a
    transient parse failure never silently hides a real commitment —
    only an EXPLICITLY closed status (``not _is_open``) hides the entry.
    Reuses :func:`_is_open` / ``OPEN_STATUSES`` — no new status set.
    """
    if entry.routine_item is not None:
        return False  # routine-origin — no task record to check
    record_name = _wikilink_to_record_name(entry.task or "")
    if record_name is None:
        return False  # malformed/absent task ref → fail-open (keep)
    fm = status_map.get(record_name)
    if fm is None:
        return False  # task record not found → fail-open (keep)
    return not _is_open(fm)


def render_curated_tier_section_for_today(
    daily_curation: DailyCuration | None,
    vault_path: Path | None = None,
    today: date | None = None,
) -> str:
    """Render the curated-only tier section body (the ``/today`` view).

    Built as the operator-committed view for the ``/today`` slash command
    (2026-05-30 scope refinement). The /today command died with the
    Telegram retirement (T5, 2026-08-19); this renderer is KEPT as a
    tested mode of the shared tier surface with no production caller
    today — the ``/today`` mentions below describe its historical
    consumer's contract, which any future glance-view door inherits.
    Renders ONLY the operator-curated T1/T2/T3
    shortlists from the daily_curation block — no auto-T1 candidates,
    no T2 selection pool, no auto-T2-routine subsection, no rollover,
    no confirm prompts. Operator already committed; the view's purpose
    is "what's on my plate right now" not "what should I commit to."

    **Live completed-task filter** (2026-06-15). When ``vault_path`` is
    provided, each curated T1/T2 task-origin entry is checked against
    its task record's CURRENT status and OMITTED when closed (status not
    in ``OPEN_STATUSES``) — operator closes ``task/Foo`` via the talker
    and ``/today`` stops showing it. Routine-origin T1/T2 entries and
    all T3 free-text entries have no task record and always pass. The
    filter fails OPEN on a missing/unreadable record (only an
    explicitly-closed status hides an item). When ``vault_path`` is
    ``None`` (the default — e.g. the morning brief's full-materials
    render, or any caller that doesn't thread the path) NO filtering
    happens and the render is byte-identical to the pre-2026-06-15
    behaviour, so existing callers are unaffected.

    **Done-T3 drop** (Arc #20, 2026-07-22). When ``today`` is provided,
    free-text T3 items the operator has checked off today (``done_at ==
    today``, via the ``tier_done`` tool) are DROPPED — the mirror of the
    completed-task filter above, keeping the operator-committed view
    ("what's still on my plate") clean. Contrast the morning brief, which
    ✓-STRIKES done T3 (a day snapshot showing progress —
    :func:`_render_t3_entry`). When ``today`` is ``None`` (the default —
    callers that don't thread it) NO T3 done-filtering happens and the
    render is byte-identical, so existing callers are unaffected. The
    ``/today`` composer always threads ``today``.

    Contrast with :func:`render_tier_section` (the full materials view
    the morning brief uses): that function consumes the same
    daily_curation PLUS auto-T1 candidates + selection pool + rollover
    + confirm affordances. The two surfaces share the per-entry
    rendering primitives (:func:`_render_t2_entry`,
    :func:`_render_t3_entry`) so the shape of a single curated entry
    stays consistent across both views.

    Empty-bucket convention (per ``feedback_intentionally_left_blank``,
    Andrew-ratified 2026-05-30): header-suffix sentinel
    ``### T1 — (no items yet)`` keeps all three headers visible while
    distinguishing "operator hasn't curated yet" from "broken render."
    The morning brief uses a separate per-bucket sentinel line — the
    ``/today`` view prefers the suffix because the operator-committed
    framing means an empty bucket reads as "nothing committed to T2
    yet" rather than "what's available for T2."

    When ``daily_curation`` is ``None`` (no daily file exists yet for
    today, e.g. running ``/today`` before the 06:00 brief / 05:59
    aggregator has fired), all three buckets render with the empty-
    suffix sentinel so the operator sees the same shape they'd see
    after a deliberate empty curation.

    Cross-Ship contract: T1/T2/T3 entries render identically to the
    morning brief's curated section (same per-entry helpers), minus
    the confirm prompts that fire on auto-surfaced candidates the
    operator hasn't yet committed to. A render-shape change on the
    morning brief side propagates here through the shared helpers.
    """
    curated_t1 = daily_curation.t1 if daily_curation else []
    curated_t2 = daily_curation.t2 if daily_curation else []
    curated_t3 = daily_curation.t3 if daily_curation else []

    # Live completed-task filter (2026-06-15). Only when a vault_path is
    # threaded (the /today composer). T1/T2 task-origin entries whose
    # referenced task record is closed are dropped; routine-origin + T3
    # entries are untouched (no task record). An emptied bucket still
    # hits the header-suffix sentinel below (ILB preserved).
    filtered_closed = 0
    if vault_path is not None:
        status_map = _build_task_status_map(vault_path)
        before = len(curated_t1) + len(curated_t2)
        curated_t1 = [
            e for e in curated_t1
            if not _curated_entry_is_closed(e, status_map)
        ]
        curated_t2 = [
            e for e in curated_t2
            if not _curated_entry_is_closed(e, status_map)
        ]
        filtered_closed = before - (len(curated_t1) + len(curated_t2))

    # Arc #20 (2026-07-22): DROP free-text T3 items the operator has
    # checked off TODAY (``done_at == today``) — mirrors the task-origin
    # closed-filter above, keeping the operator-committed ``/today`` view
    # clean ("what's still on my plate"). Contrast the morning brief,
    # which ✓-STRIKES done T3 (a day snapshot; :func:`_render_t3_entry`).
    # Only fires when ``today`` is threaded (the /today composer always
    # threads it); ``today=None`` keeps the render byte-identical for
    # callers that don't (existing tests / the brief's full-materials
    # render). A same-day ``done_at`` is the only value a daily file
    # carries (T3 never rolls over; back-dates land in their own file).
    t3_done_filtered = 0
    if today is not None:
        _today_iso = today.isoformat()
        before_t3 = len(curated_t3)
        curated_t3 = [
            e for e in curated_t3
            if getattr(e, "done_at", None) != _today_iso
        ]
        t3_done_filtered = before_t3 - len(curated_t3)

    def _bucket(header_label: str, entries: list, render_entry) -> list[str]:
        """Compose one bucket's lines.

        Empty bucket → header-suffix sentinel only (single line +
        trailing blank). Populated → header + entries + trailing
        blank. The shared shape keeps the three-bucket render
        rhythm uniform.
        """
        if not entries:
            return [f"### {header_label} — (no items yet)", ""]
        out = [f"### {header_label}", ""]
        for entry in entries:
            out.append(render_entry(entry))
        out.append("")
        return out

    # Reuse the existing per-entry helpers — they already discriminate
    # task vs routine_item shape WITHOUT confirm prompts, which is
    # exactly what ``/today`` wants. T1 uses :func:`_render_t2_entry`
    # (NOT :func:`_render_t1_entry`) because the T1 render path with
    # confirm/reason annotations is for the auto-surfaced morning-brief
    # view, not the operator-committed ``/today`` view.
    lines: list[str] = []
    lines.extend(_bucket("T1", curated_t1, _render_t2_entry))
    lines.extend(_bucket("T2", curated_t2, _render_t2_entry))
    lines.extend(_bucket("T3", curated_t3, _render_t3_entry))

    body = "\n".join(lines).rstrip() + "\n"

    log.info(
        "brief.tier_section.rendered_curated_for_today",
        curation_loaded=daily_curation is not None,
        curated_t1=len(curated_t1),
        curated_t2=len(curated_t2),
        curated_t3=len(curated_t3),
        # ILB: surface the completed-task filter so an operator can grep
        # "why did my T1 item disappear" — distinguishes "I closed it"
        # (status_filtered>0) from "render dropped it" (a bug).
        # status_filter_applied is False when no vault_path was threaded
        # (filtering off — e.g. the brief's full-materials render).
        status_filter_applied=vault_path is not None,
        status_filtered=filtered_closed,
        # Arc #20 ILB: surface the done-T3 drop so an operator can grep
        # "why did my T3 item disappear from /today" — distinguishes "I
        # checked it off" (t3_done_filtered>0) from a render bug.
        # t3_done_filter_applied is False when no ``today`` was threaded.
        t3_done_filter_applied=today is not None,
        t3_done_filtered=t3_done_filtered,
    )
    return body


__all__ = [
    "ROLLOVER_HEADER",
    "SECTION_HEADER",
    "T1_CONFIRM_PROMPT",
    "T2_AUTO_ROUTINE_HEADER",
    "T2_EMPTY_PROMPT",
    "T2_POOL_HEADER",
    "T2_ROUTINE_CONFIRM_PROMPT",
    "T3_AUTO_ANNOTATION_TEMPLATE",
    "T3_AUTO_CONFIRM_PROMPT",
    "T3_AUTO_DAYS_SINCE_NEVER_LABEL",
    "T3_AUTO_SECTION_HEADER",
    "T3_AUTO_TALKER_DEFERRED_NOTE",
    "T3_EMPTY_PROMPT",
    "render_curated_tier_section_for_today",
    "render_tier_section",
]
