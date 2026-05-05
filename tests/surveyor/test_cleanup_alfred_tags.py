"""Phase 2 (tag side) — alfred_tags contamination cleanup tests.

Pairs with the per-record write-side gate shipped in `47b1b75` /
`004ac54` (tag-anchor predicate enforced at writer). This is the
historical-data scrub: walks the vault, finds records whose
``alfred_tags`` list contains tags whose anchor term has no textual
presence in the record's content, removes those tags. Architectural
twin to ``cleanup_entity_link_contamination``.

Coverage mirrors the link-side test file
(``test_cleanup_contamination.py``):

  Predicate decisions (per tag, per record):
    * Record with anchored tag → kept
    * Record with unanchored tag → removed
    * Record with mixed (some anchored, some not) → partial removal
    * Body-only / title-only / description-only / related-list /
      relationships-anchor presence — each surface preserves the tag
    * Hierarchical tag (events/music) anchor extraction — last
      slash-segment is the anchor term
    * Compound tag (live-music) anchor extraction — last dash-segment
    * Case-insensitive match

  Frontmatter shape edge cases:
    * Record without alfred_tags field → no-op, not counted in
      records_with_tags
    * Record with non-list alfred_tags (operator-malformed scalar) →
      no-op, not counted
    * All tags fail predicate → field preserved as empty list
      (not deleted)
    * Non-string entries in tags list (defensive) → kept verbatim

  Dry-run vs apply:
    * Dry-run produces accurate report, mutates nothing
    * Apply mutates only what dry-run reported (vault state matches)
    * Idempotency: second apply finds zero additional removals
    * Audit log: one JSONL line per modified file on apply

  Aggregate report shape:
    * records_scanned / records_with_tags / records_modified /
      tags_removed_total
    * per_record_modifications only contains modified records
    * Empty vault → all zeros, not a crash
    * Vault path missing → VaultError

  Per-record-failure isolation:
    * Malformed YAML on one record → recorded in failed_records,
      doesn't abort the bulk operation

  CLI integration:
    * ``alfred surveyor cleanup-alfred-tags`` parses
    * ``--apply`` flag toggles
    * Default is dry-run (apply=False)

Uses ``structlog.testing.capture_logs`` per
``feedback_structlog_assertion_patterns.md``.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from alfred.surveyor.cleanup import (
    TagCleanupRecord,
    TagCleanupReport,
    cleanup_alfred_tags_contamination,
)
from alfred.vault.ops import VaultError


# ---------------------------------------------------------------------------
# Helper: write a synthetic record
# ---------------------------------------------------------------------------


def _write_record(
    vault: Path,
    *,
    rel_path: str,
    body: str = "default body content",
    alfred_tags: list | None = None,
    title: str | None = None,
    description: str | None = None,
    summary: str | None = None,
    related: list[str] | None = None,
    relationships: list[dict] | None = None,
    extra_fm: dict | None = None,
) -> Path:
    """Write a synthetic markdown record. ``alfred_tags=None`` skips
    the field; ``alfred_tags=[]`` writes an empty list (different
    semantics — matches the operator-state distinctions the cleanup
    tracks)."""
    fm: dict = {
        "type": rel_path.split("/")[0] if "/" in rel_path else "note",
    }
    if alfred_tags is not None:
        fm["alfred_tags"] = alfred_tags
    if title is not None:
        fm["title"] = title
    if description is not None:
        fm["description"] = description
    if summary is not None:
        fm["summary"] = summary
    if related is not None:
        fm["related"] = related
    if relationships is not None:
        fm["relationships"] = relationships
    if extra_fm:
        fm.update(extra_fm)
    full_path = vault / rel_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(body, **fm)
    full_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return full_path


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Empty vault root."""
    return tmp_path


# ---------------------------------------------------------------------------
# Headline scenarios — predicate decisions
# ---------------------------------------------------------------------------


