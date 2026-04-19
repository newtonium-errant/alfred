"""BIT state — retention + history tracking.

Plan Part 11 Q5: keep all BIT records (matches brief pattern — no
pruning). State file tracks the last N runs for ``alfred bit history``.
The retention cap only applies to the in-memory run list, not to the
vault records themselves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


@dataclass
class BITRun:
    """One BIT run's summary record."""

    date: str
    generated_at: str
    vault_path: str
    overall_status: str  # "ok" | "warn" | "fail" | "skip"
    mode: str
    tool_counts: dict[str, int] = field(default_factory=dict)
    # tool_counts -> e.g. {"ok": 5, "warn": 1, "fail": 0, "skip": 1}

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "generated_at": self.generated_at,
            "vault_path": self.vault_path,
            "overall_status": self.overall_status,
            "mode": self.mode,
            "tool_counts": dict(self.tool_counts),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BITRun":
        return cls(
            date=data.get("date", ""),
            generated_at=data.get("generated_at", ""),
            vault_path=data.get("vault_path", ""),
            overall_status=data.get("overall_status", ""),
            mode=data.get("mode", ""),
            tool_counts=dict(data.get("tool_counts", {})),
        )


@dataclass
class State:
    version: int = 1
    last_run: str = ""
    runs: list[BITRun] = field(default_factory=list)

    def add_run(self, run: BITRun, max_history: int = 30) -> None:
        self.last_run = run.generated_at
        self.runs.append(run)
        if len(self.runs) > max_history:
            self.runs = self.runs[-max_history:]

    def latest(self) -> BITRun | None:
        return self.runs[-1] if self.runs else None

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "last_run": self.last_run,
            "runs": [r.to_dict() for r in self.runs],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "State":
        return cls(
            version=data.get("version", 1),
            last_run=data.get("last_run", ""),
            runs=[BITRun.from_dict(r) for r in data.get("runs", [])],
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
                log.info("bit.state.loaded", runs=len(self.state.runs))
            except (json.JSONDecodeError, KeyError) as exc:
                log.warning("bit.state.load_failed", error=str(exc))
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
