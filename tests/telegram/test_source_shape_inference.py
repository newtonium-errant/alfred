"""Source-shape inference tests — Phase 2 deliverable #3 (2026-05-17).

The opening-pattern parser now infers a ``source_type`` from the verb
in the operator's opening turn:

  reading      → book (default; or article if title has URL fragment)
  watching     → video
  listening to → podcast
  in conversation with / talking with → conversation
  at a lecture by → lecture

The inferred shape is stored on the source record as ``source_type:``
frontmatter via the resolver chain
(``parse_opening_anchors`` → ``resolve_session_anchors`` →
``resolve_or_create_source`` → ``vault_create``).

Coverage:
  * Unit tests on ``parse_opening_anchors`` for each verb pattern
  * URL-fragment refinement (reading → article when title looks
    like a URL / Substack post)
  * Pattern ordering: most-specific first (lecture / conversation
    don't get shadowed by reading / watching)
  * End-to-end: resolver writes ``source_type:`` to the created
    record's frontmatter
  * Pre-existing source records are NOT mutated (operator-set values
    win — the resolver only writes source_type on CREATE)
  * Empty / unrecognised opening text → no source_type inferred
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from alfred.telegram import capture_source_anchor as csa


# --- Unit tests on parse_opening_anchors source_type inference ---------


@pytest.mark.parametrize("text,expected_type,expected_title,expected_author", [
    # Reading → book
    ("I'm reading Meditations by Marcus Aurelius",
     "book", "Meditations", "Marcus Aurelius"),
    ("Currently reading The Iliad by Homer.",
     "book", "The Iliad", "Homer"),
    ("Reading Crime and Punishment by Fyodor Dostoevsky.",
     "book", "Crime and Punishment", "Fyodor Dostoevsky"),

    # Reading + URL → article (Substack hint)
    ("I'm reading https://example.substack.com/p/foo by Author X",
     "article", "https://example.substack.com/p/foo", "Author X"),
    ("Currently reading https://stratechery.com/2026/foo by Ben Thompson",
     "article", "https://stratechery.com/2026/foo", "Ben Thompson"),

    # Watching → video
    ("I'm watching The Knife by Carlo",
     "video", "The Knife", "Carlo"),
    ("Currently watching The Long Cut by Hadot.",
     "video", "The Long Cut", "Hadot"),

    # Listening to → podcast
    ("I'm listening to The Stoic Cast by Some Host",
     "podcast", "The Stoic Cast", "Some Host"),
    ("Currently listening to Acquired by Ben Gilbert.",
     "podcast", "Acquired", "Ben Gilbert"),

    # In conversation with → conversation. Author is the interlocutor;
    # title is the topic ("about Y") if given.
    ("I'm in conversation with Xian Niles about Fiore manuscripts",
     "conversation", "Fiore manuscripts", "Xian Niles"),
    ("I'm talking with Jamie about the clinic move.",
     "conversation", "the clinic move", "Jamie"),

    # At a lecture by → lecture. Speaker is "author"; title is topic.
    ("I'm at a lecture by Hadot on Stoic practice",
     "lecture", "Stoic practice", "Hadot"),
    ("At a lecture by Pierre Hadot on Spiritual Exercises.",
     "lecture", "Spiritual Exercises", "Pierre Hadot"),
])
def test_parse_opening_anchors_source_type_inference(
    text: str,
    expected_type: str,
    expected_title: str,
    expected_author: str,
) -> None:
    """Each opening-pattern variant infers the correct source_type +
    extracts title/author correctly."""
    parsed = csa.parse_opening_anchors(text)
    assert parsed.source_type == expected_type, (
        f"source_type mismatch for {text!r}: "
        f"expected {expected_type!r}, got {parsed.source_type!r}"
    )
    assert parsed.title == expected_title, (
        f"title mismatch for {text!r}: "
        f"expected {expected_title!r}, got {parsed.title!r}"
    )
    assert parsed.author == expected_author, (
        f"author mismatch for {text!r}: "
        f"expected {expected_author!r}, got {parsed.author!r}"
    )


def test_parse_opening_anchors_empty_text_no_source_type() -> None:
    """Empty text → no source_type."""
    parsed = csa.parse_opening_anchors("")
    assert parsed.source_type == ""


def test_parse_opening_anchors_no_pattern_match_no_source_type() -> None:
    """Text that doesn't match any verb pattern → no source_type."""
    parsed = csa.parse_opening_anchors(
        "just rambling about Q2 plans, no source mentioned"
    )
    assert parsed.source_type == ""


def test_parse_opening_anchors_continues_from_alone_no_source_type() -> None:
    """``This continues from [[X]]`` alone doesn't infer a source_type
    (continuation is a different signal than source-shape)."""
    parsed = csa.parse_opening_anchors(
        "This continues from [[session/prior-capture]]"
    )
    assert parsed.continues_from == "session/prior-capture"
    assert parsed.source_type == ""


# --- Pattern ordering: most-specific first --------------------------------


def test_lecture_pattern_beats_reading_pattern() -> None:
    """``at a lecture by Hadot`` should match LECTURE, not slip through
    to a fallback. The string contains no "reading" keyword so this is
    really testing that the lecture pattern is in the iteration order
    AT ALL — but pin it explicitly so future re-orderings can't silently
    regress."""
    parsed = csa.parse_opening_anchors(
        "I'm at a lecture by Hadot on Stoic practice"
    )
    assert parsed.source_type == "lecture"
    assert parsed.author == "Hadot"


def test_conversation_pattern_handles_no_topic() -> None:
    """``I'm in conversation with X`` (no "about Y" topic) still infers
    conversation; title is empty in that case."""
    parsed = csa.parse_opening_anchors("I'm in conversation with Xian Niles")
    assert parsed.source_type == "conversation"
    assert parsed.author == "Xian Niles"
    # Title may be empty when no "about Y" clause given.
    assert parsed.title == ""


