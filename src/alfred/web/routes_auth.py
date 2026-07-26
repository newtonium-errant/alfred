"""Web auth routes — magic-link login (Sub-arc B) + OTP re-auth (#23).

    POST /auth/login       { email }        → { status: "sent" }   (uniform)
    POST /auth/verify      { token }        → { session_token, name, role, exp }
    POST /auth/otp/request { email }        → { status: "sent" }   (uniform)
    POST /auth/otp/verify  { email, code }  → { session_token, name, role, exp }

Login flow: ``/auth/login`` looks the email up in the ``web.users``
allowlist, mints a single-use magic token (+ nonce recorded server-side),
and emails the link via Resend. The response is uniform ``{status:"sent"}``
whether or not the email matched (no user enumeration) — the server logs
the miss. Missing send prerequisites (Resend creds / base_url unresolved)
return 503 and never crash the route.

``/auth/verify`` (called by the BFF from the callback) verifies the magic
token's signature + type + expiry, CONSUMES the nonce (single-use; a
replayed link finds no nonce → 401), re-resolves the user against the
current allowlist, and mints the session token the BFF stores + relays as
``X-Alfred-Session``.

OTP re-auth (parity #23 — the iOS PWA cookie-jar fix): a magic link tapped
from iOS Mail opens in Safari, a DIFFERENT browser context from the
installed PWA's WebView cookie jar — the session lands in the wrong jar
and the PWA stays logged out. ``/auth/otp/request`` emails a 6-digit
passcode the user TYPES INTO the already-open PWA; ``/auth/otp/verify``
exchanges it for a session token minted by the SAME
``make_session_token`` path as ``/auth/verify`` (byte-shape-identical
claims/signer/exp), so the BFF sets the cookie on the PWA's own origin
response. Keep-both: the magic-link path above is unchanged (desktop).

OTP controls, all server-side: DEFAULT-OFF (``web.auth.otp_enabled``;
routes answer 404 until flipped), HASH-ONLY storage (HMAC of the code —
the raw 6 digits are emailed and never persisted or logged), TTL burn
(``otp_ttl_minutes``), per-code attempt cap (``otp_max_attempts``; the
cap-th failure burns the code), single-use burn on success, timing-safe
compare, per-email issuance rate limit, and a single uniform 401
``invalid_or_expired`` for EVERY rejection (bad code / expired / unknown
email / exhausted — no oracle).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from collections import OrderedDict
from typing import Any, Callable
from urllib.parse import quote

from aiohttp import web

from .auth import (
    hash_otp_code,
    make_magic_token,
    make_session_token,
    verify_magic_token,
    verify_session_token,
)
from .config import WebConfig, WebUser, resolve_signing_secret
from .email import email_configured, send_magic_link, send_otp_code
from .identity import resolve_identity_from_name
from .keys import KEY_WEB_AUTH_STATE, KEY_WEB_CONFIG
from .utils import get_logger

log = get_logger(__name__)


# --- Login rate limiting (bit b) -------------------------------------------
# An unauthenticated caller can POST /auth/login unbounded — every known-email
# hit spends a Resend send + emails the target (email-bomb / quota drain). This
# bounds it in-process (no Redis): a per-``(client-ip, email)`` sliding window
# plus a small GLOBAL ceiling (email-rotation defense). Mirrors the OrderedDict
# LRU shape of routes_stt.py's ``_SttDedupCache`` — bounded key set, injectable
# clock for tests. Tuned so a legit "didn't get it, resend" never trips: the
# per-email cap permits several sends per window; the (N+1)th within the window
# → 429, and a send after the window elapses is allowed again.
_LOGIN_MAX_PER_EMAIL = 4       # allowed sends per (client-ip, email) per window
_LOGIN_WINDOW_S = 15 * 60      # 15-minute sliding window
_LOGIN_MAX_GLOBAL = 20         # global send ceiling per window
_LOGIN_MAX_KEYS = 512          # bounded distinct-key set (LRU-evicted)


class _LoginRateLimiter:
    """Bounded in-process sliding-window limiter for ``/auth/login``.

    Two gates, over separate signals:
      * per-key ``(client_ip, email)`` — caps repeat ATTEMPTS to one target;
        checked + recorded in :meth:`allow_attempt` (BEFORE the user lookup, so
        known and unknown emails are rate-limited identically — the enumeration
        defense).
      * global — caps actual SENDS across all keys (email-rotation defense);
        incremented ONLY in :meth:`record_send` (a real, looked-up user about
        to be emailed). :meth:`allow_attempt` applies a READ-ONLY check of it.

    Why the split (self-inflicted-DoS fix): an unknown/junk email never sends,
    so counting attempts against the GLOBAL window let ~``max_global`` junk
    POSTs lock out every legitimate login for a window. Counting SENDS instead
    keeps the rotation defense intact (rotating through REAL addresses still
    hits the send cap) while making junk-flood lockout impossible. The
    per-email gate still runs on every attempt, so junk is bounded and
    known-vs-unknown stays uniform.

    Only accepted attempts / real sends are recorded; a rejected request
    consumes no budget, and the reject path NEVER inserts a new key (a flood of
    distinct rejected keys must not grow ``_events`` past ``max_keys``). In-
    process + single event-loop thread + await-free methods → no lock needed.
    NOT persisted (a restart resetting the window is acceptable). Tests reset
    via :meth:`clear` (autouse fixture) or swap the module singleton for one
    with a fake clock.
    """

    def __init__(
        self,
        *,
        max_per_email: int = _LOGIN_MAX_PER_EMAIL,
        window_s: float = _LOGIN_WINDOW_S,
        max_global: int = _LOGIN_MAX_GLOBAL,
        max_keys: int = _LOGIN_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._events: "OrderedDict[tuple[str, str], list[float]]" = OrderedDict()
        self._global: list[float] = []
        self._max_per_email = max_per_email
        self._window = window_s
        self._max_global = max_global
        self._max_keys = max_keys
        self._clock = clock

    def _prune(self, stamps: list[float], now: float) -> list[float]:
        cutoff = now - self._window
        return [t for t in stamps if t > cutoff]

    def allow_attempt(self, key: "tuple[str, str]") -> bool:
        """Gate a login POST BEFORE the user lookup — ``False`` (→ 429) if over
        either gate.

        Records the PER-EMAIL attempt (so known and unknown emails are
        rate-limited identically — no enumeration) and applies a READ-ONLY
        check of the global send window. The global counter is NOT touched here
        — it increments only on an actual send (:meth:`record_send`), so a junk
        / unknown-email flood (which never sends) can't exhaust the global
        window and lock out real logins.
        """
        now = self._clock()
        bucket = self._prune(self._events.get(key, []), now)
        self._global = self._prune(self._global, now)  # keep pruned view
        over_email = len(bucket) >= self._max_per_email
        over_global = len(self._global) >= self._max_global
        if over_email or over_global:
            # Rejected: record NOTHING. Only refresh an ALREADY-TRACKED key's
            # pruned window; NEVER insert a new key on the reject path — a flood
            # of distinct rejected keys (e.g. a global-saturating junk flood)
            # must not grow ``_events`` past ``max_keys`` (bounded-but-
            # attacker-controlled memory growth otherwise).
            if key in self._events:
                self._events[key] = bucket
                self._events.move_to_end(key)
            return False
        bucket.append(now)
        self._events[key] = bucket
        self._events.move_to_end(key)
        while len(self._events) > self._max_keys:
            self._events.popitem(last=False)  # evict oldest (LRU)
        return True

    def record_send(self) -> None:
        """Increment the GLOBAL send window — called ONLY when a real,
        looked-up user is about to be emailed a magic link.

        Junk / unknown emails never reach here (the handler returns the uniform
        "sent" at the allowlist miss WITHOUT sending), so a junk flood can't
        consume the global budget. Rotating through REAL allowlisted addresses
        still hits this cap — the email-rotation defense is intact.
        """
        now = self._clock()
        self._global = self._prune(self._global, now)
        self._global.append(now)

    def clear(self) -> None:
        self._events.clear()
        self._global.clear()


# Module-level singleton (the hot-path limiter). Tests reset it per-test
# (autouse ``clear``) or replace it with a fake-clock instance.
_LOGIN_RATE_LIMITER = _LoginRateLimiter()

# OTP issuance rate limiter (#23) — its OWN instance of the same tested
# class, so the magic-link and OTP budgets are independent (a user who
# burns the link budget can still fall back to a code, and vice versa)
# while the mail-bomb / enumeration defenses are identical.
_OTP_RATE_LIMITER = _LoginRateLimiter()


# --- Deep-link after login (bit c) -----------------------------------------
def safe_next_path(raw: Any) -> str:
    """Sanitize a post-login redirect target — open-redirect defense.

    Python port of the front-end ``safeNextPath``
    (``web/lib/algernon/safeNextPath.ts``), kept byte-compatible so the server
    (which embeds ``next`` in the emailed magic link) and the callback (which
    re-sanitizes before redirecting) agree. Allows ONLY a same-origin relative
    path: must start with a single ``/``, must not be protocol-relative
    (``//host``) or a backslash trick (``/\\``), and must contain no backslash
    or ASCII control/whitespace char (browsers can normalise those in ways that
    change the effective origin). Anything else → the default ``/``.
    """
    if not isinstance(raw, str) or not raw:
        return "/"
    if raw[0] != "/":
        return "/"
    if len(raw) > 1 and (raw[1] == "/" or raw[1] == "\\"):
        return "/"
    for ch in raw:
        if ch == "\\" or ord(ch) <= 0x20:
            return "/"
    return raw


async def _read_json_body(request: web.Request) -> dict[str, Any]:
    """Best-effort JSON body read; returns ``{}`` on empty / invalid body."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body → treat as empty
        return {}
    return body if isinstance(body, dict) else {}


def _find_user_by_email(web_config: WebConfig, email: str) -> WebUser | None:
    """Case-insensitive email → allowlist user, or ``None``."""
    target = email.strip().lower()
    if not target:
        return None
    for user in web_config.users:
        if user.email and user.email.strip().lower() == target:
            return user
    return None


def _build_magic_link(base_url: str, token: str, next_path: str = "/") -> str:
    """Build the magic-link callback URL the user clicks.

    ``next_path`` (bit c) is a post-login deep-link target. It is re-sanitized
    (:func:`safe_next_path`) and appended URL-encoded ONLY when it is a real
    relative path — a default / open-redirect value collapses to ``/`` and is
    omitted, so the link is byte-identical to the pre-deep-link form when there
    is no next.
    """
    link = f"{base_url.rstrip('/')}/auth/callback?token={token}"
    safe = safe_next_path(next_path)
    if safe != "/":
        link += "&next=" + quote(safe, safe="")
    return link


async def _handle_auth_login(request: web.Request) -> web.StreamResponse:
    """POST /auth/login — request a magic link.

    Uniform ``{status:"sent"}`` on success AND on unknown email (no
    enumeration). 503 when send prerequisites are missing; 400 on a missing
    email field. Itself peer-gated by ``auth_middleware`` (only the
    registered front-end BFF can even request a link).
    """
    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    web_auth_state = request.app[KEY_WEB_AUTH_STATE]

    body = await _read_json_body(request)
    email = body.get("email")
    if not isinstance(email, str) or not email.strip():
        return web.json_response({"error": "email_required"}, status=400)

    # Rate-limit (bit b): bound magic-link sends per (client-ip, email) plus a
    # small global ceiling, so an unauthenticated caller can't drive unbounded
    # Resend spend / email-bomb a user. Checked BEFORE the user lookup so a
    # known and an unknown email are treated identically (no enumeration via a
    # 429). A legit "didn't get it, resend" stays under the per-email cap. The
    # GLOBAL counter is NOT incremented here — only on an actual send below
    # (record_send), so a junk-email flood can't lock out real logins.
    email_norm = email.strip().lower()
    client_ip = getattr(request, "remote", None) or ""
    if not _LOGIN_RATE_LIMITER.allow_attempt((client_ip, email_norm)):
        log.warning(
            "web.auth.login_rate_limited",
            client_ip=client_ip,
            # Hash, never the raw address (PII / enumeration in logs).
            email_sha=hashlib.sha256(email_norm.encode("utf-8")).hexdigest()[:8],
        )
        return web.json_response({"error": "rate_limited"}, status=429)

    # Deep-link (bit c): capture an optional post-login redirect target. It is
    # sanitized here (open-redirect defense) before being embedded in the
    # emailed magic link; the callback re-sanitizes (defense in depth). A
    # missing / non-conforming value collapses to the default ``/``.
    next_path = safe_next_path(body.get("next"))

    # Send prerequisites gate (server-config, independent of user match) so a
    # broken mailer surfaces as 503, not a silent no-op. base_url is folded
    # in — a link with an unresolved base is unusable. Single 503 code keeps
    # the contract; the specific gap is logged server-side.
    from .config import _is_unresolved

    if not email_configured(web_config.email) or _is_unresolved(
        web_config.auth.base_url
    ):
        log.warning(
            "web.auth.login_not_configured",
            email_ok=email_configured(web_config.email),
            base_url_ok=not _is_unresolved(web_config.auth.base_url),
            detail="Resend creds and/or web.auth.base_url unset/unresolved",
        )
        return web.json_response({"error": "email_not_configured"}, status=503)

    user = _find_user_by_email(web_config, email)
    if user is None:
        # Uniform response — no enumeration. Logged server-side. NOTE: no
        # record_send() here — an unknown email never sends, so it never
        # consumes the global budget (that's the junk-flood-lockout fix).
        log.info("web.auth.login_unknown_email")
        return web.json_response({"status": "sent"})

    # A real, allowlisted user is about to be emailed a magic link — count it
    # against the GLOBAL send window (email-rotation defense). The per-email
    # gate above already recorded the attempt.
    _LOGIN_RATE_LIMITER.record_send()

    secret = resolve_signing_secret(web_config.auth)
    ttl_minutes = web_config.auth.magic_link_ttl_minutes
    token, nonce = make_magic_token(
        user.name, secret=secret, ttl_minutes=ttl_minutes
    )
    # Record the nonce with the token's own expiry (decode it back rather
    # than recompute, so nonce-exp and token-exp can never drift).
    payload = verify_magic_token(token, secret=secret)
    exp = int(payload["exp"]) if payload else 0
    web_auth_state.record_nonce(nonce, user.name, exp)
    try:
        web_auth_state.save()
    except OSError:
        log.exception("web.auth.nonce_save_failed")

    link = _build_magic_link(web_config.auth.base_url, token, next_path)
    sent = await send_magic_link(
        web_config.email,
        user.email,
        link,
        instance_name=_instance_name(request),
    )
    if not sent:
        # Mailer failed after the prereq gate (transport error / non-2xx).
        return web.json_response({"error": "email_not_configured"}, status=503)

    log.info("web.auth.login_sent", user=user.name)
    return web.json_response({"status": "sent"})


async def _handle_auth_verify(request: web.Request) -> web.StreamResponse:
    """POST /auth/verify — exchange a magic token for a session token.

    Single failure shape (401 ``invalid_or_expired``) for every rejection
    path — bad/expired/forged token, consumed/unknown nonce (replay),
    removed user — so the client can't distinguish them.
    """
    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    web_auth_state = request.app[KEY_WEB_AUTH_STATE]

    body = await _read_json_body(request)
    token = body.get("token")
    if not isinstance(token, str) or not token:
        return web.json_response({"error": "invalid_or_expired"}, status=401)

    secret = resolve_signing_secret(web_config.auth)
    payload = verify_magic_token(token, secret=secret)
    if payload is None:
        return web.json_response({"error": "invalid_or_expired"}, status=401)

    # Consume the nonce (single-use). Save immediately so the consumption is
    # durable even if a later step fails — a replayed link must never work.
    nonce = payload.get("n")
    consumed = (
        web_auth_state.consume_nonce(nonce) if isinstance(nonce, str) else None
    )
    try:
        web_auth_state.save()
    except OSError:
        log.exception("web.auth.nonce_save_failed")
    if consumed is None:
        log.info("web.auth.verify_nonce_rejected")
        return web.json_response({"error": "invalid_or_expired"}, status=401)

    # Re-resolve against the CURRENT allowlist — a user removed between
    # link-issue and verify is locked out; role comes from live config.
    identity = resolve_identity_from_name(web_config, payload.get("u"))
    if identity is None:
        return web.json_response({"error": "invalid_or_expired"}, status=401)

    session_token = make_session_token(
        identity.user,
        identity.role,
        secret=secret,
        ttl_hours=web_config.auth.session_ttl_hours,
    )
    sess_payload = verify_session_token(session_token, secret=secret)
    exp = int(sess_payload["exp"]) if sess_payload else 0
    log.info("web.auth.verify_ok", user=identity.user, role=identity.role)
    return web.json_response(
        {
            "session_token": session_token,
            "name": identity.user,
            "role": identity.role,
            "exp": exp,
        }
    )


# ---------------------------------------------------------------------------
# OTP re-auth (parity #23 — iOS PWA cookie-jar fix)
# ---------------------------------------------------------------------------

# The exact wire shape of a passcode. Anything else — shorter, longer,
# non-digits — is rejected on the SAME uniform 401 as a wrong code.
_OTP_CODE_RE = re.compile(r"^[0-9]{6}$")


def _otp_disabled_response() -> web.StreamResponse:
    """404 for /auth/otp/* while ``web.auth.otp_enabled`` is off.

    The feature is DEFAULT-OFF and must be inert until the operator flips the
    flag. The STATUS is a 404 (not a 401/503) so a probe can't tell a disabled
    feature from an unconfigured deployment — but the BODY deliberately carries
    ``{"error": "otp_disabled"}`` so the BFF (``otp/verify.ts``) can
    graceful-degrade the feature-off case instead of treating it as a generic
    404. So it is NOT byte-indistinguishable from a nonexistent route: the
    status is, the body is not, by design."""
    return web.json_response({"error": "otp_disabled"}, status=404)


def _otp_uniform_401() -> web.StreamResponse:
    """The single failure shape for /auth/otp/verify.

    Identical for bad code / expired / unknown email / exhausted attempts /
    malformed body — byte-for-byte the magic-link verify's failure body, so
    no rejection path is an oracle."""
    return web.json_response({"error": "invalid_or_expired"}, status=401)


async def _handle_otp_request(request: web.Request) -> web.StreamResponse:
    """POST /auth/otp/request — email a one-time sign-in passcode.

    Mirrors ``/auth/login`` gate-for-gate (rate limit BEFORE the user
    lookup; uniform ``{status:"sent"}`` for known AND unknown emails; 503
    only on server-side mailer misconfig/failure) with two deliberate
    differences: no ``base_url`` prerequisite (no link is built — the code
    is typed into the already-open PWA) and no ``next`` handling (the PWA
    keeps its own post-login target client-side; an extra body field is
    ignored). The raw code is generated here, emailed, and NEVER persisted
    or logged — only its HMAC reaches the state store.
    """
    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    web_auth_state = request.app[KEY_WEB_AUTH_STATE]

    if not web_config.auth.otp_enabled:
        return _otp_disabled_response()

    body = await _read_json_body(request)
    email = body.get("email")
    if not isinstance(email, str) or not email.strip():
        return web.json_response({"error": "email_required"}, status=400)

    # Issuance rate limit — checked BEFORE the user lookup so known and
    # unknown emails are limited identically (no enumeration via a 429).
    email_norm = email.strip().lower()
    client_ip = getattr(request, "remote", None) or ""
    if not _OTP_RATE_LIMITER.allow_attempt((client_ip, email_norm)):
        log.warning(
            "web.auth.otp_rate_limited",
            client_ip=client_ip,
            # Hash, never the raw address (PII / enumeration in logs).
            email_sha=hashlib.sha256(email_norm.encode("utf-8")).hexdigest()[:8],
        )
        return web.json_response({"error": "rate_limited"}, status=429)

    # Send-prerequisite gate (server config, independent of user match) —
    # a broken mailer surfaces as 503, not a silent no-op. Unlike the
    # magic-link path, base_url is NOT required (no link to build).
    if not email_configured(web_config.email):
        log.warning(
            "web.auth.otp_not_configured",
            detail="Resend creds unset/unresolved — OTP cannot be sent",
        )
        return web.json_response({"error": "email_not_configured"}, status=503)

    user = _find_user_by_email(web_config, email)
    if user is None:
        # Uniform response — no enumeration. Logged server-side only. No
        # record_send(): an unknown email never sends, so it never consumes
        # the global budget (the junk-flood-lockout fix, mirrored).
        log.info("web.auth.otp_unknown_email")
        return web.json_response({"status": "sent"})

    _OTP_RATE_LIMITER.record_send()

    secret = resolve_signing_secret(web_config.auth)
    # The raw code lives ONLY in this local and the outbound email. It is
    # stored hash-only and never logged.
    code = f"{secrets.randbelow(1_000_000):06d}"
    exp = int(time.time() + web_config.auth.otp_ttl_minutes * 60)
    web_auth_state.record_otp(
        email_norm, user.name, hash_otp_code(email_norm, code, secret=secret), exp
    )
    try:
        web_auth_state.save()
    except OSError:
        log.exception("web.auth.otp_save_failed")

    sent = await send_otp_code(
        web_config.email,
        user.email,
        code,
        instance_name=_instance_name(request),
    )
    if not sent:
        # Mailer failed after the prereq gate (transport error / non-2xx).
        return web.json_response({"error": "email_not_configured"}, status=503)

    log.info("web.auth.otp_sent", user=user.name)  # never the code
    return web.json_response({"status": "sent"})


async def _handle_otp_verify(request: web.Request) -> web.StreamResponse:
    """POST /auth/otp/verify — exchange {email, code} for a session token.

    Success mints the session token via the SAME ``make_session_token``
    signer/claims/ttl path as the magic-link ``/auth/verify`` — the two
    tokens are byte-shape-identical and validate through the same
    ``require_web_session``. Every rejection returns the single uniform
    401 (:func:`_otp_uniform_401`); the specific reason is logged
    server-side only.

    Server-side controls, in order: TTL burn (an expired entry is popped
    on sight), defensive exhausted-cap check, timing-safe HMAC compare
    (with a dummy compare on the no-entry path so absent vs present does
    not obviously diverge in timing), attempt-cap burn on failure, and a
    durable single-use burn BEFORE minting on success.
    """
    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    web_auth_state = request.app[KEY_WEB_AUTH_STATE]

    if not web_config.auth.otp_enabled:
        return _otp_disabled_response()

    body = await _read_json_body(request)
    email = body.get("email")
    code = body.get("code")
    if not isinstance(email, str) or not email.strip():
        return _otp_uniform_401()
    if not isinstance(code, str) or not _OTP_CODE_RE.fullmatch(code):
        return _otp_uniform_401()

    email_norm = email.strip().lower()
    secret = resolve_signing_secret(web_config.auth)
    provided_hmac = hash_otp_code(email_norm, code, secret=secret)

    # TTL is enforced inside peek (an expired entry is burned on sight);
    # persist the possible burn even on this reject path.
    entry = web_auth_state.peek_otp(email_norm)
    if entry is None:
        # Unknown email, no outstanding code, or expired. Burn comparable
        # time to the real compare (no cheap absent-vs-present timing tell),
        # then reject uniformly.
        hmac.compare_digest(
            provided_hmac, hash_otp_code(email_norm, "000000", secret=secret)
        )
        try:
            web_auth_state.save()
        except OSError:
            log.exception("web.auth.otp_save_failed")
        log.info("web.auth.otp_verify_rejected", reason="no_live_code")
        return _otp_uniform_401()

    # Defensive: an entry at/over the cap must never be verifiable, even if
    # a crash/restore raced the burn-at-cap in record_otp_failure.
    max_attempts = web_config.auth.otp_max_attempts
    if int(entry.get("attempts", 0)) >= max_attempts:
        web_auth_state.consume_otp(email_norm)
        try:
            web_auth_state.save()
        except OSError:
            log.exception("web.auth.otp_save_failed")
        log.info("web.auth.otp_verify_rejected", reason="attempts_exhausted")
        return _otp_uniform_401()

    expected_hmac = str(entry.get("code_hmac", ""))
    if not hmac.compare_digest(
        expected_hmac.encode("ascii", "replace"),
        provided_hmac.encode("ascii"),
    ):
        attempts = web_auth_state.record_otp_failure(email_norm, max_attempts)
        try:
            web_auth_state.save()
        except OSError:
            log.exception("web.auth.otp_save_failed")
        log.info(
            "web.auth.otp_verify_rejected",
            reason="bad_code",
            attempts=attempts,
            burned=attempts >= max_attempts,
        )
        return _otp_uniform_401()

    # Match — SINGLE-USE BURN first, saved immediately so the consumption
    # is durable even if a later step fails (a verified code must never be
    # replayable). Mirrors the nonce-consume ordering in /auth/verify.
    web_auth_state.consume_otp(email_norm)
    try:
        web_auth_state.save()
    except OSError:
        log.exception("web.auth.otp_save_failed")

    # Re-resolve against the CURRENT allowlist — a user removed between
    # code-issue and verify is locked out; role comes from live config.
    identity = resolve_identity_from_name(web_config, entry.get("name"))
    if identity is None:
        log.info("web.auth.otp_verify_rejected", reason="user_removed")
        return _otp_uniform_401()

    session_token = make_session_token(
        identity.user,
        identity.role,
        secret=secret,
        ttl_hours=web_config.auth.session_ttl_hours,
    )
    sess_payload = verify_session_token(session_token, secret=secret)
    exp = int(sess_payload["exp"]) if sess_payload else 0
    log.info("web.auth.otp_verify_ok", user=identity.user, role=identity.role)
    return web.json_response(
        {
            "session_token": session_token,
            "name": identity.user,
            "role": identity.role,
            "exp": exp,
        }
    )


def _instance_name(request: web.Request) -> str:
    """Best-effort instance display name for the email subject."""
    try:
        talker_config = request.app["web.talker_config"]
        return getattr(getattr(talker_config, "instance", None), "name", "") or ""
    except Exception:  # noqa: BLE001 — purely cosmetic
        return ""


def register_auth_handlers(app: web.Application) -> None:
    """Mount the /auth routes. Called by ``register_web_routes`` (deps already
    stashed on ``app``).

    The OTP routes are ALWAYS mounted alongside login/verify (session mode
    only, like the rest of this module) but each handler gates on
    ``web.auth.otp_enabled`` per-request — DEFAULT-OFF, so they answer 404
    until the operator flips the flag."""
    app.router.add_post("/auth/login", _handle_auth_login)
    app.router.add_post("/auth/verify", _handle_auth_verify)
    app.router.add_post("/auth/otp/request", _handle_otp_request)
    app.router.add_post("/auth/otp/verify", _handle_otp_verify)
