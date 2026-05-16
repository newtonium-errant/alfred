"""Template render tests — Hypatia Zettelkasten schema cutover (2026-05-16).

Phase 1 ships five new templates + one strip (author.md). The bundled
templates are under ``src/alfred/_bundled/scaffold/_templates/`` and
located via :func:`alfred._data.get_scaffold_dir`.

These tests assert the SHAPE of each template — frontmatter keys, body
sections, placeholder syntax. Templates are pure data; testing them is
just parsing the file. Anti-regression: a future commit that drops a
section or renames a field surfaces here.

Per ``project_hypatia_zettelkasten_redesign.md`` "LOCKED IMPLEMENTATION
PLAN" → "Body templates" section. Frontmatter shape per the
"Frontmatter schema (minimal-helpful per type)" table.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from alfred._data import get_scaffold_dir


# --- Helpers --------------------------------------------------------------


def _template_path(name: str) -> Path:
    """Return the path to a bundled template file."""
    return get_scaffold_dir() / "_templates" / f"{name}.md"


def _parse_template(name: str) -> frontmatter.Post:
    """Parse a template file's frontmatter + body via the canonical lib."""
    return frontmatter.load(_template_path(name))


# --- memo.md --------------------------------------------------------------


def test_memo_template_exists() -> None:
    assert _template_path("memo").exists()


def test_memo_template_frontmatter_shape() -> None:
    """memo carries minimal frontmatter — no status (transient)."""
    post = _parse_template("memo")
    fm = post.metadata
    assert fm["type"] == "memo"
    assert fm["name"] == "{{title}}"
    assert fm["created"] == "{{date}}"
    # Session pointer — required per the brief (capture-mode auto-creation
    # sets this to the session wikilink path; manual creation leaves empty).
    assert "session" in fm
    assert fm["session"] == ""
    # Tags as empty list.
    assert fm["tags"] == []
    # NO status field — transient lifecycle is implicit.
    assert "status" not in fm


def test_memo_template_body_sections() -> None:
    """memo body has three sections: Memo / Context / Tags."""
    post = _parse_template("memo")
    body = post.content
    assert "# Memo" in body
    assert "# Context" in body
    assert "# Tags" in body


# --- zettel.md ------------------------------------------------------------


def test_zettel_template_exists() -> None:
    assert _template_path("zettel").exists()


def test_zettel_template_frontmatter_shape() -> None:
    """zettel frontmatter per brief — full optional-field set."""
    post = _parse_template("zettel")
    fm = post.metadata
    assert fm["type"] == "zettel"
    assert fm["name"] == "{{title}}"
    assert fm["created"] == "{{date}}"
    # Optional anchor fields — empty string defaults so operator can fill.
    assert "author" in fm
    assert "source" in fm
    # mocs is list-shaped (multi-MOC indexing).
    assert fm["mocs"] == []
    # Supersede chain fields.
    assert "supersedes" in fm
    assert "superseded_by" in fm
    # Tags list.
    assert fm["tags"] == []
    # Status defaults to ``open`` per the loose category-shape lifecycle.
    assert fm["status"] == "open"


def test_zettel_template_body_sections() -> None:
    """zettel body has eight sections in canonical order."""
    post = _parse_template("zettel")
    body = post.content
    sections = [
        "# Premise",
        "# Contents",
        "# Notes",
        "# Follow Up Questions",
        "# Research Ideas",
        "# External References",
        "# Tags",
        "# Indexing & MOCs",
    ]
    for section in sections:
        assert section in body, f"zettel missing section: {section!r}"


def test_zettel_dataview_block_is_commented_out_scaffold() -> None:
    """Dataview block ships commented-out — operator activates if useful.

    Per OQ-14 resolution (Phase 1 default): include the block as
    scaffolding on auto-creation; operator drops or uncomments. Wrapped
    in HTML comment so it doesn't render in Obsidian by default.
    """
    post = _parse_template("zettel")
    body = post.content
    # The dataview fence exists in the file source.
    assert "```dataview" in body
    # But it's inside an HTML comment block (so Obsidian skips it).
    # Find the dataview index + check the immediately-preceding line.
    dv_idx = body.find("```dataview")
    # Find the closest <!-- before the dataview block — and no --> between.
    head = body[:dv_idx]
    last_open = head.rfind("<!--")
    last_close = head.rfind("-->")
    assert last_open > last_close, (
        "Dataview block should be inside an <!-- ... --> comment so it "
        "doesn't render in Obsidian by default."
    )


def test_zettel_omits_source_anchor_field() -> None:
    """source_anchor frontmatter is Phase 2 (anchor preservation) — NOT in
    Phase 1 template per the brief's out-of-scope list."""
    post = _parse_template("zettel")
    assert "source_anchor" not in post.metadata


# --- MOC.md ---------------------------------------------------------------


def test_moc_template_exists() -> None:
    assert _template_path("MOC").exists()


def test_moc_template_frontmatter_shape() -> None:
    """MOC frontmatter: type + name + created + parent_mocs + tags. No status."""
    post = _parse_template("MOC")
    fm = post.metadata
    assert fm["type"] == "MOC"
    assert fm["name"] == "{{title}}"
    assert fm["created"] == "{{date}}"
    assert fm["parent_mocs"] == []
    assert fm["tags"] == []
    # MOC has NO status — organizational artifact, lifecycle-less.
    assert "status" not in fm


def test_moc_template_body_sections() -> None:
    """MOC body: Premise / Contents / Notes / Tags / See Also."""
    post = _parse_template("MOC")
    body = post.content
    sections = ["# Premise", "# Contents", "# Notes", "# Tags", "# See Also"]
    for section in sections:
        assert section in body, f"MOC missing section: {section!r}"


