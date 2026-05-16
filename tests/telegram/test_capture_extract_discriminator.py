"""Three-tier discriminator tests — capture-extract target routing.

Phase 1.x rework (2026-05-16). Andrew's correction: "Not all Hypatia
notes are zettels. Not all capture sessions are zettels either. Notes
need to exist as well, as my non-zettelkasten held 'fleeting notes'."

The discriminator picks the extract target type from three inputs:
  * ``anchor_scope`` — instance identity ("hypatia" vs everything else)
  * ``source_anchored`` — does the session have ``source:`` / ``author:``
    wikilink frontmatter?
  * ``operator_override`` — explicit ``/end-zettel`` / ``/end-note``
    choice; ``None`` when the operator used plain ``/end``

Final routing matrix (Hypatia rows; Salem always note/):

| source_anchored | override   | result   |
|-----------------|------------|----------|
| True            | None       | zettel/  |
| True            | "note"     | note/    |
| True            | "zettel"   | zettel/  |
| False           | None       | note/    |
| False           | "zettel"   | zettel/  |
| False           | "note"     | note/    |

The memo branch (≤1 user message → memo/) lives in
``capture_batch.process_capture_session`` and runs BEFORE the
extractor — this discriminator only applies on the multi-message path.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest
import structlog

from alfred.telegram import capture_batch, capture_extract
from tests.telegram.conftest import (
    FakeAnthropicClient, FakeBlock, FakeResponse,
)


# --- _resolve_extract_target_type unit tests -----------------------------


@pytest.mark.parametrize("anchor_scope,source_anchored,override,expected", [
    # ---- Hypatia + no override → source-anchored discriminator ----
    ("hypatia", True,  None,     "zettel"),
    ("hypatia", False, None,     "note"),
    # ---- Hypatia + override "zettel" → forced zettel/ ----
    ("hypatia", True,  "zettel", "zettel"),
    ("hypatia", False, "zettel", "zettel"),
    # ---- Hypatia + override "note" → forced note/ ----
    ("hypatia", True,  "note",   "note"),
    ("hypatia", False, "note",   "note"),
    # ---- Salem + anything → always note/ (scope-gated) ----
    ("",       True,  None,     "note"),
    ("",       False, None,     "note"),
    ("",       True,  "zettel", "note"),  # override ignored for Salem
    ("",       False, "zettel", "note"),
    ("",       True,  "note",   "note"),
    # ---- Unknown / future scope → note/ (defensive) ----
    ("vera",   True,  None,     "note"),
    ("kalle",  False, "zettel", "note"),  # override ignored
    # ---- Invalid override values → treated as None ----
    ("hypatia", True,  "garbage",   "zettel"),  # invalid → use source-anchor
    ("hypatia", False, "everything", "note"),
    ("hypatia", True,  "",        "zettel"),    # empty string → use anchor
])
def test_resolve_extract_target_type(
    anchor_scope: str,
    source_anchored: bool,
    override: str | None,
    expected: str,
) -> None:
    """The 3×2×3 matrix of (anchor_scope, source_anchored, override)
    resolves to the right target type."""
    actual = capture_extract._resolve_extract_target_type(
        anchor_scope,
        source_anchored=source_anchored,
        operator_override=override,
    )
    assert actual == expected, (
        f"Mismatch for (scope={anchor_scope!r}, anchored={source_anchored}, "
        f"override={override!r}): expected {expected!r}, got {actual!r}"
    )


def test_legacy_extract_target_type_still_works() -> None:
    """The deprecated ``_extract_target_type`` shim routes through the
    new discriminator with source_anchored=True, matching the prior
    Phase 1 behaviour (Hypatia → zettel; everyone else → note).

    Regression-pin: any external caller still importing this function
    should continue to work.
    """
    assert capture_extract._extract_target_type("hypatia") == "zettel"
    assert capture_extract._extract_target_type("") == "note"
    assert capture_extract._extract_target_type("salem") == "note"
    assert capture_extract._extract_target_type("kalle") == "note"


def test_operator_override_values_constant() -> None:
    """The override allowlist is exactly ``{"zettel", "note"}``."""
    assert capture_extract._OPERATOR_OVERRIDE_VALUES == frozenset(
        {"zettel", "note"}
    )


# --- Integration tests — fixture helpers ---------------------------------


def _make_closed_session(state_mgr, short_id: str, rel_path: str) -> None:
    state_mgr.state.setdefault("closed_sessions", []).append({
        "session_id": f"{short_id}-full-session-uuid",
        "chat_id": 1,
        "started_at": "2026-05-16T10:00:00+00:00",
        "ended_at":   "2026-05-16T10:30:00+00:00",
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


def _write_session_record(
    vault_path: Path,
    name: str,
    *,
    source_wikilink: str = "",
    author_wikilink: str = "",
    extract_target_override: str = "",
) -> str:
    """Build a closed-capture session record.

    Optional ``source_wikilink`` + ``author_wikilink`` frontmatter
    drives the source-anchored discriminator. Optional
    ``extract_target_override`` simulates the ``/end-zettel`` /
    ``/end-note`` close path.
    """
    (vault_path / "session").mkdir(exist_ok=True, parents=True)
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
    if extract_target_override:
        fm_lines.append(
            f'capture_extract_target_override: "{extract_target_override}"'
        )

    summary = (
        f"{capture_batch.SUMMARY_MARKER_START}\n\n"
        "## Structured Summary\n\n"
        "### Topics\n- reflection\n\n"
        f"{capture_batch.SUMMARY_MARKER_END}\n\n"
    )
    body = (
        summary
        + "# Transcript\n\n"
        + "**Andrew** (10:00 · voice): turning over this idea today\n"
        + "**Andrew** (10:01 · voice): the implication being…\n"
    )
    (vault_path / rel).write_text(
        "---\n" + "\n".join(fm_lines) + "\n---\n\n" + body,
        encoding="utf-8",
    )
    return rel


def _tool_use_note_block(name: str, body: str) -> FakeBlock:
    return FakeBlock(
        type="tool_use",
        id=f"toolu_{name[:8]}",
        name="create_note",
        input={
            "name": name,
            "body": body,
            "confidence_tier": "high",
            "source_quote": "the implication being",
        },
    )


def _make_hypatia_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for sub in ("session", "zettel", "note", "source", "author"):
        (vault / sub).mkdir(parents=True)
    return vault


# --- Integration: Hypatia + source-anchor + no override → zettel/ -------


@pytest.mark.asyncio
async def test_hypatia_source_anchored_no_override_creates_zettel(
    state_mgr, tmp_path,
) -> None:
    """Hypatia session with ``source:`` wikilink and no operator
    override → zettel/ (default discriminator)."""
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_session_record(
        vault, "capture-2026-05-16-anchored-abcd1234",
        source_wikilink="[[source/Meditations]]",
        author_wikilink="[[author/Aurelius, Marcus]]",
    )
    _make_closed_session(state_mgr, "abcd1234", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[_tool_use_note_block("Reflection On Control",
                                         "A note about control.")],
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
    assert result.created_paths[0].startswith("zettel/")


# --- Integration: Hypatia + no anchor + no override → note/ --------------


@pytest.mark.asyncio
async def test_hypatia_unanchored_no_override_creates_note(
    state_mgr, tmp_path,
) -> None:
    """Hypatia session WITHOUT source/author wikilinks and no override
    → note/ (the fleeting-notes path Andrew flagged in the rework)."""
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_session_record(
        vault, "capture-2026-05-16-unanchored-bcde2345",
        # No source/author frontmatter — operator just rambled.
    )
    _make_closed_session(state_mgr, "bcde2345", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[_tool_use_note_block("Random Thought", "Body.")],
            stop_reason="tool_use",
        )
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client, state=state_mgr, vault_path=vault,
        short_id="bcde2345", model="claude-sonnet-4-6",
        agent_slug="hypatia", anchor_scope="hypatia",
    )

    assert result.skipped_reason == ""
    assert len(result.created_paths) == 1
    # Unanchored Hypatia falls to note/ — the fleeting-notes path.
    assert result.created_paths[0].startswith("note/"), (
        f"expected note/ for unanchored Hypatia, got {result.created_paths[0]}"
    )


# --- Integration: Hypatia + override "zettel" → forces zettel/ -----------


@pytest.mark.asyncio
async def test_hypatia_unanchored_override_zettel_forces_zettel(
    state_mgr, tmp_path,
) -> None:
    """``/end-zettel`` override forces zettel/ even when the session
    has no source-anchor wikilinks (operator deliberately elevates a
    free-thought session to a permanent zettel)."""
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_session_record(
        vault, "capture-2026-05-16-end-zettel-cdef3456",
        # No anchor wikilinks.
        extract_target_override="zettel",  # operator-set override
    )
    _make_closed_session(state_mgr, "cdef3456", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[_tool_use_note_block("Elevated Reflection", "Body.")],
            stop_reason="tool_use",
        )
    ])

    # No explicit operator_override kwarg — read from frontmatter.
    result = await capture_extract.extract_notes_from_capture(
        client=client, state=state_mgr, vault_path=vault,
        short_id="cdef3456", model="claude-sonnet-4-6",
        agent_slug="hypatia", anchor_scope="hypatia",
    )

    assert result.skipped_reason == ""
    assert result.created_paths[0].startswith("zettel/")


# --- Integration: Hypatia + anchored + override "note" → forces note/ ---


@pytest.mark.asyncio
async def test_hypatia_anchored_override_note_forces_note(
    state_mgr, tmp_path,
) -> None:
    """``/end-note`` override forces note/ even when the session has
    source-anchor wikilinks (operator caught a wrong anchor or wants
    the record as fleeting)."""
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_session_record(
        vault, "capture-2026-05-16-end-note-defg4567",
        source_wikilink="[[source/Meditations]]",
        author_wikilink="[[author/Aurelius, Marcus]]",
        extract_target_override="note",  # operator-set override
    )
    _make_closed_session(state_mgr, "defg4567", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[_tool_use_note_block("Demoted To Note", "Body.")],
            stop_reason="tool_use",
        )
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client, state=state_mgr, vault_path=vault,
        short_id="defg4567", model="claude-sonnet-4-6",
        agent_slug="hypatia", anchor_scope="hypatia",
    )

    assert result.skipped_reason == ""
    assert result.created_paths[0].startswith("note/")


# --- Integration: Salem + any anchor + any override → always note/ -------


@pytest.mark.asyncio
@pytest.mark.parametrize("source_anchor,override", [
    ("",                       ""),
    ("[[source/Random]]",      ""),
    ("",                       "zettel"),
    ("[[source/Meditations]]", "zettel"),
    ("",                       "note"),
    ("[[source/Foo]]",         "note"),
])
async def test_salem_scope_always_creates_note_regardless(
    state_mgr, tmp_path,
    source_anchor: str, override: str,
) -> None:
    """Salem (anchor_scope="") always lands records in note/, regardless
    of source-anchor state or operator override. Scope-gated: Salem's
    create-allowlist doesn't carry zettel, so the override would fail
    at vault_create anyway. Returning note/ from the discriminator
    is the contract-honest path.
    """
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_session_record(
        vault, f"Voice Session - {source_anchor or 'none'}-{override or 'none'}",
        source_wikilink=source_anchor,
        extract_target_override=override,
    )
    short_id = f"slm{len(source_anchor)+len(override):05d}"
    _make_closed_session(state_mgr, short_id, rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[_tool_use_note_block("Salem Note", "Body.")],
            stop_reason="tool_use",
        )
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client, state=state_mgr, vault_path=vault,
        short_id=short_id, model="claude-sonnet-4-6",
        agent_slug="salem", anchor_scope="",  # Salem default
    )

    assert result.skipped_reason == ""
    assert result.created_paths[0].startswith("note/"), (
        f"Salem path produced non-note record: {result.created_paths[0]} "
        f"(source={source_anchor!r}, override={override!r})"
    )


# --- Observability — log emits the discriminator inputs ------------------


@pytest.mark.asyncio
async def test_done_log_carries_discriminator_inputs(
    state_mgr, tmp_path,
) -> None:
    """``talker.extract.done`` log records target_type + source_anchored
    + operator_override so the operator can grep per-tier extraction
    activity (per builder.md pre-commit checklist #9 — log-emission
    tests must drive the production code path)."""
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_session_record(
        vault, "capture-log-test-efgh5678",
        source_wikilink="[[source/X]]",
        extract_target_override="note",
    )
    _make_closed_session(state_mgr, "efgh5678", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[_tool_use_note_block("Log Test", "Body.")],
            stop_reason="tool_use",
        )
    ])

    with structlog.testing.capture_logs() as captured:
        await capture_extract.extract_notes_from_capture(
            client=client, state=state_mgr, vault_path=vault,
            short_id="efgh5678", model="claude-sonnet-4-6",
            agent_slug="hypatia", anchor_scope="hypatia",
        )

    done_logs = [c for c in captured
                 if c.get("event") == "talker.extract.done"]
    assert len(done_logs) == 1, (
        f"expected 1 talker.extract.done, got {len(done_logs)}: {captured}"
    )
    log_entry = done_logs[0]
    assert log_entry["target_type"] == "note"  # override forced
    assert log_entry["source_anchored"] is True
    assert log_entry["operator_override"] == "note"
    assert log_entry["anchor_scope"] == "hypatia"


# --- Explicit kwarg beats frontmatter override -------------------------


@pytest.mark.asyncio
async def test_explicit_kwarg_override_wins_over_frontmatter(
    state_mgr, tmp_path,
) -> None:
    """When the caller passes ``operator_override`` explicitly, it
    takes precedence over whatever is in the session record's
    frontmatter. Used by test paths + future re-extraction tools
    that want to bypass the persisted choice.
    """
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_session_record(
        vault, "capture-kwarg-precedence-fghi6789",
        source_wikilink="[[source/Y]]",
        extract_target_override="note",  # frontmatter says note
    )
    _make_closed_session(state_mgr, "fghi6789", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[_tool_use_note_block("Precedence Test", "Body.")],
            stop_reason="tool_use",
        )
    ])

    # Explicit kwarg overrides frontmatter — should force zettel/.
    result = await capture_extract.extract_notes_from_capture(
        client=client, state=state_mgr, vault_path=vault,
        short_id="fghi6789", model="claude-sonnet-4-6",
        agent_slug="hypatia", anchor_scope="hypatia",
        operator_override="zettel",  # caller forces zettel
    )

    assert result.created_paths[0].startswith("zettel/")
