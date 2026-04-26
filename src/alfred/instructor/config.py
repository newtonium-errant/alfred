"""Load unified config.yaml into InstructorConfig dataclasses.

Same pattern as every other tool: ``load_from_unified(raw)`` takes the
pre-parsed unified config dict and returns a typed ``InstructorConfig``.
Environment variables are substituted via ``${VAR}`` syntax before the
dataclasses are built.

The instructor daemon polls vault records every ``poll_interval_seconds``
for pending ``alfred_instructions`` lists. Each directive is executed
in-process via the Anthropic Python SDK (``api_key`` resolved from the
``anthropic`` section) with retry + destructive-keyword-dry-run guards.
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


# --- Defaults ---------------------------------------------------------------

# The destructive-keyword gate. If a directive text contains any of these
# substrings (case-insensitive), the executor runs a dry-run pass first
# and writes the plan to ``alfred_instructions_last`` without executing.
# The operator then re-issues a more specific directive to confirm.
#
# Kept as a tuple so callers can't accidentally mutate it. The list is
# deliberately conservative — false positives cost one extra dry-run
# round-trip; false negatives could delete a record.
_DEFAULT_DESTRUCTIVE_KEYWORDS: tuple[str, ...] = (
    "delete",
    "remove",
    "drop",
    "purge",
    "wipe",
    "clear all",
)


# --- Dataclasses ------------------------------------------------------------


@dataclass
class VaultConfig:
    """Vault section — same shape as other tools' VaultConfig."""

    path: str = ""
    ignore_dirs: list[str] = field(
        default_factory=lambda: [".obsidian", "_templates", "_bases"],
    )
    ignore_files: list[str] = field(default_factory=list)

    @property
    def vault_path(self) -> Path:
        return Path(self.path)


@dataclass
class AnthropicConfig:
    """Anthropic SDK config — explicit api_key + model, no env var leak.

    The instructor executor builds an ``AsyncAnthropic(api_key=...)`` so
    the key from config lands directly on the SDK client. ``ANTHROPIC_*``
    environment variables are NOT stripped from the child process — there
    is no child process: the SDK call is in-process. The explicit api_key
    argument to the SDK constructor takes precedence over env vars anyway.
    """

    api_key: str = ""
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 4096


@dataclass
class InstanceConfig:
    """Per-instance persona identity for the instructor SKILL.

    Mirrors the talker's ``InstanceConfig`` — ``{{instance_name}}`` and
    ``{{instance_canonical}}`` placeholders in the SKILL.md are
    substituted at load time so a multi-instance deploy can give each
    instance its own identity without forking the skill file.

    ``name`` is **required** (no default). "Alfred" is the project /
    architecture name, never an instance name — defaulting to it
    silently produces wrong-looking SKILL prose. See
    ``feedback_hardcoding_and_alfred_naming.md``.
    """

    name: str
    canonical: str = ""


@dataclass
class StateConfig:
    path: str = "./data/instructor_state.json"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "./data/instructor.log"


@dataclass
class IdleTickConfig:
    """Instructor idle-tick heartbeat — "intentionally left blank" liveness signal.

    A periodic ``instructor.idle_tick`` log event so observers can distinguish
    *idle / healthy* from *broken*. Without it, a stretch with no pending
    directives is indistinguishable from a hung daemon.

    Counter semantic: one directive executed = one event. Poll ticks that
    find no work add zero, so the heartbeat reflects meaningful work, not
    poll noise.

    Defaults are deliberately on — see ``src/alfred/common/heartbeat.py``
    for the cadence rationale.
    """

    enabled: bool = True
    interval_seconds: int = 60


@dataclass
class InstructorConfig:
    """Typed config for the instructor daemon."""

    vault: VaultConfig = field(default_factory=VaultConfig)
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    instance: InstanceConfig = field(default_factory=InstanceConfig)
    state: StateConfig = field(default_factory=StateConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    # Poll interval — how often the daemon scans the vault for new
    # alfred_instructions entries.
    poll_interval_seconds: int = 60

    # Executor retry cap. After this many consecutive failures on the
    # same directive, the directive is dropped from alfred_instructions
    # and surfaced to a new ``alfred_instructions_error`` frontmatter
    # field so the operator sees it.
    max_retries: int = 3

    # Body-append audit window. Each executed directive leaves a 1-line
    # ``<!-- ALFRED:INSTRUCTION ... -->`` comment at the bottom of the
    # target record. Older blocks beyond this count are pruned.
    audit_window_size: int = 5

    # Destructive-keyword gate — substrings that trigger a dry-run pass.
    # Case-insensitive. Kept as a tuple to prevent accidental mutation.
    destructive_keywords: tuple[str, ...] = _DEFAULT_DESTRUCTIVE_KEYWORDS

    # Idle-tick heartbeat — see :class:`IdleTickConfig`. Defaulted-on
    # via the dataclass default_factory; absent block in YAML keeps
    # ``enabled=True`` / ``interval_seconds=60``.
    idle_tick: IdleTickConfig = field(default_factory=IdleTickConfig)


# --- Recursive builder ------------------------------------------------------


_DATACLASS_MAP: dict[str, type] = {
    "vault": VaultConfig,
    "anthropic": AnthropicConfig,
    "instance": InstanceConfig,
    "state": StateConfig,
    "logging": LoggingConfig,
    "idle_tick": IdleTickConfig,
}


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Recursively construct a dataclass from a dict.

    Unknown keys on the top-level class are tolerated only for keys that
    map to a known nested dataclass — everything else raises TypeError
    via the dataclass constructor. This matches the behaviour of the
    other tool configs (curator/janitor/distiller).
    """
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key in _DATACLASS_MAP and isinstance(value, dict):
            kwargs[key] = _build(_DATACLASS_MAP[key], value)
        elif key == "destructive_keywords" and isinstance(value, list):
            # YAML can only express lists — convert to tuple to preserve
            # our immutability contract. Strings only.
            kwargs[key] = tuple(str(v) for v in value)
        else:
            kwargs[key] = value
    return cls(**kwargs)


def load_config(path: str | Path = "config.yaml") -> InstructorConfig:
    """Load and parse config.yaml into a fully-built InstructorConfig."""
    config_path = Path(path)
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw = _substitute_env(raw or {})
    return load_from_unified(raw)


def load_from_unified(raw: dict[str, Any]) -> InstructorConfig:
    """Build InstructorConfig from a pre-loaded unified config dict.

    Extracts the ``instructor`` section. Shares the top-level ``vault``
    and ``logging`` sections with the other tools (matches the
    established pattern in curator/janitor/distiller).
    """
    raw = _substitute_env(raw)
    tool = raw.get("instructor", {}) or {}

    # Map unified logging.dir -> logging.file (same as janitor/curator).
    log_raw = dict(raw.get("logging", {}) or {})
    log_dir = log_raw.pop("dir", "./data")
    if "file" not in log_raw:
        log_raw["file"] = f"{log_dir}/instructor.log"

    merged: dict[str, Any] = {
        "vault": raw.get("vault", {}) or {},
        "logging": log_raw,
    }
    # Copy instructor-specific subsections/scalars through to _build.
    for key in (
        "anthropic", "instance", "state",
        "poll_interval_seconds", "max_retries",
        "audit_window_size", "destructive_keywords",
        "idle_tick",
    ):
        if key in tool:
            merged[key] = tool[key]

    return _build(InstructorConfig, merged)
