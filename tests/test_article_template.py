"""Template tests for article (operator-template #1, 2026-05-17).

Activates the ``article`` type — Andrew's published-writing record
shape (Substack / Andrew Errant / future venues). Distinct from the
existing ``essay`` type which is for source essays Andrew READS
(routed to ``document/essay/`` and paired with the /train workflow).

Pins:
  * ``_templates/article.md`` exists in the bundled scaffold.
  * Frontmatter parses cleanly via python-frontmatter (no YAML
    ConstructorError — the b92c982 lesson: ``{{title}}`` /
    ``{{date}}`` MUST be quoted).
  * Frontmatter shape per the brief: type / name / subtitle /
    created / status / published_url / built_from / mocs / tags.
  * Body contains all 4 Part headers (Hot Take / Story / Takeaway /
    CTA) + the 4 inter-part dividers + ``# External References``.
  * Section-guidance parentheticals preserved verbatim
    (``(Counter intuitive take)``, etc.).
  * Substack-export instruction ``(no headline, no divider ^)``
    preserved verbatim on Part 4.
  * Part 1 sentence/paragraph count guidance (``1``, ``3``, ``1``)
    preserved.
  * Status values exactly ``{draft, scheduled, published, archived}``.
  * Schema registration: ``article`` in KNOWN_TYPES_HYPATIA +
    HYPATIA_CREATE_TYPES + TYPE_DIRECTORY.
  * ``built_from`` accepts list of strings (wikilinks).
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from alfred._data import get_scaffold_dir
from alfred.vault import ops, schema, scope


# --- Helpers --------------------------------------------------------------


def _template_path() -> Path:
    return get_scaffold_dir() / "_templates" / "article.md"


def _parse_template() -> frontmatter.Post:
    return frontmatter.load(_template_path())


# --- File existence + frontmatter parse ----------------------------------


def test_article_template_exists() -> None:
    """The bundled template file is present."""
    assert _template_path().exists()


def test_article_frontmatter_parses_cleanly() -> None:
    """python-frontmatter parses the template without YAML error.

    Regression-pin for the b92c982 lesson: unquoted ``{{title}}`` /
    ``{{date}}`` placeholders break YAML parsing (double-braces are
    interpreted as flow-mapping syntax → ``ConstructorError: found
    unhashable key``). Template's placeholders MUST be quoted.
    """
    post = _parse_template()
    assert "type" in post.metadata


# --- Frontmatter shape ----------------------------------------------------


def test_article_frontmatter_shape() -> None:
    """Frontmatter carries the field set per the brief."""
    post = _parse_template()
    fm = post.metadata
    assert fm["type"] == "article"
    assert fm["name"] == "{{title}}"
    assert fm["created"] == "{{date}}"
    # Subtitle field — empty default; operator fills for Substack-
    # equivalent subtitle (Substack renders it under the title).
    assert "subtitle" in fm
    assert fm["subtitle"] == ""
    # Status defaults to ``draft`` — operator's natural starting state.
    assert fm["status"] == "draft"
    # Published URL — empty default; operator fills post-publish so the
    # vault record links to the live venue.
    assert "published_url" in fm
    assert fm["published_url"] == ""
    # ``built_from`` — list of [[zettel/Title]] wikilinks pointing at
    # the zettels the article was synthesized from. Preserves the
    # provenance chain (zettel → article).
    assert fm["built_from"] == []
    # mocs + tags — empty list defaults.
    assert fm["mocs"] == []
    assert fm["tags"] == []


def test_article_built_from_accepts_wikilink_strings(tmp_path: Path) -> None:
    """``built_from`` is list-shaped; populated values are wikilink
    strings pointing at zettel records. Sanity-check via a rendered
    fixture (the bundled template ships with [] default but the
    field's contract is list-of-strings)."""
    rendered = tmp_path / "article.md"
    rendered.write_text(
        "---\n"
        "type: article\n"
        'name: "Test Article"\n'
        'created: "2026-05-17"\n'
        "status: draft\n"
        "built_from:\n"
        '  - "[[zettel/On Vulnerability]]"\n'
        '  - "[[zettel/On Stoic Practice]]"\n'
        "mocs: []\n"
        "tags: []\n"
        "---\n\n# Body\n",
        encoding="utf-8",
    )
    post = frontmatter.load(rendered)
    bf = post["built_from"]
    assert isinstance(bf, list)
    assert "[[zettel/On Vulnerability]]" in bf
    assert "[[zettel/On Stoic Practice]]" in bf


def test_article_uses_canonical_placeholders() -> None:
    """The ``{{title}}`` and ``{{date}}`` substitution placeholders are
    present in the raw file text."""
    text = _template_path().read_text(encoding="utf-8")
    assert "{{title}}" in text
    assert "{{date}}" in text


# --- Status registration --------------------------------------------------


def test_article_status_set_registered() -> None:
    """STATUS_BY_TYPE carries the four lifecycle states exactly."""
    assert schema.STATUS_BY_TYPE["article"] == {
        "draft", "scheduled", "published", "archived",
    }


# --- Schema + scope registration -----------------------------------------


def test_article_registered_in_known_types_hypatia() -> None:
    assert "article" in schema.KNOWN_TYPES_HYPATIA


def test_article_registered_in_hypatia_create_types() -> None:
    assert "article" in scope.HYPATIA_CREATE_TYPES


def test_article_directory_routing() -> None:
    """``article`` routes to its own top-level directory (NOT to
    ``document/essay/`` like the ``essay`` type)."""
    assert schema.TYPE_DIRECTORY["article"] == "article"


def test_article_distinct_from_essay() -> None:
    """``article`` and ``essay`` are SEPARATE types with separate
    directories. article/ = essays Andrew writes; document/essay/ =
    source essays Andrew reads. Anti-regression for any future
    "consolidate the two" sweep that would collapse the distinction.
    """
    assert schema.TYPE_DIRECTORY["article"] == "article"
    assert schema.TYPE_DIRECTORY["essay"] == "document/essay"
    # Status sets are also distinct (essay has no ``scheduled`` state).
    assert "scheduled" in schema.STATUS_BY_TYPE["article"]
    assert "scheduled" not in schema.STATUS_BY_TYPE["essay"]


def test_article_NOT_in_canonical_known_types() -> None:
    """``article`` is Hypatia-only — must not leak into Salem's
    canonical KNOWN_TYPES."""
    assert "article" not in schema.KNOWN_TYPES


# --- Hypatia scope can create; other scopes cannot ----------------------


def test_hypatia_scope_can_create_article() -> None:
    """``hypatia`` scope's ``hypatia_types_only`` check admits article."""
    scope.check_scope(
        scope="hypatia", operation="create", record_type="article",
    )


def test_talker_scope_refuses_article() -> None:
    """Salem (talker) cannot create article records — Hypatia-only."""
    with pytest.raises(scope.ScopeError):
        scope.check_scope(
            scope="talker", operation="create", record_type="article",
        )


def test_kalle_scope_refuses_article() -> None:
    """KAL-LE cannot create article records — Hypatia-only."""
    with pytest.raises(scope.ScopeError):
        scope.check_scope(
            scope="kalle", operation="create", record_type="article",
        )


# --- End-to-end vault_create -----------------------------------------------


def test_vault_create_article_under_hypatia_lands_at_correct_path(
    tmp_path: Path,
) -> None:
    """vault_create with type=article + scope=hypatia writes a record
    at ``article/<name>.md``."""
    vault = tmp_path / "vault"
    (vault / "article").mkdir(parents=True)
    result = ops.vault_create(
        vault, "article", "Test Published Essay", scope="hypatia",
    )
    expected = "article/Test Published Essay.md"
    assert result["path"] == expected
    assert (vault / expected).exists()
    rec = ops.vault_read(vault, expected)
    assert rec["frontmatter"]["type"] == "article"


# --- Body — 4 Part headers ------------------------------------------------


_PART_HEADERS: tuple[str, ...] = (
    "# Part 1 Hot Take Headline",
    "# Part 2 Story Headline",
    "# Part 3 Takeaway Headline",
    "# Part 4 CTA",
)


@pytest.mark.parametrize("header", _PART_HEADERS)
def test_article_part_header_present(header: str) -> None:
    """Each of the 4 Part headers appears verbatim in the template body."""
    body = _parse_template().content
    assert header in body, f"Missing part header: {header!r}"


def test_article_external_references_section_present() -> None:
    """The ``# External References`` body section is preserved.

    Per Andrew's design: drop ``# Follow Up Questions`` and
    ``# Research Ideas`` from the article shape (elevate worthwhile
    ones to question/ / research-pointer/ records during drafting),
    BUT keep ``# External References`` — these are inline citations
    within the article body, not metadata.
    """
    body = _parse_template().content
    assert "# External References" in body


def test_article_dropped_zettel_sections_not_present() -> None:
    """``# Follow Up Questions`` and ``# Research Ideas`` are
    deliberately NOT in the article template (those belong on
    zettel/ records; for articles, operator elevates them to
    dedicated question/ / research-pointer/ records during
    drafting).
    """
    body = _parse_template().content
    assert "# Follow Up Questions" not in body
    assert "# Research Ideas" not in body


# --- Body — verbatim section-guidance parentheticals ---------------------


@pytest.mark.parametrize("hint", [
    "(Counter intuitive take)",
    "(Personal story and realization)",
    "(Takeaway for the reader)",
    "(no headline, no divider ^)",
])
def test_article_section_guidance_preserved(hint: str) -> None:
    """Operator-facing section guidance is preserved verbatim. These
    are deleted as the operator fills in — the template's job is to
    prime the operator's writing, not hide the cues."""
    body = _parse_template().content
    assert hint in body, f"Missing section guidance: {hint!r}"


def test_article_no_top_of_body_italic_title_placeholder() -> None:
    """Unlike fiction-structure (which has ``# *Title*`` at body top),
    article does NOT — the filename serves as the title. Anti-regression
    for any future "normalize all templates to have # *Title*" sweep.
    """
    body = _parse_template().content
    # Body starts with the first Part header (allowing whitespace).
    stripped = body.lstrip()
    assert stripped.startswith("# Part 1 Hot Take Headline"), (
        f"article body should start with Part 1 header, got: "
        f"{stripped[:80]!r}"
    )
    # And # *Title* italic placeholder is absent.
    assert "# *Title*" not in body


# --- Body — sentence/paragraph count guidance on Part 1 -----------------


def test_article_part1_count_guidance_lines() -> None:
    """Part 1 carries ``1``, ``3``, ``1`` lines (sentence-count
    guidance — operator fills prose to those counts: 1-sentence hook,
    3-paragraph thesis, 1-sentence transition).

    Andrew confirmed the semantics; the literals are operator-facing
    scaffolding preserved verbatim.
    """
    body = _parse_template().content
    # Locate the Part 1 section bounded by the next `---` divider.
    p1_start = body.find("# Part 1 Hot Take Headline")
    assert p1_start != -1, "Part 1 header missing"
    p1_end = body.find("---", p1_start)
    assert p1_end != -1, "no divider after Part 1"
    p1_section = body[p1_start:p1_end]
    # Each count line appears on its own (lines around it are blank /
    # other content).
    lines = [ln.strip() for ln in p1_section.splitlines()]
    # Count lines: exactly "1", "3", "1" in order.
    count_lines = [ln for ln in lines if ln in ("1", "3")]
    assert count_lines == ["1", "3", "1"], (
        f"Part 1 count guidance drift — expected ['1','3','1'], "
        f"got {count_lines}"
    )


# --- Body — divider structure --------------------------------------------


def test_article_body_has_four_dividers() -> None:
    """Body has exactly 4 ``---`` divider lines:
      1. After Part 1 (before Part 2)
      2. After Part 2 (before Part 3)
      3. After Part 3 (before Part 4)
      4. After Part 4 (before # External References)

    Note: total ``---`` lines in the FILE is 6 — 2 frontmatter
    delimiters + 4 body dividers. This test checks body-only.
    """
    body = _parse_template().content
    divider_count = sum(1 for ln in body.splitlines() if ln.strip() == "---")
    assert divider_count == 4, (
        f"expected 4 body dividers, got {divider_count}"
    )


def test_article_substack_export_instruction_on_part4() -> None:
    """The ``(no headline, no divider ^)`` line follows ``# Part 4 CTA``
    — it's an operator-instruction explaining the Substack-export
    behaviour: at export time, drop the Part 4 heading AND the
    divider above it (the ``^`` arrow points up at the divider/heading
    pair). Preserved verbatim so the operator sees the instruction
    when prepping the export.
    """
    body = _parse_template().content
    p4_idx = body.find("# Part 4 CTA")
    assert p4_idx != -1
    # The hint should appear within ~50 chars after the heading.
    after = body[p4_idx : p4_idx + 200]
    assert "(no headline, no divider ^)" in after, (
        f"Part 4 hint missing or misplaced — got: {after!r}"
    )


# --- Body — CTA Button/Link placeholder ----------------------------------


def test_article_cta_button_link_placeholder_present() -> None:
    """Part 4 body ends with ``CTA Button/Link`` — operator replaces
    with the actual button URL / inline link at publish-prep time."""
    body = _parse_template().content
    assert "CTA Button/Link" in body
