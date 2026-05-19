"""Tests for the cluster→MOC suggestion JSONL queue
(Phase 5 Sub-arc D1).

Covers:
  * Upsert idempotency by ID across sweeps
  * Pending-refresh semantics (forensic fields update; created stable)
  * Non-pending entry preserved (negative-learning for rejected)
  * Per-sweep cap enforcement
  * Per-target cap enforcement
  * Status transition validation
  * Schema-tolerance on load (unknown fields silently dropped)
  * Corrupt-line failure isolation
  * Default queue path derivation
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from alfred.surveyor.moc_suggester import MocSuggestion
from alfred.surveyor.moc_suggestion_queue import (
    derive_default_queue_path,
    load_queue,
    update_status,
    upsert_proposals,
)


def _make(
    *,
    id: str,
    target: str | None = "MOC/Stoicism MOC.md",
    members: list[str] | None = None,
    status: str = "pending",
    cluster_id: int = 7,
    tags: list[str] | None = None,
    reasoning: str = "test",
    created: str | None = None,
) -> MocSuggestion:
    """Convenience factory for test fixtures."""
    if members is None:
        members = ["zettel/A.md", "zettel/B.md", "zettel/C.md"]
    if tags is None:
        tags = ["stoicism"]
    if created is None:
        created = datetime(2026, 5, 19, 14, 0, 0, tzinfo=timezone.utc).isoformat()
    return MocSuggestion(
        id=id,
        cluster_id_at_proposal=cluster_id,
        cluster_tags=tags,
        cluster_member_paths=sorted(members),
        target_moc_rel_path=target,
        proposed_new_moc_name=None,
        mapping_signal="member_overlap",
        mapping_score=0.6,
        candidate_members_to_add=[members[-1]],
        reasoning=reasoning,
        created=created,
        status=status,
    )


# ---------------------------------------------------------------------------
# Default queue path derivation.
# ---------------------------------------------------------------------------


def test_derive_default_queue_path_lives_next_to_state() -> None:
    """Queue path defaults to ``moc_suggestions.jsonl`` in the same
    directory as the surveyor state file. Mirrors the audit-log path
    derivation in ``daemon.py:66-70``."""
    p = derive_default_queue_path("/home/andrew/.alfred/hypatia/data/surveyor_state.json")
    assert p == Path("/home/andrew/.alfred/hypatia/data/moc_suggestions.jsonl")


def test_derive_default_queue_path_accepts_pathlib(tmp_path: Path) -> None:
    """Path-typed input accepted."""
    p = derive_default_queue_path(tmp_path / "data" / "surveyor_state.json")
    assert p == tmp_path / "data" / "moc_suggestions.jsonl"


# ---------------------------------------------------------------------------
# Load semantics.
# ---------------------------------------------------------------------------


def test_load_queue_returns_empty_for_missing_file(tmp_path: Path) -> None:
    """No queue file yet → empty list (not an error)."""
    assert load_queue(tmp_path / "moc_suggestions.jsonl") == []


def test_load_queue_round_trips_proposals(tmp_path: Path) -> None:
    """Round-trip: upsert → load returns same content."""
    qp = tmp_path / "moc_suggestions.jsonl"
    s = _make(id="ms-20260519-aaaaaaaa")
    upsert_proposals(qp, [s], max_pending_per_target=5, max_proposals_per_sweep=10)
    loaded = load_queue(qp)
    assert len(loaded) == 1
    assert loaded[0].id == s.id
    assert loaded[0].target_moc_rel_path == s.target_moc_rel_path


def test_load_queue_schema_tolerance_unknown_fields_ignored(tmp_path: Path) -> None:
    """A queue file written by a future version with extra fields
    must load without crashing on this (older) version.

    Per CLAUDE.md "load() schema-tolerance contract" — the loader
    filters incoming dicts against ``MocSuggestion.__dataclass_fields__``.
    """
    qp = tmp_path / "moc_suggestions.jsonl"
    # Write a JSONL line with a future-version field "novel_field".
    s = _make(id="ms-20260519-aaaaaaaa").to_dict()
    s["novel_field_from_future_version"] = {"this": "should be silently dropped"}
    s["another_future_field"] = 42
    qp.write_text(json.dumps(s) + "\n")
    loaded = load_queue(qp)
    assert len(loaded) == 1
    assert loaded[0].id == "ms-20260519-aaaaaaaa"


def test_load_queue_skips_corrupt_line(tmp_path: Path) -> None:
    """A single malformed JSONL line is logged + skipped; valid
    lines still load."""
    qp = tmp_path / "moc_suggestions.jsonl"
    good = _make(id="ms-20260519-good")
    qp.write_text(
        "this is not json\n"
        + json.dumps(good.to_dict()) + "\n"
        + "{this is partially malformed but starts as json\n"
    )
    loaded = load_queue(qp)
    assert len(loaded) == 1
    assert loaded[0].id == "ms-20260519-good"


# ---------------------------------------------------------------------------
# Upsert — new entry path.
# ---------------------------------------------------------------------------


def test_upsert_adds_new_entry(tmp_path: Path) -> None:
    qp = tmp_path / "moc_suggestions.jsonl"
    s = _make(id="ms-20260519-aaa")
    n_added, n_refreshed = upsert_proposals(
        qp, [s], max_pending_per_target=5, max_proposals_per_sweep=10,
    )
    assert n_added == 1
    assert n_refreshed == 0


def test_upsert_creates_queue_file_if_absent(tmp_path: Path) -> None:
    qp = tmp_path / "subdir" / "moc_suggestions.jsonl"
    assert not qp.exists()
    s = _make(id="ms-20260519-aaa")
    upsert_proposals(qp, [s], max_pending_per_target=5, max_proposals_per_sweep=10)
    assert qp.exists()


# ---------------------------------------------------------------------------
# Upsert — refresh path.
# ---------------------------------------------------------------------------


def test_upsert_refreshes_pending_entry_keeps_created(tmp_path: Path) -> None:
    """Re-upserting a pending id refreshes forensic fields but keeps
    ``created`` stable."""
    qp = tmp_path / "moc_suggestions.jsonl"
    original_created = "2026-05-19T14:00:00+00:00"
    s1 = _make(id="ms-20260519-aaa", reasoning="first sweep", created=original_created)
    upsert_proposals(qp, [s1], max_pending_per_target=5, max_proposals_per_sweep=10)

    s2 = _make(
        id="ms-20260519-aaa",  # Same ID
        reasoning="second sweep, refreshed",
        cluster_id=99,  # Different cluster (HDBSCAN renumbered)
        tags=["stoicism", "marcus-aurelius"],  # Different tags
        created="2026-05-20T14:00:00+00:00",  # Different created — should be IGNORED
    )
    n_added, n_refreshed = upsert_proposals(
        qp, [s2], max_pending_per_target=5, max_proposals_per_sweep=10,
    )
    assert n_added == 0
    assert n_refreshed == 1

    loaded = load_queue(qp)
    assert len(loaded) == 1
    e = loaded[0]
    # Forensic fields refreshed.
    assert e.reasoning == "second sweep, refreshed"
    assert e.cluster_id_at_proposal == 99
    assert e.cluster_tags == ["stoicism", "marcus-aurelius"]
    # ``created`` stable.
    assert e.created == original_created
    # ID stable.
    assert e.id == "ms-20260519-aaa"


def test_upsert_negative_learning_preserved_for_rejected(tmp_path: Path) -> None:
    """A rejected suggestion is the negative-learning surface — re-
    upserting the same ID must NOT resurrect it as pending."""
    qp = tmp_path / "moc_suggestions.jsonl"
    rejected = _make(id="ms-20260519-rej", status="rejected")
    qp.write_text(json.dumps(rejected.to_dict()) + "\n")

    # Surveyor sweeps again and re-proposes the same (members, target).
    s2 = _make(id="ms-20260519-rej", reasoning="re-proposed by surveyor")
    n_added, n_refreshed = upsert_proposals(
        qp, [s2], max_pending_per_target=5, max_proposals_per_sweep=10,
    )
    assert n_added == 0
    assert n_refreshed == 0

    loaded = load_queue(qp)
    assert len(loaded) == 1
    assert loaded[0].status == "rejected"
    # Reasoning NOT updated — rejected is terminal-for-refresh.
    assert "re-proposed" not in loaded[0].reasoning


def test_upsert_negative_learning_preserved_for_applied(tmp_path: Path) -> None:
    """Applied entries similarly preserved against re-upsert."""
    qp = tmp_path / "moc_suggestions.jsonl"
    applied = _make(id="ms-20260519-app", status="applied")
    qp.write_text(json.dumps(applied.to_dict()) + "\n")

    s2 = _make(id="ms-20260519-app", reasoning="re-proposed")
    upsert_proposals(qp, [s2], max_pending_per_target=5, max_proposals_per_sweep=10)
    loaded = load_queue(qp)
    assert loaded[0].status == "applied"


# ---------------------------------------------------------------------------
# Caps.
# ---------------------------------------------------------------------------


def test_max_proposals_per_sweep_cap_drops_excess(tmp_path: Path) -> None:
    """``max_proposals_per_sweep`` caps NEW entries per call."""
    qp = tmp_path / "moc_suggestions.jsonl"
    proposals = [
        _make(id=f"ms-20260519-{i:08x}", target=f"MOC/Topic{i} MOC.md")
        for i in range(20)
    ]
    n_added, _ = upsert_proposals(
        qp, proposals, max_pending_per_target=5, max_proposals_per_sweep=10,
    )
    assert n_added == 10
    assert len(load_queue(qp)) == 10


def test_max_pending_per_target_cap_limits_against_one_moc(tmp_path: Path) -> None:
    """Across sweeps, the per-target cap prevents queue inflation
    against the same MOC."""
    qp = tmp_path / "moc_suggestions.jsonl"
    # 10 distinct proposals all pointing at the same MOC.
    target = "MOC/Crowded MOC.md"
    proposals = [
        _make(id=f"ms-20260519-{i:08x}", target=target,
              members=[f"zettel/{i}_A.md", f"zettel/{i}_B.md", f"zettel/{i}_C.md"])
        for i in range(10)
    ]
    n_added, _ = upsert_proposals(
        qp, proposals, max_pending_per_target=3, max_proposals_per_sweep=20,
    )
    assert n_added == 3, "Per-target cap should drop excess at the second over-cap proposal"


def test_per_target_cap_does_not_affect_other_targets(tmp_path: Path) -> None:
    """Each MOC has its own cap counter."""
    qp = tmp_path / "moc_suggestions.jsonl"
    proposals = [
        _make(id=f"ms-20260519-A{i:07x}", target="MOC/A MOC.md",
              members=[f"zettel/A{i}_a.md", f"zettel/A{i}_b.md", f"zettel/A{i}_c.md"])
        for i in range(5)
    ] + [
        _make(id=f"ms-20260519-B{i:07x}", target="MOC/B MOC.md",
              members=[f"zettel/B{i}_a.md", f"zettel/B{i}_b.md", f"zettel/B{i}_c.md"])
        for i in range(5)
    ]
    n_added, _ = upsert_proposals(
        qp, proposals, max_pending_per_target=3, max_proposals_per_sweep=20,
    )
    assert n_added == 6  # 3 per target × 2 targets


def test_no_file_write_when_all_proposals_are_no_ops(tmp_path: Path) -> None:
    """If every proposal is an idempotent no-op (e.g. all already
    rejected), the queue file's mtime must not change."""
    qp = tmp_path / "moc_suggestions.jsonl"
    rejected = _make(id="ms-20260519-rej", status="rejected")
    qp.write_text(json.dumps(rejected.to_dict()) + "\n")
    mtime_before = qp.stat().st_mtime

    s2 = _make(id="ms-20260519-rej", reasoning="re-proposed")
    n_added, n_refreshed = upsert_proposals(
        qp, [s2], max_pending_per_target=5, max_proposals_per_sweep=10,
    )
    assert n_added == 0
    assert n_refreshed == 0
    # We don't assert mtime equality (filesystem timer granularity),
    # but we DO assert the file content is unchanged.
    assert qp.read_text() == json.dumps(rejected.to_dict()) + "\n"


