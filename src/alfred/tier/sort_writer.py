"""Neutral slot-assignment writer — the sort lane's one write.

The operator's 2026-08-19 report: four dated tasks sat under the board's "NOT
SORTED YET" heading with DONE as their only affordance — *"These 'not sorted'
items have no way of being sorted."* The board could SHOW the residue and could
not ACT on it, so an item the classifier honestly refused to guess at stayed
refused forever. This module is the missing half: the deterministic writer that
records the operator's own answer.

**What it writes is rule 1's input, and that is the whole design.**
``tier.slots.classify_slot`` reads ``entry.explicit_slot`` first and calls it
"the operator's word is final". That value is sourced from a ``slot:`` field —
on a task record's frontmatter, or on a routine record's ``items[]`` entry. So
sorting an item is not a new mechanism bolted beside the classifier; it is
filling in the field the classifier already asks about, and the next projection
picks it up with no further plumbing. The precedent is
``transport.scheduler.clear_remind_at_and_stamp``, which writes the same field
for the same reason when a snooze returns to its ruled slot.

Two origins, two homes, one rule — THE RULING LIVES WITH THE THING IT DESCRIBES:

  * ``origin == "task"``          → ``slot:`` on the task record's frontmatter.
  * ``origin == "routine_item"``  → ``slot:`` on the matching ``items[]`` entry
    of the routine record (the per-item field ``routine.aggregator`` already
    emits and ``compute._collect_routine_today`` already reads).

**A free-text T3 entry is NOT writable here, and it is excluded upstream rather
than refused here.** It has no backing record at all (``_curated_t3_to_tier_entry``
builds it with ``routine_record=None`` and ``path="routine/"``), so there is
nowhere durable to put the answer. Dealing it a card whose verbs always fail
would be a lying affordance — the same disease as the DONE-only row this lane
exists to fix. ``tier.sort_rotation`` drops that shape from the population and
counts it in an ILB line instead; this module still refuses it (``thin_evidence``)
because a writer that trusts its caller's filter is one refactor from writing
nothing at all.

Contract — the same law as the sibling board writers (``routine/completion.py``,
``tier/task_completion.py``, ``tier/tier_confirm.py``):

  * **Exact-path addressed.** The caller passes the producer's stamped
    ``evidence.path`` / ``(routine_record, item_text)``. NO fuzzy resolution.
  * **Idempotent.** The field already carrying the requested slot →
    ``idempotent_noop``, no write.
  * **Fail-loud, never guess.** A slot outside the canonical set →
    ``invalid_slot``; a missing identity → ``thin_evidence``; a record that
    isn't there → ``unknown_record``; an item text not verbatim on the record →
    ``unknown_item``.
  * **Contained.** Every resolved path is re-verified inside the vault before
    ``os.replace``, with ``vault_path`` a REQUIRED keyword so the guard cannot
    be silently skipped by a caller that forgets it.
  * **Locked + atomic.** ``file_rmw_lock`` around load → check → write, and
    ``.tmp`` → ``os.replace``.
  * **One named log per branch** (intentionally-left-blank), carrying the
    REASON — a refusal that only says "no" is indistinguishable from a guard
    that never fired.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import frontmatter  # type: ignore[import-untyped]
import structlog
import yaml

from alfred.common.file_lock import file_rmw_lock
from alfred.tier import slots
from alfred.vault.paths import VaultContainmentError, resolve_in_vault

log = structlog.get_logger(__name__)

#: The frontmatter field this writer owns — on a task record, and on a routine
#: record's per-item dict. ONE spelling, because ``tier.compute`` reads it in
#: three places (``fm["slot"]`` for tasks at :649 and :1376, ``it.get("slot")``
#: for routine items at :2240) and a second name here would be a silent no-op.
SLOT_FIELD = "slot"

# Result kinds. String values deliberately mirror the sibling writers where they
# overlap (``success`` / ``idempotent_noop`` / ``unknown_record`` /
# ``unknown_item`` / ``thin_evidence``) so a caller maps them without a
# translation table.
SORT_KIND_SUCCESS = "success"
SORT_KIND_IDEMPOTENT_NOOP = "idempotent_noop"
SORT_KIND_UNKNOWN_RECORD = "unknown_record"
SORT_KIND_UNKNOWN_ITEM = "unknown_item"
SORT_KIND_INVALID_SLOT = "invalid_slot"
SORT_KIND_THIN_EVIDENCE = "thin_evidence"


@dataclass(frozen=True)
class SlotAssignmentResult:
    """One assignment attempt: what happened, to what, and whether it wrote."""

    kind: str
    slot: str
    origin: str
    target: str
    changed: bool = False

    @property
    def ok(self) -> bool:
        """Applied or already-applied. ``idempotent_noop`` is a SUCCESS end-state
        — the operator's intent is recorded either way, and re-tapping a sort
        must not present as an error."""
        return self.kind in (SORT_KIND_SUCCESS, SORT_KIND_IDEMPOTENT_NOOP)


def _write_record(record_path: Path, fm: dict, content: str) -> None:
    """Serialise ``fm`` (operator key order preserved) + ``content`` atomically.

    Byte-for-byte the same round-trip as ``routine.completion._write_record`` —
    ``yaml.dump(sort_keys=False)``, ``.tmp`` → ``os.replace``. Copied rather
    than imported to keep ``tier`` → ``routine`` a call-time dependency like
    every other edge in this package; the shape is pinned by a test that drives
    both writers over the same record.
    """
    fm_yaml = yaml.dump(
        fm, default_flow_style=False, allow_unicode=True, sort_keys=False,
    )
    out = f"---\n{fm_yaml}---\n\n{content}\n"
    tmp = record_path.with_name(record_path.name + ".tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, record_path)


def _contained(record_path: Path, vault_path: Path, *, writer: str) -> Path | None:
    """Re-verify an already-composed ``record_path`` sits inside the vault.

    Arc #18 defence-in-depth, same as ``routine.completion._contained``. The
    board caller gates its own composition, so in a correct build this never
    fires; it exists so a future caller that forgets cannot escape, because this
    is the last thing before ``os.replace``.
    """
    try:
        return resolve_in_vault(vault_path, record_path, writer=writer)
    except VaultContainmentError:
        return None


def assign_slot(
    vault_path: Path,
    *,
    origin: str,
    slot: str,
    path: str = "",
    routine_record: str | None = None,
    item_text: str | None = None,
) -> SlotAssignmentResult:
    """Record the operator's slot ruling on the item's backing record.

    ``origin`` / ``path`` / ``routine_record`` / ``item_text`` come from the feed
    item's OWN stamped evidence (the trusted-producer class, same as the sibling
    board writers read). ``slot`` is the operator's tap, and it is the one
    caller-composed value here — so it goes through
    :func:`~alfred.tier.slots.normalize_slot`, which accepts the operator's
    "routine" vocabulary alias and refuses everything else rather than minting a
    fourth category.

    Returns a :class:`SlotAssignmentResult`; never raises for an addressable
    failure.
    """
    canonical = slots.normalize_slot(slot)
    if canonical is None:
        # NOT a fall-through to a lower-precedence rule (which is what
        # ``normalize_slot`` means on a hand-edited record). Here the value is a
        # TAP, and an unrecognised tap is a broken client or a renamed verb, so
        # it fails loud instead of quietly writing nothing.
        log.warning(
            "tier.sort.invalid_slot",
            slot=str(slot)[:64], origin=origin,
            canonical=list(slots.CANONICAL_SLOTS),
            reason="tapped slot is not a canonical slot — refusing to write",
        )
        return SlotAssignmentResult(SORT_KIND_INVALID_SLOT, str(slot)[:64], origin, "")

    if origin == "task":
        return _assign_task_slot(vault_path, path=path, slot=canonical)
    if origin == "routine_item":
        return _assign_routine_item_slot(
            vault_path, routine_record=routine_record,
            item_text=item_text, slot=canonical,
        )
    log.info(
        "tier.sort.thin_evidence",
        origin=origin, slot=canonical,
        reason="origin is neither task nor routine_item — no record to write to",
    )
    return SlotAssignmentResult(SORT_KIND_THIN_EVIDENCE, canonical, origin, "")


def _assign_task_slot(
    vault_path: Path, *, path: str, slot: str,
) -> SlotAssignmentResult:
    """Write ``slot:`` onto a task record's frontmatter."""
    rel = (path or "").strip()
    if not rel:
        log.info(
            "tier.sort.thin_evidence",
            origin="task", slot=slot,
            reason="task entry carries no path — nothing addressable to write to",
        )
        return SlotAssignmentResult(SORT_KIND_THIN_EVIDENCE, slot, "task", "")

    contained = _contained(vault_path / rel, Path(vault_path), writer="tier.sort.task")
    if contained is None:
        log.warning(
            "tier.sort.path_escape_denied",
            origin="task", path=rel, slot=slot,
            reason="resolved task path is outside the vault — refusing to write",
        )
        return SlotAssignmentResult(SORT_KIND_UNKNOWN_RECORD, slot, "task", rel)

    if not contained.is_file():
        log.info(
            "tier.sort.unknown_record",
            origin="task", path=rel, slot=slot,
            reason="task record does not exist",
        )
        return SlotAssignmentResult(SORT_KIND_UNKNOWN_RECORD, slot, "task", rel)

    with file_rmw_lock(contained):
        try:
            post = frontmatter.load(str(contained))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            log.warning(
                "tier.sort.read_failed",
                origin="task", path=rel, slot=slot,
                error=str(exc), error_type=type(exc).__name__,
            )
            return SlotAssignmentResult(SORT_KIND_UNKNOWN_RECORD, slot, "task", rel)
        fm = dict(post.metadata or {})

        # Compare on the NORMALISED existing value, not the raw string: a record
        # already carrying ``slot: Routine`` is already Rhythm, and rewriting it
        # to ``rhythm`` would be a no-op change presented as a fresh decision.
        if slots.normalize_slot(fm.get(SLOT_FIELD)) == slot:
            log.info(
                "tier.sort.idempotent_noop",
                origin="task", path=rel, slot=slot,
                reason="record already carries this slot",
            )
            return SlotAssignmentResult(
                SORT_KIND_IDEMPOTENT_NOOP, slot, "task", rel, changed=False,
            )

        fm[SLOT_FIELD] = slot
        _write_record(contained, fm, post.content or "")

    log.info("tier.sort.success", origin="task", path=rel, slot=slot)
    return SlotAssignmentResult(SORT_KIND_SUCCESS, slot, "task", rel, changed=True)


