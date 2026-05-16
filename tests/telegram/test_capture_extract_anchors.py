"""Integration tests for capture-source-anchor wiring in capture_extract.

Covers the contract between the capture-batch orchestrator (which
writes ``source`` / ``author`` wikilinks onto the session record) and
the extraction step (which reads them and threads into derived-note
``related`` lists + fires the within-session cross-link pass).

Separate file from ``test_capture_extract.py`` to keep the scope of
each test module narrow.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.telegram import capture_batch, capture_extract
from alfred.vault import ops
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


# --- Helpers --------------------------------------------------------------


def _seed_closed_session(state_mgr, short_id: str, rel_path: str) -> None:
    state_mgr.state.setdefault("closed_sessions", []).append({
        "session_id": f"{short_id}-full-uuid",
        "chat_id": 1,
        "record_path": rel_path,
        "session_type": "capture",
    })
    state_mgr.save()


def _write_anchored_session(
    vault: Path,
    name: str,
    *,
    source_wikilink: str = "",
    author_wikilink: str = "",
) -> str:
    (vault / "session").mkdir(parents=True, exist_ok=True)
    rel = f"session/{name}.md"
    fm_lines = [
        "type: session",
        "status: completed",
        f"name: {name}",
        "created: '2026-05-16'",
        "session_type: capture",
    ]
    if source_wikilink:
        fm_lines.append(f'source: "{source_wikilink}"')
    if author_wikilink:
        fm_lines.append(f'author: "{author_wikilink}"')

    summary = (
        f"{capture_batch.SUMMARY_MARKER_START}\n\n"
        "## Structured Summary\n\n"
        "### Topics\n- stoicism\n- duty\n\n"
        f"{capture_batch.SUMMARY_MARKER_END}\n\n"
    )

    body = summary + "# Transcript\n\n**Andrew** (10:00): rambling\n"
    (vault / rel).write_text(
        "---\n" + "\n".join(fm_lines) + "\n---\n\n" + body,
        encoding="utf-8",
    )
    return rel


def _note_tool_block(name: str, body: str, quote: str) -> FakeBlock:
    return FakeBlock(
        type="tool_use",
        id=f"t_{name[:8]}",
        name="create_note",
        input={
            "name": name,
            "body": body,
            "confidence_tier": "high",
            "source_quote": quote,
        },
    )


# --- Source/author propagation -------------------------------------------


@pytest.mark.asyncio
async def test_extract_propagates_source_and_author_to_derived_notes(
    tmp_path: Path, state_mgr,
) -> None:
    """When session has source+author wikilinks, derived notes inherit them."""
    vault = tmp_path / "vault"
    for sub in ("session", "note", "source", "author"):
        (vault / sub).mkdir(parents=True)
    # The source/author records don't need to exist on disk for the
    # extraction step — the wikilinks are mere strings until something
    # follows them; we're testing propagation, not link validity.

    rel = _write_anchored_session(
        vault, "capture-2026-05-16-meditations-abc12345",
        source_wikilink="[[source/Meditations]]",
        author_wikilink="[[author/Aurelius]]",
    )
    _seed_closed_session(state_mgr, "abc12345", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[_note_tool_block(
                "Roman Stoicism on Duty",
                "Duty is the central practice in Roman Stoicism.",
                "duty is what stoicism is about",
            )],
            stop_reason="tool_use",
        ),
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client,
        state=state_mgr,
        vault_path=vault,
        short_id="abc12345",
        model="claude-sonnet-4-6",
        agent_slug="hypatia",
    )
    assert len(result.created_paths) == 1

    note = ops.vault_read(vault, result.created_paths[0])
    related = note["frontmatter"].get("related") or []
    assert "[[source/Meditations]]" in related
    assert "[[author/Aurelius]]" in related


@pytest.mark.asyncio
async def test_extract_ignores_freetext_author_field(
    tmp_path: Path, state_mgr,
) -> None:
    """Legacy free-text ``author: Carlo Atendido`` is NOT copied as a wikilink."""
    vault = tmp_path / "vault"
    for sub in ("session", "note"):
        (vault / sub).mkdir(parents=True)

    rel = f"session/capture.md"
    (vault / rel).write_text(
        "---\n"
        "type: session\n"
        "name: capture\n"
        "created: '2026-05-16'\n"
        "session_type: capture\n"
        "author: Carlo Atendido\n"  # free-text, not wikilink
        "---\n\n"
        f"{capture_batch.SUMMARY_MARKER_START}\n## Structured Summary\n"
        f"### Topics\n- drills\n\n{capture_batch.SUMMARY_MARKER_END}\n\n"
        "# Transcript\n\n**Andrew** (10:00): drills\n",
        encoding="utf-8",
    )
    _seed_closed_session(state_mgr, "xyz", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[_note_tool_block(
                "Drill Note", "body.", "drills",
            )],
            stop_reason="tool_use",
        ),
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client,
        state=state_mgr,
        vault_path=vault,
        short_id="xyz",
        model="claude-sonnet-4-6",
    )
    assert len(result.created_paths) == 1
    note = ops.vault_read(vault, result.created_paths[0])
    # The free-text author MUST NOT have been wrapped into a broken
    # wikilink — `related` should be absent or not contain the author.
    related = note["frontmatter"].get("related") or []
    assert not any("Carlo Atendido" in str(x) for x in related)


# --- Within-session peer cross-link --------------------------------------


@pytest.mark.asyncio
async def test_extract_cross_links_peers_with_shared_tokens(
    tmp_path: Path, state_mgr,
) -> None:
    """Two derived notes sharing 2+ substantive title tokens → linked."""
    vault = tmp_path / "vault"
    for sub in ("session", "note", "source"):
        (vault / sub).mkdir(parents=True)

    rel = _write_anchored_session(
        vault, "capture-2026-05-16-stoicism-xyz",
        source_wikilink="[[source/Meditations]]",
    )
    _seed_closed_session(state_mgr, "xyz", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[
                _note_tool_block(
                    "Roman Stoicism Origins",
                    "Origin of Roman Stoicism.",
                    "stoicism",
                ),
                _note_tool_block(
                    "Roman Stoicism in Practice",
                    "How to apply Roman Stoicism.",
                    "stoicism",
                ),
                # This one shares only "Greek" — no peer link expected.
                _note_tool_block(
                    "Greek Tragedy Footnote",
                    "Tragedy as moral instruction.",
                    "tragedy",
                ),
            ],
            stop_reason="tool_use",
        ),
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client,
        state=state_mgr,
        vault_path=vault,
        short_id="xyz",
        model="claude-sonnet-4-6",
    )
    assert len(result.created_paths) == 3

    # The two Roman Stoicism notes should reference each other; the
    # Greek Tragedy note should NOT carry peer links.
    note_a = ops.vault_read(vault, "note/Roman Stoicism Origins.md")
    related_a = note_a["frontmatter"].get("related") or []
    assert any("Roman Stoicism in Practice" in str(x) for x in related_a)
    assert any("source/Meditations" in str(x) for x in related_a)

    note_b = ops.vault_read(vault, "note/Roman Stoicism in Practice.md")
    related_b = note_b["frontmatter"].get("related") or []
    assert any("Roman Stoicism Origins" in str(x) for x in related_b)

    note_c = ops.vault_read(vault, "note/Greek Tragedy Footnote.md")
    related_c = note_c["frontmatter"].get("related") or []
    # Anchor (source) carried through even when no peer link was forged.
    assert any("source/Meditations" in str(x) for x in related_c)
    # No Roman wikilinks on the Greek Tragedy note.
    assert not any("Roman Stoicism" in str(x) for x in related_c)


@pytest.mark.asyncio
async def test_extract_no_cross_link_when_only_one_note(
    tmp_path: Path, state_mgr,
) -> None:
    """A single derived note has nothing to cross-link to."""
    vault = tmp_path / "vault"
    for sub in ("session", "note"):
        (vault / sub).mkdir(parents=True)

    rel = _write_anchored_session(vault, "capture-solo")
    _seed_closed_session(state_mgr, "solo123", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[_note_tool_block("Solo Note", "body.", "q")],
            stop_reason="tool_use",
        ),
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client,
        state=state_mgr,
        vault_path=vault,
        short_id="solo123",
        model="claude-sonnet-4-6",
    )
    assert len(result.created_paths) == 1
    # No peer to cross-link to — `related` may be empty (no anchors set
    # on this session either) but the test just verifies the cross-link
    # branch didn't crash.
