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

from datetime import date as date_type

import structlog

from alfred.routine.match_calibration import PendingMatch, load_pending

from . import assembler
from .config import DailySyncConfig

log = structlog.get_logger(__name__)

# Priority slot — after the attribution calibration section (25), grouping the
# routine-match calibration with the other review/calibration surfaces.
_PRIORITY = 27

# Module-level batch holder (mirrors friction_section / radar_section) so the
# daemon can read the surfaced items back after the assembler runs — Phase 2
# stashes them into ``last_batch`` for reply routing. Phase 1 populates it but
# nothing consumes it yet (harmless).
_LAST_BATCH_HOLDER: dict[str, list[PendingMatch]] = {"items": []}


def consume_last_batch() -> list[PendingMatch]:
    """Return and clear the most recently-surfaced batch."""
    items = _LAST_BATCH_HOLDER.get("items", [])
    _LAST_BATCH_HOLDER["items"] = []
    return items


def peek_last_batch_count() -> int:
    """Non-destructive count for the assembler's ``item_count_after`` hook so
    the next section's items number continuously after these."""
    return len(_LAST_BATCH_HOLDER.get("items", []))


def _format_pending(entry: PendingMatch, item_no: int) -> str:
    """Render one pending match as a numbered review line."""
    record = entry.record or "?"
    return (
        f"{item_no}. “{entry.query}” → "
        f"“{entry.matched_to}” "
        f"(conf {entry.confidence:.2f}) on {record}"
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
    _LAST_BATCH_HOLDER["items"] = list(pending)

    if not pending:
        # ILB: enabled but nothing to review — explicit, not silent.
        log.info(
            "routine_match.no_pending",
            pending_path=rm.pending_path,
        )
        return (
            "Routine match review\n"
            "No low-confidence routine matches to review."
        )

    log.info(
        "routine_match.surfaced",
        count=len(pending),
        pending_path=rm.pending_path,
    )
    lines = ["Routine match review — confirm/reject these fuzzy matches:"]
    item_no = start_index
    for entry in pending:
        lines.append(_format_pending(entry, item_no))
        item_no += 1
    return "\n".join(lines)


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
    "consume_last_batch",
    "peek_last_batch_count",
    "register",
    "routine_match_section",
]
