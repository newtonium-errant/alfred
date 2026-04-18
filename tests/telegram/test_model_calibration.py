"""Tests for wk3 commit 8 — model-selection calibration scaffold.

Covers:
    * ``parse_model_preferences`` — empty input, missing subsection,
      well-formed bullets, malformed lines skipped, duplicates.
    * ``propose_default_flip`` — not enough history, not enough
      escalations, threshold met, tie-breaking.
    * ``Session.opening_model`` serialisation round-trip.
    * ``close_session`` writes ``opening_model`` + ``closing_model`` to
      the closed_sessions summary.
    * ``_open_routed_session`` honours a Model Preferences override.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from alfred.telegram import bot, calibration, model_calibration
from alfred.telegram import session as talker_session
from alfred.telegram.session import Session
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


# --- parse_model_preferences ----------------------------------------------


def test_parse_model_preferences_empty_input_returns_empty_dict() -> None:
    assert model_calibration.parse_model_preferences(None) == {}
    assert model_calibration.parse_model_preferences("") == {}


def test_parse_model_preferences_no_subsection_returns_empty_dict() -> None:
    cal = "## Communication Style\n- terse\n"
    assert model_calibration.parse_model_preferences(cal) == {}


def test_parse_model_preferences_parses_bullets() -> None:
    cal = (
        "## Communication Style\n- terse\n\n"
        "## Model Preferences (learned)\n"
        "- journal: claude-opus-4-7 _source: session/X_\n"
        "- brainstorm: claude-opus-4-7\n"
    )
    prefs = model_calibration.parse_model_preferences(cal)
    assert "journal" in prefs
    assert prefs["journal"].model == "claude-opus-4-7"
    assert prefs["brainstorm"].model == "claude-opus-4-7"
    assert "session_type=\"journal\"" not in str(prefs)  # avoid accidental repr leak


def test_parse_model_preferences_skips_malformed_lines() -> None:
    cal = (
        "## Model Preferences (learned)\n"
        "- this is not a valid line\n"
        "- note: claude-sonnet-4-6\n"
        "- xyzzy: claude-opus-4-7\n"  # invalid session type
    )
    prefs = model_calibration.parse_model_preferences(cal)
    # Only the valid one lands.
    assert list(prefs.keys()) == ["note"]
    assert prefs["note"].model == "claude-sonnet-4-6"


def test_parse_model_preferences_last_wins_on_duplicate() -> None:
    cal = (
        "## Model Preferences (learned)\n"
        "- journal: claude-sonnet-4-6\n"
        "- journal: claude-opus-4-7\n"
    )
    prefs = model_calibration.parse_model_preferences(cal)
    assert prefs["journal"].model == "claude-opus-4-7"


def test_parse_model_preferences_stops_at_next_heading() -> None:
    """Bullets under the NEXT ## heading are not included."""
    cal = (
        "## Model Preferences (learned)\n"
        "- journal: claude-opus-4-7\n\n"
        "## Something Else\n"
        "- note: claude-opus-4-7\n"
    )
    prefs = model_calibration.parse_model_preferences(cal)
    assert "journal" in prefs
    assert "note" not in prefs


# --- propose_default_flip -------------------------------------------------


def _seed_closed(
    state_mgr,
    session_type: str,
    opening_model: str,
    closing_model: str,
    session_id: str = "",
) -> None:
    state_mgr.state.setdefault("closed_sessions", []).append({
        "session_id": session_id or "s-" + str(len(state_mgr.state["closed_sessions"])),
        "chat_id": 1,
        "started_at": "2026-04-17T09:00:00+00:00",
        "ended_at": "2026-04-17T09:45:00+00:00",
        "reason": "explicit",
        "record_path": f"session/fake-{session_type}.md",
        "message_count": 5,
        "vault_ops": 0,
        "session_type": session_type,
        "continues_from": None,
        "opening_model": opening_model,
        "closing_model": closing_model,
        "model": closing_model,
    })
    state_mgr.save()


def test_propose_default_flip_returns_none_below_window(state_mgr) -> None:
    """Fewer than MODEL_CAL_THRESHOLD same-type sessions → no proposal."""
    _seed_closed(state_mgr, "journal", "claude-sonnet-4-6", "claude-opus-4-7")
    assert model_calibration.propose_default_flip("journal", state_mgr) is None


def test_propose_default_flip_returns_none_below_threshold(state_mgr) -> None:
    """5 sessions, only 1 escalated → below threshold."""
    _seed_closed(state_mgr, "journal", "claude-sonnet-4-6", "claude-opus-4-7")
    for _ in range(4):
        _seed_closed(
            state_mgr, "journal", "claude-sonnet-4-6", "claude-sonnet-4-6",
        )
    assert model_calibration.propose_default_flip("journal", state_mgr) is None


def test_propose_default_flip_fires_at_threshold(state_mgr) -> None:
    """3 escalated / 5 same-type sessions → Proposal returned."""
    # Most recent sessions are at the tail of the list (append-only).
    for _ in range(2):
        _seed_closed(
            state_mgr, "journal", "claude-sonnet-4-6", "claude-sonnet-4-6",
        )
    for _ in range(3):
        _seed_closed(
            state_mgr, "journal", "claude-sonnet-4-6", "claude-opus-4-7",
        )

    proposal = model_calibration.propose_default_flip("journal", state_mgr)
    assert proposal is not None
    assert proposal.subsection == "Model Preferences (learned)"
    assert "journal" in proposal.bullet
    assert "claude-opus-4-7" in proposal.bullet


