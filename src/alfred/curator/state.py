"""Persistent state tracking for processed inbox files."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alfred.health.agent_failure import (
    SUSTAINED_FAILURE_STREAK,
    failure_superseded_by_success,
    next_failure_record,
    read_streak,
)

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
            # pipeline from an active outage. Bumping it on a failure path
            # makes every failure look recovered on the very next probe —
            # which is precisely the multi-day quota outage the probe was
            # built to catch.
            self._log_recovery_if_sustained()
            self.last_run = datetime.now(timezone.utc).isoformat()
        # Success clears any accumulated failure count (a transient failure that
        # later self-heals must not carry stale attempts toward the cap).
        self.failed_attempts.pop(filename, None)

    def _log_recovery_if_sustained(self) -> None:
        """Emit the recovery counterpart of ``curator.agent_failure_sustained``.

        Called from :meth:`mark_processed` BEFORE ``last_run`` is bumped —
        the order matters and is the whole trick: once ``last_run`` moves past
        the failure, :func:`failure_superseded_by_success` answers True and the
        outage is no longer active, so this fires exactly once, on the FIRST
        success that ends it. No "already logged" flag to persist.

        Silent on every ordinary success (nothing was down) and on recovery
        from a SHORT streak (a hiccup that resolved is not news). Pairing this
        with the escalation line is the ILB half that matters operationally:
        without it the log shows an outage beginning and never ending, which
        reads identically to an outage that never ended.
        """
        failure = self.last_agent_failure
        if not isinstance(failure, dict):
            return
        if failure_superseded_by_success(failure.get("ts"), self.last_run):
            return  # already recovered — this is just another ordinary success
        streak = read_streak(failure)
        if streak < SUSTAINED_FAILURE_STREAK:
            return
        log.info(
            "curator.agent_failure_recovered",
            kind=failure.get("kind", "other"),
            consecutive=streak,
            since=failure.get("since") or failure.get("ts"),
        )

    def record_agent_failure(self, kind: str, summary: str) -> None:
        """Stamp the most recent agent (``claude -p``) failure, and its STREAK.

        Called from the daemon's ``result.success is False`` branch. Does
        NOT touch ``last_run`` — that stays the last SUCCESSFUL process, so
        the BIT probe can compare the two to tell an active outage from a
        recovered one. ``summary`` is already bounded by
        ``build_failure_summary`` (<=300 chars); we store its tail defensively.

        **``consecutive`` is the STRUCTURAL outage signal** (2026-08-15), and
        it is deliberately structural rather than a re-read of the error text:
        ``kind`` already carries the classifier's opinion about WHAT failed,
        while the streak carries HOW LONG it has been failing, and only the
        second distinguishes one bad run from the multi-day agent-backend
        outage that quarantines the whole intake. A quota banner on a single
        call is a hiccup; the same banner on the third call in a row is an
        outage with a daily cost.

        The reset-vs-extend rule and the ``since`` carry-forward live in
        :func:`~alfred.health.agent_failure.next_failure_record` as of
        2026-08-22, when janitor and distiller grew the same counter. They were
        inline here while curator was the only writer; three inline copies is
        how one tool's state file starts announcing a streak of 1 on the third
        day of the same outage another tool is calling sustained.

        What stays here is curator's own two facts: which timestamp counts as
        "last success" (``last_run``, kept honest by ``mark_processed``'s
        ``bump_last_run=False`` failure path) and the log event literal.
        """
        self.last_agent_failure = next_failure_record(
            prior=self.last_agent_failure,
            last_success_ts=self.last_run,
            kind=kind,
            summary=summary,
        )
        streak = self.last_agent_failure["consecutive"]
        since = self.last_agent_failure["since"]

        if streak == SUSTAINED_FAILURE_STREAK:
            # ILB, and once per outage: logged on the CROSSING (``==``, not
            # ``>=``) so the log carries the moment the backend went from
            # flaky to down, rather than one line per failure for the rest of
            # a multi-day outage. The probe re-derives ``sustained`` from the
            # streak on every sweep, so nothing depends on this line being
            # seen — it exists so the transition has a timestamp in the log an
            # operator can grep back to when asking "when did intake stop?".
            log.warning(
                "curator.agent_failure_sustained",
                kind=kind or "other",
                consecutive=streak,
                since=since,
                threshold=SUSTAINED_FAILURE_STREAK,
                summary_tail=(summary or "")[-300:],
            )

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
