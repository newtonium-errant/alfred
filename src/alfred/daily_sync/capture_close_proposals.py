"""Propose-close proposals for capture-born tasks (#64).

The capture pipeline proactively files a ``task/`` when the operator says he
is going to do something — good, and he wants that initiative. What was
missing is the other half: nothing ever noticed when the thing got DONE. In
the motivating case the screenshots arrived, the plan was ingested, Salem
coached from it the next day, and the task sat ``todo`` until he found it by
hand.

NEVER A SILENT CLOSE. This module's only power is to raise ONE pending
proposal per task; the task closes when he confirms it and not before. That is
the house propose-then-approve shape (:mod:`.demotion_proposals`,
:mod:`.canonical_proposals_section`) and it is load-bearing here for a
specific reason: the machine's evidence is a FUZZY MATCH. A wrong auto-close
would silently delete work he still intended to do, and he would discover it
the same way he discovered the stale task — by accident, later.

## Why this mirrors the demotion queue rather than canonical-proposals

``canonical_proposals`` is the transport-layer, cross-instance queue for
canonical-record creation. A capture-born task close is neither cross-instance
nor canonical. The demotion queue is the right sibling: a durable local JSONL
queue with pending/accepted/rejected, a numbered Daily Sync item, and
``N confirm`` / ``N reject`` through the reply dispatcher — and it already
encodes the suppression rules this needs.

## The three suppressions, adapted

1. **ONE PER TASK.** A pending proposal for a task suppresses another for the
   same task, and an ACCEPTED one means the task is already closed. Two cards
   asking the same question are two chances to answer it differently.
2. **COOLDOWN AFTER A REJECTION, PER TASK.** One full ``window_days`` from the
   latest rejection FOR THAT TASK. Per-task rather than global because a
   rejection is a statement about one task's evidence; silencing every other
   task's proposal on the strength of it would be the machine over-reading a
   narrow answer. (This is the one place the shape deliberately differs from
   demotion's per-kind cooldown — demotion has one kind, this has many tasks.)
3. **A PER-RUN CARD BUDGET.** At most ``max_proposals`` cards in one pass,
   oldest task first. The deck is a scarce surface; a first run over a long
   backlog could otherwise raise dozens at once and teach him to skim the
   section — which spends the propose-then-approve channel the whole
   self-correcting standard runs on. Suppressed candidates are counted in the
   trigger log, not dropped silently.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: The ONE grep-able trigger event. Fires on EVERY evaluation, including the
#: quiet one where nothing is proposed: this section renders nothing on a
#: healthy day, so this line is what distinguishes "evaluated, nothing to
#: propose" from "the daily pass stopped running".
TRIGGER_EVENT = "daily_sync.capture_close.trigger"

STATE_PENDING = "pending"
STATE_ACCEPTED = "accepted"
STATE_REJECTED = "rejected"
_VALID_STATES = frozenset({STATE_PENDING, STATE_ACCEPTED, STATE_REJECTED})

#: Why the trigger did or did not raise a proposal for a candidate. A closed
#: vocabulary so a grep of TRIGGER_EVENT stays filterable and the reasons stay
#: distinguishable — "no proposal today" is several different facts.
REASON_PROPOSED = "proposed"
REASON_NO_CANDIDATES = "no_open_capture_tasks"
REASON_NO_EVIDENCE = "no_evidence_above_floor"
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_PENDING_EXISTS = "pending_exists"
REASON_ALREADY_ACCEPTED = "already_accepted"
REASON_COOLDOWN = "cooldown"
REASON_BUDGET_SPENT = "per_run_budget_spent"


@dataclass
class CaptureCloseProposal:
    """One proposal that a capture-born task be closed as fulfilled."""

    proposal_id: str
    ts: str
    state: str
    #: Vault-relative path of the open capture-born task.
    task_path: str
    #: The evidence, FROZEN at proposal time — stored rather than recomputed at
    #: render. The operator must be answering the question he was asked, and a
    #: match that moves between the proposal and the reply is a different
    #: question wearing the same item number.
    task_text: str = ""
    evidence_path: str = ""
    evidence_name: str = ""
    score: float = 0.0
    #: ``glossary`` or ``similarity`` — how the match was decided. Rendered so
    #: he can weigh a learned verdict differently from a raw string score.
    match_source: str = ""
    #: When he answered (ISO 8601). Empty while pending. The cooldown clock
    #: reads this, so it is the one field a resolve MUST set.
    resolved_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CaptureCloseProposal | None":
        """Schema-tolerant build; ``None`` when the row cannot be placed.

        House load-time contract — filter to known fields so a row from a newer
        build loads without its extra field. A row missing a NO-DEFAULT field
        raises on construction and is declined here rather than taking the
        reader down; the effective requirement is every field without a
        default, not only the ones checked by name below.
        """
        if not isinstance(data, dict):
            return None
        known = {f.name for f in fields(cls)}
        try:
            row = cls(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            return None
        if not isinstance(row.proposal_id, str) or not row.proposal_id.strip():
            return None
        if not isinstance(row.task_path, str) or not row.task_path.strip():
            return None
        if row.state not in _VALID_STATES:
            return None
        return row

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "ts": self.ts,
            "state": self.state,
            "task_path": self.task_path,
            "task_text": self.task_text,
            "evidence_path": self.evidence_path,
            "evidence_name": self.evidence_name,
            "score": self.score,
            "match_source": self.match_source,
            "resolved_at": self.resolved_at,
        }


def make_proposal_id(task_path: str, evidence_path: str, ts: str) -> str:
    """Stable, collision-resistant id for one (task, evidence, time) triple.

    Content-derived rather than random so a re-proposal after a rejection and
    cooldown is visibly a DIFFERENT question (different ts → different id),
    while two writers racing the same triple would produce the same id rather
    than two rows the operator sees twice.
    """
    digest = hashlib.sha256(
        f"{task_path}\x00{evidence_path}\x00{ts}".encode("utf-8")
    ).hexdigest()[:16]
    return f"cc-{digest}"


def _parse(ts: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iter_proposals(queue_path: str | Path) -> list[CaptureCloseProposal]:
    """Every parseable row, in file order.

    Unparseable rows are skipped LOUDLY. A half-corrupt queue degrades to
    "fewer proposals visible", never to a crash — this feeds a daily surface,
    and a section that cannot render because one line is malformed is a worse
    outcome than one that has forgotten a proposal.
    """
    path = Path(queue_path)
    if not path.exists():
        return []
    rows: list[CaptureCloseProposal] = []
    skipped = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            skipped += 1
            continue
        row = CaptureCloseProposal.from_dict(data)
        if row is None:
            skipped += 1
            continue
        rows.append(row)
    if skipped:
        log.warning(
            "daily_sync.capture_close.rows_skipped",
            path=str(path), skipped=skipped,
            detail="unparseable proposal rows ignored — a proposal the "
                   "operator was asked about may not render",
        )
    return rows


def list_pending(queue_path: str | Path) -> list[CaptureCloseProposal]:
    return [p for p in iter_proposals(queue_path) if p.state == STATE_PENDING]


def find_proposal(
    queue_path: str | Path, proposal_id: str,
) -> CaptureCloseProposal | None:
    for p in iter_proposals(queue_path):
        if p.proposal_id == proposal_id:
            return p
    return None


def append_proposal(
    queue_path: str | Path, proposal: CaptureCloseProposal,
) -> None:
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

    Whole-file rewrite through a temp file, order-preserving — same shape as
    the demotion and canonical queues, and for the same reason: the file is one
    row per question ever asked and the item numbering must stay stable.

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
    queue_path: str | Path, task_path: str, window_days: int,
) -> datetime | None:
    """When THIS task may next be proposed, or ``None`` if it may be now.

    One full ``window_days`` from the LATEST rejection for that task — latest
    rather than first, so a second rejection restarts the clock rather than
    being served by the first one's remaining time.

    A rejected row with no parseable ``resolved_at`` is ignored rather than
    treated as blocking forever: it is a row this build cannot place in time,
    and the safe direction is to let the question be re-asked (he can decline
    again) rather than silence it permanently on a bad timestamp.
    """
    latest: datetime | None = None
    for p in iter_proposals(queue_path):
        if p.task_path != task_path or p.state != STATE_REJECTED:
            continue
        when = _parse(p.resolved_at)
        if when is None:
            continue
        if latest is None or when > latest:
            latest = when
    return latest + timedelta(days=window_days) if latest else None


@dataclass(frozen=True)
class CloseCandidate:
    """One open capture-born task and its best fulfilment evidence."""

    task_path: str
    task_text: str
    evidence_path: str
    evidence_name: str
    score: float
    match_source: str


def maybe_propose_closes(
    queue_path: str | Path,
    candidates: list[CloseCandidate],
    *,
    threshold: float,
    window_days: int,
    max_proposals: int,
    now: datetime | None = None,
) -> list[CaptureCloseProposal]:
    """Raise up to ``max_proposals`` pending proposals; log why for the rest.

    Pure of the vault and of the matcher — candidates are built by the caller
    and passed in — so this stays the one place the POLICY lives and can be
    exercised without a fixture vault.

    Candidates are taken in the order given; the caller sorts oldest-task-first
    so a budget-limited run spends its cards on the stalest promises.
    """
    when = now or datetime.now(timezone.utc)
    existing = iter_proposals(queue_path)
    pending_tasks = {p.task_path for p in existing if p.state == STATE_PENDING}
    accepted_tasks = {p.task_path for p in existing if p.state == STATE_ACCEPTED}

    def _no(task_path: str, reason: str, **extra: Any) -> None:
        log.info(
            TRIGGER_EVENT, proposed=False, reason=reason,
            task_path=task_path, threshold=threshold,
            window_days=window_days, **extra,
        )

    if not candidates:
        # Intentionally-left-blank: the common healthy day. Without this line
        # "no open capture-born tasks" is indistinguishable from "the pass
        # never ran", which is the whole reason the trigger event exists.
        log.info(
            TRIGGER_EVENT, proposed=False, reason=REASON_NO_CANDIDATES,
            threshold=threshold, window_days=window_days, candidates=0,
        )
        return []

    raised: list[CaptureCloseProposal] = []
    budget_spent = 0
    for cand in candidates:
        if len(raised) >= max_proposals:
            budget_spent += 1
            continue
        if cand.task_path in accepted_tasks:
            _no(cand.task_path, REASON_ALREADY_ACCEPTED)
            continue
        if cand.task_path in pending_tasks:
            _no(cand.task_path, REASON_PENDING_EXISTS)
            continue
        if cand.score < threshold:
            _no(cand.task_path, REASON_BELOW_THRESHOLD, score=round(cand.score, 4))
            continue
        until = cooldown_until(queue_path, cand.task_path, window_days)
        if until is not None and when < until:
            _no(cand.task_path, REASON_COOLDOWN, cooldown_until=until.isoformat())
            continue

        ts = when.isoformat()
        proposal = CaptureCloseProposal(
            proposal_id=make_proposal_id(cand.task_path, cand.evidence_path, ts),
            ts=ts,
            state=STATE_PENDING,
            task_path=cand.task_path,
            task_text=cand.task_text,
            evidence_path=cand.evidence_path,
            evidence_name=cand.evidence_name,
            score=round(cand.score, 4),
            match_source=cand.match_source,
        )
        append_proposal(queue_path, proposal)
        raised.append(proposal)
        pending_tasks.add(cand.task_path)
        log.info(
            TRIGGER_EVENT, proposed=True, reason=REASON_PROPOSED,
            task_path=cand.task_path, evidence_path=cand.evidence_path,
            score=proposal.score, match_source=cand.match_source,
            threshold=threshold, window_days=window_days,
        )

    if budget_spent:
        # Named, never silent: a suppressed candidate is a promise still going
        # unnoticed, and the count is how the operator learns the budget is
        # too tight for his backlog.
        log.info(
            TRIGGER_EVENT, proposed=False, reason=REASON_BUDGET_SPENT,
            suppressed=budget_spent, max_proposals=max_proposals,
            raised=len(raised),
        )
    return raised
