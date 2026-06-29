"""Outbound email for the web auth surface — Resend (magic-link delivery).

A small async ``httpx`` POST to Resend's API. Env-gated and SOFT-failing:
when the Resend credentials are absent / unresolved the sender logs and
returns ``False`` (the ``/auth/login`` handler turns that into a 503) —
it NEVER crashes, so the rest of the web surface (chat) proceeds without
email wired. ``httpx`` is already a project dependency (transcribe / tts /
transport client) — no new dependency.

Never logs the magic link or token (both secret). Logs recipient presence
as a bool, not the address.
"""

from __future__ import annotations

import httpx

from .config import WebEmailConfig, _is_unresolved
from .utils import get_logger

log = get_logger(__name__)

_RESEND_ENDPOINT = "https://api.resend.com/emails"
_TIMEOUT_SECONDS = 10.0


def email_configured(cfg: WebEmailConfig) -> bool:
    """True when both Resend credentials resolve to real (non-placeholder) values."""
    return not (_is_unresolved(cfg.api_key) or _is_unresolved(cfg.from_address))


async def send_magic_link(
    cfg: WebEmailConfig,
    to_email: str,
    link: str,
    *,
    instance_name: str = "",
) -> bool:
    """Send a magic-link email via Resend. Returns success.

    Returns ``False`` (never raises) on missing/unresolved creds, transport
    error, or non-2xx response — the caller maps that to a 503 so a broken
    mailer doesn't crash the login route. On the missing-creds path this is
    the intentionally-left-blank signal: an explicit "ran, did nothing
    because unconfigured" log rather than silence.
    """
    if not email_configured(cfg):
        log.warning(
            "web.email.not_configured",
            detail=(
                "Resend api_key / from_address unset or unresolved "
                "(${...} placeholder) — magic link NOT sent"
            ),
            recipient_present=bool(to_email),
        )
        return False

    subject = f"Your {instance_name or 'Algernon'} sign-in link"
    # Plain, link-only body. The link carries the (secret) magic token —
    # never logged below.
    html = (
        "<p>Here's your sign-in link:</p>"
        f'<p><a href="{link}">Sign in</a></p>'
        "<p>This link expires shortly and can be used only once. "
        "If you didn't request it, you can ignore this email.</p>"
    )
    payload = {
        "from": cfg.from_address,
        "to": [to_email],
        "subject": subject,
        "html": html,
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                _RESEND_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {cfg.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
    except Exception as exc:  # noqa: BLE001 — transport error → soft-fail
        log.warning(
            "web.email.send_error",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return False

    if resp.status_code // 100 != 2:
        body_tail = (resp.text or "")[:200]
        log.warning(
            "web.email.send_failed",
            status=resp.status_code,
            body_tail=body_tail,
        )
        return False

    log.info(
        "web.email.sent",
        status=resp.status_code,
        recipient_present=bool(to_email),
    )
    return True
