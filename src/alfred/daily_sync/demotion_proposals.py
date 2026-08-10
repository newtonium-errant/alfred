"""Demotion proposals — asking before returning attribution cards to review.

#63a demoted attribution confirmations to the glance tier and gave them a 24h
auto-confirm. #72's corpus reader can now say how often that auto-confirm let a
wrong inference through. This module is what turns that number into a QUESTION
rather than an action.

NEVER A SILENT FLIP. The tier the operator ruled on is not something the machine
gets to re-rule on because a counter crossed a line — the counter is evidence,
and evidence goes to him. So the trigger's only power is to raise ONE pending
proposal; the tier moves when he approves it and not before. This is the house
propose-then-approve shape (:mod:`.canonical_proposals_section`), used here for
the same reason it exists there: a durable queue, a numbered Daily Sync item,
``N confirm`` / ``N reject`` through the reply dispatcher.

## What counts as evidence

``AttributionQuality.demotion_contests`` and nothing else — contests against
entries the machine confirmed unreviewed. The reasoning is in
:mod:`.attribution_quality`'s docstring beside ``DEMOTION_COUNTING_VIA``; the
short version is that a contest review would NOT have prevented is not evidence
that review should come back.

## The three suppressions, and why each exists

1. ALREADY DEMOTED. An override is in force → nothing to propose. Without this
   the proposal re-raises every day after approval, because approving it does
   not remove the contests from the window.

2. ONE AT A TIME. A pending proposal suppresses another regardless of anything
   else. Recorded in ``attribution_quality`` as a corollary of the cooldown;
   the substance is that two cards asking the same question are two chances to
   answer it differently.

3. COOLDOWN AFTER A REJECTION. One full ``window_days`` from the rejection, per
   the decision recorded beside ``DEMOTION_COUNTING_VIA``. The window's own
   arithmetic forces it: a rejected proposal leaves its evidence sitting inside
   the trailing window, so any shorter cooldown re-asks off evidence the
   operator has just declined to act on. Waiting one full window guarantees the
   next proposal is built from contests that are entirely new since he said no.

The cost of getting that wrong is not a late demotion — it is that the operator
learns to dismiss the card without reading it, which spends the
propose-then-approve channel the whole self-correcting standard runs on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# The ONE grep-able trigger event. Fires on EVERY evaluation, including — and
# especially — the quiet one where nothing is proposed: this section renders
# nothing on a healthy day, so this line is the only thing that distinguishes
# "evaluated, no case to answer" from "the trigger stopped running".
TRIGGER_EVENT = "daily_sync.attribution.demotion_trigger"

STATE_PENDING = "pending"
STATE_ACCEPTED = "accepted"
STATE_REJECTED = "rejected"
_VALID_STATES = frozenset({STATE_PENDING, STATE_ACCEPTED, STATE_REJECTED})

# The kind a demotion proposal moves. One today; named rather than inlined
# because the queue, the section and the override all have to agree on it.
ATTRIBUTION_KIND = "attribution"

# Why the trigger did or did not raise a proposal. A closed vocabulary so the
# operator's grep of TRIGGER_EVENT can be filtered, and so the reasons stay
# distinguishable from each other in the log — "no proposal today" is four
# different facts and they need four different words.
REASON_PROPOSED = "proposed"
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_ALREADY_OVERRIDDEN = "already_overridden"
REASON_PENDING_EXISTS = "pending_exists"
REASON_COOLDOWN = "cooldown"


@dataclass
class DemotionProposal:
    """One proposal that a feed kind go back under review."""

    proposal_id: str
    ts: str
    state: str
    kind: str
    #: The evidence, frozen at proposal time. Stored rather than recomputed at
    #: render: the operator must be answering the question he was asked, and a
    #: number that moves between the proposal and the reply is a different
    #: question wearing the same item number.
    demotion_contests: int = 0
    window_days: int = 0
    threshold: int = 0
    #: When the operator answered (ISO 8601). Empty while pending. The cooldown
    #: clock reads this, so it is the one field a resolve MUST set.
    resolved_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DemotionProposal | None":
        """Schema-tolerant build; ``None`` when the row cannot be placed.

        House load-time contract — filter to known fields so a row from a newer
        build loads without its extra field rather than exploding, and one from
        an older build loads with defaults. A row with no id or no state cannot
        be acted on or counted, so it is declined rather than half-loaded.
        """
        if not isinstance(data, dict):
            return None
        known = {f.name for f in fields(cls)}
        try:
            row = cls(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            # A row missing one of the NO-DEFAULT fields. The filter above
            # handles unknown keys; it cannot conjure a missing required one, so
            # construction raises and the row is declined here rather than
            # taking the reader down. Same shape as the attribution corpus
            # reader's ``except TypeError: continue``, and the same trap its
            # comment names: the effective requirement is every field without a
            # default, not the ones checked by name below.
            return None
        if not isinstance(row.proposal_id, str) or not row.proposal_id.strip():
            return None
        if row.state not in _VALID_STATES:
            return None
        return row

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "ts": self.ts,
            "state": self.state,
            "kind": self.kind,
            "demotion_contests": self.demotion_contests,
            "window_days": self.window_days,
            "threshold": self.threshold,
            "resolved_at": self.resolved_at,
        }


def _parse(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iter_proposals(queue_path: str | Path) -> list[DemotionProposal]:
    """Every row in the queue, oldest first. Missing file → ``[]``.

    Unreadable rows are skipped rather than raised, the same posture the
    attribution corpus reader takes: a queue that dies on one corrupt line stops
    being read on the day something went wrong enough to write one.
    """
    path = Path(queue_path)
    if not path.exists():
        return []
    out: list[DemotionProposal] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning(
            "daily_sync.demotion.queue_unreadable",
            path=str(path), error=str(exc),
        )
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        parsed = DemotionProposal.from_dict(data)
        if parsed is not None:
            out.append(parsed)
    return out


def list_pending(queue_path: str | Path) -> list[DemotionProposal]:
    """Pending proposals, oldest first. At most one in practice."""
    return [p for p in iter_proposals(queue_path) if p.state == STATE_PENDING]


def find_proposal(
    queue_path: str | Path, proposal_id: str,
) -> DemotionProposal | None:
    if not proposal_id:
        return None
    for p in iter_proposals(queue_path):
        if p.proposal_id == proposal_id:
            return p
    return None


def append_proposal(queue_path: str | Path, proposal: DemotionProposal) -> None:
    """Append one row. Creates the parent directory when missing."""
    path = Path(queue_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(proposal.to_dict(), ensure_ascii=False) + "\n")


def resolve_proposal(
    queue_path: str | Path,
    proposal_id: str,
    new_state: str,
    *,
    resolved_at: str,
) -> bool:
    """Flip one proposal to accepted/rejected in place. ``True`` on success.

    Whole-file rewrite through a temp file, order-preserving — the same shape as
    the canonical-proposals queue and for the same reasons (the file is one row
    per question ever asked, and the item numbering must stay stable).

    Disk errors PROPAGATE rather than being swallowed. A lost state transition
    would re-surface an already-answered proposal, and re-asking a question the
    operator just answered is the exact failure the cooldown exists to prevent.
    """
    if new_state not in _VALID_STATES:
        return False
    path = Path(queue_path)
    if not path.exists():
        return False
    rows = iter_proposals(path)
    found = False
    for row in rows:
        if row.proposal_id == proposal_id:
            row.state = new_state
            row.resolved_at = resolved_at
            found = True
    if not found:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")
    tmp.replace(path)
    return True


def cooldown_until(
    queue_path: str | Path, kind: str, window_days: int,
) -> datetime | None:
    """When this kind may next be proposed, or ``None`` if it may be now.

    One full ``window_days`` from the LATEST rejection — latest rather than
    first, so a second rejection restarts the clock rather than being served by
    the first one's remaining time.

    A rejected row with no parseable ``resolved_at`` is ignored rather than
    treated as blocking forever. It is a row this build cannot place in time,
    and the safe direction here is to let the question be re-asked (the operator
    can decline again) rather than to silence it permanently on a bad timestamp.
    """
    latest: datetime | None = None
    for p in iter_proposals(queue_path):
        if p.kind != kind or p.state != STATE_REJECTED:
            continue
        when = _parse(p.resolved_at)
        if when is None:
            continue
        if latest is None or when > latest:
            latest = when
    return latest + timedelta(days=window_days) if latest else None


def maybe_propose_demotion(
    queue_path: str | Path,
    *,
    kind: str = ATTRIBUTION_KIND,
    demotion_contests: int,
    window_days: int,
    threshold: int,
    override_in_force: bool,
    now: datetime | None = None,
) -> DemotionProposal | None:
    """Raise ONE pending proposal, or explain in the log why not.

    Returns the proposal it appended, or ``None``. Pure of the corpus and of the
    override file — both are read by the caller and passed in — so this stays
    the one place the POLICY lives and can be exercised without a fixture vault.
    """
    when = now or datetime.now(timezone.utc)

    def _no(reason: str, **extra: Any) -> None:
        log.info(
            TRIGGER_EVENT, kind=kind, proposed=False, reason=reason,
            demotion_contests=demotion_contests, window_days=window_days,
            threshold=threshold, **extra,
        )

    if override_in_force:
        _no(REASON_ALREADY_OVERRIDDEN,
            detail="this kind is already under an approved override")
        return None

    pending = [p for p in list_pending(queue_path) if p.kind == kind]
    if pending:
        _no(REASON_PENDING_EXISTS, proposal_id=pending[0].proposal_id,
            detail="a proposal is already waiting on an answer")
        return None

    until = cooldown_until(queue_path, kind, window_days)
    if until is not None and when < until:
        _no(REASON_COOLDOWN, cooldown_until=until.isoformat(),
            detail="one full window from the rejection, so the next proposal "
                   "is built from entirely new contests")
        return None

    if demotion_contests < threshold:
        _no(REASON_BELOW_THRESHOLD,
            detail="evaluated, nothing to propose")
        return None

    proposal = DemotionProposal(
        proposal_id=f"{kind}-demotion-{when.strftime('%Y%m%d%H%M%S')}",
        ts=when.isoformat(),
        state=STATE_PENDING,
        kind=kind,
        demotion_contests=demotion_contests,
        window_days=window_days,
        threshold=threshold,
    )
    append_proposal(queue_path, proposal)
    log.info(
        TRIGGER_EVENT, kind=kind, proposed=True, reason=REASON_PROPOSED,
        proposal_id=proposal.proposal_id,
        demotion_contests=demotion_contests, window_days=window_days,
        threshold=threshold,
    )
    return proposal


__all__ = [
    "ATTRIBUTION_KIND",
    "REASON_ALREADY_OVERRIDDEN",
    "REASON_BELOW_THRESHOLD",
    "REASON_COOLDOWN",
    "REASON_PENDING_EXISTS",
    "REASON_PROPOSED",
    "STATE_ACCEPTED",
    "STATE_PENDING",
    "STATE_REJECTED",
    "TRIGGER_EVENT",
    "DemotionProposal",
    "append_proposal",
    "cooldown_until",
    "find_proposal",
    "iter_proposals",
    "list_pending",
    "maybe_propose_demotion",
    "resolve_proposal",
]
