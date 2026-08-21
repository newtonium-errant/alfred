"""Calibration proposal store — the PENDING queue between capture and apply (R4 doors).

THE POINT OF THIS MODULE IS THE THUMB. ``calibration.apply_proposals`` writes
Alfred's running model of the operator into his own person record; the
self-correcting standard's guardrail is *learn → propose → operator-approves*,
never silent unsupervised mutation. This module is the "propose" half made
durable: capture appends here and stops, and the ONLY function in the tree that
calls ``apply_proposals`` is :func:`approve_proposal` below, which refuses
without a named operator.

WHY NOT REUSE THE ATTRIBUTION AUDIT FLOW (the obvious shortcut, deliberately
refused). ``apply_proposals`` already stamps ``attribution_audit`` entries that
the Daily Sync attribution section surfaces with confirm/reject. But that flow
carries ``AUTO_CONFIRM_AFTER_HOURS = 24``
(:mod:`alfred.daily_sync.attribution_section`) — an entry nobody touches is
confirmed BY TIMEOUT. That is opt-OUT, and routing calibration through it would
mean the operator's voice gets rewritten by a clock whenever he ignores a
morning card for a day. It also applies FIRST and asks second, which inverts the
arrow the standard names. So calibration proposals sit here, unapplied, until a
thumb crosses them; an ignored proposal stays pending forever, which is the
correct failure direction for this subsystem.

STORE SHAPE mirrors :mod:`alfred.tier.promote` (pending JSONL + decided JSONL,
schema-tolerant rows, degrade-not-crash on a corrupt line) because that is the
house pattern for an operator-decided proposal queue and it already survived a
review. ``decided_ids`` is the re-proposal exclusion: a rejected proposal never
comes back, and an approved one is not re-applied on the next capture.

IDEMPOTENCE is keyed on the CONTENT, not on the session: ``proposal_id`` hashes
(subsection, bullet), so the same observation drafted twice across two sessions
collapses to one pending row rather than stacking duplicates the operator has to
reject one at a time.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import get_logger

log = get_logger(__name__)


# Decision kinds. Same two words the recurrence store uses — an operator
# decision is an approve or a reject, and a third state here would be a
# proposal that is neither pending nor decided.
DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"


_WS_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    """Fold whitespace + case for identity purposes only (never for display)."""
    return _WS_RE.sub(" ", (text or "").strip()).lower()


def proposal_id(subsection: str, bullet: str) -> str:
    """Deterministic id for a proposal, keyed on its CONTENT.

    Keyed on (subsection, bullet) rather than on the source session so the same
    observation drafted in two different sessions collapses to one pending row.
    The alternative — a session-keyed id — would make every re-observation a new
    proposal, and the operator would reject the same sentence repeatedly with the
    decided-set never able to suppress it.
    """
    key = f"{_normalise(subsection)}\x00{_normalise(bullet)}"
    return "cal-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


@dataclass
class PendingCalibrationProposal:
    """One PENDING calibration proposal — drafted by the analyzer, awaiting a thumb.

    Written ONLY by capture (:func:`record_proposals`); never by an apply path.
    """

    proposal_id: str
    subsection: str
    bullet: str
    confidence: float = 0.7
    source_session_rel: str = ""
    detected_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingCalibrationProposal":
        """Schema-tolerant construct — the house load contract (extras dropped,
        absents defaulted) so a row written by another version still loads."""
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class CalibrationDecision:
    """One operator decision on a proposal. Written ONLY by approve/reject."""

    proposal_id: str
    decision: str
    operator: str = ""
    decided_at: str = ""
    # What the approval actually wrote, for the audit trail: "" on a reject and
    # on an approve whose vault write was refused.
    applied_to: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CalibrationDecision":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _append_jsonl(path: str | Path, row: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _load_jsonl(path: str | Path, build, *, kind: str) -> list:
    """Load rows via ``build(dict)``; a malformed row is SKIPPED with a warning.

    The morning surface must degrade on a partially-corrupt store rather than
    crash — one bad line must not cost the operator the whole review.
    """
    p = Path(path)
    if not p.exists():
        return []
    out: list = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError("row is not a JSON object")
            out.append(build(data))
        except (ValueError, TypeError) as exc:
            log.warning(
                "talker.calibration.skip_bad_row",
                path=str(p), kind=kind, error=str(exc),
            )
    return out


def append_pending(path: str | Path, proposal: PendingCalibrationProposal) -> None:
    _append_jsonl(path, asdict(proposal))


def load_pending(path: str | Path) -> list[PendingCalibrationProposal]:
    return _load_jsonl(path, PendingCalibrationProposal.from_dict, kind="pending")


def append_decision(path: str | Path, decision: CalibrationDecision) -> None:
    """Append one operator decision. Called ONLY by approve/reject below."""
    _append_jsonl(path, asdict(decision))


def load_decisions(path: str | Path) -> list[CalibrationDecision]:
    return _load_jsonl(path, CalibrationDecision.from_dict, kind="decided")


def decided_ids(path: str | Path) -> set[str]:
    """Proposal ids carrying a durable decision — the re-proposal exclusion set."""
    return {d.proposal_id for d in load_decisions(path) if d.proposal_id}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_proposals(
    pending_path: str | Path,
    decided_path: str | Path,
) -> list[PendingCalibrationProposal]:
    """The review set: pending rows minus decided ones, DEDUPED by proposal_id.

    Dedupe keeps the FIRST occurrence, so ``detected_at`` reads as when the
    observation was first made rather than when it was last re-drafted.
    """
    decided = decided_ids(decided_path)
    seen: set[str] = set()
    out: list[PendingCalibrationProposal] = []
    for p in load_pending(pending_path):
        if not p.proposal_id or p.proposal_id in decided or p.proposal_id in seen:
            continue
        seen.add(p.proposal_id)
        out.append(p)
    return out


def record_proposals(
    pending_path: str | Path,
    decided_path: str | Path,
    proposals: list[Any],
    *,
    source_session_rel: str = "",
) -> list[PendingCalibrationProposal]:
    """CAPTURE door — persist analyzer drafts as pending rows. Writes NO vault.

    Takes :class:`alfred.telegram.calibration.Proposal` objects (duck-typed:
    anything carrying ``subsection`` / ``bullet`` / ``confidence`` /
    ``source_session_rel``) and appends the ones that are genuinely new.

    Returns the rows actually appended — empty when every draft was already
    pending or already decided, which is the common steady state and is logged
    explicitly rather than passing as silence.
    """
    already = {p.proposal_id for p in load_pending(pending_path)}
    decided = decided_ids(decided_path)
    appended: list[PendingCalibrationProposal] = []
    skipped_dupe = 0
    skipped_decided = 0

    for draft in proposals or []:
        bullet = str(getattr(draft, "bullet", "") or "").strip()
        if not bullet:
            continue
        subsection = str(getattr(draft, "subsection", "") or "")
        pid = proposal_id(subsection, bullet)
        if pid in decided:
            skipped_decided += 1
            continue
        if pid in already:
            skipped_dupe += 1
            continue
        try:
            confidence = float(getattr(draft, "confidence", 0.7))
        except (TypeError, ValueError):
            confidence = 0.7
        row = PendingCalibrationProposal(
            proposal_id=pid,
            subsection=subsection,
            bullet=bullet,
            confidence=max(0.0, min(1.0, confidence)),
            source_session_rel=(
                str(getattr(draft, "source_session_rel", "") or "")
                or source_session_rel
            ),
            detected_at=_now_iso(),
        )
        append_pending(pending_path, row)
        already.add(pid)
        appended.append(row)

    # ILB: a capture that drafted nothing new is the STEADY STATE, and it must
    # be distinguishable from a capture that never ran at all.
    log.info(
        "talker.calibration.capture_recorded",
        appended=len(appended),
        skipped_duplicate=skipped_dupe,
        skipped_already_decided=skipped_decided,
        drafted=len(proposals or []),
        source_session_rel=source_session_rel,
        pending_path=str(pending_path),
    )
    return appended


def approve_proposal(
    vault_path: Path,
    config: Any,
    proposal_id_: str,
    *,
    operator: str,
    user_rel_path: str,
    agent_slug: str = "salem",
) -> dict[str, Any]:
    """Operator APPROVE — THE ONLY path in the tree that writes the calibration block.

    Everything protective about this feature lives in this function's guard
    stack, so read it as the whole gate rather than as validation noise:

      * **A named operator is REQUIRED.** Blank refuses before any write. The
        guard is here at the core rather than only at the CLI boundary, so every
        future door (a feed-card dispatcher, a reply path) inherits it for free
        instead of re-implementing it — the same reason
        ``tier.promote.approve_proposal`` puts its no-junk-target refusal here.
      * **A named target record is REQUIRED.** No default person record: writing
        Alfred's model of the operator into a guessed path is exactly the blind
        placement the recurrence loop refuses.
      * **Already-decided and not-pending both refuse.** An approve cannot be
        replayed, so a double-tap cannot double-append the bullet.
      * **No timer, anywhere.** There is no age parameter, no cutoff, no
        auto-confirm. A proposal nobody approves is never applied.

    ORDER: vault write THEN decided row. A crash between them leaves the
    proposal pending and re-approvable, which over-asks; the reverse order would
    burn the proposal on a failed write and silently drop the observation.

    Returns a machine-readable result dict (``error`` key on refusal).
    """
    who = (operator or "").strip()
    if not who:
        return {
            "error": "approve requires a named operator (the approver is recorded "
                     "on the decision row; calibration is never applied anonymously)",
            "proposal_id": proposal_id_,
        }
    target = (user_rel_path or "").strip()
    if not target:
        return {
            "error": "no calibration target record — set telegram.primary_users "
                     "(there is NO default person record; placement is always "
                     "operator-chosen)",
            "proposal_id": proposal_id_,
        }

    if proposal_id_ in decided_ids(config.decided_path):
        return {
            "error": f"proposal {proposal_id_} already decided",
            "proposal_id": proposal_id_,
        }
    pending = {p.proposal_id: p for p in load_pending(config.pending_path)}
    row = pending.get(proposal_id_)
    if row is None:
        return {
            "error": f"proposal {proposal_id_} is not pending (unknown / already resolved)",
            "proposal_id": proposal_id_,
        }

    # Local import: calibration pulls vault ops (heavy) and this module is
    # imported by the Daily Sync section, which must stay import-light.
    from . import calibration

    # PRECONDITION: the target record must actually HAVE a calibration block.
    #
    # Found by its own pin rather than by reasoning, and worth stating because
    # the failure it prevents is silent. ``apply_proposals`` reports
    # ``written=True`` whenever ``vault_edit`` succeeds — and ``vault_edit``
    # succeeds on a record with NO calibration markers, because the frontmatter
    # ``attribution_audit`` write lands even though ``_insert_into_block`` logs
    # ``no_block_for_apply`` and returns the body UNCHANGED. So a bare
    # ``written`` check would record a decision for a bullet that never landed,
    # permanently excluding the observation from review with nothing applied.
    #
    # Refusing here keeps the proposal PENDING and tells the operator what to
    # fix, which is the correct direction: over-ask rather than silently lose.
    record = Path(vault_path) / (target if target.endswith(".md") else f"{target}.md")
    if not record.exists():
        return {
            "error": f"calibration target {target!r} does not exist in the vault",
            "proposal_id": proposal_id_,
        }
    try:
        if calibration.CALIBRATION_RE.search(record.read_text(encoding="utf-8")) is None:
            return {
                "error": (
                    f"{target!r} has no calibration block — add the "
                    f"{calibration.CALIBRATION_MARKER_START} / "
                    f"{calibration.CALIBRATION_MARKER_END} marker pair to the "
                    "record body first (the proposal stays pending)"
                ),
                "proposal_id": proposal_id_,
            }
    except OSError as exc:
        return {
            "error": f"could not read calibration target {target!r}: {exc}",
            "proposal_id": proposal_id_,
        }

    # Dial 1 — a SILENT write with no "[needs confirmation]" marker, and the
    # choice is load-bearing rather than incidental. The marker exists for the
    # dead bot's auto-append dials (2/3/4), where text landed in the record
    # WITHOUT the operator having seen it. Here the operator has just approved
    # this exact sentence by id, so stamping "[needs confirmation]" on it would
    # ask him to confirm a thing he is in the act of confirming. The
    # attribution-audit entry still records the provenance.
    result = calibration.apply_proposals(
        vault_path,
        target,
        [calibration.Proposal(
            subsection=row.subsection,
            bullet=row.bullet,
            confidence=row.confidence,
            source_session_rel=row.source_session_rel,
        )],
        row.source_session_rel,
        confirmation_dial=1,
        agent_slug=agent_slug,
    )

    if not result.get("written"):
        # The vault write failed or no-opped. Do NOT record a decision: the
        # proposal stays PENDING so the operator can re-approve once the record
        # is fixed. Recording it would burn the proposal on a missing
        # calibration block and leave no way back.
        reason = str(result.get("reason") or "unknown")
        log.warning(
            "talker.calibration.approve_write_failed",
            proposal_id=proposal_id_, reason=reason, target=target, operator=who,
        )
        return {
            "error": f"calibration write did not land ({reason}) — proposal stays pending",
            "proposal_id": proposal_id_,
            "reason": reason,
        }

    append_decision(config.decided_path, CalibrationDecision(
        proposal_id=proposal_id_,
        decision=DECISION_APPROVE,
        operator=who,
        decided_at=_now_iso(),
        applied_to=target,
    ))
    log.info(
        "talker.calibration.approved",
        proposal_id=proposal_id_, operator=who, target=target,
        subsection=row.subsection,
    )
    return {
        "approved": proposal_id_,
        "subsection": row.subsection,
        "bullet": row.bullet,
        "applied_to": target,
        "operator": who,
    }


def reject_proposal(
    config: Any,
    proposal_id_: str,
    *,
    operator: str,
) -> dict[str, Any]:
    """Operator REJECT — records the decision ONLY. No vault is touched.

    A rejected proposal never re-surfaces: ``decided_ids`` excludes it from both
    the review set and from re-capture, so the analyzer re-drafting the same
    sentence next week cannot put it back in front of him.
    """
    who = (operator or "").strip()
    if not who:
        return {
            "error": "reject requires a named operator (the decision is recorded)",
            "proposal_id": proposal_id_,
        }
    if proposal_id_ in decided_ids(config.decided_path):
        return {
            "error": f"proposal {proposal_id_} already decided",
            "proposal_id": proposal_id_,
        }
    if proposal_id_ not in {p.proposal_id for p in load_pending(config.pending_path)}:
        return {
            "error": f"proposal {proposal_id_} is not pending (unknown / already resolved)",
            "proposal_id": proposal_id_,
        }
    append_decision(config.decided_path, CalibrationDecision(
        proposal_id=proposal_id_,
        decision=DECISION_REJECT,
        operator=who,
        decided_at=_now_iso(),
    ))
    log.info("talker.calibration.rejected", proposal_id=proposal_id_, operator=who)
    return {"rejected": proposal_id_, "operator": who}


__all__ = [
    "DECISION_APPROVE",
    "DECISION_REJECT",
    "PendingCalibrationProposal",
    "CalibrationDecision",
    "proposal_id",
    "append_pending",
    "load_pending",
    "append_decision",
    "load_decisions",
    "decided_ids",
    "open_proposals",
    "record_proposals",
    "approve_proposal",
    "reject_proposal",
]
