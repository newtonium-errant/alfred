"""Phase 1 contamination diagnostic — entity-link attribution logging.

Background (QA finding 2026-05-03): Andrew's vault accumulated
contamination in ``related_persons`` / ``related_orgs`` fields —
e.g., ``person/Ben McMillan.md`` appearing in records that have
nothing factually to do with Ben. The 5-step investigation process
documented in the spec calls for "Fix the writer code + add
structured logging" — this is the structured-logging half.

Static analysis of ``daemon._link_entities_in_clusters`` /
``_link_noise_points_to_entities`` / ``_backfill_new_entities``
showed all three paths use per-record cosine similarity gating
above ``cfg.entity_link.threshold`` (default 0.75). No cluster-
default code path. The contamination is most likely the result of
threshold-permissive semantic-vs-thematic similarity — e.g., a
"music" cluster correctly co-clusters music events + a
music-relevant person record, then per-record cos sim > 0.75
because both embeddings live in the music topic space, even though
the person didn't appear at the specific event.

Without per-write attribution metadata in the log, an operator
cannot answer "which cluster led to this link?" / "what was the
cosine similarity?" / "was this from the cluster pass, the noise
pass, or the backfill pass?" — the investigation has no forensic
data, only the corrupted vault state. Phase 1 ships the
attribution; Phase 2 (separate ship) is the cleanup script that
relies on the attribution + a future tightened threshold to repair.

Coverage:
  * Per-call attribution kwarg on each public ``write_related_*``
  * Attribution carries ``stage`` / ``cluster_id`` / ``source_type`` /
    ``similarities`` / ``target_paths`` into the log event
  * Existing call sites without attribution still work (default None)
  * Similarity scores aligned to dedup-survived paths
  * Daemon's three call sites all pass attribution — pinned via
    source-text inspection (matches the same source-pin pattern as
    ``test_talker_transport_wiring.py``)

Uses ``structlog.testing.capture_logs`` per
``feedback_structlog_assertion_patterns.md``.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest
from structlog.testing import capture_logs

from alfred.surveyor.state import PipelineState
from alfred.surveyor.writer import VaultWriter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def vault_with_record(tmp_path: Path):
    """Tiny vault + writer with one event record to be linked."""
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "event").mkdir()
    ev = vault / "event" / "test-event.md"
    ev.write_text(
        "---\ntype: event\nname: Test Event\nstatus: active\n---\n\nEvent body.\n",
        encoding="utf-8",
    )
    state = PipelineState(state_path=tmp_path / "state.json")
    state.update_file("event/test-event.md", "hash-placeholder")
    writer = VaultWriter(vault_path=vault, state=state)
    return vault, writer, "event/test-event.md"


# ---------------------------------------------------------------------------
# Per-call attribution → log fields
# ---------------------------------------------------------------------------


def test_write_without_attribution_logs_minimal_fields(vault_with_record):
    """Existing callers (no attribution kwarg) still produce a log
    event — just without the diagnostic fields. Backward-compat
    contract: attribution is opt-in."""
    _, writer, rel = vault_with_record
    with capture_logs() as captured:
        added = writer.write_related_persons(
            rel, ["person/Andrew Newton.md"],
        )
    assert added == 1
    write_logs = [
        c for c in captured
        if c.get("event") == "writer.entity_links_written"
    ]
    assert len(write_logs) == 1
    log_entry = write_logs[0]
    # Core fields always present.
    assert log_entry["path"] == rel
    assert log_entry["field"] == "related_persons"
    assert log_entry["added"] == 1
    assert log_entry["total"] == 1
    # Attribution fields absent when none provided.
    assert "stage" not in log_entry
    assert "cluster_id" not in log_entry
    assert "similarities_added" not in log_entry


def test_write_with_attribution_includes_diagnostic_fields(vault_with_record):
    """The Phase 1 diagnostic contract: every attribution field that
    the caller provides lands in the log event. An operator grepping
    ``writer.entity_links_written`` for a contamination signature
    can filter on stage/cluster_id/source_type/similarity range."""
    _, writer, rel = vault_with_record
    with capture_logs() as captured:
        added = writer.write_related_persons(
            rel,
            ["person/Ben McMillan.md", "person/Other Person.md"],
            attribution={
                "stage": "cluster",
                "cluster_id": 42,
                "source_type": "event",
                "similarities": [0.91, 0.78],
                "target_paths": [
                    "person/Ben McMillan.md",
                    "person/Other Person.md",
                ],
            },
        )
    assert added == 2
    write_logs = [
        c for c in captured
        if c.get("event") == "writer.entity_links_written"
    ]
    assert len(write_logs) == 1
    log_entry = write_logs[0]
    assert log_entry["stage"] == "cluster"
    assert log_entry["cluster_id"] == 42
    assert log_entry["source_type"] == "event"
    assert log_entry["similarities_added"] == [0.91, 0.78]
    assert log_entry["target_paths_added"] == [
        "person/Ben McMillan.md", "person/Other Person.md",
    ]


def test_attribution_similarities_align_with_survived_paths(vault_with_record):
    """When dedup drops some incoming paths, the logged
    similarities_added must align to the paths that ACTUALLY
    landed — not the full input list. Without alignment an
    operator can't tell which similarity score caused which write.
    """
    _, writer, rel = vault_with_record
    # Pre-seed with one path so the second write has a dedup hit.
    writer.write_related_persons(rel, ["person/Already Here.md"])
    with capture_logs() as captured:
        added = writer.write_related_persons(
            rel,
            # Mix: index 0 already exists (dedup), 1 + 2 new.
            ["person/Already Here.md", "person/New A.md", "person/New B.md"],
            attribution={
                "stage": "cluster",
                "cluster_id": 7,
                "source_type": "event",
                "similarities": [0.99, 0.85, 0.77],
                "target_paths": [
                    "person/Already Here.md",
                    "person/New A.md",
                    "person/New B.md",
                ],
            },
        )
    assert added == 2
    write_logs = [
        c for c in captured
        if c.get("event") == "writer.entity_links_written"
    ]
    log_entry = write_logs[-1]
    # similarities_added should be only the survivors' sims.
    assert log_entry["similarities_added"] == [0.85, 0.77]
    assert log_entry["target_paths_added"] == [
        "person/New A.md", "person/New B.md",
    ]


def test_attribution_partial_kwargs_only_logs_what_caller_provided(
    vault_with_record,
):
    """Caller passing only ``stage`` (not ``cluster_id`` etc.) gets
    only ``stage`` in the log. No silent defaults for missing
    fields — keeps the forensic trail honest about what the caller
    actually knew."""
    _, writer, rel = vault_with_record
    with capture_logs() as captured:
        writer.write_related_persons(
            rel, ["person/X.md"],
            attribution={"stage": "noise"},
        )
    log_entry = next(
        c for c in captured
        if c.get("event") == "writer.entity_links_written"
    )
    assert log_entry["stage"] == "noise"
    assert "cluster_id" not in log_entry
    assert "similarities_added" not in log_entry
    assert "source_type" not in log_entry


def test_attribution_works_for_all_four_field_writers(vault_with_record):
    """Every public ``write_related_*`` accepts the attribution kwarg
    and threads it to the log. No partial coverage — Phase 2 cleanup
    relies on the diagnostic being present at every write site."""
    _, writer, rel = vault_with_record
    captured_events: list[dict] = []
    with capture_logs() as captured:
        writer.write_related_matters(
            rel, ["matter/m.md"],
            attribution={"stage": "cluster", "source_type": "event"},
        )
        writer.write_related_persons(
            rel, ["person/p.md"],
            attribution={"stage": "noise", "source_type": "event"},
        )
        writer.write_related_orgs(
            rel, ["org/o.md"],
            attribution={"stage": "backfill", "source_type": "event"},
        )
        writer.write_related_projects(
            rel, ["project/x.md"],
            attribution={"stage": "cluster", "source_type": "event"},
        )
        captured_events = list(captured)
    write_logs = [
        c for c in captured_events
        if c.get("event") == "writer.entity_links_written"
    ]
    assert len(write_logs) == 4
    # Each log entry carries the stage the caller passed.
    fields_to_stages = {
        log["field"]: log["stage"] for log in write_logs
    }
    assert fields_to_stages == {
        "related_matters": "cluster",
        "related_persons": "noise",
        "related_orgs": "backfill",
        "related_projects": "cluster",
    }


def test_attribution_does_not_change_write_behavior(vault_with_record):
    """The diagnostic is purely additive — frontmatter shape is
    identical with or without attribution. Pinned because a
    bug-discovery kwarg that quietly changes the write payload
    would corrupt vault state in production."""
    _, writer, rel = vault_with_record
    writer.write_related_persons(
        rel, ["person/A.md", "person/B.md"],
        attribution={
            "stage": "cluster",
            "cluster_id": 1,
            "source_type": "event",
            "similarities": [0.9, 0.8],
            "target_paths": ["person/A.md", "person/B.md"],
        },
    )
    md = frontmatter.load(writer.vault_path / rel).metadata
    assert md["related_persons"] == ["person/A.md", "person/B.md"]


def test_similarities_rounded_for_log_legibility(vault_with_record):
    """Floats are rounded to 4 decimal places in the log output
    so a grep doesn't return ``0.7500000000000001`` noise from
    floating-point arithmetic. 4 decimals is enough precision to
    distinguish a 0.7501 from a 0.7499 (which straddle the default
    threshold) while staying readable."""
    _, writer, rel = vault_with_record
    with capture_logs() as captured:
        writer.write_related_persons(
            rel, ["person/A.md"],
            attribution={
                "stage": "cluster",
                "cluster_id": 1,
                "source_type": "event",
                "similarities": [0.7500000000000001],
                "target_paths": ["person/A.md"],
            },
        )
    log_entry = next(
        c for c in captured
        if c.get("event") == "writer.entity_links_written"
    )
    # 0.75 (rounded), not the raw float-arithmetic noise.
    assert log_entry["similarities_added"] == [0.75]


# ---------------------------------------------------------------------------
# Daemon source-pin: every entity-link call site passes attribution
# ---------------------------------------------------------------------------
#
# Pure source-text inspection — same pattern as
# ``test_talker_transport_wiring.py``. If a future refactor drops
# the attribution kwarg from one of the three daemon stages, this
# test fires loudly so the gap doesn't ship to production silently.


def _daemon_source() -> str:
    here = Path(__file__).resolve().parent
    daemon_path = here.parent.parent / "src" / "alfred" / "surveyor" / "daemon.py"
    return daemon_path.read_text(encoding="utf-8")


def test_cluster_stage_passes_attribution() -> None:
    """``_link_entities_in_clusters`` must pass ``stage="cluster"``
    + ``cluster_id`` to every writer call.

    Without this, the cluster-stage links land in the log as bare
    "writer.entity_links_written" events with no provenance — Phase 2
    cleanup can't distinguish cluster links from noise / backfill
    links.
    """
    source = _daemon_source()
    # Find the cluster stage method body.
    cluster_idx = source.find("def _link_entities_in_clusters(")
    next_def = source.find("\n    def ", cluster_idx + 1)
    cluster_body = source[cluster_idx:next_def]
    assert '"stage": "cluster"' in cluster_body, (
        "_link_entities_in_clusters must pass stage='cluster' in "
        "the writer attribution kwarg"
    )
    assert '"cluster_id": cid' in cluster_body, (
        "_link_entities_in_clusters must pass the actual cluster id "
        "(cid) so the log can be filtered per-cluster"
    )


def test_noise_stage_passes_attribution() -> None:
    """``_link_noise_points_to_entities`` must pass ``stage="noise"``."""
    source = _daemon_source()
    noise_idx = source.find("def _link_noise_points_to_entities(")
    next_def = source.find("\n    def ", noise_idx + 1)
    noise_body = source[noise_idx:next_def]
    assert '"stage": "noise"' in noise_body, (
        "_link_noise_points_to_entities must pass stage='noise' in "
        "the writer attribution kwarg"
    )
    assert '"cluster_id": "noise"' in noise_body, (
        "_link_noise_points_to_entities must mark cluster_id as "
        "'noise' so log filtering can distinguish noise links"
    )


def test_backfill_stage_passes_attribution() -> None:
    """``_backfill_new_entities`` must pass ``stage="backfill"``.

    Backfill has the widest blast radius (one new entity → every
    record in the vault) so attribution is most load-bearing here.
    """
    source = _daemon_source()
    backfill_idx = source.find("def _backfill_new_entities(")
    next_def = source.find("\n    def ", backfill_idx + 1)
    if next_def < 0:
        # Last method in the class.
        backfill_body = source[backfill_idx:]
    else:
        backfill_body = source[backfill_idx:next_def]
    assert '"stage": "backfill"' in backfill_body, (
        "_backfill_new_entities must pass stage='backfill' in "
        "the writer attribution kwarg"
    )
    assert '"cluster_id": "backfill"' in backfill_body, (
        "_backfill_new_entities must mark cluster_id as 'backfill' "
        "so log filtering can distinguish reverse-backfill links"
    )
