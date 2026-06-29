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

from typing import Any

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


def _build_magic_link(base_url: str, token: str) -> str:
    """Build the magic-link callback URL the user clicks."""
    return f"{base_url.rstrip('/')}/auth/callback?token={token}"


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
        # Uniform response — no enumeration. Logged server-side.
        log.info("web.auth.login_unknown_email")
        return web.json_response({"status": "sent"})

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

    link = _build_magic_link(web_config.auth.base_url, token)
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
