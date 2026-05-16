"""Tests for capture-source-anchor module (2026-05-16 arc).

Covers:
    * Schema additions — ``author`` registered, source records still
      validate (free-text + wikilink shapes).
    * Opening-pattern parser — detects ``I'm reading X by Y``,
      ``continues from [[note/X]]``, suffix-aware last-name derivation.
    * Resolver — creates source/author records, threads wikilinks,
      flags ambiguity on conflicting authors with the same last name.
    * Within-session peer cross-link heuristic — 2 shared substantive
      tokens links; 1 token does not; stopwords filtered.
    * Re-encounter scan — finds prior records by source-anchor / author /
      topic terms, dedupes, recency-orders, empty case returns [].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.telegram import capture_source_anchor as csa
from alfred.vault import ops, schema, scope


# --- Schema + scope integration ------------------------------------------


def test_schema_registers_author_type() -> None:
    """``author`` is part of Hypatia's known types + scope create-allowlist."""
    assert "author" in schema.KNOWN_TYPES_HYPATIA
    assert "author" in scope.HYPATIA_CREATE_TYPES
    # Status set is small and includes the default ``active``.
    assert "active" in schema.STATUS_BY_TYPE["author"]
    # Filename routing → ``author/<last_name>.md``.
    assert schema.TYPE_DIRECTORY["author"] == "author"
    # Type gate admits hypatia scope, denies bare unscoped.
    union = schema.KNOWN_TYPES_BY_SCOPE["hypatia"]
    assert "author" in union


def test_source_record_with_freetext_author_still_validates(tmp_path: Path) -> None:
    """Legacy source record (``author: Carlo Atendido``) still passes _validate_type."""
    vault = tmp_path / "vault"
    (vault / "source").mkdir(parents=True)
    (vault / "source" / "Test.md").write_text(
        "---\n"
        "type: source\n"
        "name: Test\n"
        "created: '2026-05-04'\n"
        "author: Carlo Atendido\n"
        "---\n# Test\n",
        encoding="utf-8",
    )
    rec = ops.vault_read(vault, "source/Test.md")
    assert rec["frontmatter"]["type"] == "source"
    assert rec["frontmatter"]["author"] == "Carlo Atendido"


def test_source_record_with_wikilink_author_validates(tmp_path: Path) -> None:
    """New shape (``author: '[[author/Aurelius]]'``) loads cleanly."""
    vault = tmp_path / "vault"
    (vault / "source").mkdir(parents=True)
    (vault / "source" / "Meditations.md").write_text(
        "---\n"
        "type: source\n"
        "name: Meditations\n"
        "created: '2026-05-16'\n"
        'author: "[[author/Aurelius]]"\n'
        "---\n# Meditations\n",
        encoding="utf-8",
    )
    rec = ops.vault_read(vault, "source/Meditations.md")
    assert rec["frontmatter"]["author"] == "[[author/Aurelius]]"


# --- Opening-pattern parser ----------------------------------------------


@pytest.mark.parametrize("text,expected_title,expected_author", [
    ("I'm reading Meditations by Marcus Aurelius",
     "Meditations", "Marcus Aurelius"),
    ("Currently reading The Iliad by Homer.",
     "The Iliad", "Homer"),
    ("I am reading The Republic by Plato",
     "The Republic", "Plato"),
    ("I'm working through Discourses by Epictetus",
     "Discourses", "Epictetus"),
    ("Reading Crime and Punishment by Fyodor Dostoevsky.",
     "Crime and Punishment", "Fyodor Dostoevsky"),
])
def test_parse_opening_anchors_matches_reading_patterns(
    text: str, expected_title: str, expected_author: str
) -> None:
    parsed = csa.parse_opening_anchors(text)
    assert parsed.title == expected_title
    assert parsed.author == expected_author


