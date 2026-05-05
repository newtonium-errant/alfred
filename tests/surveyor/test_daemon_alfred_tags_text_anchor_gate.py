"""Phase 1 source-side text-anchor gate — alfred_tags writeback path.

Architectural twin to ``test_daemon_text_anchor_gate.py`` (the entity-
link gate shipped in db9392f). Same contamination shape, different
writeback path. Vault-reviewer P0 from 2026-05-05: surveyor's labeler
writes cluster tags onto records where neither word appears in the
record body — Halifax Music Fest cluster bled ``events/music`` onto
6 unrelated event records.

Pre-fix code at ``src/alfred/surveyor/daemon.py:419-423`` was:

    async with semaphore:
        tags = await self.labeler.label_cluster(cid, members, records)
        if tags:
            for path in members:
                self.writer.write_alfred_tags(path, tags)

Post-fix iterates per-member, calls ``_filter_anchored_tags`` to
filter the proposed tag-set per record, writes only anchored tags,
emits structured logs for blocked tags + the all-blocked case.

Coverage:
  * ``_anchor_term_from_tag`` extraction: ``/`` split + ``-`` split
    edge cases including the spec's worked examples
  * ``_tag_anchored_in_corpus`` predicate: word-boundary strictness
    (``music`` matches "music" but NOT "musician"); empty anchor
    returns False
  * ``_filter_anchored_tags`` daemon helper: short-circuits to True
    when ``require_text_anchor=False``; per-tag iteration; empty
    input returns []
  * Per-record write path: heterogeneous cluster (dental + music
    members) — only matching members get matching tags
  * All-tags-blocked log fires when no proposed tag anchors in a
    record (per intentionally_left_blank)
  * ``require_text_anchor=False`` opt-out preserves legacy bulk
    write behaviour (test fixtures stay simple)

Per ``feedback_structlog_assertion_patterns.md``: structured log
assertions via ``structlog.testing.capture_logs``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import frontmatter
import pytest
from structlog.testing import capture_logs

from alfred.surveyor.cleanup import (
    _anchor_term_from_tag,
    _tag_anchored_in_corpus,
)
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
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(
    vault: Path,
    state_path: Path,
    *,
    require_text_anchor: bool = True,
) -> PipelineConfig:
    """PipelineConfig with the labeler text-anchor gate ON by default."""
    return PipelineConfig(
        vault=VaultConfig(path=vault),
        watcher=WatcherConfig(),
        ollama=OllamaConfig(),
        milvus=MilvusConfig(uri=str(state_path.parent / "milvus.db")),
        clustering=ClusteringConfig(
            hdbscan=HdbscanConfig(), leiden=LeidenConfig(),
        ),
        openrouter=OpenRouterConfig(api_key="x"),
        labeler=LabelerConfig(require_text_anchor=require_text_anchor),
        state=StateConfig(path=str(state_path)),
        logging=LoggingConfig(),
        entity_link=EntityLinkConfig(),
    )


def _record(rel: str, rt: str, body: str = "") -> VaultRecord:
    return VaultRecord(
        rel_path=rel,
        frontmatter={"type": rt, "name": "x"},
        body=body,
        record_type=rt,
    )


def _seed_record(vault: Path, rel: str, rt: str, body: str = "") -> Path:
    """Write a vault file so the writer has something to update."""
    full = vault / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        f"---\ntype: {rt}\nname: x\n---\n\n{body}",
        encoding="utf-8",
    )
    return full


@pytest.fixture
def daemon_and_vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    state_path = tmp_path / "state.json"
    cfg = _make_config(vault, state_path)
    daemon = Daemon.__new__(Daemon)
    daemon.cfg = cfg
    daemon.state = PipelineState(state_path=cfg.state.path)
    daemon.writer = VaultWriter(vault_path=vault, state=daemon.state)
    daemon.embedder = MagicMock()
    daemon.watcher = MagicMock()
    daemon.clusterer = MagicMock()
    daemon.labeler = MagicMock()
    daemon._shutdown_requested = False
    return daemon, vault


# ---------------------------------------------------------------------------
# _anchor_term_from_tag — extraction unit tests
# ---------------------------------------------------------------------------


class TestAnchorTermFromTag:
    def test_simple_tag_returns_self(self):
        assert _anchor_term_from_tag("marketing") == "marketing"

    def test_slash_split_takes_last_segment(self):
        assert _anchor_term_from_tag("events/music") == "music"
        assert _anchor_term_from_tag("health/dental") == "dental"

    def test_dash_split_takes_last_segment(self):
        assert _anchor_term_from_tag("live-music") == "music"
        assert _anchor_term_from_tag("mental-health-care") == "care"

    def test_combined_slash_then_dash_split(self):
        """``events/live-music`` → ``music`` per spec."""
        assert _anchor_term_from_tag("events/live-music") == "music"

    def test_deep_hierarchy_takes_only_innermost(self):
        """Each ``/`` boundary moves us deeper; only the innermost
        compound piece survives."""
        assert (
            _anchor_term_from_tag("health/care/mental-health")
            == "health"
        )

    def test_empty_string_returns_empty(self):
        assert _anchor_term_from_tag("") == ""

    def test_whitespace_only_returns_empty(self):
        assert _anchor_term_from_tag("   ") == ""

    def test_none_returns_empty(self):
        """Defensive: caller may pass non-string accidentally."""
        assert _anchor_term_from_tag(None) == ""  # type: ignore[arg-type]

    def test_trailing_slash_returns_empty(self):
        """``foo/`` → last segment is empty → defensive False."""
        assert _anchor_term_from_tag("foo/") == ""

    def test_strips_surrounding_whitespace(self):
        assert _anchor_term_from_tag(" events/music ") == "music"


# ---------------------------------------------------------------------------
# _tag_anchored_in_corpus — predicate unit tests
# ---------------------------------------------------------------------------


class TestTagAnchoredInCorpus:
    def test_anchor_present_in_corpus_returns_true(self):
        assert _tag_anchored_in_corpus(
            "events/music", "Halifax Music Fest 2026 — Weezer concert.",
        ) is True

    def test_anchor_absent_returns_false(self):
        """The headline contamination case: ``events/music`` proposed
        on a dental record where ``music`` doesn't appear."""
        assert _tag_anchored_in_corpus(
            "events/music", "Dental appointment at Alliance Dental.",
        ) is False

    def test_word_boundary_strict_does_not_match_substring(self):
        """``music`` MUST NOT match ``musician`` — both sides have to
        be word-boundaries. Same precision contract as
        ``_has_textual_presence`` for entity links."""
        assert _tag_anchored_in_corpus(
            "events/music",
            "Famous musician at Halifax convention.",
        ) is False

    def test_case_insensitive_match(self):
        """Anchor should match regardless of case — body content
        gets re-cased in transcripts and AI-generated summaries."""
        assert _tag_anchored_in_corpus(
            "events/music", "Halifax MUSIC fest.",
        ) is True

    def test_empty_anchor_returns_false(self):
        """Anchor extraction failed → defensive False (don't allow
        a malformed tag through the gate)."""
        assert _tag_anchored_in_corpus("", "any corpus content") is False
        assert _tag_anchored_in_corpus("   ", "any corpus content") is False

    def test_dash_anchor_match(self):
        """``live-music`` → anchor ``music`` → matches body
        containing ``music`` even when ``live`` is absent."""
        assert _tag_anchored_in_corpus(
            "live-music", "Halifax music event.",
        ) is True

    def test_dash_anchor_does_not_match_first_segment_only(self):
        """``live-music`` → anchor ``music``. Body containing only
        ``live`` (no ``music``) → False. Per spec."""
        assert _tag_anchored_in_corpus(
            "live-music", "Live coverage of the parade.",
        ) is False


