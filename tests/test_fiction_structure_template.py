"""Template tests for fiction-structure (operator-template #2, 2026-05-16).

Activates the fiction posture for the ``fiction-structure`` type — the
type was registered in ``KNOWN_TYPES_HYPATIA`` during the 2026-04-30
Phase 2.5 ship but no template file shipped until now.

Pins:
  * ``_templates/fiction-structure.md`` exists in the bundled scaffold.
  * Frontmatter parses cleanly via python-frontmatter (no YAML
    ConstructorError — the lesson from Phase 1 commit b92c982:
    ``{{title}}`` / ``{{date}}`` MUST be quoted).
  * Frontmatter shape pins per the brief: type / name / created /
    story / status / current_chapter / tags / mocs.
  * Body contains all 8 Act headers + all 25 numbered beat headers
    (24 chapters + the optional epilogue beat #25).
  * Italic scaffold prompts preserved (operator-facing guidance text).
  * ``fiction-structure`` registered in STATUS_BY_TYPE with the four
    lifecycle states.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from alfred._data import get_scaffold_dir
from alfred.vault import schema


# --- Helpers --------------------------------------------------------------


def _template_path() -> Path:
    return get_scaffold_dir() / "_templates" / "fiction-structure.md"


def _parse_template() -> frontmatter.Post:
    return frontmatter.load(_template_path())


# --- File existence + frontmatter parse ----------------------------------


def test_fiction_structure_template_exists() -> None:
    """The bundled template file is present."""
    assert _template_path().exists()


def test_fiction_structure_frontmatter_parses_cleanly() -> None:
    """python-frontmatter parses the template without YAML error.

    Regression-pin for the Phase 1 b92c982 lesson: unquoted ``{{title}}``
    / ``{{date}}`` placeholders break YAML parsing (double-braces are
    interpreted as flow-mapping syntax → ``ConstructorError: found
    unhashable key``). The template's placeholders MUST be quoted.
    """
    post = _parse_template()
    assert "type" in post.metadata


# --- Frontmatter shape ----------------------------------------------------


def test_fiction_structure_frontmatter_shape() -> None:
    """Frontmatter carries the minimal field set per the brief."""
    post = _parse_template()
    fm = post.metadata
    assert fm["type"] == "fiction-structure"
    assert fm["name"] == "{{title}}"
    assert fm["created"] == "{{date}}"
    # Story pointer — empty string default; operator fills in with a
    # wikilink to the fiction-story record when one exists.
    assert "story" in fm
    assert fm["story"] == ""
    # Lifecycle defaults to ``outlining`` — first state per the spec.
    assert fm["status"] == "outlining"
    # current_chapter starts at 0 (no chapter drafted yet).
    assert fm["current_chapter"] == 0
    # Empty list defaults.
    assert fm["tags"] == []
    assert fm["mocs"] == []


def test_fiction_structure_uses_canonical_placeholders() -> None:
    """The ``{{title}}`` and ``{{date}}`` substitution placeholders are
    present in the raw file text."""
    text = _template_path().read_text(encoding="utf-8")
    assert "{{title}}" in text
    assert "{{date}}" in text


# --- Status registration --------------------------------------------------


def test_fiction_structure_status_set_registered() -> None:
    """STATUS_BY_TYPE carries the four lifecycle states."""
    assert schema.STATUS_BY_TYPE["fiction-structure"] == {
        "outlining", "drafting", "revising", "complete",
    }


# --- Body — Act headers (8 total) -----------------------------------------


_ACT_HEADERS: tuple[str, ...] = (
    # Act I (a + b)
    "## Act I.a Ordinary World",
    "## Act I.b Inciting Incident",
    # Act II (a + b + c + d) — note the uppercase ``ACT`` on II.c per
    # Andrew's verbatim source. Preservation is load-bearing (operator
    # convention; rewriting to mixed-case would be a silent edit).
    "## Act II.a First Plot Point (Point of No Return)",
    "## Act II.b 1st Pinch Point (First Battle)",
    "## ACT II.c Midpoint (Victim to Warrior)",
    "## Act II.d 2nd Pinch Point (Second Battle)",
    # Act III (a + b) — again, uppercase ``ACT`` on III.a.
    "## ACT III.a 2nd Plot Point (Dark Night of the Soul)",
    "## Act III.b Rebirth (Return to the Ordinary World)",
)


@pytest.mark.parametrize("header", _ACT_HEADERS)
def test_fiction_structure_act_header_present(header: str) -> None:
    """Each of the 8 Act headers appears verbatim in the template body."""
    body = _parse_template().content
    assert header in body, f"Missing act header: {header!r}"


def test_fiction_structure_preserves_uppercase_ACT_in_two_places() -> None:
    """Andrew's source has unusual capital ``ACT`` on II.c and III.a
    (the other six acts are mixed-case ``Act``). Preserve verbatim —
    a silent normalization would drift from operator convention.
    """
    body = _parse_template().content
    assert "## ACT II.c Midpoint" in body
    assert "## ACT III.a 2nd Plot Point" in body
    # The other six stay mixed-case.
    assert "## Act I.a Ordinary World" in body
    assert "## Act I.b Inciting Incident" in body
    assert "## Act II.a First Plot Point" in body
    assert "## Act II.b 1st Pinch Point" in body
    assert "## Act II.d 2nd Pinch Point" in body
    assert "## Act III.b Rebirth" in body


# --- Body — 25 numbered beat headers --------------------------------------


_BEAT_HEADERS: tuple[tuple[int, str], ...] = (
    (1,  "### 1: Really Bad Day"),
    (2,  "### 2: Something Peculiar"),
    (3,  "### 3: Grasping at Straws"),
    (4,  "### 4: Call to Adventure"),
    (5,  "### 5: Head in Sand"),
    (6,  "### 6: Pull out Rug"),
    (7,  "### 7: Enemies & Allies"),
    (8,  "### 8: Games & Trials"),
    (9,  "### 9: Earning Respect"),
    (10, "### 10: Forces of Evil"),
    (11, "### 11: Problem Revealed"),
    (12, "### 12: Discovery & Ultimatum"),
    (13, "### 13: Mirror Stage"),
    (14, "### 14: Plan of Attack"),
    (15, "### 15: Crucial Role"),
    (16, "### 16: Second Battle"),
    (17, "### 17: Surprise Failure"),
    (18, "### 18: Shocking Revelation"),
    (19, "### 19: Giving Up"),
    (20, "### 20: Pep Talk"),
    (21, "### 21: Seizing the Sword"),
    (22, "### 22: Ultimate Defeat"),
    (23, "### 23: Unexpected Victory"),
    (24, "### 24: Bittersweet Reflection"),
    (25, "### 25: Death of Self"),
)


@pytest.mark.parametrize("beat_num,header", _BEAT_HEADERS)
def test_fiction_structure_beat_header_present(
    beat_num: int, header: str,
) -> None:
    """Each of the 25 numbered beats has its header in the template body."""
    body = _parse_template().content
    assert header in body, f"Missing beat #{beat_num}: {header!r}"


def test_fiction_structure_beat_count_exact() -> None:
    """Body has exactly 25 numbered beat headers — no missing, no extra.

    Defensive pin: catches silent additions / removals if the body
    template gets restructured. Beats are ``### N:`` lines where N is
    an integer.
    """
    import re
    body = _parse_template().content
    # Match ``### <digits>:`` at line start.
    beat_lines = re.findall(r"^### (\d+):", body, re.MULTILINE)
    assert len(beat_lines) == 25, (
        f"expected 25 beats, found {len(beat_lines)}: {beat_lines}"
    )
    # Numbering is 1..25 in order.
    assert [int(n) for n in beat_lines] == list(range(1, 26))


# --- Body — italic scaffold prompts + chapter-title placeholders ---------


def test_fiction_structure_italic_beat_prompts_preserved() -> None:
    """Spot-check the italic scaffold prompts that prime operator
    fill-in. These are operator-facing guidance, deleted as the
    operator writes — preservation is load-bearing for the template
    to be useful at first creation.
    """
    body = _parse_template().content
    # First beat's italic prompt.
    assert (
        "*Ordinary world, empathy, conflict. Show flaw and lack. "
        "Want, Problem, Need.*"
    ) in body
    # Midpoint beat (13).
    assert "*Self-realization or a discovery. Victim to Warrior.*" in body
    # Beat 23 — pivotal "Unexpected Victory".
    assert (
        "*Secret weapon or ability, deep resolve, new understanding, "
        "unlikely ally. Remove glass shard. Sacrifice.*"
    ) in body
    # Final beat — explicit optional-epilogue scaffold.
    assert (
        "*From ambition to service. Death of former self. "
        "Acknowledgment ceremony.*"
    ) in body


def test_fiction_structure_chapter_title_placeholder_count() -> None:
    """Each of the 25 beats carries a ``#### "Chapter Title"`` placeholder
    (operator fills in with the actual chapter title)."""
    body = _parse_template().content
    chapter_title_count = body.count('#### "Chapter Title"')
    assert chapter_title_count == 25, (
        f"expected 25 chapter-title placeholders, found {chapter_title_count}"
    )


def test_fiction_structure_body_starts_with_italic_title() -> None:
    """The ``# *Title*`` top-of-body italic placeholder is preserved —
    operator's signal that the story title goes here (italicized
    by convention for in-text book titles)."""
    body = _parse_template().content
    # Allow whitespace before the first heading.
    assert body.lstrip().startswith("# *Title*"), (
        f"body should start with '# *Title*', got: {body[:80]!r}"
    )


def test_fiction_structure_epilogue_section_has_optional_marker() -> None:
    """Act III.b's optional-epilogue scaffold marker is present BOTH on
    the section header AND on the beat-25 line — Andrew's verbatim
    source repeats the marker.
    """
    body = _parse_template().content
    # The marker appears exactly twice (once under the section header,
    # once under beat 25).
    assert body.count(
        "*Optional: Hints of future challenges or antagonist lives.*"
    ) == 2
