"""Scope-aware extraction target — Hypatia produces ``zettel/`` records.

Phase 1 commit 3/5 of the Hypatia Zettelkasten schema cutover. Per the
"LOCKED IMPLEMENTATION PLAN" decision (Q2 ratified):

    target_type = "zettel" if anchor_scope == "hypatia" else "note"

Salem captures continue producing ``note/`` records (legacy default,
preserved by ``anchor_scope=""`` default-arg). Hypatia captures
produce ``zettel/`` records. KAL-LE has no capture-mode (uses surveyor
instead) so no entry; falls through to ``note/`` default.

This file tests:
  1. ``_extract_target_type`` mapping unit tests.
  2. End-to-end Hypatia extraction → zettel/ records.
  3. End-to-end Salem extraction → note/ records (regression).
  4. Unset / unknown scope → note/ records (regression).
  5. Observability — ``talker.extract.done`` log carries target_type.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from alfred.telegram import capture_batch, capture_extract
from tests.telegram.conftest import (
    FakeAnthropicClient, FakeBlock, FakeResponse,
)


# --- _extract_target_type unit tests --------------------------------------


def test_extract_target_type_hypatia() -> None:
    """Hypatia scope → zettel/ extraction."""
    assert capture_extract._extract_target_type("hypatia") == "zettel"


def test_extract_target_type_salem() -> None:
    """Salem (empty scope) → note/ extraction (legacy default)."""
    assert capture_extract._extract_target_type("") == "note"


def test_extract_target_type_unknown_scope_falls_through_to_note() -> None:
    """Unknown / future scope → note/ (defensive default).

    A future instance with capture-mode (e.g. V.E.R.A.) would either
    register an entry in ``_EXTRACT_TARGET_TYPE_BY_SCOPE`` or fall
    through to ``note/``. Defensive default keeps the surface working
    without coupling to a specific new instance.
    """
    assert capture_extract._extract_target_type("vera") == "note"
    assert capture_extract._extract_target_type("kalle") == "note"
    assert capture_extract._extract_target_type("nonexistent") == "note"


# --- Helpers (mirrors test_capture_extract.py shape) ----------------------


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
    vault_path: Path, name: str, with_summary: bool = True,
) -> str:
    (vault_path / "session").mkdir(exist_ok=True, parents=True)
    rel = f"session/{name}.md"
    body = (
        "**Andrew** (10:00 · voice): reading Meditations, struck by Book 5\n"
        "**Andrew** (10:01 · voice): the dichotomy of control comes alive here\n"
    )
    if with_summary:
        summary = (
            f"{capture_batch.SUMMARY_MARKER_START}\n\n"
            "## Structured Summary\n\n"
            "### Topics\n- stoicism\n- dichotomy of control\n\n"
            f"{capture_batch.SUMMARY_MARKER_END}\n\n"
        )
        body = summary + "# Transcript\n\n" + body
    else:
        body = "# Transcript\n\n" + body

    (vault_path / rel).write_text(
        "---\n"
        "type: session\n"
        "status: completed\n"
        f"name: {name}\n"
        "created: '2026-05-16'\n"
        "session_type: capture\n"
        "---\n\n" + body,
        encoding="utf-8",
    )
    return rel


def _tool_use_note_block(
    name: str, body: str, tier: str = "high",
    quote: str = "dichotomy of control comes alive",
) -> FakeBlock:
    return FakeBlock(
        type="tool_use",
        id=f"toolu_{name[:8]}",
        name="create_note",
        input={
            "name": name,
            "body": body,
            "confidence_tier": tier,
            "source_quote": quote,
        },
    )


def _make_hypatia_vault(tmp_path: Path) -> Path:
    """Build a Hypatia-shaped vault under ``tmp_path/vault``.

    Includes ``zettel/`` directory (the new target) and the support
    directories the resolver / extractor expect.
    """
    vault = tmp_path / "vault"
    for sub in ("session", "zettel", "note", "source", "author"):
        (vault / sub).mkdir(parents=True)
    return vault


# --- End-to-end: Hypatia scope → zettel/ extraction -----------------------


@pytest.mark.asyncio
async def test_extract_hypatia_scope_creates_zettel_records(
    state_mgr, tmp_path,
) -> None:
    """When ``anchor_scope='hypatia'``, extracted records land at
    ``zettel/<name>.md`` (not ``note/<name>.md``)."""
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_session_record(vault, "capture-2026-05-16-stoicism-abcd1234")
    _make_closed_session(state_mgr, "abcd1234", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[
                _tool_use_note_block(
                    "Dichotomy of Control as Stoic Foundation",
                    "Marcus returns to this principle repeatedly — what is "
                    "in our power and what is not.",
                    quote="the dichotomy of control comes alive here",
                ),
                _tool_use_note_block(
                    "Meditations Book 5 Reading Observation",
                    "Personal noticing of how the principle applies to "
                    "current ops work.",
                    tier="medium",
                ),
            ],
            stop_reason="tool_use",
        )
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client,
        state=state_mgr,
        vault_path=vault,
        short_id="abcd1234",
        model="claude-sonnet-4-6",
        agent_slug="hypatia",
        anchor_scope="hypatia",
    )

    assert result.skipped_reason == ""
    assert len(result.created_paths) == 2
    # Every record under zettel/ — NOT note/.
    for p in result.created_paths:
        assert p.startswith("zettel/"), (
            f"Hypatia extraction landed record at {p!r} — expected zettel/ prefix"
        )
        assert (vault / p).exists()

    # Frontmatter type field reflects zettel.
    import frontmatter
    first = frontmatter.load(vault / result.created_paths[0])
    assert first["type"] == "zettel"

    # Session's derived_notes list points at zettel/ paths.
    sess = frontmatter.load(vault / rel)
    derived = sess.get("derived_notes") or []
    assert len(derived) == 2
    assert all("[[zettel/" in d for d in derived)


@pytest.mark.asyncio
async def test_extract_salem_default_scope_creates_note_records(
    state_mgr, tmp_path,
) -> None:
    """Without ``anchor_scope`` (or with ``''``), Salem behaviour: ``note/``.

    Regression guard — the default-arg change must not silently flip
    Salem's extraction target.
    """
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_session_record(vault, "Voice Session — 2026-05-16 abc12345")
    _make_closed_session(state_mgr, "abc12345", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[_tool_use_note_block("Test Note", "body text")],
            stop_reason="tool_use",
        )
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client,
        state=state_mgr,
        vault_path=vault,
        short_id="abc12345",
        model="claude-sonnet-4-6",
        # NO anchor_scope kwarg — defaults to "" (Salem behaviour).
    )

    assert result.skipped_reason == ""
    assert len(result.created_paths) == 1
    assert result.created_paths[0].startswith("note/")


@pytest.mark.asyncio
async def test_extract_unknown_scope_falls_through_to_note(
    state_mgr, tmp_path,
) -> None:
    """``anchor_scope='someunregisteredinstance'`` → note/ (defensive)."""
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_session_record(vault, "Future Session — 2026-05-16 future01")
    _make_closed_session(state_mgr, "future01", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[_tool_use_note_block("Test", "body")],
            stop_reason="tool_use",
        )
    ])

    # NOTE: ops.vault_create rejects unknown scopes with ScopeError, so
    # we need a scope value that's empty-equivalent (falls through to
    # None in the bot.py plumbing). Test simulates the case where
    # ``config.instance.tool_set`` doesn't match any registered
    # scope — bot.py's derivation returns ``""`` not the unrecognised
    # name, so this test reflects the actual call shape.
    result = await capture_extract.extract_notes_from_capture(
        client=client,
        state=state_mgr,
        vault_path=vault,
        short_id="future01",
        model="claude-sonnet-4-6",
        anchor_scope="",  # bot.py returns "" for non-Hypatia instances
    )

    # Without a Hypatia scope, target falls through to note/.
    assert result.skipped_reason == ""
    assert result.created_paths[0].startswith("note/")


# --- Observability — log carries target_type ------------------------------


@pytest.mark.asyncio
async def test_extract_done_log_carries_target_type_for_hypatia(
    state_mgr, tmp_path,
) -> None:
    """``talker.extract.done`` log emits target_type=zettel for Hypatia.

    Per builder.md pre-commit checklist item #9 — log-emission tests
    must drive the production code path. The operator's grep workflow
    relies on this field to distinguish per-instance extraction
    activity.
    """
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_session_record(vault, "capture-2026-05-16-log-test-abcdef01")
    _make_closed_session(state_mgr, "abcdef01", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[_tool_use_note_block("Test Zettel", "body")],
            stop_reason="tool_use",
        )
    ])

    with structlog.testing.capture_logs() as captured:
        await capture_extract.extract_notes_from_capture(
            client=client,
            state=state_mgr,
            vault_path=vault,
            short_id="abcdef01",
            model="claude-sonnet-4-6",
            agent_slug="hypatia",
            anchor_scope="hypatia",
        )

    done_logs = [c for c in captured
                 if c.get("event") == "talker.extract.done"]
    assert len(done_logs) == 1, (
        f"expected 1 talker.extract.done, got {len(done_logs)}: {captured}"
    )
    assert done_logs[0]["target_type"] == "zettel"
    assert done_logs[0]["anchor_scope"] == "hypatia"
    assert done_logs[0]["created"] == 1


@pytest.mark.asyncio
async def test_extract_done_log_carries_target_type_for_salem(
    state_mgr, tmp_path,
) -> None:
    """Same — but Salem default → target_type=note."""
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_session_record(vault, "Voice Session — log abc12350")
    _make_closed_session(state_mgr, "abc12350", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[_tool_use_note_block("Test Note", "body")],
            stop_reason="tool_use",
        )
    ])

    with structlog.testing.capture_logs() as captured:
        await capture_extract.extract_notes_from_capture(
            client=client,
            state=state_mgr,
            vault_path=vault,
            short_id="abc12350",
            model="claude-sonnet-4-6",
        )

    done_logs = [c for c in captured
                 if c.get("event") == "talker.extract.done"]
    assert len(done_logs) == 1
    assert done_logs[0]["target_type"] == "note"
    assert done_logs[0]["anchor_scope"] == ""


# Note: ``vault_create_failed`` log also carries target_type=... in
# production (line ~426 of capture_extract.py). Not separately
# regression-tested here because vault_create's failure modes are hard
# to deterministically trigger inside a fixture vault (parent
# auto-creation handles most "directory missing" cases; collision
# disambiguation handles name collisions). The structural log-shape
# (target_type field present on emitted events) is covered by the
# ``.done`` log-emission tests above; if vault_create_failed ever needs
# its own emission pin, this is the spot to add it.
