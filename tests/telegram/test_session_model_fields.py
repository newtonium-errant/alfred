"""Session opening/closing model-field pins.

Moved verbatim from ``tests/telegram/test_model_calibration.py`` when
``telegram/model_calibration.py`` was deleted (T5, 2026-08-19): these
three tests never drove that module — they pin LIVE ``session.py``
behaviour (``Session.opening_model`` serialisation round-trip, the
pre-wk3 ``from_dict`` fallback, and ``close_session`` writing both
model fields into the ``closed_sessions`` state summary). The fields'
sole analysis consumer (``propose_default_flip``) died with the module;
the fields themselves remain part of the closed-session state contract
and are recorded as per-session model provenance.
"""

from __future__ import annotations

from datetime import datetime, timezone

from alfred.telegram import session as talker_session
from alfred.telegram.session import Session


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
