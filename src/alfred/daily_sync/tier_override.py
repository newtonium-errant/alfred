"""Persisted per-KIND feed tier overrides — the half an approved demotion writes.

``KIND_DEFAULTS`` in :mod:`alfred.feed.model` answers "what tier is this kind"
and stays the CODE default. This file holds the operator's standing decision
layered over it: "attribution cards go back under needs-you until I say
otherwise". The two are deliberately different things in different places — an
approved proposal never edits the dict, because a code default that an operator
decision can rewrite has no defaults left.

## Why a persisted file rather than a flag on the items

The feed producer re-derives every open item's tier on every fire (reconcile
re-upserts them), so a tier written once into the store is flattened back to the
kind default by the next sync. The override therefore has to live somewhere the
producer READS each time, which is this file. The same property is what makes it
reversible: delete the entry and the next fire returns the kind to
``KIND_DEFAULTS`` with no migration and no second approval flow.

## Fail-open, deliberately

Every decline path below — missing file, corrupt JSON, an unrecognised mode or
attention, a row that is not an object — logs and yields NO override, so the
kind falls back to its code default. The alternative (refusing to build the feed
until the file parses) would let one bad byte in a tuning file take down the
morning surface. The direction is safe in this specific case and it is worth
saying why rather than assuming it: the override only ever moves attribution
cards from glance to needs-you, so losing it under-asks nothing — it returns the
operator to the tier he was already living with when he raised the proposal. A
LOST override costs a day of glance cards; a feed that will not build costs the
day.

The reverse is not true for a future override that DEMOTES something into
glance, and whoever adds one should re-derive this decision rather than inherit
it.

## Reversibility (v1)

``alfred tier-override {list | clear <kind>}`` — see :mod:`alfred.cli`. A CLI escape hatch
rather than a second propose-flow, per the decision recorded above
``feed_producer._attribution_tier``: a re-demotion proposal is speculative until
an operator actually asks to undo one, and this direction over-asks rather than
silently under-asking, so a stuck override is visible every morning rather than
invisible forever.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import structlog

from alfred.feed.model import (
    ATTENTION_FYI,
    ATTENTION_NEEDS_YOU,
    MODE_DECIDE,
    MODE_FYI,
)

log = structlog.get_logger(__name__)

# The ONE grep-able load event. A constant because the operator's grep is a
# consumer — a silent rename strands it.
LOAD_EVENT = "daily_sync.tier_override.loaded"

# What a stored override is allowed to say. Validated on load rather than
# trusted: this file is hand-editable by design (it IS the escape hatch), and an
# unrecognised attention value would otherwise reach the feed store and land in
# a card the PWA has no lane for.
VALID_MODES = frozenset({MODE_DECIDE, MODE_FYI})
VALID_ATTENTIONS = frozenset({ATTENTION_NEEDS_YOU, ATTENTION_FYI})


@dataclass
class TierOverride:
    """One kind's standing tier decision."""

    kind: str
    mode: str
    attention: str
    #: When the operator approved it (ISO 8601). Audit only — nothing reads it
    #: to decide anything, which is why a missing one does not decline the row.
    approved_at: str = ""
    #: Free text naming the evidence, echoed by the CLI so the operator can see
    #: WHY a tier is where it is without opening the corpus.
    reason: str = ""
    #: The demotion proposal this came from, when it came from one. Empty for a
    #: hand-written entry.
    proposal_id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TierOverride | None":
        """Schema-tolerant build, or ``None`` when the row cannot be trusted.

        The house load-time contract (filter to known fields) plus a VALUE gate
        the house contract does not cover: tolerance is about surviving a field
        this build has never heard of, not about accepting an attention value
        the feed has no lane for.
        """
        if not isinstance(data, dict):
            return None
        known = {f.name for f in fields(cls)}
        try:
            row = cls(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            # Missing one of the NO-DEFAULT fields (kind/mode/attention). The
            # known-field filter cannot conjure one, so construction raises;
            # declined here rather than propagated, because this file is
            # hand-editable and a half-written entry must cost the operator his
            # override, not his feed.
            return None
        if not isinstance(row.kind, str) or not row.kind.strip():
            return None
        if row.mode not in VALID_MODES or row.attention not in VALID_ATTENTIONS:
            return None
        return row

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "mode": self.mode,
            "attention": self.attention,
            "approved_at": self.approved_at,
            "reason": self.reason,
            "proposal_id": self.proposal_id,
        }


