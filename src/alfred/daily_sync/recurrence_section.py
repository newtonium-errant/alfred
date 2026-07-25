"""Daily Sync section — ad-hoc-T3 recurrence→promote proposals for review (#20 P5 B1).

PROPOSE-ONLY surface of the self-correcting recurrence loop
(``feedback_self_correcting_design_standard``). Unlike ``routine_match_section`` (which only READS a
sink written elsewhere), this section MATERIALIZES on render: it runs the cross-day ``done_at``-history
detection (:func:`alfred.tier.promote.materialize_proposals`), which idempotently appends any NEW
recurrence proposals to the pending queue, then surfaces the pending set so the operator can promote a
recurring ad-hoc chore to a routine. The GUARDRAIL holds: detection writes ONLY the pending queue — no
``routine/`` record is ever created here (that is B2's approval-gated write); the decided store is
written only by B2's approve/reject.

Mirrors ``routine_match_section`` (batch holder + ``consume_last_batch`` / ``peek_last_batch_count`` for
continuous global numbering + B2 reply routing) and ``triage_section`` (``set_vault_path`` injected once
by the daemon; a defensive guard renders nothing when the vault isn't wired). ILB sentinel when
enabled-but-empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from pathlib import Path
from typing import Any

import structlog

from . import assembler
from .config import DailySyncConfig

log = structlog.get_logger(__name__)

# Priority slot — immediately after routine_match (27), grouping with the review/calibration surfaces.
_PRIORITY = 28


# The daemon injects the vault path once at startup (mirrors email_section / triage_section); the
# provider reads it to scan ``vault/daily/*.md``. Absent (a daemon that didn't wire it, or a test) →
# the section renders nothing (defensive — never crash the assembly).
_VAULT_PATH_HOLDER: dict[str, Path | None] = {"path": None}


def set_vault_path(vault_path: Path) -> None:
    _VAULT_PATH_HOLDER["path"] = vault_path


def get_vault_path() -> Path | None:
    return _VAULT_PATH_HOLDER.get("path")


@dataclass
class RecurrenceProposalDisplayItem:
    """One Daily Sync recurrence-proposal review item (display + routing). Carries the GLOBAL
    ``item_number`` (assigned from the assembler's ``start_index``) + the proposal fields, persisted
    into ``last_batch`` so B2's reply dispatcher routes an approve/reject to the right ``proposal_id``
    without re-reading the pending sink."""

    item_number: int          # 1-indexed, GLOBAL across Daily Sync sections
    proposal_id: str
    sample_text: str
    done_days: int
    window_days: int
    suggested_cadence_days: int = 7   # the inferred soft-cadence suggestion (display; operator confirms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_number": self.item_number,
            "proposal_id": self.proposal_id,
            "sample_text": self.sample_text,
            "done_days": self.done_days,
            "window_days": self.window_days,
            "suggested_cadence_days": self.suggested_cadence_days,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecurrenceProposalDisplayItem":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


_LAST_BATCH_HOLDER: dict[str, list[RecurrenceProposalDisplayItem]] = {"items": []}


def consume_last_batch() -> list[RecurrenceProposalDisplayItem]:
    """Return and clear the most recently-surfaced batch (the daemon persists it into ``last_batch``
    for B2 reply routing)."""
    items = _LAST_BATCH_HOLDER.get("items", [])
    _LAST_BATCH_HOLDER["items"] = []
    return items


def peek_last_batch_count() -> int:
    """Non-destructive count for the assembler's ``item_count_after`` hook (continuous numbering)."""
    return len(_LAST_BATCH_HOLDER.get("items", []))


def _format_item(item: RecurrenceProposalDisplayItem) -> str:
    return (
        f"{item.item_number}. “{item.sample_text}” — done {item.done_days} "
        f"day{'s' if item.done_days != 1 else ''} in the last {item.window_days} "
        f"→ promote to a routine (suggest every ~{item.suggested_cadence_days}d)?  [{item.proposal_id}]"
    )


def recurrence_review_section(
    config: DailySyncConfig,
    today: date_type,
    start_index: int = 1,
) -> str | None:
    """Section provider — materialize + surface ad-hoc-T3 recurrence-promotion proposals.

    Returns ``None`` (section omitted) when disabled, or when the vault path isn't wired (defensive).
    When ENABLED, always renders: the proposal list, or the intentionally-left-blank sentinel when
    there's nothing over threshold (idle distinguishable from broken)."""
    tr = config.tier_recurrence
    if not tr.enabled:
        _LAST_BATCH_HOLDER["items"] = []
        return None

    vault_path = get_vault_path()
    if vault_path is None or not vault_path.is_dir():
        _LAST_BATCH_HOLDER["items"] = []
        log.info(
            "tier_recurrence.vault_not_wired",
            detail="daily_sync.tier_recurrence enabled but no vault path wired — section omitted this fire.",
        )
        return None

    # Lazy import — keeps the heavy tier/routine deps off the daily_sync import path.
    from alfred.tier import promote

    proposals = promote.materialize_proposals(vault_path, tr, today)
    items = [
        RecurrenceProposalDisplayItem(
            item_number=start_index + i,
            proposal_id=p.proposal_id,
            sample_text=p.sample_text,
            done_days=p.done_days,
            window_days=p.window_days,
            suggested_cadence_days=promote.infer_cadence_days(p),
        )
        for i, p in enumerate(proposals)
    ]
    _LAST_BATCH_HOLDER["items"] = items

    if not items:
        log.info("tier_recurrence.no_proposals", pending_path=tr.pending_path)
        return (
            "## Recurring task review\n\n"
            "No recurring ad-hoc items to propose."
        )

    log.info("tier_recurrence.surfaced", count=len(items), pending_path=tr.pending_path)
    plural = "s" if len(items) != 1 else ""
    lines = [f"## Recurring task review ({len(items)} item{plural})", ""]
    for item in items:
        lines.append(_format_item(item))
    lines.append("")
    # Decisions run on the box via the CLI (the routine-write is a deliberate, placed action). No-junk-
    # default: placement is operator-chosen (--routine, or the configured promote_routine) — never a
    # blind default. (The Daily Sync reply-approve/reject flow is the boarded B2b follow-up.)
    default_hint = (f" (default routine: “{tr.promote_routine}”)" if tr.promote_routine else "")
    lines.append(
        "Approve on the box: `alfred tier-recurrence approve <id> --operator <you> --routine <record>"
        f"`{default_hint}. Reject: `alfred tier-recurrence reject <id> --operator <you>`.")
    return "\n".join(lines).rstrip()


def register() -> None:
    """Register the section provider (idempotent — the daemon re-registers every fire)."""
    if "tier_recurrence" in assembler.registered_providers():
        return
    assembler.register_provider(
        "tier_recurrence",
        priority=_PRIORITY,
        provider=recurrence_review_section,
        item_count_after=peek_last_batch_count,
    )


__all__ = [
    "RecurrenceProposalDisplayItem",
    "consume_last_batch",
    "peek_last_batch_count",
    "register",
    "recurrence_review_section",
    "set_vault_path",
    "get_vault_path",
]
