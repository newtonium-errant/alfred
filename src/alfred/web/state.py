"""State persistence for the web auth surface — the single-use nonce store.

JSON-backed, atomic writes (``.tmp`` → ``os.replace``), same contract as
``transport/state.py``. Holds the outstanding magic-link nonces so a link
is **single-use**: ``/auth/verify`` consumes (pops) the nonce on first use,
so a replayed link finds no nonce and is rejected. The store survives a
daemon restart within the link's TTL window.

Schema-tolerance: a corrupt / partial file is logged and tolerated (the
in-memory default heals on next save), and only the known top-level keys
are read — an older/newer file with extra keys loads without crashing.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()


@dataclass
class WebAuthState:
    """In-memory mirror of the web-auth state file.

    ``nonces`` maps ``nonce -> {"name": str, "exp": int}`` (``exp`` is a
    unix timestamp). One entry per outstanding magic link.
    """

    state_path: Path
    version: int = 1
    nonces: dict[str, dict[str, Any]] = field(default_factory=dict)

    # --- load/save ---------------------------------------------------------

    @classmethod
    def create(cls, state_path: str | Path) -> "WebAuthState":
        return cls(state_path=Path(state_path))

    def load(self) -> None:
        """Load state from disk if present; tolerate missing / corrupt."""
        if not self.state_path.exists():
            log.info("web.auth_state.no_existing_state", path=str(self.state_path))
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "web.auth_state.load_failed",
                path=str(self.state_path),
                error=str(exc),
            )
            return
        self.version = int(raw.get("version", 1))
        nonces_raw = raw.get("nonces", {}) or {}
        # Only keep well-shaped entries — schema-tolerant against drift.
        self.nonces = {
            str(k): dict(v)
            for k, v in nonces_raw.items()
            if isinstance(v, dict)
        }
        log.info("web.auth_state.loaded", nonces=len(self.nonces))

    def save(self) -> None:
        """Atomic save: write to ``.tmp`` then ``os.replace``."""
        data = {"version": self.version, "nonces": self.nonces}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, self.state_path)

    # --- nonce lifecycle ---------------------------------------------------

    def record_nonce(self, nonce: str, name: str, exp: int) -> None:
        """Record an outstanding magic-link nonce. Caller saves."""
        self.nonces[nonce] = {"name": name, "exp": int(exp)}

    def consume_nonce(
        self, nonce: str, now: float | None = None,
    ) -> dict[str, Any] | None:
        """Pop ``nonce`` (single-use) and return its entry IFF still valid.

        Always removes the nonce — a replayed link finds nothing on the
        second call. Returns the ``{"name", "exp"}`` entry only when the
        nonce existed AND has not expired; an absent or expired nonce
        returns ``None`` (and, if it existed, is now consumed). Caller
        MUST ``save()`` after a consume so the single-use removal is
        durable even if the rest of the request fails.
        """
        entry = self.nonces.pop(nonce, None)
        if entry is None:
            return None
        current = time.time() if now is None else now
        if current > float(entry.get("exp", 0)):
            return None
        return entry

    def prune_expired(self, now: float | None = None) -> int:
        """Drop expired nonces; return the count removed. Caller saves."""
        current = time.time() if now is None else now
        expired = [
            n for n, e in self.nonces.items()
            if current > float(e.get("exp", 0))
        ]
        for n in expired:
            del self.nonces[n]
        return len(expired)
