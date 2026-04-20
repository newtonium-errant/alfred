"""Load config.yaml into typed dataclasses with env-var substitution."""

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
    """Recursively replace ${VAR} placeholders with environment variables."""
    if isinstance(value, str):
        def _replace(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(0))
        return ENV_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


# --- Dataclasses ---

@dataclass
class VaultConfig:
    path: str = ""
    # `inbox/processed` is the curator's audit trail of consumed raw inputs;
    # distilling from those risks double-extraction (once from the raw email,
    # once from the derived note). Matches surveyor's full-inbox exclusion
    # policy while keeping the curator's fresh inbox visible.
    ignore_dirs: list[str] = field(default_factory=lambda: [".obsidian", "inbox/processed"])
    ignore_files: list[str] = field(default_factory=list)

    @property
    def vault_path(self) -> Path:
        return Path(self.path)


@dataclass
class ClaudeBackendConfig:
    command: str = "claude"
    args: list[str] = field(default_factory=lambda: ["-p"])
    timeout: int = 600
    allowed_tools: list[str] = field(default_factory=lambda: ["Bash"])


@dataclass
class ZoBackendConfig:
    url: str = ""
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    request_body_template: dict[str, Any] = field(default_factory=dict)
    response_content_path: str = "response.content"
    timeout: int = 600


@dataclass
class OpenClawBackendConfig:
    command: str = "openclaw"
    args: list[str] = field(default_factory=list)
    workspace_mount: str = ""
    timeout: int = 600
    agent_id: str = "vault-distiller"


@dataclass
class AgentConfig:
    backend: str = "claude"
    claude: ClaudeBackendConfig = field(default_factory=ClaudeBackendConfig)
    zo: ZoBackendConfig = field(default_factory=ZoBackendConfig)
    openclaw: OpenClawBackendConfig = field(default_factory=OpenClawBackendConfig)


@dataclass
class ExtractionConfig:
    interval_seconds: int = 86400
    # Deprecated fallback — preserved so old config.yaml files still
    # load, but ``deep_extraction_schedule`` is the canonical gate for
    # the LLM-heavy deep extraction pass. See c4 in the scheduling
    # consolidation arc. Value is ignored when the schedule is set.
    deep_interval_hours: int = 168
    # Clock-aligned deep extraction. Default 03:30 Halifax daily so
    # the LLM-heavy deep pass lands overnight, ~1h after the janitor
    # deep sweep completes. Daily-only (day_of_week=None).
    deep_extraction_schedule: ScheduleConfig = field(
        default_factory=lambda: ScheduleConfig(
            time="03:30", timezone="America/Halifax",
        )
    )
    candidate_threshold: float = 0.6
    max_sources_per_batch: int = 20
    source_types: list[str] = field(default_factory=lambda: [
        "conversation", "session", "note", "task", "project",
    ])
    learn_types: list[str] = field(default_factory=lambda: [
        "assumption", "decision", "constraint", "contradiction", "synthesis",
    ])
    # Deprecated fallback for the weekly consolidation pass, replaced
    # by ``consolidation_schedule`` in c5. Kept for backward-compat.
    consolidation_interval_hours: int = 168  # 7 days
    # Clock-aligned consolidation pass — weekly on Sundays at 04:00
    # Halifax by default. ``day_of_week`` on ScheduleConfig turns the
    # daily pattern into a weekly gate; the next fire is the upcoming
    # Sunday (today if today is Sunday and the time is still ahead).
    consolidation_schedule: ScheduleConfig = field(
        default_factory=lambda: ScheduleConfig(
            time="04:00", timezone="America/Halifax", day_of_week="sunday",
        )
    )


@dataclass
class StateConfig:
    path: str = "./data/state.json"
    max_run_history: int = 20


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "./data/distiller.log"


@dataclass
class DistillerConfig:
    vault: VaultConfig = field(default_factory=VaultConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    extraction: ExtractionConfig = field(default_factory=ExtractionConfig)
    state: StateConfig = field(default_factory=StateConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


# --- Recursive builder ---

_DATACLASS_MAP: dict[str, type] = {
    "vault": VaultConfig,
    "agent": AgentConfig,
    "claude": ClaudeBackendConfig,
    "zo": ZoBackendConfig,
    "openclaw": OpenClawBackendConfig,
    "extraction": ExtractionConfig,
    "deep_extraction_schedule": ScheduleConfig,
    "consolidation_schedule": ScheduleConfig,
    "state": StateConfig,
    "logging": LoggingConfig,
}


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Recursively construct a dataclass from a dict."""
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key in _DATACLASS_MAP and isinstance(value, dict):
            kwargs[key] = _build(_DATACLASS_MAP[key], value)
        else:
            kwargs[key] = value
    return cls(**kwargs)


def load_config(path: str | Path = "config.yaml") -> DistillerConfig:
    """Load and parse config.yaml into DistillerConfig."""
    config_path = Path(path)
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw = _substitute_env(raw or {})
    return _build(DistillerConfig, raw)


def load_from_unified(raw: dict[str, Any]) -> DistillerConfig:
    """Build DistillerConfig from a pre-loaded unified config dict."""
    raw = _substitute_env(raw)
    tool = raw.get("distiller", {})
    # Map unified logging.dir -> logging.file
    log_raw = dict(raw.get("logging", {}))
    log_dir = log_raw.pop("dir", "./data")
    if "file" not in log_raw:
        log_raw["file"] = f"{log_dir}/distiller.log"
    return _build(DistillerConfig, {
        "vault": raw.get("vault", {}),
        "agent": raw.get("agent", {}),
        "extraction": tool.get("extraction", {}),
        "state": tool.get("state", {}),
        "logging": log_raw,
    })
