"""Tests for the ``## Alfred Learnings`` section detector.

KAL-LE distiller-radar Phase 1 (2026-04-29) treats the explicitly
flagged Learnings section in dev session notes as DIRECT extraction
signal — bullets each represent a candidate knowledge atom. The
detector must:

  - Match the exact ``## Alfred Learnings`` heading (no fuzz, no
    ``# Alfred Learnings``, no ``### Alfred Learnings``).
  - Capture content from the heading line down to the next ``## ``
    heading or end-of-document.
  - Tolerate multi-paragraph sections (each bullet often is its own
    paragraph).
  - Return ``None`` when the section is absent so the caller falls
    back to full-body extraction.

The legacy ``format_source_records`` helper and the V2 extractor's
``_render_user_prompt`` both consume this — keep behavior identical.
"""

from __future__ import annotations

from alfred.distiller.parser import (
    extract_alfred_learnings_section,
)


# --- happy paths -----------------------------------------------------------


def test_extract_returns_section_body_when_present() -> None:
    body = (
        "# Title\n"
        "\n"
        "## Intent\n"
        "Some intent here.\n"
        "\n"
        "## Alfred Learnings\n"
        "\n"
        "**Pattern** — when X then Y.\n"
        "\n"
        "**Gotcha** — Z bites you on the second run.\n"
    )
    out = extract_alfred_learnings_section(body)
    assert out is not None
    assert "**Pattern**" in out
    assert "**Gotcha**" in out
    # Doesn't include the heading line itself — the caller already
    # knows it's the Alfred Learnings section.
    assert "## Alfred Learnings" not in out


def test_extract_stops_at_next_h2_heading() -> None:
    body = (
        "## Alfred Learnings\n"
        "\n"
        "Bullet 1.\n"
        "\n"
        "Bullet 2.\n"
        "\n"
        "## Verification\n"
        "\n"
        "Should not appear in output.\n"
    )
    out = extract_alfred_learnings_section(body)
    assert out is not None
    assert "Bullet 1." in out
    assert "Bullet 2." in out
    assert "Verification" not in out
    assert "Should not appear" not in out


def test_extract_stops_at_eof_when_section_is_last() -> None:
    body = (
        "## Intent\n"
        "Setup.\n"
        "\n"
        "## Alfred Learnings\n"
        "\n"
        "Final bullet.\n"
    )
    out = extract_alfred_learnings_section(body)
    assert out is not None
    assert "Final bullet." in out
    assert "Setup." not in out


def test_extract_handles_multi_paragraph_section() -> None:
    body = (
        "## Alfred Learnings\n"
        "\n"
        "**First**: a long paragraph that explains the first learning\n"
        "across multiple lines and even multiple sentences. The point\n"
        "is the matcher must not stop at a blank line.\n"
        "\n"
        "**Second**: another multi-line paragraph that also wraps.\n"
        "Continuation here.\n"
        "\n"
        "**Third**: shorter.\n"
        "\n"
        "## Other\n"
    )
    out = extract_alfred_learnings_section(body)
    assert out is not None
    assert "**First**" in out
    assert "**Second**" in out
    assert "**Third**" in out
    assert "Continuation here" in out
    assert "## Other" not in out


def test_extract_preserves_nested_h3_subheadings() -> None:
    """Sub-section headings inside the Learnings block remain part of the body."""
    body = (
        "## Alfred Learnings\n"
        "\n"
        "### Patterns validated\n"
        "\n"
        "- A pattern.\n"
        "\n"
        "### Gotchas\n"
        "\n"
        "- A gotcha.\n"
        "\n"
        "## Done\n"
    )
    out = extract_alfred_learnings_section(body)
    assert out is not None
    assert "### Patterns validated" in out
    assert "### Gotchas" in out
    assert "- A pattern." in out
    assert "- A gotcha." in out


# --- edge cases / negative paths -------------------------------------------


def test_extract_returns_none_when_section_absent() -> None:
    body = (
        "# Title\n\n"
        "## Intent\n\nSome intent.\n\n"
        "## Verification\n\nDone.\n"
    )
    assert extract_alfred_learnings_section(body) is None


def test_extract_returns_none_for_empty_body() -> None:
    assert extract_alfred_learnings_section("") is None


def test_extract_returns_none_for_section_with_only_whitespace() -> None:
    """A section heading with no real content shouldn't fool the detector."""
    body = (
        "## Alfred Learnings\n"
        "\n"
        "   \n"
        "\n"
        "## Next\n"
    )
    # Stripped content is empty — caller wants None to fall back to
    # full-body extraction rather than feed a blank slug to the LLM.
    assert extract_alfred_learnings_section(body) is None


def test_extract_does_not_match_h1_alfred_learnings() -> None:
    """``# Alfred Learnings`` (H1) must NOT match — convention is H2."""
    body = (
        "# Alfred Learnings\n\n"
        "Not the section we mean.\n"
    )
    assert extract_alfred_learnings_section(body) is None


def test_extract_does_not_match_h3_alfred_learnings() -> None:
    """``### Alfred Learnings`` (H3) must NOT match — convention is H2."""
    body = (
        "## Real Section\n\n"
        "### Alfred Learnings\n\n"
        "Sub-heading mention shouldn't trigger.\n"
    )
    assert extract_alfred_learnings_section(body) is None


