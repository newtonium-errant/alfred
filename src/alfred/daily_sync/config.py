"""Daily Sync config — typed dataclasses + ``load_from_unified``.

Per-instance config block at the top level of the unified config:

```yaml
daily_sync:
  enabled: true
  schedule:
    time: "09:00"
    timezone: "America/Halifax"
  batch_size: 5
  corpus:
    path: "./data/email_calibration.salem.jsonl"
  confidence:
    high: false
    medium: false
    low: false
    spam: false
  state:
    path: "./data/daily_sync_state.json"
```

When the block is absent (or ``enabled: false``) the orchestrator does
not start the Daily Sync daemon, the slash commands report "not
configured", and the email classifier's few-shot rotation is silently
disabled (no corpus to read from).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from alfred.common.schedule import ScheduleConfig

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
class CorpusConfig:
    """Path to the per-instance calibration corpus.

    Append-only JSONL. One row per Andrew-confirmed (or Andrew-corrected)
    classifier item. The Daily Sync writer appends; the classifier
    reader rotates the tail into its few-shot example slots.
    """

    path: str = "./data/email_calibration.salem.jsonl"


@dataclass
class ConfidenceConfig:
    """Per-tier confidence flags.

    Flipped via the ``/calibration_ok <tier>`` Telegram command and
    persisted to a small state file (NOT this dataclass — the dataclass
    only holds the seed values from config). The flags are read by
    surfacing consumers (c3/c4/c5) to gate per-tier surfacing on
    Andrew's explicit approval.
    """

    high: bool = False
    medium: bool = False
    low: bool = False
    spam: bool = False


@dataclass
class StateConfig:
    """Path to the Daily Sync state file.

    Holds: last-fired date, last batch (item index → record path), the
    Telegram message_id sequence of the most recent push (so the reply
    parser can match), and the persisted per-tier confidence flags.
    """

    path: str = "./data/daily_sync_state.json"


@dataclass
class AttributionConfig:
    """Attribution-audit section provider config (Phase 2 of audit arc).

    The Daily Sync's attribution-audit section reads
    ``attribution_audit`` frontmatter from across the vault and surfaces
    unconfirmed items for Andrew's per-item ``confirm`` / ``reject``.
    See ``src/alfred/daily_sync/attribution_section.py`` for the
    section provider and ``src/alfred/vault/attribution.py`` for the
    underlying primitives shipped in c1.

    ``scan_paths`` is empty by default → the section walks the whole
    vault. Restrict for performance once vault grows past ~10k records;
    typical entries are vault-relative subpaths like ``["note", "person"]``.
    """

    enabled: bool = True
    batch_size: int = 5
    scan_paths: list[str] = field(default_factory=list)
    # Audit corpus path — separate from the email calibration corpus
    # so the two streams stay independently auditable. Append-only
    # JSONL; one row per Andrew confirm or reject. The path is a
    # default; the production config may override it.
    corpus_path: str = "./data/attribution_audit_corpus.jsonl"


@dataclass
class DailySyncConfig:
    """Top-level Daily Sync config.

    ``enabled`` is the master switch — when False, the orchestrator skips
    starting the daemon and slash commands reply "not configured".
    """

    enabled: bool = False
    schedule: ScheduleConfig = field(
        default_factory=lambda: ScheduleConfig(time="09:00", timezone="America/Halifax"),
    )
    batch_size: int = 5
    corpus: CorpusConfig = field(default_factory=CorpusConfig)
    confidence: ConfidenceConfig = field(default_factory=ConfidenceConfig)
    state: StateConfig = field(default_factory=StateConfig)
    attribution: AttributionConfig = field(default_factory=AttributionConfig)


_DATACLASS_MAP: dict[str, type] = {
    "schedule": ScheduleConfig,
    "corpus": CorpusConfig,
    "confidence": ConfidenceConfig,
    "state": StateConfig,
    "attribution": AttributionConfig,
}


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Recursively construct a dataclass from a dict.

    Unknown top-level keys are ignored so a future schema bump on
    ``config.yaml.example`` doesn't break parsing on installs pinned to
    an older copy of this code.
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


def load_from_unified(raw: dict[str, Any]) -> DailySyncConfig:
    """Build a DailySyncConfig from the unified config dict.

    Returns a default-constructed (``enabled=False``) config when the
    ``daily_sync`` block is absent. Callers can rely on ``.enabled`` to
    decide whether to wire downstream work.
    """
    raw = _substitute_env(raw)
    section = raw.get("daily_sync", {}) or {}
    if not section:
        return DailySyncConfig(enabled=False)
    return _build(DailySyncConfig, section)


def load_config(path: str | Path = "config.yaml") -> DailySyncConfig:
    """Load and parse a config file (test helper)."""
    with open(Path(path), "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return load_from_unified(raw or {})