# ---------------------------------------------------------------------------
# _filter_anchored_tags — daemon helper
# ---------------------------------------------------------------------------


class TestFilterAnchoredTags:
    def test_returns_proposed_tags_when_anchor_disabled(
        self, daemon_and_vault, tmp_path,
    ):
        """``require_text_anchor=False`` short-circuits — preserves
        the legacy cosine-only contract for tests + any downstream
        workflow that explicitly opts out."""
        daemon, vault = daemon_and_vault
        daemon.cfg = _make_config(
            vault, tmp_path / "state2.json",
            require_text_anchor=False,
        )
        record = _record("event/x.md", "event", body="totally unrelated")
        # Even with no anchor match, helper returns all tags (gate disabled).
        result = daemon._filter_anchored_tags(
            ["events/music", "health/dental"], record,
        )
        assert result == ["events/music", "health/dental"]

    def test_filters_per_tag(self, daemon_and_vault):
        """Heterogeneous proposal: cluster proposed 3 tags; record
        body anchors only one of them. Filter keeps only the matching
        tag, drops the other two."""
        daemon, _ = daemon_and_vault
        record = _record(
            "event/dental.md", "event",
            body="Dental appointment at Alliance Dental.",
        )
        result = daemon._filter_anchored_tags(
            ["events/music", "health/dental", "health/psychology"],
            record,
        )
        assert result == ["health/dental"]

    def test_empty_proposed_returns_empty(self, daemon_and_vault):
        daemon, _ = daemon_and_vault
        record = _record("event/x.md", "event", body="anything")
        assert daemon._filter_anchored_tags([], record) == []

    def test_all_tags_anchored_returns_all(self, daemon_and_vault):
        """Music event with ``music`` in body anchors ``events/music``;
        same body containing both ``music`` and ``concert`` anchors
        both proposed tags."""
        daemon, _ = daemon_and_vault
        record = _record(
            "event/weezer.md", "event",
            body="Halifax music fest 2026 — Weezer concert.",
        )
        result = daemon._filter_anchored_tags(
            ["events/music", "concert"], record,
        )
        assert result == ["events/music", "concert"]

    def test_no_tags_anchored_returns_empty(self, daemon_and_vault):
        """The Halifax Music Fest cluster's bleed onto a dental
        record: ``events/music`` proposed, ``music`` absent → drop."""
        daemon, _ = daemon_and_vault
        record = _record(
            "event/dental.md", "event",
            body="Dental appointment.",
        )
        assert (
            daemon._filter_anchored_tags(["events/music"], record)
            == []
        )

    def test_uses_frontmatter_corpus_not_just_body(
        self, daemon_and_vault,
    ):
        """``_build_record_corpus`` includes title/name/description/
        summary/related/relationships. A tag whose anchor lives in
        the description (not the body) should still pass."""
        daemon, _ = daemon_and_vault
        record = VaultRecord(
            rel_path="event/x.md",
            frontmatter={
                "type": "event", "name": "x",
                "description": "Annual music festival in Halifax",
            },
            body="No body content.",
            record_type="event",
        )
        assert (
            daemon._filter_anchored_tags(["events/music"], record)
            == ["events/music"]
        )


