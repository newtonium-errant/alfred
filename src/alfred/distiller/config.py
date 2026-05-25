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
    #
    # See ``alfred.vault.config_helpers`` for the dont_scan/dont_index split.
    # ``ignore_dirs`` is the legacy field, semantically equivalent to
    # ``dont_scan_dirs`` (outbound scan exclusion). New code should prefer
    # ``dont_scan_dirs``; both are kept in sync by ``normalize_vault_block``.
    ignore_dirs: list[str] = field(default_factory=lambda: [".obsidian", "inbox/processed"])
    ignore_files: list[str] = field(default_factory=list)
    # New (2026-05-01) — see vault/config_helpers.py for the rationale.
    dont_scan_dirs: list[str] | None = None
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
class AgentConfig:
    """Agent backend selector.

    Post backend-abstraction-collapse (2026-05-25): ``claude`` is the
    only surviving backend. ZoBackendConfig / OpenClawBackendConfig
    dataclasses were removed in the same arc. Re-introducing a backend
    (Q3 MCP / local Ollama / etc.) extends this dataclass with a new
    field + a matching sibling module under ``backends/``.

    Note: the distiller's V2 non-agentic extractor (used when
    ``extraction.use_deterministic_v2`` is True or when the Anthropic
    SDK direct path is enabled) is configured via
    :class:`AnthropicConfig` and :class:`ExtractionConfig`, not through
    this dataclass. The two paths are independent — ``agent`` selects
    the agentic CLI backend, ``extraction`` selects the non-agentic
    extractor backend.
    """

    backend: str = "claude"
    claude: ClaudeBackendConfig = field(default_factory=ClaudeBackendConfig)


@dataclass
class ExtractionConfig:
    # Path C Phase 1 spike (2026-05-06) — backend selector for the
    # non-agentic v2 extractor. ``"anthropic"`` (default) uses the
    # Anthropic Messages API per ``backends/anthropic_sdk.py``;
    # ``"ollama"`` uses Ollama's OpenAI-compatible chat-completions
    # endpoint per ``backends/ollama.py``. Adding new backends is a
    # pure-extend: register a new value here + a new sibling module
    # in ``backends/``; ``_call_extraction_llm`` in ``extractor.py``
    # is the single dispatch point. Defaulted to ``"anthropic"`` so
    # existing config.yaml files load unchanged.
    backend: str = "anthropic"
    # Ollama endpoint (used only when ``backend == "ollama"``). Default
    # matches Ollama's standard local install. The spike harness
    # overrides via ``distiller.extraction.ollama_endpoint`` to point
    # at a different host (e.g. a Framework Desktop on the LAN).
    ollama_endpoint: str = "http://localhost:11434"
    # Default Ollama model — chosen for the spike's hardware-feasibility
    # test (qwen2.5:72b at q4_K_M is ~40GB, fits in 64GB unified memory
    # with overhead). The spike harness overrides per-run to compare
    # 7b / 14b / 32b / 72b candidates.
    ollama_model: str = "qwen2.5:72b-instruct-q4_K_M"
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
    # are written to ``shadow_root``. v2 never touches the live vault;
    # widening ``v2_types`` later costs only shadow re-writes, not
    # re-extractions (the extractor already paid the LLM cost on
    # the full set).
    # ``v2_types`` filters OUTPUT (learning) types — "assumption",
    # "decision", "constraint", "contradiction", "synthesis". NOT source
    # record types like "session" or "note" — sources aren't filtered at
    # the daemon layer; the extractor decides per-source what to emit.
    # Default c9 (2026-04-24): ALL FIVE learn types (full extraction).
    # Previously defaulted to ``["assumption"]`` as a Week-2 measurement
    # cap, but the Plan-agent diagnosis showed the filter was silently
    # dropping 3 of 4 extracted learnings (75% loss) — widening = ~4x
    # baseline lift for zero extra LLM cost. Operators CAN still narrow
    # via ``distiller.extraction.v2_types`` in config.yaml to restrict
    # Week-2-style measurement scope.
    # See docs/proposals/distiller-rebuild-team2-*.md for the rollout.
    use_deterministic_v2: bool = False
    shadow_root: str = "data/shadow/distiller"
    v2_types: list[str] = field(default_factory=lambda: [
        "assumption", "decision", "constraint", "contradiction", "synthesis",
    ])


