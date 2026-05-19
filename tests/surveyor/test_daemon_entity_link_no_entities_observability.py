"""Tests for daemon._gate_entity_link_no_entities_observability.

Phase 5 Sub-arc B observability ship: when a vault has zero entity
records (``matter``/``person``/``org``/``project``), the three
entity-link helpers (``_link_entities_in_clusters``,
``_link_noise_points_to_entities``, ``_backfill_new_entities``)
silently no-op. Hypatia's vault is the canonical example: it has no
entity record types at all. Per ``feedback_intentionally_left_blank.md``,
silence-from-no-data must surface distinctly from silence-from-failure.

The gate at ``daemon._gate_entity_link_no_entities_observability``
emits ``surveyor.entity_link_no_entities_in_vault`` once per
daemon lifecycle when the empty-entities state is first observed;
subsequent sweeps in the same empty state do NOT re-emit. A
sweep that DOES find entities resets the latch so a transition
back to empty re-emits.

Per ``feedback_log_emission_test_pattern.md``: the observability
log emission must be pinned via ``structlog.testing.capture_logs``
or future refactors will silently degrade the operator's grep
workflow.
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


def _make_config(vault: Path, state_path: Path) -> PipelineConfig:
    return PipelineConfig(
        vault=VaultConfig(path=vault),
        watcher=WatcherConfig(),
        ollama=OllamaConfig(),
        milvus=MilvusConfig(uri=str(state_path.parent / "milvus.db")),
        clustering=ClusteringConfig(
            hdbscan=HdbscanConfig(), leiden=LeidenConfig(),
        ),
        openrouter=OpenRouterConfig(api_key="x"),
        labeler=LabelerConfig(),
        state=StateConfig(path=str(state_path)),
        logging=LoggingConfig(),
        entity_link=EntityLinkConfig(),
    )


def _record(rel: str, rt: str) -> VaultRecord:
    return VaultRecord(
        rel_path=rel,
        frontmatter={"type": rt, "name": "x"},
        body="",
        record_type=rt,
    )


@pytest.fixture
def daemon(tmp_path):
    """Stub daemon — gate helper depends only on cfg + the lifecycle
    latch attribute, so the heavy collaborators stay mocked.
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    state_path = tmp_path / "state.json"
    cfg = _make_config(vault, state_path)

    d = Daemon.__new__(Daemon)
    d.cfg = cfg
    d.state = PipelineState(state_path=cfg.state.path)
    d.embedder = MagicMock()
    d.writer = MagicMock()
    d.watcher = MagicMock()
    d.clusterer = MagicMock()
    d.labeler = MagicMock()
    d._shutdown_requested = False
    # Initialise the once-per-lifecycle gate latch — the real
    # ``__init__`` sets this, but ``__new__`` skips ``__init__``.
    d._entity_link_no_entities_logged = False
    return d


def test_first_sweep_with_no_entities_emits_observability_log(daemon):
    """First sweep on a vault with zero entity records emits the
    ``surveyor.entity_link_no_entities_in_vault`` log so operators
    grepping for the no-op-cause can confirm idle-from-no-data
    rather than idle-from-failure.

    Asserts the full field shape required by the spec:
      * ``entity_types_searched`` — the 4-type set the gate scanned for
      * ``vault_path`` — instance-disambiguating identifier
    """
    # Hypatia-shaped: only non-entity record types
    records = {
        "session/s1.md": _record("session/s1.md", "session"),
        "session/s2.md": _record("session/s2.md", "session"),
        "log/l1.md": _record("log/l1.md", "log"),
    }

    with capture_logs() as captured:
        result = daemon._gate_entity_link_no_entities_observability(records)

    # Gate returns False so caller skips stage 5/6/7
    assert result is False
    # Latch flipped so subsequent empty-state sweeps don't re-emit
    assert daemon._entity_link_no_entities_logged is True

    matches = [
        c for c in captured
        if c.get("event") == "surveyor.entity_link_no_entities_in_vault"
    ]
    assert len(matches) == 1, (
        f"expected exactly 1 observability log, got {len(matches)}: "
        f"{[c.get('event') for c in captured]}"
    )
    entry = matches[0]
    # Pin the searched-types set — the 4 entity types
    # (matter/person/org/project) are the closed contract surveyor's
    # entity-link stages care about. Future widening of the set should
    # update this assertion lockstep.
    assert entry.get("entity_types_searched") == [
        "matter", "org", "person", "project",
    ]
    # vault_path provides instance disambiguation for operators running
    # multiple instances (Salem, KAL-LE, Hypatia) — without it the log
    # is ambiguous across daemons.
    assert entry.get("vault_path") == str(daemon.cfg.vault.path)