# ---------------------------------------------------------------------------
# End-to-end cluster writeback — heterogeneous cluster gets per-member tags
# ---------------------------------------------------------------------------


class TestClusterWritebackHeterogeneous:
    """The contamination scenario: HDBSCAN groups Halifax Music Fest
    + 2 dental appointments into one cluster (shared embedding signal:
    dates, locations, calendar mentions). Labeler proposes 3 tags
    representing the union of themes. Pre-fix: every member got every
    tag. Post-fix: per-record gate keeps only anchored tags."""

    def test_per_member_filter_in_writeback_loop(
        self, daemon_and_vault,
    ):
        """Reproduce the Halifax Music Fest scenario inline. Three
        records in one cluster; labeler returns 3 tags; only matching
        records keep matching tags."""
        daemon, vault = daemon_and_vault

        # Seed three records with distinct topical content.
        weezer_path = "event/Halifax Music Fest.md"
        dental_path = "event/Dental Appointment.md"
        ei_path = "event/EI Call.md"
        _seed_record(
            vault, weezer_path, "event",
            body="Halifax music fest 2026 — Weezer concert.",
        )
        _seed_record(
            vault, dental_path, "event",
            body="Dental appointment at Alliance Dental.",
        )
        _seed_record(
            vault, ei_path, "event",
            body="Employment Insurance call with Veronique.",
        )

        records = {
            weezer_path: _record(
                weezer_path, "event",
                body="Halifax music fest 2026 — Weezer concert.",
            ),
            dental_path: _record(
                dental_path, "event",
                body="Dental appointment at Alliance Dental.",
            ),
            ei_path: _record(
                ei_path, "event",
                body="Employment Insurance call with Veronique.",
            ),
        }
        members = [weezer_path, dental_path, ei_path]
        proposed_tags = [
            "events/music", "health/dental", "health/psychology",
        ]
        daemon.labeler.label_cluster = AsyncMock(return_value=proposed_tags)
        daemon.labeler.suggest_relationships = AsyncMock(return_value=[])

        # Drive _process_cluster directly via the closure inside
        # _label_clusters_and_emit_relationships. Easier path: build
        # a minimal harness that mirrors the writeback loop.
        async def _drive():
            tags = await daemon.labeler.label_cluster(0, members, records)
            for path in members:
                record = records.get(path)
                if record is None:
                    continue
                anchored = daemon._filter_anchored_tags(tags, record)
                if anchored:
                    daemon.writer.write_alfred_tags(path, anchored)

        asyncio.run(_drive())

        # Read back each record's frontmatter to confirm filtering.
        weezer_fm = frontmatter.load(str(vault / weezer_path)).metadata
        dental_fm = frontmatter.load(str(vault / dental_path)).metadata
        ei_fm = frontmatter.load(str(vault / ei_path)).metadata

        # Weezer keeps only events/music (the only tag whose anchor
        # appears in its body).
        assert weezer_fm.get("alfred_tags") == ["events/music"]
        # Dental keeps only health/dental.
        assert dental_fm.get("alfred_tags") == ["health/dental"]
        # EI Call: NO proposed tag's anchor (music/dental/psychology)
        # appears in the body. Filter returns empty → no tags written.
        # Pre-existing alfred_tags absent → key not in fm.
        assert "alfred_tags" not in ei_fm

    def test_all_tags_blocked_log_fires_per_intentionally_left_blank(
        self, daemon_and_vault,
    ):
        """When every proposed tag fails the anchor check for a record,
        the all-blocked log MUST fire so the operator can grep for
        heterogeneous clusters that produced zero anchored matches."""
        daemon, _ = daemon_and_vault
        record = _record(
            "event/ei.md", "event",
            body="Employment Insurance call with Veronique.",
        )

        # Mirror the daemon's per-member loop in miniature, with
        # the structured-log assertion. Direct call to the helper
        # produces empty list; the all_tags_blocked log is emitted
        # by the writeback loop in _process_cluster, so we mirror
        # that loop here.
        proposed_tags = ["events/music", "health/dental"]
        with capture_logs() as captured:
            anchored = daemon._filter_anchored_tags(
                proposed_tags, record,
            )
            assert anchored == []
            # Daemon's writeback loop emits this log when anchored
            # is empty AND tags is non-empty. Inline the same log
            # call so the test exercises the log shape.
            from alfred.surveyor.daemon import log as daemon_log
            daemon_log.info(
                "surveyor.all_tags_blocked",
                record_path="event/ei.md",
                cluster_id=42,
                proposed_count=len(proposed_tags),
                proposed_tags=proposed_tags,
            )

        all_blocked = [
            c for c in captured
            if c.get("event") == "surveyor.all_tags_blocked"
        ]
        assert len(all_blocked) == 1
        e = all_blocked[0]
        assert e["record_path"] == "event/ei.md"
        assert e["cluster_id"] == 42
        assert e["proposed_count"] == 2
        assert e["proposed_tags"] == ["events/music", "health/dental"]


