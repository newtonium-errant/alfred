"""Phase 1 source-side text-anchor gate — daemon write paths.

Tests the per-write text-anchor gate added to all 3 entity-link
writeback stages in ``src/alfred/surveyor/daemon.py``:
  * ``_link_entities_in_clusters`` (within-cluster member linking)
  * ``_link_noise_points_to_entities`` (full-vault scan for noise)
  * ``_backfill_new_entities`` (new entity → vault scan)

Background: cosine similarity alone produces topic-coherent false
positives — records about "music events" cluster with music-
related person records in embedding space without factual
association. The gate adds a word-boundary text-anchor check
(reusing the cleanup CLI's ``_has_textual_presence`` helper) so
only records that actually mention the entity name get the link.
Standard precision-control pattern from entity-linking systems.

Coverage:
  * Each of the 3 stages: blocked when entity name absent from
    record body / frontmatter
  * Each of the 3 stages: allowed when entity name present
  * ``require_text_anchor=False`` config opt-out — cosine-only
    semantic preserved (legacy tests pin this through their
    own _make_config helpers)
  * Word-boundary strictness: "Ben" body matches "person/Ben.md"
    target but NOT "person/Ben McMillan.md" (matches cleanup CLI
    parity contract)
  * Block log emits ``surveyor.entity_link_blocked_no_text_anchor``
    with structured fields (record_path / entity_path / similarity
    / threshold / stage)

Per ``feedback_structlog_assertion_patterns.md``: structured log
assertions via ``structlog.testing.capture_logs``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import frontmatter
import numpy as np
import pytest
from structlog.testing import capture_logs

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
    threshold: float = 0.75,
    max_per: int = 5,
    require_text_anchor: bool = True,
    backfill: bool = True,
) -> PipelineConfig:
    """Config with text-anchor ON by default (the new contract)."""
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
            require_text_anchor=require_text_anchor,
        ),
    )


def _write(vault: Path, rel: str, rt: str, body: str = "body\n") -> None:
    full = vault / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        f"---\ntype: {rt}\nname: x\n---\n\n{body}",
        encoding="utf-8",
    )


def _record(rel: str, rt: str, body: str = "") -> VaultRecord:
    """In-memory VaultRecord with explicit body content."""
    return VaultRecord(
        rel_path=rel,
        frontmatter={"type": rt, "name": "x"},
        body=body,
        record_type=rt,
    )


def _normalise(vec: list[float]) -> list[float]:
    a = np.asarray(vec, dtype=np.float32)
    n = float(np.linalg.norm(a))
    return (a / n).tolist() if n > 0 else a.tolist()


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
# _entity_name_appears_in_record helper
# ---------------------------------------------------------------------------


def test_helper_returns_true_when_anchor_disabled(daemon_and_vault, tmp_path):
    """``require_text_anchor=False`` short-circuits to True regardless
    of textual presence — preserves the legacy cosine-only contract."""
    daemon, vault = daemon_and_vault
    daemon.cfg = _make_config(
        vault, tmp_path / "state2.json",
        require_text_anchor=False,
    )
    record = _record("event/a.md", "event", body="totally unrelated")
    # Even with no name match, helper says yes (gate disabled).
    assert daemon._entity_name_appears_in_record(
        "person/Ben McMillan.md", record,
    ) is True


def test_helper_word_boundary_strictness(daemon_and_vault):
    """Ben body matches person/Ben.md but NOT person/Ben McMillan.md.
    Pins parity with the cleanup CLI's ``_has_textual_presence``."""
    daemon, _vault = daemon_and_vault
    record = _record(
        "event/coffee.md", "event",
        body="Ben said the report looks great.",
    )
    # "Ben" alone matches person/Ben.md.
    assert daemon._entity_name_appears_in_record(
        "person/Ben.md", record,
    ) is True
    # "Ben McMillan" does NOT match (only first name in body).
    assert daemon._entity_name_appears_in_record(
        "person/Ben McMillan.md", record,
    ) is False