def test_parse_opening_anchors_continues_from() -> None:
    parsed = csa.parse_opening_anchors(
        "This continues from [[session/conversation-2026-05-10-x-d2ff1a5a]]"
    )
    assert parsed.continues_from == "session/conversation-2026-05-10-x-d2ff1a5a"


def test_parse_opening_anchors_both_patterns_fire() -> None:
    """A session can be both a continuation AND from a source."""
    parsed = csa.parse_opening_anchors(
        "I'm reading Meditations by Marcus Aurelius. "
        "This continues from [[session/prior]]."
    )
    assert parsed.title == "Meditations"
    assert parsed.author == "Marcus Aurelius"
    assert parsed.continues_from == "session/prior"


def test_parse_opening_anchors_no_match() -> None:
    parsed = csa.parse_opening_anchors("just rambling about Q2 plans")
    assert parsed.title == ""
    assert parsed.author == ""
    assert parsed.continues_from == ""


def test_parse_opening_anchors_empty_text() -> None:
    parsed = csa.parse_opening_anchors("")
    assert parsed.title == ""


# --- Last-name derivation ------------------------------------------------


@pytest.mark.parametrize("author,expected", [
    ("Marcus Aurelius",       "Aurelius"),
    ("Foo Bar Jr.",           "Bar"),
    ("Foo Bar Jr",            "Bar"),
    ("Foo Bar III",           "Bar"),
    ("Aurelius, Marcus",      "Aurelius"),
    ("Aristotle",             "Aristotle"),
    ("John Smith PhD",        "Smith"),
    ("",                      ""),
    ("   ",                   ""),
])
def test_derive_last_name(author: str, expected: str) -> None:
    assert csa.derive_last_name(author) == expected


# --- Resolver integration ------------------------------------------------


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for sub in ("session", "source", "author", "note"):
        (vault / sub).mkdir(parents=True)
    return vault


