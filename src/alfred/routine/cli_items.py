"""``alfred routine item`` subcommand handlers — Phase 2B B3.

Item-level CRUD on existing routine records:

  - ``alfred routine item add [<record>] <item_text> [--priority X]
    [--target-cadence-days N] [--due-pattern JSON]
    [--surface-at-days N] [--escalate-at-days N]
    [--escalate-after-gap-days N] [--warn-after-gap-days N]
    [--self-care]`` — append new item.
  - ``alfred routine item remove [<record>] <item_text>`` — delete one
    item by text match. Strips ``completion_log[<item_text>]`` if present.
  - ``alfred routine item edit [<record>] <item_text> [--text NEW]
    [--priority X] [--target-cadence-days N] [--due-pattern JSON]
    [--surface-at-days N] [--escalate-at-days N]
    [--escalate-after-gap-days N] [--warn-after-gap-days N]
    [--self-care/--no-self-care] [--clear-due-pattern]
    [--clear-target-cadence-days] [--clear-escalate-after-gap-days]``
    — mutate one item. Renaming (``--text NEW``) migrates
    ``completion_log[old] → completion_log[new]`` atomically.

## The two escalation axes are DIFFERENT fields — do not conflate them

``escalate_at_days`` is days **BEFORE DUE** on a ``due_pattern`` item.
``escalate_after_gap_days`` is days **SINCE LAST COMPLETION** on an item
with **no** ``due_pattern``. The names are one word apart and the meanings
do not overlap; the pair is mutually exclusive and enforced as such below
(``_check_cadence_conflict_on_add`` / ``_on_edit``). See
:data:`ITEM_FIELD_SPECS` for the glosses that get quoted back at a caller
who supplies the wrong one.

Sibling module to ``routine/cli.py`` (which carries B1's ``cmd_done``
+ Phase 1's ``cmd_run_now`` / ``cmd_status``). Split so each module
stays under ~1000 lines as the routine subsystem grows. ``cli.py``
re-exports the three handlers via ``__all__`` so the import path
``from alfred.routine.cli import cmd_item_add`` keeps working.

## Atomic mutation primitive

All three handlers route through ``_atomic_item_mutate(record_path,
mutator_fn)`` — loads the record, calls ``mutator_fn(items_list,
completion_log)`` which returns ``(new_items, new_completion_log)``,
writes back via ``frontmatter.dumps``-with-sort_keys=False (same
shape as B1's ``cmd_done`` to preserve operator key order).

The primitive enforces the contract that items mutations are SINGLE
write: add+remove+edit all replace the entire items list value
(``set_fields``-style overwrite). The unset-capability dual-emission
audit shape applies — one ``op=edit`` row per mutation.

## Cadence-conflict enforcement

A single item can carry EITHER ``target_cadence_days`` (soft cadence,
T3 auto-suggest surface) OR ``due_pattern`` (hard cadence, T1/T2
auto-surface) — never both. This contract was established by Phase
2A-soft-cadence's mutually-exclusive field handling at the aggregator
(``_decide_tier_handoff`` emits ``routine.item_both_cadence_modes``
warn on both-set + prefers ``due_pattern``).

The B3 edit verb enforces the contract at write time, NOT at read
time:
  * ``--target-cadence-days N`` on an item with existing
    ``due_pattern`` → require ``--clear-due-pattern`` OR reject with
    ``ITEM_KIND_CADENCE_CONFLICT``.
  * ``--due-pattern JSON`` on an item with existing
    ``target_cadence_days`` → require
    ``--clear-target-cadence-days`` OR reject with
    ``ITEM_KIND_CADENCE_CONFLICT``.
  * ``--target-cadence-days N`` + ``--due-pattern JSON`` in the
    SAME edit call → reject (same kind) regardless of clear flags.

The add verb's cadence-conflict path: ``--target-cadence-days`` +
``--due-pattern`` both supplied → reject with the same canary kind
(no "existing state" to conflict against, but the mutually-exclusive
semantic still holds).

## Canary kinds

Per ``feedback_cli_json_mode_single_line`` (single-line JSON +
gated logs); see ``cli.py``'s ``_emit_canary`` for the emission
helper. The B3 canary kinds (``ITEM_KIND_*``) live in ``cli.py``
alongside ``DONE_KIND_*`` to keep the cross-agent-contract export
list unified.

## Salem-only enforcement

Same as B1's ``cmd_done`` — each handler calls ``_check_salem_only``
at entry. Routine subsystem refuses non-Salem instances.
"""

from __future__ import annotations

import difflib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import frontmatter  # type: ignore[import-untyped]
import structlog
import yaml

from alfred.common.file_lock import file_rmw_lock
from alfred.vault.paths import (
    VaultContainmentError,
    resolve_in_vault,
    vault_relative,
)

from . import completion as _completion
from .cli import (
    ITEM_KIND_ADDED,
    ITEM_KIND_AMBIGUOUS_ITEM,
    ITEM_KIND_CADENCE_CONFLICT,
    ITEM_KIND_DUPLICATE_ITEM,
    ITEM_KIND_EDITED,
    ITEM_KIND_INVALID_FIELD,
    ITEM_KIND_NOT_LOGGED,
    ITEM_KIND_REMOVED,
    ITEM_KIND_UNKNOWN_ITEM,
    ITEM_KIND_UNKNOWN_RECORD,
    ITEM_KIND_UNLOGGED,
    _check_salem_only,
    _emit_canary,
    _fuzzy_match_vault_wide,
    _ItemCandidate,
    _json_stdout_safe,
    _matches_item,
    _routine_path,
    _today_iso,
)
from .config import DuePattern, RoutineConfig, _coerce_self_care

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Accepted-field registry — THE single source of truth (2026-08-21)
# ---------------------------------------------------------------------------
#
# WHY THIS TABLE EXISTS. On 2026-08-20 the operator asked for a routine item
# with a 7-day neglect-gap escalation. The talker's ``routine_item`` dispatcher
# read a HAND-WRITTEN subset of the ``fields`` dict, so
# ``escalate_after_gap_days`` — a real, load-bearing, tier-engine-consumed
# field — was dropped on the floor and the tool reported ``ok=True``. Operator
# intent was discarded while the reply said "done".
#
# The general defect is not the missing field; it is that the SET of writable
# fields was implicit, duplicated across four layers (argparse, the cli.py
# dispatch call, the handler kwargs, the talker's argv builder), and enforced
# by NONE of them. A field present in three of the four is invisible: every
# layer that does not know about it silently no-ops.
#
# This table is the accepted set. It is consumed by:
#   * :func:`accepted_item_fields` — what the talker dispatcher will accept
#   * :func:`unsupported_item_fields` — what it must REFUSE BY NAME
#   * ``conversation._dispatch_routine_item`` — the argv builder itself
# so acceptance and threading read from ONE list. Adding a field means adding
# a row here plus its argparse flag and its handler kwarg;
# ``test_routine_item_field_registry.py`` drives every row through the real
# dispatcher and fails if a row is not threaded into argv.
#
# NOT a substitute for the argparse surface: argparse is what a HUMAN hits at
# a terminal, and it already refuses unknown ``--flags`` loudly (exit 2). The
# silent-drop surface was only ever the talker's dict→argv hop, which is what
# this table gates.


@dataclass(frozen=True)
class ItemFieldSpec:
    """One field the ``routine_item`` tool accepts, and how to encode it.

    ``name`` is the key in the talker tool's ``fields`` dict (and, for the
    real item fields, the frontmatter key on the stored item). ``flag`` is
    the ``alfred routine item`` CLI long flag it serialises to.

    ``encoding`` selects the argv encoder:
      * ``"value"``     — ``[flag, str(value)]``. The plain scalar class;
        this is the class that got silently dropped, and it is the one the
        dispatcher now builds mechanically from this table.
      * ``"json"``      — dict serialised via ``json.dumps`` (``due_pattern``).
      * ``"bool_pair"`` — ``--x`` / ``--no-x`` (``self_care``).
      * ``"switch"``    — bare presence flag when truthy (the ``clear_*``
        mode-switch opt-ins).
      * ``"text"``      — the rename control key (``fields.text`` → ``--text``).

    ``meaning`` is a one-line operator-facing gloss. It is NOT decoration: it
    is quoted verbatim in the refusal message when a caller supplies a
    near-name, which is the entire fix for the ``escalate_at_days`` vs
    ``escalate_after_gap_days`` trap — two fields one character-class apart
    that mean completely different things (days BEFORE DUE vs days SINCE
    LAST COMPLETION). A refusal that only said "unknown field" would leave
    the caller to guess between them.
    """

    name: str
    flag: str
    encoding: str
    actions: frozenset[str]
    meaning: str


_ADD_EDIT = frozenset({"add", "edit"})
_EDIT_ONLY = frozenset({"edit"})


