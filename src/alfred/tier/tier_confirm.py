"""Neutral slot-accept writer — the board's tier-curation commit lane (C2).

Phase C slice 2 (board ACCEPT path): C1 made COMPLETION real; C2 makes
ACCEPTANCE real. The brief's "reply T1 confirm / T2 confirm / T3 confirm
<item>" text-asks become card decisions — the operator taps *Accept* on an
auto-surfaced slot candidate and it lands on today's tier list.

This module is the DETERMINISTIC writer for that commit. It persists an
auto-surfaced candidate INTO the daily ``tier_curation`` block
(``vault/daily/<date>.md``) — the SAME data home the talker's "T1/T2/T3
confirm" grammar targets (which today does an LLM whole-block rewrite; task
#21 will route that grammar through THIS writer, one writer per lane, the C1
lineage law). It is the sibling of ``routine/completion.py`` (routine lane) and
``tier/task_completion.py`` (task lane): CODE, not the LLM, is the allowlist.

What each tier writes (per the ``daily_curation`` schema):

  * **T1** → append a :class:`~alfred.tier.daily_curation.T1T2Entry` with
    ``confirmed=True`` and the candidate's ORIGINAL auto ``source`` preserved
    (the documented ``source: auto-due, confirmed: true`` shape — an auto-T1
    candidate the operator confirmed). ``confirmed=True`` is what flips the
    producer's ``candidate`` flag off, so the re-emitted item is committed.
  * **T2** → append a ``T1T2Entry`` with ``source="operator"`` and no
    ``confirmed`` field (T2 has no ``confirmed`` — "operator-add IS the
    confirmation"). ``source="operator"`` (NOT the auto source) is what flips
    ``candidate`` off, since T2 can't rely on ``confirmed``.
  * **T3** → append a :class:`~alfred.tier.daily_curation.T3Entry` (free-text)
    with ``source="operator"``. T3 is free-text-only by data-model; a
    routine-origin or task-origin T3 candidate commits as its free-text item;
    the routine/task anchor is intentionally not carried (the done-state then
    resolves through the board's ``mark_t3_done`` / the same-text completion
    fallback in ``entry_is_done``).

Entry identity comes from the producer-stamped evidence (trusted, same class as
``last_batch``): ``origin == "task"`` → a ``[[task/<name>]]`` wikilink built
from the display ``name`` (NOT the path stem — ``compute_today_view`` dedups
task lanes by the ``name`` field, so a stem-built wikilink would double-surface
the auto candidate alongside the committed entry); ``origin == "routine_item"``
→ the ``(routine_record, item_text)`` pair.

Contract (same law as the routine/tier/task board writers):

  * **Idempotent.** An entry already present in the target lane by its identity
    (task display-name / routine ``(record, text)`` / lowered T3 text) →
    ``idempotent_noop``, NO duplicate, NO write.
  * **Fail-loud.** ``tier`` outside ``{1, 2, 3}`` → ``invalid_tier``; missing
    the identity the lane needs (a T1/T2 with neither a task ``name`` nor a
    routine ``(record, text)``; a T3 with no text) → ``thin_evidence``. Never
    guess.
  * **Locked + atomic.** The whole load → check → append → write runs under
    ``file_rmw_lock`` on the daily file. It calls ``load_daily_curation`` +
    ``_write_curation_into_daily_file`` (the lock-free inner write) DIRECTLY —
    NEVER ``save_tier_curation``, which re-takes ``file_rmw_lock`` on a second
    fd and self-deadlocks (``flock(LOCK_EX)`` on a distinct open file
    description blocks even within one process). This mirrors ``mark_t3_done``
    exactly; see ``_write_curation_into_daily_file``'s precondition note.
  * **Structured result + one named log per branch** (intentionally-left-blank)
    so the outcome is grep-able without re-reading the file. The caller owns
    the operator-facing reply / optimistic render.

The write touches ONLY the top-level ``tier_curation`` key (via the shared
``_write_curation_into_daily_file``), so it honours the existing
``TALKER_TIER_CURATION_FIELDS`` allowlist with ZERO widening.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path
from typing import Any

import structlog

from alfred.common.file_lock import file_rmw_lock
from alfred.tier.daily_curation import (
    DailyCuration,
    T1T2Entry,
    T3Entry,
    _daily_file_path,
    _write_curation_into_daily_file,
    load_daily_curation,
)

log = structlog.get_logger(__name__)

# --- result kinds ------------------------------------------------------------
# DONE_KIND-style vocab (string values mirror the routine/tier/task writers
# where they overlap, so a caller maps them without translating).
CONFIRM_KIND_SUCCESS = "success"
CONFIRM_KIND_IDEMPOTENT_NOOP = "idempotent_noop"
CONFIRM_KIND_INVALID_TIER = "invalid_tier"
CONFIRM_KIND_THIN_EVIDENCE = "thin_evidence"

# The committed ``source`` for a T2/T3 accept. T1 preserves the candidate's
# ORIGINAL auto source (paired with ``confirmed=True``); T2/T3 have no
# ``confirmed`` field, so they must write a NON-auto source to flip the
# producer's ``candidate`` derivation off. "operator" — the operator's accept
# IS the confirmation.
_COMMITTED_SOURCE = "operator"


@dataclass
class ConfirmResult:
    """Outcome of a :func:`confirm_slot_candidate` call.

    ``kind`` is the discriminator (a ``CONFIRM_KIND_*`` constant). ``tier`` is
    the lane written (or the offending value on ``invalid_tier``). ``name`` is
    the committed display string (task name / routine item text / free-text T3
    item) echoed for the caller's reply / optimistic render. ``date`` is the ISO
    date of the daily file acted on. ``changed`` is True only on a real write
    (the ``success`` path); False on the idempotent no-op and the error
    end-states. ``daily_file`` is the absolute path written.
    """

    kind: str
    tier: int | None = None
    name: str = ""
    date: str = ""
    changed: bool = False
    daily_file: str = ""

    @property
    def ok(self) -> bool:
        """True for the non-error end-states (the candidate IS on today's list):
        ``success`` / ``idempotent_noop``. ``invalid_tier`` / ``thin_evidence``
        are the error end-states."""
        return self.kind in (CONFIRM_KIND_SUCCESS, CONFIRM_KIND_IDEMPOTENT_NOOP)


def _task_wikilink(name: str) -> str:
    """Build the curation ``task:`` wikilink from a display name — the shape
    ``compute_today_view._curated_task_display_name`` reads back and the shape
    that dedups against the auto-T1 candidate (keyed on the ``name`` field)."""
    return f"[[task/{name}]]"


def _wikilink_display_name(wikilink: str) -> str:
    """Extract the display name from a ``[[task/Name]]`` wikilink (or bare name).

    Byte-mirror of ``compute._curated_task_display_name`` — kept local so this
    writer imports nothing from the heavy ``tier.compute`` module (no cycle, no
    frontmatter pull at import). Used by the task-lane idempotency check.
    """
    s = (wikilink or "").strip().strip("[]").strip()
    if "/" in s:
        s = s.split("/", 1)[1]
    if "|" in s:
        s = s.split("|", 1)[1]
    return s.strip()


def _task_already_present(entries: list[T1T2Entry], name_lower: str) -> bool:
    """True iff a T1/T2 entry already commits the task with this display name."""
    for e in entries:
        if e.task and _wikilink_display_name(e.task).strip().lower() == name_lower:
            return True
    return False


def _routine_already_present(
    entries: list[T1T2Entry], record_lower: str, text_lower: str,
) -> bool:
    """True iff a T1/T2 entry already commits this routine ``(record, text)``."""
    for e in entries:
        ri = e.routine_item
        if not isinstance(ri, dict):
            continue
        rec = str(ri.get("record") or "").strip().lower()
        txt = str(ri.get("text") or "").strip().lower()
        if rec == record_lower and txt == text_lower:
            return True
    return False


def _t3_already_present(entries: list[T3Entry], text_lower: str) -> bool:
    """True iff a T3 entry already commits this free-text item (lowered match)."""
    return any((e.item or "").strip().lower() == text_lower for e in entries)


def confirm_slot_candidate(
    vault_path: Path,
    *,
    tier: Any,
    origin: str,
    name: str,
    path: str,
    routine_record: str | None,
    item_text: str | None,
    source: str,
    date: _date,
) -> ConfirmResult:
    """Commit an auto-surfaced slot candidate into ``date``'s ``tier_curation``.

    Persists the candidate as the appropriate entry shape for its ``tier``
    (T1/T2 → ``T1T2Entry``; T3 → free-text ``T3Entry``), keyed by the
    producer-stamped evidence identity. The WHOLE load → idempotency-check →
    append → write runs inside ``file_rmw_lock`` (one critical section) so a
    concurrent accept / aggregator RMW can't lost-update; the inner write is the
    lock-free ``_write_curation_into_daily_file`` (NEVER ``save_tier_curation``,
    which self-deadlocks — see the module docstring).

    ``path`` is accepted for signature symmetry with the completion writers /
    for future use; the task-lane wikilink is built from ``name`` (the dedup
    key), not ``path``.

    Returns:
      * ``invalid_tier`` — ``tier`` not in ``{1, 2, 3}`` (no write).
      * ``thin_evidence`` — the lane's required identity is missing (no write).
      * ``idempotent_noop`` — the entry is already committed in the target lane
        (no duplicate, no write).
      * ``success`` — appended (one write).

    Every branch emits ONE named log (intentionally-left-blank) so the outcome
    is grep-able.
    """
    vault_path = Path(vault_path)
    date_iso = date.isoformat()

    # --- gate 1: tier vocabulary (fail loud) ------------------------------
    try:
        tier_int = int(tier)
    except (TypeError, ValueError):
        tier_int = -1
    if tier_int not in (1, 2, 3):
        log.info(
            "tier.confirm.invalid_tier", tier=tier, origin=origin, name=name,
            date=date_iso,
        )
        return ConfirmResult(
            kind=CONFIRM_KIND_INVALID_TIER, tier=None, name=name, date=date_iso,
        )

    name = (name or "").strip()
    routine_record = (routine_record or "").strip() or None
    item_text = (item_text or "").strip() or None
    is_routine = origin == "routine_item" or bool(routine_record)

    # --- gate 2: resolve the entry + identity (fail loud on thin evidence) -
    #
    # ``committed_name`` is the display string echoed back / used for the
    # optimistic render. ``identity`` is the tuple the idempotency check keys on.
    if tier_int in (1, 2):
        if origin == "task":
            if not name:
                return _thin(origin, name, date_iso, "task_no_name")
            committed_name = name
            identity = ("task", name.lower())
            entry_source, confirmed = _t12_source_confirmed(tier_int, source)
            entry = T1T2Entry(
                task=_task_wikilink(name), source=entry_source, confirmed=confirmed,
            )
        elif is_routine:
            if not (routine_record and item_text):
                return _thin(origin, name, date_iso, "routine_no_record_or_text")
            committed_name = item_text
            identity = ("routine", routine_record.lower(), item_text.lower())
            entry_source, confirmed = _t12_source_confirmed(tier_int, source)
            entry = T1T2Entry(
                routine_item={"record": routine_record, "text": item_text},
                source=entry_source, confirmed=confirmed,
            )
        else:
            return _thin(origin, name, date_iso, "unknown_origin")
    else:  # tier_int == 3 — free-text lane
        free_text = item_text or name
        if not free_text:
            return _thin(origin, name, date_iso, "t3_no_text")
        committed_name = free_text
        identity = ("t3", free_text.strip().lower())
        entry = T3Entry(item=free_text, source=_COMMITTED_SOURCE)

    # --- gate 3: locked RMW (load → idempotency → append → inner write) ----
    daily_file = _daily_file_path(vault_path, date)
    with file_rmw_lock(daily_file):
        curation = load_daily_curation(vault_path, date)
        if curation is None:
            # No daily file / no tier_curation block yet — accept into a fresh
            # curation. ``_write_curation_into_daily_file`` seeds a minimal
            # ``type: daily`` file (and preserves the aggregator's keys if the
            # file already exists with other frontmatter).
            curation = DailyCuration()

        lane = {1: curation.t1, 2: curation.t2, 3: curation.t3}[tier_int]

        if _entry_present(lane, identity):
            log.info(
                "tier.confirm.idempotent_noop", tier=tier_int, origin=origin,
                name=committed_name, date=date_iso,
            )
            return ConfirmResult(
                kind=CONFIRM_KIND_IDEMPOTENT_NOOP, tier=tier_int,
                name=committed_name, date=date_iso, changed=False,
                daily_file=str(daily_file),
            )

        lane.append(entry)
        _write_curation_into_daily_file(daily_file, date, curation)

    log.info(
        "tier.confirm.success", tier=tier_int, origin=origin,
        name=committed_name, date=date_iso,
    )
    return ConfirmResult(
        kind=CONFIRM_KIND_SUCCESS, tier=tier_int, name=committed_name,
        date=date_iso, changed=True, daily_file=str(daily_file),
    )


def _t12_source_confirmed(tier: int, source: str) -> tuple[str, bool | None]:
    """The committed ``(source, confirmed)`` for a T1/T2 entry.

    * T1 → preserve the candidate's ORIGINAL auto ``source`` (the documented
      ``source: auto-due, confirmed: true`` shape) with ``confirmed=True``.
      ``confirmed=True`` is what the producer reads to flip ``candidate`` off.
    * T2 → ``source="operator"`` (NON-auto, so ``candidate`` flips off — T2 has
      no ``confirmed`` field), ``confirmed=None``.
    """
    if tier == 1:
        return (source or _COMMITTED_SOURCE), True
    return _COMMITTED_SOURCE, None


def _entry_present(lane: list[Any], identity: tuple) -> bool:
    """Dispatch the per-lane idempotency check on the identity tuple."""
    kind = identity[0]
    if kind == "task":
        return _task_already_present(lane, identity[1])
    if kind == "routine":
        return _routine_already_present(lane, identity[1], identity[2])
    return _t3_already_present(lane, identity[1])


def _thin(origin: str, name: str, date_iso: str, reason: str) -> ConfirmResult:
    """Build + log a ``thin_evidence`` refusal (never guess an identity)."""
    log.info(
        "tier.confirm.thin_evidence", origin=origin, name=name, date=date_iso,
        reason=reason,
    )
    return ConfirmResult(
        kind=CONFIRM_KIND_THIN_EVIDENCE, name=name, date=date_iso,
    )


__all__ = [
    "CONFIRM_KIND_IDEMPOTENT_NOOP",
    "CONFIRM_KIND_INVALID_TIER",
    "CONFIRM_KIND_SUCCESS",
    "CONFIRM_KIND_THIN_EVIDENCE",
    "ConfirmResult",
    "confirm_slot_candidate",
]