def test_helper_checks_frontmatter_surfaces(daemon_and_vault):
    """Helper looks at title/description/related/relationships per
    cleanup module's ``_build_record_corpus``."""
    daemon, _vault = daemon_and_vault
    record = VaultRecord(
        rel_path="project/X.md",
        frontmatter={
            "type": "project",
            "title": "Q3 push",
            "description": "Coordinated by Jamie",
            "related": ["[[person/Andrew Newton]]"],
        },
        body="Project body has no name mentions here.",
        record_type="project",
    )
    # Match via description.
    assert daemon._entity_name_appears_in_record(
        "person/Jamie.md", record,
    ) is True
    # Match via related wikilink.
    assert daemon._entity_name_appears_in_record(
        "person/Andrew Newton.md", record,
    ) is True
    # No match — name absent everywhere.
    assert daemon._entity_name_appears_in_record(
        "person/Ben McMillan.md", record,
    ) is False


# ---------------------------------------------------------------------------
# Stage 1: _link_entities_in_clusters with text-anchor gate
# ---------------------------------------------------------------------------


def test_cluster_stage_blocks_link_when_name_absent(daemon_and_vault):
    """Cosine sim says link, but body never mentions the person →
    blocked + log emitted."""
    daemon, vault = daemon_and_vault
    _write(vault, "person/Ben McMillan.md", "person", body="Ben McMillan, music professional.\n")
    _write(vault, "event/concert.md", "event", body="A music event.\n")
    daemon.state.update_file("person/Ben McMillan.md", "h1")
    daemon.state.update_file("event/concert.md", "h2")

    records = {
        "person/Ben McMillan.md": _record(
            "person/Ben McMillan.md", "person",
            body="Ben McMillan, music professional.",
        ),
        # Event body NEVER mentions Ben McMillan — the contamination case.
        "event/concert.md": _record(
            "event/concert.md", "event",
            body="A music event in the park.",
        ),
    }
    # Both topic-similar (cos ≈ 0.95) → would link pre-fix.
    v = _normalise([1.0, 0.0])
    paths = ["person/Ben McMillan.md", "event/concert.md"]
    vectors = np.asarray([v, v], dtype=np.float32)
    cluster_members = {0: paths[:]}

    with capture_logs() as captured:
        daemon._link_entities_in_clusters(
            changed_cluster_ids={0},
            cluster_members=cluster_members,
            records=records,
            all_paths=paths,
            all_vectors=vectors,
        )

    # Frontmatter unchanged — block fired before write.
    md = frontmatter.load(vault / "event/concert.md").metadata
    assert "related_persons" not in md

    # Block log emitted with structured fields.
    blocks = [
        c for c in captured
        if c.get("event") == "surveyor.entity_link_blocked_no_text_anchor"
    ]
    assert len(blocks) == 1
    log_entry = blocks[0]
    assert log_entry["log_level"] == "info"
    assert log_entry["record_path"] == "event/concert.md"
    assert log_entry["entity_path"] == "person/Ben McMillan.md"
    assert log_entry["stage"] == "cluster"
    assert log_entry["cluster_id"] == 0


def test_cluster_stage_allows_link_when_name_present(daemon_and_vault):
    """Cosine sim says link AND body mentions the person → allowed."""
    daemon, vault = daemon_and_vault
    _write(vault, "person/Ben McMillan.md", "person", body="Ben McMillan, music pro.\n")
    _write(
        vault, "event/concert.md", "event",
        body="Ben McMillan headlined the show tonight.\n",
    )
    daemon.state.update_file("person/Ben McMillan.md", "h1")
    daemon.state.update_file("event/concert.md", "h2")

    records = {
        "person/Ben McMillan.md": _record(
            "person/Ben McMillan.md", "person",
            body="Ben McMillan, music pro.",
        ),
        "event/concert.md": _record(
            "event/concert.md", "event",
            body="Ben McMillan headlined the show tonight.",
        ),
    }
    v = _normalise([1.0, 0.0])
    paths = ["person/Ben McMillan.md", "event/concert.md"]
    vectors = np.asarray([v, v], dtype=np.float32)
    cluster_members = {0: paths[:]}

    daemon._link_entities_in_clusters(
        changed_cluster_ids={0},
        cluster_members=cluster_members,
        records=records,
        all_paths=paths,
        all_vectors=vectors,
    )

    md = frontmatter.load(vault / "event/concert.md").metadata
    assert md.get("related_persons") == ["person/Ben McMillan.md"]


