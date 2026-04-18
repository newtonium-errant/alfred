"""Tests for wk3 commit 2 — calibration block read + inject.

Covers:
    * ``read_calibration`` return-value contract (happy path, missing file,
      missing block, empty block, malformed).
    * ``_open_routed_session`` stashes the snapshot on the active dict.
    * ``handle_message`` threads the snapshot into ``run_turn`` → it lands
      as the third cache-control system block.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from alfred.telegram import bot, calibration, conversation
from alfred.telegram.session import Session
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


# --- read_calibration ------------------------------------------------------


def _write_user_record(
    vault_path: Path,
    user_rel: str,
    body: str,
) -> Path:
    """Helper: write a minimal person record at ``user_rel`` with ``body``."""
    path = vault_path / f"{user_rel}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: person\nname: "
        + user_rel.split("/")[-1]
        + "\n---\n\n"
        + body,
        encoding="utf-8",
    )
    return path


def test_read_calibration_happy_path(tmp_path: Path) -> None:
    """Wrapped block → stripped inner text returned."""
    inner = "## Communication Style\n- terse military cadence"
    body = (
        f"# Andrew\n\n{calibration.CALIBRATION_MARKER_START}\n"
        f"{inner}\n{calibration.CALIBRATION_MARKER_END}\n"
    )
    _write_user_record(tmp_path, "person/Andrew Newton", body)
    result = calibration.read_calibration(tmp_path, "person/Andrew Newton")
    assert result == inner


def test_read_calibration_accepts_path_with_or_without_md_suffix(
    tmp_path: Path,
) -> None:
    inner = "- test"
    body = (
        f"{calibration.CALIBRATION_MARKER_START}\n{inner}\n"
        f"{calibration.CALIBRATION_MARKER_END}\n"
    )
    _write_user_record(tmp_path, "person/X", body)

    assert calibration.read_calibration(tmp_path, "person/X") == inner
    assert calibration.read_calibration(tmp_path, "person/X.md") == inner


def test_read_calibration_missing_file_returns_none(tmp_path: Path) -> None:
    assert calibration.read_calibration(tmp_path, "person/Nobody") is None


def test_read_calibration_empty_rel_path_returns_none(tmp_path: Path) -> None:
    assert calibration.read_calibration(tmp_path, "") is None


def test_read_calibration_no_block_returns_none(tmp_path: Path) -> None:
    _write_user_record(tmp_path, "person/X", "# X\n\nJust a body, no block.\n")
    assert calibration.read_calibration(tmp_path, "person/X") is None


def test_read_calibration_empty_block_returns_none(tmp_path: Path) -> None:
    body = (
        f"{calibration.CALIBRATION_MARKER_START}\n   \n"
        f"{calibration.CALIBRATION_MARKER_END}\n"
    )
    _write_user_record(tmp_path, "person/X", body)
    assert calibration.read_calibration(tmp_path, "person/X") is None


def test_read_calibration_block_spans_multiple_lines(tmp_path: Path) -> None:
    inner = (
        "## Communication Style\n"
        "- bullet one\n"
        "- bullet two\n\n"
        "## Workflow Preferences\n"
        "- another"
    )
    body = (
        f"{calibration.CALIBRATION_MARKER_START}\n{inner}\n"
        f"{calibration.CALIBRATION_MARKER_END}\n"
    )
    _write_user_record(tmp_path, "person/X", body)
    got = calibration.read_calibration(tmp_path, "person/X")
    assert got is not None
    assert "Communication Style" in got
    assert "Workflow Preferences" in got


# --- Injection into system blocks ------------------------------------------


def test_build_system_blocks_includes_calibration_between_vault_and_pushback() -> None:
    """Canonical four-block layout: system → vault → calibration → pushback."""
    blocks = conversation._build_system_blocks(
        system_prompt="SYS",
        vault_context_str="VAULT",
        calibration_str="CAL_BODY",
        pushback_level=3,
    )
    assert len(blocks) == 4
    assert blocks[2]["text"].startswith("## Alfred's calibration for this user")
    assert "CAL_BODY" in blocks[2]["text"]
    # Pushback still tails so the suffix stays stable across sessions.
    assert "Session pushback directive" in blocks[3]["text"]


def test_build_system_blocks_calibration_none_skips_block() -> None:
    blocks = conversation._build_system_blocks(
        system_prompt="SYS",
        vault_context_str="VAULT",
        calibration_str=None,
        pushback_level=3,
    )
    assert len(blocks) == 3
    assert all("calibration" not in b["text"].lower() for b in blocks[:2])


# --- Session-open stash ----------------------------------------------------


@pytest.mark.asyncio
async def test_routed_open_stashes_calibration_snapshot(
    state_mgr, talker_config
) -> None:
    """Calibration is read once at session open and stashed for the session."""
    # Prepare a user record with a calibration block.
    vault_path = Path(talker_config.vault.path)
    (vault_path / "person").mkdir(exist_ok=True)
    user_rel = talker_config.primary_users[0]
    inner = "## Communication Style\n- test block"
    (vault_path / f"{user_rel}.md").write_text(
        "---\ntype: person\nname: Andrew Newton\n---\n\n"
        f"{calibration.CALIBRATION_MARKER_START}\n{inner}\n"
        f"{calibration.CALIBRATION_MARKER_END}\n",
        encoding="utf-8",
    )

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(
            type="text",
            text='{"session_type": "note", "continues_from": null, '
                 '"reasoning": "quick capture"}',
        )]),
    ])

    await bot._open_routed_session(
        state_mgr,
        talker_config,
        client,
        chat_id=11,
        first_message="quick reminder",
    )

    active = state_mgr.get_active(11)
    assert active is not None
    assert active["_calibration_snapshot"] == inner


@pytest.mark.asyncio
async def test_routed_open_stashes_none_when_no_calibration_block(
    state_mgr, talker_config
) -> None:
    """No calibration block on the person record → snapshot is ``None``."""
    # The user record doesn't exist at all — the stash value should still
    # land on the active dict (as None) so the bot can detect wk3+ opens.
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(
            type="text",
            text='{"session_type": "note", "continues_from": null, '
                 '"reasoning": "quick capture"}',
        )]),
    ])

    await bot._open_routed_session(
        state_mgr,
        talker_config,
        client,
        chat_id=12,
        first_message="quick reminder",
    )

    active = state_mgr.get_active(12)
    assert active is not None
    assert "_calibration_snapshot" in active
    assert active["_calibration_snapshot"] is None


# --- run_turn threading ----------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_injects_calibration_into_api_call(
    state_mgr, talker_config
) -> None:
    """``run_turn(calibration_str=...)`` lands as the third system block."""
    sess = Session(
        session_id="cal-test",
        chat_id=1,
        started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
        model="claude-sonnet-4-6",
    )
    state_mgr.set_active(1, sess.to_dict())

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="ok")]),
    ])

    await conversation.run_turn(
        client=client,
        state=state_mgr,
        session=sess,
        user_message="hi",
        config=talker_config,
        vault_context_str="VAULT",
        system_prompt="SYS",
        calibration_str="## Style\n- terse",
        pushback_level=2,
    )

    call = client.messages.calls[0]
    system = call["system"]
    assert len(system) == 4
    assert "Alfred's calibration" in system[2]["text"]
    assert "- terse" in system[2]["text"]
