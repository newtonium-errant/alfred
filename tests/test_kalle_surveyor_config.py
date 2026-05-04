"""Smoke tests for KAL-LE's surveyor config block.

Per the multi-instance wiring antipattern memo
(``feedback_multi_instance_wiring_pattern.md``): each new per-instance
wiring step gets a smoke test that confirms the config block loads
without falling back to defaults silently AND that critical isolation
fields (state path, milvus path, collection name) don't collide with
Salem's.

KAL-LE's ``surveyor:`` block was added to ``config.kalle.yaml`` in
c2 of the Phase 3 completion arc. ``config.kalle.yaml`` is NOT
tracked in git (it carries deployment ${VAR} references), so this
test exercises the canonical block SHAPE via a synthetic fixture
that mirrors what the live config carries. If the live config drifts
from this shape, the surveyor daemon misbehaves but this test still
passes — the contract here is "the shape, when loaded, produces a
working PipelineConfig with KAL-LE-isolated paths".

For end-to-end live-config validation, see
``alfred --config config.kalle.yaml status`` after the c2 ship +
restart.
"""

from __future__ import annotations

import pytest


# Canonical KAL-LE surveyor block — kept in lockstep with what's
# written to ``config.kalle.yaml`` in c2 of the Phase 3 completion arc.
# When the canonical shape changes (new sub-block, threshold change,
# model change), update both here and the live config file.
KALLE_SURVEYOR_BLOCK: dict = {
    "vault": {
        "path": "/home/andrew/aftermath-lab",
        "dont_scan_dirs": [
            "_templates", "_bases", "_docs", ".obsidian", "view", "inbox",
        ],
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
            "uri": "/home/andrew/.alfred/kalle/data/milvus_lite.db",
            "collection_name": "kalle_vault_embeddings",
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
            "path": "/home/andrew/.alfred/kalle/data/surveyor_state.json",
        },
    },
}


# Pin Salem's post-2026-05-03 entity_link.threshold so this test acts
# as a cross-instance hardcoding guard per
# ``feedback_hardcoding_and_alfred_naming.md``: changing one instance's
# threshold without bumping the other should be a deliberate decision,
# not a silent drift.
SALEM_ENTITY_LINK_THRESHOLD: float = 0.85


@pytest.fixture
def kalle_raw() -> dict:
    """Synthetic config dict mirroring KAL-LE's surveyor block."""
    import copy
    return copy.deepcopy(KALLE_SURVEYOR_BLOCK)


def test_kalle_surveyor_config_loads(kalle_raw: dict) -> None:
    """``load_from_unified`` produces a valid PipelineConfig with the
    KAL-LE-specific values."""
    from alfred.surveyor.config import PipelineConfig, load_from_unified
    cfg = load_from_unified(kalle_raw)
    assert isinstance(cfg, PipelineConfig)
    assert cfg.enabled is True
    # Vault path resolved to aftermath-lab.
    from pathlib import Path
    assert cfg.vault.path == Path("/home/andrew/aftermath-lab")


def test_kalle_surveyor_milvus_path_isolated(kalle_raw: dict) -> None:
    """Milvus DB path must be under /home/andrew/.alfred/kalle/, NOT
    colliding with Salem's ./data/milvus_lite.db. Two daemons writing
    the same Milvus Lite file would corrupt each other's collections.
    """
    from alfred.surveyor.config import load_from_unified
    cfg = load_from_unified(kalle_raw)
    assert "/home/andrew/.alfred/kalle/" in cfg.milvus.uri
    assert cfg.milvus.uri.endswith("milvus_lite.db")
    # Collection name must also differ — even if path was shared,
    # distinct collection names within one Milvus instance prevent
    # cross-vault contamination.
    assert cfg.milvus.collection_name == "kalle_vault_embeddings"
    assert cfg.milvus.collection_name != "vault_embeddings"


def test_kalle_surveyor_state_path_isolated(kalle_raw: dict) -> None:
    """State file must be under KAL-LE's data dir, not colliding with
    Salem's ./data/surveyor_state.json."""
    from alfred.surveyor.config import load_from_unified
    cfg = load_from_unified(kalle_raw)
    assert "/home/andrew/.alfred/kalle/" in cfg.state.path
    assert cfg.state.path.endswith("surveyor_state.json")


