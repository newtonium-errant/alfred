"""Anchor preservation tests — Phase 2 deliverable #2 (2026-05-17).

When the operator dictates a positional anchor near a claim during
capture (e.g., "p. 23 says..." / "at the 15-minute mark..." /
"in paragraph 3..."), the extraction LLM emits the anchor in the
``source_anchor`` field of the ``create_note`` tool call. The
extraction loop preserves it on the spawned zettel BOTH as:

  1. ``source_anchor:`` frontmatter (queryable surface)
  2. Inline ``(<anchor>)`` annotation prepended to the body text
     (human-readable in the rendered note)

These tests pin both surfaces + the absence behaviour (no anchor →
no inline annotation, no frontmatter field).

LLM-shape note (per the brief): anchor parsing is LLM-shaped, not
regex-shaped. The extraction prompt teaches the LLM to recognize
"p. 23", "the 15-minute mark", "paragraph 3" → emit normalized
``source_anchor`` value. These tests mock the LLM output directly
(via FakeAnthropicClient) — they do NOT test the LLM's recognition
quality (that's a prompt-tuner / vault-reviewer concern). The pin is:
when the LLM emits a non-empty ``source_anchor``, the extraction
flow propagates it to both surfaces; when empty / absent, both
surfaces stay clean.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from alfred.telegram import capture_batch, capture_extract
from tests.telegram.conftest import (
    FakeAnthropicClient, FakeBlock, FakeResponse,
)


# --- Helpers (mirror test_capture_extract.py shape) ---------------------


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
    *,
    source_wikilink: str = "[[source/Meditations]]",
    author_wikilink: str = "[[author/Aurelius, Marcus]]",
) -> str:
    """Build a Hypatia capture session with source-anchor frontmatter so
    the discriminator routes to zettel/."""
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
        "**Andrew** (10:01): on page 23 Marcus talks about the dichotomy\n"
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


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for sub in ("session", "zettel", "note", "source", "author"):
        (vault / sub).mkdir(parents=True)
    return vault


def _tool_use_note(
    name: str,
    body: str,
    *,
    source_anchor: str = "",
    confidence_tier: str = "high",
    source_quote: str = "Marcus on page 23 talks about the dichotomy",
) -> FakeBlock:
    """Build a create_note tool_use block with optional source_anchor."""
    input_dict = {
        "name": name,
        "body": body,
        "confidence_tier": confidence_tier,
        "source_quote": source_quote,
    }
    if source_anchor:
        # Only include the field when non-empty so we can also test the
        # "field absent from tool_use call" path.
        input_dict["source_anchor"] = source_anchor
    return FakeBlock(
        type="tool_use",
        id=f"toolu_{name[:8]}",
        name="create_note",
        input=input_dict,
    )


# --- Tool schema pin ----------------------------------------------------


def test_extract_tool_schema_includes_source_anchor() -> None:
    """The ``_EXTRACT_TOOL`` schema advertises the optional
    ``source_anchor`` field so the LLM knows it can emit anchors."""
    schema = capture_extract._EXTRACT_TOOL["input_schema"]
    assert "source_anchor" in schema["properties"]
    # Optional — NOT in required.
    assert "source_anchor" not in schema["required"]
    # Type is string.
    assert schema["properties"]["source_anchor"]["type"] == "string"


def test_extract_system_prompt_teaches_anchor_preservation() -> None:
    """The extraction system prompt carries the ANCHOR PRESERVATION
    section + worked examples so the LLM knows when to emit anchors."""
    prompt = capture_extract._EXTRACT_SYSTEM_PROMPT
    assert "ANCHOR PRESERVATION" in prompt
    # Worked examples covering at least 3 of the 4 anchor format
    # families (book / video / article / conversation).
    assert "p.23" in prompt  # book
    assert "0:15:30" in prompt or "0:15:00" in prompt  # video / podcast
    assert "¶" in prompt  # article
    # "When in doubt, leave empty" discipline.
    assert "empty" in prompt.lower()


# --- _note_body anchor-inline behaviour ---------------------------------


def test_note_body_prepends_inline_anchor_when_present() -> None:
    """``_note_body`` prepends ``(<anchor>) `` to body text when the
    ``source_anchor`` kwarg is non-empty."""
    rendered = capture_extract._note_body(
        body="The dichotomy of control is foundational.",
        source_quote="dichotomy of control",
        source_session_rel="session/capture-foo.md",
        source_anchor="p.23",
    )
    # Body line starts with the inline anchor.
    first_line = rendered.split("\n")[0]
    assert first_line == "(p.23) The dichotomy of control is foundational."


def test_note_body_omits_inline_anchor_when_empty() -> None:
    """No ``source_anchor`` kwarg → no inline annotation; backwards-
    compatible with pre-Phase-2 callers."""
    rendered = capture_extract._note_body(
        body="A standalone thought.",
        source_quote="standalone thought",
        source_session_rel="session/capture-foo.md",
    )
    first_line = rendered.split("\n")[0]
    assert first_line == "A standalone thought."
    # No leftover `()` anywhere.
    assert "()" not in rendered


@pytest.mark.parametrize("anchor,expected_prefix", [
    ("p.23",         "(p.23) "),
    ("0:15:30",      "(0:15:30) "),
    ("¶3",           "(¶3) "),
    ("slide 12",     "(slide 12) "),
    ("§2.3",         "(§2.3) "),
])
def test_note_body_inline_anchor_format_variations(
    anchor: str, expected_prefix: str,
) -> None:
    """Per-source-type anchor formats all render with the same
    ``(<anchor>) `` inline prefix shape."""
    rendered = capture_extract._note_body(
        body="Body text.",
        source_quote="",
        source_session_rel="session/x.md",
        source_anchor=anchor,
    )
    first_line = rendered.split("\n")[0]
    assert first_line.startswith(expected_prefix)


def test_note_body_strips_whitespace_in_anchor() -> None:
    """Leading/trailing whitespace in anchor is stripped before inline
    formatting (defensive against LLM emitting `` p.23 ``)."""
    rendered = capture_extract._note_body(
        body="Body.",
        source_quote="",
        source_session_rel="session/x.md",
        source_anchor="  p.23  ",
    )
    first_line = rendered.split("\n")[0]
    assert first_line == "(p.23) Body."


# --- End-to-end: extraction propagates anchor to zettel ------------------


@pytest.mark.asyncio
async def test_extract_propagates_anchor_to_zettel_frontmatter_and_body(
    state_mgr, tmp_path,
) -> None:
    """End-to-end: capture session → LLM emits source_anchor=``p.23`` →
    spawned zettel carries:
      * ``source_anchor: "p.23"`` frontmatter
      * inline ``(p.23) `` annotation at body start
    """
    vault = _make_vault(tmp_path)
    rel = _write_anchored_session(
        vault, "capture-2026-05-17-anchor-test-abcd1234",
    )
    _make_closed_session(state_mgr, "abcd1234", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[
                _tool_use_note(
                    "Dichotomy of Control as Foundation",
                    "Marcus returns to this principle repeatedly.",
                    source_anchor="p.23",
                ),
            ],
            stop_reason="tool_use",
        )
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client, state=state_mgr, vault_path=vault,
        short_id="abcd1234", model="claude-sonnet-4-6",
        agent_slug="hypatia", anchor_scope="hypatia",
    )

    assert result.skipped_reason == ""
    assert len(result.created_paths) == 1
    zettel_rel = result.created_paths[0]
    assert zettel_rel.startswith("zettel/")

    # Frontmatter pin.
    post = frontmatter.load(vault / zettel_rel)
    assert post["source_anchor"] == "p.23"

    # Inline body annotation pin.
    body = post.content
    # First substantive line carries the inline anchor.
    first_substantive = next(
        ln for ln in body.splitlines() if ln.strip()
    )
    assert first_substantive.startswith("(p.23) "), (
        f"expected inline anchor at body start; got: {first_substantive!r}"
    )


@pytest.mark.asyncio
async def test_extract_omits_anchor_when_llm_doesnt_emit_one(
    state_mgr, tmp_path,
) -> None:
    """Regression-pin: when the LLM emits no ``source_anchor`` (or empty
    string), the spawned zettel has NO ``source_anchor`` frontmatter
    field and NO inline body annotation. The capture-extract flow
    must not invent anchors."""
    vault = _make_vault(tmp_path)
    rel = _write_anchored_session(
        vault, "capture-2026-05-17-no-anchor-bcde2345",
    )
    _make_closed_session(state_mgr, "bcde2345", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[
                _tool_use_note(
                    "A General Reflection",
                    "Some musing without a positional reference.",
                    # No source_anchor kwarg → field absent from tool_use input.
                ),
            ],
            stop_reason="tool_use",
        )
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client, state=state_mgr, vault_path=vault,
        short_id="bcde2345", model="claude-sonnet-4-6",
        agent_slug="hypatia", anchor_scope="hypatia",
    )

    assert result.skipped_reason == ""
    zettel_rel = result.created_paths[0]
    post = frontmatter.load(vault / zettel_rel)
    # No source_anchor field on the spawned record.
    assert "source_anchor" not in post.metadata
    # No leading `(` in the body — no inline annotation.
    body = post.content.lstrip()
    assert not body.startswith("("), (
        f"body should not start with '(' when no anchor; got: {body[:60]!r}"
    )


@pytest.mark.asyncio
async def test_extract_empty_string_anchor_is_omitted(
    state_mgr, tmp_path,
) -> None:
    """When the LLM emits ``source_anchor=""`` (explicit empty), the
    extraction flow treats it as "no anchor" — no frontmatter field,
    no inline annotation. Matches the brief's "false anchors are worse
    than missing anchors" discipline."""
    vault = _make_vault(tmp_path)
    rel = _write_anchored_session(
        vault, "capture-2026-05-17-empty-anchor-cdef3456",
    )
    _make_closed_session(state_mgr, "cdef3456", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[
                FakeBlock(
                    type="tool_use",
                    id="toolu_x",
                    name="create_note",
                    input={
                        "name": "Empty Anchor Test",
                        "body": "Body text.",
                        "confidence_tier": "high",
                        "source_quote": "quote",
                        "source_anchor": "",  # explicit empty
                    },
                ),
            ],
            stop_reason="tool_use",
        )
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client, state=state_mgr, vault_path=vault,
        short_id="cdef3456", model="claude-sonnet-4-6",
        agent_slug="hypatia", anchor_scope="hypatia",
    )

    zettel_rel = result.created_paths[0]
    post = frontmatter.load(vault / zettel_rel)
    assert "source_anchor" not in post.metadata
    assert not post.content.lstrip().startswith("(")


