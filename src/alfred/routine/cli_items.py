"""``alfred routine item`` subcommand handlers — Phase 2B B3.

Item-level CRUD on existing routine records:

  - ``alfred routine item add [<record>] <item_text> [--priority X]
    [--target-cadence-days N] [--due-pattern JSON]
    [--surface-at-days N] [--escalate-at-days N]`` — append new item.
  - ``alfred routine item remove [<record>] <item_text>`` — delete one
    item by text match. Strips ``completion_log[<item_text>]`` if present.
  - ``alfred routine item edit [<record>] <item_text> [--text NEW]
    [--priority X] [--target-cadence-days N] [--due-pattern JSON]
    [--surface-at-days N] [--escalate-at-days N]
    [--clear-due-pattern] [--clear-target-cadence-days]`` — mutate one
    item. Renaming (``--text NEW``) migrates ``completion_log[old] →
    completion_log[new]`` atomically.

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

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import frontmatter  # type: ignore[import-untyped]
import structlog
import yaml

from .cli import (
    ITEM_KIND_ADDED,
    ITEM_KIND_AMBIGUOUS_ITEM,
    ITEM_KIND_CADENCE_CONFLICT,
    ITEM_KIND_DUPLICATE_ITEM,
    ITEM_KIND_EDITED,
    ITEM_KIND_INVALID_FIELD,
    ITEM_KIND_REMOVED,
    ITEM_KIND_UNKNOWN_ITEM,
    ITEM_KIND_UNKNOWN_RECORD,
    _check_salem_only,
    _emit_canary,
    _fuzzy_match_vault_wide,
    _ItemCandidate,
    _matches_item,
    _routine_path,
)
from .config import DuePattern, RoutineConfig

log = structlog.get_logger(__name__)


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


def _validate_positive_int(
    value: Any, field_name: str,
) -> tuple[int | None, str | None]:
    """Validate operator-supplied positive-int field
    (target_cadence_days / surface_at_days / escalate_at_days).

    Returns ``(parsed, error)``. ``escalate_at_days`` may be 0
    (T1-on-due semantics); ``target_cadence_days`` and
    ``surface_at_days`` must be > 0. ``field_name`` parameterises
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
    # surface_at_days must be > 0 (zero/negative produce undefined
    # semantics — see tier.compute.compute_auto_t3_candidates'
    # defensive skip on non-positive target).
    if field_name == "escalate_at_days":
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
    record_path.write_text(out, encoding="utf-8")


def _atomic_item_mutate(
    record_path: Path,
    mutator_fn: Callable[
        [list[dict], dict[str, list[str]]],
        _MutationResult,
    ],
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
    due_pattern: Any = None,
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
    return None


def _check_cadence_conflict_on_edit(
    existing_item: dict,
    new_fields: dict,
    *,
    clear_due_pattern: bool,
    clear_target_cadence_days: bool,
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

    return None


# ---------------------------------------------------------------------------
# cmd_item_add
# ---------------------------------------------------------------------------


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
    due_pattern: Any = None,
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
        due_pattern=due_pattern,
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
        for k in (
            "target_cadence_days",
            "surface_at_days",
            "escalate_at_days",
            "due_pattern",
        ):
            if k in new_fields:
                new_item[k] = new_fields[k]
        items.append(new_item)
        return _MutationResult(
            items=items,
            completion_log=completion_log,
            payload_extras={"new_item": new_item},
        )

    result = _atomic_item_mutate(resolved_path, _mutator)

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
            path=str(resolved_path.relative_to(vault_path)),
        )
    return _emit_canary(
        wants_json=wants_json,
        kind=ITEM_KIND_ADDED,
        exit_code=0,
        message=f"Added {item_text!r} to {resolved_record}",
        payload={
            "record": resolved_record,
            "item": item_text,
            "path": str(resolved_path.relative_to(vault_path)),
            **result.payload_extras,
        },
    )


# ---------------------------------------------------------------------------
# cmd_item_remove
# ---------------------------------------------------------------------------


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

    result = _atomic_item_mutate(resolved_path, _mutator)

    if not wants_json:
        log.info(
            "routine.cli.item.removed",
            record=resolved_record,
            item=canonical_item,
            path=str(resolved_path.relative_to(vault_path)),
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
            "path": str(resolved_path.relative_to(vault_path)),
            **result.payload_extras,
        },
    )


# ---------------------------------------------------------------------------
# cmd_item_edit
# ---------------------------------------------------------------------------


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
    due_pattern: Any = None,
    clear_due_pattern: bool = False,
    clear_target_cadence_days: bool = False,
) -> int:
    """Edit one item's fields. Rename (``new_text``) migrates
    ``completion_log[old_text] → completion_log[new_text]`` atomically.

    All field kwargs default ``None`` (no change). ``clear_*`` flags
    are the explicit opt-in for the cadence-mode switch (hard ↔ soft).
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
        due_pattern=due_pattern,
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
        if clear_due_pattern:
            for k in ("due_pattern", "escalate_at_days", "surface_at_days"):
                existing.pop(k, None)
        if clear_target_cadence_days:
            existing.pop("target_cadence_days", None)

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
                ),
            },
        )

    result = _atomic_item_mutate(resolved_path, _mutator)

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
            path=str(resolved_path.relative_to(vault_path)),
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
            "path": str(resolved_path.relative_to(vault_path)),
            **result.payload_extras,
        },
    )


__all__ = [
    "cmd_item_add",
    "cmd_item_remove",
    "cmd_item_edit",
]
