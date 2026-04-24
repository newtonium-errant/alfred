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
    # Distiller rebuild (Week 1 MVP) — feature flag for the non-agentic
    # extractor + deterministic writer path. When False (default), only
    # the legacy pipeline runs. When True, v2 runs in parallel with the
    # legacy path on every source; the extractor's output is then
    # filtered so only learnings whose ``type`` is in ``v2_types``
    # (default: ``["assumption"]`` — narrows blast radius during Week 2
    # measurement) are written to ``shadow_root``. v2 never touches the
    # live vault; widening ``v2_types`` later costs only shadow re-writes,
    # not re-extractions (the extractor already paid the LLM cost on
    # the full set).
    # ``v2_types`` filters OUTPUT (learning) types — "assumption",
    # "decision", "constraint", "contradiction", "synthesis". NOT source
    # record types like "session" or "note" — sources aren't filtered at
    # the daemon layer; the extractor decides per-source what to emit.
    # See docs/proposals/distiller-rebuild-team2-*.md for the rollout.
    use_deterministic_v2: bool = False
    shadow_root: str = "data/shadow/distiller"
    v2_types: list[str] = field(default_factory=lambda: ["assumption"])


@dataclass
class StateConfig:
    path: str = "./data/state.json"
    max_run_history: int = 20


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "./data/distiller.log"


@dataclass
class AnthropicConfig:
    """Anthropic SDK config for the non-agentic distiller rebuild (Week 1+).

    The rebuilt extractor calls the Messages API directly with no tools
    (see ``distiller/backends/anthropic_sdk.py``). Mirrors the shape of
    ``instructor/config.py::AnthropicConfig`` so config.yaml stays
    consistent across tools. Defaults to Claude Opus 4.7 because the
    rebuild's extraction prompt is the single expensive LLM call per
    source — the cheaper-model path lives on the drafter (Week 3).

    ``api_key`` falls back to the ``ANTHROPIC_API_KEY`` env var at call
    time when empty (see ``call_anthropic_no_tools``). Explicit config
    wins over env.
    """

    api_key: str = ""
    model: str = "claude-opus-4-7"
    max_tokens: int = 4096


@dataclass
class IdleTickConfig:
    """Distiller idle-tick heartbeat — "intentionally left blank" liveness signal.

    A periodic ``distiller.idle_tick`` log event so observers can distinguish
    *idle / healthy* from *broken*. Without it, a stretch with no learn
    records being created is indistinguishable from a hung daemon. See
    ``src/alfred/common/heartbeat.py`` for rationale and cadence.

    Counter semantic: one learn record created = one event.

    Defaults are deliberately on — the cost is negligible (~290 KB/day at
    60s) and the diagnostic value compounds.
    """

    enabled: bool = True
    interval_seconds: int = 60


@dataclass
class DistillerConfig:
    vault: VaultConfig = field(default_factory=VaultConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    # Anthropic SDK config for the non-agentic rebuild path (Week 1+).
    # Used only when ``extraction.use_deterministic_v2`` is True;
    # absent block in YAML keeps the legacy agent path untouched.
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    extraction: ExtractionConfig = field(default_factory=ExtractionConfig)
    state: StateConfig = field(default_factory=StateConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    # Idle-tick heartbeat — see :class:`IdleTickConfig`. Defaulted-on
    # via the dataclass default_factory; absent block in YAML keeps
    # ``enabled=True`` / ``interval_seconds=60``.
    idle_tick: IdleTickConfig = field(default_factory=IdleTickConfig)


# --- Recursive builder ---

_DATACLASS_MAP: dict[str, type] = {
    "vault": VaultConfig,
    "agent": AgentConfig,
    "claude": ClaudeBackendConfig,
    "zo": ZoBackendConfig,
    "openclaw": OpenClawBackendConfig,
    "anthropic": AnthropicConfig,
    "extraction": ExtractionConfig,
    "deep_extraction_schedule": ScheduleConfig,
    "consolidation_schedule": ScheduleConfig,
    "state": StateConfig,
    "logging": LoggingConfig,
    "idle_tick": IdleTickConfig,
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
    built: dict[str, Any] = {
        "vault": raw.get("vault", {}),
        "agent": raw.get("agent", {}),
        "extraction": tool.get("extraction", {}),
        "state": tool.get("state", {}),
        "logging": log_raw,
    }
    # Anthropic SDK config for the rebuild path. Per-tool block so the
    # distiller can use a different api_key / model than the instructor
    # or the talker if needed. Absent block → dataclass defaults.
    anthropic_raw = tool.get("anthropic")
    if isinstance(anthropic_raw, dict):
        built["anthropic"] = anthropic_raw
    # Idle-tick — defaulted-on; partial dict merges over dataclass default.
    idle_raw = tool.get("idle_tick")
    if isinstance(idle_raw, dict):
        built["idle_tick"] = idle_raw
    return _build(DistillerConfig, built)