@pytest.mark.asyncio
async def test_extract_multiple_zettels_each_with_own_anchor(
    state_mgr, tmp_path,
) -> None:
    """Two zettels from one session, each with a distinct anchor.
    Validates the per-call anchor propagation isn't entangled across
    notes in the same extraction batch."""
    vault = _make_vault(tmp_path)
    rel = _write_anchored_session(
        vault, "capture-2026-05-17-multi-anchor-defg4567",
    )
    _make_closed_session(state_mgr, "defg4567", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[
                _tool_use_note(
                    "First Claim", "First body.",
                    source_anchor="p.23",
                ),
                _tool_use_note(
                    "Second Claim", "Second body.",
                    source_anchor="p.45",
                ),
            ],
            stop_reason="tool_use",
        )
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client, state=state_mgr, vault_path=vault,
        short_id="defg4567", model="claude-sonnet-4-6",
        agent_slug="hypatia", anchor_scope="hypatia",
    )

    assert len(result.created_paths) == 2
    posts = [
        frontmatter.load(vault / p) for p in result.created_paths
    ]
    anchors = {str(p["source_anchor"]) for p in posts}
    assert anchors == {"p.23", "p.45"}
    # Each body's inline annotation matches its own anchor.
    for post in posts:
        anchor = str(post["source_anchor"])
        body = post.content.lstrip()
        assert body.startswith(f"({anchor}) "), (
            f"anchor mismatch — body starts with {body[:30]!r}, "
            f"frontmatter anchor is {anchor!r}"
        )
