"""Which claim lines need the operator — the six ratified classes.

Classification is a JUDGMENT, so this module is built to the platform's
self-correcting standard rather than as a static heuristic. The three
parts are all here, by design and not bolted on:

1. **Capture the correction signal** — :class:`Correction` rows, appended
   to a sidecar the operator's rulings accumulate in.
2. **Feed it back** — :func:`classify` consults corrections first, so a
   line the operator has ruled on classifies his way from then on.
3. **Surface the learning for approval** — :func:`propose_eob_mappings`
   turns repeated corrections into PROPOSED code->class mappings for the
   morning review. Nothing here ever writes a mapping on its own.

**Propose-only. Nothing auto-closes.** No path in this module marks a
line resolved, paid, or dismissed.

**Unknown EOB codes fail OPEN.** The code->class map is a DENYLIST in
effect: a code that is present but unmapped yields
:data:`CLASS_UNKNOWN_EOB` and the line surfaces. This is the direction the
health-status doctrine settled (``QUIET_HEALTH_STATUSES``) and the reason
is the same: an extra card costs a glance, a dropped one costs the thing
itself, and on a payment statement the thing is money.

**The default code map is EMPTY, deliberately.** The real provider's EOB
code list is not in this repo, and inventing plausible codes would be
worse than shipping none — a fabricated mapping classifies real money with
made-up authority, and reads as knowledge rather than as a guess. So every
coded line starts as :data:`CLASS_UNKNOWN_EOB`, the first bulk-review
report is where the operator maps the codes he actually sees, and
``reconcile.eob_classes`` in config is where the map lives once he has.
That is not a gap in the build; it is the first turn of the correction
loop the standard asks for.

**Two classes are arithmetic; four need the map.** Reversal and short-pay
are derivable from the numbers on the line. Duplicate-denial,
documentation-required, resubmission-required and identity-mismatch are
statements ABOUT the line that only its EOB code (or the operator) can
make — in particular identity-mismatch (the Slaney/Staney shape) is not
derivable statement-side at all, because detecting it needs a reference
for the correct spelling, which is the P2 invoice export. The class exists
in the vocabulary and is reachable through the map and through a
correction; it is not claimed to be auto-detected.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from .ledger import ClaimLine
from .money import FULL_PERCENT

log = structlog.get_logger(__name__)

# --- the ratified vocabulary -------------------------------------------------
CLASS_DUPLICATE_DENIAL = "duplicate_denial"
CLASS_DOCUMENTATION_REQUIRED = "documentation_required"
CLASS_RESUBMISSION_REQUIRED = "resubmission_required"
CLASS_IDENTITY_MISMATCH = "identity_mismatch"
CLASS_REVERSAL = "reversal"
CLASS_SHORT_PAY = "short_pay"
#: Not one of the six — the fail-open bucket for a code we cannot read.
CLASS_UNKNOWN_EOB = "unknown_eob"

#: The six ratified classes (2026-08-12 operator walkthrough, Q4).
ATTENTION_CLASSES: frozenset[str] = frozenset({
    CLASS_DUPLICATE_DENIAL,
    CLASS_DOCUMENTATION_REQUIRED,
    CLASS_RESUBMISSION_REQUIRED,
    CLASS_IDENTITY_MISMATCH,
    CLASS_REVERSAL,
    CLASS_SHORT_PAY,
})

#: Every class a line can carry, including the fail-open bucket. Membership
#: is what a correction is validated against.
ALL_CLASSES: frozenset[str] = ATTENTION_CLASSES | {CLASS_UNKNOWN_EOB}

#: Classes that mean "the provider refused this line". Short-pay is
#: suppressed when one is present: a denied line is not a line that was
#: underpaid, and reporting it as both would double-count the same event.
DENIAL_CLASSES: frozenset[str] = frozenset({
    CLASS_DUPLICATE_DENIAL,
    CLASS_DOCUMENTATION_REQUIRED,
    CLASS_RESUBMISSION_REQUIRED,
    CLASS_IDENTITY_MISMATCH,
})

#: Human-readable one-liners, for the report and any future card.
CLASS_LABELS: dict[str, str] = {
    CLASS_DUPLICATE_DENIAL: "Duplicate denial",
    CLASS_DOCUMENTATION_REQUIRED: "Documentation required",
    CLASS_RESUBMISSION_REQUIRED: "Resubmission required",
    CLASS_IDENTITY_MISMATCH: "Identity mismatch",
    CLASS_REVERSAL: "Reversal / clawback",
    CLASS_SHORT_PAY: "Short pay",
    CLASS_UNKNOWN_EOB: "Unrecognised EOB code",
}

#: Splits a multi-code EOB cell: "42, 91" / "42;91" / "42 91".
_EOB_SPLIT_RE = re.compile(r"[,;/|\s]+")

_ZERO = Decimal("0")


def normalise_eob(code: str) -> str:
    """Uppercase, trimmed. ``" e42 "`` and ``"E42"`` are the same code."""
    return (code or "").strip().upper()


def split_eob_codes(cell: str) -> list[str]:
    """Split an EOB cell into individual codes, preserving order.

    A cell holding several codes is common and each one is a separate
    statement about the line, so all of them are consulted — taking only
    the first would let a mapped code mask an unmapped one and quietly
    close the fail-open path.
    """
    if not (cell or "").strip():
        return []
    return [normalise_eob(part) for part in _EOB_SPLIT_RE.split(cell.strip()) if part.strip()]


@dataclass
class Correction:
    """One operator ruling about one line's classification."""

    line_key: str = ""
    classes: list[str] = field(default_factory=list)
    operator: str = ""
    at: str = ""
    note: str = ""
    #: The EOB code(s) the line carried when the ruling was made. This is
    #: what makes a correction GENERALISABLE — without it a ruling teaches
    #: only about the one line, and :func:`propose_eob_mappings` has
    #: nothing to aggregate.
    eob_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {f_.name: getattr(self, f_.name) for f_ in fields(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Correction":
        """Schema-tolerant load — the house contract."""
        known = {
            k: v for k, v in data.items() if k in cls.__dataclass_fields__
        }
        for list_field in ("classes", "eob_codes"):
            if list_field in known:
                value = known[list_field]
                if isinstance(value, str):
                    known[list_field] = [value]
                elif isinstance(value, list):
                    known[list_field] = [str(x) for x in value]
                else:
                    known[list_field] = []
        return cls(**known)


@dataclass
class Classification:
    """What a line was classified as, and why."""

    line_key: str = ""
    classes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    #: ``"operator"`` when a correction decided it, ``"derived"`` otherwise.
    #: The report shows this so a ruled line is visibly ruled.
    source: str = "derived"

    @property
    def needs_attention(self) -> bool:
        return bool(self.classes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_key": self.line_key,
            "classes": list(self.classes),
            "reasons": list(self.reasons),
            "source": self.source,
        }


def classify(
    line: ClaimLine,
    *,
    eob_map: dict[str, str] | None = None,
    corrections: dict[str, Correction] | None = None,
) -> Classification:
    """Classify one claim line. Returns an empty class list when it is clean.

    An empty ``classes`` means "paid as expected, nothing to look at" — a
    real result, not an absence, and the report states the count of these
    rather than omitting them.
    """
    eob_map = eob_map or {}
    corrections = corrections or {}

    key = line.key
    ruled = corrections.get(key)
    if ruled is not None:
        # The operator has ruled on this line. His ruling REPLACES the
        # derivation — including a ruling of "nothing here", which is how a
        # false positive gets retired. Feeding the ruling back is part 2 of
        # the self-correcting standard.
        return Classification(
            line_key=key,
            classes=sorted(set(ruled.classes)),
            reasons=[
                f"operator ruling by {ruled.operator or 'unknown'}"
                + (f": {ruled.note}" if ruled.note else "")
            ],
            source="operator",
        )

    classes: set[str] = set()
    reasons: list[str] = []

    # --- map-derived ---------------------------------------------------------
    codes = split_eob_codes(line.eob_code)
    unmapped: list[str] = []
    for code in codes:
        mapped = eob_map.get(code)
        if mapped and mapped in ALL_CLASSES:
            classes.add(mapped)
            reasons.append(f"EOB {code} maps to {CLASS_LABELS.get(mapped, mapped)}")
        elif mapped:
            # Configured to a class this build does not know. Fail open
            # rather than drop: a typo in config must not silence a line.
            unmapped.append(code)
            log.warning(
                "reconcile.attention.unknown_configured_class",
                code=code,
                configured=mapped,
                known=sorted(ALL_CLASSES),
                detail="reconcile.eob_classes maps this code to a class this "
                       "build does not define; the line falls through to "
                       "unknown_eob rather than being silently cleared",
            )
        else:
            unmapped.append(code)

    if unmapped:
        classes.add(CLASS_UNKNOWN_EOB)
        reasons.append(
            "EOB code(s) " + ", ".join(unmapped) + " are not mapped — "
            "surfaced because an unrecognised code fails OPEN"
        )

    # --- arithmetic-derived --------------------------------------------------
    paid = line.amount_paid
    billed = line.total_billed
    eligible = line.amt_eligible
    pct = line.pct_paid

    if paid is not None and paid < _ZERO:
        classes.add(CLASS_REVERSAL)
        reasons.append(f"amount paid is negative ({paid}) — a reversal/clawback")

    is_denied = bool(classes & DENIAL_CLASSES)
    is_reversal = CLASS_REVERSAL in classes

    if not is_denied and not is_reversal and billed is not None and billed > _ZERO:
        effective_paid = paid if paid is not None else _ZERO
        if effective_paid < billed:
            classes.add(CLASS_SHORT_PAY)
            detail = f"paid {effective_paid} against {billed} billed"
            if pct is not None and pct < FULL_PERCENT:
                detail += f" ({pct}% PD)"
            if eligible is not None and eligible < billed:
                detail += f"; eligible {eligible} below billed"
            reasons.append(detail)

    return Classification(
        line_key=key,
        classes=sorted(classes),
        reasons=reasons,
        source="derived",
    )


def classify_all(
    lines: list[ClaimLine],
    *,
    eob_map: dict[str, str] | None = None,
    corrections: dict[str, Correction] | None = None,
) -> list[Classification]:
    """Classify every line, in input order."""
    results = [
        classify(line, eob_map=eob_map, corrections=corrections)
        for line in lines
    ]
    flagged = sum(1 for r in results if r.needs_attention)
    log.info(
        "reconcile.attention.classified",
        lines=len(lines),
        flagged=flagged,
        clean=len(lines) - flagged,
        detail=(
            "every line classified clean — nothing needs attention on this "
            "ledger, which is a result rather than an empty run"
            if lines and not flagged else ""
        ),
    )
    return results


def class_counts(results: list[Classification]) -> dict[str, int]:
    """``{class: count}`` over every class present. Deterministic order."""
    counts: dict[str, int] = {}
    for r in results:
        for c in r.classes:
            counts[c] = counts.get(c, 0) + 1
    return dict(sorted(counts.items()))


# --- corrections store -------------------------------------------------------


def load_corrections(path: Path | str) -> dict[str, Correction]:
    """Read the corrections sidecar into ``{line_key: latest Correction}``.

    LATEST wins: the file is append-only, so a second ruling on the same
    line is the operator changing his mind and must supersede the first.
    A missing file is an empty ruling set, not an error.
    """
    p = Path(path)
    out: dict[str, Correction] = {}
    if not p.is_file():
        return out
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log.warning(
            "reconcile.corrections.unreadable",
            path=str(p),
            error_class=type(e).__name__,
        )
        return out
    skipped = 0
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:  # noqa: BLE001 — torn tail line
            skipped += 1
            continue
        if not isinstance(obj, dict) or not obj.get("line_key"):
            skipped += 1
            continue
        corr = Correction.from_dict(obj)
        out[corr.line_key] = corr
    if skipped:
        log.warning(
            "reconcile.corrections.rows_skipped",
            path=str(p),
            skipped=skipped,
            kept=len(out),
        )
    return out


def append_correction(path: Path | str, correction: Correction) -> None:
    """Append one ruling. Append-only: the history of rulings is evidence.

    Unknown class names are REFUSED rather than stored — a ruling naming a
    class nothing consumes would look recorded and do nothing, which is the
    quiet failure this whole module is arranged against.
    """
    bad = [c for c in correction.classes if c not in ALL_CLASSES]
    if bad:
        raise ValueError(
            f"unknown attention class(es) {bad!r} — known classes are "
            f"{sorted(ALL_CLASSES)}. Refused rather than stored: a ruling "
            f"naming a class nothing consumes would appear recorded and "
            f"have no effect."
        )
    if not correction.at:
        correction.at = datetime.now(timezone.utc).isoformat()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(correction.to_dict(), ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    log.info(
        "reconcile.corrections.appended",
        line_key=correction.line_key,
        classes=correction.classes,
        operator=correction.operator,
    )


@dataclass
class ProposedMapping:
    """A code->class mapping the corrections suggest. PROPOSED, never applied."""

    code: str = ""
    proposed_class: str = ""
    supporting: int = 0
    conflicting: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {f_.name: getattr(self, f_.name) for f_ in fields(self)}


def propose_eob_mappings(
    corrections: dict[str, Correction],
    *,
    eob_map: dict[str, str] | None = None,
    min_support: int = 2,
) -> list[ProposedMapping]:
    """Turn repeated rulings into proposed code mappings. Never applies them.

    Part 3 of the self-correcting standard: the accumulated learning is
    SURFACED for human-in-the-loop approval. The operator adds the row to
    ``reconcile.eob_classes`` himself, or does not — this function's whole
    output is a suggestion, and nothing downstream reads it as config.

    A code already in the map is skipped (it is not a learning any more). A
    code whose rulings disagree is still reported, with the disagreement
    counted, because "the operator has ruled this code three different
    ways" is exactly the thing worth seeing.
    """
    eob_map = eob_map or {}
    by_code: dict[str, dict[str, int]] = {}
    for corr in corrections.values():
        for code in corr.eob_codes:
            norm = normalise_eob(code)
            if not norm or norm in eob_map:
                continue
            bucket = by_code.setdefault(norm, {})
            for cls in corr.classes:
                if cls == CLASS_UNKNOWN_EOB:
                    # Ruling a line "still unknown" teaches nothing about
                    # what the code MEANS, so it is not evidence for a map.
                    continue
                bucket[cls] = bucket.get(cls, 0) + 1

    out: list[ProposedMapping] = []
    for code in sorted(by_code):
        tally = by_code[code]
        if not tally:
            continue
        best = max(tally.items(), key=lambda kv: (kv[1], kv[0]))
        support = best[1]
        conflicting = sum(v for k, v in tally.items() if k != best[0])
        if support < min_support:
            continue
        out.append(ProposedMapping(
            code=code,
            proposed_class=best[0],
            supporting=support,
            conflicting=conflicting,
        ))

    log.info(
        "reconcile.attention.proposals",
        proposals=len(out),
        corrections=len(corrections),
        min_support=min_support,
        detail=(
            "no code has enough consistent rulings to propose a mapping yet "
            "— the loop is running, it just has nothing to say"
            if not out else ""
        ),
    )
    return out


__all__ = [
    "ALL_CLASSES",
    "ATTENTION_CLASSES",
    "CLASS_DOCUMENTATION_REQUIRED",
    "CLASS_DUPLICATE_DENIAL",
    "CLASS_IDENTITY_MISMATCH",
    "CLASS_LABELS",
    "CLASS_RESUBMISSION_REQUIRED",
    "CLASS_REVERSAL",
    "CLASS_SHORT_PAY",
    "CLASS_UNKNOWN_EOB",
    "DENIAL_CLASSES",
    "Classification",
    "Correction",
    "ProposedMapping",
    "append_correction",
    "class_counts",
    "classify",
    "classify_all",
    "load_corrections",
    "normalise_eob",
    "propose_eob_mappings",
    "split_eob_codes",
]
