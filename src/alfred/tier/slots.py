"""Slot classifier — Duty / Rhythm / Fuel.

Ratified design: ``~/reports/slot-classifier-design-2026-08-04.md`` (operator
ratified §5, team-lead ratified §6, 2026-08-04).

**Slot is a second axis, orthogonal to tier.** Tier answers *when does this
press*; slot answers *what does this do for the day*. Both survive; neither
replaces the other. The ruling's own words settle it: routine records "feed ALL
three slots", so slot cannot be derived from tier or from origin. G9's
rename-only reading (Duty=T1, Rhythm=T2, Fuel=T3) is SUPERSEDED.

The payload of the feature is that ``balanced_day`` eventually stops meaning
"one urgent + one nearly-urgent + one not-urgent" and starts meaning "one
obligation + one practice + one restoration". That flip is stage 3 of the
ratified rollout; this module is stage 1 and changes no metric.

## Deterministic-first, and why

This classifier guesses as little as possible. Most of the work is already
encoded in fields the operator maintains by hand — they set ``self_care``, they
chose ``due_pattern`` over ``target_cadence_days``. The classifier is mostly a
READER of existing operator intent, not an opinion.

The asymmetry that shapes everything here: **Duty and Rhythm are inferable from
record structure; Fuel is a judgment about what a thing does for the person.**
No frontmatter tells you that guitar is restorative and scales are a chore. That
is a question with one authority and it is not a model — which is why there is
no LLM pass over the residue, and why :data:`SLOT_UNSLOTTED` is a first-class
answer rather than a default bucket.

## Precedence (highest first)

===  =========================================  ============  ==================
 #   Rule                                       Slot          Basis
===  =========================================  ============  ==================
 1   Explicit ``slot:`` on the item/record      *as written*  Operator's word is final
 2   Learned override for this identity         *as learned*  DEFERRED — see below
 3   ``self_care: true``                        Fuel          Already means "intrinsic"
 4   Routine item with ``due_pattern``          Duty          A scheduled obligation
 5   Routine item with ``target_cadence_days``  Rhythm        The definition of a practice
 6   ``origin == "task"`` WITH a due date       Duty          A dated task is an obligation
 7   anything else                              unslotted     Refuse to guess
===  =========================================  ============  ==================

Rules 4 and 5 are mutually exclusive at the semantic level (``routine/config``
prefers ``due_pattern`` when an item carries both), so their order matters only
for malformed records — but it is fixed here anyway, because a precedence that
depends on data being well-formed is not a precedence.

**Rule 2 is deliberately not implemented yet, and its seam is REQUIRED rather
than defaulted.** :func:`classify_slot` takes ``learned`` as a required
keyword-only argument, so every production call site threads it from day one and
slice 2 fills the store rather than retrofitting a parameter. A defaulted gate
parameter is the standing trap from builder.md: the tests thread it, production
never does, every pin stays green and the feature is accepted-then-ignored. That
is exactly how R3's snooze suppression shipped write-live/read-dead.

## Why ``unslotted`` is a state, not a bucket

Every item could be defaulted to Duty and the rings would look full. That is the
"lying DONE button" disease in a new costume — a surface showing a confident
answer it does not have. So ``unslotted`` items render honestly, are EXCLUDED
from the balanced-day denominator rather than counted against it (an unreachable
daily goal reads as personal failure, not as a migration artifact), and are the
highest-value correction targets. It also makes rollout observable: "37% of
today's items are unslotted" is a number that can be watched falling, instead of
a silent wrong-answer rate.

## Overlay, not stored

The derived classification is a producer-time OVERLAY, recomputed every emit and
never written to a record — the law ``brief/feed_producer`` already follows for
``candidate`` and ``done`` (derived) versus ``confirmed`` (persisted, because
nothing can re-derive an operator decision). Two consequences worth stating:
change a rule and the whole vault reclassifies on the next fire, with no
migration and nothing to reconcile; and the classifier stays entirely off the
vault-write surface arc #18 just spent five slices containing.

The cost, stated plainly: there is no history of what an item was slotted as
last week. That needs a deliberate audit trail if it is ever wanted, not a side
effect of storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import structlog

log = structlog.get_logger(__name__)

# Canonical slot keys. FIXED — per-user vocabulary renames the LABELS only
# (``tier.slots.labels`` config), never the set. Same discipline as "storage may
# keep t1/t2/t3 keys internally" applied one level up: the wire format, the
# store, the classifier and the pins all speak these keys; the person sees their
# own words. A vocabulary change is then a config edit with ZERO data migration,
# which is what keeps the workshop cheap to leave open while the operator is
# still deciding whether the set of three is right.
SLOT_DUTY = "duty"
SLOT_RHYTHM = "rhythm"
SLOT_FUEL = "fuel"

#: The honest residue. NOT a slot — an admission that no rule fired.
SLOT_UNSLOTTED = "unslotted"

#: The three real slots, in ring order. This tuple IS the balanced-day
#: denominator: a day is balanced when each of these has at least one done.
#: ``SLOT_UNSLOTTED`` is deliberately absent — that is the exclusion the design
#: calls for, expressed as set membership rather than as a filter someone has to
#: remember to apply.
CANONICAL_SLOTS: tuple[str, str, str] = (SLOT_DUTY, SLOT_RHYTHM, SLOT_FUEL)

#: Every value ``classify_slot`` can return.
ALL_SLOT_VALUES: frozenset[str] = frozenset(CANONICAL_SLOTS) | {SLOT_UNSLOTTED}

# Rule identifiers, returned alongside the slot. These are the "why" — an item
# that is Duty because the operator wrote ``slot: duty`` and one that is Duty
# because it is a dated task are the same answer for different reasons, and the
# coverage telemetry is far less useful if it cannot tell them apart.
RULE_EXPLICIT = "explicit"
RULE_LEARNED = "learned"
RULE_SELF_CARE = "self_care"
RULE_DUE_PATTERN = "due_pattern"
RULE_CADENCE = "target_cadence_days"
RULE_DATED_TASK = "dated_task"
RULE_NONE = "no_signal"


#: Operator-vocabulary aliases for canonical slot keys.
#:
#: The operator says "routine" for what this module calls Rhythm, and it is
#: very likely the app taught him the word: the home view renders T3 items as
#: "RHYTHM — auto-cadence-routine" every morning. His 2026-08-14 routing
#: rulings used it as a slot name directly ("surface as ROUTINE slot; ESCALATE
#: to DUTY"), pairing it with a real slot, and the Phase-1 pass wrote
#: ``return_slot: routine`` onto two live records.
#:
#: The mapping lives HERE, at the read boundary, rather than being normalised
#: away in the vault: the operator will keep saying "routine", and the talker
#: will keep writing what he says. One named mapping is the cheap place for the
#: impedance mismatch; rewriting records would have to be redone every time a
#: new snooze is spoken.
SLOT_ALIASES: dict[str, str] = {
    "routine": SLOT_RHYTHM,
}


def normalize_slot(raw: Any) -> str | None:
    """Coerce an operator-written ``slot:`` value to a canonical key, or None.

    Case- and whitespace-insensitive, because this reads hand-edited
    frontmatter. Accepts :data:`SLOT_ALIASES` as operator vocabulary for a
    canonical key. Returns ``None`` for anything unrecognised — including
    ``"unslotted"`` itself, which is the classifier's answer to give, never the
    operator's to write. An unrecognised value falls through to the next rule
    rather than becoming a slot, so a typo degrades to a lower-precedence
    (still honest) answer instead of inventing a fourth category.

    **An unrecognised non-empty value is LOGGED, not silently dropped.**
    Falling through is the right behaviour but it is indistinguishable, from
    the outside, from a value that was never written — and this function now
    sits on the path that decides where a returning snooze lands. A typo in
    ``return_slot`` would otherwise present as "the operator's ruling quietly
    did nothing". Absent and blank values are NOT logged: they mean "not set",
    which is ordinary.
    """
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if value in CANONICAL_SLOTS:
        return value
    aliased = SLOT_ALIASES.get(value)
    if aliased is not None:
        return aliased
    if value:
        log.warning(
            "tier.slots.unrecognized_slot_value",
            value=value,
            canonical=list(CANONICAL_SLOTS),
            aliases=sorted(SLOT_ALIASES),
            hint=(
                "slot value not recognised — falling through to the next "
                "classifier rule, so this record will NOT land in the slot "
                "that was written. Fix the value or add an alias."
            ),
        )
    return None


class SlotOverrides(Protocol):
    """The learned-override lookup (rule 2). Slice 2 supplies the real store.

    A Protocol rather than a concrete class so the store can land in its own
    module (``match_calibration``-shaped, per the ratified design) without this
    one importing it. :meth:`lookup` returns a canonical slot key, or ``None``
    when nothing is learned for this identity.
    """

    def lookup(self, entry: Any) -> str | None:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class NoOverrides:
    """The empty learned store — stage 1's ``learned`` argument.

    Exists so that ``learned`` can be a REQUIRED parameter before the store is
    built. Production threads this today and threads the real store in slice 2;
    the call sites do not change, so rule 2 arriving cannot be a retrofit that
    forgets a caller.
    """

    def lookup(self, entry: Any) -> str | None:
        return None


@dataclass(frozen=True)
class SlotVerdict:
    """One classification: the answer and the rule that produced it."""

    slot: str
    rule: str

    @property
    def is_slotted(self) -> bool:
        return self.slot in CANONICAL_SLOTS


def classify_slot(entry: Any, *, learned: SlotOverrides) -> SlotVerdict:
    """Classify one ``TierEntry`` into a slot. Never raises, never guesses.

    ``entry`` is duck-typed rather than imported to keep ``tier.compute`` →
    ``tier.slots`` a one-way dependency; the fields read are ``explicit_slot``,
    ``self_care``, ``has_due_pattern``, ``target_cadence_days``, ``origin`` and
    ``due_iso``, each accessed defensively so an older entry shape degrades to
    ``unslotted`` rather than crashing the whole projection.

    Note the deliberate split between ``entry.explicit_slot`` (the operator's
    frontmatter value — an INPUT, rule 1) and ``entry.slot`` (this function's
    stamped OUTPUT). Collapsing them into one field would make the classifier
    read its own previous answer on any re-projection, which is how an overlay
    quietly becomes stored state.

    ``learned`` is required — see the module docstring on the optional-gate
    trap.
    """
    # Rule 1 — the operator's own word, on the record. Outranks everything,
    # including a learned override: an explicit field is a more recent and more
    # deliberate statement than an accumulated one.
    explicit = normalize_slot(getattr(entry, "explicit_slot", None))
    if explicit is not None:
        return SlotVerdict(explicit, RULE_EXPLICIT)

    # Rule 2 — learned from approved corrections. Empty in stage 1.
    remembered = normalize_slot(learned.lookup(entry))
    if remembered is not None:
        return SlotVerdict(remembered, RULE_LEARNED)

    # Rule 3 — self_care already means "intrinsic" in this codebase and already
    # routes to T3. It outranks the cadence rule ON PURPOSE: a self-care item
    # that also carries a cadence target ("guitar, every 3 days") is Fuel that
    # happens to be scheduled, not a practice that happens to be pleasant. That
    # ordering is the single place where the plumbing cost bites — see the
    # ``self_care`` threading note in ``compute.AutoT3Candidate``.
    if bool(getattr(entry, "self_care", False)):
        return SlotVerdict(SLOT_FUEL, RULE_SELF_CARE)

    # Rule 4 — a recurring hard deadline is a scheduled obligation.
    if bool(getattr(entry, "has_due_pattern", False)):
        return SlotVerdict(SLOT_DUTY, RULE_DUE_PATTERN)

    # Rule 5 — "should be done at least every N days" is the definition of a
    # practice.
    if getattr(entry, "target_cadence_days", None) is not None:
        return SlotVerdict(SLOT_RHYTHM, RULE_CADENCE)

    # Rule 6 — a dated task record is an obligation. Undated tasks fall
    # through: "someday" is not a duty, and calling it one is the confident
    # wrong answer this design exists to avoid.
    if getattr(entry, "origin", None) == "task" and getattr(entry, "due_iso", None):
        return SlotVerdict(SLOT_DUTY, RULE_DATED_TASK)

    # Rule 7 — refuse to guess.
    return SlotVerdict(SLOT_UNSLOTTED, RULE_NONE)


@dataclass(frozen=True)
class SlotCoverage:
    """How much of today the classifier could actually answer for.

    The rollout's visible convergence metric, and the number that gates the
    stage-2 rings swap. Reported every projection so a stalled or regressing
    classifier is legible rather than silently wrong.
    """

    total: int = 0
    slotted: int = 0
    unslotted: int = 0
    by_slot: dict[str, int] | None = None
    by_rule: dict[str, int] | None = None

    @property
    def coverage_pct(self) -> float:
        """Percentage of today's entries that got a real slot. 100.0 on an
        empty day — nothing was left unanswered, so nothing is missing. Zero
        would read as total classifier failure on a day with no items."""
        if self.total <= 0:
            return 100.0
        return round(100.0 * self.slotted / self.total, 1)


def summarize_coverage(verdicts: list[SlotVerdict]) -> SlotCoverage:
    """Roll a day's verdicts into the coverage report."""
    by_slot: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    slotted = 0
    for v in verdicts:
        by_slot[v.slot] = by_slot.get(v.slot, 0) + 1
        by_rule[v.rule] = by_rule.get(v.rule, 0) + 1
        if v.is_slotted:
            slotted += 1
    return SlotCoverage(
        total=len(verdicts),
        slotted=slotted,
        unslotted=len(verdicts) - slotted,
        by_slot=by_slot,
        by_rule=by_rule,
    )