def test_resolve_session_anchors_creates_source_and_author(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    result = csa.resolve_session_anchors(
        vault, "I'm reading Meditations by Marcus Aurelius",
    )
    assert result.source_wikilink == "[[source/Meditations]]"
    assert result.author_wikilink == "[[author/Aurelius]]"
    assert result.source_created is True
    assert result.author_created is True
    assert not result.author_ambiguous

    # Files actually exist + author wikilink threaded into source.
    assert (vault / "source" / "Meditations.md").exists()
    assert (vault / "author" / "Aurelius.md").exists()
    src = ops.vault_read(vault, "source/Meditations.md")
    assert src["frontmatter"]["author"] == "[[author/Aurelius]]"
    auth = ops.vault_read(vault, "author/Aurelius.md")
    assert auth["frontmatter"]["name"] == "Marcus Aurelius"
    assert auth["frontmatter"]["last_name"] == "Aurelius"


def test_resolve_session_anchors_jr_suffix(tmp_path: Path) -> None:
    """Suffixed author derives the correct last-name lookup key."""
    vault = _make_vault(tmp_path)
    result = csa.resolve_session_anchors(
        vault, "Currently reading The Frame by Foo Bar Jr.",
    )
    assert result.author_wikilink == "[[author/Bar]]"
    assert (vault / "author" / "Bar.md").exists()


def test_resolve_session_anchors_idempotent(tmp_path: Path) -> None:
    """Second call resolves to existing records — no double-creation."""
    vault = _make_vault(tmp_path)
    csa.resolve_session_anchors(vault, "I'm reading X by Y Smith")
    again = csa.resolve_session_anchors(vault, "I'm reading X by Y Smith")
    assert again.source_wikilink == "[[source/X]]"
    assert again.author_wikilink == "[[author/Smith]]"
    assert again.source_created is False
    assert again.author_created is False


def test_resolve_session_anchors_ambiguous_author(tmp_path: Path) -> None:
    """Same last-name with different ``name`` returns ambiguity flag."""
    vault = _make_vault(tmp_path)
    # Pre-existing author/Smith.md with a different full name.
    (vault / "author" / "Smith.md").write_text(
        "---\n"
        "type: author\n"
        "name: Adam Smith\n"
        "last_name: Smith\n"
        "created: '2026-05-15'\n"
        "---\n# Adam Smith\n",
        encoding="utf-8",
    )
    result = csa.resolve_session_anchors(
        vault, "I'm reading Methodology by John Smith",
    )
    assert result.author_ambiguous is True
    # Source gets created but WITHOUT the (ambiguous) author wikilink —
    # the operator disambiguates first.
    assert result.source_wikilink == "[[source/Methodology]]"
    src = ops.vault_read(vault, "source/Methodology.md")
    # Free-text fallback so the trace isn't lost.
    assert src["frontmatter"].get("author") == "John Smith"


def test_resolve_session_anchors_no_match(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    result = csa.resolve_session_anchors(vault, "rambling about plans")
    assert result.source_wikilink == ""
    assert result.author_wikilink == ""
    assert result.continues_from == ""


# --- Within-session cross-link -------------------------------------------


def test_cross_link_two_shared_substantive_tokens_link() -> None:
    """Two notes sharing 2+ substantive tokens → wikilinked."""
    notes = [
        ("note/Roman Stoicism Origins.md", "Roman Stoicism Origins"),
        ("note/Roman Stoicism in Practice.md", "Roman Stoicism in Practice"),
    ]
    links = csa.compute_peer_cross_links(notes)
    assert "[[note/Roman Stoicism in Practice]]" in links["note/Roman Stoicism Origins.md"]
    assert "[[note/Roman Stoicism Origins]]" in links["note/Roman Stoicism in Practice.md"]


def test_cross_link_one_shared_substantive_token_no_link() -> None:
    """One shared substantive token < 2 threshold → NO link."""
    notes = [
        ("note/Roman Cooking.md", "Roman Cooking"),
        ("note/Greek History.md", "Greek History"),  # no overlap
        ("note/Roman Architecture.md", "Roman Architecture"),  # 1 token shared with first
    ]
    links = csa.compute_peer_cross_links(notes)
    # Only 1 shared token ("roman") between first + third — below threshold.
    assert "note/Roman Cooking.md" not in links
    assert "note/Roman Architecture.md" not in links


def test_cross_link_stopwords_filtered() -> None:
    """Stopwords + short tokens don't drive cross-linking."""
    notes = [
        ("note/The And With.md", "The And With"),  # all stopwords
        ("note/About The With.md", "About The With"),
    ]
    links = csa.compute_peer_cross_links(notes)
    # Titles have NO substantive tokens after stopword filter — no link.
    assert links == {}


def test_cross_link_empty_input() -> None:
    assert csa.compute_peer_cross_links([]) == {}


def test_cross_link_single_note_no_peers() -> None:
    """One note in a session → no peer to link to."""
    assert csa.compute_peer_cross_links([("note/Solo.md", "Solo")]) == {}


# --- Re-encounter scan ---------------------------------------------------


def test_re_encounters_finds_prior_source_anchor(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    # Prior session anchored to the same source.
    (vault / "session" / "prior.md").write_text(
        "---\n"
        "type: session\n"
        "name: prior\n"
        "created: '2026-05-10'\n"
        'source: "[[source/Meditations]]"\n'
        "---\n# prior\n",
        encoding="utf-8",
    )
    rows = csa.find_re_encounters(
        vault,
        source_wikilink="[[source/Meditations]]",
        author_wikilink="",
        topic_terms=[],
        current_session_rel_path="session/current.md",
    )
    assert len(rows) == 1
    assert rows[0].rel_path == "session/prior.md"
    assert rows[0].reason == "source-anchor"


def test_re_encounters_empty_returns_empty_list(tmp_path: Path) -> None:
    """No matches → empty list (renderer turns this into '(none)')."""
    vault = _make_vault(tmp_path)
    rows = csa.find_re_encounters(
        vault,
        source_wikilink="[[source/Nonexistent]]",
        author_wikilink="",
        topic_terms=[],
    )
    assert rows == []


def test_re_encounters_excludes_current_session(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    # Write the current session itself — it should NOT surface.
    current = "session/current.md"
    (vault / "session" / "current.md").write_text(
        "---\n"
        "type: session\n"
        "name: current\n"
        "created: '2026-05-16'\n"
        'source: "[[source/Meditations]]"\n'
        "---\n# current\n",
        encoding="utf-8",
    )
    rows = csa.find_re_encounters(
        vault,
        source_wikilink="[[source/Meditations]]",
        author_wikilink="",
        topic_terms=[],
        current_session_rel_path=current,
    )
    assert rows == []


def test_re_encounters_caps_at_render_max(tmp_path: Path) -> None:
    """More than 5 candidates → top 5 by mtime."""
    import os, time
    vault = _make_vault(tmp_path)
    # Create 7 prior sessions all anchored to the same source.
    for i in range(7):
        path = vault / "session" / f"prior_{i}.md"
        path.write_text(
            "---\n"
            "type: session\n"
            f"name: prior_{i}\n"
            "created: '2026-05-01'\n"
            'source: "[[source/Meditations]]"\n'
            "---\n# prior\n",
            encoding="utf-8",
        )
        # Stagger mtimes so recency ordering is deterministic.
        os.utime(path, (time.time() + i, time.time() + i))
    rows = csa.find_re_encounters(
        vault,
        source_wikilink="[[source/Meditations]]",
        author_wikilink="",
        topic_terms=[],
        render_max=5,
    )
    assert len(rows) == 5
    # Most-recent first → prior_6 then prior_5 etc.
    assert rows[0].rel_path == "session/prior_6.md"
    assert rows[-1].rel_path == "session/prior_2.md"


def test_re_encounters_deduplicates_across_reasons(tmp_path: Path) -> None:
    """A record that matches BOTH source AND author appears once."""
    vault = _make_vault(tmp_path)
    (vault / "session" / "double.md").write_text(
        "---\n"
        "type: session\n"
        "name: double\n"
        "created: '2026-05-10'\n"
        'source: "[[source/Meditations]]"\n'
        'author: "[[author/Aurelius]]"\n'
        "---\n# double\n",
        encoding="utf-8",
    )
    rows = csa.find_re_encounters(
        vault,
        source_wikilink="[[source/Meditations]]",
        author_wikilink="[[author/Aurelius]]",
        topic_terms=[],
    )
    assert len(rows) == 1
    # Source-anchor wins (first scan that catches it).
    assert rows[0].reason == "source-anchor"


def test_re_encounters_renders_section() -> None:
    """Renderer produces the expected markdown bullets."""
    rows = [
        csa.ReEncounter(rel_path="session/a.md", name="a", reason="source-anchor"),
        csa.ReEncounter(rel_path="session/b.md", name="b", reason="topic:stoicism"),
    ]
    out = csa.render_re_encounters_section(rows)
    assert "[[session/a]] — source-anchor" in out
    assert "[[session/b]] — topic:stoicism" in out


def test_re_encounters_renders_none_on_empty() -> None:
    """Empty rows → '(none)' per intentionally-left-blank rule."""
    assert csa.render_re_encounters_section([]) == "(none)"


# --- Orchestrator integration (re-encounter wiring) ----------------------


@pytest.mark.asyncio
async def test_process_capture_session_re_encounters_logged(tmp_path: Path) -> None:
    """Re-encounter scan emits a log line tagging the hit count.

    Per ``feedback_intentionally_left_blank.md`` + the builder pre-commit
    checklist item #9 (log-emission tests must drive the production
    code path) — the scan log MUST fire on every capture session,
    including the empty case, so an operator can grep
    ``talker.capture.re_encounters_scanned`` for daily activity.
    """
    import structlog
    from alfred.telegram import capture_batch
    from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse

    vault = _make_vault(tmp_path)
    # Session record (capture-mode) — needs the body shape the writer expects.
    (vault / "session" / "current.md").write_text(
        "---\n"
        "type: session\n"
        "name: current\n"
        "created: '2026-05-16'\n"
        "session_type: capture\n"
        "---\n\n# Transcript\n\n**Andrew** (10:00): rambling\n",
        encoding="utf-8",
    )

    client = FakeAnthropicClient([
        FakeResponse(
            content=[FakeBlock(
                type="tool_use",
                id="t1",
                name="emit_structured_summary",
                input={
                    "topics": ["stoicism"],
                    "decisions": [],
                    "open_questions": [],
                    "action_items": [],
                    "key_insights": [],
                    "raw_contradictions": [],
                },
            )],
            stop_reason="tool_use",
        )
    ])

    transcript = [
        {"role": "user", "content": "rambling about stoicism",
         "_ts": "2026-05-16T10:00:00+00:00"},
    ]

    with structlog.testing.capture_logs() as captured:
        await capture_batch.process_capture_session(
            client=client,
            vault_path=vault,
            session_rel_path="session/current.md",
            transcript=transcript,
            model="claude-sonnet-4-6",
            send_follow_up=None,
            short_id="abc12345",
            agent_slug="hypatia",
            anchor_scope="hypatia",
        )

    # Re-encounter scan always emits — empty vault should still log "hits=0".
    scan_logs = [c for c in captured
                 if c.get("event") == "talker.capture.re_encounters_scanned"]
    assert len(scan_logs) == 1, f"expected 1 scan log, got {len(scan_logs)}: {captured}"
    assert scan_logs[0]["hits"] == 0
    assert scan_logs[0]["session_rel_path"] == "session/current.md"


@pytest.mark.asyncio
async def test_process_capture_session_anchors_resolved_logged(tmp_path: Path) -> None:
    """When the opening turn matches a reading pattern, the resolver logs."""
    import structlog
    from alfred.telegram import capture_batch
    from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse

    vault = _make_vault(tmp_path)
    (vault / "session" / "current.md").write_text(
        "---\ntype: session\nname: current\n"
        "created: '2026-05-16'\nsession_type: capture\n---\n\n"
        "# Transcript\n\n**Andrew** (10:00): I'm reading Meditations by Marcus Aurelius\n",
        encoding="utf-8",
    )

    client = FakeAnthropicClient([
        FakeResponse(
            content=[FakeBlock(
                type="tool_use", id="t1", name="emit_structured_summary",
                input={
                    "topics": [], "decisions": [], "open_questions": [],
                    "action_items": [], "key_insights": [], "raw_contradictions": [],
                },
            )],
            stop_reason="tool_use",
        )
    ])

    transcript = [
        {"role": "user",
         "content": "I'm reading Meditations by Marcus Aurelius",
         "_ts": "2026-05-16T10:00:00+00:00"},
    ]

    with structlog.testing.capture_logs() as captured:
        await capture_batch.process_capture_session(
            client=client,
            vault_path=vault,
            session_rel_path="session/current.md",
            transcript=transcript,
            model="claude-sonnet-4-6",
            send_follow_up=None,
            short_id="abc",
            agent_slug="hypatia",
            anchor_scope="hypatia",
        )

    resolved_logs = [c for c in captured
                     if c.get("event") == "talker.capture.anchors_resolved"]
    assert len(resolved_logs) == 1
    assert resolved_logs[0]["source_wikilink"] == "[[source/Meditations]]"
    assert resolved_logs[0]["author_wikilink"] == "[[author/Aurelius]]"
    assert resolved_logs[0]["source_created"] is True
    assert resolved_logs[0]["author_created"] is True

    # Session record now carries the wikilinks too.
    rec = ops.vault_read(vault, "session/current.md")
    assert rec["frontmatter"]["source"] == "[[source/Meditations]]"
    assert rec["frontmatter"]["author"] == "[[author/Aurelius]]"
