"""Cloudflared daemon config — typed dataclass + ``load_from_unified``.

Per-instance opt-in. Mirror of digest/daily_sync conventions: block
ABSENT = daemon never registered; block PRESENT with
``enabled: false`` = daemon registered but exits 78 immediately so the
orchestrator's auto-restart skips it (and the operator can grep the
log for ``cloudflared.disabled_in_config`` to confirm intent).

Schema (config.yaml)::

    cloudflared:
      enabled: true
      tunnel_id: "5e44e541-b24c-4caa-8246-105559dd8744"
      config_path: "~/.cloudflared/config.yml"     # optional override
      binary_path: "/usr/local/bin/cloudflared"    # optional override
      log_path: "./data/cloudflared.log"           # optional override
      metrics_port: 20241                          # optional override

When ``config_path`` is empty, cloudflared uses its own default
(``~/.cloudflared/config.yml``). When ``binary_path`` is empty we fall
back to ``/usr/local/bin/cloudflared`` — the standard install location
on the dev box. When ``log_path`` is empty we route into
``<logging.dir>/cloudflared.log`` to match the other daemons' log
locations. ``metrics_port`` defaults to cloudflared's own default
(20241) and is consumed by the health probe + detect-and-takeover at
daemon spawn time; operators rarely override it.

**Config-layer note:** keys are intentionally kept distinct from
existing :data:`_DATACLASS_MAP` entries in other tools' config.py
loaders (``state``, ``schedule``, ``agent``, ``openrouter``). This
module hand-rolls its construction so there is no recursive ``_build``
risk to begin with, but the discipline keeps the option open for a
future unified loader.

**Schema-tolerance contract** (per CLAUDE.md "State persistence —
load() schema-tolerance contract"): the loader filters incoming dicts
against the dataclass's known fields before constructing instances.
An older config file missing fields gets defaults; a newer one with
extra fields ignores the extras silently rather than crashing
``CloudflaredConfig.__init__``. Same forward-compatibility shape used
by brief / janitor / distiller / daily_sync / instructor.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# Default binary location — matches the standard cloudflared install
# path on the dev box. Operators can override via ``binary_path``.
DEFAULT_BINARY_PATH = "/usr/local/bin/cloudflared"

# Default Prometheus metrics port — cloudflared's own default. Surfaces
# on ``localhost:<port>/metrics``. Health probe + detect-and-takeover
# both consult this endpoint.
DEFAULT_METRICS_PORT = 20241


@dataclass
class CloudflaredConfig:
    """Resolved cloudflared daemon config.

    All path fields are stored as plain strings; ``~`` expansion happens
    at config-load time so the daemon receives absolute paths.
    """

    enabled: bool = False
    tunnel_id: str = ""
    config_path: str = ""
    binary_path: str = DEFAULT_BINARY_PATH
    log_path: str = ""
    metrics_port: int = DEFAULT_METRICS_PORT

    @property
    def metrics_url(self) -> str:
        """Composed metrics endpoint URL.

        Health probe + detect-and-takeover both read from this URL.
        We deliberately bind to ``localhost`` rather than ``0.0.0.0`` —
        the metrics endpoint is for operator/probe consumption only and
        shouldn't be exposed externally.
        """
        return f"http://localhost:{self.metrics_port}/metrics"


def _expand_path(raw_path: str) -> str:
    """Expand ``~`` and environment variables; empty stays empty.

    We deliberately do NOT resolve relative paths to absolute here —
    they stay relative so the daemon resolves them against its own
    CWD at runtime, consistent with how other tools handle
    ``./data/...`` log paths.
    """
    if not raw_path:
        return ""
    return os.path.expanduser(os.path.expandvars(raw_path))


def _coerce_int(value: object, default: int) -> int:
    """Tolerate string-shaped ints from YAML edge cases.

    YAML 1.1 parses ``20241`` as int but ``"20241"`` (quoted) as str.
    We coerce so the operator can write either form. Bad values fall
    back to the default rather than crashing the loader — a wrong
    ``metrics_port`` surfaces at probe time with a clear "unreachable"
    rather than ImportError-shaped death at startup.
    """
    if isinstance(value, bool):
        # ``bool`` is a subclass of ``int`` — guard against ``enabled: true``-
        # style copy-paste accidents leaking into ``metrics_port``.
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def _from_dict(data: dict, defaults_ctx: dict) -> CloudflaredConfig:
    """Apply schema-tolerance filter + per-field coercion.

    Canonical shape per CLAUDE.md schema-tolerance contract: filter
    incoming keys against ``__dataclass_fields__``, then apply per-field
    type coercion / path expansion / defaulting AFTER filtering.

    ``defaults_ctx`` is a small bag of cross-section defaults the loader
    computed up-front (currently just ``log_dir`` for the
    ``<logging.dir>/cloudflared.log`` fallback). Kept out of ``data`` so
    it can't be accidentally clobbered by a future config key collision.
    """
    known = {
        k: v
        for k, v in data.items()
        if k in CloudflaredConfig.__dataclass_fields__
    }

    # ``binary_path`` — empty-string coalesces to default. An operator
    # who wrote ``binary_path: ""`` explicitly meant "use the default"
    # by analogy with ``bit.schedule.time``; honor that intent rather
    # than passing the empty string through to the daemon (which would
    # fail-fast with binary_missing, but with a confusing error).
    binary_raw = str(known.get("binary_path", "") or "") or DEFAULT_BINARY_PATH
    known["binary_path"] = _expand_path(binary_raw)

    # ``config_path`` — ~ expansion at load time, empty stays empty
    # (cloudflared will use its own ~/.cloudflared/config.yml default).
    known["config_path"] = _expand_path(str(known.get("config_path", "") or ""))

    # ``log_path`` — empty derives from logging.dir; matches the other
    # daemons' default log locations so operators can find all daemon
    # logs in one directory.
    log_raw = str(known.get("log_path", "") or "")
    if not log_raw:
        log_raw = str(Path(defaults_ctx["log_dir"]) / "cloudflared.log")
    known["log_path"] = _expand_path(log_raw)

    # ``tunnel_id`` — string-coerce to tolerate YAML int-shaped IDs.
    known["tunnel_id"] = str(known.get("tunnel_id", "") or "")

    # ``enabled`` — explicit ``bool()`` so YAML ``"true"`` / ``"false"``
    # string variants don't slip through as truthy.
    known["enabled"] = bool(known.get("enabled", False))

    # ``metrics_port`` — int coercion with default fallback.
    known["metrics_port"] = _coerce_int(
        known.get("metrics_port", DEFAULT_METRICS_PORT),
        DEFAULT_METRICS_PORT,
    )

    return CloudflaredConfig(**known)


def load_from_unified(raw: dict) -> CloudflaredConfig:
    """Build :class:`CloudflaredConfig` from the unified config dict.

    Returns a disabled config (``enabled=False``, all other fields
    defaulted) when the ``cloudflared`` section is absent — callers
    can short-circuit on ``config.enabled`` without inspecting the
    raw dict.
    """
    section = raw.get("cloudflared") or {}
    if not isinstance(section, dict):
        # Tolerate ``cloudflared: null`` (commented-out-then-uncommented
        # YAML often parses to None). Treat as block-absent.
        return CloudflaredConfig()

    defaults_ctx = {
        "log_dir": (raw.get("logging") or {}).get("dir", "./data"),
    }
    return _from_dict(section, defaults_ctx)