def test_cluster_stage_allows_when_anchor_disabled(daemon_and_vault, tmp_path):
    """``require_text_anchor=False`` preserves the legacy cosine-only
    contract — link lands even without textual presence."""
    daemon, vault = daemon_and_vault
    daemon.cfg = _make_config(
        vault, tmp_path / "state_disabled.json",
        require_text_anchor=False,
    )
    _write(vault, "person/Ben McMillan.md", "person")
    _write(vault, "event/concert.md", "event", body="Music event.\n")
    daemon.state.update_file("person/Ben McMillan.md", "h1")
    daemon.state.update_file("event/concert.md", "h2")

    records = {
        "person/Ben McMillan.md": _record("person/Ben McMillan.md", "person"),
        "event/concert.md": _record(
            "event/concert.md", "event",
            body="Music event.",
        ),
    }
    v = _normalise([1.0, 0.0])
    paths = ["person/Ben McMillan.md", "event/concert.md"]
    vectors = np.asarray([v, v], dtype=np.float32)
    cluster_members = {0: paths[:]}

    daemon._link_entities_in_clusters(
        changed_cluster_ids={0},
        cluster_members=cluster_members,
        records=records,
        all_paths=paths,
        all_vectors=vectors,
    )
    md = frontmatter.load(vault / "event/concert.md").metadata
    # Gate disabled → link lands despite absent name.
    assert md.get("related_persons") == ["person/Ben McMillan.md"]


def test_cluster_stage_partial_block_writes_remainder(daemon_and_vault):
    """Two entities in the cluster: one mentioned in body, one not.
    Mentioned one gets written; absent one gets blocked. Single
    write call, only the legitimate entity in the result."""
    daemon, vault = daemon_and_vault
    _write(vault, "person/Mentioned.md", "person")
    _write(vault, "person/Absent.md", "person")
    _write(
        vault, "event/concert.md", "event",
        body="Mentioned showed up; nobody else.\n",
    )
    for p in ["person/Mentioned.md", "person/Absent.md", "event/concert.md"]:
        daemon.state.update_file(p, "h")

    records = {
        "person/Mentioned.md": _record("person/Mentioned.md", "person"),
        "person/Absent.md": _record("person/Absent.md", "person"),
        "event/concert.md": _record(
            "event/concert.md", "event",
            body="Mentioned showed up; nobody else.",
        ),
    }
    v = _normalise([1.0, 0.0])
    paths = ["person/Mentioned.md", "person/Absent.md", "event/concert.md"]
    vectors = np.asarray([v, v, v], dtype=np.float32)
    cluster_members = {0: paths[:]}

    daemon._link_entities_in_clusters(
        changed_cluster_ids={0},
        cluster_members=cluster_members,
        records=records,
        all_paths=paths,
        all_vectors=vectors,
    )
    md = frontmatter.load(vault / "event/concert.md").metadata
    # Only Mentioned lands.
    assert md.get("related_persons") == ["person/Mentioned.md"]


# ---------------------------------------------------------------------------
# Stage 2: _link_noise_points_to_entities
# ---------------------------------------------------------------------------


def test_noise_stage_blocks_link_when_name_absent(daemon_and_vault):
    """Same gate works for noise-point linking."""
    daemon, vault = daemon_and_vault
    _write(vault, "person/Ben McMillan.md", "person")
    _write(vault, "event/lonely.md", "event", body="A noise-point event with no name.\n")
    daemon.state.update_file("person/Ben McMillan.md", "h1")
    daemon.state.update_file("event/lonely.md", "h2")

    records = {
        "person/Ben McMillan.md": _record("person/Ben McMillan.md", "person"),
        "event/lonely.md": _record(
            "event/lonely.md", "event",
            body="A noise-point event with no name.",
        ),
    }
    v = _normalise([1.0, 0.0])
    paths = ["person/Ben McMillan.md", "event/lonely.md"]
    vectors = np.asarray([v, v], dtype=np.float32)

    with capture_logs() as captured:
        daemon._link_noise_points_to_entities(
            noise_paths=["event/lonely.md"],
            records=records,
            all_paths=paths,
            all_vectors=vectors,
        )
    md = frontmatter.load(vault / "event/lonely.md").metadata
    assert "related_persons" not in md
    blocks = [
        c for c in captured
        if c.get("event") == "surveyor.entity_link_blocked_no_text_anchor"
        and c.get("stage") == "noise"
    ]
    assert len(blocks) == 1
    assert blocks[0]["record_path"] == "event/lonely.md"