#: THE accepted set. Order is the operator-facing listing order.
ITEM_FIELD_SPECS: tuple[ItemFieldSpec, ...] = (
    ItemFieldSpec(
        name="text", flag="--text", encoding="text", actions=_EDIT_ONLY,
        meaning="rename the item (migrates its completion_log history)",
    ),
    ItemFieldSpec(
        name="priority", flag="--priority", encoding="value",
        actions=_ADD_EDIT,
        meaning="critical / tracked / aspirational",
    ),
    ItemFieldSpec(
        name="target_cadence_days", flag="--target-cadence-days",
        encoding="value", actions=_ADD_EDIT,
        meaning=(
            "SOFT cadence — aim to do it every N days; surfaces quietly in "
            "T3 at gap >= N and never escalates on its own"
        ),
    ),
    ItemFieldSpec(
        name="due_pattern", flag="--due-pattern", encoding="json",
        actions=_ADD_EDIT,
        meaning=(
            "HARD deadline shape (weekly/monthly/...); the item is due BY a "
            "date rather than every N days"
        ),
    ),
    ItemFieldSpec(
        name="surface_at_days", flag="--surface-at-days", encoding="value",
        actions=_ADD_EDIT,
        meaning=(
            "days BEFORE DUE at which a due_pattern item starts surfacing "
            "as T2. Requires due_pattern"
        ),
    ),
    ItemFieldSpec(
        name="escalate_at_days", flag="--escalate-at-days", encoding="value",
        actions=_ADD_EDIT,
        meaning=(
            "days BEFORE DUE at which a due_pattern item escalates to T1 "
            "(0 = on the due date itself). Requires due_pattern. This is "
            "the DEADLINE axis"
        ),
    ),
    ItemFieldSpec(
        name="escalate_after_gap_days", flag="--escalate-after-gap-days",
        encoding="value", actions=_ADD_EDIT,
        meaning=(
            "days SINCE LAST COMPLETION at which a NO-deadline item "
            "escalates to T1 and visits Duty for the day. Requires NO "
            "due_pattern. This is the NEGLECT-GAP axis"
        ),
    ),
    ItemFieldSpec(
        name="warn_after_gap_days", flag="--warn-after-gap-days",
        encoding="value", actions=_ADD_EDIT,
        meaning=(
            "days SINCE LAST COMPLETION at which the routine section "
            "ANNOTATES the item. Annotation only — never changes its tier"
        ),
    ),
    ItemFieldSpec(
        name="self_care", flag="--self-care", encoding="bool_pair",
        actions=_ADD_EDIT,
        meaning="route the item to the T3 self-care lane",
    ),
    ItemFieldSpec(
        name="clear_due_pattern", flag="--clear-due-pattern",
        encoding="switch", actions=_EDIT_ONLY,
        meaning=(
            "opt-in to strip due_pattern (+ its escalate_at_days / "
            "surface_at_days knobs) when switching off a hard deadline"
        ),
    ),
    ItemFieldSpec(
        name="clear_target_cadence_days", flag="--clear-target-cadence-days",
        encoding="switch", actions=_EDIT_ONLY,
        meaning="opt-in to strip target_cadence_days when switching soft → hard",
    ),
    ItemFieldSpec(
        name="clear_escalate_after_gap_days",
        flag="--clear-escalate-after-gap-days",
        encoding="switch", actions=_EDIT_ONLY,
        meaning=(
            "opt-in to strip escalate_after_gap_days when putting a hard "
            "deadline on an item that had neglect-gap escalation"
        ),
    ),
)


#: ``{action: frozenset(field names)}`` — derived, never hand-maintained.
_ACCEPTED_BY_ACTION: dict[str, frozenset[str]] = {
    action: frozenset(
        spec.name for spec in ITEM_FIELD_SPECS if action in spec.actions
    )
    for action in ("add", "edit", "remove")
}

_SPEC_BY_NAME: dict[str, ItemFieldSpec] = {
    spec.name: spec for spec in ITEM_FIELD_SPECS
}


def accepted_item_fields(action: str) -> frozenset[str]:
    """Field names the ``routine_item`` tool accepts for ``action``.

    ``remove`` accepts NONE — it takes no fields at all, so any field on a
    remove call is operator/model confusion worth naming rather than
    ignoring.
    """
    return _ACCEPTED_BY_ACTION.get(action, frozenset())


def unsupported_item_fields(action: str, keys: Any) -> list[str]:
    """Return the sorted subset of ``keys`` this action does NOT accept.

    Empty list == every supplied field is writable. The caller REFUSES on a
    non-empty result — it must never proceed and report success, which is
    exactly the 2026-08-20 defect this function exists to prevent.
    """
    if not isinstance(keys, dict):
        return []
    accepted = accepted_item_fields(action)
    return sorted(str(k) for k in keys if str(k) not in accepted)


#: Field groups whose members are genuinely CONFUSABLE — near-identical
#: names, non-overlapping meanings. When a refusal's near-name lands inside
#: one of these, the WHOLE group is spelled out rather than just the top
#: string-distance match.
#:
#: Why this is not left to difflib: string distance answers "which name did
#: you probably mis-type", and the failure here is not a typo — it is a
#: caller who knows exactly what they want and reaches for the wrong one of
#: two similar names. Measured: ``escalate_gap_days`` (a caller plainly
#: after the GAP axis) scores CLOSEST to ``escalate_at_days``, the DEADLINE
#: axis. Taking that top match alone would confidently hand back the wrong
#: field. The group makes the distinction unmissable regardless of scoring.
_CONFUSABLE_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({
        "escalate_at_days",
        "escalate_after_gap_days",
        "warn_after_gap_days",
    }),
)


#: Real ``Item`` fields (``alfred.routine.config.Item``) that this tool
#: deliberately CANNOT write, with the reason. Every one of these is a
#: genuine frontmatter field the tier engine reads — they are boarded, not
#: forgotten, and the refusal says so.
#:
#: The bound is mechanical: ``Item.__dataclass_fields__`` minus the accepted
#: set. ``test_known_unwritable_covers_every_boarded_item_field`` asserts
#: this dict covers the difference exactly, so a future field added to
#: ``Item`` and to neither list fails the suite rather than falling into the
#: generic "unknown field" branch and reading as a typo.
_KNOWN_UNWRITABLE_FIELDS: dict[str, str] = {
    "slot": (
        "it is the operator's own Duty/Rhythm/Fuel ruling and the "
        "highest-precedence signal the slot classifier has, so writing it "
        "needs validation against the canonical slot vocabulary that this "
        "tool does not yet carry."
    ),
    "time": (
        "it is the HH:MM clock time for a critical item, and this tool has "
        "no time-format validation."
    ),
}


def _confusable_group_for(field: str) -> frozenset[str]:
    """The confusable group containing ``field``, or an empty frozenset."""
    for group in _CONFUSABLE_GROUPS:
        if field in group:
            return group
    return frozenset()


def describe_unsupported_field(name: str, action: str) -> str:
    """Human-readable refusal line for ONE unsupported field name.

    Names the field, and — when a near-name exists in the accepted set —
    spells out its meaning, plus the meanings of every field in its
    confusable group. The group branch is the ``escalate_at_days`` /
    ``escalate_after_gap_days`` trap: the two differ by one word and mean
    different axes (days-BEFORE-DUE vs days-SINCE-LAST-COMPLETION). Telling
    a caller only "did you mean escalate_at_days?" would actively invite the
    wrong one — which is how the original defect stayed plausible for a day.
    """
    accepted = sorted(accepted_item_fields(action))

    known_gap = _KNOWN_UNWRITABLE_FIELDS.get(name)
    if known_gap is not None:
        # A REAL routine-item field that this tool genuinely cannot write.
        # Saying only "unknown field" here would be a lie by omission — the
        # field exists, the operator may well have meant it, and there IS a
        # path. Naming the path is what stops the model inventing one or
        # quietly giving up. (Intentionally-left-blank: a known gap must
        # announce itself as a gap, not as a nonsense name.)
        return (
            f"{name!r} IS a real routine-item field but the routine_item "
            f"tool cannot write it — {known_gap} Nothing was changed. Do "
            f"not retry through this tool; either use vault_edit on the "
            f"routine record, or tell the operator this one needs a hand "
            f"edit."
        )

    own = _SPEC_BY_NAME.get(name)
    if own is not None and name not in accepted:
        # Real field, wrong action (e.g. a clear_* switch on an add). Name
        # the action, not the field, as the problem — the caller's field
        # name is correct and telling them otherwise would send them
        # hunting for a spelling error that isn't there.
        only = "edit" if "edit" in own.actions else "add"
        return (
            f"{name!r} is not accepted on action={action!r} "
            f"(it is {only}-only) — {own.meaning}."
        )

    near = difflib.get_close_matches(name, accepted, n=1, cutoff=0.55)
    if not near:
        return f"{name!r} is not a field the routine_item tool can write."

    suggestion = near[0]
    spec = _SPEC_BY_NAME.get(suggestion)
    gloss = f" — {spec.meaning}" if spec is not None else ""
    line = (
        f"{name!r} is not a field the routine_item tool can write. "
        f"The closest field it DOES accept is {suggestion!r}{gloss}."
    )

    group = _confusable_group_for(suggestion)
    others = sorted(
        f for f in group if f != suggestion and f in accepted
    )
    if others:
        detail = " ".join(
            f"{f!r} — {_SPEC_BY_NAME[f].meaning}." for f in others
        )
        line += (
            f" CAREFUL — these are easy to confuse and mean different "
            f"things: {detail} Pick by which question you are answering, "
            f"not by which name looks closest."
        )
    return line