def test_extract_does_not_match_partial_string() -> None:
    """Heading must be exact ``Alfred Learnings`` token, not substring."""
    body = (
        "## Alfred Learnings Discussion\n\n"
        "Different heading entirely.\n"
    )
    # The regex requires the heading line to end after "Alfred Learnings"
    # (allowing trailing whitespace only) — extra tokens disqualify it.
    out = extract_alfred_learnings_section(body)
    # Note: the regex allows trailing whitespace ``\s*`` after "Learnings",
    # which would tolerate ``## Alfred Learnings   \n`` but NOT
    # ``## Alfred Learnings Discussion\n``. We assert the strict behavior.
    assert out is None


def test_extract_tolerates_trailing_whitespace_on_heading() -> None:
    """``## Alfred Learnings   `` (trailing spaces on heading) still matches."""
    body = (
        "## Alfred Learnings   \n"
        "\n"
        "Bullet.\n"
    )
    out = extract_alfred_learnings_section(body)
    assert out is not None
    assert "Bullet." in out


def test_extract_only_picks_first_section_when_duplicated() -> None:
    """Pathological case: two ``## Alfred Learnings`` headings in one file.

    The first match wins. Authors shouldn't write multiple sections, but
    the regex must not crash or concatenate them.
    """
    body = (
        "## Alfred Learnings\n"
        "\n"
        "First instance bullet.\n"
        "\n"
        "## Body\n"
        "\n"
        "Stuff.\n"
        "\n"
        "## Alfred Learnings\n"
        "\n"
        "Second instance bullet.\n"
    )
    out = extract_alfred_learnings_section(body)
    assert out is not None
    assert "First instance bullet." in out
    # Second instance is in a separate match; first wins.
    assert "Second instance bullet." not in out


# --- integration with extractor / format_source_records --------------------


def test_format_source_records_surfaces_flagged_section() -> None:
    """``format_source_records`` must surface the Learnings section as a
    distinct block ahead of the full body."""
    from alfred.distiller.backends import format_source_records
    from alfred.distiller.candidates import CandidateSignal, ScoredCandidate
    from alfred.distiller.parser import VaultRecord

    rec = VaultRecord(
        rel_path="session/Test.md",
        frontmatter={"type": "session"},
        body=(
            "# Test\n\n"
            "## Intent\n\nSetup.\n\n"
            "## Alfred Learnings\n\n"
            "**Flagged item** — important.\n"
        ),
        record_type="session",
        wikilinks=[],
    )
    sc = ScoredCandidate(
        record=rec, score=0.7, signals=CandidateSignal(), md5="x", body_hash="y",
    )
    out = format_source_records([sc])
    assert "EXPLICITLY FLAGGED LEARNINGS" in out
    assert "**Flagged item**" in out
    assert "FULL CONTEXT:" in out


def test_format_source_records_falls_back_when_no_section() -> None:
    from alfred.distiller.backends import format_source_records
    from alfred.distiller.candidates import CandidateSignal, ScoredCandidate
    from alfred.distiller.parser import VaultRecord

    rec = VaultRecord(
        rel_path="note/Plain.md",
        frontmatter={"type": "note"},
        body="Just a note body. No flagged section here.\n",
        record_type="note",
        wikilinks=[],
    )
    sc = ScoredCandidate(
        record=rec, score=0.5, signals=CandidateSignal(), md5="x", body_hash="y",
    )
    out = format_source_records([sc])
    # No flagged-section banner when section is absent.
    assert "EXPLICITLY FLAGGED LEARNINGS" not in out
    assert "FULL CONTEXT:" not in out
    # Body still rendered.
    assert "Just a note body." in out


def test_v2_extractor_user_prompt_surfaces_flagged_section() -> None:
    """The V2 extractor's user prompt must split flagged from full context."""
    from alfred.distiller.candidates import CandidateSignal
    from alfred.distiller.extractor import _render_user_prompt

    body_with_section = (
        "# Title\n\n"
        "## Intent\n\nDoing X.\n\n"
        "## Alfred Learnings\n\n"
        "**Pattern** — X works.\n"
    )
    prompt = _render_user_prompt(
        source_body=body_with_section,
        source_frontmatter={"type": "session"},
        existing_learn_titles=[],
        signals=CandidateSignal(),
    )
    assert "EXPLICITLY FLAGGED LEARNINGS" in prompt
    assert "**Pattern**" in prompt
    assert "FULL CONTEXT" in prompt


def test_v2_extractor_user_prompt_falls_back_when_no_section() -> None:
    from alfred.distiller.candidates import CandidateSignal
    from alfred.distiller.extractor import _render_user_prompt

    body_no_section = "Just regular content.\n"
    prompt = _render_user_prompt(
        source_body=body_no_section,
        source_frontmatter={"type": "note"},
        existing_learn_titles=[],
        signals=CandidateSignal(),
    )
    assert "EXPLICITLY FLAGGED LEARNINGS" not in prompt
    # The original "--- Source body ---" framing is preserved.
    assert "Source body" in prompt
    assert "Just regular content." in prompt