@dataclass
class StateConfig:
    # Tool-scoped default to prevent cross-tool state-file collisions.
    # Multi-instance installs (Salem / KAL-LE / Hypatia) plus the four
    # tools (curator/janitor/distiller/surveyor) previously all defaulted
    # to ``./data/state.json``; the first tool to load whichever shape
    # was on disk would crash or silently load wrong-tool data. Salem's
    # legacy config explicitly overrides ``state.path``, so this default
    # only takes effect for fresh per-instance configs that don't pin it.
    path: str = "./data/distiller_state.json"
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
class RadarDayConfig:
    """Daily radar auto-fire — Phase 3a CLI on a scheduler.

    When ``enabled: true``, the orchestrator spawns a daemon that fires
    ``run_daily_radar`` once per day at ``schedule.time`` in the
    configured timezone. The daily file lands at
    ``<vault>/digests/daily/YYYY-MM-DD.md`` per Phase 3a; the surfaced-
    log dedup guarantees an item that surfaced earlier in the week
    doesn't re-surface.

    Default schedule (08:00 ADT) sits 1h ahead of KAL-LE's Daily Sync
    at 09:00 ADT so the radar provider has a freshly-written daily
    file to read. It also sits ~4.5h after the 03:30 distiller
    deep_extraction so the latest synthesis records are eligible.

    ``enabled`` defaults to False so instances that don't run radar
    (Salem, Hypatia today) stay unaffected; only KAL-LE flips it on
    in its config.
    """

    enabled: bool = False
    schedule: ScheduleConfig = field(
        default_factory=lambda: ScheduleConfig(
            time="08:00", timezone="America/Halifax",
        )
    )
    # Optional overrides for the inner ``run_daily_radar`` call.
    # Defaults match the Phase 3a CLI defaults.
    top_n: int = 5
    min_score: float | None = None
    # Optional digests-dir override; default is ``<vault>/digests``
    # resolved at run time so the daemon doesn't need vault path here.
    digests_dir: str = ""
    # Optional state-dir override; default is the parent of
    # ``distiller.state.path`` resolved at run time.
    state_dir: str = ""


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
class PatternMinerOpenRouterConfig:
    """OpenAI-compatible LLM endpoint config for the Phase 4 drafter.

    Mirrors the surveyor's :class:`OpenRouterConfig` shape so a
    multi-instance deployment can reuse the same backend (e.g.
    KAL-LE's local Ollama at qwen2.5:14b) for both the labeler and
    the pattern-miner drafter without re-stating connection details.

    Empty ``base_url`` or empty ``model`` triggers the "drafter
    unavailable" fallback in :func:`mine_patterns` — proposals are
    still written, but with a placeholder paragraph the operator
    fills in manually. The API-key field is sent as a Bearer token
    when non-empty (Ollama accepts any value); leave empty for
    local Ollama.
    """

    api_key: str = ""
    base_url: str = ""
    model: str = ""


@dataclass
class PatternMinerStateConfig:
    """State-path config for the Phase 4 miner.

    Tool-scoped default per the load() schema-tolerance contract +
    the per-tool-state-path discipline in CLAUDE.md. Distinct file
    from the main distiller_state.json so a Phase 4 wipe doesn't
    invalidate the extraction-pipeline state.
    """

    path: str = "./data/pattern_miner_state.json"


@dataclass
class PatternMinerConfig:
    """Phase 4 embedding-pattern miner config.

    Defaults are conservative — the block is opt-in via
    ``enabled: true``. Salem omits the block; KAL-LE adds it in
    config.kalle.yaml. See ``project_kalle_phase4_pattern_miner.md``
    for the design memo.

    All defaults are instance-agnostic — no kalle / aftermath-lab
    literals. Per-instance values (state path, label denylist
    extensions) come from the config file at load time.
    """

    enabled: bool = False
    # Path to the surveyor's state JSON. Defaults to the surveyor's
    # default path so a co-located install (KAL-LE-style) needs no
    # override; per-instance configs (e.g. KAL-LE) point at their
    # own /home/andrew/.alfred/<instance>/data/surveyor_state.json.
    surveyor_state_path: str = "./data/surveyor_state.json"
    # Vault-relative dir the proposal markdown lands in. Resolved
    # against config.vault.path when relative; absolute paths used
    # as-is. The curator daemon's inbox watcher picks up files
    # written here.
    proposed_dir: str = "inbox/proposed-canonical"
    # Cluster-size gate threshold. Below this size, the cluster is
    # noise risk even when cohered. CLI --min-cluster-size overrides.
    min_cluster_size: int = 3
    # Dirs scanned for the no-canonical-match gate (Q3 rule 3 in the
    # design memo). The miner walks these for slug stems and treats
    # any cluster whose label-segments match an existing slug as
    # "already canonized." KAL-LE's three are the default; Salem
    # would override if it ever runs Phase 4.
    canonical_match_dirs: list[str] = field(default_factory=lambda: [
        "architecture", "principles", "stack",
    ])
    # Operator-extended label denylist. Union'd with the module-level
    # default in pattern_miner.py at run time. Use this to add
    # instance-specific low-signal labels surveyor sometimes emits
    # without redefining the default set.
    label_denylist: list[str] = field(default_factory=list)
    # Stage 2e (2026-05-11) — Jaccard similarity threshold for the
    # semantic-dupe gate. Candidate clusters whose member-set Jaccard
    # against any terminal-status (promoted/discarded) state entry
    # meets or exceeds this value are rejected as semantic duplicates.
    # Default 0.5 matches the dispatch's calibration; operators can
    # tune in per-instance config (raise to 0.7 for tighter rejection,
    # lower to 0.3 to catch looser overlaps at the cost of more
    # false rejects). Pre-stage-2e state entries with empty
    # source_member_files default to Jaccard 0.0 — they don't
    # contribute false rejects.
    jaccard_threshold: float = 0.5
    state: PatternMinerStateConfig = field(default_factory=PatternMinerStateConfig)
    openrouter: PatternMinerOpenRouterConfig = field(
        default_factory=PatternMinerOpenRouterConfig,
    )


