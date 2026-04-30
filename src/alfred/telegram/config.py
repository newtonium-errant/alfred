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
    # Phase 2 deferred-enhancement #1 (per ``project_hypatia_phase2_followups.md``):
    # when true, after a substantive session closes, the talker invokes a
    # short LLM call to derive a 3-5 word topic slug from the transcript
    # and renames the session record so the filename reflects what the
    # session was *about* (not the opening greeting). Defaults OFF for
    # safety — only opt-in instances (Hypatia in Phase 2) flip it. Failure
    # is isolated: if derivation errors, the original opening-text slug
    # is preserved and a warning is logged.
    derive_slug_from_substance: bool = False


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "./data/talker.log"


@dataclass
class TtsConfig:
    """ElevenLabs TTS config for the wk2b ``/brief`` command.

    ``voice_id`` accepts either an ElevenLabs canonical voice id (e.g.
    ``21m00Tcm4TlvDq8ikWAM`` for Rachel) or a friendly name (``"Rachel"``)
    which the synthesiser resolves via a lookup at call time. The
    friendly-name path is offered because ElevenLabs voice ids are
    opaque and unmemorable; config-by-name lets users read their
    config at a glance.
    """

    provider: str = "elevenlabs"
    api_key: str = ""
    model: str = "eleven_turbo_v2_5"
    voice_id: str = "Rachel"
    summary_word_target: int = 300


@dataclass
class InstanceConfig:
    """Per-instance persona identity for the talker.

    ``name`` is the casual, greeting-friendly form ("Salem", "KAL-LE",
    "Hypatia"). ``canonical`` is the formal form used once in the SKILL's
    identity paragraph ("S.A.L.E.M.", "K.A.L.L.E.", "H.Y.P.A.T.I.A.").
    ``aliases`` is the multi-instance router's case-insensitive accept
    list so phone-autocorrect / voice-transcription variants still route
    correctly (``"Salem"`` → S.A.L.E.M., ``"Pat"`` → Hypatia) without a
    code change.

    ``name`` is **required** (no default). "Alfred" is the project /
    architecture name, never an instance name — defaulting to it
    silently mis-attributes prose ("Alfred's earlier message" on a
    Salem-installed bot) and silently misconfigures peer-protocol
    identification. A config YAML without ``instance.name`` raises
    ``TypeError`` at load time. See
    ``feedback_hardcoding_and_alfred_naming.md``.

    ``skill_bundle`` picks which SKILL bundle the talker loads at
    startup (``"vault-talker"`` for Salem, ``"vault-kalle"`` for KAL-LE,
    ``"vault-hypatia"`` for Hypatia). The bundle name resolves to
    ``src/alfred/_bundled/skills/<skill_bundle>/SKILL.md``.

    ``tool_set`` selects which vault-bridge tool schema the talker
    exposes to the model AND which scope the dispatcher routes to in
    ``conversation._execute_tool``. ``"talker"`` (default) uses
    ``TALKER_VAULT_TOOLS``; ``"kalle"`` adds ``bash_exec``;
    ``"hypatia"`` reuses the talker tool list with the hypatia scope.
    Callers read ``conversation.VAULT_TOOLS_BY_SET``.
    """

    name: str
    canonical: str = ""
    aliases: list[str] = field(default_factory=list)
    skill_bundle: str = "vault-talker"
    tool_set: str = "talker"


@dataclass
class IdleTickConfig:
    """Talker idle-tick heartbeat — "intentionally left blank" liveness signal.

    A periodic ``talker.idle_tick`` log event so observers can distinguish
    *idle / healthy* from *broken*. Without it, a quiet stretch (no inbound
    traffic) is indistinguishable from a hung daemon. See
    ``src/alfred/telegram/heartbeat.py`` for the rationale and the cadence-
    rationale comment block.

    Defaults are deliberately on — the cost is negligible (~290 KB/day at
    60s) and the diagnostic value compounds.
    """

    enabled: bool = True
    interval_seconds: int = 60


@dataclass
class BashExecConfig:
    """KAL-LE's ``bash_exec`` tool config.

    ``audit_path`` is the JSONL log path every bash_exec invocation
    appends to. Separate from the main talker/transport audit logs
    because security review treats this one as high-sensitivity (code-
    mutation history).
    """

    audit_path: str = "./data/bash_exec.jsonl"
    # Timeout + output caps are enforced as hard-coded constants in
    # ``bash_exec.py`` — they're invariants, not config. Putting them
    # here would invite "let's just raise the timeout" which breaks
    # the safety contract. Config only carries the audit path.


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
    # wk2b c5: ``tts`` is optional — absent means /brief falls back to
    # "not configured" reply, not a hard failure. ``None`` sentinel kept
    # as a default so health.py can distinguish "section missing" from
    # "section present with empty fields".
    tts: TtsConfig | None = None
    # Stage 3.5: bash_exec — only relevant when
    # ``instance.tool_set == "kalle"``. Absent on Salem, present on
    # KAL-LE with a KAL-LE-specific audit path.
    bash_exec: BashExecConfig | None = None
    # Idle-tick heartbeat — see :class:`IdleTickConfig`. Defaulted-on
    # via the dataclass default_factory; absent block in YAML keeps
    # ``enabled=True`` / ``interval_seconds=60``.
    idle_tick: IdleTickConfig = field(default_factory=IdleTickConfig)


# --- Recursive builder ---

_DATACLASS_MAP: dict[str, type] = {
    "vault": VaultConfig,
    "anthropic": AnthropicConfig,
    "stt": STTConfig,
    "session": SessionConfig,
    "logging": LoggingConfig,
    "instance": InstanceConfig,
    "tts": TtsConfig,
    "bash_exec": BashExecConfig,
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

    # wk2b c5: the ``tts`` section is optional. If omitted we leave the
    # field as ``None`` so health probes + /brief handler can distinguish
    # "not configured" from "configured with empty values".
    tts_raw = tool.get("tts")
    built = _build(TalkerConfig, {
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
    if isinstance(tts_raw, dict) and tts_raw:
        built.tts = _build(TtsConfig, tts_raw)
    bash_raw = tool.get("bash_exec")
    if isinstance(bash_raw, dict) and bash_raw:
        built.bash_exec = _build(BashExecConfig, bash_raw)
    # Idle-tick — defaulted-on; if the user provides a partial dict
    # (just ``enabled: false``), merge over the dataclass default.
    idle_raw = tool.get("idle_tick")
    if isinstance(idle_raw, dict):
        built.idle_tick = _build(IdleTickConfig, idle_raw)
    return built
