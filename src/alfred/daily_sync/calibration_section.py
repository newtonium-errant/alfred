"""Daily Sync section — voice-calibration proposals awaiting review (R4 door 2).

READ-ONLY surface of the voice-calibration learning loop
(``feedback_self_correcting_design_standard``). It renders what the capture door
drafted and stops; the decision is made with ``alfred voice-calibration
approve|reject``.

WHY THE CLI IS THE MUTATION PATH rather than a Daily Sync reply. The reply
dispatcher (``daily_sync.reply_dispatch.handle_daily_sync_reply``) has had ZERO
production callers since ``bot.py`` was deleted in T4 C3 — four existing
sections still render reply instructions nobody can act on, because the
transport that carried the reply died with Telegram. Wiring a fifth into it
would have produced a section that looks live and is not. So this section
directs to the CLI, exactly as ``recurrence_section`` does, and the CLI verb is
the only door it names.

STRUCTURALLY READ-ONLY: this module imports ``calibration_store`` for
``open_proposals`` only. It never imports ``calibration`` (the writer) and never
calls ``approve_proposal``. Nothing on this path can mutate a vault record, and
nothing on it is time-based — an unreviewed proposal stays pending indefinitely
rather than being confirmed by a clock (the deliberate contrast with
``attribution_section``'s ``AUTO_CONFIRM_AFTER_HOURS``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from typing import Any

import structlog

from . import assembler
from .config import DailySyncConfig

log = structlog.get_logger(__name__)

# Priority slot — immediately after tier_recurrence (28), grouping with the
# review/calibration surfaces.
_PRIORITY = 29


@dataclass
class CalibrationProposalDisplayItem:
    """One Daily Sync calibration-review item (display + routing).

    Carries the GLOBAL ``item_number`` (assigned from the assembler's
    ``start_index``) alongside the proposal fields, matching the batch-holder
    shape every other numbered section uses.
    """

    item_number: int          # 1-indexed, GLOBAL across Daily Sync sections
    proposal_id: str
    subsection: str
    bullet: str
    confidence: float = 0.7
    source_session_rel: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_number": self.item_number,
            "proposal_id": self.proposal_id,
            "subsection": self.subsection,
            "bullet": self.bullet,
            "confidence": self.confidence,
            "source_session_rel": self.source_session_rel,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CalibrationProposalDisplayItem":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


_LAST_BATCH_HOLDER: dict[str, list[CalibrationProposalDisplayItem]] = {"items": []}


def consume_last_batch() -> list[CalibrationProposalDisplayItem]:
    """Return and clear the most recently-surfaced batch."""
    items = _LAST_BATCH_HOLDER.get("items", [])
    _LAST_BATCH_HOLDER["items"] = []
    return items


def peek_last_batch_count() -> int:
    """Non-destructive count for the assembler's ``item_count_after`` hook."""
    return len(_LAST_BATCH_HOLDER.get("items", []))


def _format_item(item: CalibrationProposalDisplayItem) -> str:
    src = item.source_session_rel or "(unknown session)"
    if src.endswith(".md"):
        src = src[:-3]
    return (
        f"{item.item_number}. [{item.subsection}] {item.bullet} "
        f"(confidence {item.confidence:.2f}, from {src})  [{item.proposal_id}]"
    )


def calibration_review_section(
    config: DailySyncConfig,
    today: date_type,
    start_index: int = 1,
) -> str | None:
    """Section provider — surface pending voice-calibration proposals.

    Returns ``None`` (section omitted) when disabled. When ENABLED, always
    renders: the proposal list, or the intentionally-left-blank sentinel when
    nothing is pending — so a quiet loop is distinguishable from a broken one.
    """
    cr = config.calibration_review
    if not cr.enabled:
        _LAST_BATCH_HOLDER["items"] = []
        return None

    # Lazy import — keeps the talker package off the daily_sync import path.
    from alfred.telegram import calibration_store

    proposals = calibration_store.open_proposals(cr.pending_path, cr.decided_path)
    items = [
        CalibrationProposalDisplayItem(
            item_number=start_index + i,
            proposal_id=p.proposal_id,
            subsection=p.subsection,
            bullet=p.bullet,
            confidence=p.confidence,
            source_session_rel=p.source_session_rel,
        )
        for i, p in enumerate(proposals)
    ]
    _LAST_BATCH_HOLDER["items"] = items

    if not items:
        log.info("calibration_review.no_proposals", pending_path=cr.pending_path)
        return (
            "## Voice calibration review\n\n"
            "No calibration proposals pending."
        )

    log.info(
        "calibration_review.surfaced",
        count=len(items), pending_path=cr.pending_path,
    )
    plural = "s" if len(items) != 1 else ""
    lines = [f"## Voice calibration review ({len(items)} item{plural})", ""]
    for item in items:
        lines.append(_format_item(item))
    lines.append("")
    # Decisions run on the box via the CLI. Stated as the ONLY door on purpose —
    # see the module docstring for why this section does not offer a reply.
    lines.append(
        "Approve on the box: `alfred voice-calibration approve <id> --operator <you>`. "
        "Reject: `alfred voice-calibration reject <id> --operator <you>`. "
        "Nothing is applied until you do — there is no timeout."
    )
    return "\n".join(lines).rstrip()


def register() -> None:
    """Register the section provider (idempotent — the daemon re-registers every fire)."""
    if "calibration_review" in assembler.registered_providers():
        return
    assembler.register_provider(
        "calibration_review",
        priority=_PRIORITY,
        provider=calibration_review_section,
        item_count_after=peek_last_batch_count,
    )


__all__ = [
    "CalibrationProposalDisplayItem",
    "consume_last_batch",
    "peek_last_batch_count",
    "register",
    "calibration_review_section",
]