@dataclass
class DistillerConfig:
    # Top-level opt-out flag. Distinct from the orchestrator's
    # configuration-by-presence gate: the orchestrator already skips
    # distiller when the ``distiller:`` block is entirely absent. This
    # flag adds an explicit "block present but disabled" case so an
    # instance config can declare distiller off intentionally — useful
    # for rosters that define a distiller block for documentation /
    # future-enablement but want the daemon dormant today.
    enabled: bool = True
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
    # Daily radar auto-fire — see :class:`RadarDayConfig`. Defaulted-off
    # via the dataclass default; instances that want auto-fire opt in
    # via ``distiller.radar_day.enabled: true`` in their config.
    radar_day: RadarDayConfig = field(default_factory=RadarDayConfig)
    # Phase 4 embedding-pattern miner — see :class:`PatternMinerConfig`.
    # Defaulted-OFF; instances opt in via
    # ``distiller.pattern_miner.enabled: true`` in their config.
    # ``Optional`` so the absence-vs-block-with-defaults case is
    # observable to callers (the CLI handler treats None and
    # enabled=False identically). ``load_from_unified`` constructs
    # the field manually rather than via _build to avoid the nested-
    # ``state`` key collision with the top-level :class:`StateConfig`
    # in the recursive builder's global key dispatch.
    pattern_miner: PatternMinerConfig | None = None


# --- Recursive builder ---

