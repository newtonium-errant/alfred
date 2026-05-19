"""Load config.yaml → typed dataclasses with env var substitution."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class VaultConfig:
    path: Path
    # See ``alfred.vault.config_helpers`` for the dont_scan/dont_index split.
    # Surveyor only consumes ``ignore_dirs`` for embedding-scope exclusion
    # (a scanning concern, semantically ``dont_scan_dirs``). It does not
    # build a wikilink target index, so ``dont_index_dirs`` is carried for
    # config-shape consistency only.
    ignore_dirs: list[str] = field(default_factory=lambda: ["_templates", "_bases", "_docs", ".obsidian", "view", "session", "inbox"])
    ignore_files: list[str] = field(default_factory=lambda: [".gitkeep"])
    # New (2026-05-01) — see vault/config_helpers.py for the rationale.
    dont_scan_dirs: list[str] | None = None
    dont_index_dirs: list[str] = field(default_factory=list)


@dataclass
class WatcherConfig:
    debounce_seconds: float = 30.0


@dataclass
class OllamaConfig:
    base_url: str = "http://localhost:11434"
    model: str = "nomic-embed-text"
    embedding_dims: int = 768
    api_key: str = ""  # If set, uses OpenAI-compatible /v1/embeddings endpoint


@dataclass
class MilvusConfig:
    uri: str = "./data/milvus_lite.db"
    collection_name: str = "vault_embeddings"


@dataclass
class HdbscanConfig:
    min_cluster_size: int = 3
    min_samples: int = 2


@dataclass
class LeidenConfig:
    resolution: float = 1.0


@dataclass
class ClusteringConfig:
    hdbscan: HdbscanConfig = field(default_factory=HdbscanConfig)
    leiden: LeidenConfig = field(default_factory=LeidenConfig)


@dataclass
class OpenRouterConfig:
    api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1"
    model: str = "x-ai/grok-4.1-fast"
    temperature: float = 0.3


@dataclass
class LabelerConfig:
    max_files_per_cluster_context: int = 20
    body_preview_chars: int = 200
    min_cluster_size_to_label: int = 2
    min_relationship_confidence: float = 0.65
    # Belt-and-suspenders cap on Ollama/OpenRouter labeler calls. The c1
    # membership-stability gate should already eliminate the wasted-call
    # pattern that OOM-killed WSL on 2026-04-23, but a pathological burst
    # (e.g. an unforeseen membership-invalidating cascade) shouldn't be
    # allowed to saturate the LLM backend regardless. When
    # ``rate_limit_enabled`` is True, ``Labeler._llm_call`` prunes a
    # sliding 60-second window of call timestamps and drops the call
    # (returning None) if the window is already at
    # ``max_calls_per_minute``. Both have defaults so existing configs
    # won't break.
    max_calls_per_minute: int = 30
    rate_limit_enabled: bool = True
    # Max LLM calls in flight during the cluster-labeling pass. Each cluster
    # fires 2 sequential calls (label_cluster + suggest_relationships), and
    # we fan out across clusters. 8 keeps well under OpenRouter's default
    # rate limits for the fast-tier models while shortening full-vault
    # labeling from tens of minutes to single digits.
    max_concurrent: int = 8

    # Per-write text-anchor gate on alfred_tags writeback (architectural
    # twin to ``EntityLinkConfig.require_text_anchor`` shipped in
    # ``db9392f`` for entity-link writes; same contamination class,
    # different writeback path). Before persisting any tag to a record,
    # verify the tag's last segment (split on ``/`` then ``-``) has a
    # word-boundary text match in the record's body / title / description
    # / related list / relationships array's anchor strings. The labeler
    # proposes one tag-set per cluster; this gate runs per-MEMBER so
    # different members of the same cluster may keep different filtered
    # subsets.
    #
    # Cosine clustering produces topic-coherent false positives — events
    # cluster with music records in embedding space because they share
    # date/location signal even when no music event is the subject.
    # Combining cosine + last-segment word-boundary mention-detection
    # is standard precision-control in entity-linking systems
    # (Bio-Yodie, WikiData linkers).
    #
    # Default: True (production). Tests that exercise the legacy cosine-
    # only contract pass ``require_text_anchor=False`` so their fixtures
    # don't have to embed tag-words in every test body. Operator can flip
    # to False if a downstream workflow needs the looser cosine-only
    # semantic for some reason — but the contamination bug class
    # returns at full strength.
    require_text_anchor: bool = True


@dataclass
class EntityLinkConfig:
    """Structured entity-link writeback — when a cluster contains entity
    records (matter/person/org/project), non-entity members with cosine
    similarity above threshold get the entity's vault path written into
    a typed frontmatter field (related_matters / related_persons / etc).
    """
    threshold: float = 0.75
    max_per_record: int = 5
    # When a new entity record (matter/person/org/project) is created, run
    # a reverse scan across the vault and add it to matching records'
    # related_<type> frontmatter. Without this, brand-new matters show up
    # as structurally disconnected until the next clustering pass happens
    # to co-cluster them with something.
    backfill_enabled: bool = True

    # Per-write text-anchor gate (Phase 1 of source-side fix, ratified
    # 2026-05-03). Before persisting any entity link, verify the entity's
    # display name has a word-boundary text match in the source record's
    # body / title / description / related list / relationships array.
    # Cosine similarity alone produces topic-coherent false positives —
    # records about "music events" cluster with music-related person
    # records in embedding space without factual association. Combining
    # cosine + mention-detection is standard precision-control in entity-
    # linking systems (Bio-Yodie, WikiData linkers).
    #
    # Default: True (production). Tests that exercise the legacy
    # threshold-only contract (test_daemon_entity_link.py et al.) set
    # this False so their fixtures don't have to embed entity names in
    # every test body. Operator can flip to False if a downstream
    # workflow needs the looser cosine-only semantic for some reason —
    # but the contamination bug class returns at full strength.
    require_text_anchor: bool = True


@dataclass
class StateConfig:
    # Tool-scoped default; see ``distiller/config.py`` for rationale.
    path: str = "./data/surveyor_state.json"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "./data/pipeline.log"


@dataclass
class MocSuggestionConfig:
    """Cluster→MOC suggestion mechanism (Phase 5 Sub-arc D1, 2026-05-19).

    Per ``project_hypatia_zettelkasten_redesign.md`` Phase 5: when the
    surveyor labels a cluster, propose adding cluster members to relevant
    MOC ``# Contents``. Operator reviews via ``/moc-suggestions`` and
    accepts via ``/accept-moc <id>`` (D2 ship). D1 emits suggestions to
    an out-of-vault JSONL queue; D1 NEVER writes to the vault.

    Mapping logic (ratified Q2):
      * Primary signal: ``member_overlap`` — fraction of cluster members
        that already cite the candidate MOC via their ``mocs:``
        frontmatter list. High signal because operator has already
        validated the membership; surveyor just generalizes.
      * Tiebreaker: ``fuzzy_label`` Jaccard overlap between the cluster
        label tag-set and the MOC filename tokens. Used only when
        member-overlap returns zero candidates.
      * Propose-new: when both signals return zero, emit a suggestion
        with ``target_moc_rel_path=None`` + ``proposed_new_moc_name``
        derived from the cluster label.

    Lifecycle (ratified Q5):
      * Pending stays pending until operator acts.
      * Rejected persists indefinitely — negative-learning surface;
        re-proposals of the same (members, target) hash are suppressed.
      * Accepted-but-apply-failed flips back to pending.

    Inventory-MOC filter (ratified Q7): ``MOC/_*.md`` paths NEVER appear
    as suggestion targets or proposed-new names. Inventory MOCs are
    predicate-driven, system-maintained per Phase 4 Sub-arc B.

    Disabled by default for compatibility — Salem + KAL-LE configs don't
    set this block, so the orchestrator's existing surveyor stays silent
    on the new stage. Hypatia config sets ``enabled: true`` to opt in.
    """

    enabled: bool = False

    #: Member-overlap threshold (fraction). 0.4 = at least 40% of cluster
    #: members already cite the target MOC. Below this, no suggestion is
    #: emitted on the member-overlap signal.
    member_overlap_threshold: float = 0.4

    #: Fuzzy-label Jaccard threshold (token-overlap on lowercased,
    #: hyphen/underscore/space-split tokens). Tiebreaker only — consulted
    #: when member-overlap returns zero candidates.
    fuzzy_label_jaccard_threshold: float = 0.5

    #: Minimum cluster size for suggestion eligibility. Singletons + tiny
    #: clusters carry too little signal for MOC-grouping confidence. Set
    #: explicitly here so the gate is independent of the labeler's
    #: min_cluster_size_to_label (which is about LLM-cost gating, a
    #: different concern).
    min_cluster_size: int = 3

    #: Per-sweep cap on new proposals emitted. Prevents a fan-out
    #: cascade if the LLM labels a burst of new records.
    max_proposals_per_sweep: int = 10

    #: Per-target-MOC cap on pending proposals. Prevents queue inflation
    #: against the same MOC across sweeps.
    max_pending_per_target: int = 5

    #: Optional explicit queue path. When None (default), the queue
    #: derives from ``cfg.state.path.parent / "moc_suggestions.jsonl"``
    #: — same convention as the audit log path derivation in
    #: ``daemon.py:66-70``. Settable for tests + alternate deployments.
    queue_path: str | None = None


@dataclass
class IdleTickConfig:
    """Surveyor idle-tick heartbeat — "intentionally left blank" liveness signal.

    A periodic ``surveyor.idle_tick`` log event so observers can distinguish
    *idle / healthy* (recent heartbeat present, low or zero
    ``events_in_window``) from *broken* (no heartbeat at all). Without it,
    a quiet stretch (no vault writes touching surveyor scope) is
    indistinguishable from a hung daemon.

    Counter semantic: one record re-embedded = one event. The labeling
    pass is downstream of embedding and runs at most once per cluster
    change, so embedding is the more meaningful per-record signal.

    Defaults are deliberately on — see ``src/alfred/common/heartbeat.py``
    for the cadence rationale.
    """

    enabled: bool = True
    interval_seconds: int = 60


@dataclass
class PipelineConfig:
    vault: VaultConfig
    watcher: WatcherConfig
    ollama: OllamaConfig
    milvus: MilvusConfig
    clustering: ClusteringConfig
    openrouter: OpenRouterConfig
    labeler: LabelerConfig
    state: StateConfig
    logging: LoggingConfig
    idle_tick: IdleTickConfig = field(default_factory=IdleTickConfig)
    entity_link: EntityLinkConfig = field(default_factory=EntityLinkConfig)
    moc_suggestion: MocSuggestionConfig = field(default_factory=MocSuggestionConfig)
    # Top-level opt-out flag. Distinct from the orchestrator's
    # configuration-by-presence gate: the orchestrator already skips
    # surveyor when the ``surveyor:`` block is entirely absent. This
    # flag adds an explicit "block present but disabled" case so an
    # instance config can declare surveyor off intentionally.
    enabled: bool = True


_ENV_PATTERN = re.compile(r"\$\{(\w+)\}")


def _substitute_env(value: str) -> str:
    """Replace ${ENV_VAR} patterns with environment variable values."""
    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        env_val = os.environ.get(var_name, "")
        return env_val
    return _ENV_PATTERN.sub(replacer, value)


def _walk_and_substitute(obj: object) -> object:
    """Recursively substitute env vars in all string values."""
    if isinstance(obj, str):
        return _substitute_env(obj)
    if isinstance(obj, dict):
        return {k: _walk_and_substitute(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_and_substitute(item) for item in obj]
    return obj


def _build_dataclass(cls, data: dict | None):
    """Build a dataclass from a dict, handling nested dataclasses."""
    if data is None:
        return cls()
    # Resolve string annotations to actual types
    import typing
    hints = typing.get_type_hints(cls)
    kwargs = {}
    for f in cls.__dataclass_fields__.values():
        if f.name not in data:
            continue
        val = data[f.name]
        resolved_type = hints.get(f.name, f.type)
        # Check if the field type is itself a dataclass
        origin = getattr(resolved_type, "__origin__", None)
        if origin is None and hasattr(resolved_type, "__dataclass_fields__"):
            kwargs[f.name] = _build_dataclass(resolved_type, val)
        elif resolved_type is Path or (isinstance(resolved_type, type) and issubclass(resolved_type, Path)):
            kwargs[f.name] = Path(val)
        else:
            kwargs[f.name] = val
    return cls(**kwargs)


def load_config(config_path: str | Path) -> PipelineConfig:
    """Load config.yaml, substitute env vars, return typed PipelineConfig."""
    from alfred.vault.config_helpers import normalize_vault_block

    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    raw = _walk_and_substitute(raw)

    vault_raw = raw.get("vault")
    if isinstance(vault_raw, dict):
        vault_raw = normalize_vault_block(vault_raw)

    return PipelineConfig(
        vault=_build_dataclass(VaultConfig, vault_raw),
        watcher=_build_dataclass(WatcherConfig, raw.get("watcher")),
        ollama=_build_dataclass(OllamaConfig, raw.get("ollama")),
        milvus=_build_dataclass(MilvusConfig, raw.get("milvus")),
        clustering=_build_dataclass(ClusteringConfig, raw.get("clustering")),
        openrouter=_build_dataclass(OpenRouterConfig, raw.get("openrouter")),
        labeler=_build_dataclass(LabelerConfig, raw.get("labeler")),
        state=_build_dataclass(StateConfig, raw.get("state")),
        logging=_build_dataclass(LoggingConfig, raw.get("logging")),
        idle_tick=_build_dataclass(IdleTickConfig, raw.get("idle_tick")),
        entity_link=_build_dataclass(EntityLinkConfig, raw.get("entity_link")),
        moc_suggestion=_build_dataclass(
            MocSuggestionConfig, raw.get("moc_suggestion"),
        ),
    )


def load_from_unified(raw: dict) -> PipelineConfig:
    """Build PipelineConfig from a pre-loaded unified config dict."""
    from alfred.vault.config_helpers import normalize_vault_block

    raw = _walk_and_substitute(raw)
    tool = raw.get("surveyor", {})
    vault_raw = raw.get("vault")
    if isinstance(vault_raw, dict):
        vault_raw = normalize_vault_block(vault_raw)
    return PipelineConfig(
        vault=_build_dataclass(VaultConfig, vault_raw),
        watcher=_build_dataclass(WatcherConfig, tool.get("watcher")),
        ollama=_build_dataclass(OllamaConfig, tool.get("ollama")),
        milvus=_build_dataclass(MilvusConfig, tool.get("milvus")),
        clustering=_build_dataclass(ClusteringConfig, tool.get("clustering")),
        openrouter=_build_dataclass(OpenRouterConfig, tool.get("openrouter")),
        labeler=_build_dataclass(LabelerConfig, tool.get("labeler")),
        state=_build_dataclass(StateConfig, tool.get("state")),
        logging=_build_dataclass(LoggingConfig, raw.get("logging")),
        # Idle-tick lives under ``surveyor:``; defaulted-on if absent.
        idle_tick=_build_dataclass(IdleTickConfig, tool.get("idle_tick")),
        entity_link=_build_dataclass(EntityLinkConfig, tool.get("entity_link")),
        moc_suggestion=_build_dataclass(
            MocSuggestionConfig, tool.get("moc_suggestion"),
        ),
        # Top-level opt-out — defaults True if absent.
        enabled=bool(tool.get("enabled", True)),
    )
