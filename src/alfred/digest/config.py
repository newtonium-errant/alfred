"""Digest config — schedule + output path.

Top-level ``digest`` block in the unified config::

    digest:
      enabled: false
      schedule:
        time: "07:00"
        timezone: "America/Halifax"
        day_of_week: "sunday"
      output_dir: "/home/andrew/aftermath-lab/digests"
      window_days: 7

Default ``enabled: false`` so subordinate instances that don't want
digests don't fire one. The Sunday morning slot is the ratified
cadence; ``output_dir`` defaults to ``~/aftermath-lab/digests`` (the
canonical home).

The list of projects-to-scan reuses the ``kalle.projects`` map from
:mod:`alfred.reviews.config` — both capabilities operate over the
same project universe.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from alfred.common.schedule import ScheduleConfig

ENV_RE = re.compile(r"\$\{(\w+)\}")


def _substitute_env(value: Any) -> Any:
    if isinstance(value, str):
        def _replace(m: re.Match) -> str:
            return os.environ.get(m.group(1), m.group(0))
        return ENV_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


def _default_output_dir() -> str:
    return str(Path.home() / "aftermath-lab" / "digests")


def _default_synthesis_vault() -> str:
    """Where the synthesis ranker reads distilled records from.

    Defaults to KAL-LE's vault root (``~/aftermath-lab``). The ranker
    walks ``synthesis/``, ``decision/``, and ``contradiction/`` under
    this root for the digest's section 4 ("Cross-arc patterns").
    """
    return str(Path.home() / "aftermath-lab")


@dataclass
class DigestConfig:
    """Top-level digest config."""

    enabled: bool = False
    schedule: ScheduleConfig = field(
        default_factory=lambda: ScheduleConfig(
            time="07:00", timezone="America/Halifax", day_of_week="sunday",
        ),
    )
    output_dir: str = field(default_factory=_default_output_dir)
    window_days: int = 7
    # Phase 2 — synthesis ranker source vault. Distinct from
    # ``output_dir`` because the ranker reads distilled-learn records,
    # not the digest's own output. Defaults to the canonical KAL-LE
    # vault (``~/aftermath-lab``).
    synthesis_vault: str = field(default_factory=_default_synthesis_vault)
    # Top N records the ranker surfaces into section 4. 0 disables the
    # ranker call entirely (section renders the empty-state message).
    synthesis_top_n: int = 12
    # Operator-tunable weight overrides. See
    # :mod:`alfred.distiller.synthesis_ranker` for the four supported
    # keys (``cross_source``, ``entity_diversity``, ``recency``,
    # ``type``) and their defaults. Empty dict → ranker defaults.
    synthesis_weights: dict[str, float] = field(default_factory=dict)


def load_from_unified(raw: dict[str, Any]) -> DigestConfig:
    raw = _substitute_env(raw or {})
    section = raw.get("digest", {}) or {}
    if not section:
        return DigestConfig(enabled=False)
    schedule_raw = section.get("schedule", {}) or {}
    schedule = ScheduleConfig(
        time=str(schedule_raw.get("time", "07:00")),
        timezone=str(schedule_raw.get("timezone", "America/Halifax")),
        day_of_week=schedule_raw.get("day_of_week", "sunday"),
    )
    weights_raw = section.get("synthesis_weights") or {}
    if not isinstance(weights_raw, dict):
        weights_raw = {}
    return DigestConfig(
        enabled=bool(section.get("enabled", False)),
        schedule=schedule,
        output_dir=str(section.get("output_dir") or _default_output_dir()),
        window_days=int(section.get("window_days", 7)),
        synthesis_vault=str(
            section.get("synthesis_vault") or _default_synthesis_vault()
        ),
        synthesis_top_n=int(section.get("synthesis_top_n", 12)),
        synthesis_weights=dict(weights_raw),
    )
