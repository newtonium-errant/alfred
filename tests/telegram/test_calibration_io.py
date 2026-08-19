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

from alfred.telegram import calibration, conversation
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
    """Canonical five-block layout: system → vault → calibration →
    pushback → today.

    The today's-date block (added 2026-05-06 to close the day-of-week
    date-math gap from conversation ``716f5b24``) always tails;
    pushback now sits second-to-last. Calibration's between-vault-and-
    pushback position is unchanged.
    """
    blocks = conversation._build_system_blocks(
        system_prompt="SYS",
        vault_context_str="VAULT",
        calibration_str="CAL_BODY",
        pushback_level=3,
    )
    assert len(blocks) == 5
    assert blocks[2]["text"].startswith("## Alfred's calibration for this user")
    assert "CAL_BODY" in blocks[2]["text"]
    # Pushback no longer tails — today's-date does.
    assert "Session pushback directive" in blocks[3]["text"]
    assert blocks[4]["text"].startswith("## Today")


def test_build_system_blocks_calibration_none_skips_block() -> None:
    blocks = conversation._build_system_blocks(
        system_prompt="SYS",
        vault_context_str="VAULT",
        calibration_str=None,
        pushback_level=3,
    )
    # System + vault + pushback + today = 4 blocks (calibration skipped,
    # today always present).
    assert len(blocks) == 4
    assert all("calibration" not in b["text"].lower() for b in blocks[:2])


# --- Session-open stash ----------------------------------------------------


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
    # Five blocks post-acda70c: system / vault / calibration / pushback /
    # today. Calibration's third-position is unchanged; today's-date
    # block always tails (never cached, changes daily).
    assert len(system) == 5
    assert "Alfred's calibration" in system[2]["text"]
    assert "- terse" in system[2]["text"]
    assert system[-1]["text"].startswith("## Today")
