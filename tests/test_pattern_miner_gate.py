"""Gate-logic tests for the Phase 4 pattern miner.

Pins the four-part gate from project_kalle_phase4_pattern_miner.md (Q3):
  1. Labeled — at least one label string
  2. Substantive — len(member_files) >= min_cluster_size
  3. No canonical match — labels not already in canonical_index
  4. Label quality — at least one label not on the denylist

Plus the slug derivation + canonical-type heuristic from the same module.

These tests run unconditionally (no pytest.importorskip) per
feedback_regression_pin_unconditional.md — pattern_miner has no
optional ML dep, so there's nothing to skip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.distiller.pattern_miner import (
    ClusterRecord,
    cluster_matches_canonical,
    cluster_passes_label_quality,
    derive_proposed_canonical_type,
    derive_proposed_slug,
    evaluate_cluster,
    gate_cluster,
    label_segments,
    load_canonical_index,
    slugify,
)
from alfred.distiller.pattern_miner_state import (
    PatternMinerState,
    ProposalEntry,
    STATUS_DISCARDED,
    STATUS_PROMOTED,
)


# ---------------------------------------------------------------------------
# slugify + label_segments primitives
# ---------------------------------------------------------------------------


class TestSlugify:
    def test_lowercase_with_kebab(self) -> None:
        assert slugify("Local LLM Hardware") == "local-llm-hardware"

    def test_strips_special_chars(self) -> None:
        assert slugify("backend/n8n") == "backend-n8n"

    def test_collapses_runs_of_non_alnum(self) -> None:
        assert slugify("foo___bar  baz") == "foo-bar-baz"

    def test_strips_leading_trailing_hyphens(self) -> None:
        assert slugify("--foo--") == "foo"

    def test_empty_input_returns_empty(self) -> None:
        assert slugify("") == ""
        assert slugify("   ") == ""

    def test_handles_unicode_via_dropping(self) -> None:
        # Non-ASCII collapses to hyphens (we don't asciify); the result
        # should be a valid kebab token without trailing junk.
        assert slugify("café") == "caf"

    def test_already_slug_passthrough(self) -> None:
        assert slugify("already-slug") == "already-slug"


class TestLabelSegments:
    def test_full_path_first_then_segments(self) -> None:
        # backend/n8n yields the full slug AND each segment, full first.
        segments = label_segments("backend/n8n")
        assert segments[0] == "backend-n8n"
        assert "backend" in segments
        assert "n8n" in segments

    def test_single_segment_returns_full_only(self) -> None:
        # Full == segment when no slash; should not duplicate.
        segments = label_segments("python")
        assert segments == ["python"]

    def test_empty_input_returns_empty(self) -> None:
        assert label_segments("") == []
        assert label_segments(None) == []  # type: ignore[arg-type]

    def test_three_segment_label(self) -> None:
        segments = label_segments("backend/n8n/workflows")
        assert segments[0] == "backend-n8n-workflows"
        assert "backend" in segments
        assert "n8n" in segments
        assert "workflows" in segments


# ---------------------------------------------------------------------------
# Gate part 1: labeled
# ---------------------------------------------------------------------------


class TestGateLabeled:
    def test_no_labels_fails_gate(self) -> None:
        cluster = ClusterRecord(
            cluster_id="c1", labels=[],
            member_files=["a.md", "b.md", "c.md"],
        )
        assert not gate_cluster(cluster, set(), set(), min_cluster_size=3)

    def test_labels_present_passes_gate_part1(self) -> None:
        cluster = ClusterRecord(
            cluster_id="c1", labels=["topic/x"],
            member_files=["a.md", "b.md", "c.md"],
        )
        assert gate_cluster(cluster, set(), set(), min_cluster_size=3)


# ---------------------------------------------------------------------------
# Gate part 2: substantive (cluster size threshold)
# ---------------------------------------------------------------------------


class TestGateSubstantive:
    def test_below_threshold_fails(self) -> None:
        cluster = ClusterRecord(
            cluster_id="c1", labels=["topic/x"],
            member_files=["a.md", "b.md"],  # only 2
        )
        assert not gate_cluster(cluster, set(), set(), min_cluster_size=3)

    def test_at_threshold_passes(self) -> None:
        cluster = ClusterRecord(
            cluster_id="c1", labels=["topic/x"],
            member_files=["a.md", "b.md", "c.md"],  # exactly 3
        )
        assert gate_cluster(cluster, set(), set(), min_cluster_size=3)

    def test_above_threshold_passes(self) -> None:
        cluster = ClusterRecord(
            cluster_id="c1", labels=["topic/x"],
            member_files=["a.md", "b.md", "c.md", "d.md"],
        )
        assert gate_cluster(cluster, set(), set(), min_cluster_size=3)

    def test_threshold_override_applies(self) -> None:
        cluster = ClusterRecord(
            cluster_id="c1", labels=["topic/x"],
            member_files=["a.md", "b.md"],  # 2 members
        )
        assert gate_cluster(cluster, set(), set(), min_cluster_size=2)


# ---------------------------------------------------------------------------
# Gate part 3: no canonical match
# ---------------------------------------------------------------------------


class TestGateCanonicalMatch:
    def test_full_path_match_drops_cluster(self) -> None:
        cluster = ClusterRecord(
            cluster_id="c1", labels=["backend/n8n"],
            member_files=["a.md", "b.md", "c.md"],
        )
        canonical_index = {"backend-n8n"}
        assert not gate_cluster(
            cluster, canonical_index, set(), min_cluster_size=3,
        )

    def test_segment_match_drops_cluster(self) -> None:
        cluster = ClusterRecord(
            cluster_id="c1", labels=["something/n8n"],
            member_files=["a.md", "b.md", "c.md"],
        )
        # segment 'n8n' alone is enough for the conservative match.
        canonical_index = {"n8n"}
        assert not gate_cluster(
            cluster, canonical_index, set(), min_cluster_size=3,
        )

    def test_no_match_passes(self) -> None:
        cluster = ClusterRecord(
            cluster_id="c1", labels=["something/totally-new"],
            member_files=["a.md", "b.md", "c.md"],
        )
        canonical_index = {"backend-n8n", "n8n", "data-flow"}
        assert gate_cluster(
            cluster, canonical_index, set(), min_cluster_size=3,
        )

    def test_helper_returns_true_on_full_path_hit(self) -> None:
        assert cluster_matches_canonical(
            ["backend/n8n"], {"backend-n8n"},
        )

    def test_helper_returns_true_on_segment_hit(self) -> None:
        assert cluster_matches_canonical(
            ["topic/python"], {"python"},
        )

    def test_helper_returns_false_on_no_overlap(self) -> None:
        assert not cluster_matches_canonical(
            ["topic/foo"], {"bar", "baz"},
        )


# ---------------------------------------------------------------------------
# Gate part 4: label quality (denylist)
# ---------------------------------------------------------------------------


class TestGateLabelQuality:
    def test_all_labels_on_denylist_drops_cluster(self) -> None:
        cluster = ClusterRecord(
            cluster_id="c1",
            labels=["session-notes", "documentation"],
            member_files=["a.md", "b.md", "c.md"],
        )
        denylist = frozenset({"session-notes", "documentation"})
        assert not gate_cluster(
            cluster, set(), denylist, min_cluster_size=3,
        )

    def test_mixed_signal_passes(self) -> None:
        # One denylist label + one real-theme label = the real-theme
        # half is potentially worth promotion, so the cluster passes.
        cluster = ClusterRecord(
            cluster_id="c1",
            labels=["session-notes", "real/theme"],
            member_files=["a.md", "b.md", "c.md"],
        )
        denylist = frozenset({"session-notes"})
        assert gate_cluster(
            cluster, set(), denylist, min_cluster_size=3,
        )

    def test_helper_empty_labels_returns_false(self) -> None:
        assert not cluster_passes_label_quality([], frozenset())

    def test_helper_returns_false_when_all_denied(self) -> None:
        assert not cluster_passes_label_quality(
            ["session-notes"], frozenset({"session-notes"}),
        )


# ---------------------------------------------------------------------------
# Slug + canonical-type derivation
# ---------------------------------------------------------------------------


class TestSlugDerivation:
    def test_first_label_used_as_slug_source(self) -> None:
        slug = derive_proposed_slug(["llm/quantization", "performance/benchmarking"])
        assert slug == "llm-quantization"

    def test_empty_labels_return_empty(self) -> None:
        assert derive_proposed_slug([]) == ""

    def test_skips_empty_label_to_next(self) -> None:
        slug = derive_proposed_slug(["", "topic/x"])
        assert slug == "topic-x"


class TestCanonicalTypeDerivation:
    def test_default_is_architecture(self) -> None:
        assert derive_proposed_canonical_type(["backend/n8n"]) == "architecture"

    def test_discipline_token_yields_principles(self) -> None:
        assert derive_proposed_canonical_type(
            ["testing/discipline"],
        ) == "principles"

    def test_review_token_yields_principles(self) -> None:
        # "ux-review" is in the principles_tokens set.
        assert derive_proposed_canonical_type(["quality/ux-review"]) == "principles"

    def test_anti_pattern_token_yields_principles(self) -> None:
        assert derive_proposed_canonical_type(
            ["coding/anti-pattern"],
        ) == "principles"


# ---------------------------------------------------------------------------
# load_canonical_index
# ---------------------------------------------------------------------------


class TestCanonicalIndexLoader:
    def test_walks_configured_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "architecture").mkdir()
        (tmp_path / "principles").mkdir()
        (tmp_path / "stack").mkdir()
        (tmp_path / "architecture" / "data-flow.md").write_text("body")
        (tmp_path / "principles" / "qa-process.md").write_text("body")
        (tmp_path / "stack" / "n8n.md").write_text("body")
        idx = load_canonical_index(
            tmp_path, ["architecture", "principles", "stack"],
        )
        assert "data-flow" in idx
        assert "qa-process" in idx
        assert "n8n" in idx

    def test_recursive_walk_into_subdirs(self, tmp_path: Path) -> None:
        # Files under stack/n8n/patterns.md should be picked up too.
        (tmp_path / "stack" / "n8n").mkdir(parents=True)
        (tmp_path / "stack" / "n8n" / "patterns.md").write_text("body")
        idx = load_canonical_index(tmp_path, ["stack"])
        assert "patterns" in idx

    def test_missing_dir_silently_skipped(self, tmp_path: Path) -> None:
        # Vault has only architecture/, principles/ + stack/ missing —
        # the loader returns just the existing dir's slugs.
        (tmp_path / "architecture").mkdir()
        (tmp_path / "architecture" / "x.md").write_text("body")
        idx = load_canonical_index(
            tmp_path, ["architecture", "principles", "stack"],
        )
        assert "x" in idx
        # No crash from the missing dirs.

    def test_gitkeep_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "architecture").mkdir()
        (tmp_path / "architecture" / ".gitkeep").write_text("")
        (tmp_path / "architecture" / "real.md").write_text("body")
        idx = load_canonical_index(tmp_path, ["architecture"])
        assert "real" in idx
        assert "gitkeep" not in idx
        assert ".gitkeep" not in idx

    def test_slugifies_file_stems(self, tmp_path: Path) -> None:
        (tmp_path / "architecture").mkdir()
        (tmp_path / "architecture" / "Local LLM Hardware.md").write_text("body")
        idx = load_canonical_index(tmp_path, ["architecture"])
        assert "local-llm-hardware" in idx


# ---------------------------------------------------------------------------
# evaluate_cluster — gate + dedup combined
# ---------------------------------------------------------------------------


class TestEvaluateCluster:
    def test_passes_returns_candidate(self, tmp_path: Path) -> None:
        cluster = ClusterRecord(
            cluster_id="c1", labels=["new/theme"],
            member_files=["a.md", "b.md", "c.md"],
        )
        state = PatternMinerState(tmp_path / "s.json")
        candidate = evaluate_cluster(
            cluster, set(), frozenset(), state, min_cluster_size=3,
        )
        assert candidate is not None
        assert candidate.proposed_slug == "new-theme"
        assert candidate.proposed_canonical_type == "architecture"
        assert candidate.fingerprint  # non-empty

    def test_dedup_blocks_existing_fingerprint(self, tmp_path: Path) -> None:
        cluster = ClusterRecord(
            cluster_id="c1", labels=["new/theme"],
            member_files=["a.md", "b.md", "c.md"],
        )
        state = PatternMinerState(tmp_path / "s.json")
        # First eval — record entry against the resulting fingerprint.
        first = evaluate_cluster(
            cluster, set(), frozenset(), state, min_cluster_size=3,
        )
        assert first is not None
        state.record_proposal(ProposalEntry(
            fingerprint=first.fingerprint,
            cluster_id=cluster.cluster_id,
        ))
        # Second eval against same cluster — None now (deduped).
        second = evaluate_cluster(
            cluster, set(), frozenset(), state, min_cluster_size=3,
        )
        assert second is None

    def test_promoted_status_still_blocks_redo(self, tmp_path: Path) -> None:
        # Operator-decided fingerprints (any status) stay decided — re-
        # proposing a discarded theme would be noise. This is the design
        # memo Q7 contract.
        cluster = ClusterRecord(
            cluster_id="c1", labels=["new/theme"],
            member_files=["a.md", "b.md", "c.md"],
        )
        state = PatternMinerState(tmp_path / "s.json")
        first = evaluate_cluster(
            cluster, set(), frozenset(), state, min_cluster_size=3,
        )
        assert first is not None
        state.record_proposal(ProposalEntry(
            fingerprint=first.fingerprint,
            cluster_id=cluster.cluster_id,
            status=STATUS_PROMOTED,
        ))
        second = evaluate_cluster(
            cluster, set(), frozenset(), state, min_cluster_size=3,
        )
        assert second is None

    def test_discarded_status_still_blocks_redo(self, tmp_path: Path) -> None:
        cluster = ClusterRecord(
            cluster_id="c1", labels=["new/theme"],
            member_files=["a.md", "b.md", "c.md"],
        )
        state = PatternMinerState(tmp_path / "s.json")
        first = evaluate_cluster(
            cluster, set(), frozenset(), state, min_cluster_size=3,
        )
        assert first is not None
        state.record_proposal(ProposalEntry(
            fingerprint=first.fingerprint,
            cluster_id=cluster.cluster_id,
            status=STATUS_DISCARDED,
        ))
        second = evaluate_cluster(
            cluster, set(), frozenset(), state, min_cluster_size=3,
        )
        assert second is None

    def test_no_slug_returns_none(self, tmp_path: Path) -> None:
        # Pathological label: every label slugifies to empty.
        cluster = ClusterRecord(
            cluster_id="c1", labels=["///", "..."],
            member_files=["a.md", "b.md", "c.md"],
        )
        state = PatternMinerState(tmp_path / "s.json")
        candidate = evaluate_cluster(
            cluster, set(), frozenset(), state, min_cluster_size=3,
        )
        assert candidate is None


# ---------------------------------------------------------------------------
# Stage 2e gate extensions (2026-05-11). Two new helpers chained after
# gate_cluster's 4-part rule:
#   _cluster_has_canonical_source — reject when a cluster member is
#     already under canonical_match_dirs (surveyor pulled yesterday's
#     promote into today's cluster as a "source").
#   _max_jaccard_against_terminal_entries — reject when cluster member
#     set Jaccard-overlaps ≥ threshold with a prior terminal entry.
# ---------------------------------------------------------------------------


class TestClusterHasCanonicalSource:
    def test_canonical_member_rejects_returns_matched_path(self) -> None:
        from alfred.distiller.pattern_miner import _cluster_has_canonical_source

        cluster = ClusterRecord(
            cluster_id="c1",
            labels=["topic/x"],
            member_files=[
                "assumption/a.md",
                "principles/voice-corpus.md",  # canonical surface!
                "constraint/b.md",
            ],
        )
        result = _cluster_has_canonical_source(
            cluster, ["architecture", "principles", "stack"],
        )
        assert result == "principles/voice-corpus.md"

    def test_no_canonical_member_returns_none(self) -> None:
        from alfred.distiller.pattern_miner import _cluster_has_canonical_source

        cluster = ClusterRecord(
            cluster_id="c1",
            labels=["topic/x"],
            member_files=[
                "assumption/a.md",
                "constraint/b.md",
                "decision/c.md",
            ],
        )
        result = _cluster_has_canonical_source(
            cluster, ["architecture", "principles", "stack"],
        )
        assert result is None

    def test_empty_member_list_returns_none(self) -> None:
        from alfred.distiller.pattern_miner import _cluster_has_canonical_source

        cluster = ClusterRecord(
            cluster_id="c1", labels=["topic/x"], member_files=[],
        )
        result = _cluster_has_canonical_source(
            cluster, ["architecture", "principles", "stack"],
        )
        assert result is None

    def test_canonical_string_match_does_not_require_on_disk_existence(
        self, tmp_path: Path,
    ) -> None:
        # Path-prefix-only check; on-disk existence NOT verified here.
        # That's intentional per the helper's contract: the surveyor's
        # view of the member is the load-bearing signal, not the
        # current filesystem state.
        from alfred.distiller.pattern_miner import _cluster_has_canonical_source

        cluster = ClusterRecord(
            cluster_id="c1",
            labels=["topic/x"],
            # File doesn't exist anywhere on disk; helper still
            # rejects because the path is under canonical_match_dirs.
            member_files=["architecture/ghost.md"],
        )
        result = _cluster_has_canonical_source(
            cluster, ["architecture", "principles", "stack"],
        )
        assert result == "architecture/ghost.md"

    def test_first_canonical_member_returned_when_multiple(self) -> None:
        from alfred.distiller.pattern_miner import _cluster_has_canonical_source

        cluster = ClusterRecord(
            cluster_id="c1",
            labels=["topic/x"],
            member_files=[
                "principles/first-canonical.md",
                "architecture/second-canonical.md",
            ],
        )
        result = _cluster_has_canonical_source(
            cluster, ["architecture", "principles", "stack"],
        )
        # First match wins.
        assert result == "principles/first-canonical.md"

    def test_canonical_match_dirs_is_respected(self) -> None:
        # If canonical_match_dirs is narrowed, a member in a non-
        # configured dir is NOT canonical. Per-instance config wins.
        from alfred.distiller.pattern_miner import _cluster_has_canonical_source

        cluster = ClusterRecord(
            cluster_id="c1",
            labels=["topic/x"],
            member_files=["principles/voice-corpus.md"],
        )
        # Restrict canonical dirs to architecture only.
        result = _cluster_has_canonical_source(cluster, ["architecture"])
        assert result is None


class TestMaxJaccardAgainstTerminalEntries:
    def test_no_terminal_entries_returns_zero(
        self, tmp_path: Path,
    ) -> None:
        from alfred.distiller.pattern_miner import (
            _max_jaccard_against_terminal_entries,
        )

        state = PatternMinerState(tmp_path / "s.json")
        sim, matched = _max_jaccard_against_terminal_entries(
            ["a.md", "b.md", "c.md"], state,
        )
        assert sim == 0.0
        assert matched == ""

    def test_identical_member_set_promoted_entry_returns_one(
        self, tmp_path: Path,
    ) -> None:
        from alfred.distiller.pattern_miner import (
            _max_jaccard_against_terminal_entries,
        )

        members = ["a.md", "b.md", "c.md"]
        state = PatternMinerState(tmp_path / "s.json")
        state.record_proposal(ProposalEntry(
            fingerprint="fp_prior",
            status=STATUS_PROMOTED,
            source_member_files=members,
        ))
        sim, matched = _max_jaccard_against_terminal_entries(members, state)
        assert sim == 1.0
        assert matched == "fp_prior"

    def test_fifty_percent_overlap_returns_one_third(
        self, tmp_path: Path,
    ) -> None:
        # 2 of 4 elements shared → |∩|=2, |∪|=6 → 2/6 ≈ 0.333.
        # Pinning the math so future "looks like 0.5" intuition errors
        # surface in the test diff.
        from alfred.distiller.pattern_miner import (
            _max_jaccard_against_terminal_entries,
        )

        cluster_members = ["a.md", "b.md", "c.md", "d.md"]
        prior_members = ["c.md", "d.md", "e.md", "f.md"]
        state = PatternMinerState(tmp_path / "s.json")
        state.record_proposal(ProposalEntry(
            fingerprint="fp_prior",
            status=STATUS_PROMOTED,
            source_member_files=prior_members,
        ))
        sim, _ = _max_jaccard_against_terminal_entries(cluster_members, state)
        assert abs(sim - 1/3) < 1e-9

    def test_jaccard_exactly_half_returns_half(
        self, tmp_path: Path,
    ) -> None:
        # 2 of 4 shared on each side → |∩|=2, |∪|=4 → 0.5 exactly.
        from alfred.distiller.pattern_miner import (
            _max_jaccard_against_terminal_entries,
        )

        cluster_members = ["a.md", "b.md", "c.md", "d.md"]
        prior_members = ["c.md", "d.md", "e.md", "f.md"]
        # Note: above test had |∪|=6, this one shrinks union to 4 by
        # using a smaller prior set.
        prior_members = ["c.md", "d.md"]
        state = PatternMinerState(tmp_path / "s.json")
        state.record_proposal(ProposalEntry(
            fingerprint="fp_prior",
            status=STATUS_PROMOTED,
            source_member_files=prior_members,
        ))
        sim, _ = _max_jaccard_against_terminal_entries(cluster_members, state)
        # |∩|=2, |∪|=4 → 0.5 exactly.
        assert sim == 0.5

    def test_pending_entries_excluded_from_jaccard(
        self, tmp_path: Path,
    ) -> None:
        # Only terminal-status entries (promoted/discarded) count.
        # Pending / split_pending entries can still be deduped via the
        # fingerprint path; semantic-dupe-against-pending would
        # double-count active proposals.
        from alfred.distiller.pattern_miner import (
            _max_jaccard_against_terminal_entries,
        )

        members = ["a.md", "b.md", "c.md"]
        state = PatternMinerState(tmp_path / "s.json")
        state.record_proposal(ProposalEntry(
            fingerprint="fp_pending",
            status="pending",  # NOT terminal
            source_member_files=members,
        ))
        sim, matched = _max_jaccard_against_terminal_entries(members, state)
        # No terminal entries → 0.0.
        assert sim == 0.0
        assert matched == ""

    def test_legacy_entry_empty_source_member_files_returns_zero(
        self, tmp_path: Path,
    ) -> None:
        # Pre-stage-2e entries have empty source_member_files. Jaccard
        # against an empty set is 0.0 → no false rejects.
        from alfred.distiller.pattern_miner import (
            _max_jaccard_against_terminal_entries,
        )

        state = PatternMinerState(tmp_path / "s.json")
        state.record_proposal(ProposalEntry(
            fingerprint="fp_legacy",
            status=STATUS_PROMOTED,
            source_member_files=[],  # legacy: empty
        ))
        sim, _ = _max_jaccard_against_terminal_entries(
            ["a.md", "b.md", "c.md"], state,
        )
        assert sim == 0.0

    def test_max_taken_when_multiple_terminal_entries(
        self, tmp_path: Path,
    ) -> None:
        # Cluster matches two terminals — one partial, one full. Max
        # similarity wins; matched fingerprint = the one with the
        # max.
        from alfred.distiller.pattern_miner import (
            _max_jaccard_against_terminal_entries,
        )

        members = ["a.md", "b.md", "c.md"]
        state = PatternMinerState(tmp_path / "s.json")
        state.record_proposal(ProposalEntry(
            fingerprint="fp_partial",
            status=STATUS_PROMOTED,
            source_member_files=["a.md", "d.md", "e.md"],  # 1/5 overlap
        ))
        state.record_proposal(ProposalEntry(
            fingerprint="fp_full",
            status=STATUS_DISCARDED,
            source_member_files=members,  # full overlap
        ))
        sim, matched = _max_jaccard_against_terminal_entries(members, state)
        assert sim == 1.0
        assert matched == "fp_full"

    def test_discarded_entries_also_count(self, tmp_path: Path) -> None:
        # Both promoted AND discarded entries are terminal; both
        # contribute to Jaccard. (Catches a regression that limits to
        # only promoted.)
        from alfred.distiller.pattern_miner import (
            _max_jaccard_against_terminal_entries,
        )

        members = ["a.md", "b.md", "c.md"]
        state = PatternMinerState(tmp_path / "s.json")
        state.record_proposal(ProposalEntry(
            fingerprint="fp_disc",
            status=STATUS_DISCARDED,
            source_member_files=members,
        ))
        sim, matched = _max_jaccard_against_terminal_entries(members, state)
        assert sim == 1.0
        assert matched == "fp_disc"

    def test_empty_cluster_members_returns_zero(
        self, tmp_path: Path,
    ) -> None:
        # Defensive: empty input → 0.0 (no members to compare). The
        # caller's earlier "labeled / substantive" gate already drops
        # this case but the helper handles it cleanly anyway.
        from alfred.distiller.pattern_miner import (
            _max_jaccard_against_terminal_entries,
        )

        state = PatternMinerState(tmp_path / "s.json")
        state.record_proposal(ProposalEntry(
            fingerprint="fp",
            status=STATUS_PROMOTED,
            source_member_files=["a.md"],
        ))
        sim, matched = _max_jaccard_against_terminal_entries([], state)
        assert sim == 0.0
        assert matched == ""
