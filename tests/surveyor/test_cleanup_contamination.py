"""Phase 2 contamination cleanup tests — body-text-anchor heuristic.

Pairs with Phase 1 attribution-logging (commit `96796d4` / `d0fd84c`).
Phase 2 is the bulk repair of existing 0.75-threshold contamination.

Coverage:

  Body-text-anchor heuristic:
    * Record with target in related_persons + body never mentions
      target → marked for removal
    * Record with target + body mentions target → preserved
    * Edge: body mentions "Ben" only (different person) — preserves
      "person/Ben.md", removes "person/Ben McMillan.md"
    * Mention in description / title / summary frontmatter →
      preserved
    * Mention in related (operator-curated wikilinks) → preserved
    * Mention in relationships array's source_anchor / target_anchor
      → preserved (LLM-emitted relationship anchor counts as
      textual presence)
    * Case-insensitive match (BEN MCMILLAN / ben mcmillan / Ben
      McMillan all preserve)
    * Records without the target field → no-op (not_present_in
      counter increments)

  Dry-run vs apply:
    * Dry-run produces accurate report, mutates nothing
    * Apply mutates only what dry-run reported (vault state matches
      report)
    * Idempotency: second run after first-apply finds zero
      additional removals

  Aggregate report shape:
    * total_removed / total_preserved / affected_record_count
    * per-target breakdown matches actual file changes

  Edge cases:
    * Target with type-prefix not in surveyor's surface (e.g.
      "event/X.md" → not in related_* schema) → skipped with
      structured warning, not crash
    * Malformed YAML frontmatter on one record → recorded in
      failed_records, doesn't abort the bulk operation
    * Vault path doesn't exist → VaultError

Uses ``structlog.testing.capture_logs`` per
``feedback_structlog_assertion_patterns.md``.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest
from structlog.testing import capture_logs

from alfred.surveyor.cleanup import (
    CleanupReport,
    TargetReport,
    _build_record_corpus,
    _display_name_from_path,
    _has_textual_presence,
    _infer_field_for_target,
    cleanup_entity_link_contamination,
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
    related_persons: list[str] | None = None,
    related_orgs: list[str] | None = None,
    related_matters: list[str] | None = None,
    related_projects: list[str] | None = None,
    title: str | None = None,
    description: str | None = None,
    summary: str | None = None,
    related: list[str] | None = None,
    relationships: list[dict] | None = None,
    extra_fm: dict | None = None,
) -> Path:
    """Write a synthetic markdown record with the given frontmatter shape."""
    fm: dict = {
        "type": rel_path.split("/")[0] if "/" in rel_path else "note",
    }
    if related_persons is not None:
        fm["related_persons"] = related_persons
    if related_orgs is not None:
        fm["related_orgs"] = related_orgs
    if related_matters is not None:
        fm["related_matters"] = related_matters
    if related_projects is not None:
        fm["related_projects"] = related_projects
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
# Pure-function unit tests
# ---------------------------------------------------------------------------


def test_display_name_from_path_strips_dir_and_extension():
    assert _display_name_from_path("person/Ben McMillan.md") == "Ben McMillan"
    assert _display_name_from_path("org/Halifax Music Fest.md") == "Halifax Music Fest"
    assert _display_name_from_path("matter/erste.md") == "erste"


def test_infer_field_for_target():
    assert _infer_field_for_target("person/X.md") == "related_persons"
    assert _infer_field_for_target("org/X.md") == "related_orgs"
    assert _infer_field_for_target("matter/X.md") == "related_matters"
    assert _infer_field_for_target("project/X.md") == "related_projects"
    # Unknown type → None (caller handles).
    assert _infer_field_for_target("event/X.md") is None
    assert _infer_field_for_target("note/X.md") is None
    # Empty / malformed → None.
    assert _infer_field_for_target("") is None


def test_has_textual_presence_word_boundary():
    """Exact word-boundary match — neither prefix nor substring fakes."""
    # Positive: full name appears as a phrase.
    assert _has_textual_presence(
        "I talked to Ben McMillan about the project.",
        "Ben McMillan",
    )
    # Positive: case-insensitive.
    assert _has_textual_presence(
        "Notes from BEN MCMILLAN's talk.", "Ben McMillan",
    )
    # Positive: at start / end of string.
    assert _has_textual_presence("Ben McMillan said hi.", "Ben McMillan")
    assert _has_textual_presence("Hi to Ben McMillan", "Ben McMillan")

    # Negative: only first name appears (different person).
    assert not _has_textual_presence(
        "Ben said hi.", "Ben McMillan",
    )
    # Negative: only surname.
    assert not _has_textual_presence(
        "McMillan family.", "Ben McMillan",
    )
    # Negative: empty corpus.
    assert not _has_textual_presence("", "Ben McMillan")
    # Negative: typo'd name.
    assert not _has_textual_presence(
        "Talked to Ben Macmilan today.", "Ben McMillan",
    )


def test_has_textual_presence_handles_punctuation_in_names():
    """Names with regex specials (parens / dots) get escaped properly."""
    assert _has_textual_presence(
        "U.S. Postal Service notes.", "U.S. Postal Service",
    )
    # Without escape, the . in "U.S." would match any char — but the
    # word boundaries on both sides still constrain enough that this
    # negative case correctly fails:
    assert not _has_textual_presence(
        "USA postal service notes.", "U.S. Postal Service",
    )


def test_build_record_corpus_includes_all_searchable_surfaces():
    fm = {
        "type": "note",
        "title": "Title here",
        "description": "Description here",
        "summary": "Summary here",
        "related": ["[[person/Andrew Newton]]", "[[org/RRTS]]"],
        "relationships": [
            {
                "target": "person/Jamie.md",
                "type": "related-to",
                "context": "shared at the marketing call",
                "source_anchor": "Jamie ran the call",
                "target_anchor": "Jamie facilitating",
                "confidence": 0.85,
            },
        ],
        # related_persons is INTENTIONALLY excluded from corpus so
        # the heuristic doesn't short-circuit to "always preserve."
        "related_persons": ["person/Should Not Appear In Corpus.md"],
    }
    body = "Body text here mentioning Foo Bar."
    corpus = _build_record_corpus(fm, body)
    assert "Title here" in corpus
    assert "Description here" in corpus
    assert "Summary here" in corpus
    assert "Andrew Newton" in corpus  # via the related list
    assert "RRTS" in corpus
    assert "Jamie" in corpus  # via relationships' anchor strings
    assert "Foo Bar" in corpus  # body
    # Critical: related_persons list NOT in corpus (would short-circuit).
    assert "Should Not Appear In Corpus" not in corpus


# ---------------------------------------------------------------------------
# Headline scenarios — body-text-anchor decisions
# ---------------------------------------------------------------------------


def test_removal_when_entity_has_no_textual_presence(vault: Path):
    """The QA-finding case: Ben McMillan in related_persons but body
    never mentions him → mark for removal."""
    _write_record(
        vault,
        rel_path="event/Random Event.md",
        body="Coffee meetup at the cafe. Discussed gardening tips.",
        related_persons=["person/Ben McMillan.md"],
    )
    report = cleanup_entity_link_contamination(
        vault, ["person/Ben McMillan.md"], dry_run=True,
    )
    assert len(report.targets) == 1
    target = report.targets[0]
    assert "event/Random Event.md" in target.removed_from
    assert target.preserved_in == []


def test_preservation_when_entity_mentioned_in_body(vault: Path):
    """Body mentions the entity → preserve the link."""
    _write_record(
        vault,
        rel_path="event/Concert.md",
        body="Ben McMillan played a great set tonight.",
        related_persons=["person/Ben McMillan.md"],
    )
    report = cleanup_entity_link_contamination(
        vault, ["person/Ben McMillan.md"], dry_run=True,
    )
    target = report.targets[0]
    assert "event/Concert.md" in target.preserved_in
    assert target.removed_from == []


def test_distinct_persons_first_name_vs_full_name(vault: Path):
    """Edge case from the spec: body mentions 'Ben' only. The
    'person/Ben.md' link is preserved (text match), the
    'person/Ben McMillan.md' link is removed (no full-name match)."""
    _write_record(
        vault,
        rel_path="event/Coffee Chat.md",
        body="Ben said the report looks great.",
        related_persons=["person/Ben.md", "person/Ben McMillan.md"],
    )
    report = cleanup_entity_link_contamination(
        vault,
        ["person/Ben.md", "person/Ben McMillan.md"],
        dry_run=True,
    )
    by_target = {t.target_path: t for t in report.targets}
    # "Ben" alone preserved — text match.
    assert "event/Coffee Chat.md" in by_target["person/Ben.md"].preserved_in
    # "Ben McMillan" removed — no full-name match.
    assert (
        "event/Coffee Chat.md"
        in by_target["person/Ben McMillan.md"].removed_from
    )


def test_preservation_via_description_field(vault: Path):
    """Mention in frontmatter description → preserve."""
    _write_record(
        vault,
        rel_path="project/Marketing Push.md",
        body="Generic project body.",
        description="Coordinated by Jamie and the marketing team.",
        related_persons=["person/Jamie.md"],
    )
    report = cleanup_entity_link_contamination(
        vault, ["person/Jamie.md"], dry_run=True,
    )
    target = report.targets[0]
    assert "project/Marketing Push.md" in target.preserved_in


def test_preservation_via_related_wikilink_list(vault: Path):
    """Operator-curated 'related' list mentions the entity → preserve."""
    _write_record(
        vault,
        rel_path="task/Follow Up.md",
        body="Generic task body.",
        related=["[[person/Jamie]]"],
        related_persons=["person/Jamie.md"],
    )
    report = cleanup_entity_link_contamination(
        vault, ["person/Jamie.md"], dry_run=True,
    )
    target = report.targets[0]
    assert "task/Follow Up.md" in target.preserved_in


def test_preservation_via_relationships_anchor(vault: Path):
    """LLM-emitted relationships array's anchor strings count as
    textual presence (the anchor BY DEFINITION names the shared
    entity)."""
    _write_record(
        vault,
        rel_path="event/Gala.md",
        body="The annual gala.",
        relationships=[
            {
                "target": "person/Jamie.md",
                "type": "related-to",
                "context": "Jamie hosted",
                "source_anchor": "the gala was hosted by Jamie",
                "target_anchor": "Jamie's hosting role",
                "confidence": 0.9,
            },
        ],
        related_persons=["person/Jamie.md"],
    )
    report = cleanup_entity_link_contamination(
        vault, ["person/Jamie.md"], dry_run=True,
    )
    assert "event/Gala.md" in report.targets[0].preserved_in


def test_case_insensitive_match(vault: Path):
    """BEN MCMILLAN / ben mcmillan / Ben McMillan all match (the
    LLM re-cases names in transcripts/summaries)."""
    _write_record(
        vault,
        rel_path="note/Transcript.md",
        body="ben mcmillan mentioned the budget.",
        related_persons=["person/Ben McMillan.md"],
    )
    report = cleanup_entity_link_contamination(
        vault, ["person/Ben McMillan.md"], dry_run=True,
    )
    assert "note/Transcript.md" in report.targets[0].preserved_in


def test_target_not_in_record_increments_not_present_counter(vault: Path):
    """Records without the target in related_* skip cleanly + bump
    the not_present_in counter."""
    _write_record(
        vault, rel_path="note/A.md",
        related_persons=["person/Other.md"],
    )
    _write_record(
        vault, rel_path="note/B.md",
        related_persons=["person/Other.md"],
    )
    report = cleanup_entity_link_contamination(
        vault, ["person/Ben McMillan.md"], dry_run=True,
    )
    assert report.targets[0].not_present_in == 2
    assert report.targets[0].removed_from == []
    assert report.targets[0].preserved_in == []


# ---------------------------------------------------------------------------
# Dry-run vs apply
# ---------------------------------------------------------------------------


def test_dry_run_does_not_mutate_vault(vault: Path):
    rec_path = _write_record(
        vault,
        rel_path="event/Quiet.md",
        body="No mention of the contaminator.",
        related_persons=["person/Ben McMillan.md"],
    )
    cleanup_entity_link_contamination(
        vault, ["person/Ben McMillan.md"], dry_run=True,
    )
    fm = frontmatter.load(str(rec_path))
    assert fm.metadata.get("related_persons") == ["person/Ben McMillan.md"]


def test_apply_mutates_only_what_dry_run_reported(vault: Path):
    """Dry-run report and apply-run report should have identical
    removed_from lists (deterministic)."""
    _write_record(
        vault,
        rel_path="event/Remove.md",
        body="Quiet body.",
        related_persons=["person/Ben McMillan.md"],
    )
    _write_record(
        vault,
        rel_path="event/Preserve.md",
        body="Ben McMillan was there.",
        related_persons=["person/Ben McMillan.md"],
    )

    dry = cleanup_entity_link_contamination(
        vault, ["person/Ben McMillan.md"], dry_run=True,
    )
    apply = cleanup_entity_link_contamination(
        vault, ["person/Ben McMillan.md"], dry_run=False,
    )
    # Same records affected.
    assert sorted(dry.targets[0].removed_from) == sorted(
        apply.targets[0].removed_from,
    )
    # Vault state matches: Remove.md no longer has Ben McMillan;
    # Preserve.md still does.
    remove_fm = frontmatter.load(str(vault / "event/Remove.md"))
    preserve_fm = frontmatter.load(str(vault / "event/Preserve.md"))
    assert remove_fm.metadata.get("related_persons", []) == []
    assert preserve_fm.metadata.get("related_persons") == ["person/Ben McMillan.md"]


def test_apply_is_idempotent(vault: Path):
    """Second apply-run finds zero additional removals — already cleaned."""
    _write_record(
        vault,
        rel_path="event/X.md",
        body="No mention.",
        related_persons=["person/Ben McMillan.md"],
    )
    first = cleanup_entity_link_contamination(
        vault, ["person/Ben McMillan.md"], dry_run=False,
    )
    assert len(first.targets[0].removed_from) == 1
    second = cleanup_entity_link_contamination(
        vault, ["person/Ben McMillan.md"], dry_run=False,
    )
    assert second.targets[0].removed_from == []
    assert second.targets[0].preserved_in == []
    # not_present_in: the one record now has no Ben McMillan in
    # related_persons (filtered out in the first run), so the second
    # run sees 1 record without the target.
    assert second.targets[0].not_present_in == 1


def test_multiple_targets_one_record_one_write(vault: Path):
    """Record carrying multiple contaminating targets gets ONE
    vault_edit per record (efficient — one open/close/write)."""
    _write_record(
        vault,
        rel_path="event/Multi.md",
        body="Just a coffee meetup, nothing notable.",
        related_persons=["person/Ben McMillan.md", "person/Jamie.md"],
        related_orgs=["org/TIXR.md"],
    )
    report = cleanup_entity_link_contamination(
        vault,
        ["person/Ben McMillan.md", "person/Jamie.md", "org/TIXR.md"],
        dry_run=False,
    )
    by_target = {t.target_path: t for t in report.targets}
    for t in [
        "person/Ben McMillan.md", "person/Jamie.md", "org/TIXR.md",
    ]:
        assert "event/Multi.md" in by_target[t].removed_from

    fm = frontmatter.load(str(vault / "event/Multi.md"))
    assert fm.metadata.get("related_persons", []) == []
    assert fm.metadata.get("related_orgs", []) == []


# ---------------------------------------------------------------------------
# Aggregate report shape
# ---------------------------------------------------------------------------


def test_aggregate_counts(vault: Path):
    _write_record(
        vault, rel_path="a/r1.md", body="quiet",
        related_persons=["person/Ben McMillan.md"],
    )
    _write_record(
        vault, rel_path="a/r2.md", body="quiet",
        related_persons=["person/Ben McMillan.md"],
    )
    _write_record(
        vault, rel_path="a/r3.md", body="Ben McMillan was here.",
        related_persons=["person/Ben McMillan.md"],
    )
    report = cleanup_entity_link_contamination(
        vault, ["person/Ben McMillan.md"], dry_run=True,
    )
    assert report.total_removed == 2
    assert report.total_preserved == 1
    assert report.affected_record_count == 2


def test_affected_record_count_dedupes_across_targets(vault: Path):
    """A record losing TWO different entries counts as ONE affected record."""
    _write_record(
        vault, rel_path="event/Multi.md", body="quiet",
        related_persons=["person/Ben McMillan.md", "person/Jamie.md"],
    )
    report = cleanup_entity_link_contamination(
        vault,
        ["person/Ben McMillan.md", "person/Jamie.md"],
        dry_run=True,
    )
    assert report.total_removed == 2
    assert report.affected_record_count == 1  # one record, two removals


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_target_with_unsupported_type_logs_warning_and_skips(vault: Path):
    """event/X.md has no surveyor-written related_* field; cleanup
    can't address tag-style contamination from this CLI. Log warning,
    skip the target, return a report with the other targets intact."""
    _write_record(
        vault, rel_path="note/A.md",
        related_persons=["person/Ben McMillan.md"],
        body="quiet",
    )
    with capture_logs() as captured:
        report = cleanup_entity_link_contamination(
            vault,
            ["event/Some Event.md", "person/Ben McMillan.md"],
            dry_run=True,
        )
    # Only the known-target appears in the report.
    assert len(report.targets) == 1
    assert report.targets[0].target_path == "person/Ben McMillan.md"
    # Warning log fired with the structured-fields contract.
    skip_logs = [
        c for c in captured
        if c.get("event") == "surveyor.cleanup.target_field_unknown"
    ]
    assert len(skip_logs) == 1
    assert skip_logs[0]["log_level"] == "warning"
    assert skip_logs[0]["target_path"] == "event/Some Event.md"


def test_malformed_frontmatter_recorded_in_failed_records(vault: Path):
    """One bad record doesn't abort the bulk operation."""
    # Good record.
    _write_record(
        vault, rel_path="note/Good.md",
        body="quiet",
        related_persons=["person/Ben McMillan.md"],
    )
    # Bad record — invalid YAML.
    bad = vault / "note" / "Bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(
        "---\n: : invalid yaml :: garbage\n  - mismatched: indentation\n---\n\nbody\n",
        encoding="utf-8",
    )
    report = cleanup_entity_link_contamination(
        vault, ["person/Ben McMillan.md"], dry_run=True,
    )
    # Good record still processed.
    assert "note/Good.md" in report.targets[0].removed_from
    # Bad record recorded as a failure.
    bad_paths = [f["path"] for f in report.failed_records]
    assert "note/Bad.md" in bad_paths


