"""Permanent Notes spawned auto-append tests — Phase 2 deliverable #5
(2026-05-17).

When a derived zettel is created with ``source:`` frontmatter set,
idempotently append ``- [[zettel/<Title>]]`` to that source's
``## Permanent Notes spawned`` body section. Per the locked plan's
"Auto-maintenance behaviors" → #5:

  "when a ``zettel/`` is created with ``source:`` set, Hypatia
  appends ``- [[zettel/Title]]`` to the source's ``## Permanent
  Notes spawned``, idempotent."

Behaviour matrix:
  * Zettel created + source has Permanent Notes spawned section
    → wikilink appended.
  * Same zettel re-extracted (same wikilink) → no-op (idempotent).
  * Multiple zettels from one extraction batch → each appended once.
  * note/ records (Salem default OR Hypatia /end-note override) →
    NO append; the rule is zettel-specific.
  * Source missing the ``## Permanent Notes spawned`` section
    (pre-Phase-2 record) → no-op.
  * Source record doesn't exist → no-op.

Coverage:
  * Unit tests on _build_permanent_notes_rewriter (pure-function)
  * End-to-end via append_permanent_note_spawned helper
  * Integration via extract_notes_from_capture (note vs. zettel
    target driven by anchor_scope + source_anchored)
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from alfred.telegram import capture_batch, capture_extract
from alfred.telegram import capture_source_anchor as csa
from tests.telegram.conftest import (
    FakeAnthropicClient, FakeBlock, FakeResponse,
)


# --- Fixture helpers -----------------------------------------------------


def _phase2_source_body() -> str:
    return (
        "# Source Details\n\n"
        "## Bibliographic Details\n\n"
        "## Goal\n\n"
        "## Overview\n\n"
        "# Notes\n\n"
        "## Summary Statement\n\n"
        "## Why It Matters\n\n"
        "## Observations During\n\n"
        "## Permanent Notes spawned\n\n"
        "# External References\n\n"
        "# Tags\n\n"
        "# Indexing & MOCs\n"
    )


def _write_source_record(
    vault: Path, title: str, body: str | None = None,
) -> str:
    (vault / "source").mkdir(parents=True, exist_ok=True)
    rel = f"source/{title}.md"
    if body is None:
        body = _phase2_source_body()
    (vault / rel).write_text(
        "---\n"
        "type: source\n"
        f"name: {title}\n"
        "created: '2026-05-15'\n"
        "status: active\n"
        "---\n\n" + body,
        encoding="utf-8",
    )
    return rel


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for sub in ("source", "author", "session", "zettel", "note"):
        (vault / sub).mkdir(parents=True)
    return vault


# --- _build_permanent_notes_rewriter (pure-function) ---------------------


def test_perm_notes_rewriter_appends_to_empty_section() -> None:
    """First wikilink → appended to empty Permanent Notes spawned section."""
    body = _phase2_source_body()
    rewriter = csa._build_permanent_notes_rewriter("[[zettel/On Stoicism]]")
    result = rewriter(body)
    assert "- [[zettel/On Stoicism]]" in result
    # Append landed WITHIN the section (between heading and the next
    # H1/H2 boundary).
    perm_idx = result.index("## Permanent Notes spawned")
    link_idx = result.index("- [[zettel/On Stoicism]]")
    ext_idx = result.index("# External References")
    assert perm_idx < link_idx < ext_idx


def test_perm_notes_rewriter_idempotent_on_existing_link() -> None:
    """When the wikilink already exists in the section, the rewriter
    no-ops (returns body unchanged)."""
    body = _phase2_source_body().replace(
        "## Permanent Notes spawned\n\n",
        "## Permanent Notes spawned\n\n- [[zettel/On Stoicism]]\n\n",
    )
    rewriter = csa._build_permanent_notes_rewriter("[[zettel/On Stoicism]]")
    result = rewriter(body)
    assert result == body
    # Only one occurrence of the wikilink.
    assert result.count("[[zettel/On Stoicism]]") == 1


def test_perm_notes_rewriter_appends_second_link_alongside_first() -> None:
    """Second distinct wikilink → appended below the first; both
    preserved."""
    body = _phase2_source_body().replace(
        "## Permanent Notes spawned\n\n",
        "## Permanent Notes spawned\n\n- [[zettel/First]]\n\n",
    )
    rewriter = csa._build_permanent_notes_rewriter("[[zettel/Second]]")
    result = rewriter(body)
    assert "- [[zettel/First]]" in result
    assert "- [[zettel/Second]]" in result
    first_idx = result.index("[[zettel/First]]")
    second_idx = result.index("[[zettel/Second]]")
    assert first_idx < second_idx


def test_perm_notes_rewriter_no_section_is_noop() -> None:
    """Pre-Phase-2 source (no ``## Permanent Notes spawned`` section)
    → rewriter returns body unchanged."""
    body = "# Old Source\n\n## Notes\n\n(running notes)\n"
    rewriter = csa._build_permanent_notes_rewriter("[[zettel/X]]")
    result = rewriter(body)
    assert result == body


def test_perm_notes_rewriter_preserves_subsequent_sections() -> None:
    body = _phase2_source_body()
    rewriter = csa._build_permanent_notes_rewriter("[[zettel/X]]")
    result = rewriter(body)
    # All canonical sections still present in order.
    canonical = [
        "## Permanent Notes spawned",
        "# External References",
        "# Tags",
        "# Indexing & MOCs",
    ]
    indexes = [result.index(s) for s in canonical]
    assert indexes == sorted(indexes)


# --- End-to-end via append_permanent_note_spawned helper ----------------


def test_append_perm_notes_first_link(tmp_path: Path) -> None:
    """E2E: source has empty Permanent Notes spawned → first append
    adds the wikilink + returns True (body changed)."""
    vault = _make_vault(tmp_path)
    rel = _write_source_record(vault, "Meditations")
    ok = csa.append_permanent_note_spawned(
        vault_path=vault,
        source_rel_path=rel,
        zettel_wikilink="[[zettel/On Stoicism]]",
        scope="hypatia",
    )
    assert ok is True
    body = (vault / rel).read_text(encoding="utf-8")
    assert "- [[zettel/On Stoicism]]" in body


def test_append_perm_notes_idempotent(tmp_path: Path) -> None:
    """E2E: append same wikilink twice → first returns True, second
    returns False (idempotent skip)."""
    vault = _make_vault(tmp_path)
    rel = _write_source_record(vault, "Meditations")

    first = csa.append_permanent_note_spawned(
        vault_path=vault, source_rel_path=rel,
        zettel_wikilink="[[zettel/X]]", scope="hypatia",
    )
    second = csa.append_permanent_note_spawned(
        vault_path=vault, source_rel_path=rel,
        zettel_wikilink="[[zettel/X]]", scope="hypatia",
    )
    assert first is True
    assert second is False
    body = (vault / rel).read_text(encoding="utf-8")
    assert body.count("[[zettel/X]]") == 1


def test_append_perm_notes_multiple_distinct_links(
    tmp_path: Path,
) -> None:
    """E2E: append three distinct wikilinks → all present, in order
    of append."""
    vault = _make_vault(tmp_path)
    rel = _write_source_record(vault, "Meditations")
    for link in [
        "[[zettel/First]]", "[[zettel/Second]]", "[[zettel/Third]]",
    ]:
        csa.append_permanent_note_spawned(
            vault_path=vault, source_rel_path=rel,
            zettel_wikilink=link, scope="hypatia",
        )
    body = (vault / rel).read_text(encoding="utf-8")
    for link in [
        "[[zettel/First]]", "[[zettel/Second]]", "[[zettel/Third]]",
    ]:
        assert f"- {link}" in body


def test_append_perm_notes_pre_phase2_source_noop(tmp_path: Path) -> None:
    """Pre-Phase-2 source (no Permanent Notes spawned section) → returns
    False, body unchanged."""
    vault = _make_vault(tmp_path)
    rel = _write_source_record(
        vault, "Old Source",
        body="# Old\n\n## My Notes\n\n(notes)\n",
    )
    before_body = (vault / rel).read_text(encoding="utf-8")
    ok = csa.append_permanent_note_spawned(
        vault_path=vault, source_rel_path=rel,
        zettel_wikilink="[[zettel/X]]", scope="hypatia",
    )
    assert ok is False
    after_body = (vault / rel).read_text(encoding="utf-8")
    # Body content unchanged (frontmatter may rewrite identically).
    before_post = frontmatter.loads(before_body)
    after_post = frontmatter.loads(after_body)
    assert before_post.content == after_post.content


def test_append_perm_notes_missing_source_returns_false(
    tmp_path: Path,
) -> None:
    """Source record doesn't exist → returns False; no crash."""
    vault = _make_vault(tmp_path)
    ok = csa.append_permanent_note_spawned(
        vault_path=vault,
        source_rel_path="source/Nonexistent.md",
        zettel_wikilink="[[zettel/X]]",
        scope="hypatia",
    )
    assert ok is False


def test_append_perm_notes_handles_wikilink_input_form(
    tmp_path: Path,
) -> None:
    """Source path can be passed as wikilink form ``[[source/Title]]``;
    the helper strips brackets + appends .md."""
    vault = _make_vault(tmp_path)
    _write_source_record(vault, "Meditations")
    ok = csa.append_permanent_note_spawned(
        vault_path=vault,
        source_rel_path="[[source/Meditations]]",
        zettel_wikilink="[[zettel/X]]",
        scope="hypatia",
    )
    assert ok is True


# --- Integration via extract_notes_from_capture --------------------------


def _make_closed_session(state_mgr, short_id: str, rel_path: str) -> None:
    state_mgr.state.setdefault("closed_sessions", []).append({
        "session_id": f"{short_id}-uuid",
        "chat_id": 1,
        "started_at": "2026-05-17T10:00:00+00:00",
        "ended_at":   "2026-05-17T10:30:00+00:00",
        "reason": "explicit",
        "record_path": rel_path,
        "message_count": 5,
        "vault_ops": 0,
        "session_type": "capture",
        "continues_from": None,
        "opening_model": "claude-sonnet-4-6",
        "closing_model": "claude-sonnet-4-6",
    })
    state_mgr.save()


def _write_anchored_session(
    vault_path: Path, name: str,
    source_wikilink: str = "[[source/Meditations]]",
    author_wikilink: str = "[[author/Aurelius, Marcus]]",
) -> str:
    (vault_path / "session").mkdir(exist_ok=True, parents=True)
    rel = f"session/{name}.md"
    summary = (
        f"{capture_batch.SUMMARY_MARKER_START}\n\n"
        "## Structured Summary\n\n"
        "### Topics\n- stoicism\n\n"
        f"{capture_batch.SUMMARY_MARKER_END}\n\n"
    )
    body = (
        summary + "# Transcript\n\n"
        "**Andrew** (10:00): reading Meditations\n"
        "**Andrew** (10:01): the dichotomy of control\n"
    )
    (vault_path / rel).write_text(
        "---\n"
        "type: session\n"
        f"name: {name}\n"
        "created: '2026-05-17'\n"
        "session_type: capture\n"
        f'source: "{source_wikilink}"\n'
        f'author: "{author_wikilink}"\n'
        "---\n\n" + body,
        encoding="utf-8",
    )
    return rel


def _tool_use_note(name: str, body: str) -> FakeBlock:
    return FakeBlock(
        type="tool_use",
        id=f"toolu_{name[:8]}",
        name="create_note",
        input={
            "name": name, "body": body,
            "confidence_tier": "high",
            "source_quote": "test quote",
        },
    )


@pytest.mark.asyncio
async def test_extract_to_zettel_appends_to_perm_notes_spawned(
    state_mgr, tmp_path,
) -> None:
    """End-to-end: capture session with source anchor → zettel extraction
    → wikilink appended to source's ``## Permanent Notes spawned``
    section."""
    vault = _make_vault(tmp_path)
    # Pre-existing source with Phase 2 template body.
    source_rel = _write_source_record(vault, "Meditations")
    session_rel = _write_anchored_session(
        vault, "capture-2026-05-17-abc12345",
    )
    _make_closed_session(state_mgr, "abc12345", session_rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[
                _tool_use_note("Dichotomy of Control", "Body about control."),
            ],
            stop_reason="tool_use",
        )
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client, state=state_mgr, vault_path=vault,
        short_id="abc12345", model="claude-sonnet-4-6",
        agent_slug="hypatia", anchor_scope="hypatia",
    )

    assert result.skipped_reason == ""
    assert len(result.created_paths) == 1
    zettel_rel = result.created_paths[0]
    assert zettel_rel.startswith("zettel/")

    # Source body now carries the wikilink.
    src_body = (vault / source_rel).read_text(encoding="utf-8")
    zettel_no_md = zettel_rel[:-3]  # strip .md
    expected_link = f"[[{zettel_no_md}]]"
    assert expected_link in src_body, (
        f"expected wikilink {expected_link!r} in source body; got:\n{src_body}"
    )
    # Specifically present under Permanent Notes spawned section
    # (not e.g. accidentally appended at end of file).
    perm_idx = src_body.index("## Permanent Notes spawned")
    link_idx = src_body.index(expected_link)
    ext_idx = src_body.index("# External References")
    assert perm_idx < link_idx < ext_idx