def _assign_routine_item_slot(
    vault_path: Path, *, routine_record: str | None, item_text: str | None,
    slot: str,
) -> SlotAssignmentResult:
    """Write ``slot:`` onto the matching ``items[]`` entry of a routine record."""
    record = (routine_record or "").strip()
    text = (item_text or "").strip()
    if not record or not text:
        log.info(
            "tier.sort.thin_evidence",
            origin="routine_item", record=record, item=text, slot=slot,
            reason="routine entry needs BOTH a record and an item text — a "
                   "free-text T3 intention has no backing record to write to",
        )
        return SlotAssignmentResult(SORT_KIND_THIN_EVIDENCE, slot, "routine_item", record)

    rel = f"routine/{record}.md"
    contained = _contained(vault_path / rel, Path(vault_path), writer="tier.sort.routine")
    if contained is None:
        log.warning(
            "tier.sort.path_escape_denied",
            origin="routine_item", path=rel, slot=slot,
            reason="resolved routine path is outside the vault — refusing to write",
        )
        return SlotAssignmentResult(SORT_KIND_UNKNOWN_RECORD, slot, "routine_item", rel)

    if not contained.is_file():
        log.info(
            "tier.sort.unknown_record",
            origin="routine_item", path=rel, slot=slot,
            reason="routine record does not exist",
        )
        return SlotAssignmentResult(SORT_KIND_UNKNOWN_RECORD, slot, "routine_item", rel)

    with file_rmw_lock(contained):
        try:
            post = frontmatter.load(str(contained))
        except (OSError, ValueError, yaml.YAMLError) as exc:
            log.warning(
                "tier.sort.read_failed",
                origin="routine_item", path=rel, slot=slot,
                error=str(exc), error_type=type(exc).__name__,
            )
            return SlotAssignmentResult(SORT_KIND_UNKNOWN_RECORD, slot, "routine_item", rel)
        fm = dict(post.metadata or {})

        raw_items = fm.get("items")
        target = None
        if isinstance(raw_items, list):
            for entry in raw_items:
                if isinstance(entry, dict) and str(entry.get("text") or "").strip() == text:
                    target = entry
                    break
        if target is None:
            # EXACT match only — the caller resolved the canonical text from the
            # producer's own evidence, so a miss is an honest dead end (item
            # renamed or removed since the card was dealt), not an invitation to
            # fuzzy-match a different item into the operator's ruling.
            log.info(
                "tier.sort.unknown_item",
                origin="routine_item", path=rel, item=text, slot=slot,
                reason="item text is not verbatim on the record's items",
            )
            return SlotAssignmentResult(SORT_KIND_UNKNOWN_ITEM, slot, "routine_item", rel)

        if slots.normalize_slot(target.get(SLOT_FIELD)) == slot:
            log.info(
                "tier.sort.idempotent_noop",
                origin="routine_item", path=rel, item=text, slot=slot,
                reason="item already carries this slot",
            )
            return SlotAssignmentResult(
                SORT_KIND_IDEMPOTENT_NOOP, slot, "routine_item", rel, changed=False,
            )

        target[SLOT_FIELD] = slot
        _write_record(contained, fm, post.content or "")

    log.info("tier.sort.success", origin="routine_item", path=rel, item=text, slot=slot)
    return SlotAssignmentResult(SORT_KIND_SUCCESS, slot, "routine_item", rel, changed=True)


__all__ = [
    "SLOT_FIELD",
    "SORT_KIND_IDEMPOTENT_NOOP",
    "SORT_KIND_INVALID_SLOT",
    "SORT_KIND_SUCCESS",
    "SORT_KIND_THIN_EVIDENCE",
    "SORT_KIND_UNKNOWN_ITEM",
    "SORT_KIND_UNKNOWN_RECORD",
    "SlotAssignmentResult",
    "assign_slot",
]
