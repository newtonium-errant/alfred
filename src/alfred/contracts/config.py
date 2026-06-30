"""Contract config — the ``contracts:`` section (mirror
``load_message_bus_config``).

KAL-LE (the broker) holds the contract store. ``operator_id`` stamps the
ratify/reject authority (generic default ``"operator"`` — NOT an
instance-specific literal; the operator sets their own).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


# Tool-scoped default; ``<kalle_data>/contracts`` at deploy.
DEFAULT_CONTRACT_STORE_DIR = "./data/contracts"
_AUDIT_FILENAME = "contract_audit.jsonl"


@dataclass
class ContractConfig:
    """Typed view of the ``contracts:`` config section."""

    enabled: bool = False
    store_path: str = DEFAULT_CONTRACT_STORE_DIR
    audit_log_path: str = ""  # defaults to <store_path>/contract_audit.jsonl
    operator_id: str = "operator"

    def resolved_audit_path(self) -> str:
        return self.audit_log_path or os.path.join(
            self.store_path, _AUDIT_FILENAME,
        )


def load_contract_config(raw: dict[str, Any]) -> ContractConfig:
    """Build :class:`ContractConfig` from the unified config dict.

    Tolerant of an absent block (all-default, ``enabled=False``) so the
    code is byte-inert without a ``contracts:`` section."""
    section = raw.get("contracts") or {}
    if not isinstance(section, dict):
        return ContractConfig()
    return ContractConfig(
        enabled=bool(section.get("enabled", False)),
        store_path=str(
            section.get("store_path", "") or DEFAULT_CONTRACT_STORE_DIR
        ),
        audit_log_path=str(section.get("audit_log_path", "") or ""),
        operator_id=str(section.get("operator_id", "") or "operator"),
    )


__all__ = [
    "DEFAULT_CONTRACT_STORE_DIR",
    "ContractConfig",
    "load_contract_config",
]