@pytest.mark.asyncio
async def test_extract_to_note_does_NOT_append_to_perm_notes(
    state_mgr, tmp_path,
) -> None:
    """Regression-pin: when extraction routes to note/ (Salem default
    OR Hypatia + unanchored), the Permanent Notes spawned append does
    NOT fire. The rule is zettel-specific per the locked plan.

    Setup: Salem capture session (anchor_scope="") → target_type=note.
    Even if a source happens to be referenced, the append shouldn't fire.
    """
    vault = _make_vault(tmp_path)
    source_rel = _write_source_record(vault, "Some Salem Source")
    # Salem-shape session — no source/author frontmatter; the discriminator
    # routes to note/ regardless.
    session_rel = _write_anchored_session(
        vault, "Voice Session — 2026-05-17 def67890",
        source_wikilink="",  # no anchor
        author_wikilink="",
    )
    # Wait — the helper sets source/author frontmatter even with empty
    # wikilinks. Let's strip the frontmatter and rewrite by hand.
    (vault / session_rel).write_text(
        "---\n"
        "type: session\n"
        "name: Voice Session — 2026-05-17 def67890\n"
        "created: '2026-05-17'\n"
        "session_type: capture\n"
        "---\n\n"
        f"{capture_batch.SUMMARY_MARKER_START}\n"
        "## Structured Summary\n### Topics\n- x\n"
        f"{capture_batch.SUMMARY_MARKER_END}\n\n"
        "# Transcript\n\n**Andrew** (10:00): rambling\n",
        encoding="utf-8",
    )
    _make_closed_session(state_mgr, "def67890", session_rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[_tool_use_note("Salem Note", "Body.")],
            stop_reason="tool_use",
        )
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client, state=state_mgr, vault_path=vault,
        short_id="def67890", model="claude-sonnet-4-6",
        agent_slug="salem", anchor_scope="",  # Salem
    )

    assert result.skipped_reason == ""
    note_rel = result.created_paths[0]
    assert note_rel.startswith("note/"), (
        f"Salem path should produce note/, got {note_rel}"
    )

    # Source body's Permanent Notes spawned should be EMPTY (no
    # wikilink appended for note/ records).
    src_body = (vault / source_rel).read_text(encoding="utf-8")
    note_no_md = note_rel[:-3]
    forbidden_link = f"[[{note_no_md}]]"
    assert forbidden_link not in src_body, (
        f"note/ record's wikilink should NOT have been appended to "
        f"source's Permanent Notes spawned; got:\n{src_body}"
    )