def unsupported_fields_message(action: str, unsupported: list[str]) -> str:
    """Full operator-facing refusal message for a set of unsupported fields.

    Always enumerates the accepted set. A caller told only what is wrong has
    to guess what is right; the enumeration is what turns this refusal into
    something the model can immediately retry correctly.
    """
    lines = [describe_unsupported_field(n, action) for n in unsupported]
    accepted = sorted(accepted_item_fields(action))
    return (
        f"routine_item refused: {len(unsupported)} field(s) on "
        f"action={action!r} cannot be written by this tool, so NOTHING was "
        f"changed. "
        + " ".join(lines)
        + f" Fields accepted on action={action!r}: "
        + (", ".join(accepted) if accepted else "(none — remove takes no fields)")
        + ". If you need a field that is not on that list, say so plainly "
        "rather than reporting the change as done."
    )


# ---------------------------------------------------------------------------
# Field validation primitives
# ---------------------------------------------------------------------------


#: Accepted priority values — matches ``aggregator._PRIORITY_ORDER``
#: keys (the aggregator's sort surface). Operator-set values go
#: through ``.lower()`` first; the routine record convention is
#: lowercase but operator typo tolerance is cheap.
_VALID_PRIORITIES: frozenset[str] = frozenset(
    {"critical", "tracked", "aspirational"},
)


def _validate_priority(value: Any) -> tuple[str | None, str | None]:
    """Validate operator-supplied priority value.

    Returns ``(normalised, error)``: ``normalised`` is the lowercased
    string when valid (``None`` when caller didn't supply); ``error``
    is a human-readable message when supplied-but-invalid.
    """
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, f"priority must be a string; got {type(value).__name__}"
    lowered = value.strip().lower()
    if lowered not in _VALID_PRIORITIES:
        return None, (
            f"priority {value!r} not in allowed set "
            f"({', '.join(sorted(_VALID_PRIORITIES))})"
        )
    return lowered, None


#: Fields for which 0 is a MEANINGFUL value, not a no-op.
#:
#: ``escalate_at_days: 0`` means "T1 fires on the due date itself" — a real
#: setting. Every other numeric field treats 0 as undefined semantics and is
#: rejected below.
#:
#: ``escalate_after_gap_days`` is deliberately NOT a member, and the reason is
#: load-bearing rather than stylistic: ``tier.compute.classify_routine_item``
#: gates the neglect-gap branch on ``gap_threshold > 0``, so a stored 0 would
#: be accepted, written to the record, and then silently never fire — the same
#: shape of silent-drop this commit exists to close, just one layer down.
#: Pinned by ``test_escalate_after_gap_days_rejects_zero_because_engine_gates_gt_zero``.
#: A frozenset rather than a ``==`` so a future ``escalate_*`` field cannot
#: join the zero-allowed class by name-prefix accident.
_ZERO_VALID_FIELDS: frozenset[str] = frozenset({"escalate_at_days"})


def _validate_positive_int(
    value: Any, field_name: str,
) -> tuple[int | None, str | None]:
    """Validate operator-supplied positive-int field (target_cadence_days /
    surface_at_days / escalate_at_days / escalate_after_gap_days /
    warn_after_gap_days).

    Returns ``(parsed, error)``. Fields in :data:`_ZERO_VALID_FIELDS` may be
    0; every other numeric field must be > 0. ``field_name`` parameterises
    the zero-vs-positive check.

    The aggregator's defensive parsing tolerates strings that look
    like ints (``raw_item.get("escalate_at_days") → int(...)``); we
    accept the same shape here for operator convenience but reject
    non-numeric strings explicitly so the canary surfaces the typo
    rather than silently storing the wrong shape.
    """
    if value is None:
        return None, None
    # Reject bool BEFORE the int try because ``isinstance(True, int)``
    # is True in Python — would silently coerce ``--target-cadence-days
    # True`` to 1 without this guard.
    if isinstance(value, bool):
        return None, f"{field_name} must be an integer; got bool"
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None, (
            f"{field_name} must be an integer; got {value!r}"
        )
    # escalate_at_days may be 0 (item fires T1 only on the due date
    # itself, Pay-Clinic-Rental shape). target_cadence_days +
    # surface_at_days + the two gap fields must be > 0 (zero/negative
    # produce undefined semantics — see tier.compute's defensive skip on
    # non-positive target, and its ``gap_threshold > 0`` gate).
    if field_name in _ZERO_VALID_FIELDS:
        if parsed < 0:
            return None, (
                f"{field_name} must be >= 0 (got {parsed}); "
                f"0 means T1 fires on the due date itself"
            )
    else:
        if parsed <= 0:
            return None, (
                f"{field_name} must be > 0 (got {parsed}); "
                f"non-positive produces undefined cadence semantics"
            )
    return parsed, None


def _validate_due_pattern(value: Any) -> tuple[dict | None, str | None]:
    """Validate operator-supplied due_pattern dict.

    Accepts either a dict (already-parsed) or a JSON string (operator
    typed ``--due-pattern '{"type": "weekly", "day": "thu"}'`` at
    the CLI). Parses, validates via
    :meth:`alfred.routine.config.DuePattern.from_dict` (which checks
    the ``type`` discriminator against ``DUE_PATTERN_TYPES``), and
    returns the canonical dict shape on success.

    Returns ``(parsed_dict, error)``. ``None, None`` when caller
    didn't supply. ``None, error`` when supplied-but-invalid.
    """
    if value is None:
        return None, None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            return None, (
                f"due_pattern is not valid JSON: {exc}"
            )
    if not isinstance(value, dict):
        return None, (
            f"due_pattern must be a dict or JSON-encoded dict; "
            f"got {type(value).__name__}"
        )
    parsed = DuePattern.from_dict(value)
    if parsed is None:
        return None, (
            f"due_pattern {value!r} did not parse — check 'type' "
            f"against DUE_PATTERN_TYPES (weekly, biweekly, monthly, "
            f"every_n_days, monthly_nth_weekday, weekly_soft)"
        )
    # Convert back to dict for storage (DuePattern dataclass is for
    # in-memory typed access; on-disk storage is the dict shape).
    # Strip default-None fields so YAML output stays clean.
    out: dict[str, Any] = {"type": parsed.type}
    if parsed.day is not None:
        out["day"] = parsed.day
    if parsed.anchor is not None:
        out["anchor"] = parsed.anchor
    if parsed.n is not None:
        out["n"] = parsed.n
    if parsed.weekday is not None:
        out["weekday"] = parsed.weekday
    if parsed.soft:
        out["soft"] = True
    return out, None


# ---------------------------------------------------------------------------
# Atomic mutation primitive
# ---------------------------------------------------------------------------


@dataclass
class _MutationResult:
    """Result of a mutator_fn call (success OR refusal).

    ``items`` is the new items-list value. ``completion_log`` is the
    new completion_log dict value. ``payload_extras`` is a per-action
    dict that's merged into the canary JSON payload (so the
    operator-facing reply can name what changed).

    ``aborted`` is the refusal-path flag — when ``True``, the
    primitive ``_atomic_item_mutate`` will SKIP the on-disk write
    even though the mutator ran to completion. The caller (CLI
    handler) sets this when discovering a precondition violation
    INSIDE the mutator closure (duplicate-item on add, cadence-
    conflict on edit, TOCTOU-disappeared on edit) — the closure
    can't return early via canary emission because the canary
    emission happens AFTER ``_atomic_item_mutate`` returns. Setting
    ``aborted=True`` is the in-band signal "I declined to mutate;
    don't write."

    **Why this matters**: pre-fix the primitive always called
    ``_write_record_state``, which round-trips the YAML through
    ``yaml.dump``. Even identical-content round-trips bump mtime +
    can drift YAML formatting (number normalisation, multiline
    flatten, list-of-dicts reflow). Operator semantics: "I refused,
    your file is untouched." Actual semantics: "I rewrote it
    identically-modulo-formatting." The ``aborted`` gate closes that
    mismatch — reviewer-flagged 2026-05-30 WARN; regression-pinned
    by ``test_atomic_item_mutate_refusal_does_not_touch_file``.
    """
    items: list[dict]
    completion_log: dict[str, list[str]]
    payload_extras: dict[str, Any]
    aborted: bool = False


