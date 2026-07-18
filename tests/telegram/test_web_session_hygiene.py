"""Talker web-session hygiene batch (2026-07) — four fixes surfaced by a
vault-reviewer pass over Salem's recent PWA conversations.

Fixes pinned here:
  1. **Empty-session suppression** — a session that closes with a 0-turn
     transcript (the PWA open-then-reopen churn, an idle Telegram open, a
     shutdown of a freshly-opened session) writes NO ``…-untitled-…`` stub
     into ``session/`` (which is in ``dont_scan_dirs`` so the janitor never
     reaps it) and instead emits the intentionally-left-blank
     ``talker.session.closed_empty`` signal. State is still cleaned.
  2. **Web idle-timeout close** — the shared ``stash_close_contract_metadata``
     helper stamps ``_vault_path_root`` (+ the rest of the close contract)
     onto a session so the daemon idle-timeout sweeper actually closes it.
     A session WITHOUT ``_vault_path_root`` is SKIPPED by the sweeper — the
     root cause of PWA sessions staying open for days (date-drift).
  4. **stt_model parity** — ``_stt_model_used`` stamped at open lands on the
     eventual record's ``telegram.stt_model`` even on the timeout-close path.

(Fix 3 — the capture-structured consumer — is a config/deploy gap, not a
code change; see the batch receipt.)

Each test names the mutation that flips it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import frontmatter
import pytest
import structlog

from alfred.telegram import session as talker_session
from alfred.telegram.session import stash_close_contract_metadata

_NOW = datetime(2026, 7, 10, 13, 0, tzinfo=timezone.utc)


def _seed(state_mgr, chat_id, *, transcript, vault_ops=None, images=None,
          documents=None, session_type="conversation", started_at=_NOW,
          last_message_at=_NOW, vault_path_root="", stt_model_used="",
          tool_set="talker"):
    """Seed one active session dict in the web/PWA shape."""
    active = {
        "session_id": f"{chat_id:08x}-0000-0000-0000-000000000000",
        "chat_id": chat_id,
        "started_at": started_at.isoformat(),
        "last_message_at": last_message_at.isoformat(),
        "model": "claude-sonnet-4-6",
        "opening_model": "claude-sonnet-4-6",
        "transcript": transcript,
        "vault_ops": vault_ops or [],
        "images": images or [],
        "documents": documents or [],
        "_session_type": session_type,
        "_user_vault_path": "person/Andrew Newton",
        "_tool_set": tool_set,
    }
    if vault_path_root:
        active["_vault_path_root"] = vault_path_root
    if stt_model_used:
        active["_stt_model_used"] = stt_model_used
    state_mgr.set_active(chat_id, active)
    state_mgr.save()


def _real_transcript() -> list[dict]:
    return [
        {"role": "user", "content": "add milk to the shopping list", "_kind": "text"},
        {"role": "assistant", "content": "done"},
    ]


# --- Fix 1: empty-session suppression -------------------------------------


def test_close_empty_session_writes_no_record_and_logs(
    state_mgr, talker_config,
) -> None:
    """A 0-turn close writes NO vault record, returns "", and emits the
    ``closed_empty`` intentionally-left-blank signal. Mutation: delete the
    empty-suppression early-return in ``close_session`` → a stub record is
    written and this fails on the glob / return-value / log asserts."""
    _seed(state_mgr, 101, transcript=[],
          vault_path_root=talker_config.vault.path)
    session_dir = Path(talker_config.vault.path) / "session"

    with structlog.testing.capture_logs() as cap:
        rel_path = talker_session.close_session(
            state_mgr, vault_path_root=talker_config.vault.path, chat_id=101,
            reason="web_session_reopened",
            user_vault_path="person/Andrew Newton",
            stt_model_used="whisper-large-v3", session_type="conversation",
            tool_set=talker_config.instance.tool_set,
        )

    assert rel_path == ""
    assert list(session_dir.glob("*.md")) == []       # no stub written
    assert state_mgr.get_active(101) is None           # active popped

    empties = [c for c in cap if c.get("event") == "talker.session.closed_empty"]
    assert len(empties) == 1
    assert empties[0]["reason"] == "web_session_reopened"
    assert empties[0]["session_type"] == "conversation"
    assert "nothing to persist" in empties[0]["detail"]
    # No normal close log fired.
    assert not [c for c in cap if c.get("event") == "talker.session.closed"]

    # No ``closed_sessions`` entry either — an empty session produced no
    # record, so it would only pollute the router history. The log above is
    # the audit trail. Mutation: re-add the append_closed → this fails.
    assert state_mgr.state.get("closed_sessions", []) == []


@pytest.mark.parametrize(
    "reason", ["timeout", "shutdown", "explicit", "cli_manual", "timeout_on_restart"],
)
def test_close_empty_session_suppressed_across_all_reasons(
    state_mgr, talker_config, reason,
) -> None:
    """Empty-suppression is reason-agnostic — it fires on EVERY close path,
    not just web reopen. Mutation: gate the suppression on
    ``reason == 'web_session_reopened'`` → the other reasons write stubs and
    this fails."""
    _seed(state_mgr, 102, transcript=[],
          vault_path_root=talker_config.vault.path)
    rel_path = talker_session.close_session(
        state_mgr, vault_path_root=talker_config.vault.path, chat_id=102,
        reason=reason, user_vault_path="person/Andrew Newton",
        stt_model_used="", session_type="conversation",
        tool_set=talker_config.instance.tool_set,
    )
    assert rel_path == ""
    assert list((Path(talker_config.vault.path) / "session").glob("*.md")) == []


def test_close_nonempty_session_still_writes_record(
    state_mgr, talker_config,
) -> None:
    """Control (over-suppression guard): a session WITH turns still writes a
    real record and returns a non-empty path. Mutation: broaden the
    suppression to fire on any session → this fails (no record)."""
    _seed(state_mgr, 103, transcript=_real_transcript(),
          vault_path_root=talker_config.vault.path)
    with structlog.testing.capture_logs() as cap:
        rel_path = talker_session.close_session(
            state_mgr, vault_path_root=talker_config.vault.path, chat_id=103,
            reason="timeout", user_vault_path="person/Andrew Newton",
            stt_model_used="whisper-large-v3", session_type="conversation",
            tool_set=talker_config.instance.tool_set,
        )
    assert rel_path
    assert (Path(talker_config.vault.path) / rel_path).exists()
    assert [c for c in cap if c.get("event") == "talker.session.closed"]
    assert not [c for c in cap if c.get("event") == "talker.session.closed_empty"]


def test_close_transcriptless_but_with_vault_ops_still_writes(
    state_mgr, talker_config,
) -> None:
    """Defensive OR-guard: an empty transcript but non-empty ``vault_ops``
    (a pathological but possible shape) is NOT suppressed — real work
    happened. Mutation: narrow the suppression to ``not session.transcript``
    only → this fails (the ops-bearing record is silently dropped)."""
    _seed(state_mgr, 104, transcript=[],
          vault_ops=[{"op": "create", "path": "task/X.md", "ts": _NOW.isoformat()}],
          vault_path_root=talker_config.vault.path)
    rel_path = talker_session.close_session(
        state_mgr, vault_path_root=talker_config.vault.path, chat_id=104,
        reason="timeout", user_vault_path="person/Andrew Newton",
        stt_model_used="", session_type="conversation",
        tool_set=talker_config.instance.tool_set,
    )
    assert rel_path
    assert (Path(talker_config.vault.path) / rel_path).exists()


# --- Fix 2: stash helper is the single source of truth --------------------


def test_stash_close_contract_metadata_stamps_all_fields(state_mgr) -> None:
    """The shared helper stamps the full close contract. Mutation: drop any
    ``active[...] = ...`` line in the helper → the matching assert fails."""
    open_session = talker_session.open_session
    open_session(state_mgr, 201, model="claude-sonnet-4-6")

    stash_close_contract_metadata(
        state_mgr, 201,
        vault_path_root="/vault/root",
        user_vault_path="person/Andrew Newton",
        stt_model_used="whisper-large-v3",
        session_type="conversation",
        tool_set="talker",
    )
    active = state_mgr.get_active(201)
    assert active["_vault_path_root"] == "/vault/root"
    assert active["_user_vault_path"] == "person/Andrew Newton"
    assert active["_stt_model_used"] == "whisper-large-v3"
    assert active["_session_type"] == "conversation"
    assert active["_tool_set"] == "talker"
    assert active["_continues_from"] is None           # stamped even when None
    assert "_pushback_level" not in active             # omitted when not given


def test_stash_close_contract_metadata_pushback_when_provided(
    state_mgr,
) -> None:
    """``_pushback_level`` is stamped only when provided (parity with the
    pre-refactor Telegram stash). Mutation: make the helper stamp it
    unconditionally → the None-case test above would gain the key."""
    talker_session.open_session(state_mgr, 202, model="claude-sonnet-4-6")
    stash_close_contract_metadata(
        state_mgr, 202, vault_path_root="/v", user_vault_path="p",
        stt_model_used="whisper-large-v3", session_type="conversation",
        tool_set="talker", pushback_level=3,
    )
    assert state_mgr.get_active(202)["_pushback_level"] == 3


# --- Fix 2: the sweeper closes a stashed web session (root cause) ----------


def test_web_session_without_vault_path_root_is_not_swept(
    state_mgr, talker_config,
) -> None:
    """ROOT-CAUSE regression pin: a web session lacking ``_vault_path_root``
    is SKIPPED by the idle-timeout sweeper — it never times out. This is the
    pre-fix behaviour that left PWA sessions open for days. Mutation: remove
    the ``if not vault_path_root: continue`` guard → the session would close
    and this (asserting it stays active) fails."""
    old = _NOW - timedelta(hours=3)
    _seed(state_mgr, 301, transcript=_real_transcript(),
          last_message_at=old, vault_path_root="")   # NO _vault_path_root
    metas = talker_session.check_timeouts_with_meta(
        state_mgr, _NOW, gap_seconds=1800,
    )
    assert metas == []                          # not swept
    assert state_mgr.get_active(301) is not None  # still open


def test_web_session_with_stash_is_swept_and_carries_stt_model(
    state_mgr, talker_config,
) -> None:
    """Fix 2 + Fix 4: a web session WITH the stashed close contract is closed
    by the idle-timeout sweeper, and the written record carries the
    configured ``stt_model`` (was '' on web-voice records) plus the
    conversation session_type and the session's start-date ``created``.
    Mutation: revert the web-open stash (or the ``_stt_model_used`` stamp)
    → the sweep produces no meta / a '' stt_model and this fails."""
    old = _NOW - timedelta(hours=3)
    _seed(state_mgr, 302, transcript=_real_transcript(),
          last_message_at=old, started_at=_NOW - timedelta(hours=3),
          vault_path_root=talker_config.vault.path,
          stt_model_used="whisper-large-v3", session_type="conversation")

    metas = talker_session.check_timeouts_with_meta(
        state_mgr, _NOW, gap_seconds=1800,
    )
    assert len(metas) == 1                        # swept (fix 2)
    assert state_mgr.get_active(302) is None       # closed
    assert metas[0]["session_type"] == "conversation"

    rel_path = metas[0]["rel_path"]
    assert rel_path
    post = frontmatter.load(str(Path(talker_config.vault.path) / rel_path))
    assert post["telegram"]["stt_model"] == "whisper-large-v3"   # fix 4
    assert post["session_type"] == "conversation"
    # Closed promptly after idle → record files under the session's own
    # start date (the date-drift the fix resolves).
    assert post["created"] == (_NOW - timedelta(hours=3)).date().isoformat()
    assert rel_path.startswith("session/conversation-")