@pytest.mark.asyncio
async def test_extract_multi_zettel_batch_appends_each(
    state_mgr, tmp_path,
) -> None:
    """Extracting multiple zettels from one session → each one's
    wikilink appended to the source. Idempotency tested separately."""
    vault = _make_vault(tmp_path)
    source_rel = _write_source_record(vault, "Meditations")
    session_rel = _write_anchored_session(
        vault, "capture-2026-05-17-multi-ghij7890",
    )
    _make_closed_session(state_mgr, "ghij7890", session_rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[
                _tool_use_note("First Insight", "Body 1."),
                _tool_use_note("Second Insight", "Body 2."),
                _tool_use_note("Third Insight", "Body 3."),
            ],
            stop_reason="tool_use",
        )
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client, state=state_mgr, vault_path=vault,
        short_id="ghij7890", model="claude-sonnet-4-6",
        agent_slug="hypatia", anchor_scope="hypatia",
    )

    assert len(result.created_paths) == 3
    src_body = (vault / source_rel).read_text(encoding="utf-8")
    # All three wikilinks present in source body.
    for zettel_rel in result.created_paths:
        zettel_no_md = zettel_rel[:-3]
        assert f"[[{zettel_no_md}]]" in src_body


# --- WARN-1 hardening regression: line-anchored section detection -------