@dataclass
class TierOverrides:
    """Every stored override, plus what the reader had to decline."""

    by_kind: dict[str, TierOverride] = field(default_factory=dict)
    #: Rows the reader refused — unrecognised tier values, non-objects. Counted
    #: for the same reason the corpus reader counts its skips: a silently
    #: shrinking set of overrides and a genuinely empty one look identical, and
    #: only this number tells them apart.
    declined: int = 0
    #: True when the file exists but could not be read at all (corrupt JSON,
    #: unreadable). Distinct from "no overrides": one is a tuning file nobody
    #: has written yet, the other is one that needs a human.
    unreadable: bool = False

    def tier_for(self, kind: str) -> tuple[str, str] | None:
        """The ``(mode, attention)`` this kind is overridden to, or ``None``."""
        row = self.by_kind.get(kind)
        return (row.mode, row.attention) if row else None


def load_overrides(path: str | Path | None) -> TierOverrides:
    """Read the override file. Never raises; every decline is logged.

    ILB: logs on EVERY call including the empty steady state, which is the
    common case. An override that stops being applied and an override file that
    stopped being read produce the same feed, and this line is what separates
    them.
    """
    result = TierOverrides()
    if not path:
        # Not a failure: no path configured means no override layer at all.
        log.info(LOAD_EVENT, path="", count=0, declined=0, detail="no path configured")
        return result

    file_path = Path(path)
    if not file_path.exists():
        log.info(
            LOAD_EVENT, path=str(file_path), count=0, declined=0,
            detail="no overrides recorded — every kind is at its code default",
        )
        return result

    try:
        raw = json.loads(file_path.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        result.unreadable = True
        log.warning(
            "daily_sync.tier_override.unreadable",
            path=str(file_path), error=str(exc),
            error_type=exc.__class__.__name__,
            detail="falling back to code defaults for every kind",
        )
        return result

    rows = raw.get("overrides") if isinstance(raw, dict) else None
    if not isinstance(rows, dict):
        result.unreadable = True
        log.warning(
            "daily_sync.tier_override.unreadable",
            path=str(file_path), error="no 'overrides' object",
            detail="falling back to code defaults for every kind",
        )
        return result

    for kind, row in rows.items():
        parsed = TierOverride.from_dict(
            {**row, "kind": kind} if isinstance(row, dict) else {},
        )
        if parsed is None:
            result.declined += 1
            log.warning(
                "daily_sync.tier_override.row_declined",
                path=str(file_path), kind=str(kind)[:64],
                detail="unrecognised tier values — this kind stays at its code default",
            )
            continue
        result.by_kind[parsed.kind] = parsed

    log.info(
        LOAD_EVENT,
        path=str(file_path),
        count=len(result.by_kind),
        declined=result.declined,
        kinds=sorted(result.by_kind),
    )
    return result


def _write(path: Path, overrides: dict[str, TierOverride]) -> None:
    """Atomic whole-file write (``.tmp`` → rename), the house state pattern."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"overrides": {k: v.to_dict() for k, v in sorted(overrides.items())}}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def set_override(
    path: str | Path,
    override: TierOverride,
) -> None:
    """Store (or replace) one kind's override.

    Read-modify-write of the whole file. Safe here for the same reason the
    canonical-proposals queue rewrites in place: the file holds one row per
    feed kind, so it stays a handful of lines, and the two writers (an approved
    proposal in the reply dispatcher, the CLI escape hatch) are both operator-
    driven and never concurrent.
    """
    file_path = Path(path)
    existing = load_overrides(file_path).by_kind
    existing[override.kind] = override
    _write(file_path, existing)
    log.info(
        "daily_sync.tier_override.set",
        path=str(file_path), kind=override.kind,
        mode=override.mode, attention=override.attention,
        proposal_id=override.proposal_id,
    )


def clear_override(path: str | Path, kind: str) -> bool:
    """Drop one kind's override. ``True`` when there was one to drop.

    The escape hatch's whole mechanism. Returning False rather than raising on a
    kind that has no override lets the CLI say "there was nothing to clear",
    which is a different sentence from "cleared" and the operator needs to be
    able to tell them apart.
    """
    file_path = Path(path)
    existing = load_overrides(file_path).by_kind
    if kind not in existing:
        log.info(
            "daily_sync.tier_override.clear_noop",
            path=str(file_path), kind=kind,
            detail="no override stored for this kind — nothing to clear",
        )
        return False
    del existing[kind]
    _write(file_path, existing)
    log.info("daily_sync.tier_override.cleared", path=str(file_path), kind=kind)
    return True


__all__ = [
    "LOAD_EVENT",
    "VALID_ATTENTIONS",
    "VALID_MODES",
    "TierOverride",
    "TierOverrides",
    "clear_override",
    "load_overrides",
    "set_override",
]