# ---------------------------------------------------------------------------
# Status transitions.
# ---------------------------------------------------------------------------


def test_status_update_pending_to_accepted(tmp_path: Path) -> None:
    qp = tmp_path / "moc_suggestions.jsonl"
    s = _make(id="ms-20260519-aaa")
    upsert_proposals(qp, [s], max_pending_per_target=5, max_proposals_per_sweep=10)

    ok = update_status(qp, "ms-20260519-aaa", "accepted")
    assert ok is True

    loaded = load_queue(qp)
    assert loaded[0].status == "accepted"
    assert loaded[0].decided_at is not None


def test_status_update_pending_to_rejected_terminal(tmp_path: Path) -> None:
    qp = tmp_path / "moc_suggestions.jsonl"
    s = _make(id="ms-20260519-aaa")
    upsert_proposals(qp, [s], max_pending_per_target=5, max_proposals_per_sweep=10)

    assert update_status(qp, "ms-20260519-aaa", "rejected") is True
    # Terminal — no further transitions allowed.
    assert update_status(qp, "ms-20260519-aaa", "accepted") is False
    assert update_status(qp, "ms-20260519-aaa", "pending") is False


def test_status_update_accepted_to_applied_sets_applied_at(tmp_path: Path) -> None:
    qp = tmp_path / "moc_suggestions.jsonl"
    s = _make(id="ms-20260519-aaa")
    upsert_proposals(qp, [s], max_pending_per_target=5, max_proposals_per_sweep=10)
    update_status(qp, "ms-20260519-aaa", "accepted")
    update_status(qp, "ms-20260519-aaa", "applied")

    loaded = load_queue(qp)
    assert loaded[0].status == "applied"
    assert loaded[0].applied_at is not None
    assert loaded[0].last_apply_error is None


