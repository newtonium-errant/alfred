"""Tests for wk3 commit 4 — distiller calibration exclusion.

The distiller must strip Alfred's own marker-fenced blocks
(``ALFRED:DYNAMIC``, ``ALFRED:CALIBRATION``) before extraction, alongside
the pre-existing ``KEN:DYNAMIC`` pattern. Both the parsed body and the
``stripped_body_length`` helper must honour the strip — divergence would
silently re-feed Alfred's output into its own learning loop.
"""

from __future__ import annotations

from pathlib import Path

from alfred.distiller import parser


# --- _strip_excluded_blocks ------------------------------------------------


def test_strip_excluded_blocks_removes_ken_dynamic() -> None:
    text = "before\n<!-- KEN:DYNAMIC -->leak<!-- END KEN:DYNAMIC -->\nafter"
    out = parser._strip_excluded_blocks(text)
    assert "leak" not in out
    assert "before" in out and "after" in out


def test_strip_excluded_blocks_removes_alfred_dynamic() -> None:
    text = (
        "before\n<!-- ALFRED:DYNAMIC -->Alfred brief\n"
        "- bullet<!-- END ALFRED:DYNAMIC -->\nafter"
    )
    out = parser._strip_excluded_blocks(text)
    assert "Alfred brief" not in out
    assert "bullet" not in out
    assert "before" in out and "after" in out


def test_strip_excluded_blocks_removes_alfred_calibration() -> None:
    text = (
        "prefix\n<!-- ALFRED:CALIBRATION -->\n## Style\n- terse\n"
        "<!-- END ALFRED:CALIBRATION -->\nsuffix"
    )
    out = parser._strip_excluded_blocks(text)
    assert "Style" not in out
    assert "terse" not in out
    assert "prefix" in out and "suffix" in out


def test_strip_excluded_blocks_handles_all_three_in_one_body() -> None:
    """One body with all three marker types — every block leaves."""
    text = (
        "A\n<!-- KEN:DYNAMIC -->ken<!-- END KEN:DYNAMIC -->\n"
        "B\n<!-- ALFRED:DYNAMIC -->dyn<!-- END ALFRED:DYNAMIC -->\n"
        "C\n<!-- ALFRED:CALIBRATION -->cal<!-- END ALFRED:CALIBRATION -->\n"
        "D"
    )
    out = parser._strip_excluded_blocks(text)
    for leaked in ("ken", "dyn", "cal"):
        assert leaked not in out
    for preserved in ("A", "B", "C", "D"):
        assert preserved in out


# --- parse_file -----------------------------------------------------------


def _write_record(tmp_path: Path, rel: str, raw: str) -> None:
    full = tmp_path / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(raw, encoding="utf-8")


def test_parse_file_strips_alfred_calibration_from_body(tmp_path: Path) -> None:
    raw = (
        "---\ntype: person\nname: Andrew\n---\n\n"
        "# Andrew\n\n"
        "<!-- ALFRED:CALIBRATION -->\n"
        "## Style\n- terse\n"
        "<!-- END ALFRED:CALIBRATION -->\n\n"
        "Real body content here.\n"
    )
    _write_record(tmp_path, "person/Andrew.md", raw)
    rec = parser.parse_file(tmp_path, "person/Andrew.md")
    assert rec.record_type == "person"
    assert "Real body content here" in rec.body
    assert "terse" not in rec.body
    assert "Style" not in rec.body
    # The pre-strip headings ("# Andrew") still ride along — only the
    # fenced block is targeted, not every heading.
    assert "# Andrew" in rec.body


def test_parse_file_strips_alfred_dynamic_from_body(tmp_path: Path) -> None:
    raw = (
        "---\ntype: project\nname: X\n---\n\n"
        "Intro.\n\n"
        "<!-- ALFRED:DYNAMIC -->\nMachine-generated brief.\n"
        "<!-- END ALFRED:DYNAMIC -->\n\nOutro.\n"
    )
    _write_record(tmp_path, "project/X.md", raw)
    rec = parser.parse_file(tmp_path, "project/X.md")
    assert "Machine-generated brief" not in rec.body
    assert "Intro." in rec.body
    assert "Outro." in rec.body


def test_parse_file_leaves_frontmatter_and_wikilinks_intact(tmp_path: Path) -> None:
    """Stripping body blocks mustn't disturb frontmatter or wikilink extraction.

    Wikilinks are extracted from the raw text (pre-strip) to capture
    links in the excluded block — the semantic intent is "this record
    references these targets", which holds regardless of whether the
    link came from a dynamic or static section. Locking that behaviour
    here so a future refactor doesn't silently drop wikilinks inside
    calibration blocks.
    """
    raw = (
        "---\ntype: person\nname: X\nrelated:\n- '[[project/A]]'\n---\n\n"
        "<!-- ALFRED:CALIBRATION -->\n"
        "linked: [[project/B]]\n"
        "<!-- END ALFRED:CALIBRATION -->\n"
    )
    _write_record(tmp_path, "person/X.md", raw)
    rec = parser.parse_file(tmp_path, "person/X.md")
    assert rec.frontmatter.get("name") == "X"
    # Body is stripped of the calibration content.
    assert "linked" not in rec.body
    # Wikilinks from the raw file still surface (project/A from fm,
    # project/B from inside the calibration block).
    assert "project/A" in rec.wikilinks
    assert "project/B" in rec.wikilinks


# --- stripped_body_length -------------------------------------------------


def test_stripped_body_length_excludes_alfred_calibration_from_count() -> None:
    """A record whose body is *only* a calibration block reads as empty."""
    body = (
        "<!-- ALFRED:CALIBRATION -->\n"
        "## Style\n- a very long list of bullets that would otherwise\n"
        "- count as meaningful body content\n"
        "<!-- END ALFRED:CALIBRATION -->\n"
    )
    assert parser.stripped_body_length(body) == 0


def test_stripped_body_length_excludes_alfred_dynamic_from_count() -> None:
    body = (
        "<!-- ALFRED:DYNAMIC -->\n"
        "lots of dynamic text that shouldn't count\n"
        "<!-- END ALFRED:DYNAMIC -->\n"
    )
    assert parser.stripped_body_length(body) == 0


def test_stripped_body_length_counts_real_content_beside_alfred_block() -> None:
    """Real body + calibration block → length reflects only the real body."""
    body = (
        "Real content line.\n"
        "<!-- ALFRED:CALIBRATION -->\nnoise\n<!-- END ALFRED:CALIBRATION -->\n"
    )
    # "Real content line." is 18 chars.
    assert parser.stripped_body_length(body) == len("Real content line.")


def test_stripped_body_length_still_strips_ken_dynamic() -> None:
    """Regression: the KEN:DYNAMIC strip kept working after the refactor."""
    body = "<!-- KEN:DYNAMIC -->ken noise<!-- END KEN:DYNAMIC -->"
    assert parser.stripped_body_length(body) == 0
