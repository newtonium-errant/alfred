"""Tests for wk3 commit 7 — session-end calibration writes.

Covers:
    * ``vault_edit(body_rewriter=...)`` kwarg: rewrites run, idempotent
      rewrites don't flag body as changed.
    * ``Proposal`` dataclass shape and defaults.
    * ``propose_updates`` graceful-degrades on parse errors / network errors.
    * ``apply_proposals`` dial behaviour (0/1/2/3/4), unknown subsection
      fallback, no-proposals / empty-rewrite cases.
    * End-to-end: ``_insert_into_block`` preserves surrounding body,
      inserts bullets under the right subsection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.telegram import calibration
from alfred.vault import ops
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


# --- vault_edit(body_rewriter=...) ----------------------------------------


def _make_person_record(tmp_path: Path, name: str, body: str) -> str:
    """Write a person record with the given body under tmp_path."""
    (tmp_path / "person").mkdir(exist_ok=True)
    file_path = tmp_path / "person" / f"{name}.md"
    file_path.write_text(
        f"---\ntype: person\nname: {name}\ncreated: '2026-04-18'\n---\n\n" + body,
        encoding="utf-8",
    )
    return f"person/{name}.md"


def test_vault_edit_body_rewriter_runs_and_rewrites_body(tmp_path: Path) -> None:
    rel = _make_person_record(tmp_path, "X", "Original body.\n")

    def rewrite(body: str) -> str:
        return body.replace("Original", "Updated")

    result = ops.vault_edit(tmp_path, rel, body_rewriter=rewrite)
    assert "body" in result["fields_changed"]
    new_text = (tmp_path / rel).read_text(encoding="utf-8")
    assert "Updated body" in new_text
    assert "Original body" not in new_text


def test_vault_edit_body_rewriter_noop_does_not_mark_body_changed(
    tmp_path: Path,
) -> None:
    """If the rewriter returns the body unchanged, fields_changed stays clean."""
    rel = _make_person_record(tmp_path, "Y", "Stable body.\n")

    def identity(body: str) -> str:
        return body

    result = ops.vault_edit(tmp_path, rel, body_rewriter=identity)
    assert "body" not in result["fields_changed"]


def test_vault_edit_body_rewriter_composes_with_body_append(tmp_path: Path) -> None:
    """``body_append`` runs first, then ``body_rewriter`` sees the post-append body."""
    rel = _make_person_record(tmp_path, "Z", "Original.\n")

    def uppercase(body: str) -> str:
        return body.upper()

    ops.vault_edit(
        tmp_path, rel,
        body_append="added line",
        body_rewriter=uppercase,
    )
    new_text = (tmp_path / rel).read_text(encoding="utf-8")
    assert "ADDED LINE" in new_text
    assert "ORIGINAL." in new_text


# --- Proposal + propose_updates -------------------------------------------


def test_proposal_defaults() -> None:
    p = calibration.Proposal(subsection="Communication Style", bullet="terse")
    assert p.confidence == 0.7
    assert p.source_session_rel == ""


@pytest.mark.asyncio
async def test_propose_updates_parses_well_formed_json() -> None:
    payload = (
        '[{"subsection": "Workflow Preferences", '
        '"bullet": "Prefers to end sessions with /end.", '
        '"confidence": 0.82}]'
    )
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text=payload)]),
    ])
    got = await calibration.propose_updates(
        client=client,
        transcript_text="USER: example\nASSISTANT: ok",
        current_calibration="(empty)",
        session_type="note",
        source_session_rel="session/Test.md",
    )
    assert len(got) == 1
    p = got[0]
    assert p.subsection == "Workflow Preferences"
    assert "Prefers to end sessions" in p.bullet
    assert p.confidence == pytest.approx(0.82)
    assert p.source_session_rel == "session/Test.md"


@pytest.mark.asyncio
async def test_propose_updates_returns_empty_on_parse_failure() -> None:
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="not json at all")]),
    ])
    got = await calibration.propose_updates(
        client=client,
        transcript_text="USER: hi",
        current_calibration=None,
        session_type="note",
        source_session_rel="session/T.md",
    )
    assert got == []


@pytest.mark.asyncio
async def test_propose_updates_returns_empty_on_api_error() -> None:
    class FailingClient:
        class FailingMessages:
            async def create(self, **_):
                raise RuntimeError("network down")
        messages = FailingMessages()

    got = await calibration.propose_updates(
        client=FailingClient(),
        transcript_text="USER: hi",
        current_calibration=None,
        session_type="note",
        source_session_rel="session/T.md",
    )
    assert got == []


@pytest.mark.asyncio
async def test_propose_updates_drops_empty_bullets() -> None:
    """Proposals with empty/whitespace bullets are filtered out."""
    payload = (
        '[{"subsection": "Workflow Preferences", "bullet": "  ", "confidence": 0.9},'
        ' {"subsection": "Workflow Preferences", "bullet": "real one", "confidence": 0.8}]'
    )
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text=payload)]),
    ])
    got = await calibration.propose_updates(
        client=client,
        transcript_text="",
        current_calibration=None,
        session_type="note",
        source_session_rel="session/T.md",
    )
    assert len(got) == 1
    assert got[0].bullet == "real one"


@pytest.mark.asyncio
async def test_propose_updates_clamps_bad_confidence() -> None:
    payload = '[{"subsection": "Notes", "bullet": "x", "confidence": 9.5}]'
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text=payload)]),
    ])
    got = await calibration.propose_updates(
        client=client,
        transcript_text="",
        current_calibration=None,
        session_type="note",
        source_session_rel="session/T.md",
    )
    assert got[0].confidence == 1.0


# --- apply_proposals + dial behaviour -------------------------------------


def _make_person_with_calibration(tmp_path: Path, subsections: str = "") -> str:
    body = (
        "# Andrew\n\n"
        f"{calibration.CALIBRATION_MARKER_START}\n"
        + subsections
        + f"\n{calibration.CALIBRATION_MARKER_END}\n"
    )
    return _make_person_record(tmp_path, "Andrew Newton", body)


def test_apply_proposals_dial_zero_is_a_noop(tmp_path: Path) -> None:
    rel = _make_person_with_calibration(tmp_path, "## Communication Style\n\n")
    result = calibration.apply_proposals(
        vault_path=tmp_path,
        user_rel_path="person/Andrew Newton",
        proposals=[calibration.Proposal("Communication Style", "terse")],
        session_record_path="session/T.md",
        confirmation_dial=0,
    )
    assert result["written"] is False
    assert result["reason"] == "dial_zero"


def test_apply_proposals_dial_one_silent_write(tmp_path: Path) -> None:
    _make_person_with_calibration(tmp_path, "## Communication Style\n\n")
    result = calibration.apply_proposals(
        vault_path=tmp_path,
        user_rel_path="person/Andrew Newton",
        proposals=[calibration.Proposal(
            "Communication Style", "terse cadence", confidence=0.5,
        )],
        session_record_path="session/T.md",
        confirmation_dial=1,
    )
    assert result["written"] is True
    new_text = (tmp_path / "person" / "Andrew Newton.md").read_text()
    assert "terse cadence" in new_text
    # Dial 1 = silent; no marker regardless of confidence.
    assert "[needs confirmation]" not in new_text


def test_apply_proposals_dial_two_marks_low_confidence_only(tmp_path: Path) -> None:
    _make_person_with_calibration(tmp_path, "## Communication Style\n\n")
    result = calibration.apply_proposals(
        vault_path=tmp_path,
        user_rel_path="person/Andrew Newton",
        proposals=[
            calibration.Proposal(
                "Communication Style", "high-confidence item", confidence=0.9,
            ),
            calibration.Proposal(
                "Communication Style", "low-confidence item", confidence=0.3,
            ),
        ],
        session_record_path="session/T.md",
        confirmation_dial=2,
    )
    assert result["written"] is True
    new_text = (tmp_path / "person" / "Andrew Newton.md").read_text()
    # Low-confidence one has the marker, high-confidence one doesn't.
    import re as _re
    lines_with_marker = [
        l for l in new_text.splitlines() if "[needs confirmation]" in l
    ]
    assert any("low-confidence item" in l for l in lines_with_marker)
    assert not any("high-confidence item" in l for l in lines_with_marker)


def test_apply_proposals_dial_three_marks_everything(tmp_path: Path) -> None:
    _make_person_with_calibration(tmp_path, "## Communication Style\n\n")
    result = calibration.apply_proposals(
        vault_path=tmp_path,
        user_rel_path="person/Andrew Newton",
        proposals=[calibration.Proposal(
            "Communication Style", "high-confidence", confidence=0.99,
        )],
        session_record_path="session/T.md",
        confirmation_dial=3,
    )
    assert result["written"] is True
    new_text = (tmp_path / "person" / "Andrew Newton.md").read_text()
    assert "high-confidence" in new_text
    # Dial 3 marks everything regardless of confidence.
    assert "[needs confirmation]" in new_text


def test_apply_proposals_writes_source_attribution(tmp_path: Path) -> None:
    _make_person_with_calibration(tmp_path, "## Current Priorities\n\n")
    calibration.apply_proposals(
        vault_path=tmp_path,
        user_rel_path="person/Andrew Newton",
        proposals=[calibration.Proposal(
            "Current Priorities", "New initiative",
            confidence=0.9,
            source_session_rel="session/Foo.md",
        )],
        session_record_path="session/Foo.md",
        confirmation_dial=4,
    )
    new_text = (tmp_path / "person" / "Andrew Newton.md").read_text()
    assert "_source: session/Foo_" in new_text


def test_apply_proposals_creates_missing_subsection_heading(tmp_path: Path) -> None:
    """If the target subsection isn't in the block, a fresh heading is appended."""
    # Start with an empty calibration block.
    _make_person_with_calibration(tmp_path, "")
    calibration.apply_proposals(
        vault_path=tmp_path,
        user_rel_path="person/Andrew Newton",
        proposals=[calibration.Proposal(
            "Current Priorities", "new bullet", confidence=0.9,
        )],
        session_record_path="session/T.md",
        confirmation_dial=4,
    )
    new_text = (tmp_path / "person" / "Andrew Newton.md").read_text()
    assert "## Current Priorities" in new_text
    assert "new bullet" in new_text