def _load_record_state(
    record_path: Path,
) -> tuple[dict, list[dict], dict[str, list[str]], frontmatter.Post]:
    """Read a routine record and return its parts.

    Returns ``(fm, items, completion_log, post)``:
      * ``fm`` is the full frontmatter dict (mutated by the caller
        and serialised back via ``frontmatter.Post(content, **fm)``).
      * ``items`` is the items list (or empty list when missing /
        malformed — silent fallback matches the aggregator's tolerance).
      * ``completion_log`` is the completion_log dict, normalised:
        each value is a list of ISO date strings (mirrors the
        normalisation in ``cmd_done``).
      * ``post`` is the raw ``frontmatter.Post`` for body preservation
        on round-trip.
    """
    post = frontmatter.load(str(record_path))
    fm = dict(post.metadata or {})
    raw_items = fm.get("items") or []
    if not isinstance(raw_items, list):
        raw_items = []
    items: list[dict] = []
    for it in raw_items:
        if isinstance(it, dict):
            items.append(dict(it))  # shallow copy — caller mutates safely
    completion_log_raw = fm.get("completion_log") or {}
    if not isinstance(completion_log_raw, dict):
        completion_log_raw = {}
    completion_log: dict[str, list[str]] = {}
    from datetime import date as date_type
    for key, val in completion_log_raw.items():
        if isinstance(val, list):
            normalised: list[str] = []
            for v in val:
                if isinstance(v, date_type):
                    normalised.append(v.isoformat())
                elif isinstance(v, str):
                    normalised.append(v)
            completion_log[str(key)] = normalised
        elif isinstance(val, (str, date_type)):
            completion_log[str(key)] = [
                val.isoformat() if isinstance(val, date_type) else val
            ]
        else:
            completion_log[str(key)] = []
    return fm, items, completion_log, post


def _write_record_state(
    record_path: Path,
    fm: dict,
    items: list[dict],
    completion_log: dict[str, list[str]],
    post: frontmatter.Post,
) -> None:
    """Write the mutated record back to disk.

    Mirrors ``cmd_done``'s round-trip pattern — use ``yaml.dump`` with
    ``sort_keys=False`` so the operator's original key order is
    preserved across the rewrite. ``frontmatter.dumps`` would
    alphabetise keys via ``yaml.safe_dump``'s default behaviour,
    which would scramble operator-edited record layouts.
    """
    fm["items"] = items
    fm["completion_log"] = completion_log
    fm_yaml = yaml.dump(
        fm, default_flow_style=False, allow_unicode=True, sort_keys=False,
    )
    out = f"---\n{fm_yaml}---\n\n{post.content}\n"
    # Atomic write (torn-read fix) — pairs with the file_rmw_lock in
    # _atomic_item_mutate (lost-update fix). See alfred.common.file_lock.
    tmp_path = record_path.with_name(record_path.name + ".tmp")
    tmp_path.write_text(out, encoding="utf-8")
    os.replace(tmp_path, record_path)


def _atomic_item_mutate(
    record_path: Path,
    mutator_fn: Callable[
        [list[dict], dict[str, list[str]]],
        _MutationResult,
    ],
    *,
    vault_path: Path,
) -> _MutationResult:
    """Load the record, run ``mutator_fn``, write atomically (UNLESS
    the mutator signalled ``aborted=True``).

    The mutator function receives the items list + completion_log
    (both already deep-copied by ``_load_record_state``) and returns
    the new state. The primitive then writes the file once IF the
    mutator's return carries ``aborted=False`` (the default —
    success path). When ``aborted=True``, the write is skipped
    entirely: file bytes + mtime stay untouched. The caller (CLI
    handler) then emits the refusal canary based on closure state
    captured during the aborted mutator run.

    There's no rollback story for the success path — the mutator
    either succeeds (returns a ``_MutationResult`` with
    ``aborted=False``) or raises (we don't catch; the CLI handler
    above raises a canary). The ``aborted`` path is the in-band
    refusal channel for preconditions only detectable AFTER load
    (duplicate-item check, cadence-conflict check, TOCTOU-disappeared
    check).
    """
    # Arc #18 containment (defence in depth — ``_routine_path`` already gates
    # the composition its callers use). MUST run before ``file_rmw_lock``, not
    # merely before the write: the lock does ``lock_path.parent.mkdir(
    # parents=True)``, so a late check still creates directories + a ``.lock``
    # sidecar at an out-of-vault target. ``vault_path`` is a REQUIRED keyword
    # for the reason in ``completion._contained`` — a defaulted one would make
    # this silently skippable in production while every test stays green.
    try:
        record_path = resolve_in_vault(
            vault_path, record_path, writer="routine.cli_items.item_mutate",
        )
    except VaultContainmentError:
        log.warning(
            "routine.cli_items.path_escape_denied", path=str(record_path),
        )
        raise

    # Hold the cross-process RMW lock across the WHOLE read → mutate → write,
    # on the SAME record-path sidecar that ``tier.promote`` locks — so
    # concurrent routine writers (cmd_undone/add/remove/edit) + promote
    # serialize instead of clobbering. The flock fixes lost updates;
    # _write_record_state's atomic replace fixes torn reads.
    with file_rmw_lock(record_path):
        fm, items, completion_log, post = _load_record_state(record_path)
        result = mutator_fn(items, completion_log)
        if not result.aborted:
            _write_record_state(
                record_path, fm, result.items, result.completion_log, post,
            )
    return result


# ---------------------------------------------------------------------------
# Record resolution (record-name OR vault-wide fuzzy on item)
# ---------------------------------------------------------------------------


def _resolve_record_for_item_op(
    vault_path: Path,
    record_name: str,
    item_text: str,
    *,
    wants_json: bool,
) -> tuple[Path | None, str, str, int]:
    """Resolve ``(record_path, resolved_record_name, canonical_item_text,
    exit_code)`` for an item-level operation that needs to identify a
    specific existing item.

    Used by ``cmd_item_remove`` and ``cmd_item_edit`` — both need to
    locate an EXISTING item by text. ``cmd_item_add`` does NOT use
    this helper because it creates a new item (no existing one to
    find); it only resolves the record itself.

    On non-zero exit, the canary has already been emitted; caller
    returns the exit code directly. On success exit_code is 0 and
    the path/name/text are populated.

    Routing mirrors ``cmd_done``'s shape:
      * Empty ``record_name`` → vault-wide fuzzy by item text. 0 →
        unknown_item; 2+ → ambiguous_item; 1 → use.
      * Supplied ``record_name`` → strict record lookup + (strict
        OR fuzzy) item lookup on that record.
    """
    resolved_path: Path | None = None
    resolved_record = ""
    canonical_item = item_text

    if record_name and record_name.strip():
        try:
            resolved_path = _routine_path(vault_path, record_name)
            resolved_record = record_name
        except FileNotFoundError:
            return None, "", "", _emit_canary(
                wants_json=wants_json,
                kind=ITEM_KIND_UNKNOWN_RECORD,
                exit_code=1,
                message=(
                    f"Routine record {record_name!r} not found under "
                    f"{vault_path / 'routine'}"
                ),
                payload={"record_name_input": record_name},
            )
    else:
        # Vault-wide fuzzy.
        matches, all_candidates = _fuzzy_match_vault_wide(
            vault_path, item_text,
        )
        if not matches:
            return None, "", "", _emit_canary(
                wants_json=wants_json,
                kind=ITEM_KIND_UNKNOWN_ITEM,
                exit_code=1,
                message=(
                    f"No active routine item matches {item_text!r}. "
                    f"Available items: "
                    f"{', '.join(c.item_text for c in all_candidates[:20])}"
                    f"{' (showing first 20)' if len(all_candidates) > 20 else ''}"
                ),
                payload={
                    "item_text_input": item_text,
                    "available_count": len(all_candidates),
                    "available_items": [
                        {"record": c.record_name, "item": c.item_text}
                        for c in all_candidates
                    ],
                },
            )
        if len(matches) > 1:
            return None, "", "", _emit_canary(
                wants_json=wants_json,
                kind=ITEM_KIND_AMBIGUOUS_ITEM,
                exit_code=1,
                message=(
                    f"{item_text!r} matches {len(matches)} routine items. "
                    f"Ask back with the candidate list."
                ),
                payload={
                    "item_text_input": item_text,
                    "candidates": [
                        {"record": c.record_name, "item": c.item_text}
                        for c in matches
                    ],
                },
            )
        chosen = matches[0]
        resolved_path = chosen.path
        resolved_record = chosen.record_name
        canonical_item = chosen.item_text

    # When record_name was supplied explicitly, verify item exists on
    # THAT record + fall through to fuzzy on this record's items.
    assert resolved_path is not None
    if record_name and record_name.strip():
        fm, raw_items, _comp_log, _post = _load_record_state(resolved_path)
        known_items: list[_ItemCandidate] = []
        for it in raw_items:
            t = str(it.get("text") or "").strip()
            if t:
                known_items.append(_ItemCandidate(
                    record_name=resolved_record,
                    item_text=t,
                    path=resolved_path,
                ))
        known_texts = {c.item_text for c in known_items}
        if item_text not in known_texts:
            on_record_matches = [
                c for c in known_items
                if _matches_item(item_text, c.item_text)
            ]
            if not on_record_matches:
                return None, "", "", _emit_canary(
                    wants_json=wants_json,
                    kind=ITEM_KIND_UNKNOWN_ITEM,
                    exit_code=1,
                    message=(
                        f"Item {item_text!r} not found on routine "
                        f"{resolved_record!r}. Known items: "
                        f"{sorted(known_texts) if known_texts else '(none)'}"
                    ),
                    payload={
                        "item_text_input": item_text,
                        "record": resolved_record,
                        "known_items": sorted(known_texts),
                    },
                )
            if len(on_record_matches) > 1:
                return None, "", "", _emit_canary(
                    wants_json=wants_json,
                    kind=ITEM_KIND_AMBIGUOUS_ITEM,
                    exit_code=1,
                    message=(
                        f"{item_text!r} matches "
                        f"{len(on_record_matches)} items on "
                        f"{resolved_record!r}. Ask back."
                    ),
                    payload={
                        "item_text_input": item_text,
                        "record": resolved_record,
                        "candidates": [
                            {"record": c.record_name, "item": c.item_text}
                            for c in on_record_matches
                        ],
                    },
                )
            canonical_item = on_record_matches[0].item_text

    return resolved_path, resolved_record, canonical_item, 0


