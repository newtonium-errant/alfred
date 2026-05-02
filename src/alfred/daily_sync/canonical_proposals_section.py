"""Canonical proposals section provider — propose-person c2.

Surfaces pending ``POST /canonical/<type>/propose`` queue entries
(written by :mod:`alfred.transport.canonical_proposals` on the Salem
side) as numbered items in the Daily Sync. Andrew replies ``N
confirm`` / ``N reject`` to flip each proposal's state.

On confirm: the dispatcher calls :func:`alfred.vault.ops.vault_create`
with ``scope='salem'`` and the proposer's ``proposed_fields`` to
create the canonical record, then marks the proposal ``accepted`` in
the queue JSONL.

On reject: the proposal is marked ``rejected`` and no record is
created. The audit line stays in the queue file so operators can
inspect why a given proposal didn't land.

Priority 15: renders after email calibration (10) and before
attribution audit (25). Proposals are relatively rare — a handful per
week once KAL-LE is routing coding sessions through canonical person
lookups — so surfacing them early in the Daily Sync (above attribution
audit but below email calibration) keeps them visible without crowding
out the high-volume email stream.

Design ratified in ``project_kalle_propose_person.md`` (2026-04-23).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import structlog

from alfred.transport.canonical_proposals import Proposal, list_pending

from .config import DailySyncConfig

log = structlog.get_logger(__name__)


@dataclass
class ProposalItem:
    """One canonical-proposal item in a Daily Sync batch.

    Persisted into the state file's ``last_batch.proposal_items`` list
    so the reply dispatcher can resolve "item 12" → proposal
    correlation_id without re-reading the queue file.
    """

    item_number: int  # 1-indexed, GLOBAL across Daily Sync sections
    correlation_id: str
    proposer: str
    record_type: str
    name: str
    proposed_fields: dict[str, Any]
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_number": self.item_number,
            "correlation_id": self.correlation_id,
            "proposer": self.proposer,
            "record_type": self.record_type,
            "name": self.name,
            "proposed_fields": dict(self.proposed_fields or {}),
            "source": self.source,
        }


def _proposals_queue_path(config: DailySyncConfig) -> str | None:
    """Resolve the proposals-queue path from the transport config block.

    The queue path lives in ``transport.canonical.proposals_path`` (c1).
    Rather than thread TransportConfig through the DailySyncConfig
    plumbing (which would be a wider refactor), we read the raw
    ``config.yaml`` via :func:`alfred.transport.config.load_config` and
    pick out the path. Returns ``None`` when the transport config can't
    be resolved (treated as "no proposals to surface").

    Threads ``config.config_path`` through to ``load_config(path)`` so
    a per-instance daily_sync daemon (Hypatia, KAL-LE) reads ITS OWN
    config file instead of silently defaulting to Salem's
    ``config.yaml``. Falls back to ``"config.yaml"`` when
    ``config.config_path`` is unset (backward compat for test fixtures
    that build a DailySyncConfig directly without going through the
    CLI's ``_load_unified_config``). Mirrors commit 420364b's pattern.
    """
    try:
        from alfred.transport.config import load_config
        transport_config = load_config(config.config_path or "config.yaml")
    except (FileNotFoundError, Exception) as exc:  # noqa: BLE001
        log.info(
            "daily_sync.proposals.config_unavailable",
            error=str(exc),
        )
        return None
    path = transport_config.canonical.proposals_path
    return path or None


def build_batch(
    config: DailySyncConfig,
    *,
    start_index: int = 1,
) -> list[ProposalItem]:
    """Sample pending proposals and return them as :class:`ProposalItem` rows.

    Returns ``[]`` when no proposals are pending (the steady state).
    Oldest-first preserves the chronological dialogue order — if Andrew
    didn't respond to yesterday's proposal, it still leads today's
    batch.

    ``start_index`` (1-based, GLOBAL across Daily Sync sections) lets
    the assembler keep numbering continuous.
    """
    queue_path = _proposals_queue_path(config)
    if queue_path is None:
        return []
    try:
        pending: list[Proposal] = list_pending(queue_path)
    except Exception as exc:  # noqa: BLE001
        log.info(
            "daily_sync.proposals.read_failed",
            error=str(exc),
        )
        return []
    if not pending:
        return []
    return [
        ProposalItem(
            item_number=start_index + i,
            correlation_id=p.correlation_id,
            proposer=p.proposer,
            record_type=p.record_type,
            name=p.name,
            proposed_fields=dict(p.proposed_fields or {}),
            source=p.source,
        )
        for i, p in enumerate(pending)
    ]


def render_batch(items: list[ProposalItem]) -> str | None:
    """Render the canonical-proposals section as a Daily Sync block.

    Returns ``None`` when the batch is empty — unlike the attribution
    section, we DO suppress the header on zero pending proposals so
    the Daily Sync stays concise on the common case (no pending
    proposals). The email-section model uses the same convention.

    Format::

        ## Canonical proposals (2 items)

        12. [KAL-LE proposes] person: "Elena Brighton"
            fields: description="NP colleague mentioned in aftermath-lab"
            source: KAL-LE session corr-id kal-le-propose-person-17a93f4c

        13. [KAL-LE proposes] person: "Arthur Mbeki"
            fields: description="co-author on Foundation DB migration"
            source: KAL-LE session corr-id kal-le-propose-person-19c0d3a1

        Reply with `N confirm` to create the record, `N reject` to discard.
    """
    if not items:
        return None
    plural = "s" if len(items) != 1 else ""
    lines = [f"## Canonical proposals ({len(items)} item{plural})", ""]
    for item in items:
        proposer_label = item.proposer.upper() if item.proposer else "?"
        lines.append(
            f'{item.item_number}. [{proposer_label} proposes] '
            f'{item.record_type}: "{item.name}"'
        )
        # Render a compact fields summary — one line per field, up to
        # 3 fields. Fields beyond the cap collapse to a "+N more" tail
        # so the Telegram bubble stays readable.
        field_lines = _render_fields_summary(item.proposed_fields)
        for field_line in field_lines:
            lines.append(f"   {field_line}")
        if item.source:
            lines.append(f"   source: {item.source}")
        lines.append("")
    lines.append(
        "Reply with `N confirm` to create the record, "
        "`N reject` to discard."
    )
    return "\n".join(lines).rstrip()


def _render_fields_summary(fields: dict[str, Any]) -> list[str]:
    """Compact one-line-per-field renderer. Caps long values at ~80 chars.

    Returns an empty list when fields is empty. When more than 3 fields
    are present, the first 3 render normally and the remainder collapse
    into a ``+N more`` tail — keeps the Telegram bubble compact.
    """
    if not fields:
        return []
    ordered = list(fields.items())
    out: list[str] = []
    for key, value in ordered[:3]:
        v_str = str(value)
        if len(v_str) > 80:
            v_str = v_str[:77] + "..."
        out.append(f'fields: {key}="{v_str}"' if len(ordered) == 1 else f'  {key}="{v_str}"')
    # For multi-field records, prepend a single "fields:" header so the
    # rendered block reads cleanly.
    if len(ordered) > 1:
        out = ["fields:"] + out
    remainder = len(ordered) - 3
    if remainder > 0:
        out.append(f"  ... and {remainder} more")
    return out


# ---------------------------------------------------------------------------
# Section provider entry point + registration
# ---------------------------------------------------------------------------


# Module-level holder so the daemon can read the batch back after the
# assembler runs. Mirrors the pattern used by email + attribution
# sections — the assembler signature returns only a string, so
# per-section metadata flows through this side channel.
_LAST_BATCH_HOLDER: dict[str, list[ProposalItem]] = {"items": []}


def consume_last_batch() -> list[ProposalItem]:
    """Return and clear the most recently-built batch.

    Called by the daemon after :func:`assemble_message` so it can
    persist the item ↔ correlation_id mapping into the Daily Sync
    state file under ``last_batch.proposal_items``.
    """
    items = _LAST_BATCH_HOLDER.get("items", [])
    _LAST_BATCH_HOLDER["items"] = []
    return items


def peek_last_batch_count() -> int:
    """Non-destructive count for the assembler's ``item_count_after`` hook.

    Lets the next section provider's items number continuously after
    this one's without consuming the batch (the daemon calls
    :func:`consume_last_batch` afterwards to actually persist).
    """
    return len(_LAST_BATCH_HOLDER.get("items", []))


def canonical_proposals_section(
    config: DailySyncConfig,
    today: date,
    *,
    start_index: int = 1,
) -> str | None:
    """Section provider — builds and renders the canonical-proposals batch.

    Registered with priority 15 (between email at 10 and attribution at
    25). Returns ``None`` when nothing is pending so the Daily Sync
    stays concise on quiet days.
    """
    items = build_batch(config, start_index=start_index)
    _LAST_BATCH_HOLDER["items"] = items
    return render_batch(items)


def register() -> None:
    """Idempotent provider registration. Safe to call multiple times."""
    from . import assembler
    if "canonical_proposals" in assembler.registered_providers():
        return
    assembler.register_provider(
        "canonical_proposals",
        priority=15,
        provider=canonical_proposals_section,
        item_count_after=peek_last_batch_count,
    )


__all__ = [
    "ProposalItem",
    "build_batch",
    "canonical_proposals_section",
    "consume_last_batch",
    "peek_last_batch_count",
    "register",
    "render_batch",
]
