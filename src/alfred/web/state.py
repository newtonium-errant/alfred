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

Per-email failed-verify lockout (#38) — ``otp_email_failures`` maps an
email HASH (``hash_otp_email``, IP-INDEPENDENT) → ``{count, window_start,
locked_until}``. It bounds total failed /auth/otp/verify guesses per email
across ALL client IPs and ALL issued codes, closing the IP-rotation bypass
of the per-code cap + the ``(client_ip, email)`` issuance limiter. Keyed on
the email ONLY — never the IP — so IP rotation cannot widen the guess
budget. Windowed + self-healing: the count decays after the window and the
lock clears after the cooldown.

Schema-tolerance: a corrupt / partial file is logged and tolerated (the
in-memory default heals on next save), and only the known top-level keys
are read — an older/newer file with extra keys loads without crashing (an
older daemon reading a newer file simply ignores ``otp_codes`` /
``otp_email_failures``; a pre-#38 file with no ``otp_email_failures`` key
loads to an empty dict).
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

# Bound the persisted ``otp_email_failures`` dict — DEFENSE-IN-DEPTH belt, no
# longer the primary control. The routes_auth caller records a per-email
# failure ONLY when a LIVE code was present (the wrong-code / exhausted paths),
# NEVER on the no-live-code path, and a live code only ever exists for an
# ALLOWLISTED email (``/auth/otp/request`` → ``web.users``). So this store is
# structurally bounded to ~(number of operators) and this cap is never
# approached in practice — the earlier flood-eviction attack (fill the store
# with distinct junk no-live-code emails to evict a real accumulating target)
# is impossible because junk emails never create an entry at all. The cap +
# eviction stays purely as a bounded-memory belt (a corrupt/enormous restored
# file, or a future caller that records more broadly, still can't grow memory
# without bound). When over the cap the entry with the OLDEST activity
# (``max(window_start, locked_until)``) is evicted; a currently-locked entry
# has ``locked_until`` in the future → freshest activity → survives eviction.
_MAX_EMAIL_FAILURE_KEYS = 4096


@dataclass
class WebAuthState:
    """In-memory mirror of the web-auth state file.

    ``nonces`` maps ``nonce -> {"name": str, "exp": int}`` (``exp`` is a
    unix timestamp). One entry per outstanding magic link.

    ``otp_codes`` maps ``email -> {"name": str, "code_hmac": str, "exp":
    int, "attempts": int}`` — one outstanding OTP per (lowercased) email;
    a new request REPLACES the old entry. HASH-ONLY: ``code_hmac`` is the
    HMAC of the code, never the raw 6 digits.

    ``otp_email_failures`` (#38) maps an email HASH (``hash_otp_email``,
    IP-independent) -> ``{"count": int, "window_start": float,
    "locked_until": float}`` — the rolling per-email failed-verify counter
    that locks further verifies once ``count`` reaches the threshold within
    the window, closing the IP-rotation brute-force bypass.
    """

    state_path: Path
    version: int = 1
    nonces: dict[str, dict[str, Any]] = field(default_factory=dict)
    otp_codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    otp_email_failures: dict[str, dict[str, Any]] = field(default_factory=dict)

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
        # #38 — schema-tolerant: a pre-#38 file has no ``otp_email_failures``
        # key, so this coalesces to ``{}`` (no lockout state carried forward).
        ef_raw = raw.get("otp_email_failures", {}) or {}
        self.otp_email_failures = {
            str(k): dict(v)
            for k, v in ef_raw.items()
            if isinstance(v, dict)
        }
        log.info(
            "web.auth_state.loaded",
            nonces=len(self.nonces),
            otp_codes=len(self.otp_codes),
            otp_email_failures=len(self.otp_email_failures),
        )

    def save(self) -> None:
        """Atomic save: write to ``.tmp`` then ``os.replace``."""
        data = {
            "version": self.version,
            "nonces": self.nonces,
            "otp_codes": self.otp_codes,
            "otp_email_failures": self.otp_email_failures,
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

    # --- Per-email failed-verify lockout (#38) -----------------------------
    # An IP-INDEPENDENT guess ceiling. ``email_hash`` is ``hash_otp_email``
    # (keyed on the normalized email ONLY, never client_ip) so an attacker
    # rotating client_ip cannot widen the budget. Layered ON TOP of the #23
    # per-code cap / issuance limiter, not replacing them.

    def otp_email_locked(self, email_hash: str, now: float | None = None) -> bool:
        """True while ``email_hash`` is inside its lockout cooldown.

        A locked email must have its /auth/otp/verify rejected with the SAME
        uniform 401 as an ordinary wrong code (no locked-vs-not oracle) — the
        caller gates on this BEFORE peeking any live code, so even a correct
        code is rejected while locked. Self-healing: once ``now`` passes
        ``locked_until`` this returns ``False`` and the counter is reset on the
        next failure (see :meth:`record_otp_email_failure`)."""
        entry = self.otp_email_failures.get(email_hash)
        if entry is None:
            return False
        current = time.time() if now is None else now
        return current < float(entry.get("locked_until", 0))

    def record_otp_email_failure(
        self,
        email_hash: str,
        *,
        threshold: int,
        window_s: float,
        cooldown_s: float,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Count ONE failed verify against ``email_hash``. Returns the updated
        entry.

        #38 fix — the caller (``_handle_otp_verify``) invokes this ONLY when a
        LIVE outstanding code was present (the wrong-code / exhausted-attempts
        paths), NEVER on the no-live-code path. A live code only ever exists for
        an ALLOWLISTED email, so entries here are bounded to ~(operators) and
        the store is un-floodable — see :data:`_MAX_EMAIL_FAILURE_KEYS`. (The
        method itself is generic; the un-floodable invariant lives at the call
        site, so do not re-add a no-live-code caller without re-opening it.)

        Windowed + self-healing. The count accumulates within a rolling
        ``window_s``; the counter RESETS to a fresh window when (a) there is
        no entry, (b) the window has elapsed since ``window_start`` (a legit
        user's occasional fumble decays and never accumulates to a lockout),
        or (c) a prior lock has expired (``now >= locked_until`` — the
        post-cooldown fresh budget, so a single post-cooldown failure can't
        instantly re-lock). When ``count`` reaches ``threshold`` a
        ``locked_until = now + cooldown_s`` is armed. Caller saves.

        Keyed on ``email_hash`` ONLY — never client_ip — so IP rotation does
        not widen the budget (that is the whole point of #38). Bounded: the
        store is capped at :data:`_MAX_EMAIL_FAILURE_KEYS` (a defense-in-depth
        memory belt — the store is un-floodable at the call site), evicting the
        oldest-activity entry."""
        current = time.time() if now is None else now
        entry = self.otp_email_failures.get(email_hash)
        window_start = float(entry.get("window_start", 0)) if entry else 0.0
        locked_until = float(entry.get("locked_until", 0)) if entry else 0.0
        reset = (
            entry is None
            or (current - window_start >= window_s)
            or (locked_until > 0 and current >= locked_until)
        )
        # When ``reset`` is False, ``entry`` is guaranteed non-None (the first
        # ``reset`` clause is ``entry is None``), so the accumulate path below
        # is safe.
        if reset:
            entry = {"count": 0, "window_start": current, "locked_until": 0}
        assert entry is not None  # narrows for the type-checker; see above
        entry["count"] = int(entry.get("count", 0)) + 1
        if entry["count"] >= threshold:
            entry["locked_until"] = current + cooldown_s
        self.otp_email_failures[email_hash] = entry
        self._bound_email_failures()
        return entry

    def clear_otp_email_failures(self, email_hash: str) -> None:
        """Drop ``email_hash``'s failure counter — called on a SUCCESSFUL
        verify (a correct code proves the requester holds it, so their guess
        budget resets). Caller saves."""
        self.otp_email_failures.pop(email_hash, None)

    def _bound_email_failures(self) -> None:
        """Evict the oldest-activity entry when over the cap. Activity is
        ``max(window_start, locked_until)`` so a live lock (``locked_until`` in
        the future) is the freshest and survives a junk-email flood."""
        while len(self.otp_email_failures) > _MAX_EMAIL_FAILURE_KEYS:
            oldest = min(
                self.otp_email_failures,
                key=lambda k: max(
                    float(self.otp_email_failures[k].get("window_start", 0)),
                    float(self.otp_email_failures[k].get("locked_until", 0)),
                ),
            )
            self.otp_email_failures.pop(oldest, None)