def test_perm_notes_rewriter_does_not_false_match_h3() -> None:
    """WARN-1 regression-pin (2026-05-17). Body containing an H3
    heading like ``### Permanent Notes spawned Yesterday`` must NOT
    cause the rewriter to false-match on the substring ``##
    Permanent Notes spawned`` at offset+1 within the H3 line.

    Pre-hardening shape: ``body.find("## Permanent Notes spawned")``
    would lock onto the H3-line's offset+1 (because ``### Foo`` =
    ``#`` + ``## Foo``) and corrupt subsequent section-bounded
    operations.

    Post-hardening: ``_find_h2_section_start`` enforces
    line-anchored detection — the H3 doesn't match.

    The fixture body has NO real ``## Permanent Notes spawned`` H2
    heading. Post-hardening, the rewriter detects no section and
    returns body unchanged (the canonical "no section → no-op" path).
    """
    # Body with an H3 that contains the substring "## Permanent Notes
    # spawned" at offset+1 within the H3 line — but NO real H2 heading.
    body = (
        "# Source Details\n\n"
        "## Bibliographic Details\n\n"
        "# Notes\n\n"
        "### Permanent Notes spawned Yesterday\n\n"  # H3 — must NOT false-match
        "Some prior content.\n\n"
        "# External References\n"
    )
    rewriter = csa._build_permanent_notes_rewriter("[[zettel/X]]")
    result = rewriter(body)
    # No real H2 ``## Permanent Notes spawned`` → rewriter no-ops.
    assert result == body, (
        f"H3 ``### Permanent Notes spawned Yesterday`` false-matched "
        f"the H2 anchor; rewriter corrupted body."
    )
