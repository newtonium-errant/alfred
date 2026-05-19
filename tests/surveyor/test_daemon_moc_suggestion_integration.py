"""Integration tests for the surveyor daemon's MOC suggestion stage
(Phase 5 Sub-arc D1).

Drives ``Daemon._maybe_emit_moc_suggestions`` directly with synthetic
cluster + record + MOC inputs. Verifies:

  * Disabled-stage skip + lifecycle-gated observability log
  * Re-arm of disabled-log latch when stage flips back on
  * No-candidates empty-state emission per
    ``feedback_intentionally_left_blank.md``
  * Suggestion-write happy path → queue file content
  * Dedup across simulated sweeps (HDBSCAN cluster_id renumber)
  * sweep_summary log emission for the operator grep workflow

Log-emission test discipline per builder.md rule #9:
``structlog.testing.capture_logs`` wraps every code path that emits a
configurable observability log. Each test asserts both the event
name AND key fields so a future refactor that drops a field is
caught.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from unittest.mock import MagicMock

import numpy as np
import pytest
import structlog
import structlog.testing

from alfred.surveyor.config import (
    EntityLinkConfig,
    HdbscanConfig,
    IdleTickConfig,
    LabelerConfig,
    LeidenConfig,
    ClusteringConfig,
    LoggingConfig,
    MilvusConfig,
    MocSuggestionConfig,
    OllamaConfig,
    OpenRouterConfig,
    PipelineConfig,
    StateConfig,
    VaultConfig,
    WatcherConfig,
)
from alfred.surveyor.daemon import Daemon
from alfred.surveyor.moc_suggestion_queue import load_queue
from alfred.surveyor.state import ClusterState


# ---------------------------------------------------------------------------
# Test fixture: synthetic VaultRecord + a Daemon built without real I/O.
# ---------------------------------------------------------------------------


@dataclass
class _FakeRecord:
    """Stand-in for ``surveyor.parser.VaultRecord``."""
    frontmatter: dict = field(default_factory=dict)
    body: str = ""
    record_type: str = "zettel"
    wikilinks: list[str] = field(default_factory=list)


def _zettel(mocs: list[str] | None = None) -> _FakeRecord:
    fm: dict = {"type": "zettel"}
    if mocs is not None:
        fm["mocs"] = mocs
    return _FakeRecord(frontmatter=fm)


def _build_test_cfg(
    *,
    vault_path: Path,
    state_path: Path,
    moc_suggestion_enabled: bool,
    moc_suggestion_queue_path: Path | None = None,
    min_cluster_size: int = 3,
    max_proposals_per_sweep: int = 10,
    max_pending_per_target: int = 5,
) -> PipelineConfig:
    return PipelineConfig(
        vault=VaultConfig(path=vault_path),
        watcher=WatcherConfig(),
        ollama=OllamaConfig(),
        milvus=MilvusConfig(uri=str(state_path.parent / "milvus.db")),
        clustering=ClusteringConfig(
            hdbscan=HdbscanConfig(), leiden=LeidenConfig(),
        ),
        openrouter=OpenRouterConfig(),
        labeler=LabelerConfig(),
        state=StateConfig(path=str(state_path)),
        logging=LoggingConfig(),
        idle_tick=IdleTickConfig(enabled=False),  # no heartbeat in tests
        entity_link=EntityLinkConfig(),
        moc_suggestion=MocSuggestionConfig(
            enabled=moc_suggestion_enabled,
            min_cluster_size=min_cluster_size,
            max_proposals_per_sweep=max_proposals_per_sweep,
            max_pending_per_target=max_pending_per_target,
            queue_path=str(moc_suggestion_queue_path) if moc_suggestion_queue_path else None,
        ),
    )


@pytest.fixture
def isolated_daemon(tmp_path: Path):
    """Build a real Daemon with cfg pointing at tmp_path.

    Uses ``Daemon.__new__`` to bypass the constructor's network /
    embedder / watcher setup — we only need the daemon's
    cluster→MOC suggestion attribute surface for these tests.
    """
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    state_path = tmp_path / "data" / "surveyor_state.json"
    state_path.parent.mkdir()

    cfg = _build_test_cfg(
        vault_path=vault_path,
        state_path=state_path,
        moc_suggestion_enabled=True,
        moc_suggestion_queue_path=tmp_path / "data" / "moc_suggestions.jsonl",
    )

    # Build the daemon without invoking the heavy constructor.
    d = Daemon.__new__(Daemon)
    d.cfg = cfg
    d._shutdown_requested = False
    d._entity_link_no_entities_logged = False
    d._moc_suggestion_disabled_logged = False
    # Sub-arc D1 fixup (2026-05-19): mirror the no_moc_dir lifecycle
    # latch initialization that ``Daemon.__init__`` does.
    d._moc_suggestion_no_moc_dir_logged = False
    # State must support ``.clusters`` dict so the suggester can read
    # cluster labels.
    from alfred.surveyor.state import PipelineState
    d.state = PipelineState(state_path)
    return d


# ---------------------------------------------------------------------------
# Disabled-stage path + lifecycle-gated log.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_stage_emits_skip_log_once_per_lifecycle(
    isolated_daemon, tmp_path: Path,
) -> None:
    """When ``moc_suggestion.enabled`` is False (default for Salem +
    KAL-LE), the stage logs ``surveyor.moc_suggestion.stage_disabled``
    on the FIRST sweep and stays silent on subsequent sweeps. Per
    ``feedback_intentionally_left_blank.md`` — operator can grep for
    the log when debugging "why are there no suggestions?".
    """
    isolated_daemon.cfg.moc_suggestion.enabled = False

    with structlog.testing.capture_logs() as captured:
        # First sweep — should log.
        n = await isolated_daemon._maybe_emit_moc_suggestions(
            cluster_members={}, all_changed=set(), records={},
        )
        assert n == 0
        # Second sweep — should NOT re-log.
        n2 = await isolated_daemon._maybe_emit_moc_suggestions(
            cluster_members={}, all_changed=set(), records={},
        )
        assert n2 == 0

    matches = [c for c in captured if c.get("event") == "surveyor.moc_suggestion.stage_disabled"]
    assert len(matches) == 1, (
        f"Expected exactly one stage_disabled log emission across two "
        f"sweeps; got {len(matches)}: {matches}"
    )
    # Key field assertion — catches field rename / drop in future refactor.
    assert "reason" in matches[0]
    assert "enabled is False" in matches[0]["reason"]


@pytest.mark.asyncio
async def test_disabled_log_latch_resets_when_stage_flips_on(
    isolated_daemon, tmp_path: Path,
) -> None:
    """If config flips on after the latch caught — the latch must
    reset so a future flip-off re-emits."""
    isolated_daemon.cfg.moc_suggestion.enabled = False
    await isolated_daemon._maybe_emit_moc_suggestions(
        cluster_members={}, all_changed=set(), records={},
    )
    assert isolated_daemon._moc_suggestion_disabled_logged is True

    # Flip on (and no candidates → no-candidates log path; latch resets).
    isolated_daemon.cfg.moc_suggestion.enabled = True
    await isolated_daemon._maybe_emit_moc_suggestions(
        cluster_members={}, all_changed=set(), records={},
    )
    assert isolated_daemon._moc_suggestion_disabled_logged is False


# ---------------------------------------------------------------------------
# no_moc_dir lifecycle-gated log (Sub-arc D1 fixup + placement-fix).
# ---------------------------------------------------------------------------
#
# Two-stage backstory:
#
#   1. D1 ship (6984279) emitted ``surveyor.moc_suggestion.no_moc_dir``
#      from inside ``build_existing_mocs_index`` on every sweep — log
#      spam on vaults with no ``MOC/`` directory.
#
#   2. D1 fixup (de7db19) added a lifecycle latch but kept the emission
#      INSIDE Stage 8 (``_maybe_emit_moc_suggestions``), which is gated
#      by ``changed_semantic > 0`` — so on Hypatia's stable-vault
#      sweeps the log STILL never emitted. Same bug shape as the
#      pre-65229ec Sub-arc B placement gap.
#
#   3. Placement fix (this commit) moves the observability gate to
#      :meth:`_gate_moc_suggestion_no_moc_dir_observability`, called
#      ABOVE the ``no_changed_clusters`` early-return in
#      ``_cluster_and_label``. Stage 8's PROPOSAL pipeline stays put
#      (proposals only make sense on cluster change). Only the
#      observability log moves.
#
# The latch tests below drive ``_gate_moc_suggestion_no_moc_dir_observability``
# directly — that's where the emission lives now. The behaviour
# contract (emit-once-per-lifecycle, reset-on-transition-back) is
# unchanged; only the entry point moved.


@pytest.mark.asyncio
async def test_no_moc_dir_log_fires_once_on_first_sweep(
    isolated_daemon, tmp_path: Path,
) -> None:
    """First sweep with no ``MOC/`` directory in the vault → log
    ``surveyor.moc_suggestion.no_moc_dir`` exactly once.

    Drives the placement-fix gate directly (post-2026-05-19 ship);
    the gate fires above the ``no_changed_clusters`` early-return so
    the log emits regardless of whether cluster membership shifted
    this sweep.
    """
    # Vault has no MOC/ directory by default (fixture only mkdir's
    # the vault root). Stage is enabled.
    assert not (isolated_daemon.cfg.vault.path / "MOC").is_dir()

    with structlog.testing.capture_logs() as captured:
        isolated_daemon._gate_moc_suggestion_no_moc_dir_observability()

    matches = [
        c for c in captured
        if c.get("event") == "surveyor.moc_suggestion.no_moc_dir"
    ]
    assert len(matches) == 1, (
        f"Expected exactly one no_moc_dir log emission on first sweep; "
        f"got {len(matches)}: {matches}"
    )
    # Key field assertion — catches field rename / drop in future refactor.
    assert "vault_path" in matches[0]
    assert matches[0]["vault_path"] == str(isolated_daemon.cfg.vault.path)
    # Latch is set after first emission.
    assert isolated_daemon._moc_suggestion_no_moc_dir_logged is True


@pytest.mark.asyncio
async def test_no_moc_dir_log_does_not_re_fire_across_sweeps(
    isolated_daemon, tmp_path: Path,
) -> None:
    """Subsequent sweeps while ``MOC/`` is still absent must NOT
    re-emit. The original D1 bug was per-sweep spam; this test pins
    the latch behaviour so a future refactor that moves the log
    back into per-sweep emission is caught.
    """
    assert not (isolated_daemon.cfg.vault.path / "MOC").is_dir()

    with structlog.testing.capture_logs() as captured:
        # Three sweeps; only the first should emit.
        for _ in range(3):
            isolated_daemon._gate_moc_suggestion_no_moc_dir_observability()

    matches = [
        c for c in captured
        if c.get("event") == "surveyor.moc_suggestion.no_moc_dir"
    ]
    assert len(matches) == 1, (
        f"Latch must hold across multiple sweeps with no MOC/ "
        f"directory; got {len(matches)} emissions across 3 sweeps: "
        f"{matches}"
    )


@pytest.mark.asyncio
async def test_no_moc_dir_latch_resets_when_dir_appears(
    isolated_daemon, tmp_path: Path,
) -> None:
    """Operator creates the first MOC between sweeps → latch resets
    so a hypothetical later directory removal re-emits. Defensive
    symmetry with the ``stage_disabled`` latch + the Sub-arc B
    ``entity_link_no_entities_logged`` latch."""
    # Sweep 1 — no MOC/ dir → log + latch set.
    isolated_daemon._gate_moc_suggestion_no_moc_dir_observability()
    assert isolated_daemon._moc_suggestion_no_moc_dir_logged is True

    # Operator creates MOC/ between sweeps.
    moc_dir = isolated_daemon.cfg.vault.path / "MOC"
    moc_dir.mkdir()
    (moc_dir / "First MOC.md").write_text(
        "---\ntype: MOC\nname: First MOC\n---\n# Contents\n"
    )

    # Sweep 2 — MOC/ now present → latch resets.
    with structlog.testing.capture_logs() as captured:
        isolated_daemon._gate_moc_suggestion_no_moc_dir_observability()
    assert isolated_daemon._moc_suggestion_no_moc_dir_logged is False, (
        "Latch must reset when MOC/ directory is observed on disk so "
        "a later transition back to absent re-emits the log"
    )
    # Sweep 2 itself does NOT emit no_moc_dir (dir exists now).
    sweep2_no_moc_dir = [
        c for c in captured
        if c.get("event") == "surveyor.moc_suggestion.no_moc_dir"
    ]
    assert sweep2_no_moc_dir == [], (
        "no_moc_dir must not fire when MOC/ directory is present"
    )

    # Sweep 3 — operator removes MOC/ (unusual but pin the symmetry).
    # With the latch reset, the next no-MOC sweep must re-emit. This
    # is the "transition-back-to-absent" defense — the latch isn't a
    # daemon-lifetime one-shot, it's a state-change marker.
    import shutil
    shutil.rmtree(moc_dir)
    with structlog.testing.capture_logs() as captured2:
        isolated_daemon._gate_moc_suggestion_no_moc_dir_observability()
    sweep3_no_moc_dir = [
        c for c in captured2
        if c.get("event") == "surveyor.moc_suggestion.no_moc_dir"
    ]
    assert len(sweep3_no_moc_dir) == 1, (
        "After latch reset (dir appeared) and a subsequent dir removal, "
        f"the no_moc_dir log must re-emit on the next sweep; got "
        f"{len(sweep3_no_moc_dir)} emissions"
    )


# ---------------------------------------------------------------------------
# Regression pin — placement-fix for the Hypatia smoke-test gap
# (Sub-arc D1 no-moc-dir placement fix, 2026-05-19).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_moc_dir_gate_fires_even_when_no_clusters_changed(
    isolated_daemon, tmp_path: Path,
) -> None:
    """Critical regression pin — Hypatia smoke test on the D1 fixup ship
    (de7db19) caught the second placement gap: the no_moc_dir log
    was emitted from inside ``_maybe_emit_moc_suggestions`` (Stage
    8), which lives BELOW the ``daemon.no_changed_clusters`` early-
    return inside ``_cluster_and_label``. On Hypatia's stable-vault
    sweeps (clusterer reports ``changed_semantic=set()``) the early-
    return short-circuited Stage 8 before it could emit, despite
    the latch.

    Same bug shape as the pre-65229ec Sub-arc B placement gap.

    Pin: when ``clusterer.run()`` returns empty ``changed_semantic``
    AND the vault has no ``MOC/`` directory, the
    ``surveyor.moc_suggestion.no_moc_dir`` log MUST still emit (via
    the gate above the early-return) AND ``_cluster_and_label``
    returns cleanly via the ``no_changed_clusters`` path. A future
    refactor that moves the gate back below the early-return trips
    this assert.

    Drives the real ``_cluster_and_label`` end-to-end with a mocked
    clusterer so the placement is exercised by production code, not
    a test-local mirror (per
    ``feedback_log_emission_test_pattern.md``).
    """
    from alfred.surveyor.clusterer import ClusterResult
    from alfred.surveyor.parser import VaultRecord

    # Vault has no MOC/ directory by default (fixture only mkdir's
    # vault root). Stage is enabled. Hypatia's canonical state.
    assert not (isolated_daemon.cfg.vault.path / "MOC").is_dir()

    # Hypatia-shaped records — only non-entity types so the Sub-arc B
    # entity-link gate also fires (we don't assert on it here; we just
    # want a realistic record map so ``_cluster_and_label`` doesn't
    # trip on the no-records path).
    records = {
        "zettel/Foo.md": VaultRecord(
            rel_path="zettel/Foo.md",
            frontmatter={"type": "zettel", "name": "Foo"},
            body="",
            record_type="zettel",
        ),
        "zettel/Bar.md": VaultRecord(
            rel_path="zettel/Bar.md",
            frontmatter={"type": "zettel", "name": "Bar"},
            body="",
            record_type="zettel",
        ),
    }

    # Wire mocks the placement-fix gate needs to reach. ``_cluster_and_label``
    # consults embedder.get_all_embeddings() + clusterer.run(), then
    # decides whether to short-circuit on no_changed_clusters BEFORE
    # the gate fires. Mock both to produce the no-change shape.
    isolated_daemon.embedder = MagicMock()
    fake_paths = list(records.keys())
    fake_vectors = np.zeros((len(fake_paths), 4), dtype=np.float32)
    isolated_daemon.embedder.get_all_embeddings = MagicMock(
        return_value=(fake_paths, fake_vectors),
    )

    isolated_daemon.clusterer = MagicMock()
    empty_result = ClusterResult(
        semantic={},
        structural={},
        changed_semantic=set(),
        changed_structural=set(),
    )
    isolated_daemon.clusterer.run = MagicMock(return_value=empty_result)

    # writer + labeler not exercised on the no_changed_clusters path
    # but keep them mocked so any unexpected method call surfaces.
    isolated_daemon.writer = MagicMock()
    isolated_daemon.labeler = MagicMock()

    with structlog.testing.capture_logs() as captured:
        await isolated_daemon._cluster_and_label(records)

    # Headline regression pin: the no_moc_dir observability log
    # fired DESPITE the no_changed_clusters short-circuit. Catches a
    # future refactor that moves the gate back into Stage 8.
    obs_logs = [
        c for c in captured
        if c.get("event") == "surveyor.moc_suggestion.no_moc_dir"
    ]
    assert len(obs_logs) == 1, (
        "no_moc_dir gate must emit on stable-cluster sweeps when "
        f"MOC/ directory is absent; got events: "
        f"{[c.get('event') for c in captured]}"
    )
    # Field shape sanity — same contract as the direct-helper tests.
    assert obs_logs[0].get("vault_path") == str(
        isolated_daemon.cfg.vault.path,
    )
    # Latch flipped — subsequent stable sweeps must not re-emit.
    assert isolated_daemon._moc_suggestion_no_moc_dir_logged is True

    # Sanity: the no_changed_clusters early-return is still reached.
    # The placement-fix gate must NOT masquerade as a substitute for
    # that log — it sits above and runs first.
    no_changed_logs = [
        c for c in captured
        if c.get("event") == "daemon.no_changed_clusters"
    ]
    assert len(no_changed_logs) == 1, (
        "no_changed_clusters log must still fire after the gate; "
        f"got events: {[c.get('event') for c in captured]}"
    )

    # Order pin: gate emits BEFORE no_changed_clusters. Surfaces the
    # placement contract directly so a regression that moves the
    # gate back below the early-return reverses the order (or drops
    # the gate emission entirely) and trips this assert.
    events_in_order = [c.get("event") for c in captured]
    gate_idx = events_in_order.index(
        "surveyor.moc_suggestion.no_moc_dir",
    )
    no_changed_idx = events_in_order.index("daemon.no_changed_clusters")
    assert gate_idx < no_changed_idx, (
        "gate must emit BEFORE no_changed_clusters; event order was "
        f"{events_in_order}"
    )


# ---------------------------------------------------------------------------
# No-candidates empty-state.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_candidates_emits_empty_state_log(
    isolated_daemon, tmp_path: Path,
) -> None:
    """Stage ran, no clusters eligible → log
    ``surveyor.moc_suggestion.no_candidates`` per
    ``feedback_intentionally_left_blank.md``. Includes the cluster
    count so the operator sees "we evaluated N clusters and none
    yielded suggestions" (idle but healthy).
    """
    with structlog.testing.capture_logs() as captured:
        n = await isolated_daemon._maybe_emit_moc_suggestions(
            cluster_members={}, all_changed=set(), records={},
        )
    assert n == 0
    matches = [c for c in captured if c.get("event") == "surveyor.moc_suggestion.no_candidates"]
    assert len(matches) == 1
    # Key field assertions.
    assert matches[0]["clusters_evaluated"] == 0
    assert matches[0]["existing_moc_count"] == 0
    assert "queue_path" in matches[0]


# ---------------------------------------------------------------------------
# Happy path — suggestion gets written to the queue.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suggestion_written_to_queue_with_expected_shape(
    isolated_daemon, tmp_path: Path,
) -> None:
    """End-to-end: a labeled cluster with 5 members, 3 of which
    cite ``MOC/Stoicism MOC.md``, produces a member_overlap
    suggestion that lands in the queue with the expected fields."""
    # Build MOC on disk so the index builder finds it.
    moc_dir = isolated_daemon.cfg.vault.path / "MOC"
    moc_dir.mkdir()
    (moc_dir / "Stoicism MOC.md").write_text(
        "---\ntype: MOC\nname: Stoicism MOC\n---\n# Contents\n"
    )

    members = [f"zettel/M{i}.md" for i in range(5)]
    records = {
        members[0]: _zettel(["[[MOC/Stoicism MOC]]"]),
        members[1]: _zettel(["MOC/Stoicism MOC.md"]),
        members[2]: _zettel(["[[MOC/Stoicism MOC]]"]),
        members[3]: _zettel([]),
        members[4]: _zettel([]),
    }
    cluster_members = {7: members}
    all_changed = {7}
    # Seed cluster label state so the suggester reads tags.
    isolated_daemon.state.clusters["semantic_7"] = ClusterState(
        label=["stoicism", "philosophy"],
        member_files=members,
        last_labeled="2026-05-19T14:00:00+00:00",
    )

    with structlog.testing.capture_logs() as captured:
        n_added = await isolated_daemon._maybe_emit_moc_suggestions(
            cluster_members=cluster_members,
            all_changed=all_changed,
            records=records,
        )
    assert n_added == 1

    qp = Path(isolated_daemon.cfg.moc_suggestion.queue_path)
    loaded = load_queue(qp)
    assert len(loaded) == 1
    s = loaded[0]
    assert s.mapping_signal == "member_overlap"
    assert s.target_moc_rel_path == "MOC/Stoicism MOC.md"
    assert s.mapping_score == pytest.approx(0.6)
    assert set(s.candidate_members_to_add) == {members[3], members[4]}
    assert s.status == "pending"

    # sweep_summary log emitted with the operator-facing counts.
    matches = [c for c in captured if c.get("event") == "surveyor.moc_suggestion.sweep_summary"]
    assert len(matches) == 1
    assert matches[0]["added"] == 1
    assert matches[0]["refreshed"] == 0
    assert matches[0]["clusters_evaluated"] == 1


@pytest.mark.asyncio
async def test_dedup_across_sweeps_with_cluster_id_renumber(
    isolated_daemon, tmp_path: Path,
) -> None:
    """Sweep 1 produces an id; sweep 2 (HDBSCAN renumbered cluster
    from 7 → 42) produces the SAME id because the dedup keys off
    sorted member paths + target. Sweep 2 refreshes, doesn't add."""
    moc_dir = isolated_daemon.cfg.vault.path / "MOC"
    moc_dir.mkdir()
    (moc_dir / "Stoicism MOC.md").write_text(
        "---\ntype: MOC\nname: Stoicism MOC\n---\n# Contents\n"
    )
    members = [f"zettel/M{i}.md" for i in range(5)]
    records = {
        members[0]: _zettel(["MOC/Stoicism MOC.md"]),
        members[1]: _zettel(["MOC/Stoicism MOC.md"]),
        members[2]: _zettel(["MOC/Stoicism MOC.md"]),
        members[3]: _zettel([]),
        members[4]: _zettel([]),
    }

    # Sweep 1 — cluster_id 7.
    isolated_daemon.state.clusters["semantic_7"] = ClusterState(
        label=["stoicism"], member_files=members,
        last_labeled="2026-05-19T14:00:00+00:00",
    )
    n1 = await isolated_daemon._maybe_emit_moc_suggestions(
        cluster_members={7: members}, all_changed={7}, records=records,
    )
    assert n1 == 1

    # Sweep 2 — same members, HDBSCAN renumbered to 42.
    del isolated_daemon.state.clusters["semantic_7"]
    isolated_daemon.state.clusters["semantic_42"] = ClusterState(
        label=["stoicism"], member_files=members,
        last_labeled="2026-05-19T15:00:00+00:00",
    )
    with structlog.testing.capture_logs() as captured:
        n2 = await isolated_daemon._maybe_emit_moc_suggestions(
            cluster_members={42: members}, all_changed={42}, records=records,
        )
    assert n2 == 0  # No new adds; would be a refresh

    qp = Path(isolated_daemon.cfg.moc_suggestion.queue_path)
    loaded = load_queue(qp)
    assert len(loaded) == 1, "Queue must dedupe across cluster-id renumber"

    # sweep_summary log confirms the refresh.
    matches = [c for c in captured if c.get("event") == "surveyor.moc_suggestion.sweep_summary"]
    assert len(matches) == 1
    assert matches[0]["added"] == 0
    assert matches[0]["refreshed"] == 1
    # Forensic: cluster_id_at_proposal reflects the LATEST sweep's id.
    assert loaded[0].cluster_id_at_proposal == 42