def log_coverage(coverage: SlotCoverage, *, where: str) -> None:
    """Emit the coverage signal — ALWAYS, including on an empty day.

    Intentionally-left-blank: a projection that classified nothing and a
    classifier that stopped running are indistinguishable without this line, and
    the stage-2 threshold decision is made from exactly this number.
    """
    log.info(
        "tier.slots.coverage",
        where=where,
        total=coverage.total,
        slotted=coverage.slotted,
        unslotted=coverage.unslotted,
        coverage_pct=coverage.coverage_pct,
        by_slot=dict(coverage.by_slot or {}),
        by_rule=dict(coverage.by_rule or {}),
    )


__all__ = [
    "ALL_SLOT_VALUES",
    "CANONICAL_SLOTS",
    "NoOverrides",
    "RULE_CADENCE",
    "RULE_DATED_TASK",
    "RULE_DUE_PATTERN",
    "RULE_EXPLICIT",
    "RULE_LEARNED",
    "RULE_NONE",
    "RULE_SELF_CARE",
    "SLOT_DUTY",
    "SLOT_FUEL",
    "SLOT_RHYTHM",
    "SLOT_UNSLOTTED",
    "SlotCoverage",
    "SlotOverrides",
    "SlotVerdict",
    "classify_slot",
    "log_coverage",
    "normalize_slot",
    "summarize_coverage",
]
