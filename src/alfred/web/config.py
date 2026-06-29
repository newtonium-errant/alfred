"""Typed config for the Algernon web surface (``web:`` section).

Follows the per-tool config pattern (``load_from_unified`` + ``${VAR}``
substitution), but DELIBERATELY hand-rolls every nested-block construction
instead of routing through a shared ``_build`` / ``_DATACLASS_MAP`` helper.
The dispatch-by-key-name footgun (CLAUDE.md "``_build`` collision footgun")
would bite here: the ``auth`` / ``email`` / ``state`` / ``users`` keys are
exactly the common ones already mapped to OTHER dataclasses in sibling
config modules. Hand-rolling sidesteps the collision class entirely — each
sub-block is constructed explicitly with a schema-tolerance filter
(``__dataclass_fields__``) so an older/newer config with extra keys loads
without crashing (the load-time schema-tolerance contract).

The named-user allowlist (``web.users``) IS the user table — no DB. Each
entry is ``{name, role, email}``. Auth (magic-link / HMAC session token /
Resend sender) is wired in Sub-arc B; this module carries the config those
will read.

Env substitution uses the canonical :func:`alfred._env.substitute_env_in_value`
(NOT a local hand-roll). Its coalesce semantics are load-bearing here: an
env var that is absent OR explicitly empty resolves to the literal
``${VAR}`` placeholder, so :func:`resolve_signing_secret` can fail loud on
BOTH cases (empty + unresolved) rather than silently HMAC-signing tokens
with a placeholder/garbage key. Per ``feedback_substitute_env_consolidation``
+ ``feedback_env_injection_load_bearing``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from alfred._env import substitute_env_in_value


def _is_unresolved(value: str | None) -> bool:
    """True when a config string is empty OR an unresolved ``${VAR}``.

    The canonical :func:`alfred._env.substitute_env_in_value` leaves an env
    var that is absent OR set to the empty string as its literal
    ``${VAR}`` placeholder. So "unconfigured" has exactly two surface forms
    — empty string and a leftover ``${...}`` — and this predicate is the
    single place that recognises both. Used by the signing-secret guard
    (fail-loud) and the Resend-creds check (soft-fail → 503).
    """
    return (not value) or value.startswith("${")


# --- Dataclasses -----------------------------------------------------------


@dataclass
class WebUser:
    """One named user in the ``web.users`` allowlist.

    ``name`` is the stable handle (lowercased for lookups + the synthetic
    session id); ``role`` maps to the existing ``run_turn(user_role=...)``
    → ``resolve_scope`` rail (``owner`` / ``ops``); ``email`` is the
    magic-link delivery address (Sub-arc B).
    """

    name: str = ""
    role: str = "owner"
    email: str = ""


@dataclass
class WebAuthConfig:
    """Session + magic-link signing config (consumed in Sub-arc B).

    ``session_secret`` is the instance HMAC signing key (env-substituted).
    Empty is tolerated at load; the auth-use site fails loud when
    ``web.enabled`` and the secret is empty (never sign with an empty key).
    ``base_url`` is the public front-end origin (the cloudflared subdomain)
    used to build magic-link URLs.
    """

    session_secret: str = ""
    session_ttl_hours: int = 168
    magic_link_ttl_minutes: int = 15
    base_url: str = ""


@dataclass
class WebEmailConfig:
    """Outbound email (Resend) config for magic-link delivery (Sub-arc B).

    Missing ``api_key`` / ``from_address`` is a deliberate soft-fail at the
    send site (log + 503), never a crash — so the chat surface proceeds
    without email wired.
    """

    provider: str = "resend"
    api_key: str = ""
    from_address: str = ""


@dataclass
class WebConfig:
    """Typed config for the ``web:`` section.

    ``enabled`` defaults False — an absent or disabled ``web:`` block means
    no web routes are mounted (opt-in inertness; the transport server is
    byte-unchanged for every instance that doesn't opt in).
    """

    enabled: bool = False
    users: list[WebUser] = field(default_factory=list)
    auth: WebAuthConfig = field(default_factory=WebAuthConfig)
    email: WebEmailConfig = field(default_factory=WebEmailConfig)
    # Tool-scoped state path for the single-use magic-link nonce store
    # (per the load() schema-tolerance contract's "default state paths must
    # be tool-scoped" rule). Overridable per-instance.
    state_path: str = "./data/web_auth_state.json"


# --- Hand-rolled construction ----------------------------------------------


def _build_users(raw: Any) -> list[WebUser]:
    """Build the ``users`` allowlist, skipping malformed / nameless entries.

    Each entry must be a dict carrying a non-empty ``name``. A nameless or
    non-dict entry is dropped (it can never be matched/authenticated) rather
    than constructing a blank user that would silently shadow nobody.
    """
    out: list[WebUser] = []
    if not isinstance(raw, list):
        return out
    known = WebUser.__dataclass_fields__
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        filtered = {k: v for k, v in entry.items() if k in known}
        name = str(filtered.get("name", "") or "").strip()
        if not name:
            continue
        out.append(
            WebUser(
                name=name,
                role=str(filtered.get("role", "owner") or "owner"),
                email=str(filtered.get("email", "") or ""),
            )
        )
    return out


def _build_auth(raw: Any) -> WebAuthConfig:
    """Hand-roll ``WebAuthConfig`` with a schema-tolerance filter."""
    if not isinstance(raw, dict):
        return WebAuthConfig()
    known = WebAuthConfig.__dataclass_fields__
    filtered = {k: v for k, v in raw.items() if k in known}
    defaults = WebAuthConfig()
    return WebAuthConfig(
        session_secret=str(filtered.get("session_secret", "") or ""),
        session_ttl_hours=_int(
            filtered.get("session_ttl_hours"), defaults.session_ttl_hours
        ),
        magic_link_ttl_minutes=_int(
            filtered.get("magic_link_ttl_minutes"),
            defaults.magic_link_ttl_minutes,
        ),
        base_url=str(filtered.get("base_url", "") or ""),
    )


def _build_email(raw: Any) -> WebEmailConfig:
    """Hand-roll ``WebEmailConfig`` with a schema-tolerance filter."""
    if not isinstance(raw, dict):
        return WebEmailConfig()
    known = WebEmailConfig.__dataclass_fields__
    filtered = {k: v for k, v in raw.items() if k in known}
    defaults = WebEmailConfig()
    return WebEmailConfig(
        provider=str(filtered.get("provider", defaults.provider)
                     or defaults.provider),
        api_key=str(filtered.get("api_key", "") or ""),
        from_address=str(filtered.get("from_address", "") or ""),
    )


def _int(value: Any, default: int) -> int:
    """Coerce to int, falling back to ``default`` on None / bad input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_from_unified(raw: dict[str, Any]) -> WebConfig:
    """Build :class:`WebConfig` from a pre-loaded unified config dict.

    Extracts the ``web`` section. Returns an all-default (disabled) config
    when the section is absent — which the daemon treats as "do not mount
    web routes" (opt-in inertness).
    """
    raw = substitute_env_in_value(raw or {})
    section = raw.get("web", {}) or {}
    if not isinstance(section, dict):
        section = {}
    return WebConfig(
        enabled=bool(section.get("enabled", False)),
        users=_build_users(section.get("users")),
        auth=_build_auth(section.get("auth")),
        email=_build_email(section.get("email")),
        state_path=str(
            section.get("state_path", "./data/web_auth_state.json")
            or "./data/web_auth_state.json"
        ),
    )


def resolve_signing_secret(auth: WebAuthConfig) -> str:
    """Return the validated HMAC signing secret, or fail loud.

    Raises :class:`ValueError` when the secret is empty OR an unresolved
    ``${VAR}`` placeholder (env var absent or set to empty — both coalesce
    to the literal placeholder via the canonical substituter). Never sign
    web session / magic-link tokens with an empty or placeholder key: a
    silent garbage key would either mint tokens nobody can verify or, worse,
    make forgery trivial depending on the bug. Fail-loud at the use site is
    the only safe behaviour — this is the load-bearing reason the WARN fix
    migrated to the coalesce-to-literal env semantics.

    Actual call sites (kept honest — comment-lies-about-behavior class):
    (1) ``require_web_session`` / the auth token codec, before any
    sign/verify; (2) ``register_web_routes``' startup guard; and (3) the
    talker daemon's web-wiring boot check (``daemon.py``), gated on
    ``web.enabled``. Sites (2)+(3) mean an enabled-but-unconfigured instance
    fails to MOUNT the web surface at startup (fail-closed — loud
    ``web_secret_unconfigured`` error), rather than booting clean and dying
    at first login. Web is opt-in, so the core talker daemon stays up — the
    misconfig disables only the web surface.
    """
    secret = auth.session_secret or ""
    if _is_unresolved(secret):
        raise ValueError(
            "web.auth.session_secret is unset or unresolved (empty or a "
            "literal ${...} placeholder) — refusing to sign web tokens with "
            "an empty/placeholder key. Set ALFRED_WEB_SESSION_SECRET (or "
            "web.auth.session_secret) to a strong random value."
        )
    return secret
