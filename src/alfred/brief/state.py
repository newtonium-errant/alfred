"""State tracking for morning brief generation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import structlog

from .utils import SectionReadStatus, safe_read_section_file

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
        # Schema-tolerance filter per CLAUDE.md "State persistence —
        # load() schema-tolerance contract": ignore unknown keys so
        # a state file written by a newer/older version of the tool
        # doesn't crash the loader.
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(
            date=known.get("date", ""),
            generated_at=known.get("generated_at", ""),
            vault_path=known.get("vault_path", ""),
            sections=known.get("sections", []),
            success=known.get("success", False),
        )


@dataclass
class State:
    version: int = 1
    last_run: str = ""
    runs: list[BriefRun] = field(default_factory=list)
    # ``last_error`` is parallel state at the State-level (not a per-run
    # thing). Shape is ``{"ts": iso_string, "message": str}`` when
    # populated; None when no error since last success. Added 2026-05-14
    # so the BIT ``last-successful-brief`` probe can surface WHY the
    # daemon stalled, not just that it did.
    last_error: dict | None = None

    def add_run(self, run: BriefRun) -> None:
        self.last_run = run.generated_at
        self.runs.append(run)
        if len(self.runs) > MAX_HISTORY:
            self.runs = self.runs[-MAX_HISTORY:]
        # Clear-on-success: a successful run wipes the last_error
        # field so the probe doesn't trail stale failure context after
        # the daemon recovers. Failed runs (success=False) leave
        # last_error alone — the explicit ``record_error`` call from
        # the daemon's except-block is what owns the failure-side write.
        if run.success:
            self.last_error = None

    def has_brief_for_date(self, date: str) -> bool:
        return any(r.date == date and r.success for r in self.runs)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "last_run": self.last_run,
            "runs": [r.to_dict() for r in self.runs],
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> State:
        # Schema-tolerance filter per CLAUDE.md "State persistence —
        # load() schema-tolerance contract". Surfaces forward/backward
        # compat: an older state file without ``last_error`` loads
        # fine; a newer state file with extra fields is tolerated.
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(
            version=known.get("version", 1),
            last_run=known.get("last_run", ""),
            runs=[BriefRun.from_dict(r) for r in known.get("runs", [])],
            last_error=known.get("last_error", None),
        )


class StateManager:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.state = State()

    def load(self) -> State:
        if self.path.exists():
            # Defensive read via the shared helper — the old
            # ``(json.JSONDecodeError, KeyError)`` catch missed BOTH
            # UnicodeDecodeError (a non-UTF-8 file; subclasses ValueError, not
            # OSError) AND OSError (permission-denied / is-a-dir / I/O). load()
            # is called UNGUARDED at daemon.py inside run_daemon, so an escaping
            # read exception crash-loops the brief daemon at startup (the
            # orchestrator retries 5× then gives up). Degrade to a fresh
            # State() + warning instead. Worst blast radius of the #25 class.
            read = safe_read_section_file(self.path)
            if read.status is not SectionReadStatus.OK:
                log.warning(
                    "brief.state.load_failed",
                    error=read.detail,
                    error_type=read.error_type,
                )
                self.state = State()
                return self.state
            try:
                data = json.loads(read.text)
                self.state = State.from_dict(data)
                log.info("brief.state.loaded", runs=len(self.state.runs))
            except (json.JSONDecodeError, KeyError) as e:
                # Clean read but bad JSON / unexpected shape → same fresh-State
                # degrade the pre-migration code produced (semantics preserved).
                log.warning(
                    "brief.state.load_failed",
                    error=str(e),
                    error_type=e.__class__.__name__,
                )
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

    def record_error(self, message: str) -> None:
        """Capture a daemon-level failure into ``state.last_error`` and
        persist.

        Called from the daemon's outer ``except Exception:`` so the BIT
        ``last-successful-brief`` probe can surface the failure cause
        (e.g. ``KeyError: 'visib'``) on the BIT line itself rather than
        forcing the operator to grep ``data/brief.log``.

        Does NOT crash the daemon if persistence itself fails — a
        broken state file shouldn't compound a broken brief. Logs the
        secondary failure and returns.
        """
        self.state.last_error = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "message": message,
        }
        try:
            self.save()
        except OSError as e:
            log.warning("brief.state.record_error_save_failed", error=str(e))
