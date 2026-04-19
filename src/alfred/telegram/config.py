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
    ignore_dirs: list[str] = field(default_factory=lambda: [".obsidian"])

    @property
    def vault_path(self) -> Path:
        return Path(self.path)


@dataclass
class AnthropicConfig:
    api_key: str = ""
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 4096
    temperature: float = 0.7


@dataclass
class STTConfig:
    provider: str = "groq"
    api_key: str = ""
    model: str = "whisper-large-v3"


@dataclass
class SessionConfig:
    gap_timeout_seconds: int = 1800
    state_path: str = "./data/talker_state.json"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "./data/talker.log"


@dataclass
class InstanceConfig:
    """Per-instance persona identity for the talker.

    ``name`` is the casual, greeting-friendly form ("Alfred", "Salem").
    ``canonical`` is the formal form used once in the SKILL's identity
    paragraph ("Alfred", "S.A.L.E.M."). ``aliases`` is unused at this
    stage — reserved for the multi-instance router (see
    ``memory/project_multi_instance_design.md``) so case-insensitive
    inbound routing can accept phone-autocorrect-friendly variants
    (``"Salem"`` for ``S.A.L.E.M.``) without a code change.
    """

    name: str = "Alfred"
    canonical: str = "Alfred"
    aliases: list[str] = field(default_factory=list)


@dataclass
class TalkerConfig:
    bot_token: str = ""
    allowed_users: list[int] = field(default_factory=list)
    primary_users: list[str] = field(default_factory=list)
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    vault: VaultConfig = field(default_factory=VaultConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    instance: InstanceConfig = field(default_factory=InstanceConfig)


# --- Recursive builder ---

_DATACLASS_MAP: dict[str, type] = {
    "vault": VaultConfig,
    "anthropic": AnthropicConfig,
    "stt": STTConfig,
    "session": SessionConfig,
    "logging": LoggingConfig,
    "instance": InstanceConfig,
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


def load_config(path: str | Path = "config.yaml") -> TalkerConfig:
    """Load and parse config.yaml into TalkerConfig."""
    config_path = Path(path)
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw = _substitute_env(raw or {})
    return load_from_unified(raw)


def load_from_unified(raw: dict[str, Any]) -> TalkerConfig:
    """Build TalkerConfig from a pre-loaded unified config dict."""
    raw = _substitute_env(raw)
    tool = dict(raw.get("telegram", {}) or {})
    vault_raw = dict(raw.get("vault", {}) or {})
    # Strip vault keys that don't exist on our trimmed VaultConfig.
    vault_raw.pop("inbox_dir", None)
    vault_raw.pop("processed_dir", None)
    vault_raw.pop("ignore_files", None)
    # Map unified logging.dir -> logging.file
    log_raw = dict(raw.get("logging", {}) or {})
    log_dir = log_raw.pop("dir", "./data")
    if "file" not in log_raw:
        log_raw["file"] = f"{log_dir}/talker.log"

    return _build(TalkerConfig, {
        "bot_token": tool.get("bot_token", ""),
        "allowed_users": tool.get("allowed_users", []) or [],
        "primary_users": tool.get("primary_users", []) or [],
        "anthropic": tool.get("anthropic", {}) or {},
        "stt": tool.get("stt", {}) or {},
        "session": tool.get("session", {}) or {},
        "vault": vault_raw,
        "logging": log_raw,
        "instance": tool.get("instance", {}) or {},
    })