def test_second_sweep_still_no_entities_does_not_reemit(daemon):
    """Once-per-lifecycle idempotency: a subsequent sweep that still
    finds no entities MUST NOT re-emit the log. The first emission
    flipped the latch; the second invocation respects it.

    Gate-held-by-state semantics (per spec) — distinct from once-
    per-sweep emission which would fire on every tick (too noisy
    for an idle Hypatia daemon's log file).
    """
    records = {
        "session/s1.md": _record("session/s1.md", "session"),
        "log/l1.md": _record("log/l1.md", "log"),
    }

    # First sweep: emits and flips the latch
    with capture_logs() as first_captured:
        first_result = daemon._gate_entity_link_no_entities_observability(
            records,
        )
    assert first_result is False
    assert daemon._entity_link_no_entities_logged is True
    first_matches = [
        c for c in first_captured
        if c.get("event") == "surveyor.entity_link_no_entities_in_vault"
    ]
    assert len(first_matches) == 1

    # Second sweep: still no entities, must NOT re-emit
    with capture_logs() as second_captured:
        second_result = daemon._gate_entity_link_no_entities_observability(
            records,
        )

    assert second_result is False
    # Latch must remain True
    assert daemon._entity_link_no_entities_logged is True
    second_matches = [
        c for c in second_captured
        if c.get("event") == "surveyor.entity_link_no_entities_in_vault"
    ]
    assert second_matches == [], (
        "expected no re-emission on second empty-state sweep, got: "
        f"{second_matches}"
    )


# ---------------------------------------------------------------------------
# Regression pin — placement-fix for the Hypatia smoke-test gap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_fires_even_when_no_clusters_changed(daemon):
    """Regression pin — Sub-arc B smoke test on Hypatia 2026-05-19
    caught the original gate placement BELOW the
    ``daemon.no_changed_clusters`` early-return inside
    ``_cluster_and_label``. Hypatia's first sweep produced 81 embedded
    records but the clusterer reported ``changed_semantic=set()``
    (no membership shift since last tick), so the early-return
    short-circuited the method before the gate could emit — exactly
    the Hypatia case Sub-arc B was designed to surface.

    Pin: when ``clusterer.run()`` returns a ClusterResult with empty
    ``changed_semantic``, the gate MUST still emit the observability
    log AND the function returns cleanly (via the no_changed_clusters
    path). If a future refactor pushes the gate back below the early-
    return, this test fails.

    Drives the real ``_cluster_and_label`` end-to-end with a mocked
    clusterer so the placement is exercised by production code, not
    a test-local mirror (per ``feedback_log_emission_test_pattern.md``).
    """
    # Hypatia-shaped records: no entity types at all
    records = {
        "session/s1.md": _record("session/s1.md", "session"),
        "session/s2.md": _record("session/s2.md", "session"),
        "log/l1.md": _record("log/l1.md", "log"),
    }

    # Embedder returns non-None so we get past the early ``no_embeddings``
    # return; the actual values don't matter because the mocked
    # clusterer doesn't consume them meaningfully.
    fake_paths = list(records.keys())
    fake_vectors = np.zeros((len(fake_paths), 4), dtype=np.float32)
    daemon.embedder.get_all_embeddings = MagicMock(
        return_value=(fake_paths, fake_vectors),
    )

    # Clusterer returns the no-change shape: empty ``changed_semantic``.
    # ``semantic`` map is empty too, matching what a fresh Hypatia sweep
    # would produce. ``_cluster_and_label`` is expected to hit the gate
    # (now placed above the early-return), then return via the
    # ``no_changed_clusters`` log path.
    empty_result = ClusterResult(
        semantic={},
        structural={},
        changed_semantic=set(),
        changed_structural=set(),
    )
    daemon.clusterer.run = MagicMock(return_value=empty_result)

    with capture_logs() as captured:
        await daemon._cluster_and_label(records)

    # The observability gate fired despite no_changed_clusters short-
    # circuit — the headline regression pin for the placement fix.
    obs_logs = [
        c for c in captured
        if c.get("event") == "surveyor.entity_link_no_entities_in_vault"
    ]
    assert len(obs_logs) == 1, (
        "gate must emit when entities absent even on stable-cluster "
        f"sweeps; got events: {[c.get('event') for c in captured]}"
    )
    # Field shape sanity — same contract as the direct-helper test.
    assert obs_logs[0].get("entity_types_searched") == [
        "matter", "org", "person", "project",
    ]
    assert obs_logs[0].get("vault_path") == str(daemon.cfg.vault.path)

    # Latch flipped — subsequent empty-entities sweeps would not
    # re-emit.
    assert daemon._entity_link_no_entities_logged is True

    # Sanity: the no_changed_clusters early-return is still reached
    # (the gate doesn't masquerade as a substitute for that log).
    no_changed_logs = [
        c for c in captured
        if c.get("event") == "daemon.no_changed_clusters"
    ]
    assert len(no_changed_logs) == 1, (
        "no_changed_clusters log must still fire after the gate; "
        f"got events: {[c.get('event') for c in captured]}"
    )

    # Order pin: gate emits BEFORE no_changed_clusters. Surfaces the
    # placement contract directly so a regression that moves the gate
    # back below the early-return reverses the order (or drops the
    # gate emission entirely) and trips this assert.
    events_in_order = [c.get("event") for c in captured]
    gate_idx = events_in_order.index(
        "surveyor.entity_link_no_entities_in_vault",
    )
    no_changed_idx = events_in_order.index("daemon.no_changed_clusters")
    assert gate_idx < no_changed_idx, (
        f"gate must emit BEFORE no_changed_clusters; event order was "
        f"{events_in_order}"
    )
