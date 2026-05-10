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