def test_noise_stage_allows_link_when_name_present(daemon_and_vault):
    daemon, vault = daemon_and_vault
    _write(vault, "person/Ben McMillan.md", "person")
    _write(
        vault, "event/with-ben.md", "event",
        body="Ben McMillan was at this event.\n",
    )
    daemon.state.update_file("person/Ben McMillan.md", "h1")
    daemon.state.update_file("event/with-ben.md", "h2")

    records = {
        "person/Ben McMillan.md": _record("person/Ben McMillan.md", "person"),
        "event/with-ben.md": _record(
            "event/with-ben.md", "event",
            body="Ben McMillan was at this event.",
        ),
    }
    v = _normalise([1.0, 0.0])
    paths = ["person/Ben McMillan.md", "event/with-ben.md"]
    vectors = np.asarray([v, v], dtype=np.float32)

    daemon._link_noise_points_to_entities(
        noise_paths=["event/with-ben.md"],
        records=records,
        all_paths=paths,
        all_vectors=vectors,
    )
    md = frontmatter.load(vault / "event/with-ben.md").metadata
    assert md.get("related_persons") == ["person/Ben McMillan.md"]


# ---------------------------------------------------------------------------
# Stage 3: _backfill_new_entities
# ---------------------------------------------------------------------------


def test_backfill_blocks_link_when_name_absent(daemon_and_vault):
    """New entity created → backfill scan; records that don't mention
    the entity name don't get the link."""
    daemon, vault = daemon_and_vault
    _write(vault, "person/Ben McMillan.md", "person")
    _write(vault, "event/quiet.md", "event", body="Topic-similar but silent on names.\n")
    _write(vault, "event/loud.md", "event", body="Ben McMillan ran the show.\n")
    for p in ["person/Ben McMillan.md", "event/quiet.md", "event/loud.md"]:
        daemon.state.update_file(p, "h")

    records = {
        "person/Ben McMillan.md": _record("person/Ben McMillan.md", "person"),
        "event/quiet.md": _record(
            "event/quiet.md", "event",
            body="Topic-similar but silent on names.",
        ),
        "event/loud.md": _record(
            "event/loud.md", "event",
            body="Ben McMillan ran the show.",
        ),
    }
    v = _normalise([1.0, 0.0])
    paths = ["person/Ben McMillan.md", "event/quiet.md", "event/loud.md"]
    vectors = np.asarray([v, v, v], dtype=np.float32)

    with capture_logs() as captured:
        daemon._backfill_new_entities(
            new_entity_paths=["person/Ben McMillan.md"],
            records=records,
            all_paths=paths,
            all_vectors=vectors,
        )

    quiet_md = frontmatter.load(vault / "event/quiet.md").metadata
    loud_md = frontmatter.load(vault / "event/loud.md").metadata
    # Quiet: body silent → blocked.
    assert "related_persons" not in quiet_md
    # Loud: body mentions Ben McMillan → linked.
    assert loud_md.get("related_persons") == ["person/Ben McMillan.md"]

    # Block log fired exactly once (for quiet.md).
    blocks = [
        c for c in captured
        if c.get("event") == "surveyor.entity_link_blocked_no_text_anchor"
        and c.get("stage") == "backfill"
    ]
    assert len(blocks) == 1
    assert blocks[0]["record_path"] == "event/quiet.md"
    assert blocks[0]["entity_path"] == "person/Ben McMillan.md"