def _resolve_record_for_add(
    vault_path: Path,
    record_name: str,
    *,
    wants_json: bool,
) -> tuple[Path | None, str, int]:
    """Resolve ``(record_path, resolved_record_name, exit_code)`` for
    ``cmd_item_add`` — no item-text disambiguation needed (new item).

    ``record_name`` MUST be supplied for add (vault-wide-fuzzy on a
    NEW item that doesn't exist anywhere makes no sense). If the
    caller passes empty, return unknown_record canary so the SKILL
    asks back for the routine name.
    """
    if not (record_name and record_name.strip()):
        return None, "", _emit_canary(
            wants_json=wants_json,
            kind=ITEM_KIND_UNKNOWN_RECORD,
            exit_code=1,
            message=(
                "routine item add requires a record name — vault-wide "
                "fuzzy doesn't apply when adding a new item (no existing "
                "match to anchor against). Ask the operator which routine "
                "the new item belongs to."
            ),
            payload={"record_name_input": record_name},
        )
    try:
        resolved_path = _routine_path(vault_path, record_name)
    except FileNotFoundError:
        return None, "", _emit_canary(
            wants_json=wants_json,
            kind=ITEM_KIND_UNKNOWN_RECORD,
            exit_code=1,
            message=(
                f"Routine record {record_name!r} not found under "
                f"{vault_path / 'routine'}"
            ),
            payload={"record_name_input": record_name},
        )
    return resolved_path, record_name, 0


# ---------------------------------------------------------------------------
# Field-bundle validation (shared by add + edit)
# ---------------------------------------------------------------------------


def _validate_field_bundle(
    *,
    priority: Any = None,
    target_cadence_days: Any = None,
    surface_at_days: Any = None,
    escalate_at_days: Any = None,
    escalate_after_gap_days: Any = None,
    warn_after_gap_days: Any = None,
    due_pattern: Any = None,
    self_care: Any = None,
) -> tuple[dict, str | None]:
    """Validate a bundle of operator-supplied item fields.

    Returns ``(canonical_fields_dict, error)``. The dict contains only
    the fields that were actually supplied + validated. Caller merges
    into the item dict. None-value fields are dropped (operator didn't
    supply them).

    Order of validation: priority → numeric fields → due_pattern.
    First failure short-circuits; subsequent fields aren't checked.
    The canary carries the first error so the operator can fix it
    one at a time.
    """
    out: dict[str, Any] = {}

    pri, err = _validate_priority(priority)
    if err is not None:
        return {}, err
    if pri is not None:
        out["priority"] = pri

    for name, value in (
        ("target_cadence_days", target_cadence_days),
        ("surface_at_days", surface_at_days),
        ("escalate_at_days", escalate_at_days),
        # FUEL-ESCALATION write side (2026-08-21). The NEGLECT-GAP axis —
        # days SINCE LAST COMPLETION — NOT to be confused with
        # ``escalate_at_days`` two lines up, which is days BEFORE DUE.
        ("escalate_after_gap_days", escalate_after_gap_days),
        ("warn_after_gap_days", warn_after_gap_days),
    ):
        parsed, err = _validate_positive_int(value, name)
        if err is not None:
            return {}, err
        if parsed is not None:
            out[name] = parsed

    dp, err = _validate_due_pattern(due_pattern)
    if err is not None:
        return {}, err
    if dp is not None:
        out["due_pattern"] = dp

    # self_care (Q2 read-side → T3 lane). The SET-path: when supplied,
    # coerce via the SAME ``_coerce_self_care`` the read side uses so
    # SET↔READ round-trip exactly. Absent (None) → not written (item has
    # no self_care field → reads False; behavior-preserving default).
    if self_care is not None:
        out["self_care"] = _coerce_self_care(self_care)

    return out, None


# ---------------------------------------------------------------------------
# Cadence-conflict enforcement (shared by add + edit)
# ---------------------------------------------------------------------------


def _check_cadence_conflict_on_add(
    new_fields: dict,
) -> str | None:
    """Reject ``add`` when the operator supplies BOTH cadence modes.

    Returns an error message string when conflict, ``None`` when OK.
    There's no "existing state" for an add, so the only conflict
    surface is: operator supplied both ``target_cadence_days`` AND
    ``due_pattern`` in the same call.
    """
    if (
        new_fields.get("target_cadence_days") is not None
        and new_fields.get("due_pattern") is not None
    ):
        return (
            "Cannot set both ``target_cadence_days`` (soft cadence) "
            "and ``due_pattern`` (hard cadence) on the same item — "
            "they are mutually exclusive. Pick one based on the "
            "SOFT-vs-HARD discrimination table in the SKILL's "
            "'Adjusting routines' section."
        )
    # NEGLECT-GAP axis vs the DEADLINE axis (2026-08-21). Same
    # write-time-not-read-time enforcement as the pair above, and the same
    # rationale as the read side: a raw completion gap is undefined under a
    # deadline item's cycle-based doneness, so the tier engine ignores the
    # gap field and flags ``gap_escalation_conflict``
    # (``tier.compute.classify_routine_item``). Refusing at write time means
    # the operator hears about it now, rather than the field sitting inert
    # on the record forever.
    #
    # NOTE what is deliberately ABSENT: ``escalate_after_gap_days`` +
    # ``target_cadence_days`` is NOT a conflict. Those two COMPOSE and are
    # the common shape — a quiet daily cadence that turns into a Duty visit
    # once neglected (the operator's Pool Chemistry ask is exactly this).
    # Pinned by ``test_gap_escalation_composes_with_target_cadence_on_add``.
    if (
        new_fields.get("escalate_after_gap_days") is not None
        and new_fields.get("due_pattern") is not None
    ):
        return (
            "Cannot set both ``escalate_after_gap_days`` (neglect-gap "
            "escalation — days SINCE LAST COMPLETION) and ``due_pattern`` "
            "(a hard deadline) on the same item. A completion gap is "
            "undefined under a deadline item's cycle-based doneness, so "
            "the tier engine would ignore the gap field entirely. If you "
            "want deadline escalation, use ``escalate_at_days`` (days "
            "BEFORE DUE) instead — that is the due-axis field and it is a "
            "DIFFERENT question from the gap axis."
        )
    return None


