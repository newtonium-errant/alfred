"""Tests for Sub-arc B+ silent-no-op-paths sweep (2026-05-21).

Sub-arc B (master ``1174c79`` + ``65229ec`` placement fix) shipped the
once-per-lifecycle ``surveyor.entity_link_no_entities_in_vault`` gate
for the vault-state observation "no entity record types exist in the
vault." During that work, builder flagged four ADJACENT silent-no-op
paths in ``src/alfred/surveyor/daemon.py``:

1. ``_link_entities_in_clusters`` — ``if clusters_processed > 0:``
   guard; silent when entities exist but no current cluster has both
   an entity AND a non-entity member.
2. ``_link_noise_points_to_entities`` — ``if noise_processed > 0:``
   guard; silent when no noise points have non-None vectors.
3. ``_backfill_new_entities`` — ``if entities_processed > 0:``
   guard; silent when ``new_entity_paths`` are None-record / None-vec
   / wrong-type.
4. ``_cluster_and_label`` call site at the ``if noise_paths:`` gate
   (line ~589); silent when HDBSCAN produces zero noise points.

Per ``feedback_intentionally_left_blank.md``: silence-from-no-data
must surface differently from silence-from-failure. Each site
classified as PER-SWEEP observation (not vault-state), so no
lifecycle latch needed — emit "ran, nothing to do" inside the work
block.

Per ``feedback_log_emission_test_pattern.md`` + builder rule #9:
each new log emission pinned via ``structlog.testing.capture_logs``,
asserting both the event name AND key fields.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
from structlog.testing import capture_logs

from alfred.surveyor.clusterer import ClusterResult
from alfred.surveyor.config import (
    ClusteringConfig,
    EntityLinkConfig,
    HdbscanConfig,
    LabelerConfig,
    LeidenConfig,
    LoggingConfig,
    MilvusConfig,
    OllamaConfig,
    OpenRouterConfig,
    PipelineConfig,
    StateConfig,
    VaultConfig,
    WatcherConfig,
)
from alfred.surveyor.daemon import Daemon
from alfred.surveyor.parser import VaultRecord
from alfred.surveyor.state import PipelineState
from alfred.surveyor.writer import VaultWriter


# ---------------------------------------------------------------------------
# Fixtures (mirror shape of test_daemon_backfill.py + test_daemon_noise_linking.py)
# ---------------------------------------------------------------------------


def _cfg(
    vault: Path,
    state_path: Path,
    threshold: float = 0.75,
    max_per: int = 5,
    backfill: bool = True,
) -> PipelineConfig:
    return PipelineConfig(
        vault=VaultConfig(path=vault),
        watcher=WatcherConfig(),
        ollama=OllamaConfig(),
        milvus=MilvusConfig(uri=str(state_path.parent / "milvus.db")),
        clustering=ClusteringConfig(hdbscan=HdbscanConfig(), leiden=LeidenConfig()),
        openrouter=OpenRouterConfig(api_key="x"),
        labeler=LabelerConfig(),
        state=StateConfig(path=str(state_path)),
        logging=LoggingConfig(),
        entity_link=EntityLinkConfig(
            threshold=threshold,
            max_per_record=max_per,
            backfill_enabled=backfill,
            # Same legacy-contract preservation as sibling tests —
            # source-side text-anchor gate disabled so these tests
            # exercise the threshold-only no-op paths.
            require_text_anchor=False,
        ),
    )


def _write(vault: Path, rel: str, rt: str) -> None:
    full = vault / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(f"---\ntype: {rt}\nname: x\n---\n\nbody\n", encoding="utf-8")


def _record(rel: str, rt: str) -> VaultRecord:
    return VaultRecord(
        rel_path=rel,
        frontmatter={"type": rt, "name": "x"},
        body="",
        record_type=rt,
    )


@pytest.fixture
def daemon_and_vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    state_path = tmp_path / "state.json"
    daemon = Daemon.__new__(Daemon)
    daemon.cfg = _cfg(vault, state_path)
    daemon.state = PipelineState(state_path=daemon.cfg.state.path)
    daemon.writer = VaultWriter(vault_path=vault, state=daemon.state)
    daemon.embedder = MagicMock()
    daemon.watcher = MagicMock()
    daemon.clusterer = MagicMock()
    daemon.labeler = MagicMock()
    daemon._shutdown_requested = False
    # Sub-arc B latch attrs that __new__ skips since it bypasses
    # __init__; only needed for the call-site (site 4) tests that
    # drive the full ``_cluster_and_label`` orchestration.
    daemon._entity_link_no_entities_logged = False
    daemon._moc_suggestion_disabled_logged = False
    daemon._moc_suggestion_no_moc_dir_logged = False
    return daemon, vault


# ===========================================================================
# Site 1 — _link_entities_in_clusters zero-eligible-clusters
# ===========================================================================


def test_link_entities_in_clusters_emits_no_eligible_clusters_when_only_singleton_clusters(
    daemon_and_vault,
) -> None:
    """When every changed cluster has fewer than 2 members (the
    ``if len(members) < 2`` short-circuit), ``clusters_processed``
    stays 0 and the original
    ``daemon.entity_linking_complete`` log doesn't fire. The new
    ``surveyor.entity_linking.no_eligible_clusters`` log MUST fire
    instead, with the ``changed_clusters_total`` field set to the
    total number of changed clusters the caller passed in.
    """
    daemon, vault = daemon_and_vault
    _write(vault, "matter/m1.md", "matter")
    _write(vault, "event/e1.md", "event")

    records = {
        "matter/m1.md": _record("matter/m1.md", "matter"),
        "event/e1.md": _record("event/e1.md", "event"),
    }
    paths = ["matter/m1.md", "event/e1.md"]
    vectors = np.asarray(
        [[1.0, 0.0], [0.0, 1.0]], dtype=np.float32,
    )

    # Two changed clusters, each with only ONE member → both fail
    # the ``len(members) < 2`` gate.
    changed_cluster_ids = {0, 1}
    cluster_members = {0: ["matter/m1.md"], 1: ["event/e1.md"]}

    with capture_logs() as captured:
        daemon._link_entities_in_clusters(
            changed_cluster_ids, cluster_members, records, paths, vectors,
        )

    matches = [
        c for c in captured
        if c.get("event") == "surveyor.entity_linking.no_eligible_clusters"
    ]
    assert len(matches) == 1, (
        "expected no_eligible_clusters log on the singleton-clusters "
        f"path; got events: {[c.get('event') for c in captured]}"
    )
    entry = matches[0]
    assert entry["changed_clusters_total"] == 2
    assert entry["threshold"] == daemon.cfg.entity_link.threshold
    assert entry["max_per_record"] == daemon.cfg.entity_link.max_per_record

    # Sanity: the ``entity_linking_complete`` log did NOT fire (the
    # two events are mutually exclusive).
    complete_matches = [
        c for c in captured
        if c.get("event") == "daemon.entity_linking_complete"
    ]
    assert complete_matches == []


def test_link_entities_in_clusters_emits_no_eligible_clusters_when_cluster_lacks_entity_regular_pair(
    daemon_and_vault,
) -> None:
    """A two-member cluster that's all-entity (matter+matter) or
    all-regular (event+event) doesn't qualify — the helper short-
    circuits on ``if not entities_by_type or not regulars``. The
    no-eligible-clusters log fires.
    """
    daemon, vault = daemon_and_vault
    _write(vault, "event/e1.md", "event")
    _write(vault, "event/e2.md", "event")

    records = {
        "event/e1.md": _record("event/e1.md", "event"),
        "event/e2.md": _record("event/e2.md", "event"),
    }
    paths = ["event/e1.md", "event/e2.md"]
    vectors = np.asarray([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)

    changed_cluster_ids = {42}
    cluster_members = {42: ["event/e1.md", "event/e2.md"]}

    with capture_logs() as captured:
        daemon._link_entities_in_clusters(
            changed_cluster_ids, cluster_members, records, paths, vectors,
        )

    matches = [
        c for c in captured
        if c.get("event") == "surveyor.entity_linking.no_eligible_clusters"
    ]
    assert len(matches) == 1
    assert matches[0]["changed_clusters_total"] == 1


def test_link_entities_in_clusters_emits_complete_not_no_eligible_on_happy_path(
    daemon_and_vault,
) -> None:
    """Positive control: when a cluster IS eligible
    (entity + regular pair, vectors above threshold), the original
    ``daemon.entity_linking_complete`` log fires AND the
    no-eligible-clusters log does NOT. Pins the mutual exclusivity
    of the two log paths.
    """
    daemon, vault = daemon_and_vault
    _write(vault, "matter/m1.md", "matter")
    _write(vault, "event/e1.md", "event")

    records = {
        "matter/m1.md": _record("matter/m1.md", "matter"),
        "event/e1.md": _record("event/e1.md", "event"),
    }
    paths = ["matter/m1.md", "event/e1.md"]
    # Identical normalized vectors → similarity 1.0 >> threshold 0.75
    vectors = np.asarray([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)

    changed_cluster_ids = {7}
    cluster_members = {7: ["matter/m1.md", "event/e1.md"]}

    with capture_logs() as captured:
        daemon._link_entities_in_clusters(
            changed_cluster_ids, cluster_members, records, paths, vectors,
        )

    complete = [
        c for c in captured
        if c.get("event") == "daemon.entity_linking_complete"
    ]
    no_eligible = [
        c for c in captured
        if c.get("event") == "surveyor.entity_linking.no_eligible_clusters"
    ]
    assert len(complete) == 1, (
        "happy path must emit entity_linking_complete; events: "
        f"{[c.get('event') for c in captured]}"
    )
    assert no_eligible == [], (
        "happy path must NOT emit no_eligible_clusters; events: "
        f"{[c.get('event') for c in captured]}"
    )


# ===========================================================================
# Site 2 — _link_noise_points_to_entities zero-vectored-noise
# ===========================================================================


def test_link_noise_points_to_entities_emits_no_vectored_when_records_absent(
    daemon_and_vault,
) -> None:
    """When ``noise_paths`` is non-empty but every entry's
    ``records.get()`` returns None (state-staleness: vector store
    has paths that the in-memory records map doesn't), the helper
    short-circuits and ``noise_processed`` stays 0. The new
    ``surveyor.entity_linking.no_vectored_noise_points`` log fires.
    """
    daemon, vault = daemon_and_vault
    _write(vault, "matter/anchor.md", "matter")
    # records ONLY has the matter; noise_paths references absent records
    records = {
        "matter/anchor.md": _record("matter/anchor.md", "matter"),
    }
    paths = ["matter/anchor.md"]
    vectors = np.asarray([[1.0, 0.0]], dtype=np.float32)

    noise_paths = ["session/missing-1.md", "session/missing-2.md"]

    with capture_logs() as captured:
        daemon._link_noise_points_to_entities(
            noise_paths, records, paths, vectors,
        )

    matches = [
        c for c in captured
        if c.get("event") == "surveyor.entity_linking.no_vectored_noise_points"
    ]
    assert len(matches) == 1, (
        "expected no_vectored_noise_points log when noise records "
        f"absent from records map; got events: "
        f"{[c.get('event') for c in captured]}"
    )
    entry = matches[0]
    assert entry["noise_paths_total"] == 2
    assert entry["threshold"] == daemon.cfg.entity_link.threshold
    assert entry["max_per_record"] == daemon.cfg.entity_link.max_per_record


def test_link_noise_points_to_entities_emits_complete_not_no_vectored_on_happy_path(
    daemon_and_vault,
) -> None:
    """Positive control: when a noise point has both a record AND a
    vector, ``noise_processed`` increments to ≥1 and the original
    ``daemon.noise_linking_complete`` log fires. The
    no_vectored_noise_points log does NOT.
    """
    daemon, vault = daemon_and_vault
    _write(vault, "matter/anchor.md", "matter")
    _write(vault, "event/noisy.md", "event")

    records = {
        "matter/anchor.md": _record("matter/anchor.md", "matter"),
        "event/noisy.md": _record("event/noisy.md", "event"),
    }
    paths = ["matter/anchor.md", "event/noisy.md"]
    vectors = np.asarray([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)

    noise_paths = ["event/noisy.md"]

    with capture_logs() as captured:
        daemon._link_noise_points_to_entities(
            noise_paths, records, paths, vectors,
        )

    complete = [
        c for c in captured
        if c.get("event") == "daemon.noise_linking_complete"
    ]
    no_vectored = [
        c for c in captured
        if c.get("event") == "surveyor.entity_linking.no_vectored_noise_points"
    ]
    assert len(complete) == 1
    assert no_vectored == []


# ===========================================================================
# Site 3 — _backfill_new_entities no-vectored-entities
# ===========================================================================


def test_backfill_new_entities_emits_no_vectored_when_paths_absent_from_records(
    daemon_and_vault,
) -> None:
    """When ``new_entity_paths`` is non-empty but every path's
    ``records.get()`` returns None (or the path's record_type is
    NOT in ENTITY_RECORD_TYPES), ``entities_processed`` stays 0
    and the new
    ``surveyor.entity_linking.no_vectored_entities_for_backfill``
    log fires.
    """
    daemon, vault = daemon_and_vault
    _write(vault, "event/e1.md", "event")
    records = {
        "event/e1.md": _record("event/e1.md", "event"),
    }
    paths = ["event/e1.md"]
    vectors = np.asarray([[1.0, 0.0]], dtype=np.float32)

    # Three "new entity paths" — none of them resolve to a known
    # entity record (e1 is an event, not entity; the others don't
    # exist in records at all).
    new_entity_paths = [
        "matter/never-created.md",
        "person/also-missing.md",
        "event/e1.md",  # wrong type — event isn't an ENTITY_RECORD_TYPE
    ]

    with capture_logs() as captured:
        daemon._backfill_new_entities(
            new_entity_paths, records, paths, vectors,
        )

    matches = [
        c for c in captured
        if c.get("event")
        == "surveyor.entity_linking.no_vectored_entities_for_backfill"
    ]
    assert len(matches) == 1, (
        "expected no_vectored_entities_for_backfill log when no "
        f"candidates resolve; got events: "
        f"{[c.get('event') for c in captured]}"
    )
    entry = matches[0]
    assert entry["new_entity_paths_total"] == 3
    assert entry["threshold"] == daemon.cfg.entity_link.threshold
    assert entry["max_per_record"] == daemon.cfg.entity_link.max_per_record


def test_backfill_new_entities_emits_complete_not_no_vectored_on_happy_path(
    daemon_and_vault,
) -> None:
    """Positive control: when a new entity path resolves cleanly,
    the original ``daemon.entity_backfill_complete`` log fires; the
    no_vectored_entities_for_backfill log does NOT.
    """
    daemon, vault = daemon_and_vault
    _write(vault, "matter/new.md", "matter")
    _write(vault, "event/old.md", "event")
    records = {
        "matter/new.md": _record("matter/new.md", "matter"),
        "event/old.md": _record("event/old.md", "event"),
    }
    paths = ["matter/new.md", "event/old.md"]
    vectors = np.asarray([[1.0, 0.0], [1.0, 0.0]], dtype=np.float32)

    new_entity_paths = ["matter/new.md"]

    with capture_logs() as captured:
        daemon._backfill_new_entities(
            new_entity_paths, records, paths, vectors,
        )

    complete = [
        c for c in captured
        if c.get("event") == "daemon.entity_backfill_complete"
    ]
    no_vectored = [
        c for c in captured
        if c.get("event")
        == "surveyor.entity_linking.no_vectored_entities_for_backfill"
    ]
    assert len(complete) == 1
    assert no_vectored == []


# ===========================================================================
# Site 4 — _cluster_and_label caller-side no-noise-points gate
# ===========================================================================


@pytest.mark.asyncio
async def test_cluster_and_label_emits_no_noise_points_when_hdbscan_finds_no_noise(
    daemon_and_vault,
) -> None:
    """When HDBSCAN produces a ``result.semantic`` map with NO
    ``cluster_id == -1`` entries, ``noise_paths`` is empty and stage 6
    is skipped silently. The new
    ``surveyor.entity_linking.no_noise_points`` log fires from the
    caller-side ``else`` branch.

    Drives the real ``_cluster_and_label`` with a stubbed clusterer
    so the call site is exercised by production code (per
    ``feedback_log_emission_test_pattern.md``).
    """
    daemon, vault = daemon_and_vault
    # Use one entity-type record so the
    # ``no_entities_in_vault`` gate doesn't short-circuit stage 5/6/7.
    _write(vault, "matter/m.md", "matter")
    _write(vault, "event/e.md", "event")
    records = {
        "matter/m.md": _record("matter/m.md", "matter"),
        "event/e.md": _record("event/e.md", "event"),
    }
    fake_paths = list(records.keys())
    fake_vectors = np.asarray(
        [[1.0, 0.0], [1.0, 0.0]], dtype=np.float32,
    )
    daemon.embedder.get_all_embeddings = MagicMock(
        return_value=(fake_paths, fake_vectors),
    )

    # Cluster result: both records assigned to cluster 0, NO noise.
    # changed_semantic = {0} so we get past the no_changed_clusters
    # early-return and reach the noise-path computation at L588.
    result = ClusterResult(
        semantic={"matter/m.md": 0, "event/e.md": 0},
        structural={"matter/m.md": 0, "event/e.md": 0},
        changed_semantic={0},
        changed_structural=set(),
    )
    daemon.clusterer.run = MagicMock(return_value=result)
    # Stub the labeler so stage 4 returns without LLM calls.
    daemon.labeler = MagicMock()

    async def _empty_label(*_args, **_kwargs):
        return []

    async def _empty_rels(*_args, **_kwargs):
        return []

    daemon.labeler.label_cluster = _empty_label
    daemon.labeler.suggest_relationships = _empty_rels
    daemon.labeler.max_concurrent = 1
    daemon.cfg.labeler.max_concurrent = 1
    daemon.cfg.labeler.min_cluster_size_to_label = 1
    # Disable MOC suggestion stage so the test doesn't trip the
    # configured-off path (separate code surface).
    daemon.cfg.moc_suggestion.enabled = False

    with capture_logs() as captured:
        await daemon._cluster_and_label(records)

    matches = [
        c for c in captured
        if c.get("event") == "surveyor.entity_linking.no_noise_points"
    ]
    assert len(matches) == 1, (
        "expected no_noise_points log when HDBSCAN produced zero "
        f"noise points; got events: "
        f"{[c.get('event') for c in captured]}"
    )
    entry = matches[0]
    # semantic_cluster_count = unique non-noise cluster IDs → 1 (just 0)
    assert entry["semantic_cluster_count"] == 1
    # total_clustered_paths = len(result.semantic) → 2
    assert entry["total_clustered_paths"] == 2


@pytest.mark.asyncio
async def test_cluster_and_label_does_not_emit_no_noise_points_when_noise_exists(
    daemon_and_vault,
) -> None:
    """Positive control: when HDBSCAN DOES produce noise points,
    ``_link_noise_points_to_entities`` runs and the
    no_noise_points caller-side log does NOT fire.
    """
    daemon, vault = daemon_and_vault
    _write(vault, "matter/m.md", "matter")
    _write(vault, "event/e1.md", "event")
    _write(vault, "event/e2.md", "event")
    records = {
        "matter/m.md": _record("matter/m.md", "matter"),
        "event/e1.md": _record("event/e1.md", "event"),
        "event/e2.md": _record("event/e2.md", "event"),
    }
    fake_paths = list(records.keys())
    fake_vectors = np.asarray(
        [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]], dtype=np.float32,
    )
    daemon.embedder.get_all_embeddings = MagicMock(
        return_value=(fake_paths, fake_vectors),
    )

    # Cluster result: m + e1 in cluster 0, e2 in noise (-1).
    result = ClusterResult(
        semantic={"matter/m.md": 0, "event/e1.md": 0, "event/e2.md": -1},
        structural={
            "matter/m.md": 0, "event/e1.md": 0, "event/e2.md": 0,
        },
        changed_semantic={0},
        changed_structural=set(),
    )
    daemon.clusterer.run = MagicMock(return_value=result)
    daemon.labeler = MagicMock()

    async def _empty_label(*_args, **_kwargs):
        return []

    async def _empty_rels(*_args, **_kwargs):
        return []

    daemon.labeler.label_cluster = _empty_label
    daemon.labeler.suggest_relationships = _empty_rels
    daemon.labeler.max_concurrent = 1
    daemon.cfg.labeler.max_concurrent = 1
    daemon.cfg.labeler.min_cluster_size_to_label = 1
    daemon.cfg.moc_suggestion.enabled = False

    with capture_logs() as captured:
        await daemon._cluster_and_label(records)

    no_noise = [
        c for c in captured
        if c.get("event") == "surveyor.entity_linking.no_noise_points"
    ]
    assert no_noise == [], (
        "expected NO no_noise_points log when HDBSCAN produced noise; "
        f"got events: {[c.get('event') for c in captured]}"
    )
