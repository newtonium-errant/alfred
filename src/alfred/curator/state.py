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
    # 2026-07-29 — the most recent AGENT (``claude -p``) failure, so the curator
    # BIT can surface quota/auth outages that ``last_run`` alone can't (a stale
    # ``last_run`` can't distinguish "no work" from "every call failing"). Shape:
    # {"ts": ISO-UTC, "kind": <closed set>, "summary_tail": <bounded CLI message>}.
    # ``None`` until the first agent failure. Compared against ``last_run`` by
    # the ``agent-failure-kind`` probe: a failure NEWER than the last success is
    # an active outage; older means the pipeline recovered.
    last_agent_failure: dict[str, Any] | None = None

    def is_processed(self, filename: str) -> bool:
        return filename in self.processed

    def mark_processed(
        self,
        filename: str,
        inbox_path: str,
        files_created: list[str],
        files_modified: list[str],
        backend_used: str,
        *,
        bump_last_run: bool = True,
    ) -> None:
        """Record ``filename`` as handled.

        ``bump_last_run`` exists because this method serves two callers with
        different truth: a genuine success (bump — the default, correct for
        every ordinary caller) and the legacy ``on_failure.action: "processed"``
        escape hatch, which retires a FAILED file without a run having
        succeeded. Passing ``True`` there would let a failure self-certify as
        recovered — see the note at the ``last_run`` assignment below.

        Clearing ``failed_attempts`` is unconditional and correct in both
        cases: the file is being retired either way, so its attempt count has
        no one left to count for.
        """
        self.processed[filename] = ProcessedEntry(
            inbox_path=inbox_path,
            processed_at=datetime.now(timezone.utc).isoformat(),
            files_created=files_created,
            files_modified=files_modified,
            backend_used=backend_used,
        )
        if bump_last_run:
            # ``last_run`` is the last SUCCESSFUL process, and the curator's
            # ``agent-failure-kind`` probe reads it as exactly that: a
            # ``last_run >= failure_ts`` comparison is how it tells a recovered
            # pipeline from an active outage (``curator/health.py:385``).
            # Bumping it on a failure path makes every failure look recovered
            # on the very next probe — which is precisely the multi-day quota
            # outage the probe was built to catch.
            self.last_run = datetime.now(timezone.utc).isoformat()
        # Success clears any accumulated failure count (a transient failure that
        # later self-heals must not carry stale attempts toward the cap).
        self.failed_attempts.pop(filename, None)

    def record_agent_failure(self, kind: str, summary: str) -> None:
        """Stamp the most recent agent (``claude -p``) failure.

        Called from the daemon's ``result.success is False`` branch. Does
        NOT touch ``last_run`` — that stays the last SUCCESSFUL process, so
        the BIT probe can compare the two to tell an active outage from a
        recovered one. ``summary`` is already bounded by
        ``build_failure_summary`` (<=300 chars); we store its tail defensively.
        """
        self.last_agent_failure = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind or "other",
            "summary_tail": (summary or "")[-300:],
        }

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
            "last_agent_failure": self.last_agent_failure,
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
        # Schema-tolerant: state files written before 2026-07-29 have no
        # ``last_agent_failure`` key → default None. A malformed value
        # (non-dict) degrades to None rather than crashing the loader.
        last_agent_failure = data.get("last_agent_failure")
        if not isinstance(last_agent_failure, dict):
            last_agent_failure = None
        return cls(
            version=data.get("version", 2),
            last_run=data.get("last_run", ""),
            processed=processed,
            failed_attempts={k: int(v) for k, v in failed_attempts.items()},
            last_agent_failure=last_agent_failure,
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
