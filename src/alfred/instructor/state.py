"""State persistence for the instructor daemon.

Minimal shape — the vault itself is the source of truth for
``alfred_instructions`` / ``alfred_instructions_last``. The state file
is just bookkeeping:

- ``file_hashes``: ``{rel_path: sha256}`` — content-hash gate. Skip a
  record on the poll pass if the hash matches what we saw last time.
- ``retry_counts``: ``{rel_path: int}`` — retry bookkeeping across
  daemon restarts. Incremented on executor failure, cleared on success
  or when the file's hash changes (indicating the operator edited the
  directive).
- ``last_run_ts``: ISO-8601 timestamp of the last completed poll pass.
- ``last_error``: diagnostic field for daemon-loop failures captured by
  the outer ``except Exception:`` at daemon.py:331. Shape is
  ``{"ts": iso_string, "message": str}`` when populated; None when no
  error since the last successful poll. Surfaced via the BIT
  ``last-successful-poll`` probe so operators see WHY the poll
  stalled, not just that it did. Cleared by :meth:`stamp_run` on
  every successful completion — the poll loop reaches stamp_run at
  the end of each tick that didn't raise, so the recovery semantic
  is per-tick (unlike distiller's per-deep-extraction clear, because
  instructor only has one tick cadence). Added 2026-05-14 — mirrors
  the brief / janitor / distiller / daily_sync last_error patterns
  shipped earlier in the cross-daemon swallow audit arc.

Atomic writes use the same ``.tmp → os.replace`` contract every other
tool follows — see ``tests/state/test_state_roundtrip.py`` for the
round-trip test scaffold.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import structlog

log = structlog.get_logger()


class InstructorState:
    """JSON-backed state for the instructor daemon."""

    def __init__(self, state_path: str | Path) -> None:
        self.state_path = Path(state_path)
        self.version: int = 1
        self.file_hashes: dict[str, str] = {}
        self.retry_counts: dict[str, int] = {}
        self.last_run_ts: str | None = None
        self.last_error: dict | None = None

    def load(self) -> None:
        """Load state from disk if it exists.

        A missing file is fine — the daemon just starts with empty
        dicts. A corrupt file is tolerated: we log a warning and fall
        back to empty state so the next save heals it (same policy as
        every other tool's state manager).
        """
        if not self.state_path.exists():
            log.info("instructor.state.no_existing_state", path=str(self.state_path))
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "instructor.state.load_failed",
                path=str(self.state_path),
                error=str(exc),
            )
            return
        self.version = int(raw.get("version", 1))
        self.file_hashes = dict(raw.get("file_hashes", {}) or {})
        self.retry_counts = {
            k: int(v) for k, v in (raw.get("retry_counts", {}) or {}).items()
        }
        self.last_run_ts = raw.get("last_run_ts") or None
        # Schema tolerance per CLAUDE.md "load-time schema-tolerance
        # contract": older state files (pre-2026-05-14) won't have
        # last_error at all → default None. A corrupt non-dict value
        # also degrades to None so a malformed state file can't poison
        # the probe-side _read_last_error helper.
        last_error_raw = raw.get("last_error")
        self.last_error = last_error_raw if isinstance(last_error_raw, dict) else None
        log.info(
            "instructor.state.loaded",
            tracked_files=len(self.file_hashes),
            pending_retries=len(self.retry_counts),
        )

    def save(self) -> None:
        """Atomic save: write to ``.tmp`` then ``os.replace``."""
        data = {
            "version": self.version,
            "file_hashes": self.file_hashes,
            "retry_counts": self.retry_counts,
            "last_run_ts": self.last_run_ts,
            "last_error": self.last_error,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, self.state_path)

    # --- Hash gate helpers ---

    def hash_unchanged(self, rel_path: str, current_hash: str) -> bool:
        """Return True if the file's hash matches what we saw last time."""
        return self.file_hashes.get(rel_path) == current_hash

    def record_hash(self, rel_path: str, current_hash: str) -> None:
        """Store the current content hash for ``rel_path``."""
        self.file_hashes[rel_path] = current_hash

    def forget_hash(self, rel_path: str) -> None:
        """Drop the hash entry (e.g. when the file is deleted)."""
        self.file_hashes.pop(rel_path, None)

    # --- Retry bookkeeping ---

    def get_retry_count(self, rel_path: str) -> int:
        """Return the current retry count for ``rel_path`` (0 if unseen)."""
        return int(self.retry_counts.get(rel_path, 0))

    def bump_retry(self, rel_path: str) -> int:
        """Increment the retry counter and return the new value."""
        new_count = self.get_retry_count(rel_path) + 1
        self.retry_counts[rel_path] = new_count
        return new_count

    def clear_retry(self, rel_path: str) -> None:
        """Reset the retry counter (on success or after a hash change)."""
        self.retry_counts.pop(rel_path, None)

    # --- Run timestamp ---

    def stamp_run(self) -> None:
        """Stamp ``last_run_ts`` with the current UTC time.

        Also clears ``last_error`` — reaching this call site means the
        poll loop completed without raising (detection + execution +
        re-hash-seal all returned), so the recovery semantic treats
        the tick as successful and wipes any stale failure context
        the probe would otherwise trail across the BIT line. Mirrors
        the brief.State / janitor.State / daily_sync clear-on-success
        patterns from 2026-05-14. Per-tick clear is appropriate here
        because instructor only has one tick cadence (unlike
        distiller's deep-vs-light split where the clear lives only on
        the deep path).
        """
        self.last_run_ts = datetime.now(timezone.utc).isoformat()
        self.last_error = None

    # --- Error capture ---

    def record_error(self, message: str) -> None:
        """Capture a daemon-level failure into ``state.last_error`` and persist.

        Called from the daemon's outer ``except Exception:`` at
        daemon.py:331 so the BIT ``last-successful-poll`` probe can
        surface the failure cause (e.g. ``KeyError: 'foo'``) on the
        BIT line rather than forcing the operator to grep
        ``data/instructor.log``.

        Does NOT crash the daemon if persistence itself fails — a
        broken state file shouldn't compound a broken poll. Logs the
        secondary failure and returns. Mirrors the brief / janitor /
        distiller record_error patterns from 2026-05-14.
        """
        self.last_error = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "message": message,
        }
        try:
            self.save()
        except OSError as e:
            log.warning("instructor.state.record_error_save_failed", error=str(e))