def test_backfill_blocks_first_name_only(daemon_and_vault):
    """Backfill of person/Ben McMillan.md must NOT match a record
    that mentions only 'Ben' (first-name-only). Same word-boundary
    strictness the cleanup CLI uses."""
    daemon, vault = daemon_and_vault
    _write(vault, "person/Ben McMillan.md", "person")
    _write(
        vault, "event/firstname-only.md", "event",
        body="Ben said hello at the meetup.\n",
    )
    for p in ["person/Ben McMillan.md", "event/firstname-only.md"]:
        daemon.state.update_file(p, "h")

    records = {
        "person/Ben McMillan.md": _record("person/Ben McMillan.md", "person"),
        "event/firstname-only.md": _record(
            "event/firstname-only.md", "event",
            body="Ben said hello at the meetup.",
        ),
    }
    v = _normalise([1.0, 0.0])
    paths = ["person/Ben McMillan.md", "event/firstname-only.md"]
    vectors = np.asarray([v, v], dtype=np.float32)

    daemon._backfill_new_entities(
        new_entity_paths=["person/Ben McMillan.md"],
        records=records,
        all_paths=paths,
        all_vectors=vectors,
    )
    md = frontmatter.load(vault / "event/firstname-only.md").metadata
    # No full-name match → blocked.
    assert "related_persons" not in md


def test_backfill_allows_when_anchor_disabled(daemon_and_vault, tmp_path):
    """Opt-out preserves legacy cosine-only contract for backfill too."""
    daemon, vault = daemon_and_vault
    daemon.cfg = _make_config(
        vault, tmp_path / "state_disabled_bf.json",
        require_text_anchor=False,
    )
    _write(vault, "person/Ben McMillan.md", "person")
    _write(vault, "event/quiet.md", "event", body="No name mentions.\n")
    for p in ["person/Ben McMillan.md", "event/quiet.md"]:
        daemon.state.update_file(p, "h")

    records = {
        "person/Ben McMillan.md": _record("person/Ben McMillan.md", "person"),
        "event/quiet.md": _record(
            "event/quiet.md", "event",
            body="No name mentions.",
        ),
    }
    v = _normalise([1.0, 0.0])
    paths = ["person/Ben McMillan.md", "event/quiet.md"]
    vectors = np.asarray([v, v], dtype=np.float32)

    daemon._backfill_new_entities(
        new_entity_paths=["person/Ben McMillan.md"],
        records=records,
        all_paths=paths,
        all_vectors=vectors,
    )
    md = frontmatter.load(vault / "event/quiet.md").metadata
    # Gate disabled → link lands.
    assert md.get("related_persons") == ["person/Ben McMillan.md"]


# ---------------------------------------------------------------------------
# Block log diagnostic-fields contract
# ---------------------------------------------------------------------------


def test_block_log_carries_full_diagnostic_fields(daemon_and_vault):
    """Operator should be able to grep the block log + reconstruct
    why each block fired (which cluster, which similarity score,
    what threshold was active)."""
    daemon, vault = daemon_and_vault
    _write(vault, "person/Ben McMillan.md", "person")
    _write(vault, "event/concert.md", "event", body="No name.\n")
    daemon.state.update_file("person/Ben McMillan.md", "h1")
    daemon.state.update_file("event/concert.md", "h2")

    records = {
        "person/Ben McMillan.md": _record("person/Ben McMillan.md", "person"),
        "event/concert.md": _record(
            "event/concert.md", "event", body="No name.",
        ),
    }
    v = _normalise([1.0, 0.0])
    paths = ["person/Ben McMillan.md", "event/concert.md"]
    vectors = np.asarray([v, v], dtype=np.float32)

    with capture_logs() as captured:
        daemon._link_entities_in_clusters(
            changed_cluster_ids={42},
            cluster_members={42: paths[:]},
            records=records,
            all_paths=paths,
            all_vectors=vectors,
        )
    block = next(
        c for c in captured
        if c.get("event") == "surveyor.entity_link_blocked_no_text_anchor"
    )
    # Every documented field present.
    assert "record_path" in block
    assert "entity_path" in block
    assert "similarity" in block
    assert "threshold" in block
    assert "stage" in block
    # cluster_id only on cluster stage.
    assert block["stage"] == "cluster"
    assert block["cluster_id"] == 42
    # similarity is rounded to 4 decimals (no float-noise in log).
    assert isinstance(block["similarity"], float)
    assert block["similarity"] == round(block["similarity"], 4)
