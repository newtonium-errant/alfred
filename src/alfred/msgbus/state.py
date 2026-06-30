"""Router state — the routed-message ledger (mirror ``TicketForwardState``).

Keyed by message ``id`` (the stable dedup key). A routed id is never
re-placed: ``scan_spool`` archives a re-dropped known id to ``routed/``
instead of placing a duplicate. State is deletable bookkeeping — losing it
only re-routes unrouted spool files; the id-keyed destination filename +
atomic write make re-placement idempotent (it overwrites the same target).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass
class MessageBusEntry:
    """Per-message routing bookkeeping, keyed by ``id``."""

    id: str = ""
    from_project: str = ""
    to_project: str = ""
    kind: str = ""
    correlation_id: str = ""
    routed_at: str = ""
    dest_path: str = ""
    attempts: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> "MessageBusEntry":
        """Load-time schema-tolerance contract (per CLAUDE.md)."""
        known = {
            k: v for k, v in data.items() if k in cls.__dataclass_fields__
        }
        return cls(**known)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MessageBusState:
    """Router state — ``id`` → :class:`MessageBusEntry`.

    Atomic save (``.tmp`` → rename); defensive load (missing → empty;
    corrupt → log + empty)."""

    path: Path
    entries: dict[str, MessageBusEntry] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "MessageBusState":
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            log.warning(
                "msgbus.state_load_failed",
                path=str(p),
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            return cls(path=p)
        entries_raw = data.get("entries") if isinstance(data, dict) else None
        entries: dict[str, MessageBusEntry] = {}
        if isinstance(entries_raw, dict):
            for key, entry_data in entries_raw.items():
                if isinstance(entry_data, dict):
                    entries[str(key)] = MessageBusEntry.from_dict(entry_data)
        return cls(path=p, entries=entries)

    def save(self) -> None:
        """Atomic write — ``.tmp`` then ``os.replace`` rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": {
                key: entry.to_dict() for key, entry in self.entries.items()
            },
        }
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp_path, self.path)


__all__ = ["MessageBusEntry", "MessageBusState"]
