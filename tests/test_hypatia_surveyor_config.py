"""Smoke tests for Hypatia's surveyor config block (Phase 5 Sub-arc A).

Per the multi-instance wiring antipattern memo
(``feedback_multi_instance_wiring_pattern.md``): each new per-instance
wiring step gets a smoke test that confirms the config block loads
without falling back to defaults silently AND that critical isolation
fields (state path, milvus path, collection name) don't collide with
Salem's or KAL-LE's.

Hypatia's ``surveyor:`` block was added to ``config.hypatia.yaml`` in
Sub-arc A of the Phase 5 enablement arc
(``project_hypatia_zettelkasten_redesign.md``). ``config.hypatia.yaml``
is NOT tracked in git (it carries deployment ${VAR} references), so
this test exercises the canonical block SHAPE via a synthetic fixture
that mirrors what the live config carries. If the live config drifts
from this shape, the surveyor daemon misbehaves but this test still
passes — the contract here is "the shape, when loaded, produces a
working PipelineConfig with Hypatia-isolated paths".

For end-to-end live-config validation, see
``alfred --config config.hypatia.yaml status`` after the Sub-arc A
ship + restart.

Cross-instance pinning (per
``feedback_hardcoding_and_alfred_naming.md`` + builder rule #8): the
labeler model + Ollama embedding dims + entity_link.threshold + watcher
debounce are pinned shared across Salem / KAL-LE / Hypatia via named
constants. A drift in any one instance surfaces here as a failing
assertion with an actionable message.
"""

from __future__ import annotations

import pytest


# Canonical Hypatia surveyor block — kept in lockstep with what's
# written to ``config.hypatia.yaml`` in Sub-arc A of the Phase 5 arc.
# When the canonical shape changes (new sub-block, threshold change,
# model change), update both here and the live config file.
HYPATIA_SURVEYOR_BLOCK: dict = {
    "vault": {
        "path": "/home/andrew/library-alexandria",
        # Mirrors config.hypatia.yaml's vault block. session/ is
        # deliberately NOT excluded — Hypatia surfaces from session
        # notes per the surfacing-engine design. inbox/ is also not
        # excluded today (Hypatia's vault has no curator inbox pattern
        # the way Salem and KAL-LE do).
        "dont_scan_dirs": ["_bases", ".obsidian"],
        "dont_index_dirs": [],
        "ignore_files": [".gitkeep"],
    },
    "surveyor": {
        "watcher": {"debounce_seconds": 30},
        "ollama": {
            "base_url": "http://172.22.0.1:11434",
            "model": "nomic-embed-text",
            "embedding_dims": 768,
        },
        "milvus": {
            "uri": "/home/andrew/.alfred/hypatia/data/milvus_lite.db",
            "collection_name": "hypatia_vault_embeddings",
        },
        "clustering": {
            "hdbscan": {"min_cluster_size": 3, "min_samples": 2},
            "leiden": {"resolution": 1.0},
        },
        "openrouter": {
            "api_key": "ollama",
            "base_url": "http://172.22.0.1:11434/v1",
            "model": "qwen2.5:14b",
            "temperature": 0.3,
        },
        "labeler": {
            "max_files_per_cluster_context": 20,
            "body_preview_chars": 200,
            "min_cluster_size_to_label": 2,
            "min_relationship_confidence": 0.65,
        },
        "entity_link": {
            "threshold": 0.85,
        },
        "state": {
            "path": "/home/andrew/.alfred/hypatia/data/surveyor_state.json",
        },
    },
}


# ---------------------------------------------------------------------------
# Cross-instance pinned constants — per
# ``feedback_hardcoding_and_alfred_naming.md`` rule #8 / builder rule #8.
# Each constant names the originating instance so the pin direction is
# explicit. If Salem's value moves (next contamination tuning cycle or
# model upgrade), the decision to keep Hypatia in sync vs let it diverge
# is deliberate, and the test surfaces it.
# ---------------------------------------------------------------------------

# Salem's post-2026-05-03 entity_link.threshold (also adopted by KAL-LE
# 2026-05-12). KAL-LE's test pins the same value via
# ``test_kalle_surveyor_config.SALEM_ENTITY_LINK_THRESHOLD = 0.85``.
SALEM_ENTITY_LINK_THRESHOLD: float = 0.85

# Salem's labeler model on local Ollama. KAL-LE adopted the same model
# 2026-05-12 (cost-zero local inference). Hypatia adopts it for Phase 5
# Sub-arc A per ratified design Q3. Pin is shared across all three.
SALEM_LABELER_MODEL: str = "qwen2.5:14b"

# Ollama embedding model + dim — shared across all three instances since
# Salem's initial surveyor ship. Pinning here surfaces any per-instance
# drift in either direction.
SALEM_OLLAMA_EMBED_MODEL: str = "nomic-embed-text"
SALEM_OLLAMA_EMBED_DIMS: int = 768

# Watcher debounce — same 30s across all three instances per ratified
# design Q6. No tuning reason exists to diverge.
SALEM_WATCHER_DEBOUNCE_SECONDS: float = 30.0


