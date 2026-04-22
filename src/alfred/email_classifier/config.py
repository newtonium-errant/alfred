"""Email classifier config — typed dataclasses + ``load_from_unified``.

Per-instance config block at the top level of the unified config:

```yaml
email_classifier:
  enabled: true
  anthropic:
    api_key: "${ANTHROPIC_API_KEY}"
    model: "claude-sonnet-4-6"
    max_tokens: 1024
  prompt:
    high:
      - "From a named person Andrew has interacted with recently"
      - ...
    medium: [...]
    low: [...]
    spam: [...]
  state:
    path: "./data/email_classifier_state.json"
```

When the block is absent (or ``enabled: false``), the post-processor
hook in the curator daemon short-circuits — no LLM call, no frontmatter
mutation. KAL-LE's ``config.kalle.yaml`` deliberately omits the block.
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


# --- Cold-prompt seed for Salem (c1 default) -------------------------------

# Verbatim from project_email_surfacing.md. These cue groups are the
# starting rule set — calibration in c2 will accumulate corrections that
# feed back into the few-shot example slots, and a later phase rotates
# them into this list. For now they live in code as the dataclass
# defaults; instance configs can override the whole ``prompt`` block.

_DEFAULT_HIGH = (
    "From a named person Andrew has interacted with recently "
    "(vault-lookup for person records)",
    "Explicit time-pressure language (\"today\", \"by EOD\", \"urgent\", "
    "deadline within 48h)",
    "Reply-required signals (direct question to Andrew, RSVP request)",
    "Jamie Newton's messages",
    "RRTS customer messages",
)

_DEFAULT_MEDIUM = (
    "Appointments / confirmations / notices with future dates",
    "Subscription renewals",
    "Financial notifications (transactions, statements)",
    "Family mentions (Newton family members beyond Andrew/Jamie)",
)

_DEFAULT_LOW = (
    "Newsletters Andrew chose to receive",
    "Marketing from established relationships (vendors he uses)",
    "Automated notifications (system status, build emails, etc.)",
)

_DEFAULT_SPAM = (
    "Unsolicited commercial",
    "Phishing-shape messages",
    "Unknown senders pitching products / services",
)


# --- Dataclasses ------------------------------------------------------------


@dataclass
class AnthropicConfig:
    """Anthropic SDK config for the classifier.

    Mirrors ``instructor.AnthropicConfig`` — explicit api_key + model;
    the SDK call is in-process so the key from config goes straight onto
    the client constructor. ``max_tokens`` defaults are smaller than the
    instructor's because the classifier only emits a 4-field JSON object.
    """

    api_key: str = ""
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 1024
    timeout_seconds: int = 60


@dataclass
class PromptConfig:
    """Cue groups for each tier.

    Each list is a bag of natural-language cues passed to the model under
    a labeled section. The model picks the best tier match based on the
    email content. Lists may be overridden in YAML; if a list key is
    omitted the corresponding tuple default applies.
    """

    high: list[str] = field(default_factory=lambda: list(_DEFAULT_HIGH))
    medium: list[str] = field(default_factory=lambda: list(_DEFAULT_MEDIUM))
    low: list[str] = field(default_factory=lambda: list(_DEFAULT_LOW))
    spam: list[str] = field(default_factory=lambda: list(_DEFAULT_SPAM))


@dataclass
class StateConfig:
    path: str = "./data/email_classifier_state.json"


@dataclass
class EmailClassifierConfig:
    """Top-level classifier config.

    ``enabled`` is the master switch — when False, the post-processor
    short-circuits without making an LLM call. Use this on instances
    that have no email pipeline (KAL-LE) or to gate the feature off
    while debugging.

    ``named_contact_cache_seconds`` controls how long the person-record
    helper memoises the contact list. The classifier daemon's lifetime
    is short (one inbox file per call), so 0 means "no cache" — but
    batch curator runs reuse the cache to avoid scanning ``person/``
    repeatedly. Default of 60s is generous for batch runs.
    """

    enabled: bool = False
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    state: StateConfig = field(default_factory=StateConfig)
    named_contact_cache_seconds: int = 60
    # Sentinel value written to ``priority`` when the LLM call fails or
    # returns malformed JSON. Calibration in c2 can flag records with
    # this value as "needs reclassification" instead of treating them
    # like a real tier. Keep this stable — c2 + c3 will look for it.
    unclassified_sentinel: str = "unclassified"
    # c2 Phase 1 corpus → classifier feedback. When set, the classifier
    # rotates the most recent N entries from this JSONL into its system
    # prompt as few-shot examples. Empty string disables the rotation
    # (classifier falls back to the cold cue lists alone). Daily Sync
    # writes here; classifier reads. Decoupled from the daily_sync
    # config so a deployment can disable Daily Sync but keep the
    # accumulated corpus active.
    calibration_corpus_path: str = ""
    # How many corpus entries to inject as few-shot examples. 0 disables
    # injection even if the path is set.
    calibration_few_shot_count: int = 10


# --- Recursive builder ------------------------------------------------------


_DATACLASS_MAP: dict[str, type] = {
    "anthropic": AnthropicConfig,
    "prompt": PromptConfig,
    "state": StateConfig,
}


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Recursively construct a dataclass from a dict.

    Unknown top-level keys are ignored (rather than raising) so a future
    config schema bump on the example doesn't break parsing on installs
    pinned to an older copy of this code.
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


def load_from_unified(raw: dict[str, Any]) -> EmailClassifierConfig:
    """Build EmailClassifierConfig from a pre-loaded unified config dict.

    Returns a default-constructed (``enabled=False``) config when the
    ``email_classifier`` block is absent. Callers can rely on the
    ``.enabled`` flag to decide whether to run the post-processor.
    """
    raw = _substitute_env(raw)
    section = raw.get("email_classifier", {}) or {}
    if not section:
        return EmailClassifierConfig(enabled=False)
    return _build(EmailClassifierConfig, section)


def load_config(path: str | Path = "config.yaml") -> EmailClassifierConfig:
    """Load and parse a config file into EmailClassifierConfig (test helper)."""
    config_path = Path(path)
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return load_from_unified(raw or {})
