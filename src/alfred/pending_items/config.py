"""Pending Items Queue config — typed dataclasses + ``load_from_unified``.

Per-instance config block at the top level of the unified config:

```yaml
pending_items:
  enabled: true
  # Local JSONL queue path.
  queue_path: "./data/pending_items.jsonl"
  # Vault markdown view regenerated on every queue mutation
  # (debounced to ≤1 write per `view_debounce_seconds`).
  view_path: "process/Pending Items.md"
  view_debounce_seconds: 30
  # Periodic peer-push (peer → Salem). Salem itself omits this block
  # or sets ``target_peer: ""`` to disable.
  push:
    target_peer: "salem"        # peer key under transport.peers.<this>
    self_name: "hypatia"        # this instance's identity for body.from_instance
    interval_seconds: 300       # 5 min flush
  # Soft + hard expiry windows. Soft items render as "stale" in
  # Daily Sync. Hard items get auto-expired (status=expired).
  expiry:
    stale_days: 7
    expire_days: 14
  # Outbound failure detector — scans session frontmatter for new
  # outbound_failures and emits queue rows. Always enabled when the
  # parent block is enabled; the field-level toggle exists for tests.
  outbound_failure_detector:
    enabled: true
```

Defaults are tuned for Salem (queue_path under data/, view under
vault/process/). Peer instances override the push.target_peer +
self_name. Salem leaves push.target_peer empty so it doesn't try to
push to itself — Salem aggregates peer pushes via the inbound
endpoint instead.
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


@dataclass
class PushConfig:
    """Periodic peer-push settings.

    Empty ``target_peer`` disables the pusher daemon entirely (Salem
    leaves it empty since Salem aggregates instead of pushing).
    """

    target_peer: str = ""
    self_name: str = ""
    interval_seconds: int = 300


@dataclass
class ExpiryConfig:
    """Soft + hard expiry windows.

    Items older than ``stale_days`` render as "(stale)" in the Daily
    Sync section. Items older than ``expire_days`` get auto-expired
    by the periodic sweep.
    """

    stale_days: int = 7
    expire_days: int = 14


@dataclass
class OutboundFailureDetectorConfig:
    """Outbound-failure session-scanner config.

    Salem + every peer instance runs this scanner against its own
    ``vault/session/`` directory. New ``outbound_failures`` entries on
    closed sessions become pending-item rows. Already-emitted entries
    are tracked via a small state file so repeated scans don't
    duplicate.
    """

    enabled: bool = True
    # Per-instance state file tracking which (session_id, turn_index)
    # tuples have already been emitted as queue rows.
    state_path: str = "./data/pending_items_outbound_failure_state.json"
    # Subpath under the vault to scan. Most instances put session
    # records under "session/"; left configurable for instances with
    # exotic vault layouts.
    session_subpath: str = "session"


@dataclass
class PendingItemsConfig:
    """Top-level pending-items config.

    ``enabled`` is the master switch — when False the orchestrator
    skips starting the pusher + detector daemons, the queue file
    stays untouched, and the Daily Sync section silently renders
    nothing.
    """

    enabled: bool = False
    queue_path: str = "./data/pending_items.jsonl"
    view_path: str = "process/Pending Items.md"
    view_debounce_seconds: int = 30
    push: PushConfig = field(default_factory=PushConfig)
    expiry: ExpiryConfig = field(default_factory=ExpiryConfig)
    outbound_failure_detector: OutboundFailureDetectorConfig = field(
        default_factory=OutboundFailureDetectorConfig,
    )


_DATACLASS_MAP: dict[str, type] = {
    "push": PushConfig,
    "expiry": ExpiryConfig,
    "outbound_failure_detector": OutboundFailureDetectorConfig,
}


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Recursively construct a dataclass from a dict.

    Unknown top-level keys are ignored — keeps forward-compat room
    for a Phase 2/3 schema bump that older installs don't yet read.
    """
    field_names = {f.name for f in cls.__dataclass_fields__.values()}
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key not in field_names:
            continue
        if key in _DATACLASS_MAP and isinstance(value, dict):
            kwargs[key] = _build(_DATACLASS_MAP[key], value)
        else:
            kwargs[key] = value
    return cls(**kwargs)


def load_from_unified(raw: dict[str, Any]) -> PendingItemsConfig:
    """Build a :class:`PendingItemsConfig` from the unified config dict.

    Returns a default-constructed (``enabled=False``) config when the
    ``pending_items`` block is absent. Callers can rely on
    ``.enabled`` to decide whether to wire downstream work.
    """
    raw = _substitute_env(raw)
    section = raw.get("pending_items", {}) or {}
    if not section:
        return PendingItemsConfig(enabled=False)
    return _build(PendingItemsConfig, section)


def load_config(path: str | Path = "config.yaml") -> PendingItemsConfig:
    """Load and parse a config file (test helper)."""
    with open(Path(path), "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return load_from_unified(raw or {})


__all__ = [
    "ExpiryConfig",
    "OutboundFailureDetectorConfig",
    "PendingItemsConfig",
    "PushConfig",
    "load_config",
    "load_from_unified",
]
