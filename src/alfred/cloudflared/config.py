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

When ``config_path`` is empty, cloudflared uses its own default
(``~/.cloudflared/config.yml``). When ``binary_path`` is empty we fall
back to ``/usr/local/bin/cloudflared`` — the standard install location
on the dev box. When ``log_path`` is empty we route into
``<logging.dir>/cloudflared.log`` to match the other daemons' log
locations.

**Config-layer note:** keys are intentionally kept distinct from
existing :data:`_DATACLASS_MAP` entries in other tools' config.py
loaders (``state``, ``schedule``, ``agent``, ``openrouter``). This
module hand-rolls its construction so there is no recursive ``_build``
risk to begin with, but the discipline keeps the option open for a
future unified loader.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# Default binary location — matches the standard cloudflared install
# path on the dev box. Operators can override via ``binary_path``.
DEFAULT_BINARY_PATH = "/usr/local/bin/cloudflared"


@dataclass
class CloudflaredConfig:
    """Resolved cloudflared daemon config.

    All fields are stored as plain strings; ``~`` expansion happens at
    config-load time so the daemon receives absolute paths.
    """

    enabled: bool = False
    tunnel_id: str = ""
    config_path: str = ""
    binary_path: str = DEFAULT_BINARY_PATH
    log_path: str = ""


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

    log_path_raw = section.get("log_path", "")
    if not log_path_raw:
        # Default to ``<logging.dir>/cloudflared.log`` for consistency
        # with the other daemons. Fall through to a bare path if logging
        # dir isn't configured.
        log_dir = (raw.get("logging") or {}).get("dir", "./data")
        log_path_raw = str(Path(log_dir) / "cloudflared.log")

    binary_path_raw = section.get("binary_path", "") or DEFAULT_BINARY_PATH

    return CloudflaredConfig(
        enabled=bool(section.get("enabled", False)),
        tunnel_id=str(section.get("tunnel_id", "") or ""),
        config_path=_expand_path(str(section.get("config_path", "") or "")),
        binary_path=_expand_path(binary_path_raw),
        log_path=_expand_path(log_path_raw),
    )
