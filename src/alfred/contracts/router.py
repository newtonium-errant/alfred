"""Contract router — the bus entry point for CONTRACT_KINDS messages.

The bus routing daemon hands a routed message to :func:`apply_contract_message`
(instead of plain inbox-routing) when its ``kind ∈ CONTRACT_KINDS``. This
applies the message to the KAL-LE-held contract via
:meth:`ContractStore.apply_message`, then routes a notice to the
counterparty's inbox and flags an operator ping on convergence / block.

Bus messages are AGENT actions (``actor_is_operator=False``). Operator
ratify/reject arrive via the ``alfred contract`` CLI (the operator
authority), NOT the bus.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import frontmatter
import structlog

from alfred.msgbus.record import (
    MessageRecord,
    message_filename,
    write_message_file,
)
from alfred.msgbus.router import _now_iso, mint_message_id

from .config import ContractConfig, load_contract_config
from .schema import STATE_BLOCKED, find_gaps
from .store import ApplyResult, ContractStore

log = structlog.get_logger(__name__)

ROUTED_BY = "kalle"


def _write_notice(inbox: Path, to_project: str, subject: str, body: str) -> None:
    """Write an ``fyi`` notice into a participant's inbox (the bus carries
    the heads-up; the contract artifact is the source of truth)."""
    created = _now_iso()
    rec = MessageRecord(
        from_project=ROUTED_BY,
        to_project=to_project,
        kind="fyi",
        correlation_id="",
        created=created,
        subject=subject,
        body=body,
    )
    rec.id = mint_message_id(rec.from_project, to_project, created, subject, body)
    rec.routed_at = created
    rec.routed_by = ROUTED_BY
    write_message_file(inbox / message_filename(rec), rec)


def _notice_text(c, result: ApplyResult) -> tuple[str, str]:
    subject = (
        f"[contract] {c.seam} — {result.kind} → {c.state} (v{c.version})"
    )
    lines = [
        f"contract_id: {c.contract_id}",
        f"seam: {c.seam}",
        f"state: {c.state}  version: {c.version}",
        f"last action: {result.kind} by {result.actor}",
    ]
    if result.converged:
        lines.append("status: CONVERGED — awaiting operator ratification")
    gaps = find_gaps(c)
    if gaps:
        lines.append("gaps: " + ", ".join(g.item for g in gaps))
    if c.state == STATE_BLOCKED and c.blocked_reason:
        lines.append(f"blocked: {c.blocked_reason}")
    return subject, "\n".join(lines)


def apply_contract_message(
    msg_fm: dict[str, Any],
    body: str,
    *,
    contract_config: ContractConfig,
    registry: Any = None,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply one CONTRACT_KINDS bus message + route notices.

    Returns a small dict for the bus tick: ``{ok, contract_id, state,
    converged, notified, ping_operator, reason}``."""
    store = ContractStore(
        contract_config.store_path, contract_config.resolved_audit_path(),
    )
    actor = str(msg_fm.get("from") or "")
    result = store.apply_message(
        msg_fm, body, actor=actor, actor_is_operator=False,
    )
    if not result.ok:
        log.warning(
            "contracts.router.apply_rejected",
            reason=result.reason, actor=actor, kind=msg_fm.get("kind"),
        )
        return {"ok": False, "reason": result.reason}

    c = result.contract
    notified: list[str] = []
    actor_project = actor.split("/")[0]
    if registry is not None:
        subject, notice_body = _notice_text(c, result)
        for p in c.participants:
            if p.project == actor_project:
                continue
            inbox = registry.inbox_for(p.project)
            if inbox is None:
                continue
            try:
                _write_notice(inbox, p.project, subject, notice_body)
                notified.append(p.project)
            except OSError as exc:
                log.warning(
                    "contracts.router.notice_write_failed",
                    to=p.project, error_type=exc.__class__.__name__,
                )

    ping_operator = result.converged or c.state == STATE_BLOCKED
    if ping_operator:
        # ILB-style signal — an operator-attention event every time a
        # contract converges or blocks (the C3 surfaces consume it).
        log.info(
            "contracts.router.operator_attention",
            contract_id=c.contract_id, state=c.state,
            converged=result.converged,
        )
    return {
        "ok": True,
        "contract_id": c.contract_id,
        "state": c.state,
        "converged": result.converged,
        "notified": notified,
        "ping_operator": ping_operator,
    }


def handle_bus_contract_message(
    src_path: str | Path,
    *,
    registry: Any,
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Bus-side entry: re-load the raw frontmatter (carries contract_id /
    seam, which the bus MessageRecord drops) + body, then apply. Called by
    ``msgbus.router.run_route_once`` for a CONTRACT_KINDS message."""
    config = load_contract_config(raw)
    post = frontmatter.load(str(src_path))
    return apply_contract_message(
        dict(post.metadata or {}),
        post.content or "",
        contract_config=config,
        registry=registry,
        raw=raw,
    )


__all__ = ["apply_contract_message", "handle_bus_contract_message"]