def _check_cadence_conflict_on_edit(
    existing_item: dict,
    new_fields: dict,
    *,
    clear_due_pattern: bool,
    clear_target_cadence_days: bool,
    clear_escalate_after_gap_days: bool = False,
) -> str | None:
    """Reject ``edit`` when the operator's change would create a
    both-modes-set state without explicit clear flags.

    Returns an error message string when conflict, ``None`` when OK.

    Three cases produce a conflict:
      1. New ``target_cadence_days`` + existing ``due_pattern``
         (or new ``due_pattern`` in same call) + no
         ``clear_due_pattern`` flag.
      2. New ``due_pattern`` + existing ``target_cadence_days``
         (or new ``target_cadence_days`` in same call) + no
         ``clear_target_cadence_days`` flag.
      3. Both ``target_cadence_days`` AND ``due_pattern`` supplied in
         the same edit call → reject (mutually exclusive even if both
         clear flags are set; nonsensical operator intent).
    """
    setting_target = new_fields.get("target_cadence_days") is not None
    setting_pattern = new_fields.get("due_pattern") is not None
    has_target = existing_item.get("target_cadence_days") is not None
    has_pattern = existing_item.get("due_pattern") is not None

    if setting_target and setting_pattern:
        return (
            "Cannot set both ``target_cadence_days`` (soft cadence) "
            "and ``due_pattern`` (hard cadence) in the same edit — "
            "they are mutually exclusive. Pick one based on the "
            "SOFT-vs-HARD discrimination table in the SKILL."
        )

    if setting_target and has_pattern and not clear_due_pattern:
        return (
            "Item currently uses a hard deadline (``due_pattern``). "
            "Setting ``target_cadence_days`` would create a "
            "both-modes-set state which violates the mutual-exclusion "
            "contract. Pass ``--clear-due-pattern`` (CLI) or "
            "``clear_due_pattern: true`` (talker) to confirm the "
            "switch from hard → soft cadence."
        )

    if setting_pattern and has_target and not clear_target_cadence_days:
        return (
            "Item currently uses a soft cadence "
            "(``target_cadence_days``). Setting ``due_pattern`` would "
            "create a both-modes-set state which violates the "
            "mutual-exclusion contract. Pass "
            "``--clear-target-cadence-days`` (CLI) or "
            "``clear_target_cadence_days: true`` (talker) to confirm "
            "the switch from soft → hard cadence."
        )

    # ---- NEGLECT-GAP axis vs DEADLINE axis (2026-08-21) -----------------
    # Mirror of the two blocks above, on the gap axis. Three cases, and the
    # ``clear_*`` opt-in is what distinguishes "the operator means to switch"
    # from "the operator does not realise these are exclusive".
    setting_gap = new_fields.get("escalate_after_gap_days") is not None
    has_gap = existing_item.get("escalate_after_gap_days") is not None

    if setting_gap and clear_escalate_after_gap_days:
        # Contradictory intent in one call. The clear runs after the set, so
        # proceeding would store nothing while reporting success — the exact
        # silent-discard shape this commit exists to close. Refuse instead.
        return (
            "Cannot both set ``escalate_after_gap_days`` and pass "
            "``clear_escalate_after_gap_days`` in the same edit — the clear "
            "would discard the value you just supplied and the reply would "
            "still say success. Send one or the other."
        )

    if setting_gap and setting_pattern:
        return (
            "Cannot set both ``escalate_after_gap_days`` (neglect-gap "
            "escalation — days SINCE LAST COMPLETION) and ``due_pattern`` "
            "(a hard deadline) in the same edit. They are mutually "
            "exclusive: a completion gap is undefined under cycle-based "
            "doneness. If you meant deadline escalation, the field is "
            "``escalate_at_days`` (days BEFORE DUE)."
        )

    if setting_gap and has_pattern and not clear_due_pattern:
        return (
            "Item currently carries a hard deadline (``due_pattern``), so "
            "``escalate_after_gap_days`` would be written and then ignored "
            "by the tier engine (a completion gap is undefined under "
            "cycle-based doneness). Pass ``--clear-due-pattern`` (CLI) or "
            "``clear_due_pattern: true`` (talker) to drop the deadline and "
            "move this item onto the neglect-gap axis. If instead you want "
            "escalation RELATIVE TO THE DEADLINE, the field you want is "
            "``escalate_at_days`` (days BEFORE DUE) — a different question."
        )

    if setting_pattern and has_gap and not clear_escalate_after_gap_days:
        return (
            "Item currently uses neglect-gap escalation "
            "(``escalate_after_gap_days`` — days since last completion). "
            "Setting ``due_pattern`` would leave that field inert on the "
            "record. Pass ``--clear-escalate-after-gap-days`` (CLI) or "
            "``clear_escalate_after_gap_days: true`` (talker) to confirm "
            "the switch from the gap axis to the deadline axis."
        )

    return None


# ---------------------------------------------------------------------------
# cmd_item_add
# ---------------------------------------------------------------------------


@_json_stdout_safe
def cmd_item_add(
    config: RoutineConfig,
    record_name: str,
    item_text: str,
    *,
    wants_json: bool = False,
    priority: Any = None,
    target_cadence_days: Any = None,
    surface_at_days: Any = None,
    escalate_at_days: Any = None,
    escalate_after_gap_days: Any = None,
    warn_after_gap_days: Any = None,
    due_pattern: Any = None,
    self_care: Any = None,
) -> int:
    """Append a new item to the routine record's items list.

    ``record_name`` is REQUIRED (vault-wide fuzzy doesn't apply for
    add — no existing match to anchor against). Empty record_name
    triggers an unknown_record canary so the SKILL asks back.

    Returns exit code (0 on success, 1 on every refusal canary).
    Idempotency contract: an add with text exactly matching an
    existing item's text raises ``ITEM_KIND_DUPLICATE_ITEM``. (The
    operator may legitimately want two items with the same text; the
    talker grammar can ask back. Per the dispatch's "single-item
    operations are the common case" framing, no batch-add path.)
    """
    _check_salem_only(config)
    vault_path = Path(config.vault_path)

    # ---- Validate item text + field bundle ---------------------------
    if not item_text or not item_text.strip():
        return _emit_canary(
            wants_json=wants_json,
            kind=ITEM_KIND_INVALID_FIELD,
            exit_code=1,
            message="item text is required and must be non-empty",
            payload={"item_text_input": item_text},
        )
    item_text = item_text.strip()

    new_fields, err = _validate_field_bundle(
        priority=priority,
        target_cadence_days=target_cadence_days,
        surface_at_days=surface_at_days,
        escalate_at_days=escalate_at_days,
        escalate_after_gap_days=escalate_after_gap_days,
        warn_after_gap_days=warn_after_gap_days,
        due_pattern=due_pattern,
        self_care=self_care,
    )
    if err is not None:
        return _emit_canary(
            wants_json=wants_json,
            kind=ITEM_KIND_INVALID_FIELD,
            exit_code=1,
            message=err,
            payload={"record": record_name, "item": item_text},
        )

    cadence_err = _check_cadence_conflict_on_add(new_fields)
    if cadence_err is not None:
        return _emit_canary(
            wants_json=wants_json,
            kind=ITEM_KIND_CADENCE_CONFLICT,
            exit_code=1,
            message=cadence_err,
            payload={"record": record_name, "item": item_text},
        )

    # ---- Resolve record ----------------------------------------------
    resolved_path, resolved_record, code = _resolve_record_for_add(
        vault_path, record_name, wants_json=wants_json,
    )
    if code != 0:
        return code
    assert resolved_path is not None

    # ---- Atomic mutation --------------------------------------------
    duplicate_seen = {"hit": False}

    def _mutator(
        items: list[dict],
        completion_log: dict[str, list[str]],
    ) -> _MutationResult:
        # Duplicate check inside the mutator so it runs against
        # post-load state (defends against TOCTOU even though the
        # CLI is single-threaded — operator may have hand-edited
        # the record between resolve + mutate).
        for it in items:
            t = str(it.get("text") or "").strip()
            if t == item_text:
                duplicate_seen["hit"] = True
                # Refusal path: signal aborted so the primitive
                # skips the write — file bytes + mtime stay
                # untouched. Caller emits the duplicate_item canary
                # AFTER the primitive returns.
                return _MutationResult(
                    items=items,
                    completion_log=completion_log,
                    payload_extras={},
                    aborted=True,
                )
        # Build the new item dict — text + priority (defaulting to
        # tracked per the aggregator's convention) + any operator-
        # supplied fields.
        new_item: dict[str, Any] = {"text": item_text}
        new_item["priority"] = new_fields.get("priority", "tracked")
        # Copy every OTHER validated field through. This used to be a
        # hardcoded name tuple, which is precisely how a field can be
        # accepted by the validator and then dropped on the way to disk —
        # the same silent-drop shape as the 2026-08-20 dispatcher defect,
        # one layer down. ``new_fields`` contains only validated keys, so
        # deriving from it makes the omission structurally impossible
        # rather than merely currently-correct.
        for k, v in new_fields.items():
            if k != "priority":
                new_item[k] = v
        items.append(new_item)
        return _MutationResult(
            items=items,
            completion_log=completion_log,
            payload_extras={"new_item": new_item},
        )

    result = _atomic_item_mutate(resolved_path, _mutator, vault_path=vault_path)

    if duplicate_seen["hit"]:
        return _emit_canary(
            wants_json=wants_json,
            kind=ITEM_KIND_DUPLICATE_ITEM,
            exit_code=1,
            message=(
                f"Routine {resolved_record!r} already has an item "
                f"with text {item_text!r}. Pick a different text or "
                f"ask the operator if they meant to edit the existing "
                f"item instead."
            ),
            payload={"record": resolved_record, "item": item_text},
        )

    if not wants_json:
        log.info(
            "routine.cli.item.added",
            record=resolved_record,
            item=item_text,
            path=vault_relative(vault_path, resolved_path),
        )
    return _emit_canary(
        wants_json=wants_json,
        kind=ITEM_KIND_ADDED,
        exit_code=0,
        message=f"Added {item_text!r} to {resolved_record}",
        payload={
            "record": resolved_record,
            "item": item_text,
            "path": vault_relative(vault_path, resolved_path),
            **result.payload_extras,
        },
    )


