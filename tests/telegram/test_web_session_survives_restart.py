"""#94 (a) — a deploy restart must not sever a live browser conversation.

THE INCIDENT, 2026-08-11. Three deliberate restarts of algernon-hypatia landed
inside one conversation window (03:10:38, 04:22:58, 05:52:58Z). Each time, the
shutdown sweep closed the operator's active web session with
``close_reason=shutdown``; the PWA's stored key then 404'd on resume, the red
"conversation has ended" banner appeared, and a reply he had already COMPOSED
was lost — he screenshotted it into the next session to recover it.

WHY TELEGRAM WAS NEVER EXPOSED, and why the fix is web-only: a Telegram client
holds no session key, so the next message simply opens a session and the user
notices nothing. A browser DOES hold a key, so for it a shutdown-close is
indistinguishable from the conversation being deliberately ended.

WHAT THE FIX IS NOT. No new persistence layer: ``StateManager`` already writes
``active_sessions`` atomically after every turn, and ``resolve_on_startup``
already leaves any session inside the idle gap in place. The whole defect was
that the shutdown sweep DELETED the session on the way out. So the change is a
skip, and the tests below are mostly about what must NOT happen.

The load-bearing pins here are the negative ones:

  * a Telegram session must STILL close at shutdown (the sweep's real job),
  * an IDLE web session must STILL close on the normal cadence — otherwise
    "survives restart" quietly becomes "never times out", and web records go
    back to being filed days behind their content (the bug #62's stash fixed).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import structlog

from alfred.telegram import session as session_mod
from alfred.telegram.daemon import _close_open_sessions_on_shutdown
from alfred.web.identity import (
    WEB_USER_ID_BASE,
    WEB_USER_ID_SPAN,
    is_web_chat_id,
    synthetic_chat_id,
)

WEB_CHAT_ID = synthetic_chat_id("andrew")
TELEGRAM_CHAT_ID = 42


def _seed(
    state_mgr,
    talker_config,
    chat_id: int,
    *,
    minutes_ago: int = 0,
    text: str = "the reply I had already composed",
) -> dict:
    """Seed an active session with the open-time stash both channels write."""
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    active = {
        "session_id": f"sess-{chat_id}",
        "chat_id": chat_id,
        "started_at": when.isoformat(),
        "last_message_at": when.isoformat(),
        "model": "claude-sonnet-4-6",
        "transcript": [{"role": "user", "content": text}],
        "vault_ops": [],
        "_vault_path_root": talker_config.vault.path,
        "_user_vault_path": "person/Andrew Newton",
        "_stt_model_used": "whisper-large-v3",
        "_session_type": "conversation",
    }
    state_mgr.set_active(chat_id, active)
    state_mgr.save()
    return active


# ---------------------------------------------------------------------------
# The band predicate
# ---------------------------------------------------------------------------


def test_the_predicate_recognises_a_real_web_id() -> None:
    assert is_web_chat_id(synthetic_chat_id("andrew")) is True
    assert is_web_chat_id(synthetic_chat_id("someone else")) is True


def test_the_predicate_rejects_telegram_ids() -> None:
    """Both directions of the band's collision proof.

    Positive Telegram ids sit below 2**52; group ids are negative. Either
    being misread as web would preserve a Telegram session forever.
    """
    for tid in (1, 42, 2**52 - 1, -1001234567890, 0):
        assert is_web_chat_id(tid) is False, tid


def test_the_predicate_holds_at_the_band_edges() -> None:
    assert is_web_chat_id(WEB_USER_ID_BASE) is True
    assert is_web_chat_id(WEB_USER_ID_BASE - 1) is False
    assert is_web_chat_id(WEB_USER_ID_BASE + WEB_USER_ID_SPAN - 1) is True
    assert is_web_chat_id(WEB_USER_ID_BASE + WEB_USER_ID_SPAN) is False


def test_the_predicate_takes_string_keys() -> None:
    """Every sweep iterates ``active_sessions``, which is keyed by STRING.

    A predicate that only accepted ints would return False for every real
    call site and preserve nothing — while its own unit tests passed.
    """
    assert is_web_chat_id(str(WEB_CHAT_ID)) is True
    assert is_web_chat_id("42") is False


def test_a_corrupt_key_is_not_a_web_session() -> None:
    """The honest answer, and it routes to the path that already handles it."""
    assert is_web_chat_id("not-an-int") is False
    assert is_web_chat_id(None) is False


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


async def test_a_web_session_survives_shutdown(state_mgr, talker_config) -> None:
    """THE incident pin. The session must still be there afterwards."""
    _seed(state_mgr, talker_config, WEB_CHAT_ID)

    closed = await _close_open_sessions_on_shutdown(
        state_mgr, talker_config, client=None,
    )

    assert closed == [], "a web session was archived at shutdown"
    survivor = state_mgr.get_active(WEB_CHAT_ID)
    assert survivor is not None, "the browser's session was severed by a restart"
    assert survivor["session_id"] == f"sess-{WEB_CHAT_ID}"
    # And the composed turn is still there to come back to.
    assert survivor["transcript"][0]["content"] == (
        "the reply I had already composed"
    )


async def test_no_session_record_is_written_for_a_preserved_session(
    state_mgr, talker_config,
) -> None:
    """Preserved means PRESERVED — not "closed quietly into the vault".

    A record written here would also mean the conversation was archived
    mid-flight and the next turn would start a second one.
    """
    _seed(state_mgr, talker_config, WEB_CHAT_ID)
    await _close_open_sessions_on_shutdown(state_mgr, talker_config, client=None)

    records = list((Path(talker_config.vault.path) / "session").glob("*.md"))
    assert records == [], f"unexpected session record(s): {records}"


async def test_a_telegram_session_still_closes_at_shutdown(
    state_mgr, talker_config,
) -> None:
    """The sweep's actual job, unchanged.

    Without this, "skip web sessions" could quietly become "skip everything"
    and Telegram transcripts would stop being archived at all.
    """
    _seed(state_mgr, talker_config, TELEGRAM_CHAT_ID)

    closed = await _close_open_sessions_on_shutdown(
        state_mgr, talker_config, client=None,
    )

    assert len(closed) == 1
    assert state_mgr.get_active(TELEGRAM_CHAT_ID) is None
    import frontmatter

    post = frontmatter.load(str(Path(talker_config.vault.path) / closed[0]))
    assert post["telegram"]["close_reason"] == "shutdown"


async def test_a_mixed_sweep_closes_only_the_telegram_session(
    state_mgr, talker_config,
) -> None:
    """Both channels in one shutdown — the realistic case on a live box."""
    _seed(state_mgr, talker_config, WEB_CHAT_ID)
    _seed(state_mgr, talker_config, TELEGRAM_CHAT_ID)

    closed = await _close_open_sessions_on_shutdown(
        state_mgr, talker_config, client=None,
    )

    assert len(closed) == 1
    assert state_mgr.get_active(WEB_CHAT_ID) is not None
    assert state_mgr.get_active(TELEGRAM_CHAT_ID) is None


async def test_the_preserved_session_is_persisted_to_disk(
    state_mgr, talker_config,
) -> None:
    """In-memory survival is not survival — the next process reads the FILE.

    Also pins the other half: the Telegram session's removal must be durable
    too, or a closed-and-archived session comes back from the dead on boot.
    """
    _seed(state_mgr, talker_config, WEB_CHAT_ID)
    _seed(state_mgr, talker_config, TELEGRAM_CHAT_ID)
    await _close_open_sessions_on_shutdown(state_mgr, talker_config, client=None)

    from alfred.telegram.state import StateManager

    reloaded = StateManager(state_mgr.path)
    reloaded.load()
    assert reloaded.get_active(WEB_CHAT_ID) is not None
    assert reloaded.get_active(TELEGRAM_CHAT_ID) is None


async def test_shutdown_reports_what_it_preserved(
    state_mgr, talker_config,
) -> None:
    """ILB: the signal that would have made this defect visible."""
    _seed(state_mgr, talker_config, WEB_CHAT_ID)
    with structlog.testing.capture_logs() as captured:
        await _close_open_sessions_on_shutdown(
            state_mgr, talker_config, client=None,
        )
    events = [
        c for c in captured
        if c.get("event") == "talker.daemon.web_sessions_preserved"
    ]
    assert len(events) == 1
    assert events[0]["preserved"] == 1
    assert str(WEB_CHAT_ID) in events[0]["chat_ids"]


async def test_shutdown_reports_the_zero_case_too(
    state_mgr, talker_config,
) -> None:
    """"Nothing survived" and "the preserve branch stopped running" look
    identical without a line for the first."""
    with structlog.testing.capture_logs() as captured:
        await _close_open_sessions_on_shutdown(
            state_mgr, talker_config, client=None,
        )
    events = [
        c for c in captured
        if c.get("event") == "talker.daemon.web_sessions_preserved"
    ]
    assert len(events) == 1
    assert events[0]["preserved"] == 0
    assert "nothing to preserve" in events[0]["detail"]


# ---------------------------------------------------------------------------
# Boot — the other half of the round trip
# ---------------------------------------------------------------------------


def test_a_fresh_web_session_survives_the_startup_sweep(
    state_mgr, talker_config,
) -> None:
    """Shutdown preserving is useless if boot then closes it."""
    _seed(state_mgr, talker_config, WEB_CHAT_ID, minutes_ago=1)

    closed = session_mod.resolve_on_startup(
        state_mgr, datetime.now(timezone.utc), gap_seconds=3600,
    )

    assert closed == []
    assert state_mgr.get_active(WEB_CHAT_ID) is not None


def test_an_IDLE_web_session_still_closes_on_boot(
    state_mgr, talker_config,
) -> None:
    """THE negative pin — survival must not become immortality.

    Idle-timeout semantics are explicitly unchanged by #94. If a web session
    stopped timing out, records would again be filed under a ``started_at``
    days behind their content, which is the separate bug the open-time stash
    was added to fix. "Survives a restart" and "never expires" are different
    claims and only the first one is being made.
    """
    _seed(state_mgr, talker_config, WEB_CHAT_ID, minutes_ago=120)

    closed = session_mod.resolve_on_startup(
        state_mgr, datetime.now(timezone.utc), gap_seconds=3600,
    )

    assert len(closed) == 1, "an idle web session was not timed out"
    assert state_mgr.get_active(WEB_CHAT_ID) is None


def test_the_round_trip_end_to_end(state_mgr, talker_config) -> None:
    """Shutdown → new process reads the file → boot sweep → still resumable.

    Driven through both real entry points with a genuine reload between them,
    because the failure this fixes lives precisely in the gap between the two:
    each half was correct on its own while the conversation still died.
    """
    import asyncio

    from alfred.telegram.state import StateManager

    _seed(state_mgr, talker_config, WEB_CHAT_ID, minutes_ago=1)
    asyncio.run(
        _close_open_sessions_on_shutdown(state_mgr, talker_config, client=None)
    )

    booted = StateManager(state_mgr.path)   # the next process
    booted.load()
    session_mod.resolve_on_startup(
        booted, datetime.now(timezone.utc), gap_seconds=3600,
    )

    resumed = booted.get_active(WEB_CHAT_ID)
    assert resumed is not None, "the conversation did not survive the restart"
    assert resumed["session_id"] == f"sess-{WEB_CHAT_ID}"