@pytest.fixture
def hypatia_raw() -> dict:
    """Synthetic config dict mirroring Hypatia's surveyor block."""
    import copy
    return copy.deepcopy(HYPATIA_SURVEYOR_BLOCK)


def test_hypatia_surveyor_config_loads(hypatia_raw: dict) -> None:
    """``load_from_unified`` produces a valid PipelineConfig with the
    Hypatia-specific values."""
    from alfred.surveyor.config import PipelineConfig, load_from_unified
    cfg = load_from_unified(hypatia_raw)
    assert isinstance(cfg, PipelineConfig)
    assert cfg.enabled is True
    # Vault path resolved to library-alexandria.
    from pathlib import Path
    assert cfg.vault.path == Path("/home/andrew/library-alexandria")


def test_hypatia_surveyor_milvus_path_isolated(hypatia_raw: dict) -> None:
    """Milvus DB path must be under /home/andrew/.alfred/hypatia/, NOT
    colliding with Salem's ./data/milvus_lite.db or KAL-LE's
    /home/andrew/.alfred/kalle/data/milvus_lite.db. Three daemons writing
    the same Milvus Lite file would corrupt each other's collections.
    """
    from alfred.surveyor.config import load_from_unified
    cfg = load_from_unified(hypatia_raw)
    assert "/home/andrew/.alfred/hypatia/" in cfg.milvus.uri
    assert cfg.milvus.uri.endswith("milvus_lite.db")
    # Collection name must also differ — even if path was shared,
    # distinct collection names within one Milvus instance prevent
    # cross-vault contamination. Three-way distinction.
    assert cfg.milvus.collection_name == "hypatia_vault_embeddings"
    assert cfg.milvus.collection_name != "vault_embeddings"  # Salem
    assert cfg.milvus.collection_name != "kalle_vault_embeddings"  # KAL-LE


def test_hypatia_surveyor_state_path_isolated(hypatia_raw: dict) -> None:
    """State file must be under Hypatia's data dir, not colliding with
    Salem's ./data/surveyor_state.json or KAL-LE's path."""
    from alfred.surveyor.config import load_from_unified
    cfg = load_from_unified(hypatia_raw)
    assert "/home/andrew/.alfred/hypatia/" in cfg.state.path
    assert cfg.state.path.endswith("surveyor_state.json")
    # Three-way isolation: not Salem's, not KAL-LE's.
    assert "/home/andrew/.alfred/kalle/" not in cfg.state.path


def test_hypatia_surveyor_entity_link_threshold_matches_salem(
    hypatia_raw: dict,
) -> None:
    """Pin: Hypatia's entity_link.threshold matches Salem's post-2026-05-03
    contamination-fix value (0.85). FUNCTIONALLY INERT on Hypatia because
    no entity record types (matter/person/org/project) exist in
    ``KNOWN_TYPES_HYPATIA`` — ``_link_entities_in_clusters`` never finds
    a target. Pinned anyway for future-proofing per ratified design Q7
    (if a future Hypatia type joins ``ENTITY_RECORD_TYPES``, the
    threshold is already calibrated).

    If Salem's threshold moves (next contamination tuning cycle), the
    decision to keep Hypatia in sync vs let it diverge should be
    deliberate. This test surfaces the change-set.
    """
    from alfred.surveyor.config import load_from_unified
    cfg = load_from_unified(hypatia_raw)
    assert cfg.entity_link.threshold == SALEM_ENTITY_LINK_THRESHOLD, (
        f"Hypatia entity_link.threshold ({cfg.entity_link.threshold}) "
        f"diverges from Salem's pinned {SALEM_ENTITY_LINK_THRESHOLD}. "
        "If this is intentional, update SALEM_ENTITY_LINK_THRESHOLD "
        "(here AND in test_kalle_surveyor_config.py); otherwise update "
        "config.hypatia.yaml."
    )


def test_hypatia_surveyor_text_anchor_gate_inherits_default(
    hypatia_raw: dict,
) -> None:
    """The Phase 1 source-side text-anchor gate (commit db9392f) is
    architecture-level — applies to all instances via the
    ``EntityLinkConfig.require_text_anchor`` default of True. Hypatia's
    surveyor enablement inherits this without re-stating it.

    Inert on Hypatia today (no entity types), but the gate is still
    armed for if/when entity types arrive.
    """
    from alfred.surveyor.config import load_from_unified
    cfg = load_from_unified(hypatia_raw)
    assert cfg.entity_link.require_text_anchor is True


