"""Persistent state tracking for processed inbox files."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import get_logger

log = get_logger(__name__)


@dataclass
class ProcessedEntry:
    inbox_path: str
    processed_at: str
    files_created: list[str] = field(default_factory=list)
    files_modified: list[str] = field(default_factory=list)
    backend_used: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "inbox_path": self.inbox_path,
            "processed_at": self.processed_at,
            "files_created": self.files_created,
            "files_modified": self.files_modified,
            "backend_used": self.backend_used,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProcessedEntry:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class State:
    version: int = 2
    last_run: str = ""
    processed: dict[str, ProcessedEntry] = field(default_factory=dict)
    # #34 — per-file failed-attempt counter for the retry-in-place-then-quarantine
    # path. Keyed by inbox filename; bumped on each processing failure, cleared on
    # success (or quarantine). Lives in STATE, never in the arrival file's
    # frontmatter — a frontmatter write would bump the file's mtime and mask
    # Monitor A's delivery-staleness signal (#31).
    failed_attempts: dict[str, int] = field(default_factory=dict)

    def is_processed(self, filename: str) -> bool:
        return filename in self.processed

    def mark_processed(
        self,
        filename: str,
        inbox_path: str,
        files_created: list[str],
        files_modified: list[str],
        backend_used: str,
    ) -> None:
        self.processed[filename] = ProcessedEntry(
            inbox_path=inbox_path,
            processed_at=datetime.now(timezone.utc).isoformat(),
            files_created=files_created,
            files_modified=files_modified,
            backend_used=backend_used,
        )
        self.last_run = datetime.now(timezone.utc).isoformat()
        # Success clears any accumulated failure count (a transient failure that
        # later self-heals must not carry stale attempts toward the cap).
        self.failed_attempts.pop(filename, None)

    def failed_count(self, filename: str) -> int:
        return self.failed_attempts.get(filename, 0)

    def bump_failed(self, filename: str) -> int:
        """Increment and return the file's failure count."""
        self.failed_attempts[filename] = self.failed_attempts.get(filename, 0) + 1
        return self.failed_attempts[filename]

    def clear_failed(self, filename: str) -> None:
        self.failed_attempts.pop(filename, None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "last_run": self.last_run,
            "processed": {k: v.to_dict() for k, v in self.processed.items()},
            "failed_attempts": dict(self.failed_attempts),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> State:
        processed = {}
        for k, v in data.get("processed", {}).items():
            processed[k] = ProcessedEntry.from_dict(v)
        # Schema-tolerant: state files written before #34 have no
        # ``failed_attempts`` key → default to empty.
        failed_attempts = data.get("failed_attempts", {})
        if not isinstance(failed_attempts, dict):
            failed_attempts = {}
        return cls(
            version=data.get("version", 2),
            last_run=data.get("last_run", ""),
            processed=processed,
            failed_attempts={k: int(v) for k, v in failed_attempts.items()},
        )


class StateManager:
    """Load/save state from a JSON file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.state = State()

    def load(self) -> State:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.state = State.from_dict(data)
                log.info("state.loaded", entries=len(self.state.processed))
            except (json.JSONDecodeError, KeyError) as e:
                log.warning("state.load_failed", error=str(e))
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
        log.debug("state.saved", path=str(self.path))
