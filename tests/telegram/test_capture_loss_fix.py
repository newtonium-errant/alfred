"""Clinic-capture arc (Piece 1) — a substantive/capture session must NEVER be
silently lost on close.

The incident: a clinician dictated action items into a Hypatia PWA voice session;
the PWA reopened (``web_session_reopened``) and the capture was archived with NO
structuring and NO signal — silently lost. Web sessions are always
``session_type=="conversation"`` (no web /capture, no web /end), so the fix keys
off a deterministic ``is_capture_candidate`` and writes an UNCONDITIONAL
``capture_structured: pending`` fail-safe marker at close (drainable / grep-able),
plus a flag-gated auto-structuring pass on the non-/end close paths.

Pins here (each mutation-verified — the note on each says which revert flips it):
  * ``is_capture_candidate`` — the deterministic gate (pure).
  * ``close_session`` — the unconditional marker + candidacy log.
  * ``check_timeouts_with_meta`` — carries ``capture_candidate`` for the sweeper.
  * ``schedule_capture_structuring`` — the shared close-path scheduler.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import frontmatter
import pytest
import structlog

from alfred.telegram import capture_batch
from alfred.telegram import session as talker_session
from alfred.telegram.session import Session, is_capture_candidate

_NOW = datetime(2026, 7, 5, 13, 0, tzinfo=timezone.utc)

# A user monologue that clears ``is_substantive`` (≥3 turns, ≥150 chars of user
# substance) — non-PHI, clinic-shaped (the incident's flavour).
_LONG_USER_1 = (
    "I need to write that clinical note tomorrow and invoice the room rental "
    "for the Tuesday clinic and send the prescription refill to the pharmacy."
)
_LONG_USER_2 = (
    "Also fax the disability forms for the client and book the follow-up "
    "appointment for next week and submit the VAC paperwork before Friday."
)


def _capture_transcript() -> list[dict]:
    return [
        {"role": "user", "content": _LONG_USER_1},
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": _LONG_USER_2},
        {"role": "assistant", "content": "Got it."},
    ]


def _short_transcript() -> list[dict]:
    return [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]


def _session(transcript) -> Session:
    return Session(
        session_id="abcdef12-0000-0000-0000-000000000000",
        chat_id=1, started_at=_NOW, last_message_at=_NOW,
        model="claude-sonnet-4-6", transcript=transcript, vault_ops=[],
    )


def _seed_active(state_mgr, talker_config, chat_id, transcript,
                 *, session_type="conversation", last_message_at=_NOW):
    active = {
        "session_id": f"{chat_id:08d}-0000-0000-0000-000000000000",
        "chat_id": chat_id,
        "started_at": _NOW.isoformat(),
        "last_message_at": last_message_at.isoformat(),
        "model": "claude-sonnet-4-6",
        "transcript": transcript,
        "vault_ops": [],
        "_vault_path_root": talker_config.vault.path,
        "_user_vault_path": "person/Andrew Newton",
        "_session_type": session_type,
    }
    state_mgr.set_active(chat_id, active)
    state_mgr.save()


# --- is_capture_candidate (pure) ------------------------------------------


@pytest.mark.parametrize("transcript,session_type,expected", [
    (_short_transcript(), "capture", True),        # explicit /capture wins (even short)
    (_capture_transcript(), "conversation", True), # WEB case: substantive monologue
    (_short_transcript(), "conversation", False),  # short Q&A → NOT a candidate
    ([], "conversation", False),                    # empty → NOT a candidate
])
def test_is_capture_candidate(transcript, session_type, expected) -> None:
    assert is_capture_candidate(_session(transcript), session_type) is expected


# --- close_session: unconditional fail-safe marker ------------------------


def test_close_marks_capture_candidate_pending(state_mgr, talker_config) -> None:
    """A capture candidate closing on ANY path (here: web_session_reopened) is
    stamped ``capture_structured: pending`` + logs candidacy. Mutation: revert
    the marker block in ``close_session`` → the frontmatter assert fails; drop
    the log → the log assert fails."""
    _seed_active(state_mgr, talker_config, 42, _capture_transcript())
    with structlog.testing.capture_logs() as cap:
        rel_path = talker_session.close_session(
            state_mgr, vault_path_root=talker_config.vault.path, chat_id=42,
            reason="web_session_reopened", user_vault_path="person/Andrew Newton",
            stt_model_used="", session_type="conversation",
            tool_set=talker_config.instance.tool_set,
        )
    post = frontmatter.load(str(Path(talker_config.vault.path) / rel_path))
    assert post["capture_structured"] == "pending"
    marks = [c for c in cap if c.get("event") == "talker.capture.candidate_marked"]
    assert len(marks) == 1
    assert marks[0]["turns"] == 4
    assert marks[0]["reason"] == "web_session_reopened"


def test_close_short_conversation_no_marker(state_mgr, talker_config) -> None:
    """A NON-candidate (short Q&A) close does NOT stamp the marker or log — the
    field only appears for captures. Mutation: make candidacy always-True → the
    ``not in`` assert fails."""
    _seed_active(state_mgr, talker_config, 43, _short_transcript())
    with structlog.testing.capture_logs() as cap:
        rel_path = talker_session.close_session(
            state_mgr, vault_path_root=talker_config.vault.path, chat_id=43,
            reason="web_session_reopened", user_vault_path="person/Andrew Newton",
            stt_model_used="", session_type="conversation",
            tool_set=talker_config.instance.tool_set,
        )
    post = frontmatter.load(str(Path(talker_config.vault.path) / rel_path))
    assert "capture_structured" not in post.keys()
    assert not [c for c in cap
                if c.get("event") == "talker.capture.candidate_marked"]


def test_close_explicit_capture_marked_even_when_short(
    state_mgr, talker_config,
) -> None:
    """An explicit /capture session is a candidate regardless of length — the
    marker is stamped even on a short one (the capture intent is explicit)."""
    _seed_active(state_mgr, talker_config, 44, _short_transcript(),
                 session_type="capture")
    rel_path = talker_session.close_session(
        state_mgr, vault_path_root=talker_config.vault.path, chat_id=44,
        reason="timeout", user_vault_path="person/Andrew Newton",
        stt_model_used="", session_type="capture",
        tool_set=talker_config.instance.tool_set,
    )
    post = frontmatter.load(str(Path(talker_config.vault.path) / rel_path))
    assert post["capture_structured"] == "pending"


# --- check_timeouts_with_meta carries the candidacy verdict ----------------


def test_timeout_meta_carries_capture_candidate(state_mgr, talker_config) -> None:
    """The timeout sweep meta carries ``capture_candidate`` so the daemon can
    gate structuring without re-deriving it. Mutation: drop the field from
    ``closed_meta`` → KeyError-style failure here."""
    old = datetime(2026, 7, 5, 10, 0, tzinfo=timezone.utc)
    _seed_active(state_mgr, talker_config, 45, _capture_transcript(),
                 last_message_at=old)
    metas = talker_session.check_timeouts_with_meta(
        state_mgr, old + timedelta(hours=2), gap_seconds=1800,
    )
    assert len(metas) == 1
    assert metas[0]["capture_candidate"] is True

    _seed_active(state_mgr, talker_config, 46, _short_transcript(),
                 last_message_at=old)
    metas = talker_session.check_timeouts_with_meta(
        state_mgr, old + timedelta(hours=2), gap_seconds=1800,
    )
    assert len(metas) == 1
    assert metas[0]["capture_candidate"] is False


# --- schedule_capture_structuring: the shared close-path scheduler ---------


async def test_schedule_capture_structuring_runs_process(
    tmp_path, monkeypatch,
) -> None:
    """The shared scheduler dispatches ``process_capture_session`` as a retained
    task and logs ``structuring_scheduled``. Mutation: skip the create_task /
    the log → the ``called`` / log asserts fail."""
    called: dict = {}

    async def _fake_process(**kwargs):
        called.update(kwargs)

    monkeypatch.setattr(capture_batch, "process_capture_session", _fake_process)

    with structlog.testing.capture_logs() as cap:
        task = capture_batch.schedule_capture_structuring(
            client=object(),
            vault_path=tmp_path,
            session_rel_path="session/x.md",
            transcript=_capture_transcript(),
            model="claude-sonnet-4-6",
            agent_slug="hypatia",
            anchor_scope="hypatia",
            short_id="abcdef12",
        )
    assert task is not None
    assert task in capture_batch._STRUCTURING_TASKS   # retained while in flight
    await task
    await asyncio.sleep(0)                              # let the done-callback run

    assert called["session_rel_path"] == "session/x.md"
    assert called["anchor_scope"] == "hypatia"
    assert called["send_follow_up"] is None
    sched = [c for c in cap
             if c.get("event") == "talker.capture.structuring_scheduled"]
    assert len(sched) == 1
    assert sched[0]["turns"] == 4
    assert task not in capture_batch._STRUCTURING_TASKS  # discarded when done


# --- sweeper finalize: structuring targets the RENAMED path ---------------
# (Piece-1 fast-follow — the substance-slug rename MOVES the file, so
# structuring must aim at the renamed rel_path, not the stale original.)


async def test_sweeper_finalize_structures_renamed_path(
    talker_config, monkeypatch,
) -> None:
    """With BOTH derive_slug_from_substance AND auto_structure_on_close on, a
    timed-out capture is renamed FIRST; structuring must target the RENAMED
    path. Mutation: revert ``_finalize_swept_capture`` to discard the rename
    return and reuse the original ``meta['rel_path']`` → the schedule receives
    the stale path → this fails."""
    from alfred.telegram import daemon as talker_daemon

    talker_config.session.derive_slug_from_substance = True
    talker_config.session.auto_structure_on_close = True

    async def _fake_rename(*args, **kwargs):
        return "session/RENAMED-abc12345.md"     # the MOVED path

    monkeypatch.setattr(
        talker_session, "maybe_apply_substance_slug", _fake_rename)

    scheduled: dict = {}
    monkeypatch.setattr(
        talker_daemon.capture_batch, "schedule_capture_structuring",
        lambda **kw: scheduled.update(kw) or None)

    meta = {
        "chat_id": 1,
        "session_id": "abc12345-0000-0000-0000-000000000000",
        "rel_path": "session/ORIGINAL-abc12345.md",
        "transcript": _capture_transcript(),
        "vault_path_root": talker_config.vault.path,
        "capture_candidate": True,
    }
    await talker_daemon._finalize_swept_capture(
        meta, config=talker_config, client=object(), state_mgr=None,
    )

    assert scheduled["session_rel_path"] == "session/RENAMED-abc12345.md"
    assert meta["rel_path"] == "session/RENAMED-abc12345.md"  # reassigned in place


async def test_sweeper_finalize_no_rename_uses_original(
    talker_config, monkeypatch,
) -> None:
    """derive_slug OFF (client=None → real passthrough) → rel_path unchanged →
    structuring targets the ORIGINAL path. Guards the reassign is a faithful
    no-op passthrough when no rename fires."""
    from alfred.telegram import daemon as talker_daemon

    talker_config.session.derive_slug_from_substance = False
    talker_config.session.auto_structure_on_close = True

    scheduled: dict = {}
    monkeypatch.setattr(
        talker_daemon.capture_batch, "schedule_capture_structuring",
        lambda **kw: scheduled.update(kw) or None)

    meta = {
        "chat_id": 2,
        "session_id": "def67890-0000-0000-0000-000000000000",
        "rel_path": "session/ORIGINAL-def67890.md",
        "transcript": _capture_transcript(),
        "vault_path_root": talker_config.vault.path,
        "capture_candidate": True,
    }
    await talker_daemon._finalize_swept_capture(
        meta, config=talker_config, client=None, state_mgr=None,
    )
    assert scheduled["session_rel_path"] == "session/ORIGINAL-def67890.md"


async def test_sweeper_finalize_non_candidate_no_structuring(
    talker_config, monkeypatch,
) -> None:
    """A non-candidate swept session is NOT structured even with the flag on."""
    from alfred.telegram import daemon as talker_daemon

    talker_config.session.derive_slug_from_substance = False
    talker_config.session.auto_structure_on_close = True

    scheduled: dict = {}
    monkeypatch.setattr(
        talker_daemon.capture_batch, "schedule_capture_structuring",
        lambda **kw: scheduled.update(kw) or None)

    meta = {
        "chat_id": 3, "session_id": "aaa-0000", "rel_path": "session/x.md",
        "transcript": _short_transcript(),
        "vault_path_root": talker_config.vault.path,
        "capture_candidate": False,
    }
    await talker_daemon._finalize_swept_capture(
        meta, config=talker_config, client=None, state_mgr=None,
    )
    assert scheduled == {}