def test_hypatia_surveyor_labeler_model_matches_salem(
    hypatia_raw: dict,
) -> None:
    """Pin: Hypatia labeler model matches Salem + KAL-LE
    (``qwen2.5:14b`` on local Ollama). Per ratified design Q3 — same
    model proven on Salem operational + KAL-LE coding vaults; Hypatia
    Zettelkasten clustering is closer to KAL-LE concept-clustering than
    to Salem entity-clustering, so the same model should work.

    Drift here surfaces a deliberate per-instance model decision; if
    Hypatia should diverge (e.g., to a larger model for nuanced zettel
    topical labels), bump this constant or split per-instance constants.
    """
    from alfred.surveyor.config import load_from_unified
    cfg = load_from_unified(hypatia_raw)
    assert cfg.openrouter.model == SALEM_LABELER_MODEL, (
        f"Hypatia labeler model ({cfg.openrouter.model}) diverges from "
        f"Salem's pinned {SALEM_LABELER_MODEL}. If this is intentional, "
        "split SALEM_LABELER_MODEL into per-instance constants; "
        "otherwise update config.hypatia.yaml."
    )


def test_hypatia_surveyor_uses_ollama_for_labeler_not_cloud(
    hypatia_raw: dict,
) -> None:
    """Hypatia points the OpenRouter-shaped labeler config at the local
    Ollama instance (172.22.0.1:11434/v1) running qwen2.5:14b — no
    cloud cost. Matches KAL-LE's pattern. If Hypatia ever moves to
    cloud (e.g., for larger labeling model), this test surfaces the
    deployment shift.
    """
    from alfred.surveyor.config import load_from_unified
    cfg = load_from_unified(hypatia_raw)
    assert "172.22.0.1" in cfg.openrouter.base_url, (
        "Hypatia labeler should target local Ollama, not openrouter.ai"
    )
    assert cfg.openrouter.model.startswith("qwen"), (
        "Hypatia labeler model expected to be a qwen variant on local "
        "Ollama"
    )


def test_hypatia_surveyor_embed_model_matches_salem(
    hypatia_raw: dict,
) -> None:
    """Pin: Hypatia embedding model + dims match Salem + KAL-LE
    (``nomic-embed-text``, 768 dims). Shared across all three instances
    since Salem's initial surveyor ship. Drift here would mean the
    Milvus collection schema doesn't match the embedder output —
    catastrophic.
    """
    from alfred.surveyor.config import load_from_unified
    cfg = load_from_unified(hypatia_raw)
    assert cfg.ollama.model == SALEM_OLLAMA_EMBED_MODEL, (
        f"Hypatia embed model ({cfg.ollama.model}) diverges from "
        f"pinned {SALEM_OLLAMA_EMBED_MODEL}."
    )
    assert cfg.ollama.embedding_dims == SALEM_OLLAMA_EMBED_DIMS, (
        f"Hypatia embed dims ({cfg.ollama.embedding_dims}) diverges "
        f"from pinned {SALEM_OLLAMA_EMBED_DIMS}. Collection schema "
        "drift would corrupt vector queries."
    )


def test_hypatia_surveyor_watcher_debounce_matches_salem(
    hypatia_raw: dict,
) -> None:
    """Pin: Hypatia watcher.debounce_seconds matches Salem + KAL-LE (30s).
    Per ratified design Q6 — no tuning reason exists to diverge today.
    Hypatia's vault write velocity is lower than Salem's; 30s is
    conservative for a slow vault.
    """
    from alfred.surveyor.config import load_from_unified
    cfg = load_from_unified(hypatia_raw)
    assert cfg.watcher.debounce_seconds == SALEM_WATCHER_DEBOUNCE_SECONDS, (
        f"Hypatia watcher.debounce_seconds ({cfg.watcher.debounce_seconds}) "
        f"diverges from pinned {SALEM_WATCHER_DEBOUNCE_SECONDS}s."
    )


def test_hypatia_vault_path_targets_library_alexandria(
    hypatia_raw: dict,
) -> None:
    """Hypatia's vault path is /home/andrew/library-alexandria, NOT
    Salem's ./vault or KAL-LE's /home/andrew/aftermath-lab. Three-way
    isolation.
    """
    from alfred.surveyor.config import load_from_unified
    from pathlib import Path
    cfg = load_from_unified(hypatia_raw)
    assert cfg.vault.path == Path("/home/andrew/library-alexandria")
    assert cfg.vault.path != Path("/home/andrew/aftermath-lab")  # KAL-LE
    assert str(cfg.vault.path) != "./vault"  # Salem


def test_hypatia_block_loads_with_defaults_for_unset_subfields(
    hypatia_raw: dict,
) -> None:
    """Defensive: defaults backfill for any subfield the canonical
    block doesn't explicitly set (e.g., labeler.max_calls_per_minute,
    idle_tick.enabled). Confirms no canonical-block subfield typo
    silently disables a default."""
    from alfred.surveyor.config import load_from_unified
    cfg = load_from_unified(hypatia_raw)
    # Labeler rate-limit + concurrency safety nets default-on.
    assert cfg.labeler.rate_limit_enabled is True
    assert cfg.labeler.max_calls_per_minute > 0
    assert cfg.labeler.max_concurrent > 0
    # Idle-tick defaults on (the heartbeat liveness signal).
    assert cfg.idle_tick.enabled is True
    # require_text_anchor on labeler side (architectural twin of
    # entity_link.require_text_anchor).
    assert cfg.labeler.require_text_anchor is True