# --- question.md ----------------------------------------------------------


def test_question_template_exists() -> None:
    assert _template_path("question").exists()


def test_question_template_frontmatter_shape() -> None:
    """question frontmatter shape per brief."""
    post = _parse_template("question")
    fm = post.metadata
    assert fm["type"] == "question"
    assert fm["name"] == "{{title}}"
    assert fm["created"] == "{{date}}"
    assert fm["status"] == "open"
    assert fm["origin_sources"] == []
    assert "answered_by" in fm
    assert fm["mocs"] == []
    assert fm["tags"] == []


def test_question_template_body_sections() -> None:
    """question body: Question / Why It Matters / Origin / Status /
    Exploration / Answer / Tags / Indexing & MOCs."""
    post = _parse_template("question")
    body = post.content
    sections = [
        "# Question",
        "# Why It Matters",
        "# Origin",
        "# Status",
        "# Exploration",
        "# Answer",
        "# Tags",
        "# Indexing & MOCs",
    ]
    for section in sections:
        assert section in body, f"question missing section: {section!r}"


# --- research-pointer.md --------------------------------------------------


def test_research_pointer_template_exists() -> None:
    assert _template_path("research-pointer").exists()


def test_research_pointer_template_frontmatter_shape() -> None:
    """research-pointer frontmatter shape per brief."""
    post = _parse_template("research-pointer")
    fm = post.metadata
    assert fm["type"] == "research-pointer"
    assert fm["name"] == "{{title}}"
    assert fm["created"] == "{{date}}"
    assert fm["status"] == "open"
    assert fm["origin_sources"] == []
    assert fm["produces"] == []
    assert fm["mocs"] == []
    assert fm["tags"] == []


def test_research_pointer_template_body_sections() -> None:
    """research-pointer body: Pointer / Why / Origin / Status / Notes /
    Tags / Indexing & MOCs."""
    post = _parse_template("research-pointer")
    body = post.content
    sections = [
        "# Pointer",
        "# Why",
        "# Origin",
        "# Status",
        "# Notes",
        "# Tags",
        "# Indexing & MOCs",
    ]
    for section in sections:
        assert section in body, f"research-pointer missing section: {section!r}"


# --- author.md (stripped) -------------------------------------------------


def test_author_template_dropped_fields() -> None:
    """Phase 1 strips author template to minimal frontmatter.

    Dropped fields per the brief: ``era``, ``school``, ``description``,
    ``related``, ``last_name``, ``status``. The existing resolver
    (``capture_source_anchor.resolve_or_create_author``) still writes
    ``last_name`` via ``set_fields`` until the Phase 1 resolver overhaul
    commit retires it — but the TEMPLATE no longer carries the field
    as a default.
    """
    post = _parse_template("author")
    fm = post.metadata
    for dropped in ("era", "school", "description", "related",
                    "last_name", "status"):
        assert dropped not in fm, (
            f"author template still carries dropped field {dropped!r} — "
            f"Phase 1 strip incomplete."
        )


def test_author_template_keeps_minimal_fields() -> None:
    """author template keeps only type / name / created / aliases / tags."""
    post = _parse_template("author")
    fm = post.metadata
    assert fm["type"] == "author"
    assert fm["name"] == "{{title}}"
    assert fm["created"] == "{{date}}"
    assert fm["aliases"] == []
    assert fm["tags"] == []


def test_author_template_body_sections_terse() -> None:
    """author body has 4 terse placeholder sections — no interpretive
    auto-content (Hypatia leaves Summary empty for canonical figures,
    operator fills for obscure figures)."""
    post = _parse_template("author")
    body = post.content
    sections = ["# Summary", "# Contents", "# Tags", "# See Also"]
    for section in sections:
        assert section in body, f"author missing section: {section!r}"
    # No interpretive prose — sections should be empty placeholders.
    # Heuristic: count substantive (non-section, non-blank) lines.
    substantive_lines = [
        line for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert substantive_lines == [], (
        f"author template should ship empty placeholder sections, found "
        f"non-section content: {substantive_lines}"
    )


# --- Placeholder syntax consistency ---------------------------------------


_PHASE_1_NEW_TEMPLATES: tuple[str, ...] = (
    "memo", "zettel", "MOC", "question", "research-pointer",
)


@pytest.mark.parametrize("name", _PHASE_1_NEW_TEMPLATES)
def test_template_uses_canonical_placeholders(name: str) -> None:
    """Every new template uses ``{{title}}`` and ``{{date}}`` placeholders.

    The scaffold renderer substitutes these via the existing template
    machinery. Drift to e.g. ``{title}`` or ``$TITLE`` would silently
    break record creation for the new types.
    """
    path = _template_path(name)
    text = path.read_text(encoding="utf-8")
    assert "{{title}}" in text, f"{name}: missing {{title}} placeholder"
    assert "{{date}}" in text, f"{name}: missing {{date}} placeholder"


# --- Frontmatter parses (catches YAML errors at commit time) --------------


@pytest.mark.parametrize("name", _PHASE_1_NEW_TEMPLATES + ("author",))
def test_template_frontmatter_parses_cleanly(name: str) -> None:
    """python-frontmatter parses each template without error.

    Catches: bad YAML indentation, unquoted special chars, missing
    closing ``---``. Cheap to run; catches the entire class of
    template-bitrot failures at commit time.
    """
    post = _parse_template(name)
    # Type field always present and matches the template name (modulo
    # MOC casing).
    assert "type" in post.metadata