def test_unanchored_tag_removed(vault: Path):
    """Body says nothing about music → ``music`` tag has no anchor →
    removed."""
    _write_record(
        vault, rel_path="event/Coffee Meetup.md",
        body="Quiet morning at the cafe. Talked about gardening.",
        alfred_tags=["music", "events"],
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    assert report.records_modified == 1
    mod = report.per_record_modifications[0]
    # Both tags lack anchor — both removed.
    assert set(mod.tags_removed) == {"music", "events"}
    assert mod.tags_kept == []


def test_anchored_tag_kept(vault: Path):
    """Body mentions the anchor → tag preserved."""
    _write_record(
        vault, rel_path="event/Concert.md",
        body="Live music at the local pub. Great band, three encores.",
        alfred_tags=["music"],
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    # No removals — record not in per_record_modifications, but it WAS
    # scanned and counted as having tags.
    assert report.records_modified == 0
    assert report.records_scanned == 1
    assert report.records_with_tags == 1
    assert report.per_record_modifications == []


def test_mixed_anchored_and_unanchored_partial_removal(vault: Path):
    """Three tags, one anchored, two not → partial removal; field
    keeps the surviving tag."""
    _write_record(
        vault, rel_path="event/Mixed.md",
        body="Live music at the venue. Fantastic show.",
        alfred_tags=["music", "marketing", "events"],
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    assert report.records_modified == 1
    mod = report.per_record_modifications[0]
    assert mod.tags_kept == ["music"]
    assert set(mod.tags_removed) == {"marketing", "events"}


# ---------------------------------------------------------------------------
# Anchor surfaces — title / description / summary / related / relationships
# ---------------------------------------------------------------------------


def test_anchor_in_title_preserves_tag(vault: Path):
    _write_record(
        vault, rel_path="note/A.md",
        body="quiet body content",
        title="Music industry update",
        alfred_tags=["music"],
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    assert report.records_modified == 0


def test_anchor_in_description_preserves_tag(vault: Path):
    _write_record(
        vault, rel_path="note/B.md",
        body="quiet",
        description="Discussion of upcoming music events",
        alfred_tags=["music"],
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    assert report.records_modified == 0


def test_anchor_in_summary_preserves_tag(vault: Path):
    _write_record(
        vault, rel_path="note/C.md",
        body="quiet",
        summary="Music venue planning meeting",
        alfred_tags=["music"],
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    assert report.records_modified == 0


def test_anchor_in_related_wikilink_preserves_tag(vault: Path):
    _write_record(
        vault, rel_path="note/D.md",
        body="quiet",
        related=["[[org/Halifax Music Fest]]"],
        alfred_tags=["music"],
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    assert report.records_modified == 0


def test_anchor_in_relationships_anchor_preserves_tag(vault: Path):
    """LLM-emitted relationship target_anchor naming the term → preserves."""
    _write_record(
        vault, rel_path="note/E.md",
        body="quiet",
        relationships=[
            {
                "target": "org/Some Org",
                "type": "co_present",
                "context": "shared theme",
                "source_anchor": "music industry",
                "target_anchor": "music industry",
            },
        ],
        alfred_tags=["music"],
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    assert report.records_modified == 0


# ---------------------------------------------------------------------------
# Anchor extraction — hierarchical / compound tag shapes
# ---------------------------------------------------------------------------


def test_hierarchical_tag_anchor_is_last_slash_segment(vault: Path):
    """``events/music`` → anchor is ``music`` (rightmost segment)."""
    _write_record(
        vault, rel_path="note/H.md",
        body="Live music night at the venue.",
        alfred_tags=["events/music"],
    )
    # Body mentions "music" but not "events" — anchor is "music",
    # found, so the tag is preserved.
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    assert report.records_modified == 0


def test_compound_tag_anchor_is_last_dash_segment(vault: Path):
    """``live-music`` → anchor is ``music`` (rightmost dash segment)."""
    _write_record(
        vault, rel_path="note/C.md",
        body="Music at the festival was excellent.",
        alfred_tags=["live-music"],
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    assert report.records_modified == 0


def test_hierarchical_then_compound_anchor(vault: Path):
    """``events/live-music`` → split slash first (``live-music``),
    then dash (``music``). Body must mention ``music`` to preserve."""
    _write_record(
        vault, rel_path="note/HC.md",
        body="Live music at the local pub.",
        alfred_tags=["events/live-music"],
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    assert report.records_modified == 0


def test_case_insensitive_anchor_match(vault: Path):
    """Body says ``MUSIC`` — anchor ``music`` should still match."""
    _write_record(
        vault, rel_path="note/CI.md",
        body="Tonight: live MUSIC at 8pm.",
        alfred_tags=["music"],
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    assert report.records_modified == 0


# ---------------------------------------------------------------------------
# Frontmatter shape edge cases
# ---------------------------------------------------------------------------


def test_record_without_alfred_tags_is_skipped(vault: Path):
    """Records with no alfred_tags field don't count toward
    records_with_tags."""
    _write_record(
        vault, rel_path="note/Untagged.md",
        body="quiet",
        # alfred_tags=None → field omitted
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    assert report.records_scanned == 1
    assert report.records_with_tags == 0
    assert report.records_modified == 0


def test_non_list_alfred_tags_field_is_skipped(vault: Path):
    """Operator-malformed: ``alfred_tags: "music"`` (scalar string).
    Cleanup ignores — janitor's structural validation owns this."""
    _write_record(
        vault, rel_path="note/Mal.md",
        body="quiet",
        extra_fm={"alfred_tags": "music"},  # scalar, not list
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    assert report.records_scanned == 1
    assert report.records_with_tags == 0
    assert report.records_modified == 0


def test_all_tags_fail_predicate_field_kept_empty(vault: Path):
    """All tags unanchored → field preserved as empty list
    (``alfred_tags: []``), not removed entirely. Operator can grep
    for empty lists as a diagnostic signal."""
    _write_record(
        vault, rel_path="note/Empty.md",
        body="completely quiet body",
        alfred_tags=["music", "events", "marketing"],
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=False)
    assert report.records_modified == 1
    # File still exists; alfred_tags field is empty list, not deleted.
    post = frontmatter.load(str(vault / "note/Empty.md"))
    assert "alfred_tags" in post.metadata
    assert post.metadata["alfred_tags"] == []


def test_non_string_tag_entries_kept_verbatim(vault: Path):
    """Defensive: non-string entries (operator-malformed mid-list) are
    kept verbatim — cleanup doesn't validate types, janitor does.
    Conservative bias: don't lose data we can't reason about."""
    _write_record(
        vault, rel_path="note/NS.md",
        body="quiet",
        alfred_tags=["music", 42, {"shape": "weird"}],  # mixed types
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=False)
    # "music" failed anchor — body says nothing about music → removed.
    # 42 + dict are non-strings → kept verbatim.
    assert report.records_modified == 1
    post = frontmatter.load(str(vault / "note/NS.md"))
    kept = post.metadata["alfred_tags"]
    # The two non-string entries survived.
    assert 42 in kept
    assert {"shape": "weird"} in kept
    # The string "music" did not.
    assert "music" not in kept


# ---------------------------------------------------------------------------
# Dry-run vs apply
# ---------------------------------------------------------------------------


def test_dry_run_does_not_mutate_vault(vault: Path):
    """Dry-run produces accurate report without writing."""
    md_path = _write_record(
        vault, rel_path="event/X.md",
        body="quiet",
        alfred_tags=["music", "events"],
    )
    original_text = md_path.read_text(encoding="utf-8")
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    assert report.records_modified == 1
    # File on disk unchanged.
    assert md_path.read_text(encoding="utf-8") == original_text


def test_apply_mutates_only_what_dry_run_reported(vault: Path):
    """Apply produces same report shape as dry-run, AND the on-disk
    state matches — alfred_tags now contains only the kept tags."""
    _write_record(
        vault, rel_path="event/Y.md",
        body="Live music at the venue.",
        alfred_tags=["music", "marketing"],
    )
    dry = cleanup_alfred_tags_contamination(vault, dry_run=True)
    live = cleanup_alfred_tags_contamination(vault, dry_run=False)
    # Both reports show the same modification.
    assert dry.records_modified == live.records_modified == 1
    assert (
        dry.per_record_modifications[0].tags_removed
        == live.per_record_modifications[0].tags_removed
    )
    # On-disk state reflects the live run.
    post = frontmatter.load(str(vault / "event/Y.md"))
    assert post.metadata["alfred_tags"] == ["music"]


def test_apply_is_idempotent(vault: Path):
    """Second apply after first finds zero additional removals (the
    only tags left ARE anchored)."""
    _write_record(
        vault, rel_path="event/Z.md",
        body="Live music at the venue.",
        alfred_tags=["music", "marketing"],
    )
    cleanup_alfred_tags_contamination(vault, dry_run=False)
    second = cleanup_alfred_tags_contamination(vault, dry_run=False)
    assert second.records_modified == 0
    assert second.tags_removed_total == 0


# ---------------------------------------------------------------------------
# Audit log + per-record failure isolation
# ---------------------------------------------------------------------------


def test_audit_log_written_on_apply(vault: Path, tmp_path: Path):
    """One JSONL line per modified file. Detail names the removed tags."""
    _write_record(
        vault, rel_path="event/Quiet.md",
        body="quiet",
        alfred_tags=["music", "events"],
    )
    audit_log = tmp_path / "audit.log"
    cleanup_alfred_tags_contamination(
        vault, dry_run=False, audit_log_path=audit_log,
    )
    text = audit_log.read_text(encoding="utf-8")
    assert "surveyor-cleanup-tags" in text
    assert "event/Quiet.md" in text
    assert "removed unanchored alfred_tags" in text
    # At least one of the removed tags appears in the detail string.
    assert ("music" in text) or ("events" in text)


def test_malformed_frontmatter_recorded_in_failed_records(vault: Path):
    """A bad YAML record doesn't abort the bulk operation — it lands
    in failed_records and the walker continues."""
    # Malformed YAML — frontmatter loader raises.
    bad_path = vault / "note/Bad.md"
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    bad_path.write_text(
        "---\n"
        "alfred_tags: [music\n"  # unclosed list
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    # A good record.
    _write_record(
        vault, rel_path="note/Good.md",
        body="quiet",
        alfred_tags=["music"],
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    # Bad record lands in failed_records, with parse phase.
    assert len(report.failed_records) == 1
    assert report.failed_records[0]["phase"] == "parse"
    # Good record was still processed (counted).
    assert report.records_with_tags == 1


# ---------------------------------------------------------------------------
# Aggregate report shape
# ---------------------------------------------------------------------------


def test_empty_vault_returns_zero_counts(vault: Path):
    """Empty vault → all counters zero, no crash. Per
    ``feedback_intentionally_left_blank.md``: silence is ambiguous, so
    the report still rolls up explicit zeros + the
    ``surveyor.cleanup_tags.complete`` log fires unconditionally."""
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    assert report.records_scanned == 0
    assert report.records_with_tags == 0
    assert report.records_modified == 0
    assert report.tags_removed_total == 0
    assert report.per_record_modifications == []


def test_aggregate_counts_across_multiple_records(vault: Path):
    """Three records: one untagged, one all-anchored, one partial. Aggregates
    should reflect 3 / 2 / 1 / N removed."""
    _write_record(
        vault, rel_path="note/Untagged.md",
        body="quiet",
        # no alfred_tags
    )
    _write_record(
        vault, rel_path="note/AllKept.md",
        body="Live music at the venue.",
        alfred_tags=["music"],
    )
    _write_record(
        vault, rel_path="note/Partial.md",
        body="Live music at the venue.",
        alfred_tags=["music", "marketing", "events"],
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    assert report.records_scanned == 3
    assert report.records_with_tags == 2  # untagged record excluded
    assert report.records_modified == 1
    assert report.tags_removed_total == 2  # marketing + events
    # Only the modified record appears in per_record_modifications.
    assert len(report.per_record_modifications) == 1
    assert report.per_record_modifications[0].record_path == "note/Partial.md"


def test_vault_path_does_not_exist_raises(tmp_path: Path):
    nonexistent = tmp_path / "no_vault"
    with pytest.raises(VaultError, match="not a directory"):
        cleanup_alfred_tags_contamination(nonexistent, dry_run=True)


def test_skip_ignored_dirs(vault: Path):
    """Records under _templates / _bases / .obsidian aren't scanned."""
    _write_record(
        vault, rel_path="note/Real.md",
        body="quiet",
        alfred_tags=["music"],
    )
    _write_record(
        vault, rel_path="_templates/template.md",
        body="quiet",
        alfred_tags=["music"],
    )
    report = cleanup_alfred_tags_contamination(vault, dry_run=True)
    # Real record found and modified; template not scanned at all.
    paths_modified = {m.record_path for m in report.per_record_modifications}
    assert paths_modified == {"note/Real.md"}


# ---------------------------------------------------------------------------
# Top-level CLI parser
# ---------------------------------------------------------------------------


def test_cleanup_alfred_tags_subcommand_registered_with_apply_flag():
    from alfred.cli import build_parser
    parser = build_parser()
    args = parser.parse_args([
        "surveyor", "cleanup-alfred-tags",
        "--apply",
    ])
    assert args.command == "surveyor"
    assert args.surveyor_cmd == "cleanup-alfred-tags"
    assert args.apply is True


def test_cleanup_alfred_tags_subcommand_default_is_dry_run():
    from alfred.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["surveyor", "cleanup-alfred-tags"])
    assert args.apply is False


def test_cleanup_alfred_tags_subcommand_does_not_take_target_arg():
    """Tag-side cleanup is whole-vault — no target list. argparse
    should reject ``--target`` (the link-side arg) on this subcommand."""
    from alfred.cli import build_parser
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([
            "surveyor", "cleanup-alfred-tags",
            "--target", "person/X.md",
        ])


# ---------------------------------------------------------------------------
# Dataclass shape contract
# ---------------------------------------------------------------------------


def test_tag_cleanup_record_to_dict():
    rec = TagCleanupRecord(
        record_path="event/X.md",
        tags_removed=["music"],
        tags_kept=["events"],
    )
    assert rec.to_dict() == {
        "record_path": "event/X.md",
        "tags_removed": ["music"],
        "tags_kept": ["events"],
    }


def test_tag_cleanup_report_to_dict_round_trips_per_record():
    report = TagCleanupReport(
        vault_path="/v",
        dry_run=True,
        records_scanned=3,
        records_with_tags=2,
        records_modified=1,
        tags_removed_total=2,
        per_record_modifications=[
            TagCleanupRecord(
                record_path="event/X.md",
                tags_removed=["music"],
                tags_kept=["events"],
            ),
        ],
    )
    out = report.to_dict()
    assert out["vault_path"] == "/v"
    assert out["records_scanned"] == 3
    assert out["per_record_modifications"][0]["record_path"] == "event/X.md"