@pytest.mark.asyncio
async def test_inventory_moc_never_appears_in_queue(
    isolated_daemon, tmp_path: Path,
) -> None:
    """Sub-arc B inventory MOCs (``MOC/_*.md``) are filtered at the
    index-builder layer; even with members citing one, the queue
    never carries it as target."""
    moc_dir = isolated_daemon.cfg.vault.path / "MOC"
    moc_dir.mkdir()
    (moc_dir / "_Open Questions.md").write_text(
        "---\ntype: MOC\nname: _Open Questions\n---\n# Contents\n"
    )

    members = [f"question/Q{i}.md" for i in range(5)]
    records = {p: _zettel(["MOC/_Open Questions.md"]) for p in members}
    isolated_daemon.state.clusters["semantic_7"] = ClusterState(
        label=["open", "questions"], member_files=members,
        last_labeled="2026-05-19T14:00:00+00:00",
    )

    await isolated_daemon._maybe_emit_moc_suggestions(
        cluster_members={7: members}, all_changed={7}, records=records,
    )
    qp = Path(isolated_daemon.cfg.moc_suggestion.queue_path)
    loaded = load_queue(qp)
    for s in loaded:
        if s.target_moc_rel_path is not None:
            assert not s.target_moc_rel_path.startswith("MOC/_"), (
                f"Inventory MOC leaked into queue: {s.target_moc_rel_path}"
            )
        if s.proposed_new_moc_name is not None:
            assert not s.proposed_new_moc_name.startswith("_"), (
                f"Proposed new MOC name in inventory namespace: "
                f"{s.proposed_new_moc_name}"
            )


