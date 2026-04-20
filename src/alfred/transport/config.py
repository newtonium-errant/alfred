"""Typed config for the Alfred outbound-push transport.

Same pattern as every other tool: ``load_from_unified(raw)`` takes the
pre-parsed unified config dict and returns a typed
``TransportConfig``. Environment variables are substituted via
``${VAR}`` syntax before the dataclasses are built.

The transport does NOT run as its own daemon — the server lives inside
the talker's event loop. This config is nevertheless loaded via the
same unified-config pattern so CLI commands, BIT probes, and the
talker's daemon can build it from ``config.yaml`` without a bespoke
loader.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ENV_RE = re.compile(r"\$\{(\w+)\}")


def _substitute_env(value: Any) -> Any:
    """Recursively replace ``${VAR}`` placeholders with environment variables."""
    if isinstance(value, str):
        def _replace(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(0))
        return ENV_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


# --- Dataclasses ------------------------------------------------------------


@dataclass
class ServerConfig:
    """HTTP server bind address.

    Localhost-only by default — v1 assumes all callers are co-located
    with the talker daemon. Stage 3.5 widens this to support peer
    connections from other hosts; until then, exposing the port to
    non-loopback interfaces is a policy mistake (no TLS, shared-secret
    auth, no rate-limiting outside per-chat Telegram floor).
    """

    host: str = "127.0.0.1"
    port: int = 8891


@dataclass
class SchedulerConfig:
    """In-process scheduler knobs.

    The scheduler runs inside the talker daemon as a sibling asyncio
    task. It polls the vault for due ``remind_at`` values on
    task records and dispatches scheduled entries out of the pending
    queue. See ``src/alfred/transport/scheduler.py`` (commit 4).
    """

    # How often the scheduler wakes up to look for due reminders.
    poll_interval_seconds: int = 30

    # Reminders whose ``remind_at`` is older than this window by the
    # time the scheduler sees them get routed to the dead-letter queue
    # instead of firing. Catches the "daemon was down for hours" case —
    # spamming a week's worth of stale reminders in one burst would be
    # worse than dropping them and telling the operator.
    stale_reminder_max_minutes: int = 180


@dataclass
class AuthTokenEntry:
    """One entry in the ``auth.tokens`` dict.

    The dict key is the peer name (``local``, plus Stage 3.5 peers like
    ``kal-le`` / ``stay-c``). Each peer gets its own shared secret and
    its own allowlist of ``X-Alfred-Client`` values.
    """

    token: str = ""
    allowed_clients: list[str] = field(default_factory=list)


@dataclass
class AuthConfig:
    """Bearer-token auth config.

    The ``tokens`` dict is keyed by peer name. v1 has one entry
    (``local``) whose token is injected into every co-located tool's
    subprocess env by the orchestrator. Stage 3.5 adds per-peer entries
    using the same schema — no rewrite needed.
    """

    tokens: dict[str, AuthTokenEntry] = field(default_factory=dict)


@dataclass
class StateConfig:
    """Where the transport state JSON lives.

    ``pending_queue`` holds scheduled ``/outbound/send`` entries whose
    ``scheduled_at`` is in the future. ``send_log`` is a short rolling
    record of recent sends for idempotency (the 24h dedupe window).
    ``dead_letter`` captures terminally-failed entries; the CLI exposes
    inspect/retry/drop commands against it.
    """

    path: str = "./data/transport_state.json"

    # Dead-letter entries older than this are eligible for eviction by
    # the CLI's maintenance command. The daemon does not auto-evict —
    # operators keep the visibility.
    dead_letter_max_age_days: int = 30


@dataclass
class TransportConfig:
    """Typed config for the transport module."""

    server: ServerConfig = field(default_factory=ServerConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    state: StateConfig = field(default_factory=StateConfig)


# --- Recursive builder ------------------------------------------------------


def _build_auth_token_entry(data: dict[str, Any]) -> AuthTokenEntry:
    """Build one ``AuthTokenEntry`` from a dict.

    The dict shape is ``{"token": str, "allowed_clients": [str, ...]}``.
    Unknown keys are tolerated but dropped — this keeps forward-compat
    room for Stage 3.5 additions without breaking the v1 loader.
    """
    return AuthTokenEntry(
        token=str(data.get("token", "") or ""),
        allowed_clients=list(data.get("allowed_clients", []) or []),
    )


def _build_auth(data: dict[str, Any]) -> AuthConfig:
    """Build ``AuthConfig`` with per-peer token entries."""
    tokens_raw = data.get("tokens", {}) or {}
    tokens: dict[str, AuthTokenEntry] = {}
    for peer, entry_raw in tokens_raw.items():
        if isinstance(entry_raw, dict):
            tokens[str(peer)] = _build_auth_token_entry(entry_raw)
    return AuthConfig(tokens=tokens)


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Recursively construct a dataclass from a dict.

    The ``auth`` section has a non-trivial nested structure so we
    handle it explicitly; the others map cleanly onto simple nested
    dataclasses.
    """
    if cls is TransportConfig:
        kwargs: dict[str, Any] = {}
        if "server" in data and isinstance(data["server"], dict):
            kwargs["server"] = ServerConfig(**{
                k: v for k, v in data["server"].items()
                if k in {"host", "port"}
            })
        if "scheduler" in data and isinstance(data["scheduler"], dict):
            kwargs["scheduler"] = SchedulerConfig(**{
                k: v for k, v in data["scheduler"].items()
                if k in {"poll_interval_seconds", "stale_reminder_max_minutes"}
            })
        if "auth" in data and isinstance(data["auth"], dict):
            kwargs["auth"] = _build_auth(data["auth"])
        if "state" in data and isinstance(data["state"], dict):
            kwargs["state"] = StateConfig(**{
                k: v for k, v in data["state"].items()
                if k in {"path", "dead_letter_max_age_days"}
            })
        return TransportConfig(**kwargs)
    # Fallback — not currently reached, but keeps the shape extensible.
    return cls(**data)


def load_config(path: str | Path = "config.yaml") -> TransportConfig:
    """Load and parse config.yaml into a fully-built TransportConfig."""
    config_path = Path(path)
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw = _substitute_env(raw or {})
    return load_from_unified(raw)


def load_from_unified(raw: dict[str, Any]) -> TransportConfig:
    """Build TransportConfig from a pre-loaded unified config dict.

    Extracts the ``transport`` section. Returns all-default config when
    the section is absent — callers that need a token-configured
    transport must detect the all-empty ``auth.tokens`` dict and fail
    closed, which the server's auth middleware does on every request.
    """
    raw = _substitute_env(raw)
    tool = raw.get("transport", {}) or {}
    return _build(TransportConfig, tool)
