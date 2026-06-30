"""Contracts-awaiting section — Daily Sync surface (clone of
``canonical_proposals_section``).

Surfaces contracts needing the operator (converged-but-unratified OR
blocked) as numbered items. The operator authority TODAY is the
``alfred contract ratify/reject`` CLI (what the rendered footer points at).
ILB: ``render_batch`` returns ``None`` when nothing awaits (suppress the
header on the common empty case), matching the canonical-proposals
convention.

DEFERRED (NOT yet wired — follow-up): a Daily-Sync ``N ratify`` / ``N
reject`` reply-dispatch shortcut + a Telegram ping. Until then the batch is
rendered but NOT consumed by the daemon, and replies go through the CLI.

DORMANT-SAFE: with no contracts the batch is empty and the section renders
nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import structlog

from .config import DailySyncConfig

log = structlog.get_logger(__name__)


@dataclass
class ContractAwaitingItem:
    """One contract-awaiting item in a Daily Sync batch. Carries the
    item↔contract_id mapping a FUTURE reply-dispatch shortcut would use
    (not wired yet — see the module docstring)."""

    item_number: int  # 1-indexed, GLOBAL across Daily Sync sections
    contract_id: str
    seam: str
    version: int
    converged: bool
    blocked: bool
    gaps: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_number": self.item_number,
            "contract_id": self.contract_id,
            "seam": self.seam,
            "version": self.version,
            "converged": self.converged,
            "blocked": self.blocked,
            "gaps": list(self.gaps),
        }


def _load_store(config: DailySyncConfig):
    """Resolve the contract store from this instance's config. Mirrors
    ``canonical_proposals_section._proposals_queue_path`` — threads
    ``config.config_path`` so a per-instance daemon reads ITS config."""
    try:
        from alfred.cli import _load_unified_config
        from alfred.contracts.config import load_contract_config
        from alfred.contracts.store import ContractStore
        raw = _load_unified_config(config.config_path or "config.yaml")
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — _load_unified_config sys.exits on a missing file
        log.info("daily_sync.contracts.config_unavailable", error=str(exc))
        return None
    cc = load_contract_config(raw)
    return ContractStore(cc.store_path, cc.resolved_audit_path())


def build_batch(
    config: DailySyncConfig,
    *,
    start_index: int = 1,
) -> list[ContractAwaitingItem]:
    """Contracts awaiting the operator → :class:`ContractAwaitingItem` rows.

    Returns ``[]`` when none await (the steady state). ``start_index``
    (1-based, GLOBAL) keeps Daily Sync numbering continuous."""
    from alfred.contracts.schema import find_gaps, is_converged

    store = _load_store(config)
    if store is None:
        return []
    try:
        awaiting = store.list_awaiting()
    except Exception as exc:  # noqa: BLE001
        log.info("daily_sync.contracts.read_failed", error=str(exc))
        return []
    if not awaiting:
        return []
    return [
        ContractAwaitingItem(
            item_number=start_index + i,
            contract_id=c.contract_id,
            seam=c.seam,
            version=c.version,
            converged=is_converged(c),
            blocked=(c.state == "blocked"),
            gaps=[g.item for g in find_gaps(c)],
        )
        for i, c in enumerate(awaiting)
    ]


def render_batch(items: list[ContractAwaitingItem]) -> str | None:
    """Render the contracts-awaiting section. Returns ``None`` when empty
    (suppress the header, per the canonical-proposals convention)."""
    if not items:
        return None
    plural = "s" if len(items) != 1 else ""
    lines = [f"## Contracts awaiting ratification ({len(items)} item{plural})", ""]
    for item in items:
        status = "blocked" if item.blocked else f"agents converged on v{item.version}"
        gaps = ", ".join(item.gaps) if item.gaps else "none"
        lines.append(
            f'{item.item_number}. [CONTRACT awaiting ratification] '
            f'"{item.seam}" — {status}; gaps: {gaps}'
        )
        lines.append(f"   contract: {item.contract_id}")
        lines.append("")
    # Operator authority is the CLI (the reply-verb shortcut `N ratify` is
    # a deferred convenience — see the build report). Point at the working
    # path so the footer never lies about behavior.
    lines.append(
        "Ratify via `alfred contract ratify <id>`, "
        "reject via `alfred contract reject <id>`."
    )
    return "\n".join(lines).rstrip()


# Module-level holder (mirrors canonical_proposals_section). Present so a
# FUTURE reply-dispatch shortcut can read the batch back to persist the
# item↔contract_id mapping — NOT consumed by the daemon today (the deferred
# follow-up; replies go through the CLI).
_LAST_BATCH_HOLDER: dict[str, list[ContractAwaitingItem]] = {"items": []}


def consume_last_batch() -> list[ContractAwaitingItem]:
    items = _LAST_BATCH_HOLDER.get("items", [])
    _LAST_BATCH_HOLDER["items"] = []
    return items


def peek_last_batch_count() -> int:
    return len(_LAST_BATCH_HOLDER.get("items", []))


def contracts_awaiting_section(
    config: DailySyncConfig,
    today: date,
    *,
    start_index: int = 1,
) -> str | None:
    """Section provider — builds + renders the contracts-awaiting batch."""
    items = build_batch(config, start_index=start_index)
    _LAST_BATCH_HOLDER["items"] = items
    return render_batch(items)


def register() -> None:
    """Idempotent provider registration (priority 16 — right after the
    canonical-proposals section at 15; both are operator-decision queues)."""
    from . import assembler
    if "contracts_awaiting" in assembler.registered_providers():
        return
    assembler.register_provider(
        "contracts_awaiting",
        priority=16,
        provider=contracts_awaiting_section,
        item_count_after=peek_last_batch_count,
    )


__all__ = [
    "ContractAwaitingItem",
    "build_batch",
    "consume_last_batch",
    "contracts_awaiting_section",
    "peek_last_batch_count",
    "register",
    "render_batch",
]
