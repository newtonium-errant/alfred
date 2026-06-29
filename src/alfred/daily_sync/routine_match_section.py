"""Daily Sync section — low-confidence ``routine_done`` matches for review.

Phase 1 (capture + surface, read-only) of the self-correcting matcher loop
(``feedback_self_correcting_design_standard``). The routine fuzzy matcher
captures each low-confidence completion match to a pending JSONL
(``routine.match_calibration``); this section reads that sink and SURFACES the
pending matches in the 09:00 Daily Sync so the operator can see what the matcher
was unsure about.

Phase 1 is read-only — it lists ``query → matched_to (confidence) on record``.
Phase 2 adds the confirm/reject reply routing (``reply_dispatch``) that mutates
the learned glossary. The guardrail holds across both: surfacing a pending match
changes nothing; the glossary mutates only on an operator reply.

Mirrors ``friction_section`` / ``triage_section``: a module batch holder +
``consume_last_batch`` / ``peek_last_batch_count`` (the assembler's
``item_count_after`` hook keeps numbering continuous across sections), and the
intentionally-left-blank sentinel line when enabled-but-empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from typing import Any

import structlog

from alfred.routine.match_calibration import load_pending

from . import assembler
from .config import DailySyncConfig

log = structlog.get_logger(__name__)

# Priority slot — after the attribution calibration section (25), grouping the
# routine-match calibration with the other review/calibration surfaces.
_PRIORITY = 27


@dataclass
class RoutineMatchItem:
    """One Daily Sync routine-match review item (display + routing).

    Mirrors :class:`alfred.daily_sync.attribution_section.AttributionItem`:
    the underlying capture record (``PendingMatch``) is the AuditEntry-analog,
    and this is the AttributionItem-analog — it carries the ``item_number``
    (GLOBAL across Daily Sync sections, assigned by the section provider from
    the assembler's ``start_index``) plus the captured-match fields, persisted
    into ``last_batch.routine_match_items`` so the reply dispatcher can route a
    confirm/reject to the right pending match without re-reading the capture
    sink.

    ``PendingMatch`` stays a pure capture record (no ``item_number`` — that's a
    per-Daily-Sync-render concern); this display item carries the routing key.
    """

    item_number: int  # 1-indexed, GLOBAL across Daily Sync sections
    query: str  # the operator's free-text completion phrase
    matched_to: str  # the matched item (low_conf) OR closest candidate (no_match)
    record: str  # the routine record the item lives on
    confidence: float  # the _match_confidence score at capture time
    completion_date: str = ""  # the date the completion was logged for
    captured_at: str = ""  # ISO timestamp of capture
    # Phase 3: "low_conf" (confirm/reject a below-threshold match) or
    # "no_match" (confirm = alias the phrasing, reject = suppress the
    # suggestion). Default keeps Phase-2b rows (no kind) loading unchanged.
    kind: str = "low_conf"

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_number": self.item_number,
            "query": self.query,
            "matched_to": self.matched_to,
            "record": self.record,
            "confidence": self.confidence,
            "completion_date": self.completion_date,
            "captured_at": self.captured_at,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RoutineMatchItem":
        """Schema-tolerant construct — filter to known fields (load contract)."""
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


# Module-level batch holder (mirrors attribution_section / friction_section) so
# the daemon can read the surfaced items back after the assembler runs and
# persist them into ``last_batch`` for reply routing (Phase 2b).
_LAST_BATCH_HOLDER: dict[str, list[RoutineMatchItem]] = {"items": []}


def consume_last_batch() -> list[RoutineMatchItem]:
    """Return and clear the most recently-surfaced batch.

    Called by the daemon after :func:`assemble_message` so it can persist the
    item ↔ pending-match mapping into ``last_batch.routine_match_items``.
    """
    items = _LAST_BATCH_HOLDER.get("items", [])
    _LAST_BATCH_HOLDER["items"] = []
    return items


def peek_last_batch_count() -> int:
    """Non-destructive count for the assembler's ``item_count_after`` hook so
    the next section's items number continuously after these."""
    return len(_LAST_BATCH_HOLDER.get("items", []))


def _format_item(item: RoutineMatchItem) -> str:
    """Render one routine-match review item as a numbered line.

    Two shapes by kind:
      * ``low_conf`` — a below-threshold match the matcher MADE:
        ``N. "query" → "matched_to" (conf X.XX) on record``
      * ``no_match`` — nothing matched; ``matched_to`` is the closest
        candidate suggestion:
        ``N. "query" → nothing matched — did you mean "matched_to"? (on record)``
    """
    record = item.record or "?"
    if item.kind == "no_match":
        return (
            f"{item.item_number}. “{item.query}” → nothing matched — "
            f"did you mean “{item.matched_to}”? (on {record})"
        )
    return (
        f"{item.item_number}. “{item.query}” → "
        f"“{item.matched_to}” "
        f"(conf {item.confidence:.2f}) on {record}"
    )


def routine_match_section(
    config: DailySyncConfig,
    today: date_type,
    start_index: int = 1,
) -> str | None:
    """Section provider — list low-confidence routine matches awaiting review.

    Returns ``None`` (section omitted) when the feature is disabled — instances
    that don't run routine calibration stay unaffected. When ENABLED, always
    renders: the pending list, or the intentionally-left-blank sentinel when
    there's nothing to review (idle is distinguishable from broken).
    """
    rm = config.routine_match
    if not rm.enabled:
        _LAST_BATCH_HOLDER["items"] = []
        return None

    pending = load_pending(rm.pending_path)
    # Number the items GLOBALLY from the assembler's start_index so the reply
    # dispatcher can route "item N confirm" against the persisted batch.
    items = [
        RoutineMatchItem(
            item_number=start_index + i,
            query=p.query,
            matched_to=p.matched_to,
            record=p.record,
            confidence=p.confidence,
            completion_date=p.completion_date,
            captured_at=p.captured_at,
            kind=p.kind,
        )
        for i, p in enumerate(pending)
    ]
    _LAST_BATCH_HOLDER["items"] = items

    if not items:
        # ILB: enabled but nothing to review — explicit, not silent.
        log.info(
            "routine_match.no_pending",
            pending_path=rm.pending_path,
        )
        # Markdown ``##`` section header to match the sibling sections
        # (attribution / friction / radar). The assembler joins section
        # outputs verbatim with ``\n\n`` — it does NOT wrap titles — so each
        # section emits its own header.
        return (
            "## Routine match review\n\n"
            "No low-confidence routine matches to review."
        )

    log.info(
        "routine_match.surfaced",
        count=len(items),
        pending_path=rm.pending_path,
    )
    plural = "s" if len(items) != 1 else ""
    lines = [f"## Routine match review ({len(items)} item{plural})", ""]
    for item in items:
        lines.append(_format_item(item))
    lines.append("")
    lines.append("Reply with `N confirm` / `N reject`.")
    return "\n".join(lines).rstrip()


def register() -> None:
    """Register the section provider (idempotent — re-fire safe).

    Guard against double-registration (``register_provider`` raises on a
    duplicate name; the daemon re-registers every fire) — mirrors the other
    section ``register()`` helpers.
    """
    if "routine_match" in assembler.registered_providers():
        return
    assembler.register_provider(
        "routine_match",
        priority=_PRIORITY,
        provider=routine_match_section,
        item_count_after=peek_last_batch_count,
    )


__all__ = [
    "RoutineMatchItem",
    "consume_last_batch",
    "peek_last_batch_count",
    "register",
    "routine_match_section",
]