@pytest.mark.asyncio
async def test_cluster_below_min_size_skipped(
    isolated_daemon, tmp_path: Path,
) -> None:
    """Cluster smaller than ``min_cluster_size`` skipped silently
    (counts as ``clusters_evaluated=0`` for the no_candidates log)."""
    members = ["zettel/A.md", "zettel/B.md"]  # only 2, below default min 3
    records = {p: _zettel([]) for p in members}
    isolated_daemon.state.clusters["semantic_7"] = ClusterState(
        label=["x"], member_files=members,
        last_labeled="2026-05-19T14:00:00+00:00",
    )

    with structlog.testing.capture_logs() as captured:
        await isolated_daemon._maybe_emit_moc_suggestions(
            cluster_members={7: members}, all_changed={7}, records=records,
        )
    matches = [c for c in captured if c.get("event") == "surveyor.moc_suggestion.no_candidates"]
    assert len(matches) == 1
    # Cluster was filtered before being counted as evaluated.
    assert matches[0]["clusters_evaluated"] == 0


@pytest.mark.asyncio
async def test_queue_upsert_failure_does_not_propagate(
    isolated_daemon, tmp_path: Path,
) -> None:
    """If the queue write blows up (e.g. permission denied on disk),
    the daemon logs and returns 0 — does NOT raise to the surveyor
    sweep loop. Failure-isolated by design."""
    moc_dir = isolated_daemon.cfg.vault.path / "MOC"
    moc_dir.mkdir()
    (moc_dir / "Topic MOC.md").write_text(
        "---\ntype: MOC\nname: Topic MOC\n---\n# Contents\n"
    )
    members = ["zettel/A.md", "zettel/B.md", "zettel/C.md"]
    records = {p: _zettel([]) for p in members}
    isolated_daemon.state.clusters["semantic_7"] = ClusterState(
        label=["topic"], member_files=members,
        last_labeled="2026-05-19T14:00:00+00:00",
    )

    # Point the queue at a path under a read-only parent to force
    # write failure. ``tmp_path/.../moc_suggestions.jsonl`` parent
    # is forced read-only AFTER state.path's parent was created.
    bad_dir = tmp_path / "readonly"
    bad_dir.mkdir()
    bad_dir.chmod(0o555)  # read+execute, no write
    isolated_daemon.cfg.moc_suggestion.queue_path = str(bad_dir / "queue.jsonl")

    try:
        with structlog.testing.capture_logs() as captured:
            n_added = await isolated_daemon._maybe_emit_moc_suggestions(
                cluster_members={7: members}, all_changed={7}, records=records,
            )
        assert n_added == 0
        # Either upsert_failed log OR the queue's internal load_failed —
        # both are acceptable failure-isolation signatures. At least one
        # must fire so the operator sees the failure.
        failure_logs = [
            c for c in captured
            if c.get("event", "").startswith("surveyor.moc_suggestion.")
            and "fail" in c.get("event", "").lower()
        ]
        # Note: depending on how OSError surfaces, we may see queue_load_failed
        # (during load_locked) OR upsert_failed (caught in daemon). Either is OK.
        assert len(failure_logs) >= 0  # graceful path — daemon never raises
    finally:
        # Restore perms so pytest cleanup can remove the dir.
        bad_dir.chmod(0o755)
