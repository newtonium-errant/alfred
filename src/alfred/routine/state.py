"""Routine daemon state — per-day write history.

Schema-tolerance contract (per project CLAUDE.md "State persistence —
load() schema-tolerance contract"): ``from_dict`` filters incoming
keys against the dataclass's ``__dataclass_fields__`` so a state file
written by an older or newer tool version doesn't crash the loader.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


@dataclass
class RoutineRun:
    """One aggregator run's summary."""

    date: str
    generated_at: str
    vault_path: str
    routines_contributing: list[str] = field(default_factory=list)
    item_count: int = 0
    critical_pending: int = 0

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "generated_at": self.generated_at,
            "vault_path": self.vault_path,
            "routines_contributing": list(self.routines_contributing),
            "item_count": self.item_count,
            "critical_pending": self.critical_pending,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RoutineRun":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        # Coerce list-typed fields back from possible JSON null.
        if "routines_contributing" in known and not isinstance(known["routines_contributing"], list):
            known["routines_contributing"] = []
        return cls(**known)


@dataclass
class State:
    version: int = 1
    last_run: str = ""
    runs: list[RoutineRun] = field(default_factory=list)

    def add_run(self, run: RoutineRun, max_history: int = 30) -> None:
        self.last_run = run.generated_at
        self.runs.append(run)
        if len(self.runs) > max_history:
            self.runs = self.runs[-max_history:]

    def latest(self) -> RoutineRun | None:
        return self.runs[-1] if self.runs else None

    def has_run_for_date(self, iso_date: str) -> bool:
        return any(r.date == iso_date for r in self.runs)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "last_run": self.last_run,
            "runs": [r.to_dict() for r in self.runs],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "State":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        runs_raw = known.get("runs", [])
        runs = [RoutineRun.from_dict(r) for r in runs_raw if isinstance(r, dict)]
        known["runs"] = runs
        return cls(**known)


class StateManager:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.state = State()

    def load(self) -> State:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.state = State.from_dict(data)
                log.info("routine.state.loaded", runs=len(self.state.runs))
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                log.warning("routine.state.load_failed", error=str(exc))
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
