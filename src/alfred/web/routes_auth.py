"""Web auth routes — magic-link login (Sub-arc B).

    POST /auth/login   { email }  → { status: "sent" }   (uniform; no enumeration)
    POST /auth/verify  { token }  → { session_token, name, role, exp }

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
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from typing import Any, Callable
from urllib.parse import quote

from aiohttp import web

from .auth import (
    make_magic_token,
    make_session_token,
    verify_magic_token,
    verify_session_token,
)
from .config import WebConfig, WebUser, resolve_signing_secret
from .email import email_configured, send_magic_link
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


def _instance_name(request: web.Request) -> str:
    """Best-effort instance display name for the email subject."""
    try:
        talker_config = request.app["web.talker_config"]
        return getattr(getattr(talker_config, "instance", None), "name", "") or ""
    except Exception:  # noqa: BLE001 — purely cosmetic
        return ""


def register_auth_handlers(app: web.Application) -> None:
    """Mount the /auth routes. Called by ``register_web_routes`` (deps already
    stashed on ``app``)."""
    app.router.add_post("/auth/login", _handle_auth_login)
    app.router.add_post("/auth/verify", _handle_auth_verify)