def test_vault_path_does_not_exist_raises(tmp_path: Path):
    nonexistent = tmp_path / "no_vault"
    with pytest.raises(VaultError, match="not a directory"):
        cleanup_entity_link_contamination(
            nonexistent, ["person/X.md"], dry_run=True,
        )


def test_audit_log_written_on_apply(vault: Path, tmp_path: Path):
    """Real-run mode emits one JSONL audit-log line per affected file."""
    _write_record(
        vault, rel_path="event/Quiet.md",
        body="No name.",
        related_persons=["person/Ben McMillan.md"],
    )
    audit_log = tmp_path / "audit.log"
    cleanup_entity_link_contamination(
        vault, ["person/Ben McMillan.md"], dry_run=False,
        audit_log_path=audit_log,
    )
    audit_text = audit_log.read_text(encoding="utf-8")
    assert "surveyor-cleanup" in audit_text
    assert "event/Quiet.md" in audit_text
    assert "removed contamination" in audit_text
    assert "person/Ben McMillan.md" in audit_text


def test_skip_ignored_dirs(vault: Path):
    """Records under _templates / _bases / .obsidian aren't scanned."""
    # Real record under the regular tree.
    _write_record(
        vault, rel_path="note/Real.md", body="quiet",
        related_persons=["person/Ben McMillan.md"],
    )
    # A template file with the same contamination — must NOT be touched.
    _write_record(
        vault, rel_path="_templates/template.md", body="quiet",
        related_persons=["person/Ben McMillan.md"],
    )
    report = cleanup_entity_link_contamination(
        vault, ["person/Ben McMillan.md"], dry_run=True,
    )
    assert "note/Real.md" in report.targets[0].removed_from
    # Template skipped — not in any list.
    assert "_templates/template.md" not in report.targets[0].removed_from
    assert "_templates/template.md" not in report.targets[0].preserved_in


# ---------------------------------------------------------------------------
# Top-level CLI parser
# ---------------------------------------------------------------------------


def test_cleanup_subcommand_registered_with_apply_flag():
    from alfred.cli import build_parser
    parser = build_parser()
    args = parser.parse_args([
        "surveyor", "cleanup-contamination",
        "--apply",
        "--target", "person/Ben McMillan.md",
        "--target", "org/TIXR.md",
    ])
    assert args.command == "surveyor"
    assert args.surveyor_cmd == "cleanup-contamination"
    assert args.apply is True
    assert args.target == ["person/Ben McMillan.md", "org/TIXR.md"]


def test_cleanup_subcommand_default_is_dry_run():
    from alfred.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["surveyor", "cleanup-contamination"])
    assert args.apply is False
    assert args.target is None
