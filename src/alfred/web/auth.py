"""Web auth — instance-signed magic-link + session tokens (stdlib HMAC).

Two token types, both compact ``payload_b64.sig_b64`` strings signed with
the instance's ``web.auth.session_secret`` via HMAC-SHA256 (no PyJWT
dependency — stdlib only, matching the ``secrets.compare_digest`` idiom
already used in the transport auth middleware):

* **magic** ``{t:"magic", u:name, n:nonce, exp}`` — minted by
  ``/auth/login``, mailed inside the link; exchanged at ``/auth/verify``.
  The ``nonce`` is recorded server-side (``WebAuthState``) and consumed on
  first verify, so a link is single-use (replay → unknown nonce → reject).
* **session** ``{t:"session", u:name, r:role, exp}`` — minted by
  ``/auth/verify``, relayed by the BFF on every ``/chat/*`` call in the
  ``X-Alfred-Session`` header.

The ``t`` (type) field is checked on verify, so a magic token can never be
replayed as a session token (type-confusion closed). Both are signed with
the SAME secret; the type tag is what separates them.

Confused-deputy property: the token is signed by the instance, so the
front-end can only relay a token Algernon itself issued after verifying a
real credential — it cannot fabricate "I am Andrew".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import TYPE_CHECKING, Any

from .config import WebConfig, resolve_signing_secret
from .identity import WebIdentity, resolve_identity_from_name
from .utils import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aiohttp import web

log = get_logger(__name__)

# Token type tags.
TOKEN_MAGIC = "magic"
TOKEN_SESSION = "session"

# The header the BFF relays the session token in (codebase legacy ``alfred``
# form, consistent with the existing ``X-Alfred-Client`` peer header).
SESSION_HEADER = "X-Alfred-Session"

# The header the BFF relays the asserted (verified-on-the-login-instance)
# user NAME in, for cross-instance ``relay``-mode chat. Mirrors ingest's
# ``X-Alfred-Ingest-User`` — provenance/identity only, NEVER authz: the
# Layer-1 peer token IS the authority, and the target re-resolves the name
# against its OWN ``web.users`` allowlist.
USER_HEADER = "X-Alfred-User"

# The transport peer NAME (``auth.tokens`` key) whose token authorises a
# relay-mode chat turn. The relay path PINS this explicitly rather than
# trusting ``X-Alfred-Client`` / ``allowed_clients`` alone: the chat ``web``
# token and the ``web_ingest`` token BOTH carry ``allowed_clients: [web]``,
# so a request bearing the deterministic-create-only ``web_ingest`` token +
# ``X-Alfred-Client: web`` clears Layer 1 (resolving ``transport_peer =
# "web_ingest"``) — without this pin it would then drive a FULL talker-scope
# chat turn (privilege escalation). See CLAUDE.md "Relay / asserted-identity
# routes — peer-pin requirement".
WEB_CHAT_PEER = "web"


# ---------------------------------------------------------------------------
# Compact token codec (stdlib HMAC, no dependency)
# ---------------------------------------------------------------------------


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _sign_payload(payload: dict[str, Any], secret: str) -> str:
    """Return ``payload_b64.sig_b64`` for ``payload`` signed with ``secret``."""
    payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    payload_b64 = _b64url_encode(payload_json.encode("utf-8"))
    sig = hmac.new(
        secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{payload_b64}.{_b64url_encode(sig)}"


def _verify_payload(token: str, secret: str) -> dict[str, Any] | None:
    """Verify the HMAC signature and decode the payload, or ``None``.

    Returns ``None`` on any structural problem (no dot, bad base64, bad
    JSON, non-dict payload) or signature mismatch. Signature comparison
    uses ``hmac.compare_digest`` (timing-safe). Does NOT check ``exp`` /
    type — the typed verifiers below layer those on.
    """
    if not token or "." not in token:
        return None
    payload_b64, _, sig_b64 = token.partition(".")
    if not payload_b64 or not sig_b64:
        return None
    expected = hmac.new(
        secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        provided = _b64url_decode(sig_b64)
    except Exception:  # noqa: BLE001 — malformed sig → reject
        return None
    if not hmac.compare_digest(expected, provided):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:  # noqa: BLE001 — malformed payload → reject
        return None
    return payload if isinstance(payload, dict) else None


def _verify_typed(
    token: str, secret: str, expected_type: str, now: float | None,
) -> dict[str, Any] | None:
    """Shared verify: signature + type tag + expiry."""
    payload = _verify_payload(token, secret)
    if payload is None:
        return None
    if payload.get("t") != expected_type:
        return None  # type-confusion guard (magic ↔ session)
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return None
    current = time.time() if now is None else now
    if current > float(exp):
        return None  # expired
    return payload


# ---------------------------------------------------------------------------
# Mint
# ---------------------------------------------------------------------------


def make_magic_token(
    name: str,
    *,
    secret: str,
    ttl_minutes: int,
    now: float | None = None,
) -> tuple[str, str]:
    """Mint a single-use magic-link token. Returns ``(token, nonce)``.

    The caller records the ``nonce`` in :class:`WebAuthState` (with the same
    ``exp``) so ``/auth/verify`` can enforce single-use.
    """
    current = time.time() if now is None else now
    nonce = secrets.token_urlsafe(32)
    exp = int(current + ttl_minutes * 60)
    payload = {"t": TOKEN_MAGIC, "u": name, "n": nonce, "exp": exp}
    return _sign_payload(payload, secret), nonce


def make_session_token(
    name: str,
    role: str,
    *,
    secret: str,
    ttl_hours: int,
    now: float | None = None,
) -> str:
    """Mint a session token carrying the verified ``{user, role, exp}``."""
    current = time.time() if now is None else now
    exp = int(current + ttl_hours * 3600)
    payload = {"t": TOKEN_SESSION, "u": name, "r": role, "exp": exp}
    return _sign_payload(payload, secret)


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def verify_magic_token(
    token: str, *, secret: str, now: float | None = None,
) -> dict[str, Any] | None:
    """Verify a magic token (signature + type + expiry). Returns payload."""
    return _verify_typed(token, secret, TOKEN_MAGIC, now)


def verify_session_token(
    token: str, *, secret: str, now: float | None = None,
) -> dict[str, Any] | None:
    """Verify a session token (signature + type + expiry). Returns payload."""
    return _verify_typed(token, secret, TOKEN_SESSION, now)


# ---------------------------------------------------------------------------
# Per-request session resolution (Layer 2) — replaces the Sub-arc A seam
# ---------------------------------------------------------------------------


def require_web_session(
    request: "web.Request", web_config: WebConfig,
) -> WebIdentity | None:
    """Resolve the driving user from the signed session token, fail-closed.

    Reads the ``X-Alfred-Session`` header, verifies the instance signature
    + type + expiry, then RE-RESOLVES the user against the CURRENT
    allowlist. Re-resolution (rather than trusting the token's embedded
    role) means a user removed from ``web.users`` is immediately locked out
    even with a still-valid token, and a role change in config takes effect
    on the next request — the token proves *who verified*, config decides
    *what they may do*. Returns ``None`` on any failure (→ the handler
    emits a fail-closed 401).

    This is the Sub-arc B replacement for ``_resolve_request_identity`` —
    same ``WebIdentity | None`` shape so the route swap is localised.
    """
    token = request.headers.get(SESSION_HEADER, "")
    if not token:
        return None
    try:
        secret = resolve_signing_secret(web_config.auth)
    except ValueError:
        # Server is enabled-but-unconfigured. The startup wiring guard
        # should have prevented this; fail closed defensively rather than
        # 500, and log so the misconfig is visible.
        log.warning(
            "web.auth.session_secret_unresolved_at_request",
            detail="web enabled but session_secret empty/unresolved — "
                   "rejecting session (should have failed loud at startup)",
        )
        return None
    payload = verify_session_token(token, secret=secret)
    if payload is None:
        return None
    return resolve_identity_from_name(web_config, payload.get("u"))


def _resolve_relay_identity(
    request: "web.Request", web_config: WebConfig,
) -> WebIdentity | None:
    """Resolve the driving user from the asserted ``X-Alfred-User`` header.

    The ``relay`` auth model (mirrors ``/vault/ingest``): the request has
    already passed Layer-1 peer-token auth in the transport
    ``auth_middleware`` — possession of this instance's dedicated ``web``
    peer token IS the authority. The BFF (sole holder of that token) asserts
    the verified user NAME (never a role) in ``X-Alfred-User``; this instance
    re-resolves that name against its OWN ``web.users`` allowlist to derive
    role + synthetic session id. The BFF cannot escalate (it asserts name
    only); config decides what the user may do.

    Fail-closed (→ the handler emits a 401):

    * presenting peer is not the chat ``web`` peer → reject (logged) — the
      peer-pin that blocks a ``web_ingest`` token from escalating to full
      chat scope;
    * missing / empty ``X-Alfred-User`` → reject (logged);
    * name not in this instance's ``web.users`` → reject (logged).
    """
    # Peer-pin (defense-in-depth): only the dedicated chat ``web`` peer may
    # drive a relay chat turn. ``transport_peer`` is the matched peer NAME
    # set by ``auth_middleware``; pinning it closes the shared-``allowed_clients``
    # escalation where the ``web_ingest`` token + ``X-Alfred-Client: web``
    # clears Layer 1 as peer ``web_ingest``.
    peer = request.get("transport_peer", "")
    if peer != WEB_CHAT_PEER:
        log.warning(
            "web.auth.relay_wrong_peer",
            peer=peer or "(none)",
            expected=WEB_CHAT_PEER,
            detail="relay chat requires the dedicated 'web' peer token — "
                   "refusing to honor X-Alfred-User from another peer "
                   "(e.g. web_ingest) — rejecting (401)",
        )
        return None

    name = request.headers.get(USER_HEADER, "")
    if not name or not name.strip():
        # Intentionally-left-blank: a fail-closed reject is logged so a
        # mis-wired BFF (peer token present, asserted user absent) is
        # observably distinct from a healthy idle route.
        log.warning(
            "web.auth.relay_user_missing",
            detail="relay mode but X-Alfred-User absent — rejecting (401)",
        )
        return None
    identity = resolve_identity_from_name(web_config, name)
    if identity is None:
        log.warning(
            "web.auth.relay_user_unknown",
            name=name.strip()[:64],
            detail="X-Alfred-User not in this instance's web.users — "
                   "rejecting (401)",
        )
        return None
    return identity


def resolve_web_identity(
    request: "web.Request", web_config: WebConfig,
) -> WebIdentity | None:
    """Mode-aware identity dispatcher for the ``/chat/*`` handlers.

    Returns the same ``WebIdentity | None`` shape as
    :func:`require_web_session` so the route swap is a trivial one-liner at
    each handler. Dispatch on ``web.auth.mode``:

    * ``"session"`` (default) — the existing instance-signed
      ``X-Alfred-Session`` token path (``require_web_session``), UNCHANGED.
    * ``"relay"`` — the asserted ``X-Alfred-User`` path
      (``_resolve_relay_identity``), gated by the Layer-1 peer token.
    """
    mode = getattr(web_config.auth, "mode", "session") or "session"
    if mode == "relay":
        return _resolve_relay_identity(request, web_config)
    return require_web_session(request, web_config)
