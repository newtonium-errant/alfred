"""State tracking for fetched emails."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


@dataclass
class State:
    version: int = 1
    seen_ids: dict[str, list[str]] = field(default_factory=dict)
    """Map of account name -> list of message IDs already fetched."""

    def is_seen(self, account: str, message_id: str) -> bool:
        return message_id in self.seen_ids.get(account, [])

    def mark_seen(self, account: str, message_id: str) -> None:
        if account not in self.seen_ids:
            self.seen_ids[account] = []
        self.seen_ids[account].append(message_id)

    def to_dict(self) -> dict:
        return {"version": self.version, "seen_ids": self.seen_ids}

    @classmethod
    def from_dict(cls, data: dict) -> State:
        return cls(
            version=data.get("version", 1),
            seen_ids=data.get("seen_ids", {}),
        )


class StateManager:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.state = State()

    def load(self) -> State:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.state = State.from_dict(data)
                log.info("mail.state.loaded", accounts=len(self.state.seen_ids))
            except (json.JSONDecodeError, KeyError) as e:
                log.warning("mail.state.load_failed", error=str(e))
                self.state = State()
        return self.state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.state.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.path)