def test_apply_proposals_unknown_subsection_falls_back_to_notes(
    tmp_path: Path,
) -> None:
    _make_person_with_calibration(tmp_path, "")
    calibration.apply_proposals(
        vault_path=tmp_path,
        user_rel_path="person/Andrew Newton",
        proposals=[calibration.Proposal(
            "XYZZY Invented Category", "stray bullet", confidence=0.9,
        )],
        session_record_path="session/T.md",
        confirmation_dial=4,
    )
    new_text = (tmp_path / "person" / "Andrew Newton.md").read_text()
    assert "## Notes" in new_text
    assert "stray bullet" in new_text


def test_apply_proposals_no_block_is_noop(tmp_path: Path) -> None:
    """No calibration markers → apply_proposals logs and returns unchanged."""
    _make_person_record(tmp_path, "NoBlock", "# Person without a calibration block.\n")
    result = calibration.apply_proposals(
        vault_path=tmp_path,
        user_rel_path="person/NoBlock",
        proposals=[calibration.Proposal(
            "Communication Style", "won't land", confidence=0.9,
        )],
        session_record_path="session/T.md",
        confirmation_dial=4,
    )
    # vault_edit runs but the rewriter is a no-op because the block is absent.
    # ``written`` is True because vault_edit succeeded — but the body is
    # unchanged. The log warning (``talker.calibration.no_block_for_apply``)
    # covers the observability.
    new_text = (tmp_path / "person" / "NoBlock.md").read_text()
    assert "won't land" not in new_text


