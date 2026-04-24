"""Tests for the c3 retroactive-sweep CLI primitives.

The sweep walks ``vault/person/*.md`` (or explicit paths) and promotes
pre-existing ``_source:`` soft attributions on calibration bullets
into the BEGIN_INFERRED + attribution_audit contract. Defaults to
dry-run; ``--apply`` writes the markers.

Coverage:
    * Mixed-record scan: marks bullets with _source, leaves clean
      bullets alone, skips records already-marked.
    * --dry-run doesn't write.
    * Idempotent: a second --apply finds no new candidates.
    * Section title + agent + reason derived correctly from _source.
    * Empty paths fall through cleanly.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter

from alfred.audit.sweep import sweep_paths


def _write(tmp_path: Path, rel: str, content: str) -> Path:
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full


def _calibration_record(name: str, bullets: list[str]) -> str:
    """Compose a person record with a Workflow Preferences subsection."""
    body_lines = [f"# {name}", "", "<!-- ALFRED:CALIBRATION -->", "", "## Workflow Preferences", ""]
    body_lines.extend(bullets)
    body_lines.extend(["", "<!-- END ALFRED:CALIBRATION -->", ""])
    body = "\n".join(body_lines)
    return (
        "---\n"
        f"type: person\n"
        f"name: {name}\n"
        "created: '2026-04-23'\n"
        "---\n\n"
        f"{body}"
    )


def _read(path: Path) -> tuple[dict, str]:
    post = frontmatter.load(str(path))
    return dict(post.metadata), post.content


# --- Scan / mark ----------------------------------------------------------


def test_sweep_marks_source_annotated_bullets(tmp_path: Path):
    rec = _write(
        tmp_path,
        "person/Andrew.md",
        _calibration_record("Andrew", [
            "- Prefers terse replies. _source: session/Voice 2026-04-22 abc_",
            "- Uses kettlebells. _source: session/Voice 2026-04-20 def_",
        ]),
    )

    result = sweep_paths(tmp_path, ["person/Andrew.md"], apply=True)
    assert result.marked == 2
    assert result.skipped_no_source == 0
    assert result.errors == []

    fm, content = _read(rec)
    # Two BEGIN_INFERRED + END_INFERRED pairs.
    assert content.count("BEGIN_INFERRED") == 2
    assert content.count("END_INFERRED") == 2

    audit = fm.get("attribution_audit")
    assert isinstance(audit, list) and len(audit) == 2
    titles = {e["section_title"] for e in audit}
    assert titles == {"Workflow Preferences"}
    agents = {e["agent"] for e in audit}
    assert agents == {"salem"}
    reasons = {e["reason"] for e in audit}
    # Both bullets retain their source as the reason.
    assert any("Voice 2026-04-22 abc" in r for r in reasons)
    assert any("Voice 2026-04-20 def" in r for r in reasons)


def test_sweep_dry_run_does_not_write(tmp_path: Path):
    rec = _write(
        tmp_path,
        "person/Andrew.md",
        _calibration_record("Andrew", [
            "- Pref. _source: session/X_",
        ]),
    )
    pre_text = rec.read_text(encoding="utf-8")

    result = sweep_paths(tmp_path, ["person/Andrew.md"], apply=False)
    assert len(result.candidates) == 1
    assert result.marked == 0  # dry-run never marks

    post_text = rec.read_text(encoding="utf-8")
    assert pre_text == post_text


def test_sweep_idempotent_on_re_apply(tmp_path: Path):
    rec = _write(
        tmp_path,
        "person/Andrew.md",
        _calibration_record("Andrew", [
            "- Pref one. _source: session/A_",
        ]),
    )

    first = sweep_paths(tmp_path, ["person/Andrew.md"], apply=True)
    assert first.marked == 1

    second = sweep_paths(tmp_path, ["person/Andrew.md"], apply=True)
    # Re-run: bullet now sits inside an existing BEGIN_INFERRED span,
    # so it counts under skipped_already_marked, not marked.
    assert second.marked == 0
    assert second.skipped_already_marked >= 1
    # Frontmatter still has exactly one entry.
    fm, _ = _read(rec)
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list) and len(audit) == 1


def test_sweep_no_source_records_bucketed_separately(tmp_path: Path):
    _write(
        tmp_path,
        "person/Plain.md",
        _calibration_record("Plain", [
            "- A bullet with no _source annotation",
            "- Another plain bullet",
        ]),
    )
    result = sweep_paths(tmp_path, ["person/Plain.md"], apply=True)
    assert result.marked == 0
    assert result.skipped_no_source == 1
    assert result.candidates == []


def test_sweep_missing_file_recorded_as_error(tmp_path: Path):
    result = sweep_paths(tmp_path, ["person/Nope.md"], apply=True)
    assert result.errors
    assert result.errors[0][0] == "person/Nope.md"


def test_sweep_section_title_uses_nearest_heading(tmp_path: Path):
    """Bullets under different ## headings get distinct section_title."""
    rec_text = (
        "---\ntype: person\nname: A\ncreated: '2026-04-23'\n---\n\n"
        "## Workflow Preferences\n\n"
        "- Pref one. _source: session/A_\n\n"
        "## Current Priorities\n\n"
        "- Prio one. _source: session/B_\n"
    )
    _write(tmp_path, "person/A.md", rec_text)
    result = sweep_paths(tmp_path, ["person/A.md"], apply=True)
    assert result.marked == 2

    fm, _ = _read(tmp_path / "person/A.md")
    audit = fm.get("attribution_audit")
    titles = {e["section_title"] for e in audit}
    assert titles == {"Workflow Preferences", "Current Priorities"}


def test_sweep_summary_line_has_expected_shape(tmp_path: Path):
    _write(
        tmp_path,
        "person/Andrew.md",
        _calibration_record("Andrew", [
            "- A. _source: session/A_",
        ]),
    )
    result = sweep_paths(tmp_path, ["person/Andrew.md"], apply=True)
    line = result.summary_line()
    assert "marked=" in line
    assert "skipped_already_marked=" in line
    assert "skipped_no_source=" in line
    assert "errors=" in line
    assert "elapsed=" in line


def test_sweep_empty_paths_returns_clean_result(tmp_path: Path):
    result = sweep_paths(tmp_path, [], apply=True)
    assert result.marked == 0
    assert result.errors == []
    assert result.candidates == []
