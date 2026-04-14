"""State tracking for morning brief generation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

MAX_HISTORY = 30


@dataclass
class BriefRun:
    date: str
    generated_at: str
    vault_path: str
    sections: list[str]
    success: bool

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "generated_at": self.generated_at,
            "vault_path": self.vault_path,
            "sections": self.sections,
            "success": self.success,
        }

    @classmethod
    def from_dict(cls, data: dict) -> BriefRun:
        return cls(
            date=data.get("date", ""),
            generated_at=data.get("generated_at", ""),
            vault_path=data.get("vault_path", ""),
            sections=data.get("sections", []),
            success=data.get("success", False),
        )


@dataclass
class State:
    version: int = 1
    last_run: str = ""
    runs: list[BriefRun] = field(default_factory=list)

    def add_run(self, run: BriefRun) -> None:
        self.last_run = run.generated_at
        self.runs.append(run)
        if len(self.runs) > MAX_HISTORY:
            self.runs = self.runs[-MAX_HISTORY:]

    def has_brief_for_date(self, date: str) -> bool:
        return any(r.date == date and r.success for r in self.runs)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "last_run": self.last_run,
            "runs": [r.to_dict() for r in self.runs],
        }

    @classmethod
    def from_dict(cls, data: dict) -> State:
        return cls(
            version=data.get("version", 1),
            last_run=data.get("last_run", ""),
            runs=[BriefRun.from_dict(r) for r in data.get("runs", [])],
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
                log.info("brief.state.loaded", runs=len(self.state.runs))
            except (json.JSONDecodeError, KeyError) as e:
                log.warning("brief.state.load_failed", error=str(e))
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
