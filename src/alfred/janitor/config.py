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
    # `_templates` contains placeholder wikilinks (`[[project/My Project]]`)
    # as syntax examples; the scanner must not flag those as broken links.
    # `_bases` holds Obsidian base view definitions with similar placeholders.
    # `inbox/processed` is the curator's audit trail of consumed raw inputs
    # (emails, drops) — the derived vault records are the canonical artifacts,
    # so janitor must not flag FM001 / LINK001 on raw email bodies. This
    # matches surveyor's policy (which excludes all of `inbox`) and keeps
    # the curator's fresh inbox visible for its own watcher.
    #
    # ``ignore_dirs`` is the legacy field, semantically equivalent to
    # ``dont_scan_dirs``: directories excluded from outbound issue scanning.
    # Every scanner / snapshot / walker in the codebase still reads this
    # field; ``normalize_vault_block`` keeps it in sync with
    # ``dont_scan_dirs`` for back-compat. NEW code should prefer
    # ``dont_scan_dirs`` (clearer semantics) but reading from
    # ``ignore_dirs`` still works.
    ignore_dirs: list[str] = field(default_factory=lambda: [".obsidian", "_templates", "_bases", "inbox/processed"])
    ignore_files: list[str] = field(default_factory=list)
    # New (2026-05-01): split ignore_dirs into two semantically distinct
    # fields. ``dont_scan_dirs`` is identical to ``ignore_dirs`` — included
    # so configs can use the new key without breaking compat. The helper
    # ``normalize_vault_block`` mirrors them. Defaults to None so
    # back-compat configs (only ``ignore_dirs`` set) leave it untouched.
    dont_scan_dirs: list[str] | None = None
    # ``dont_index_dirs`` controls the janitor's valid-link-target stem
    # index. A wikilink to a record under one of these dirs reports
    # LINK001 (the target is invisible to the index). Default EMPTY:
    # every record in the vault is a valid link target unless the
    # operator explicitly opts out. Fixes the historical bug where
    # ``session/`` (in ``ignore_dirs`` for outbound scan exclusion) also
    # silently fell out of the index, falsely flagging ~38 valid
    # voice-session wikilinks.
    dont_index_dirs: list[str] = field(default_factory=list)

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
    agent_id: str = "vault-janitor"


@dataclass
class AgentConfig:
    backend: str = "claude"
    claude: ClaudeBackendConfig = field(default_factory=ClaudeBackendConfig)
    zo: ZoBackendConfig = field(default_factory=ZoBackendConfig)
    openclaw: OpenClawBackendConfig = field(default_factory=OpenClawBackendConfig)


@dataclass
class SweepConfig:
    interval_seconds: int = 3600
    # Deprecated fallback — preserved so old config.yaml files still
    # load, but ``deep_sweep_schedule`` is the canonical gate. See
    # c3 in the scheduling consolidation arc. When ``deep_sweep_schedule``
    # is present the interval value is ignored.
    deep_sweep_interval_hours: int = 24
    # Clock-aligned deep sweep. Default: 02:30 Halifax daily so the
    # LLM-heavy fix pipeline runs overnight and doesn't collide with
    # the user's working hours. Weekly variants supported via
    # day_of_week if a project ever needs them.
    deep_sweep_schedule: ScheduleConfig = field(
        default_factory=lambda: ScheduleConfig(
            time="02:30", timezone="America/Halifax",
        )
    )
    structural_only: bool = False
    stub_body_threshold_chars: int = 50
    orphan_exempt_dirs: list[str] = field(default_factory=lambda: ["view"])
    max_files_per_agent_call: int = 30
    fix_log_in_vault: bool = True
    drift_sweep_interval_hours: int = 168  # 7 days
    # Upstream #15: cost guards for Stage 3 stub enrichment.
    # max_stubs_per_sweep — cap the LLM calls per sweep.
    # max_enrichment_attempts — stop retrying after N failed attempts;
    # a content-hash change resets the counter.
    max_stubs_per_sweep: int = 10
    max_enrichment_attempts: int = 3


@dataclass
class StateConfig:
    # Tool-scoped default; see ``distiller/config.py`` for rationale.
    path: str = "./data/janitor_state.json"
    max_sweep_history: int = 20


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "./data/janitor.log"


@dataclass
class IdleTickConfig:
    """Janitor idle-tick heartbeat — "intentionally left blank" liveness signal.

    A periodic ``janitor.idle_tick`` log event so observers can distinguish
    *idle / healthy* from *broken*. Without it, a quiet stretch (no issues
    fixed) is indistinguishable from a hung daemon. See
    ``src/alfred/common/heartbeat.py`` for the rationale and cadence.

    Counter semantic: one issue fixed (or deleted) = one event. Sweep
    counts that find nothing broken don't add noise to the heartbeat.

    Defaults are deliberately on — the cost is negligible (~290 KB/day at
    60s) and the diagnostic value compounds.
    """

    enabled: bool = True
    interval_seconds: int = 60


@dataclass
class JanitorConfig:
    vault: VaultConfig = field(default_factory=VaultConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    sweep: SweepConfig = field(default_factory=SweepConfig)
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
    "sweep": SweepConfig,
    "deep_sweep_schedule": ScheduleConfig,
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


def load_config(path: str | Path = "config.yaml") -> JanitorConfig:
    """Load and parse config.yaml into JanitorConfig."""
    from alfred.vault.config_helpers import normalize_vault_block

    config_path = Path(path)
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw = _substitute_env(raw or {})
    if "vault" in raw:
        raw["vault"] = normalize_vault_block(raw["vault"])
    return _build(JanitorConfig, raw)


def load_from_unified(raw: dict[str, Any]) -> JanitorConfig:
    """Build JanitorConfig from a pre-loaded unified config dict."""
    from alfred.vault.config_helpers import normalize_vault_block

    raw = _substitute_env(raw)
    tool = raw.get("janitor", {})
    # Map unified logging.dir -> logging.file
    log_raw = dict(raw.get("logging", {}))
    log_dir = log_raw.pop("dir", "./data")
    if "file" not in log_raw:
        log_raw["file"] = f"{log_dir}/janitor.log"
    built: dict[str, Any] = {
        "vault": normalize_vault_block(raw.get("vault", {})),
        "agent": raw.get("agent", {}),
        "sweep": tool.get("sweep", {}),
        "state": tool.get("state", {}),
        "logging": log_raw,
    }
    # Idle-tick — defaulted-on; partial dict merges over dataclass default.
    idle_raw = tool.get("idle_tick")
    if isinstance(idle_raw, dict):
        built["idle_tick"] = idle_raw
    return _build(JanitorConfig, built)