_DATACLASS_MAP: dict[str, type] = {
    "vault": VaultConfig,
    "agent": AgentConfig,
    "claude": ClaudeBackendConfig,
    "anthropic": AnthropicConfig,
    "extraction": ExtractionConfig,
    "deep_extraction_schedule": ScheduleConfig,
    "consolidation_schedule": ScheduleConfig,
    "state": StateConfig,
    "logging": LoggingConfig,
    "idle_tick": IdleTickConfig,
    "radar_day": RadarDayConfig,
    # Reuse the same ScheduleConfig key for the radar_day nested schedule
    # block — recursive _build dispatches by KEY name, not parent context,
    # so the existing "schedule" → ScheduleConfig mapping covers
    # extraction.deep_extraction_schedule, daily_sync.schedule, AND
    # radar_day.schedule. Adding "schedule" here makes that explicit
    # rather than relying on the side-effect of the other schedule
    # entries.
    "schedule": ScheduleConfig,
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
    crashes whenever an operator pulls ``config.yaml.example``'s
    rotation block. Pre-dispatch strip keeps the typed config slim and
    routes rotation through the orchestrator / ``__main__.py``
    ``extract_rotation_config`` path.
    """
    known = set(LoggingConfig.__dataclass_fields__)
    return {k: v for k, v in log_raw.items() if k in known}


def load_config(path: str | Path = "config.yaml") -> DistillerConfig:
    """Load and parse config.yaml into DistillerConfig.

    Note: this loader expects a distiller-only YAML (top-level keys
    are distiller fields). The unified-config entrypoint
    :func:`load_from_unified` is what the CLI actually uses; this
    function exists for stand-alone testing of distiller config
    parsing without the unified config wrapper.
    """
    from alfred.vault.config_helpers import normalize_vault_block

    config_path = Path(path)
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    raw = _substitute_env(raw or {})
    if "vault" in raw:
        raw["vault"] = normalize_vault_block(raw["vault"])
    if isinstance(raw.get("logging"), dict):
        raw["logging"] = _strip_logging_extras(raw["logging"])
    # Pattern miner is built manually for the same nested-state-key-
    # collision reason load_from_unified handles. Pop before _build
    # then re-attach.
    pm_raw = raw.pop("pattern_miner", None)
    cfg = _build(DistillerConfig, raw)
    if isinstance(pm_raw, dict):
        pm_state_raw = pm_raw.get("state")
        if isinstance(pm_state_raw, dict):
            pm_state = PatternMinerStateConfig(
                **{k: v for k, v in pm_state_raw.items()
                   if k in PatternMinerStateConfig.__dataclass_fields__}
            )
        else:
            pm_state = PatternMinerStateConfig()
        pm_or_raw = pm_raw.get("openrouter")
        if isinstance(pm_or_raw, dict):
            pm_openrouter = PatternMinerOpenRouterConfig(
                **{k: v for k, v in pm_or_raw.items()
                   if k in PatternMinerOpenRouterConfig.__dataclass_fields__}
            )
        else:
            pm_openrouter = PatternMinerOpenRouterConfig()
        scalar_kwargs = {
            k: v for k, v in pm_raw.items()
            if k in PatternMinerConfig.__dataclass_fields__
            and k not in ("state", "openrouter")
        }
        cfg.pattern_miner = PatternMinerConfig(
            state=pm_state,
            openrouter=pm_openrouter,
            **scalar_kwargs,
        )
    return cfg


def load_from_unified(raw: dict[str, Any]) -> DistillerConfig:
    """Build DistillerConfig from a pre-loaded unified config dict."""
    from alfred.vault.config_helpers import normalize_vault_block

    raw = _substitute_env(raw)
    tool = raw.get("distiller", {})
    # Map unified logging.dir -> logging.file
    log_raw = dict(raw.get("logging", {}))
    log_dir = log_raw.pop("dir", "./data")
    if "file" not in log_raw:
        log_raw["file"] = f"{log_dir}/distiller.log"
    # Strip orchestrator-only keys (``rotation``) before typed build.
    log_raw = _strip_logging_extras(log_raw)
    built: dict[str, Any] = {
        "vault": normalize_vault_block(raw.get("vault", {})),
        "agent": raw.get("agent", {}),
        "extraction": tool.get("extraction", {}),
        "state": tool.get("state", {}),
        "logging": log_raw,
    }
    # Top-level opt-out flag — distinct from the orchestrator's
    # block-presence gate. ``distiller: { enabled: false }`` lets a
    # config declare distiller off intentionally (default True).
    if "enabled" in tool:
        built["enabled"] = bool(tool["enabled"])
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
    # Radar-day auto-fire — defaulted-OFF; partial dict merges over the
    # dataclass default so an instance config that just sets
    # ``radar_day: { enabled: true }`` picks up the 08:00 ADT default
    # schedule without re-stating it.
    radar_day_raw = tool.get("radar_day")
    if isinstance(radar_day_raw, dict):
        built["radar_day"] = radar_day_raw
    # Phase 4 pattern miner. Built manually rather than via _build so
    # the nested ``state`` key (PatternMinerStateConfig) doesn't
    # collide with the top-level distiller ``state`` (StateConfig) in
    # the recursive builder's global key dispatch. Absent block →
    # field stays None; CLI handler treats None and enabled=False
    # identically (prints "not enabled in this config" + returns).
    pm_raw = tool.get("pattern_miner")
    pattern_miner_obj: PatternMinerConfig | None = None
    if isinstance(pm_raw, dict):
        pm_state_raw = pm_raw.get("state")
        if isinstance(pm_state_raw, dict):
            pm_state = PatternMinerStateConfig(
                **{k: v for k, v in pm_state_raw.items()
                   if k in PatternMinerStateConfig.__dataclass_fields__}
            )
        else:
            pm_state = PatternMinerStateConfig()
        pm_or_raw = pm_raw.get("openrouter")
        if isinstance(pm_or_raw, dict):
            pm_openrouter = PatternMinerOpenRouterConfig(
                **{k: v for k, v in pm_or_raw.items()
                   if k in PatternMinerOpenRouterConfig.__dataclass_fields__}
            )
        else:
            pm_openrouter = PatternMinerOpenRouterConfig()
        # Top-level scalar/list fields — schema-tolerant filter so an
        # older config with extra keys loads without crashing (matches
        # the from_dict pattern used in pattern_miner_state.py).
        scalar_kwargs = {
            k: v for k, v in pm_raw.items()
            if k in PatternMinerConfig.__dataclass_fields__
            and k not in ("state", "openrouter")
        }
        pattern_miner_obj = PatternMinerConfig(
            state=pm_state,
            openrouter=pm_openrouter,
            **scalar_kwargs,
        )
    cfg = _build(DistillerConfig, built)
    cfg.pattern_miner = pattern_miner_obj
    return cfg