def test_watching_pattern_handles_no_author() -> None:
    """``I'm watching The Long Cut`` (no "by Y" author) still infers
    video; author empty."""
    parsed = csa.parse_opening_anchors("I'm watching The Long Cut")
    assert parsed.source_type == "video"
    assert parsed.title == "The Long Cut"
    assert parsed.author == ""


def test_listening_pattern_handles_no_author() -> None:
    """Podcasts often have no byline — just the show name."""
    parsed = csa.parse_opening_anchors("I'm listening to Acquired")
    assert parsed.source_type == "podcast"
    assert parsed.title == "Acquired"
    assert parsed.author == ""


# --- URL refinement for reading → article --------------------------------


@pytest.mark.parametrize("url,expected_type", [
    ("https://example.com/foo",           "article"),
    ("https://example.substack.com/p/foo", "article"),
    ("https://stratechery.com/2026/foo",  "article"),
    ("http://www.example.org/post",        "article"),
    # Plain titles without URL fragments stay book.
    ("Meditations",                        "book"),
    ("The Iliad",                          "book"),
    ("Crime and Punishment",               "book"),
])
def test_reading_url_refinement(url: str, expected_type: str) -> None:
    """``reading <URL>`` infers ``article``; ``reading <plain title>``
    stays ``book``."""
    parsed = csa.parse_opening_anchors(f"I'm reading {url} by Author")
    assert parsed.source_type == expected_type


# --- End-to-end: source_type lands on the created source record ---------


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for sub in ("source", "author", "session"):
        (vault / sub).mkdir(parents=True)
    return vault


def test_resolver_writes_source_type_on_create(tmp_path: Path) -> None:
    """End-to-end: capture opens "I'm watching X by Y" → resolver
    creates ``source/X.md`` with ``source_type: video`` frontmatter."""
    vault = _make_vault(tmp_path)
    result = csa.resolve_session_anchors(
        vault, "I'm watching The Long Cut by Some Director",
    )
    assert result.source_wikilink == "[[source/The Long Cut]]"
    assert result.source_created is True
    src = frontmatter.load(vault / "source/The Long Cut.md")
    assert src.metadata["source_type"] == "video"


def test_resolver_writes_book_for_reading(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    csa.resolve_session_anchors(
        vault, "I'm reading Meditations by Marcus Aurelius",
    )
    src = frontmatter.load(vault / "source/Meditations.md")
    assert src.metadata["source_type"] == "book"


def test_resolver_writes_podcast_for_listening(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    csa.resolve_session_anchors(
        vault, "I'm listening to Acquired by Ben Gilbert",
    )
    src = frontmatter.load(vault / "source/Acquired.md")
    assert src.metadata["source_type"] == "podcast"


def test_resolver_writes_conversation_for_in_conversation_with(
    tmp_path: Path,
) -> None:
    vault = _make_vault(tmp_path)
    # Topic-as-title routes through the title field.
    result = csa.resolve_session_anchors(
        vault, "I'm in conversation with Xian Niles about Fiore manuscripts",
    )
    # The "title" here is the topic — that becomes the source filename.
    assert "source/Fiore manuscripts" in (result.source_wikilink or "")
    src = frontmatter.load(vault / "source/Fiore manuscripts.md")
    assert src.metadata["source_type"] == "conversation"


def test_resolver_writes_lecture_for_at_a_lecture_by(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    csa.resolve_session_anchors(
        vault, "I'm at a lecture by Hadot on Stoic practice",
    )
    src = frontmatter.load(vault / "source/Stoic practice.md")
    assert src.metadata["source_type"] == "lecture"


def test_resolver_omits_source_type_when_pattern_doesnt_match(
    tmp_path: Path,
) -> None:
    """When the opening text doesn't match any verb pattern, the
    resolver doesn't fire — but if it did fire with empty source_type
    (e.g., operator manually called resolve_or_create_source with
    source_type=""), the field is omitted from frontmatter."""
    vault = _make_vault(tmp_path)
    csa.resolve_or_create_source(
        vault, "Manually Created Source",
        author_full="", author_wikilink="",
        scope="hypatia",
        source_type="",  # empty
    )
    src = frontmatter.load(vault / "source/Manually Created Source.md")
    # No source_type field on the record (empty → omitted).
    assert "source_type" not in src.metadata


# --- Existing source records are NOT mutated -----------------------------


def test_resolver_does_not_overwrite_existing_source_type(
    tmp_path: Path,
) -> None:
    """Operator already has ``source/Meditations.md`` with
    ``source_type: book``. A re-encounter via ``I'm reading Meditations
    by Marcus Aurelius`` should NOT touch the existing frontmatter.

    Even more important: if the operator had hand-set source_type to
    something different (e.g., ``audiobook`` for the audio edition),
    the resolver must respect operator-curated values.
    """
    vault = _make_vault(tmp_path)
    # Operator-curated pre-existing source.
    (vault / "source" / "Meditations.md").write_text(
        "---\n"
        "type: source\n"
        "name: Meditations\n"
        "created: '2026-05-15'\n"
        "source_type: audiobook\n"  # operator-set, non-default value
        "status: active\n"
        "---\n\n# Meditations\n\n## Notes\n\n(running notes)\n",
        encoding="utf-8",
    )
    # Re-encounter — resolver should find existing source, NOT mutate.
    result = csa.resolve_session_anchors(
        vault, "I'm reading Meditations by Marcus Aurelius",
    )
    assert result.source_created is False
    src = frontmatter.load(vault / "source/Meditations.md")
    # Operator's audiobook value preserved.
    assert src.metadata["source_type"] == "audiobook"
