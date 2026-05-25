"""Load config.yaml into typed dataclasses with env-var substitution."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

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
    inbox_dir: str = "inbox"
    processed_dir: str = "inbox/processed"
    # See ``alfred.vault.config_helpers`` for the dont_scan/dont_index split.
    # ``ignore_dirs`` is the legacy field, semantically equivalent to
    # ``dont_scan_dirs`` (outbound scan exclusion). New code should prefer
    # ``dont_scan_dirs``; both are kept in sync by ``normalize_vault_block``.
    ignore_dirs: list[str] = field(default_factory=lambda: [".obsidian"])
    # New (2026-05-01) — see vault/config_helpers.py for the rationale.
    dont_scan_dirs: list[str] | None = None
    dont_index_dirs: list[str] = field(default_factory=list)

    @property
    def vault_path(self) -> Path:
        return Path(self.path)

    @property
    def inbox_path(self) -> Path:
        return self.vault_path / self.inbox_dir

    @property
    def processed_path(self) -> Path:
        return self.vault_path / self.processed_dir


@dataclass
class ClaudeBackendConfig:
    command: str = "claude"
    # ``--exclude-dynamic-system-prompt-sections`` (Claude Code CLI):
    # moves per-machine sections (cwd, env info, memory paths, git status)
    # out of the system prompt and into the first user message. The
    # documented purpose is to "improve cross-user prompt-cache reuse"
    # — for the daemon use case (many ``claude -p`` dispatches per day
    # against a stable system prompt) this also improves cache hit rate
    # across consecutive dispatches from the SAME machine, since the
    # volatile sections no longer break the system-prompt cache prefix.
    # The flag is documented to only apply when the default system prompt
    # is used (which the daemons do — we don't pass ``--system-prompt``).
    # Per Q4 SKILL.md caching investigation 2026-05-25: cache_control
    # breakpoints on user-prompt content (the SKILL.md text we send via
    # stdin) are NOT controllable through the CLI surface — that would
    # require an upstream Claude Code SDK feature. This flag is the
    # available lever today.
    args: list[str] = field(
        default_factory=lambda: ["-p", "--exclude-dynamic-system-prompt-sections"]
    )
    timeout: int = 300
    allowed_tools: list[str] = field(default_factory=lambda: ["Bash"])


@dataclass
class AgentConfig:
    """Agent backend selector.

    Post backend-abstraction-collapse (2026-05-25): ``claude`` is the
    only surviving backend. The ``backend`` field is retained as a
    fail-loud guard against stale config values (zo / openclaw /
    hermes are rejected at startup by ``_create_backend``) and to
    leave the door open for future re-introductions (Q3 MCP / local
    Ollama agent backend) which would extend the field's value set.

    ZoBackendConfig / OpenClawBackendConfig / HermesBackendConfig
    dataclasses were removed in the same arc; if a re-introduced
    backend needs its own config block, add a fresh dataclass + a
    field here in the same commit.
    """

    backend: str = "claude"
    claude: ClaudeBackendConfig = field(default_factory=ClaudeBackendConfig)


@dataclass
class WatcherConfig:
    poll_interval: int = 5
    debounce_seconds: int = 10
    rescan_interval: int = 60
    # Max number of inbox files to process concurrently via asyncio.gather.
    # Per-file processing is bounded by a semaphore — backend calls (CLI
    # subprocesses or HTTP) run in parallel up to this limit. Ref upstream
    # 163b7f9; default matches upstream.
    max_concurrent: int = 4


@dataclass
class StateConfig:
    # Tool-scoped default; see ``distiller/config.py`` for rationale.
    path: str = "./data/curator_state.json"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "./data/curator.log"


@dataclass
class IdleTickConfig:
    """Curator idle-tick heartbeat — "intentionally left blank" liveness signal.

    A periodic ``curator.idle_tick`` log event so observers can distinguish
    *idle / healthy* from *broken*. Without it, a stretch with no inbox
    files arriving is indistinguishable from a hung daemon. See
    ``src/alfred/common/heartbeat.py`` for the rationale and the cadence-
    rationale comment block.

    Defaults are deliberately on — the cost is negligible (~290 KB/day at
    60s) and the diagnostic value compounds.
    """

    enabled: bool = True
    interval_seconds: int = 60


@dataclass
class CuratorConfig:
    vault: VaultConfig = field(default_factory=VaultConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    watcher: WatcherConfig = field(default_factory=WatcherConfig)
    state: StateConfig = field(default_factory=StateConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    # Skip Stage 4 entity enrichment to reduce token consumption.
    # Entities keep their stub content from Stage 2 (includes description
    # from the manifest). Re-enable by setting to False if richer entity
    # bodies are needed. Ref: ssdavidai/alfred#14
    skip_entity_enrichment: bool = True
    # Idle-tick heartbeat — see :class:`IdleTickConfig`. Defaulted-on
    # via the dataclass default_factory; absent block in YAML keeps
    # ``enabled=True`` / ``interval_seconds=60``.
    idle_tick: IdleTickConfig = field(default_factory=IdleTickConfig)


# --- Recursive builder ---

_DATACLASS_MAP: dict[str, type] = {
    "vault": VaultConfig,
    "agent": AgentConfig,
    "claude": ClaudeBackendConfig,
    "watcher": WatcherConfig,
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


def _strip_logging_extras(log_raw: dict[str, Any]) -> dict[str, Any]:
    """Drop keys from ``log_raw`` that ``LoggingConfig`` doesn't know about.

    The unified ``logging`` block in config.yaml carries fields the
    orchestrator consumes directly (``dir``, ``rotation``) that aren't
    fields on the typed ``LoggingConfig`` dataclass (only ``level`` +
    ``file``). Without this filter, ``_build(LoggingConfig, ...)``
    crashes with ``TypeError: ... unexpected keyword argument
    'rotation'`` whenever an operator pulls ``config.yaml.example``'s
    rotation block. Pre-dispatch strip keeps the typed config slim and
    routes rotation through the orchestrator / ``__main__.py``
    ``extract_rotation_config`` path.
    """
    known = set(LoggingConfig.__dataclass_fields__)
    return {k: v for k, v in log_raw.items() if k in known}


def load_config(path: str | Path = "config.yaml") -> CuratorConfig:
    """Load and parse config.yaml into CuratorConfig."""
    from alfred.vault.config_helpers import normalize_vault_block

    config_path = Path(path)
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw = _substitute_env(raw or {})
    if "vault" in raw:
        raw["vault"] = normalize_vault_block(raw["vault"])
    # Strip orchestrator-only logging keys (``rotation``, ``dir``) so
    # ``_build(LoggingConfig, ...)`` doesn't crash on the example
    # config's rotation block. See ``_strip_logging_extras``.
    if isinstance(raw.get("logging"), dict):
        raw["logging"] = _strip_logging_extras(raw["logging"])
    return _build(CuratorConfig, raw)


def load_from_unified(raw: dict[str, Any]) -> CuratorConfig:
    """Build CuratorConfig from a pre-loaded unified config dict."""
    from alfred.vault.config_helpers import normalize_vault_block

    raw = _substitute_env(raw)
    tool = raw.get("curator", {})
    vault_raw = normalize_vault_block(raw.get("vault", {}))
    vault_raw["inbox_dir"] = tool.get("inbox_dir", "inbox")
    vault_raw["processed_dir"] = tool.get("processed_dir", "inbox/processed")
    # Strip keys that don't exist in our VaultConfig
    vault_raw.pop("ignore_files", None)
    # Map unified logging.dir -> logging.file
    log_raw = dict(raw.get("logging", {}))
    log_dir = log_raw.pop("dir", "./data")
    if "file" not in log_raw:
        log_raw["file"] = f"{log_dir}/curator.log"
    # Drop ``rotation`` (orchestrator/__main__.py consume it separately
    # via ``extract_rotation_config``) so ``_build(LoggingConfig, ...)``
    # only sees fields it knows about.
    log_raw = _strip_logging_extras(log_raw)
    top_level: dict[str, Any] = {
        "vault": vault_raw,
        "agent": raw.get("agent", {}),
        "watcher": tool.get("watcher", {}),
        "state": tool.get("state", {}),
        "logging": log_raw,
    }
    # Pass through curator-level scalar flags (not nested dataclass sections).
    if "skip_entity_enrichment" in tool:
        top_level["skip_entity_enrichment"] = bool(tool["skip_entity_enrichment"])
    # Idle-tick — defaulted-on; if the user provides a partial dict
    # (just ``enabled: false``), merge over the dataclass default.
    idle_raw = tool.get("idle_tick")
    if isinstance(idle_raw, dict):
        top_level["idle_tick"] = idle_raw
    return _build(CuratorConfig, top_level)
