"""State persistence for the outbound-push transport.

JSON-backed, atomic writes (``.tmp`` → ``os.replace``). Three lists:

- ``pending_queue``   — scheduled ``/outbound/send`` entries whose
  ``scheduled_at`` is in the future. The scheduler drains due entries
  on each tick.
- ``send_log``        — rolling log of recent sends, keyed by
  ``dedupe_key``. Used for the 24h idempotency window so a restarted
  daemon does not double-send on its first tick.
- ``dead_letter``     — terminally-failed entries (scheduled reminders
  that aged out past the stale window, send failures that exhausted
  retries). The CLI exposes list / retry / drop commands.

The shapes are kept deliberately loose — each entry is a plain dict
with the fields the caller supplies. That lets Stage 3.5 add per-peer
routing metadata without a schema migration. Callers that require
specific fields validate on the way in; the state layer only enforces
the container shape.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()


# The 24-hour idempotency window — sends with the same ``dedupe_key``
# within this span are served from state instead of re-dispatched. Used
# by the ``/outbound/send`` handler and tested against the timestamps
# written into ``send_log``.
_DEDUPE_WINDOW = timedelta(hours=24)


@dataclass
class TransportState:
    """In-memory mirror of the transport state file."""

    state_path: Path
    version: int = 1
    pending_queue: list[dict[str, Any]] = field(default_factory=list)
    send_log: list[dict[str, Any]] = field(default_factory=list)
    dead_letter: list[dict[str, Any]] = field(default_factory=list)

    # --- load/save ---------------------------------------------------------

    @classmethod
    def create(cls, state_path: str | Path) -> "TransportState":
        """Factory that matches other tools' ``StateManager`` style."""
        return cls(state_path=Path(state_path))

    def load(self) -> None:
        """Load state from disk if it exists.

        A missing file is fine (fresh daemon). A corrupt file is
        tolerated: we log a warning and keep the in-memory default so
        the next ``save()`` heals it. Same contract every other tool
        follows.
        """
        if not self.state_path.exists():
            log.info(
                "transport.state.no_existing_state",
                path=str(self.state_path),
            )
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "transport.state.load_failed",
                path=str(self.state_path),
                error=str(exc),
            )
            return
        self.version = int(raw.get("version", 1))
        self.pending_queue = list(raw.get("pending_queue", []) or [])
        self.send_log = list(raw.get("send_log", []) or [])
        self.dead_letter = list(raw.get("dead_letter", []) or [])
        log.info(
            "transport.state.loaded",
            pending=len(self.pending_queue),
            dead_letter=len(self.dead_letter),
        )

    def save(self) -> None:
        """Atomic save: write to ``.tmp`` then ``os.replace``."""
        data = {
            "version": self.version,
            "pending_queue": self.pending_queue,
            "send_log": self.send_log,
            "dead_letter": self.dead_letter,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, self.state_path)

    # --- pending queue -----------------------------------------------------

    def enqueue(self, entry: dict[str, Any]) -> None:
        """Append ``entry`` to ``pending_queue``. Caller owns the shape."""
        self.pending_queue.append(dict(entry))

    def pop_due(self, now: datetime) -> list[dict[str, Any]]:
        """Return (and remove) pending entries whose ``scheduled_at`` <= now.

        Entries without a ``scheduled_at`` are treated as "send now" —
        if something is parked in ``pending_queue`` with no schedule, it
        means the caller wanted the server to buffer and deliver on the
        next tick. Entries with an unparseable timestamp stay in the
        queue (fail-safe: better to wedge one entry than silently drop
        it).
        """
        due: list[dict[str, Any]] = []
        kept: list[dict[str, Any]] = []
        for entry in self.pending_queue:
            ts = entry.get("scheduled_at")
            if ts is None:
                due.append(entry)
                continue
            try:
                scheduled = _parse_iso(ts)
            except ValueError:
                kept.append(entry)
                continue
            if scheduled <= now:
                due.append(entry)
            else:
                kept.append(entry)
        self.pending_queue = kept
        return due

    # --- send log + idempotency -------------------------------------------

    def record_send(self, entry: dict[str, Any]) -> None:
        """Record a successful send. Used for the dedupe window.

        Entries without a ``dedupe_key`` are still recorded (for audit
        history) but they never match an incoming dedupe lookup.
        """
        self.send_log.append(dict(entry))
        # Cap the log length so it doesn't grow unboundedly. The dedupe
        # window is 24h — anything older is evictable. Keep a trailing
        # buffer (4x window) to cover clock skew and long-running
        # daemons that haven't saved in a while.
        if len(self.send_log) > 2048:
            self.send_log = self.send_log[-2048:]

    def find_recent_send(
        self,
        dedupe_key: str,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Return the most recent send with this ``dedupe_key``, if in window.

        Returns ``None`` when no match exists, the match is older than
        the 24h dedupe window, or ``dedupe_key`` is empty. The lookup
        walks the log tail-first for cheap recency.
        """
        if not dedupe_key:
            return None
        current = now or datetime.now(timezone.utc)
        for entry in reversed(self.send_log):
            if entry.get("dedupe_key") != dedupe_key:
                continue
            sent_at = entry.get("sent_at")
            if not sent_at:
                return entry  # fail-open on missing timestamp
            try:
                sent_dt = _parse_iso(sent_at)
            except ValueError:
                return entry
            if current - sent_dt <= _DEDUPE_WINDOW:
                return entry
            return None  # most-recent match is already outside the window
        return None

    # --- dead letter -------------------------------------------------------

    def append_dead_letter(
        self,
        entry: dict[str, Any],
        reason: str,
    ) -> None:
        """Park a terminally-failed entry in ``dead_letter``.

        The stored entry carries its original fields plus
        ``dead_letter_reason`` and ``dead_lettered_at`` so operators
        can see what happened without cross-referencing logs.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        stored = dict(entry)
        stored["dead_letter_reason"] = reason
        stored["dead_lettered_at"] = now_iso
        self.dead_letter.append(stored)


def _parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 timestamp, tolerating the ``Z`` shorthand.

    Returns a timezone-aware datetime. Naive timestamps are assumed to
    be UTC — the transport's contract is that every caller emits
    UTC-aware timestamps, but we fall back defensively rather than
    raise.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
