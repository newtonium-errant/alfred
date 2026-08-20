"""The sort proposer — which slot a rotation card SUGGESTS, and how it learns.

The rotation (:mod:`alfred.tier.sort_rotation`) deals exactly the items
``tier.slots.classify_slot`` REFUSED (rule 7). By construction that population
carries none of the classifier's signals: no ``explicit_slot``, no learned
override, no ``self_care``, no ``due_pattern``, no ``target_cadence_days``, and
no task-origin due date. So this module is NOT a second classifier and can never
overrule the first — an entry it sees is an entry the classifier already
declined to answer. It reads the RESIDUAL signals the refusal left behind and
makes a legible guess for the operator to confirm or correct with one gesture
(the 2026-08-19 gesture-grammar ruling: "affirm proposed slot, with swipe-hold
to engage the option selector"). The classifier answers; the proposer asks.

## The rule table (first match wins; total by construction)

===  =====================================  ========  ==========================
 #   Rule                                   Slot      Basis
===  =====================================  ========  ==========================
 0   learned shape override                 learned   see "Self-correcting" below
 1   ``due_iso`` present                    duty      a visible deadline reads as
                                                      an obligation
 2   ``tier == 3``                          fuel      the T3 lane exists for
                                                      restoration
 3   ``origin == "routine_item"``           rhythm    it recurs — the shape of a
                                                      practice
 4   anything else                          duty      an undated task the operator
                                                      curated onto TODAY's list is
                                                      obligation-shaped
===  =====================================  ========  ==========================

Rule 1 is reachable only for dated NON-task entries (a dated task is already
Duty by classifier rule 6 and never reaches the rotation). It is kept at the
top anyway: the table must hold on its own terms rather than depend on upstream
hydration being complete, and a deadline is the strongest residual signal when
one survives. Rule 2 outranks rule 3 so an unslotted T3 routine item reads as
restoration, not practice — T3 is the self-care lane, and that placement is the
stronger statement about what the item does for the day.

## Self-correcting by design (the platform standard, all three parts)

**(1) Capture.** Every sort ruling the deck records passes through
:func:`record_ruling`: the card's stamped ``proposed_slot`` against the slot the
operator actually chose. A quick affirm is a CONFIRMATION (chosen == proposed);
a hold-and-choose-different is a CORRECTION (chosen != proposed); a reject/defer
records nothing (the operator declined to judge). One JSONL row per ruling.

**(2) Feed back.** :func:`propose_slot` consults the per-shape tally first. A
SHAPE is the tuple of exactly the discriminators the heuristic branches on —
``(origin, due-present, tier)`` — so the learned layer competes on the same
evidence the static table uses, never on features nobody can audit. When a
shape has at least :data:`MIN_RULINGS_FOR_OVERRIDE` rulings and a STRICT
majority of them chose one slot, that slot is proposed (rule ``learned_shape``);
otherwise the static table stands. Known bias, stated rather than hidden: a
quick affirm is cheaper than a correction, so confirmations lean toward
whatever was proposed — the threshold and the strict-majority bar are what keep
the override conservative.

**(3) Surface.** ``alfred tier-sort-learning`` renders the tally — per-shape
rulings, correction rate, and every ACTIVE override with the heuristic answer it
displaces. The rotation's own log line carries the correction rate each fire.

**The guardrail holds by construction:** the learned layer changes only what is
PRE-SELECTED on a card the operator must still affirm — every learned adjustment
surfaces as a proposal that crosses his thumb before anything is written. That
is learn → propose → operator-approves, not silent auto-mutation; there is no
path from this store to a vault write that skips the gesture.

## The store

JSONL beside the FEED STORE (:func:`corrections_path_for`), because both writers
already hold that path: the act router has the store it just consulted, the
brief producer has the store it is about to reconcile. Deriving the sidecar from
the store's own path keeps it instance-scoped by construction (the 2026-07-31
shared-cwd incident: a cwd-relative default is ONE file across instances), with
no new config key to half-thread. Append-only, schema-tolerant on read, and
belt-swallowed everywhere: a corrupt learning store must never break a sort, a
fire, or the brief.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from alfred.common.file_lock import file_rmw_lock
from alfred.tier import slots

log = structlog.get_logger(__name__)

# Rule identifiers, carried on the card (``evidence.proposed_rule``) and in the
# store — the "why" beside every guess, same discipline as ``slots.RULE_*``.
PROPOSE_RULE_LEARNED = "learned_shape"
PROPOSE_RULE_DUE = "due_date"
PROPOSE_RULE_T3 = "t3_lane"
PROPOSE_RULE_RECURRING = "recurring"
PROPOSE_RULE_DEFAULT = "default_duty"

#: Rulings a shape needs before its majority may displace the heuristic. Three
#: matches the rotation's own cap — one full dealt day of consistent answers.
MIN_RULINGS_FOR_OVERRIDE = 3

#: The corrections sidecar's filename, beside the feed store.
CORRECTIONS_FILENAME = "sort_corrections.jsonl"


@dataclass(frozen=True)
class SlotProposal:
    """One proposal: the suggested slot and the rule that produced it."""

    slot: str
    rule: str


def shape_of(entry: Any) -> str:
    """The learning key for an entry — exactly the heuristic's discriminators.

    ``<origin>|due:<y/n>|t<tier>`` — nothing more, deliberately. A shape built
    from richer features (the name, the record) would let the learned layer
    drift onto evidence the static table cannot see, and the override would stop
    being auditable against the rule it displaces. Because the shape IS the
    heuristic's input tuple, :func:`heuristic_for_shape` can re-derive the
    displaced answer from the store alone — which is what the operator-facing
    readout renders.
    """
    origin = str(getattr(entry, "origin", "") or "") or "unknown"
    due = "y" if getattr(entry, "due_iso", None) else "n"
    tier = getattr(entry, "tier", None)
    return f"{origin}|due:{due}|t{tier if tier in (1, 2, 3) else '?'}"


def heuristic_proposal(entry: Any) -> SlotProposal:
    """The static rule table (rules 1–4). Total: every entry gets an answer."""
    if getattr(entry, "due_iso", None):
        return SlotProposal(slots.SLOT_DUTY, PROPOSE_RULE_DUE)
    if getattr(entry, "tier", None) == 3:
        return SlotProposal(slots.SLOT_FUEL, PROPOSE_RULE_T3)
    if str(getattr(entry, "origin", "") or "") == "routine_item":
        return SlotProposal(slots.SLOT_RHYTHM, PROPOSE_RULE_RECURRING)
    return SlotProposal(slots.SLOT_DUTY, PROPOSE_RULE_DEFAULT)


@dataclass(frozen=True)
class _ShapeEntry:
    """A synthetic entry rebuilt from a shape key — see :func:`heuristic_for_shape`."""

    origin: str
    due_iso: str | None
    tier: int | None


def heuristic_for_shape(shape: str) -> SlotProposal:
    """The static answer for a SHAPE — what an active override displaces.

    Possible only because the shape is the heuristic's own input tuple; a shape
    that does not parse falls to the default rule, which is also what the live
    table answers for an entry with none of the signals.
    """
    parts = shape.split("|")
    origin = parts[0] if parts else "unknown"
    due = "y" in parts[1] if len(parts) > 1 and parts[1].startswith("due:") else False
    tier: int | None = None
    if len(parts) > 2 and parts[2].startswith("t"):
        try:
            tier = int(parts[2][1:])
        except ValueError:
            tier = None
    return heuristic_proposal(
        _ShapeEntry(origin=origin, due_iso="due" if due else None, tier=tier)
    )


def learned_slot_for(counts: dict[str, int]) -> str | None:
    """The slot a shape's tally proposes, or ``None`` to leave the table alone.

    STRICT majority over at least :data:`MIN_RULINGS_FOR_OVERRIDE` rulings, and
    the winner must be a canonical slot. A tie proposes nothing: an override the
    evidence cannot pick unambiguously is the static table's to keep.
    """
    clean = {
        slot: int(n)
        for slot, n in counts.items()
        if slot in slots.CANONICAL_SLOTS and isinstance(n, int) and n > 0
    }
    total = sum(clean.values())
    if total < MIN_RULINGS_FOR_OVERRIDE:
        return None
    best_slot, best_n = max(clean.items(), key=lambda kv: kv[1])
    if best_n * 2 <= total:  # not a strict majority
        return None
    return best_slot


def propose_slot(entry: Any, tally: dict[str, dict[str, int]]) -> SlotProposal:
    """Rule 0 then the table: the proposal a dealt card carries.

    ``tally`` is :func:`load_tally`'s fold (pass ``{}`` for no learning). The
    learned answer only surfaces when it DIFFERS from the heuristic — an
    override that agrees with the table is the table, and reporting it as
    learned would overstate what the store contributed.
    """
    heuristic = heuristic_proposal(entry)
    learned = learned_slot_for(tally.get(shape_of(entry), {}))
    if learned is not None and learned != heuristic.slot:
        return SlotProposal(learned, PROPOSE_RULE_LEARNED)
    return heuristic


# --- the corrections store ----------------------------------------------------


def corrections_path_for(feed_store_path: str | Path) -> Path:
    """The sidecar beside the feed store — ONE derivation for both writers.

    The act router (write side) and the brief producer (read side) both derive
    from the same per-instance feed store path, so they cannot resolve to
    different files and the sidecar is instance-scoped for free.
    """
    return Path(feed_store_path).parent / CORRECTIONS_FILENAME


def record_ruling(
    path: str | Path,
    *,
    shape: str,
    proposed: str,
    chosen: str,
    proposed_rule: str = "",
    feed_item_id: str = "",
) -> None:
    """Append one ruling — the correction signal, captured at the act.

    ``confirmed`` is derived, not passed: chosen == proposed is the whole
    definition, and a second spelling of it would drift. Raises on I/O failure
    — the CALLER decides what a capture failure may cost (the act dispatcher
    belts it: a sort must land even when the learning store cannot).
    """
    from alfred.feed.model import _now_iso

    row = {
        "ts": _now_iso(),
        "shape": shape,
        "proposed": proposed,
        "chosen": chosen,
        "confirmed": chosen == proposed,
        "proposed_rule": proposed_rule,
        "id": feed_item_id,
    }
    p = Path(path)
    # Same pre-write mkdir the feed store's own append performs — the sidecar
    # must be creatable on an instance whose data dir the store hasn't made yet.
    p.parent.mkdir(parents=True, exist_ok=True)
    with file_rmw_lock(p):
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    log.info(
        "tier.sort.ruling_recorded",
        shape=shape,
        proposed=proposed,
        chosen=chosen,
        confirmed=chosen == proposed,
    )


def load_tally(path: str | Path | None) -> dict[str, dict[str, int]]:
    """Fold the store into ``{shape: {chosen_slot: count}}``.

    Degrades to ``{}`` on every failure — absent file, unreadable file, and
    per-line garbage are all skipped (schema-tolerance, the house ``load()``
    contract): a corrupt learning store must cost the operator nothing but the
    learning. The read failure is logged so a proposer that silently stopped
    learning is diagnosable (ILB).
    """
    if path is None:
        return {}
    p = Path(path)
    if not p.is_file():
        return {}
    tally: dict[str, dict[str, int]] = {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "tier.sort.corrections_unreadable",
            path=str(p), error=str(exc), error_type=type(exc).__name__,
            detail="proposals fall back to the static rule table this pass",
        )
        return {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        shape = str(row.get("shape") or "")
        chosen = str(row.get("chosen") or "")
        if not shape or chosen not in slots.CANONICAL_SLOTS:
            continue
        tally.setdefault(shape, {})[chosen] = tally.setdefault(shape, {}).get(chosen, 0) + 1
    return tally


def learning_summary(path: str | Path | None) -> dict[str, Any]:
    """The operator-facing readout — part (3) of the standard.

    Re-reads the raw rows (not only the tally) because the correction RATE needs
    confirmed/corrected per ruling, which the chosen-slot fold deliberately
    drops. Same degradation contract as :func:`load_tally`.
    """
    rows: list[dict[str, Any]] = []
    if path is not None:
        p = Path(path)
        if p.is_file():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("shape"):
                        rows.append(row)
            except OSError:
                rows = []
    total = len(rows)
    corrected = sum(1 for r in rows if r.get("confirmed") is False)
    tally = load_tally(path)
    overrides: list[dict[str, Any]] = []
    for shape, counts in sorted(tally.items()):
        learned = learned_slot_for(counts)
        displaced = heuristic_for_shape(shape)
        if learned is not None and learned != displaced.slot:
            overrides.append(
                {
                    "shape": shape,
                    "proposes": learned,
                    "displaces": displaced.slot,
                    "displaced_rule": displaced.rule,
                    "rulings": sum(counts.values()),
                }
            )
    return {
        "rulings": total,
        "corrected": corrected,
        "correction_rate": round(corrected / total, 3) if total else 0.0,
        "shapes": {shape: dict(counts) for shape, counts in sorted(tally.items())},
        "active_overrides": overrides,
    }


__all__ = [
    "CORRECTIONS_FILENAME",
    "MIN_RULINGS_FOR_OVERRIDE",
    "PROPOSE_RULE_DEFAULT",
    "PROPOSE_RULE_DUE",
    "PROPOSE_RULE_LEARNED",
    "PROPOSE_RULE_RECURRING",
    "PROPOSE_RULE_T3",
    "SlotProposal",
    "corrections_path_for",
    "heuristic_for_shape",
    "heuristic_proposal",
    "learned_slot_for",
    "learning_summary",
    "load_tally",
    "propose_slot",
    "record_ruling",
    "shape_of",
]
