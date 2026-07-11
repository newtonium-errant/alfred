"""Sovereign no-egress boundary — the fail-closed spine for on-box,
local-model-only instances (the ambient clinical scribe on VERA-clinical).

SECURITY-CRITICAL. A bug in this package means PHI leaks to a cloud
provider. The design is fail-closed everywhere: ambiguous => refuse =>
sovereign.

Public surface:

  * :func:`validate_sovereign_boundary` — config/process-load gate. Runs the
    four independent barriers; raises :class:`SovereignBoundaryError` unless
    every one holds. No-op for instances that do not declare
    ``sovereign: {enabled: true}``.
  * :class:`SovereignBoundaryError` — non-restartable breach (the orchestrator
    maps it to exit 79 and refuses to auto-restart into a cloud-reachable
    state).
  * :func:`install_sovereign_http_guard` / :func:`uninstall_sovereign_http_guard`
    / :func:`is_sovereign_http_guard_installed` / :func:`is_aiohttp_guard_installed`
    — the per-call HTTP guard that asserts loopback-before-connect on every
    outbound httpx AND aiohttp request (catches code drift the config-time
    barriers cannot see; the aiohttp wrap — task #40, the web STT/TTS transport —
    is import-guarded, so ``is_aiohttp_guard_installed`` reports whether it is
    live in this venv).
  * Constants (:data:`SOVEREIGN_STT_ALLOWLIST`, :data:`CLOUD_KEY_ENV_VARS`,
    :data:`EGRESS_CONFIG_SECTIONS`, :data:`LOOPBACK_HOSTS`) — the frozen
    policy the barriers enforce; imported by tests as the contract pins.
"""

from __future__ import annotations

from .boundary import (
    CLOUD_KEY_ENV_VARS,
    EGRESS_CONFIG_SECTIONS,
    LOOPBACK_HOSTS,
    SOVEREIGN_ALLOWED_SECTIONS,
    SOVEREIGN_STT_ALLOWLIST,
    SovereignBoundaryError,
    host_is_loopback,
    validate_sovereign_boundary,
)
from .http_guard import (
    install_sovereign_http_guard,
    is_aiohttp_guard_installed,
    is_sovereign_http_guard_installed,
    uninstall_sovereign_http_guard,
)

__all__ = [
    "CLOUD_KEY_ENV_VARS",
    "EGRESS_CONFIG_SECTIONS",
    "LOOPBACK_HOSTS",
    "SOVEREIGN_ALLOWED_SECTIONS",
    "SOVEREIGN_STT_ALLOWLIST",
    "SovereignBoundaryError",
    "host_is_loopback",
    "validate_sovereign_boundary",
    "install_sovereign_http_guard",
    "is_aiohttp_guard_installed",
    "is_sovereign_http_guard_installed",
    "uninstall_sovereign_http_guard",
]