def test_kalle_surveyor_entity_link_threshold_matches_salem(
    kalle_raw: dict,
) -> None:
    """Pin: KAL-LE's entity_link.threshold matches Salem's post-2026-05-03
    contamination-fix value (0.85). Per
    ``feedback_hardcoding_and_alfred_naming.md`` — surfacing
    cross-instance config drift requires explicit pins, not implicit
    "they happen to be the same today" trust.

    If Salem's threshold moves (next contamination tuning cycle), the
    decision to keep KAL-LE in sync vs let it diverge should be
    deliberate. This test surfaces the change-set.
    """
    from alfred.surveyor.config import load_from_unified
    cfg = load_from_unified(kalle_raw)
    assert cfg.entity_link.threshold == SALEM_ENTITY_LINK_THRESHOLD, (
        f"KAL-LE entity_link.threshold ({cfg.entity_link.threshold}) "
        f"diverges from Salem's pinned {SALEM_ENTITY_LINK_THRESHOLD}. "
        "If this is intentional, update SALEM_ENTITY_LINK_THRESHOLD; "
        "otherwise update config.kalle.yaml."
    )


def test_kalle_surveyor_text_anchor_gate_inherits_default(
    kalle_raw: dict,
) -> None:
    """The Phase 1 source-side text-anchor gate (commit db9392f) is
    architecture-level — applies to all instances via the
    ``EntityLinkConfig.require_text_anchor`` default of True. KAL-LE's
    surveyor enablement inherits this without re-stating it.
    """
    from alfred.surveyor.config import load_from_unified
    cfg = load_from_unified(kalle_raw)
    assert cfg.entity_link.require_text_anchor is True


def test_kalle_surveyor_uses_ollama_for_labeler_not_cloud(
    kalle_raw: dict,
) -> None:
    """KAL-LE points the OpenRouter-shaped labeler config at the local
    Ollama instance (172.22.0.1:11434/v1) running qwen2.5:14b — no
    cloud cost. Salem uses x-ai/grok-4.1-fast in production. This
    isn't a correctness bug if it changes, but it IS a deployment
    decision that should be visible.
    """
    from alfred.surveyor.config import load_from_unified
    cfg = load_from_unified(kalle_raw)
    assert "172.22.0.1" in cfg.openrouter.base_url, (
        "KAL-LE labeler should target local Ollama, not openrouter.ai"
    )
    assert cfg.openrouter.model.startswith("qwen"), (
        "KAL-LE labeler model expected to be a qwen variant on local "
        "Ollama"
    )


def test_kalle_vault_dont_scan_dirs_includes_inbox(
    kalle_raw: dict,
) -> None:
    """KAL-LE's vault block excludes inbox/ from surveyor's scan
    (mirrors Salem's exclusion to avoid embedding raw inputs that
    haven't been curated yet)."""
    from alfred.surveyor.config import load_from_unified
    cfg = load_from_unified(kalle_raw)
    # dont_scan_dirs is the canonical name (post-2026-05-01 rename);
    # ignore_dirs may or may not be derived depending on
    # normalize_vault_block's behavior — the load result must include
    # inbox in at least one of the two.
    excludes = set(cfg.vault.dont_scan_dirs or []) | set(cfg.vault.ignore_dirs)
    assert "inbox" in excludes


def test_kalle_block_loads_with_defaults_for_unset_subfields(
    kalle_raw: dict,
) -> None:
    """Defensive: defaults backfill for any subfield the canonical
    block doesn't explicitly set (e.g., labeler.max_calls_per_minute,
    idle_tick.enabled). Confirms no canonical-block subfield typo
    silently disables a default."""
    from alfred.surveyor.config import load_from_unified
    cfg = load_from_unified(kalle_raw)
    # Labeler rate-limit + concurrency safety nets default-on.
    assert cfg.labeler.rate_limit_enabled is True
    assert cfg.labeler.max_calls_per_minute > 0
    assert cfg.labeler.max_concurrent > 0
    # Idle-tick defaults on.
    assert cfg.idle_tick.enabled is True