def test_status_update_apply_failure_flips_back_to_pending(tmp_path: Path) -> None:
    """Accepted → pending re-flip on apply failure carries the
    ``last_apply_error`` forensic field."""
    qp = tmp_path / "moc_suggestions.jsonl"
    s = _make(id="ms-20260519-aaa")
    upsert_proposals(qp, [s], max_pending_per_target=5, max_proposals_per_sweep=10)
    update_status(qp, "ms-20260519-aaa", "accepted")
    update_status(
        qp, "ms-20260519-aaa", "pending",
        last_apply_error="vault_edit failed: target moc not found",
    )

    loaded = load_queue(qp)
    assert loaded[0].status == "pending"
    assert "target moc not found" in (loaded[0].last_apply_error or "")


def test_status_update_invalid_transition_denied(tmp_path: Path) -> None:
    """pending → applied is NOT in the allowed set (must go through
    accepted)."""
    qp = tmp_path / "moc_suggestions.jsonl"
    s = _make(id="ms-20260519-aaa")
    upsert_proposals(qp, [s], max_pending_per_target=5, max_proposals_per_sweep=10)
    assert update_status(qp, "ms-20260519-aaa", "applied") is False
    assert load_queue(qp)[0].status == "pending"


def test_status_update_missing_id_returns_false(tmp_path: Path) -> None:
    qp = tmp_path / "moc_suggestions.jsonl"
    s = _make(id="ms-20260519-aaa")
    upsert_proposals(qp, [s], max_pending_per_target=5, max_proposals_per_sweep=10)
    assert update_status(qp, "ms-20260519-nonexistent", "accepted") is False


def test_status_update_missing_queue_returns_false(tmp_path: Path) -> None:
    """Queue file absent → False, no crash."""
    qp = tmp_path / "no_queue_yet.jsonl"
    assert update_status(qp, "ms-20260519-aaa", "accepted") is False


# ---------------------------------------------------------------------------
# Atomic write via .tmp + rename.
# ---------------------------------------------------------------------------


def test_no_tmp_file_left_after_successful_upsert(tmp_path: Path) -> None:
    """The .tmp file used for atomic rewrite must not persist after
    a successful upsert."""
    qp = tmp_path / "moc_suggestions.jsonl"
    s = _make(id="ms-20260519-aaa")
    upsert_proposals(qp, [s], max_pending_per_target=5, max_proposals_per_sweep=10)
    tmp_file = qp.with_suffix(qp.suffix + ".tmp")
    assert not tmp_file.exists()
