"""Template tests for source/ (Phase 2 deliverable #1, 2026-05-17).

Source template enrichment per ``project_hypatia_zettelkasten_redesign.md``
"LOCKED IMPLEMENTATION PLAN" → "Body templates" → source/<title of work>.md
scaffolding. The bundled template lands at
``src/alfred/_bundled/scaffold/_templates/source.md``; operators get
the updated template via the scaffold-sync flow.

Pins:
  * Template file exists in the bundled scaffold.
  * Frontmatter parses cleanly via python-frontmatter (b92c982 lesson:
    {{title}} / {{date}} MUST be quoted).
  * Frontmatter shape per the brief's deliverable #1: type, name,
    created, author, url, source_type, source_anchor, status, mocs,
    tags. ``source_type`` and ``source_anchor`` are NEW fields
    populated by deliverables #2 and #3.
  * Body sections (in canonical order):
      # Source Details
        ## Bibliographic Details
        ## Goal
        ## Overview
      # Notes
        ## Summary Statement
        ## Why It Matters
        ## Observations During
      ## Permanent Notes spawned
      # External References
      # Tags
      # Indexing & MOCs
  * All section headers preserved verbatim (operator-facing scaffolding).
  * No interpretive auto-content under any retrospective placeholder
    (Hypatia leaves Summary Statement / Why It Matters empty).
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from alfred._data import get_scaffold_dir


# --- Helpers --------------------------------------------------------------


def _template_path() -> Path:
    return get_scaffold_dir() / "_templates" / "source.md"


def _parse_template() -> frontmatter.Post:
    return frontmatter.load(_template_path())


# --- File existence + frontmatter parse ----------------------------------


def test_source_template_exists() -> None:
    """The bundled template file is present."""
    assert _template_path().exists()


def test_source_frontmatter_parses_cleanly() -> None:
    """python-frontmatter parses the template without YAML error.

    Regression-pin for the b92c982 YAML lesson: unquoted ``{{title}}``
    / ``{{date}}`` break YAML parsing (double-braces interpreted as
    flow-mapping syntax → ConstructorError). Template's placeholders
    MUST be quoted.
    """
    post = _parse_template()
    assert "type" in post.metadata


# --- Frontmatter shape ----------------------------------------------------


def test_source_frontmatter_shape() -> None:
    """Frontmatter carries the Phase 2 field set per the brief.

    Template-default vs resolver-omit drift fix (NOTE-1 of the Phase 2
    hardening pass, 2026-05-17): ``source_type`` and ``source_anchor``
    are NOT in the template defaults. Reasoning:

      * The resolver and extraction-loop already omit these fields from
        ``set_fields`` when their values are empty (per the
        "intentionally left blank" discipline — silent absence is
        meaningful: parser couldn't infer / operator didn't dictate).
      * If the template carried ``source_type: ""`` as a default,
        ``vault_create``'s template-frontmatter merge would persist
        the empty string regardless of the resolver's omit-discipline.
        Drift between the two surfaces would silently land empty
        strings on every new record.
      * Additionally, ``source_anchor`` is a per-claim ZETTEL field
        (set by the extraction LLM on derived zettels). It doesn't
        semantically belong on ``source/`` records at all — the
        source record represents the WORK, not any single observation
        within it.

    Fresh source records get ``source_type`` only when the parser
    infers a shape (book / article / podcast / video / lecture /
    conversation). When unset, the field is absent from frontmatter
    entirely — operator can add it manually if needed.
    """
    post = _parse_template()
    fm = post.metadata
    assert fm["type"] == "source"
    assert fm["name"] == "{{title}}"
    assert fm["created"] == "{{date}}"
    # author + url default empty strings — populated by the resolver
    # (author) or operator (url).
    assert "author" in fm
    assert fm["author"] == ""
    assert "url" in fm
    assert fm["url"] == ""
    # Phase 2 hardening (NOTE-1): source_type + source_anchor REMOVED
    # from template defaults to prevent template/resolver drift. The
    # resolver writes source_type to frontmatter when the parser
    # infers a shape; otherwise the field is absent (per
    # "intentionally left blank" discipline). source_anchor is a
    # per-zettel field, not a per-source field — doesn't belong here.
    assert "source_type" not in fm
    assert "source_anchor" not in fm
    # Existing fields preserved.
    assert fm["status"] == "active"
    assert fm["mocs"] == []
    assert fm["tags"] == []


def test_source_uses_canonical_placeholders() -> None:
    """The ``{{title}}`` and ``{{date}}`` substitution placeholders are
    present in the raw file text."""
    text = _template_path().read_text(encoding="utf-8")
    assert "{{title}}" in text
    assert "{{date}}" in text


# --- Body sections --------------------------------------------------------


_BODY_H1_SECTIONS: tuple[str, ...] = (
    "# Source Details",
    "# Notes",
    "# External References",
    "# Tags",
    "# Indexing & MOCs",
)


_BODY_H2_SECTIONS: tuple[str, ...] = (
    "## Bibliographic Details",
    "## Goal",
    "## Overview",
    "## Summary Statement",
    "## Why It Matters",
    "## Observations During",
    "## Permanent Notes spawned",
)


@pytest.mark.parametrize("section", _BODY_H1_SECTIONS)
def test_source_h1_section_present(section: str) -> None:
    """Each of the 5 top-level body sections is present verbatim."""
    body = _parse_template().content
    assert section in body, f"missing H1 section: {section!r}"


@pytest.mark.parametrize("section", _BODY_H2_SECTIONS)
def test_source_h2_section_present(section: str) -> None:
    """Each of the 7 H2 sub-sections is present verbatim."""
    body = _parse_template().content
    assert section in body, f"missing H2 section: {section!r}"


def test_source_body_section_order() -> None:
    """Body sections appear in canonical order:
      Source Details → Bibliographic Details / Goal / Overview
      Notes → Summary Statement / Why It Matters / Observations During
      Permanent Notes spawned
      External References
      Tags
      Indexing & MOCs
    """
    body = _parse_template().content
    canonical_order = [
        "# Source Details",
        "## Bibliographic Details",
        "## Goal",
        "## Overview",
        "# Notes",
        "## Summary Statement",
        "## Why It Matters",
        "## Observations During",
        "## Permanent Notes spawned",
        "# External References",
        "# Tags",
        "# Indexing & MOCs",
    ]
    indexes = [body.index(s) for s in canonical_order]
    assert indexes == sorted(indexes), (
        f"section order drift — got {canonical_order} at indexes {indexes}"
    )


def test_source_retrospective_placeholders_empty() -> None:
    """Summary Statement + Why It Matters are RETROSPECTIVE placeholders
    — the operator fills them after engaging with the source. Hypatia
    auto-creation leaves them empty (no interpretive auto-content,
    per the brief's Option A discipline).

    Heuristic: each section header is followed by another section
    header (or end-of-file) with only blank lines between — no
    substantive content under either heading.
    """
    body = _parse_template().content
    # Locate the Summary Statement section + the next section header.
    ss_start = body.index("## Summary Statement")
    wim_start = body.index("## Why It Matters")
    obs_start = body.index("## Observations During")
    # Between Summary Statement and Why It Matters: only blank lines.
    ss_to_wim = body[ss_start + len("## Summary Statement"):wim_start]
    assert ss_to_wim.strip() == "", (
        f"Summary Statement should be empty placeholder, got: {ss_to_wim!r}"
    )
    # Between Why It Matters and Observations During: only blank lines.
    wim_to_obs = body[wim_start + len("## Why It Matters"):obs_start]
    assert wim_to_obs.strip() == "", (
        f"Why It Matters should be empty placeholder, got: {wim_to_obs!r}"
    )


def test_source_observations_during_starts_empty() -> None:
    """``## Observations During`` ships with no pre-populated
    ``### YYYY-MM-DD`` subsection. Per-encounter dated subsections are
    appended by the deliverable #4 re-encounter flow."""
    body = _parse_template().content
    obs_start = body.index("## Observations During")
    perm_start = body.index("## Permanent Notes spawned")
    obs_section = body[obs_start + len("## Observations During"):perm_start]
    # No `### <date>` subsection header in the empty template.
    assert "###" not in obs_section, (
        f"Observations During should be empty in the template; per-session "
        f"subsections appended by deliverable #4. Got: {obs_section!r}"
    )


def test_source_permanent_notes_spawned_starts_empty() -> None:
    """``## Permanent Notes spawned`` ships empty. Wikilinks are
    idempotently appended by deliverable #5 when zettels are created
    with ``source:`` set."""
    body = _parse_template().content
    perm_start = body.index("## Permanent Notes spawned")
    ext_start = body.index("# External References")
    perm_section = body[perm_start + len("## Permanent Notes spawned"):ext_start]
    # No `- [[zettel/...]]` wikilink in the empty template.
    assert "[[zettel/" not in perm_section
    assert "[[note/" not in perm_section
