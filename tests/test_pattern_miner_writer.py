"""Writer + reconcile tests for the Phase 4 pattern miner.

Pins:
- Proposal markdown frontmatter shape (Q4 in the design memo)
- Source-member wikilink rendering
- The pre-filled `alfred vault move` "Suggested next step" line
- Atomic write semantics
- The .gitkeep empty-state marker per the universal "intentionally
  left blank" rule
- The reconcile sweep's promoted/discarded/still_pending classifier
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.distiller.pattern_miner import (
    ClusterRecord,
    DraftResult,
    ProposalCandidate,
    _atomic_write,
    _write_empty_state_marker,
    fingerprint_cluster,
    reconcile_state,
    render_proposal_markdown,
)
from alfred.distiller.pattern_miner_state import (
    PatternMinerState,
    ProposalEntry,
    STATUS_DISCARDED,
    STATUS_PENDING,
    STATUS_PROMOTED,
)


def _candidate(
    cluster_id: str = "semantic_5",
    labels: list[str] | None = None,
    member_files: list[str] | None = None,
    proposed_slug: str = "local-llm-hardware",
    proposed_canonical_type: str = "principles",
) -> ProposalCandidate:
    cluster = ClusterRecord(
        cluster_id=cluster_id,
        labels=labels or ["llm/quantization", "performance/benchmarking"],
        member_files=member_files or [
            "assumption/Ollama defaults to Q4_K_M.md",
            "constraint/Q4 quantization amplifies failures.md",
            "synthesis/Hardware-fixable vs model-fixable gap.md",
        ],
    )
    return ProposalCandidate(
        cluster=cluster,
        fingerprint=fingerprint_cluster(cluster.member_files, cluster.labels),
        proposed_slug=proposed_slug,
        proposed_canonical_type=proposed_canonical_type,
    )


# ---------------------------------------------------------------------------
# render_proposal_markdown — frontmatter + body shape
# ---------------------------------------------------------------------------


class TestRenderProposalMarkdown:
    def test_frontmatter_includes_required_fields(self) -> None:
        candidate = _candidate()
        draft = DraftResult(paragraph="Quantization choice silently degrades extraction quality.")
        md = render_proposal_markdown(
            candidate, draft,
            proposed_at="2026-05-10T14:30:00+00:00",
            proposed_path="inbox/proposed-canonical/local-llm-hardware.md",
            proposed_canonical_type="principles",
            proposed_slug="local-llm-hardware",
        )
        # Frontmatter delimiters
        assert md.startswith("---\n")
        assert "\n---\n" in md
        # Required fields
        assert "type: proposed-canonical" in md
        assert 'proposed_at: "2026-05-10T14:30:00+00:00"' in md
        assert 'source_cluster_id: "semantic_5"' in md
        assert "source_member_count: 3" in md
        assert 'proposed_canonical_type: "principles"' in md
        assert 'proposed_slug: "local-llm-hardware"' in md
        assert 'fingerprint: "' in md
        assert "status: proposed" in md

    def test_body_includes_drafter_paragraph(self) -> None:
        candidate = _candidate()
        draft = DraftResult(paragraph="A specific claim about local-LLM hardware.")
        md = render_proposal_markdown(
            candidate, draft,
            proposed_at="2026-05-10T14:30:00+00:00",
            proposed_path="inbox/proposed-canonical/local-llm-hardware.md",
            proposed_canonical_type="principles",
            proposed_slug="local-llm-hardware",
        )
        assert "## Mined claim" in md
        assert "A specific claim about local-LLM hardware." in md

    def test_body_uses_placeholder_when_drafter_empty(self) -> None:
        # Drafter unavailable / empty → placeholder paragraph the
        # operator fills in. Per the safe-degraded path.
        candidate = _candidate()
        draft = DraftResult()  # paragraph empty
        md = render_proposal_markdown(
            candidate, draft,
            proposed_at="2026-05-10T14:30:00+00:00",
            proposed_path="inbox/proposed-canonical/local-llm-hardware.md",
            proposed_canonical_type="principles",
            proposed_slug="local-llm-hardware",
        )
        assert "Drafter LLM unavailable" in md

    def test_source_members_rendered_as_wikilinks(self) -> None:
        candidate = _candidate(member_files=[
            "assumption/Foo bar.md",
            "decision/Baz Qux.md",
        ])
        draft = DraftResult(paragraph="x")
        md = render_proposal_markdown(
            candidate, draft,
            proposed_at="2026-05-10T14:30:00+00:00",
            proposed_path="inbox/proposed-canonical/x.md",
            proposed_canonical_type="architecture",
            proposed_slug="x",
        )
        # Wikilinks omit the .md extension.
        assert "[[assumption/Foo bar]]" in md
        assert "[[decision/Baz Qux]]" in md

    def test_suggested_next_step_includes_per_instance_basename(self) -> None:
        candidate = _candidate()
        draft = DraftResult(paragraph="x")
        md = render_proposal_markdown(
            candidate, draft,
            proposed_at="2026-05-10T14:30:00+00:00",
            proposed_path="inbox/proposed-canonical/local-llm-hardware.md",
            proposed_canonical_type="principles",
            proposed_slug="local-llm-hardware",
            instance_config_basename="config.kalle.yaml",
        )
        # The hint is operator-actionable: copy-paste a real CLI line.
        assert "alfred --config config.kalle.yaml vault move" in md
        assert '"inbox/proposed-canonical/local-llm-hardware.md"' in md
        assert '"principles/local-llm-hardware.md"' in md

    def test_suggested_next_step_default_basename_is_config_yaml(self) -> None:
        candidate = _candidate()
        draft = DraftResult(paragraph="x")
        md = render_proposal_markdown(
            candidate, draft,
            proposed_at="2026-05-10T14:30:00+00:00",
            proposed_path="inbox/proposed-canonical/x.md",
            proposed_canonical_type="architecture",
            proposed_slug="x",
        )
        assert "alfred --config config.yaml vault move" in md

    def test_title_humanizes_slug(self) -> None:
        candidate = _candidate()
        draft = DraftResult(paragraph="x")
        md = render_proposal_markdown(
            candidate, draft,
            proposed_at="2026-05-10T14:30:00+00:00",
            proposed_path="inbox/proposed-canonical/local-llm-hardware.md",
            proposed_canonical_type="principles",
            proposed_slug="local-llm-hardware",
        )
        assert "# Local Llm Hardware" in md  # title-case from slug


# ---------------------------------------------------------------------------
# _atomic_write — tmp → rename
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / "x.md"
        _atomic_write(target, "hello")
        assert target.read_text() == "hello"

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "x.md"
        target.write_text("old")
        _atomic_write(target, "new")
        assert target.read_text() == "new"

    def test_no_tmp_files_left_after_success(self, tmp_path: Path) -> None:
        target = tmp_path / "x.md"
        _atomic_write(target, "hi")
        # No leftover .tmp files in the parent dir.
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []


# ---------------------------------------------------------------------------
# _write_empty_state_marker — .gitkeep with last-mined timestamp
# ---------------------------------------------------------------------------


class TestEmptyStateMarker:
    def test_writes_gitkeep_with_timestamp(self, tmp_path: Path) -> None:
        proposed_dir = tmp_path / "inbox" / "proposed-canonical"
        _write_empty_state_marker(
            proposed_dir,
            last_mined="2026-05-10T14:30:00+00:00",
            candidate_count=5,
            survivor_count=0,
        )
        keep = proposed_dir / ".gitkeep"
        assert keep.is_file()
        content = keep.read_text()
        assert "2026-05-10T14:30:00+00:00" in content
        assert "Candidates evaluated: 5" in content
        assert "Candidates that passed the gate: 0" in content

    def test_idempotent_rewrite(self, tmp_path: Path) -> None:
        proposed_dir = tmp_path / "inbox" / "proposed-canonical"
        _write_empty_state_marker(
            proposed_dir, last_mined="ts1",
            candidate_count=1, survivor_count=0,
        )
        _write_empty_state_marker(
            proposed_dir, last_mined="ts2",
            candidate_count=2, survivor_count=0,
        )
        # Latest call wins.
        content = (proposed_dir / ".gitkeep").read_text()
        assert "ts2" in content
        assert "ts1" not in content


# ---------------------------------------------------------------------------
# reconcile_state — promoted / discarded / still-pending classifier
# ---------------------------------------------------------------------------


class TestReconcileState:
    def test_pending_with_file_present_stays_pending(
        self, tmp_path: Path,
    ) -> None:
        # Vault layout: the proposal file still exists at proposed_path.
        proposed_dir = tmp_path / "inbox" / "proposed-canonical"
        proposed_dir.mkdir(parents=True)
        proposed_file = proposed_dir / "topic-x.md"
        proposed_file.write_text("body")

        state = PatternMinerState(tmp_path / "s.json")
        state.record_proposal(ProposalEntry(
            fingerprint="fp1",
            cluster_id="c1",
            proposed_path="inbox/proposed-canonical/topic-x.md",
            proposed_slug="topic-x",
            status=STATUS_PENDING,
        ))
        result = reconcile_state(
            state, tmp_path, ["architecture", "principles", "stack"],
        )
        assert result["still_pending"] == 1
        assert result["promoted"] == 0
        assert result["discarded"] == 0
        assert state.proposals["fp1"].status == STATUS_PENDING

    def test_pending_with_canonical_match_marked_promoted(
        self, tmp_path: Path,
    ) -> None:
        # Operator promoted: proposed file gone, but a canonical
        # artifact with the same slug appeared under principles/.
        (tmp_path / "principles").mkdir()
        (tmp_path / "principles" / "topic-x.md").write_text("body")

        state = PatternMinerState(tmp_path / "s.json")
        state.record_proposal(ProposalEntry(
            fingerprint="fp1",
            cluster_id="c1",
            proposed_path="inbox/proposed-canonical/topic-x.md",
            proposed_slug="topic-x",
            status=STATUS_PENDING,
        ))
        result = reconcile_state(
            state, tmp_path, ["architecture", "principles", "stack"],
        )
        assert result["promoted"] == 1
        assert state.proposals["fp1"].status == STATUS_PROMOTED

    def test_pending_with_no_match_marked_discarded(
        self, tmp_path: Path,
    ) -> None:
        # Operator deleted: proposed file gone AND no canonical artifact
        # with the matching slug exists anywhere.
        state = PatternMinerState(tmp_path / "s.json")
        state.record_proposal(ProposalEntry(
            fingerprint="fp1",
            cluster_id="c1",
            proposed_path="inbox/proposed-canonical/topic-x.md",
            proposed_slug="topic-x",
            status=STATUS_PENDING,
        ))
        result = reconcile_state(
            state, tmp_path, ["architecture", "principles", "stack"],
        )
        assert result["discarded"] == 1
        assert state.proposals["fp1"].status == STATUS_DISCARDED

    def test_already_promoted_skipped(self, tmp_path: Path) -> None:
        # Reconcile only operates on STATUS_PENDING entries — promoted /
        # discarded / superseded entries are operator-decided endpoints.
        state = PatternMinerState(tmp_path / "s.json")
        state.record_proposal(ProposalEntry(
            fingerprint="fp1",
            cluster_id="c1",
            proposed_path="inbox/proposed-canonical/topic-x.md",
            proposed_slug="topic-x",
            status=STATUS_PROMOTED,
        ))
        result = reconcile_state(
            state, tmp_path, ["architecture", "principles", "stack"],
        )
        # No bucket incremented; no status changed.
        assert result["promoted"] == 0
        assert result["discarded"] == 0
        assert result["still_pending"] == 0
        assert state.proposals["fp1"].status == STATUS_PROMOTED