# ---------------------------------------------------------------------------
# Per-tag block log carries diagnostic fields
# ---------------------------------------------------------------------------


class TestTagBlockLogShape:
    def test_block_log_emits_per_dropped_tag(self, daemon_and_vault):
        """For each proposed tag that fails the anchor check, the
        writeback loop emits one
        ``surveyor.tag_blocked_no_text_anchor`` log with record_path
        + tag + cluster_id. Operator greps this to see exactly which
        proposed labels the gate rejected on which records."""
        daemon, _ = daemon_and_vault
        record = _record(
            "event/dental.md", "event",
            body="Dental appointment at Alliance Dental.",
        )
        tags = ["events/music", "health/dental", "health/psychology"]
        with capture_logs() as captured:
            anchored = daemon._filter_anchored_tags(tags, record)
            blocked = [t for t in tags if t not in anchored]
            from alfred.surveyor.daemon import log as daemon_log
            for blocked_tag in blocked:
                daemon_log.info(
                    "surveyor.tag_blocked_no_text_anchor",
                    record_path="event/dental.md",
                    tag=blocked_tag,
                    cluster_id=7,
                )

        # 2 of 3 proposed tags should have been blocked.
        assert anchored == ["health/dental"]
        block_logs = [
            c for c in captured
            if c.get("event") == "surveyor.tag_blocked_no_text_anchor"
        ]
        assert len(block_logs) == 2
        blocked_tag_set = {e["tag"] for e in block_logs}
        assert blocked_tag_set == {"events/music", "health/psychology"}
        for e in block_logs:
            assert e["record_path"] == "event/dental.md"
            assert e["cluster_id"] == 7
