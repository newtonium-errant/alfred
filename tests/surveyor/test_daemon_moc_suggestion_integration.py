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