def test_apply_proposals_empty_list_returns_no_proposals(tmp_path: Path) -> None:
    _make_person_with_calibration(tmp_path, "")
    result = calibration.apply_proposals(
        vault_path=tmp_path,
        user_rel_path="person/Andrew Newton",
        proposals=[],
        session_record_path="session/T.md",
        confirmation_dial=4,
    )
    assert result["written"] is False
    assert result["reason"] == "no_proposals"


def test_apply_proposals_preserves_surrounding_body(tmp_path: Path) -> None:
    """Body content outside the calibration block stays untouched."""
    body = (
        "# Andrew\n\nAbove the block.\n\n"
        f"{calibration.CALIBRATION_MARKER_START}\n## Communication Style\n\n"
        f"{calibration.CALIBRATION_MARKER_END}\n\nBelow the block.\n"
    )
    _make_person_record(tmp_path, "Andrew Newton", body)
    calibration.apply_proposals(
        vault_path=tmp_path,
        user_rel_path="person/Andrew Newton",
        proposals=[calibration.Proposal(
            "Communication Style", "inserted", confidence=0.9,
        )],
        session_record_path="session/T.md",
        confirmation_dial=4,
    )
    new_text = (tmp_path / "person" / "Andrew Newton.md").read_text()
    assert "Above the block." in new_text
    assert "Below the block." in new_text
    assert "inserted" in new_text


def test_apply_proposals_inserts_under_existing_heading(tmp_path: Path) -> None:
    """Bullet lands in the correct subsection, not under a fresh heading."""
    body = (
        f"{calibration.CALIBRATION_MARKER_START}\n"
        "## Communication Style\n\n- existing one\n\n"
        "## Workflow Preferences\n\n- workflow\n"
        f"{calibration.CALIBRATION_MARKER_END}\n"
    )
    _make_person_record(tmp_path, "Andrew Newton", body)
    calibration.apply_proposals(
        vault_path=tmp_path,
        user_rel_path="person/Andrew Newton",
        proposals=[calibration.Proposal(
            "Communication Style", "added", confidence=0.9,
        )],
        session_record_path="session/T.md",
        confirmation_dial=4,
    )
    new_text = (tmp_path / "person" / "Andrew Newton.md").read_text()
    # Inserted bullet sits BEFORE the Workflow Preferences heading.
    comm_idx = new_text.index("## Communication Style")
    wf_idx = new_text.index("## Workflow Preferences")
    added_idx = new_text.index("added")
    assert comm_idx < added_idx < wf_idx