# ---------------------------------------------------------------------------
# cmd_item_remove
# ---------------------------------------------------------------------------


@_json_stdout_safe
def cmd_item_remove(
    config: RoutineConfig,
    record_name: str,
    item_text: str,
    *,
    wants_json: bool = False,
) -> int:
    """Remove one item by text match. Strips ``completion_log[item]``
    if present so historical entries don't orphan.

    Atomic mutation: items list shrinks by one + completion_log loses
    the matching key, in the same write.
    """
    _check_salem_only(config)
    vault_path = Path(config.vault_path)

    resolved_path, resolved_record, canonical_item, code = (
        _resolve_record_for_item_op(
            vault_path, record_name, item_text,
            wants_json=wants_json,
        )
    )
    if code != 0:
        return code
    assert resolved_path is not None

    removed_completion_dates = {"value": []}

    def _mutator(
        items: list[dict],
        completion_log: dict[str, list[str]],
    ) -> _MutationResult:
        new_items = [
            it for it in items
            if str(it.get("text") or "").strip() != canonical_item
        ]
        # Strip completion_log entry if present.
        if canonical_item in completion_log:
            removed_completion_dates["value"] = completion_log[
                canonical_item
            ]
            new_completion_log = {
                k: v for k, v in completion_log.items()
                if k != canonical_item
            }
        else:
            new_completion_log = completion_log
        return _MutationResult(
            items=new_items,
            completion_log=new_completion_log,
            payload_extras={
                "removed_completion_dates": removed_completion_dates[
                    "value"
                ],
            },
        )

    result = _atomic_item_mutate(resolved_path, _mutator, vault_path=vault_path)

    if not wants_json:
        log.info(
            "routine.cli.item.removed",
            record=resolved_record,
            item=canonical_item,
            path=vault_relative(vault_path, resolved_path),
            completion_entries_dropped=len(
                removed_completion_dates["value"]
            ),
        )
    return _emit_canary(
        wants_json=wants_json,
        kind=ITEM_KIND_REMOVED,
        exit_code=0,
        message=(
            f"Removed {canonical_item!r} from {resolved_record}"
            + (
                f" ({len(removed_completion_dates['value'])} "
                f"completion log entries dropped)"
                if removed_completion_dates["value"]
                else " (no completion log entries to drop)"
            )
        ),
        payload={
            "record": resolved_record,
            "item": canonical_item,
            "path": vault_relative(vault_path, resolved_path),
            **result.payload_extras,
        },
    )


# ---------------------------------------------------------------------------
# cmd_item_edit
# ---------------------------------------------------------------------------


@_json_stdout_safe
def cmd_item_edit(
    config: RoutineConfig,
    record_name: str,
    item_text: str,
    *,
    wants_json: bool = False,
    new_text: str | None = None,
    priority: Any = None,
    target_cadence_days: Any = None,
    surface_at_days: Any = None,
    escalate_at_days: Any = None,
    escalate_after_gap_days: Any = None,
    warn_after_gap_days: Any = None,
    due_pattern: Any = None,
    self_care: Any = None,
    clear_due_pattern: bool = False,
    clear_target_cadence_days: bool = False,
    clear_escalate_after_gap_days: bool = False,
) -> int:
    """Edit one item's fields. Rename (``new_text``) migrates
    ``completion_log[old_text] → completion_log[new_text]`` atomically.

    All field kwargs default ``None`` (no change). ``clear_*`` flags
    are the explicit opt-in for a mode switch: hard ↔ soft cadence
    (``clear_due_pattern`` / ``clear_target_cadence_days``) and
    deadline-axis ↔ neglect-gap-axis (``clear_escalate_after_gap_days``).
    """
    _check_salem_only(config)
    vault_path = Path(config.vault_path)

    # ---- Validate new_text if supplied -------------------------------
    if new_text is not None:
        if not isinstance(new_text, str) or not new_text.strip():
            return _emit_canary(
                wants_json=wants_json,
                kind=ITEM_KIND_INVALID_FIELD,
                exit_code=1,
                message=(
                    "new text (rename) must be a non-empty string; "
                    f"got {new_text!r}"
                ),
                payload={"item_text_input": item_text},
            )
        new_text = new_text.strip()

    # ---- Validate field bundle ---------------------------------------
    new_fields, err = _validate_field_bundle(
        priority=priority,
        target_cadence_days=target_cadence_days,
        surface_at_days=surface_at_days,
        escalate_at_days=escalate_at_days,
        escalate_after_gap_days=escalate_after_gap_days,
        warn_after_gap_days=warn_after_gap_days,
        due_pattern=due_pattern,
        self_care=self_care,
    )
    if err is not None:
        return _emit_canary(
            wants_json=wants_json,
            kind=ITEM_KIND_INVALID_FIELD,
            exit_code=1,
            message=err,
            payload={"record": record_name, "item": item_text},
        )

    # ---- Resolve record + canonical item text ------------------------
    resolved_path, resolved_record, canonical_item, code = (
        _resolve_record_for_item_op(
            vault_path, record_name, item_text,
            wants_json=wants_json,
        )
    )
    if code != 0:
        return code
    assert resolved_path is not None

    cadence_conflict = {"err": None}

    def _mutator(
        items: list[dict],
        completion_log: dict[str, list[str]],
    ) -> _MutationResult:
        # Find the existing item (canonical_item is guaranteed to
        # match exactly post-resolve).
        target_idx = -1
        for i, it in enumerate(items):
            if str(it.get("text") or "").strip() == canonical_item:
                target_idx = i
                break
        if target_idx < 0:
            # Shouldn't happen — resolve already verified — but
            # defensive guard against TOCTOU. Refusal path: aborted
            # so the primitive skips the write (file untouched).
            cadence_conflict["err"] = (
                f"Item {canonical_item!r} disappeared between resolve "
                f"and mutate; operator hand-edit during the operation?"
            )
            return _MutationResult(
                items=items,
                completion_log=completion_log,
                payload_extras={},
                aborted=True,
            )

        existing = items[target_idx]

        # Cadence-conflict check uses the EXISTING item's current
        # state — that's the right anchor for "is this edit OK?"
        c_err = _check_cadence_conflict_on_edit(
            existing,
            new_fields,
            clear_due_pattern=clear_due_pattern,
            clear_target_cadence_days=clear_target_cadence_days,
            clear_escalate_after_gap_days=clear_escalate_after_gap_days,
        )
        if c_err is not None:
            # Refusal path: aborted so the primitive skips the write
            # (file untouched). Caller emits the cadence_conflict
            # canary AFTER the primitive returns.
            cadence_conflict["err"] = c_err
            return _MutationResult(
                items=items,
                completion_log=completion_log,
                payload_extras={},
                aborted=True,
            )

        # Apply field changes to the existing item dict in place
        # (we already shallow-copied via _load_record_state).
        for k, v in new_fields.items():
            existing[k] = v

        # Apply clear flags AFTER setting new fields. Two cases:
        #   1. clear_due_pattern=True + new target_cadence_days set
        #      → switch hard → soft, strip due_pattern + the related
        #        escalate_at_days / surface_at_days knobs (they only
        #        make sense alongside due_pattern).
        #   2. clear_target_cadence_days=True + new due_pattern set
        #      → switch soft → hard, strip target_cadence_days.
        # Also support clear-without-new-set (operator explicitly
        # wants to remove cadence entirely — falls back to the
        # gap-based annotation for tracked items).
        #   3. clear_escalate_after_gap_days=True → drop the neglect-gap
        #      threshold (the deadline-axis switch, or plain removal).
        if clear_due_pattern:
            # NOTE the omission: ``escalate_after_gap_days`` is NOT stripped
            # here. ``escalate_at_days`` / ``surface_at_days`` are DUE-axis
            # knobs that are meaningless without a due_pattern, but the gap
            # threshold is the opposite — it only WORKS once the deadline is
            # gone. Stripping it would break the hard → gap switch (drop the
            # deadline and set a gap threshold in one call), which is a
            # supported path. Pinned by
            # ``test_clear_due_pattern_preserves_escalate_after_gap_days``.
            for k in ("due_pattern", "escalate_at_days", "surface_at_days"):
                existing.pop(k, None)
        if clear_target_cadence_days:
            existing.pop("target_cadence_days", None)
        if clear_escalate_after_gap_days:
            existing.pop("escalate_after_gap_days", None)

        # Handle text rename: update items[i].text AND migrate
        # completion_log key.
        new_completion_log = completion_log
        renamed_to: str | None = None
        if new_text is not None and new_text != canonical_item:
            existing["text"] = new_text
            renamed_to = new_text
            if canonical_item in completion_log:
                # Migrate history under the new key.
                new_completion_log = dict(completion_log)
                new_completion_log[new_text] = new_completion_log.pop(
                    canonical_item,
                )

        return _MutationResult(
            items=items,
            completion_log=new_completion_log,
            payload_extras={
                "renamed_to": renamed_to,
                "fields_changed": sorted(new_fields.keys()) + (
                    ["text"] if renamed_to else []
                ) + (
                    ["due_pattern (cleared)"] if clear_due_pattern else []
                ) + (
                    ["target_cadence_days (cleared)"]
                    if clear_target_cadence_days else []
                ) + (
                    ["escalate_after_gap_days (cleared)"]
                    if clear_escalate_after_gap_days else []
                ),
            },
        )

    result = _atomic_item_mutate(resolved_path, _mutator, vault_path=vault_path)

    if cadence_conflict["err"] is not None:
        return _emit_canary(
            wants_json=wants_json,
            kind=ITEM_KIND_CADENCE_CONFLICT,
            exit_code=1,
            message=cadence_conflict["err"],
            payload={"record": resolved_record, "item": canonical_item},
        )

    final_text = result.payload_extras.get("renamed_to") or canonical_item
    if not wants_json:
        log.info(
            "routine.cli.item.edited",
            record=resolved_record,
            item=canonical_item,
            renamed_to=result.payload_extras.get("renamed_to"),
            fields_changed=result.payload_extras.get("fields_changed", []),
            path=vault_relative(vault_path, resolved_path),
        )
    return _emit_canary(
        wants_json=wants_json,
        kind=ITEM_KIND_EDITED,
        exit_code=0,
        message=(
            f"Edited {canonical_item!r} on {resolved_record}"
            + (
                f" (renamed to {final_text!r})"
                if result.payload_extras.get("renamed_to")
                else ""
            )
        ),
        payload={
            "record": resolved_record,
            "item": canonical_item,
            "path": vault_relative(vault_path, resolved_path),
            **result.payload_extras,
        },
    )


