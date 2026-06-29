"""Daily Sync config — typed dataclasses + ``load_from_unified``.

Per-instance config block at the top level of the unified config:

```yaml
daily_sync:
  enabled: true
  schedule:
    time: "09:00"
    timezone: "America/Halifax"
  batch_size: 5
  corpus:
    path: "./data/email_calibration.salem.jsonl"
  confidence:
    high: false
    medium: false
    low: false
    spam: false
  state:
    path: "./data/daily_sync_state.json"
```

When the block is absent (or ``enabled: false``) the orchestrator does
not start the Daily Sync daemon, the slash commands report "not
configured", and the email classifier's few-shot rotation is silently
disabled (no corpus to read from).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml

from alfred.common.schedule import ScheduleConfig
from alfred.routine.match_calibration import (
    DEFAULT_PENDING_PATH as _ROUTINE_MATCH_PENDING_DEFAULT,
)

log = structlog.get_logger(__name__)

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
class CorpusConfig:
    """Path to the per-instance calibration corpus.

    Append-only JSONL. One row per Andrew-confirmed (or Andrew-corrected)
    classifier item. The Daily Sync writer appends; the classifier
    reader rotates the tail into its few-shot example slots.
    """

    path: str = "./data/email_calibration.salem.jsonl"


@dataclass
class ConfidenceConfig:
    """Per-tier confidence flags.

    Flipped via the ``/calibration_ok <tier>`` Telegram command and
    persisted to a small state file (NOT this dataclass — the dataclass
    only holds the seed values from config). The flags are read by
    surfacing consumers (c3/c4/c5) to gate per-tier surfacing on
    Andrew's explicit approval.
    """

    high: bool = False
    medium: bool = False
    low: bool = False
    spam: bool = False


@dataclass
class StateConfig:
    """Path to the Daily Sync state file.

    Holds: last-fired date, last batch (item index → record path), the
    Telegram message_id sequence of the most recent push (so the reply
    parser can match), and the persisted per-tier confidence flags.
    """

    path: str = "./data/daily_sync_state.json"


@dataclass
class AttributionConfig:
    """Attribution-audit section provider config (Phase 2 of audit arc).

    The Daily Sync's attribution-audit section reads
    ``attribution_audit`` frontmatter from across the vault and surfaces
    unconfirmed items for Andrew's per-item ``confirm`` / ``reject``.
    See ``src/alfred/daily_sync/attribution_section.py`` for the
    section provider and ``src/alfred/vault/attribution.py`` for the
    underlying primitives shipped in c1.

    ``scan_paths`` is empty by default → the section walks the whole
    vault. Restrict for performance once vault grows past ~10k records;
    typical entries are vault-relative subpaths like ``["note", "person"]``.
    """

    enabled: bool = True
    batch_size: int = 5
    scan_paths: list[str] = field(default_factory=list)
    # Audit corpus path — separate from the email calibration corpus
    # so the two streams stay independently auditable. Append-only
    # JSONL; one row per Andrew confirm or reject. The path is a
    # default; the production config may override it.
    corpus_path: str = "./data/attribution_audit_corpus.jsonl"


@dataclass
class FrictionThresholdsConfig:
    """Detection thresholds for the friction analyzer (K3 c1).

    Each threshold gates one friction-event category. Defaults match
    the K3 spec (3 failures / 5 successes / 24h window). Bumping any
    of these post-deploy raises the bar (fewer events surface);
    lowering it floods the queue. Tune based on operator-feedback
    signal-to-noise.
    """

    failed_pattern_count: int = 3
    repeated_pattern_count: int = 5
    window_hours: int = 24


@dataclass
class FrictionAnalyzerConfig:
    """Friction analyzer (K3 c1) config block.

    Reads KAL-LE's bash_exec.jsonl audit log, scores friction events
    along three categories (failed_pattern / repeated_pattern /
    missing_tool), and appends to the friction log file the section
    provider (K3 c2) reads from.

    ``audit_log_path`` empty string means "fall back to
    ``telegram.bash_exec.audit_path`` from the unified config" — this
    is the production case for KAL-LE. Operators can override per-
    instance for testing or split-log scenarios.

    ``log_path`` is the friction-event JSONL the section provider
    will read. Append-only; one row per friction event.

    ``enabled: false`` (default) is the per-instance opt-in switch.
    KAL-LE is the first instance to flip it on; Salem and Hypatia
    leave it absent (no bash_exec audit log → no friction surface).
    """

    enabled: bool = False
    schedule: ScheduleConfig = field(
        default_factory=lambda: ScheduleConfig(
            time="07:30", timezone="America/Halifax",
        )
    )
    audit_log_path: str = ""
    log_path: str = "./data/kalle_friction_log.jsonl"
    thresholds: FrictionThresholdsConfig = field(
        default_factory=FrictionThresholdsConfig,
    )


@dataclass
class RoutineMatchConfig:
    """Self-correcting routine matcher — Daily Sync surface (Phase 1).

    ``enabled`` defaults OFF — instances opt in via
    ``daily_sync.routine_match.enabled: true`` (Salem is the first, routine
    being Salem-only).

    **pending_path single-source contract (reviewer NOTE #2).** The routine
    CLI WRITES this capture sink (``routine.cli.cmd_done``); this Daily Sync
    section READS it — they MUST be the SAME file. Rather than hold an
    independently-defaulted duplicate (which silently drifts if an operator
    overrides ``routine.match_calibration.pending_path`` but forgets this one),
    :func:`load_from_unified` DERIVES this field from the routine tool's
    resolved config at LOAD time: absent an explicit
    ``daily_sync.routine_match.pending_path``, it tracks
    ``routine.match_calibration.pending_path`` (including operator overrides).
    The dataclass default below stays the shared
    ``routine.match_calibration.DEFAULT_PENDING_PATH`` constant as the
    final fallback. An explicit ``daily_sync.routine_match.pending_path`` is
    still honoured (for the operator who genuinely wants them split — an
    intentional, non-silent choice).
    """

    enabled: bool = False
    pending_path: str = _ROUTINE_MATCH_PENDING_DEFAULT


@dataclass
class DailySyncConfig:
    """Top-level Daily Sync config.

    ``enabled`` is the master switch — when False, the orchestrator skips
    starting the daemon and slash commands reply "not configured".
    """

    enabled: bool = False
    schedule: ScheduleConfig = field(
        default_factory=lambda: ScheduleConfig(time="09:00", timezone="America/Halifax"),
    )
    batch_size: int = 5
    corpus: CorpusConfig = field(default_factory=CorpusConfig)
    confidence: ConfidenceConfig = field(default_factory=ConfidenceConfig)
    state: StateConfig = field(default_factory=StateConfig)
    attribution: AttributionConfig = field(default_factory=AttributionConfig)
    # Friction analyzer / queue (K3) — defaulted-OFF; instances opt
    # in via ``daily_sync.friction_analyzer.enabled: true``. KAL-LE is
    # the first such instance.
    friction_analyzer: FrictionAnalyzerConfig = field(
        default_factory=FrictionAnalyzerConfig,
    )
    # Self-correcting routine matcher surface (Phase 1) — defaulted-OFF;
    # Salem opts in via ``daily_sync.routine_match.enabled: true``.
    routine_match: RoutineMatchConfig = field(
        default_factory=RoutineMatchConfig,
    )
    # Path to the config file this DailySyncConfig was loaded from.
    # Carried so lazy/late loaders (the canonical-proposals queue-path
    # helpers in ``canonical_proposals_section`` and ``reply_dispatch``)
    # can re-read the SAME config file at call time rather than
    # defaulting to ``config.yaml``. Without this, a Hypatia daily_sync
    # daemon (started with ``--config config.hypatia.yaml``) would have
    # its queue-path helpers silently fall back to Salem's transport
    # config and look up the wrong proposals JSONL. ``None`` is the
    # backward-compat default — populated by :func:`load_config` (path
    # arg known directly) and by :func:`load_from_unified` when the raw
    # dict carries the synthetic ``_config_path`` key (set by the CLI in
    # ``_load_unified_config`` before handing ``raw`` to the
    # orchestrator). Mirrors ``TalkerConfig.config_path`` shipped in
    # commit 420364b — same bug class, two more sites.
    config_path: str | None = None


_DATACLASS_MAP: dict[str, type] = {
    "schedule": ScheduleConfig,
    "corpus": CorpusConfig,
    "confidence": ConfidenceConfig,
    "state": StateConfig,
    "attribution": AttributionConfig,
    "friction_analyzer": FrictionAnalyzerConfig,
    "thresholds": FrictionThresholdsConfig,
    "routine_match": RoutineMatchConfig,
}


def _build(cls: type, data: dict[str, Any]) -> Any:
    """Recursively construct a dataclass from a dict.

    Unknown top-level keys are ignored so a future schema bump on
    ``config.yaml.example`` doesn't break parsing on installs pinned to
    an older copy of this code.
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


def load_from_unified(raw: dict[str, Any]) -> DailySyncConfig:
    """Build a DailySyncConfig from the unified config dict.

    Returns a default-constructed (``enabled=False``) config when the
    ``daily_sync`` block is absent. Callers can rely on ``.enabled`` to
    decide whether to wire downstream work.
    """
    raw = _substitute_env(raw)
    section = raw.get("daily_sync", {}) or {}
    if not section:
        cfg = DailySyncConfig(enabled=False)
    else:
        cfg = _build(DailySyncConfig, section)
    # Single-source the routine_match pending_path (reviewer NOTE #2). The
    # routine CLI WRITES this capture sink; this section READS it — same file.
    # Derive the default from the routine tool's resolved config so an operator
    # override of ``routine.match_calibration.pending_path`` propagates here
    # instead of silently drifting. An explicit
    # ``daily_sync.routine_match.pending_path`` is honoured (intentional split).
    # See ``RoutineMatchConfig`` for the full contract; mirrors the Phase-2b
    # corpus_path single-source (reply_dispatch reads the routine config).
    rm_section = section.get("routine_match") if isinstance(section, dict) else None
    rm_explicit = isinstance(rm_section, dict) and "pending_path" in rm_section
    if not rm_explicit:
        try:
            from alfred.routine.config import load_from_unified as _load_routine

            cfg.routine_match.pending_path = (
                _load_routine(raw).match_calibration.pending_path
            )
        except Exception as exc:  # noqa: BLE001
            # Never let routine-config resolution break daily_sync load — keep
            # the dataclass default (the shared constant, which is also what the
            # routine tool defaults to, so they still match in the no-override
            # case). Emit a debug line (reviewer ILB note on 9b89cb7): a real
            # routine-config breakage would silently re-introduce the read/write
            # drift via the constant fallback — this makes that diagnosable.
            log.debug(
                "daily_sync.routine_match.pending_path_derive_failed",
                error=str(exc),
            )
    # Synthetic ``_config_path`` key — set by the CLI in
    # ``_load_unified_config`` before handing ``raw`` to the
    # orchestrator, carried through ``multiprocessing`` pickling to
    # subprocess daemons. See ``DailySyncConfig.config_path`` for the
    # rationale (mirrors TalkerConfig.config_path shipped in 420364b).
    raw_path = raw.get("_config_path")
    if isinstance(raw_path, str) and raw_path:
        cfg.config_path = raw_path
    return cfg


def load_config(path: str | Path = "config.yaml") -> DailySyncConfig:
    """Load and parse a config file (test helper).

    Stamps the resolved absolute path onto ``cfg.config_path`` so lazy
    loaders (the canonical-proposals queue-path helpers) re-read the
    SAME file we just loaded — see ``DailySyncConfig.config_path`` for
    the rationale.
    """
    config_path = Path(path)
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    cfg = load_from_unified(raw or {})
    cfg.config_path = str(config_path.resolve())
    return cfg
