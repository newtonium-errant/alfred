"""Contract audit — the append-only forensic trail.

The ``canonical_audit`` split: the contract ``.md`` is the mutable
current-state file; this JSONL is the immutable transition trail (every
``apply_message`` mirrors its :class:`~alfred.contracts.schema.Transition`
here). Modeled on ``github_ops.append_github_audit`` semantics — parent
dir create, single append write, NEVER raises (an audit failure must not
interrupt the apply or its containment).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


def append_contract_audit(
    audit_log_path: str | Path,
    *,
    contract_id: str,
    kind: str,
    from_state: str,
    to_state: str,
    actor: str,
    outcome: str,
    version: int | None = None,
    correlation_id: str = "",
    reason: str = "",
) -> None:
    """Append one audit row. Never raises (disk errors log-and-continue)."""
    if not audit_log_path:
        return
    path = Path(audit_log_path)
    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "contract_id": contract_id,
        "kind": kind,
        "from_state": from_state,
        "to_state": to_state,
        "actor": actor,
        "outcome": outcome,
        "version": version,
        "correlation_id": correlation_id,
        "reason": reason,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError as exc:
        log.warning(
            "contracts.audit_write_failed",
            path=str(path),
            error_type=exc.__class__.__name__,
        )


def read_contract_audit(audit_log_path: str | Path) -> list[dict[str, Any]]:
    """Read the audit log (tests + CLI inspection)."""
    path = Path(audit_log_path)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                out.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
    return out


__all__ = ["append_contract_audit", "read_contract_audit"]