# ---------------------------------------------------------------------------
# cmd_undone — surgical single-date un-log (the inverse of cmd_done)
# ---------------------------------------------------------------------------


def _resolve_undone_date(
    date_str: str | None, tz_name: str,
) -> tuple[str, str | None]:
    """Resolve the date to un-log. Returns ``(iso, error)``.

    Default = today in ``tz_name`` (mirrors ``cmd_done``'s today-resolution).
    A provided value is parse-validated as ``YYYY-MM-DD``. Unlike
    ``cmd_done``'s ``_validate_completed_at``, there is NO future-date
    rejection — a future date simply can't be present in the log, so it
    falls through to the ``not_logged`` no-op rather than erroring.
    """
    from datetime import date as date_type

    iso_today = _today_iso(tz_name)
    if date_str is None or not str(date_str).strip():
        return iso_today, None
    try:
        parsed = date_type.fromisoformat(str(date_str).strip()[:10])
    except ValueError:
        return iso_today, (
            f"date {date_str!r} is not a valid ISO date (expected YYYY-MM-DD)"
        )
    return parsed.isoformat(), None


@_json_stdout_safe
def cmd_undone(
    config: RoutineConfig,
    record_name: str,
    item_text: str,
    *,
    date: str | None = None,
    wants_json: bool = False,
) -> int:
    """Remove ONE completion date from ``completion_log[item]`` — the surgical
    inverse of :func:`alfred.routine.cli.cmd_done`.

    ``record_name`` may be empty/whitespace to trigger vault-wide fuzzy match
    on the item text (same routing as ``done`` / ``item remove``). ``date`` is
    an optional ``YYYY-MM-DD`` (default today in ``config.schedule.timezone``)
    naming the entry to remove.

    Returns the exit code (0 on success OR the date-not-present no-op; 1 on a
    resolution canary). Reuses ``_resolve_record_for_item_op`` (record/item
    resolution + its unknown_record / unknown_item / ambiguous_item canaries)
    and ``_atomic_item_mutate`` (single-write mutation, with the ``aborted``
    gate leaving the file untouched on the no-op).

    Semantics:
      * date present → removed; remaining dates retained; if the list empties,
        ``completion_log[item]`` is kept as ``[]`` (NOT dropped — mirrors
        ``cmd_done``'s shape; only ``item remove`` drops the whole key because
        it removes the whole ITEM). Canary ``unlogged``, exit 0.
      * date NOT present → ``aborted=True`` (file bytes + mtime untouched);
        canary ``not_logged`` + explicit "‹item› was not logged on ‹date›"
        message, exit 0. The desired end-state already holds — an idempotent
        no-op, NOT a silent success (intentionally-left-blank).
      * item / record not found / ambiguous → the shared resolver canaries.

    Scope: Salem-only via ``_check_salem_only`` (same direct-frontmatter-write
    gate as ``done`` / ``item remove`` — no ``vault_edit`` / ``check_scope``
    round-trip). Deliberately NOT coupled to the matcher reject corpus: un-log
    names the item explicitly (or vault-wide fuzzy → exactly one) and writes
    ONLY ``completion_log`` — no confidence judgment, no glossary/pending write.
    """
    _check_salem_only(config)
    vault_path = Path(config.vault_path)

    iso, date_error = _resolve_undone_date(date, config.schedule.timezone)
    if date_error is not None:
        return _emit_canary(
            wants_json=wants_json,
            kind=ITEM_KIND_INVALID_FIELD,
            exit_code=1,
            message=date_error,
            payload={"date_input": date},
        )

    resolved_path, resolved_record, canonical_item, code = (
        _resolve_record_for_item_op(
            vault_path, record_name, item_text, wants_json=wants_json,
        )
    )
    if code != 0:
        return code
    assert resolved_path is not None

    # ---- Shared completion_log removal (identity-pinned with the board) ---
    # The un-log WRITE is delegated to the SAME
    # ``alfred.routine.completion.mark_routine_item_undone`` the board's
    # ``/feed/act`` undo_done dispatcher calls — single writer per lane. The
    # resolver above (``_resolve_record_for_item_op``) still owns the
    # record/item resolution + its unknown_record / unknown_item / ambiguous
    # canaries; the writer re-checks existence under its own file_rmw_lock and
    # leaves the file UNTOUCHED on the not-logged no-op (its ``aborted`` gate).
    # Called via the module attribute so the identity pin can monkeypatch one
    # symbol and see both callers route through it.
    rel_path = vault_relative(vault_path, resolved_path)
    result = _completion.mark_routine_item_undone(
        resolved_path, canonical_item, iso, vault_path=vault_path,
    )

    if result.kind == _completion.DONE_KIND_UNKNOWN_ITEM:
        # TOCTOU: the item vanished between resolution and the writer's lock.
        return _emit_canary(
            wants_json=wants_json,
            kind=ITEM_KIND_UNKNOWN_ITEM,
            exit_code=1,
            message=(
                f"Item {canonical_item!r} not found on {resolved_record!r}."
            ),
            payload={
                "item_text_input": canonical_item,
                "record": resolved_record,
            },
        )
    if result.kind == _completion.DONE_KIND_UNKNOWN_RECORD:
        return _emit_canary(
            wants_json=wants_json,
            kind=ITEM_KIND_UNKNOWN_RECORD,
            exit_code=1,
            message=(
                f"Routine record {resolved_record!r} could not be read."
            ),
            payload={"record_name_input": resolved_record},
        )

    if result.kind == _completion.UNDONE_KIND_NOT_LOGGED:
        if not wants_json:
            log.info(
                "routine.cli.item.not_logged",
                record=resolved_record,
                item=canonical_item,
                date=iso,
                path=rel_path,
            )
        return _emit_canary(
            wants_json=wants_json,
            kind=ITEM_KIND_NOT_LOGGED,
            exit_code=0,
            message=(
                f"{canonical_item!r} was not logged on {iso} — "
                f"nothing to remove."
            ),
            payload={
                "record": resolved_record,
                "item": canonical_item,
                "date": iso,
                "path": rel_path,
                "removed": False,
            },
        )

    if not wants_json:
        log.info(
            "routine.cli.item.unlogged",
            record=resolved_record,
            item=canonical_item,
            date=iso,
            path=rel_path,
            remaining=len(result.remaining_dates),
        )
    return _emit_canary(
        wants_json=wants_json,
        kind=ITEM_KIND_UNLOGGED,
        exit_code=0,
        message=f"Un-logged: {resolved_record} / {canonical_item} @ {iso}",
        payload={
            "record": resolved_record,
            "item": canonical_item,
            "date": iso,
            "path": rel_path,
            "removed": True,
            "remaining_dates": result.remaining_dates,
        },
    )


__all__ = [
    "cmd_item_add",
    "cmd_item_remove",
    "cmd_item_edit",
    "cmd_undone",
    # Accepted-field registry (2026-08-21) — consumed by the talker
    # dispatcher in ``alfred.telegram.conversation`` for BOTH argv building
    # and unsupported-field refusal. Exported so the two cannot drift.
    "ITEM_FIELD_SPECS",
    "ItemFieldSpec",
    "accepted_item_fields",
    "describe_unsupported_field",
    "unsupported_fields_message",
    "unsupported_item_fields",
]
