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
        """Stamp ``last_run_ts`` with the current UTC time."""
        self.last_run_ts = datetime.now(timezone.utc).isoformat()