def test_propose_default_flip_ignores_other_session_types(state_mgr) -> None:
    """Escalations in ``brainstorm`` do not trigger a proposal for ``journal``."""
    for _ in range(5):
        _seed_closed(
            state_mgr, "brainstorm",
            "claude-sonnet-4-6", "claude-opus-4-7",
        )
    assert model_calibration.propose_default_flip("journal", state_mgr) is None


def test_propose_default_flip_conservative_on_missing_opening_model(
    state_mgr,
) -> None:
    """Pre-commit-8 records (no opening_model) are NOT counted as escalated."""
    # Simulate 5 wk2 records (no opening_model).
    for _ in range(5):
        state_mgr.state.setdefault("closed_sessions", []).append({
            "session_id": "old",
            "chat_id": 1,
            "started_at": "2026-04-17T09:00:00+00:00",
            "ended_at": "2026-04-17T09:45:00+00:00",
            "reason": "explicit",
            "record_path": "session/old.md",
            "message_count": 5,
            "vault_ops": 0,
            "session_type": "journal",
            "continues_from": None,
            # opening_model + closing_model missing.
        })
    state_mgr.save()
    assert model_calibration.propose_default_flip("journal", state_mgr) is None


# --- Session.opening_model round-trip -------------------------------------


def test_session_opening_model_roundtrips() -> None:
    now = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
    sess = Session(
        session_id="abc",
        chat_id=1,
        started_at=now,
        last_message_at=now,
        model="claude-sonnet-4-6",
        opening_model="claude-sonnet-4-6",
    )
    dumped = sess.to_dict()
    assert dumped["opening_model"] == "claude-sonnet-4-6"

    # Round-trip.
    revived = Session.from_dict(dumped)
    assert revived.opening_model == "claude-sonnet-4-6"


def test_session_from_dict_pre_wk3_missing_opening_model_falls_back_to_model() -> None:
    """wk2 active dicts didn't have opening_model — fall back to model."""
    now = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
    raw = {
        "session_id": "abc",
        "chat_id": 1,
        "started_at": now.isoformat(),
        "last_message_at": now.isoformat(),
        "model": "claude-sonnet-4-6",
        "transcript": [],
        "vault_ops": [],
    }
    revived = Session.from_dict(raw)
    assert revived.opening_model == "claude-sonnet-4-6"


def test_close_session_writes_opening_and_closing_model(
    state_mgr, talker_config
) -> None:
    """closed_sessions summary carries both model fields."""
    chat_id = 99
    now = datetime(2026, 4, 18, 13, 30, tzinfo=timezone.utc)
    active = {
        "session_id": "dead-0000",
        "chat_id": chat_id,
        "started_at": now.isoformat(),
        "last_message_at": now.isoformat(),
        "model": "claude-opus-4-7",  # mid-session escalation
        "opening_model": "claude-sonnet-4-6",  # started on Sonnet
        "transcript": [{"role": "user", "content": "test"}],
        "vault_ops": [],
        "_vault_path_root": talker_config.vault.path,
        "_user_vault_path": "person/Andrew Newton",
        "_stt_model_used": "whisper-large-v3",
        "_session_type": "journal",
    }
    state_mgr.set_active(chat_id, active)
    state_mgr.save()

    talker_session.close_session(
        state_mgr,
        vault_path_root=talker_config.vault.path,
        chat_id=chat_id,
        reason="explicit",
        user_vault_path="person/Andrew Newton",
        stt_model_used="whisper-large-v3",
        session_type="journal",
    )

    closed = state_mgr.state["closed_sessions"][-1]
    assert closed["opening_model"] == "claude-sonnet-4-6"
    assert closed["closing_model"] == "claude-opus-4-7"


# --- _open_routed_session honours Model Preferences -----------------------


@pytest.mark.asyncio
async def test_routed_open_honours_model_preference_override(
    state_mgr, talker_config
) -> None:
    """Calibration has ``journal → opus`` preference → journal opens on Opus.

    The router would normally open journal on Sonnet (per session-type
    defaults). Commit 8 says: if the calibration block records a learned
    preference, use it instead.
    """
    vault_path = Path(talker_config.vault.path)
    (vault_path / "person").mkdir(exist_ok=True)
    user_rel = talker_config.primary_users[0]
    cal = (
        f"{calibration.CALIBRATION_MARKER_START}\n"
        "## Model Preferences (learned)\n"
        "- journal: claude-opus-4-7\n"
        f"{calibration.CALIBRATION_MARKER_END}\n"
    )
    (vault_path / f"{user_rel}.md").write_text(
        "---\ntype: person\nname: Andrew Newton\n---\n\n" + cal,
        encoding="utf-8",
    )

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(
            type="text",
            text=(
                '{"session_type": "journal", "continues_from": null, '
                '"reasoning": "reflective"}'
            ),
        )]),
    ])

    sess = await bot._open_routed_session(
        state_mgr,
        talker_config,
        client,
        chat_id=77,
        first_message="I want to think through something.",
    )

    # Router would pick Sonnet for journal; calibration overrides to Opus.
    assert sess.model == "claude-opus-4-7"
    assert sess.opening_model == "claude-opus-4-7"


@pytest.mark.asyncio
async def test_routed_open_no_preference_uses_router_default(
    state_mgr, talker_config
) -> None:
    """Empty/missing preference → router's default stands."""
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(
            type="text",
            text=(
                '{"session_type": "journal", "continues_from": null, '
                '"reasoning": "reflective"}'
            ),
        )]),
    ])

    sess = await bot._open_routed_session(
        state_mgr,
        talker_config,
        client,
        chat_id=78,
        first_message="I want to think through something.",
    )

    # Journal's default is Sonnet; no calibration override present.
    assert sess.model == "claude-sonnet-4-6"
    assert sess.opening_model == "claude-sonnet-4-6"
