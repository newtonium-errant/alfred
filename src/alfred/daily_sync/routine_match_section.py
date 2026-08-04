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

from dataclasses import dataclass, field
from datetime import date as date_type
from pathlib import Path
from typing import Any

import structlog

from alfred.routine.match_calibration import (
    filter_pending_for_review,
    load_glossary,
    load_pending,
)

from . import assembler
from .config import DailySyncConfig

log = structlog.get_logger(__name__)

# Priority slot — after the attribution calibration section (25), grouping the
# routine-match calibration with the other review/calibration surfaces.
_PRIORITY = 27

# #13 — runaway guard on the correction pick-list stamped onto each item, NOT a
# routine trim. Salem's whole active inventory is well under this, so tripping it
# means a routine record exploded; the truncation is logged rather than silent
# because a picker missing the operator's item looks like a bug in the picker.
MAX_CORRECTION_CANDIDATES = 200

# Vault-path holder, mirroring attribution_section / triage_section. The daemon
# injects it once at startup; when it is unset the section still renders (the
# review list needs no vault) but stamps an EMPTY candidate list — the card then
# offers the one-off door only, which is honest, rather than an empty picker
# that looks broken.
_VAULT_PATH_HOLDER: dict[str, Path | None] = {"path": None}


def set_vault_path(vault_path: Path) -> None:
    """Inject the vault path (daemon startup) so the section can stamp the #13
    correction pick-list onto each item."""
    _VAULT_PATH_HOLDER["path"] = vault_path


def get_vault_path() -> Path | None:
    return _VAULT_PATH_HOLDER.get("path")


def _correction_candidates(record: str) -> list[dict[str, str]]:
    """The pick-list offered when the operator rejects a suggestion (#13).

    Every ACTIVE routine item in the vault, the proposed item's OWN record
    first — a mis-match is far more often a sibling on the same routine than
    something across the vault, so that ordering puts the likely answer where
    the thumb already is. Returns ``[]`` (never raises) when the vault path was
    never injected or the walk fails: the card degrades to reject / one-off
    rather than the whole act failing over a display list.

    This list drives DISPLAY only. The resolver re-reads the vault and validates
    the operator's pick at write time, so a stale or truncated list can cost a
    retry but can never put something into the corpus that isn't a real item.
    """
    vault_path = get_vault_path()
    if vault_path is None:
        return []
    try:
        from alfred.routine.cli import _iter_active_routine_items

        candidates = _iter_active_routine_items(vault_path)
    except Exception as exc:  # noqa: BLE001 — display list, never fatal
        log.warning(
            "routine_match.candidates_unavailable",
            vault_path=str(vault_path),
            error=str(exc),
        )
        return []
    same = [c for c in candidates if c.record_name == record]
    other = [c for c in candidates if c.record_name != record]
    rows = [
        {"text": c.item_text, "record": c.record_name}
        for c in (*same, *other)
    ]
    if len(rows) > MAX_CORRECTION_CANDIDATES:
        log.warning(
            "routine_match.candidates_truncated",
            total=len(rows),
            kept=MAX_CORRECTION_CANDIDATES,
            record=record,
        )
        rows = rows[:MAX_CORRECTION_CANDIDATES]
    return rows


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
    # #13: the correction pick-list — ``{"text", "record"}`` rows for every
    # active routine item, the proposed item's own record first. Rendered by the
    # deck's "what did this mean?" picker. DISPLAY DATA ONLY: the resolver
    # re-reads the vault and validates the operator's pick before writing, so a
    # stale list can cost a retry but never poisons the corpus.
    candidates: list[dict[str, str]] = field(default_factory=list)

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
            "candidates": list(self.candidates),
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

    # Narrow the append-only sink to what's actually worth reviewing today:
    # drop rows the operator already ruled on (the corpus is the verdict
    # record), retire rows too old to be worth re-asking, cap the day's list.
    # Without this the sink only ever grows, so a rejected suggestion came
    # back every morning forever — the matcher honoured the reject while the
    # review card ignored it. Filtering HERE fixes both surfaces at once: the
    # daemon feeds the deck from this same batch (``consume_last_batch``).
    pending = load_pending(rm.pending_path)
    glossary = load_glossary(rm.corpus_path)
    surfaced, stats = filter_pending_for_review(
        pending,
        glossary,
        today=today,
        max_age_days=rm.pending_max_age_days,
        max_items=rm.pending_max_items,
    )
    if stats.suppressed():
        # ILB: a shrinking review list must be explicable — "ruled on already"
        # has to be distinguishable from "the section broke".
        log.info(
            "routine_match.pending_suppressed",
            captured=stats.captured,
            surfaced=stats.surfaced,
            resolved=stats.resolved,
            aged_out=stats.aged_out,
            capped=stats.capped,
            # #13 — reported separately from ``resolved`` so "you told me that
            # phrase means nothing" stays legible as its own reason for a
            # shrinking list.
            one_off=stats.one_off,
            corpus_path=rm.corpus_path,
        )
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
            candidates=_correction_candidates(p.record),
        )
        for i, p in enumerate(surfaced)
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
    "MAX_CORRECTION_CANDIDATES",
    "RoutineMatchItem",
    "consume_last_batch",
    "get_vault_path",
    "peek_last_batch_count",
    "register",
    "routine_match_section",
    "set_vault_path",
]
