"""Email-filing config — typed dataclasses + ``load_from_unified``.

#7 7c-i. The topical-filing axis runs BESIDE the priority ``email_classifier`` (orthogonal — a filing
config or fault never perturbs the priority axis). Per-instance opt-in block at the top level:

```yaml
email_filing:
  enabled: true
  fallback_enabled: true          # LLM fallback fires ONLY on no-rule-match (the long tail)
  anthropic:
    api_key: "${ANTHROPIC_API_KEY}"
    model: "claude-sonnet-4-6"     # same model class as email_classifier — personal-email, not clinical
    max_tokens: 256
  calibration_corpus_path: "./data/email_calibration_corpus.jsonl"   # SHARED with email_classifier
  calibration_few_shot_count: 10
  rules_additions_path: "./data/email_filing_rules.json"             # read here; WRITTEN by 7c-i-b
```

When the block is absent (or ``enabled: false``) the curator's filing post-pass short-circuits — no rule
match, no LLM call, no ``email_category`` write. KAL-LE's config omits the block (no email pipeline).
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
class AnthropicConfig:
    """Anthropic SDK config for the LLM fallback. Mirrors email_classifier's — kept local so the filing
    axis carries no import dependency on the classifier module (structural orthogonality)."""

    api_key: str = ""
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 256
    timeout_seconds: int = 60


@dataclass
class EmailFilingConfig:
    """Top-level filing config.

    ``enabled`` is the master switch — False (or an absent block) makes the curator's filing post-pass a
    no-op. ``fallback_enabled`` gates ONLY the LLM fallback (the deterministic rule table always runs when
    enabled); turn it off to run rules-only. ``rules_additions_path`` is READ in 7c-i (seeds + additions)
    but empty/absent by default — the write side (approval CLI) is 7c-i-b.
    """

    enabled: bool = False
    fallback_enabled: bool = True
    anthropic: AnthropicConfig = field(default_factory=AnthropicConfig)
    # SHARED with email_classifier — one corpus carries both the priority and the category axes.
    calibration_corpus_path: str = ""
    calibration_few_shot_count: int = 10
    # Operator-approved rule additions (7c-i-b writes; 7c-i reads seeds + additions).
    rules_additions_path: str = "./data/email_filing_rules.json"


_DATACLASS_MAP: dict[str, type] = {"anthropic": AnthropicConfig}


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Recursively construct a dataclass from a dict. Unknown keys ignored (forward-compat)."""
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


def load_from_unified(raw: dict[str, Any]) -> EmailFilingConfig:
    """Build EmailFilingConfig from a pre-loaded unified config dict.

    Returns a default-constructed (``enabled=False``) config when the ``email_filing`` block is absent —
    callers rely on ``.enabled`` to decide whether to run the filing post-pass."""
    raw = _substitute_env(raw)
    section = raw.get("email_filing", {}) or {}
    if not section:
        return EmailFilingConfig(enabled=False)
    return _build(EmailFilingConfig, section)


def load_config(path: str | Path = "config.yaml") -> EmailFilingConfig:
    """Load and parse a config file into EmailFilingConfig (test helper)."""
    config_path = Path(path)
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return load_from_unified(raw or {})
