"""State persistence for the web auth surface — nonce + OTP-code stores.

JSON-backed, atomic writes (``.tmp`` → ``os.replace``), same contract as
``transport/state.py``. Holds the outstanding magic-link nonces so a link
is **single-use**: ``/auth/verify`` consumes (pops) the nonce on first use,
so a replayed link finds no nonce and is rejected. The store survives a
daemon restart within the link's TTL window.

OTP codes (parity #23 — iOS PWA re-auth) live alongside the nonces, one
outstanding code per (lowercased) email. HASH-ONLY: the entry carries the
HMAC of the code (``code_hmac``), NEVER the raw 6-digit code — the raw
code exists only in the email and in the request that verifies it. Same
single-use discipline as the nonce (pop on success), plus a per-code
``attempts`` counter (the cap-th failure burns the code) and a TTL burn.

Schema-tolerance: a corrupt / partial file is logged and tolerated (the
in-memory default heals on next save), and only the known top-level keys
are read — an older/newer file with extra keys loads without crashing
(an older daemon reading a newer file simply ignores ``otp_codes``).
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

    ``otp_codes`` maps ``email -> {"name": str, "code_hmac": str, "exp":
    int, "attempts": int}`` — one outstanding OTP per (lowercased) email;
    a new request REPLACES the old entry. HASH-ONLY: ``code_hmac`` is the
    HMAC of the code, never the raw 6 digits.
    """

    state_path: Path
    version: int = 1
    nonces: dict[str, dict[str, Any]] = field(default_factory=dict)
    otp_codes: dict[str, dict[str, Any]] = field(default_factory=dict)

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
        otp_raw = raw.get("otp_codes", {}) or {}
        self.otp_codes = {
            str(k): dict(v)
            for k, v in otp_raw.items()
            if isinstance(v, dict)
        }
        log.info(
            "web.auth_state.loaded",
            nonces=len(self.nonces),
            otp_codes=len(self.otp_codes),
        )

    def save(self) -> None:
        """Atomic save: write to ``.tmp`` then ``os.replace``."""
        data = {
            "version": self.version,
            "nonces": self.nonces,
            "otp_codes": self.otp_codes,
        }
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
        """Drop expired nonces + OTP codes; return the count removed.
        Caller saves."""
        current = time.time() if now is None else now
        expired = [
            n for n, e in self.nonces.items()
            if current > float(e.get("exp", 0))
        ]
        for n in expired:
            del self.nonces[n]
        expired_otp = [
            k for k, e in self.otp_codes.items()
            if current > float(e.get("exp", 0))
        ]
        for k in expired_otp:
            del self.otp_codes[k]
        return len(expired) + len(expired_otp)

    # --- OTP-code lifecycle (parity #23) -----------------------------------
    # Mirrors the nonce lifecycle: record on issue, TTL burn, single-use pop
    # on success — plus a per-code attempts counter for the brute-force cap.
    # HASH-ONLY invariant: callers pass ``code_hmac`` (never the raw code);
    # nothing in this store ever sees or persists the raw 6 digits.

    def record_otp(
        self, email: str, name: str, code_hmac: str, exp: int,
    ) -> None:
        """Record the outstanding OTP for ``email``, REPLACING any prior
        entry (a fresh request invalidates the previous code — only one
        code per email is ever live). Caller saves."""
        self.otp_codes[email] = {
            "name": name,
            "code_hmac": code_hmac,
            "exp": int(exp),
            "attempts": 0,
        }

    def peek_otp(
        self, email: str, now: float | None = None,
    ) -> dict[str, Any] | None:
        """Return the live OTP entry for ``email``, or ``None``.

        An EXPIRED entry is burned (popped) on sight and reported as
        ``None`` — TTL enforcement happens here, server-side, so an old
        code can never be verified. Caller MUST ``save()`` after any call
        that may have burned (the removal must be durable)."""
        entry = self.otp_codes.get(email)
        if entry is None:
            return None
        current = time.time() if now is None else now
        if current > float(entry.get("exp", 0)):
            self.otp_codes.pop(email, None)
            return None
        return entry

    def record_otp_failure(self, email: str, max_attempts: int) -> int:
        """Count a failed verify against ``email``'s outstanding code.

        Returns the new attempt count. When the count reaches
        ``max_attempts`` the code is BURNED (popped) — the brute-force
        cap: a later attempt, even with the correct code, finds nothing.
        Caller saves."""
        entry = self.otp_codes.get(email)
        if entry is None:
            return 0
        attempts = int(entry.get("attempts", 0)) + 1
        entry["attempts"] = attempts
        if attempts >= max_attempts:
            self.otp_codes.pop(email, None)
        return attempts

    def consume_otp(self, email: str) -> dict[str, Any] | None:
        """Pop ``email``'s OTP entry (single-use burn on success).

        Caller MUST ``save()`` immediately so the burn is durable even if
        a later step of the request fails — a verified code can never be
        replayed."""
        return self.otp_codes.pop(email, None)
