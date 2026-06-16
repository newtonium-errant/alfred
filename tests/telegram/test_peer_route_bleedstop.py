"""Peer-route bleed-stop tests — §1a confirm-guard + §2 stickiness.

Covers the 2026-06-16 incident fix (design doc
``PEER_ROUTE_DESIGN_2026-06-16.md``, §1+§2 subset; §3 round-trip is a
separate later arc and is NOT exercised here).

Incident: "Check Vera gh#7 confirmed closed in peer digest" was
mis-classified ``peer_route target=kal-le`` and the opened peer_route
session swallowed the next two confirm messages, force-forwarding them to
KAL-LE which never worked them.

Three surfaces:

§1a — deterministic confirm-guard (``router.is_brief_or_status_confirm``):
    a brief/status confirm returns a matched-pattern label and is forced
    local; legit peer work is NOT eaten.

§1a wiring — ``_open_routed_session(force_local_note=True)`` opens a plain
    note WITHOUT calling the LLM router (no peer-route possible on a fresh
    confirm).

§2 — stickiness: an open peer_route session (``_peer_route_target``
    stashed) (a) diverts a confirm follow-up to local + clears the stash,
    (b) force-forwards within the TTL / on a reply-to-relay, (c) stops
    force-forwarding once the TTL expires and the turn is not relay-
    anchored.

Log-emission pins (per ``feedback_log_emission_test_pattern.md``): the
ILB signals that form the self-correcting correction-signal substrate are
pinned via ``structlog.testing.capture_logs`` with their key fields.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from alfred.telegram import bot
from alfred.telegram import conversation as conversation_mod
from alfred.telegram import router


# --- §1a: is_brief_or_status_confirm pure-function guard -------------------


@pytest.mark.parametrize(
    "message,expected_label",
    [
        # Canonical tier grammar (source-of-truth brief/tier_section.py).
        ("T1 confirm", "tier_grammar"),
        ("T2 confirm", "tier_grammar"),
        ("T3 confirm walk Fergus", "tier_grammar"),
        ("T2 add eggs and milk", "tier_grammar"),
        ("T3 drop the gym thing", "tier_grammar"),
        ("t1 done", "tier_grammar"),  # lowercase + done verb
        ("  T2 keep  ", "tier_grammar"),  # leading whitespace tolerant
        # Bare leading status verbs.
        ("confirmed", "status_verb"),
        ("confirm", "status_verb"),
        ("done", "status_verb"),
        ("closed", "status_verb"),
        ("Done!", "status_verb"),
        ("  closed the issue", "status_verb"),
    ],
)
def test_confirm_guard_forces_local(message: str, expected_label: str) -> None:
    """Canonical confirm grammar returns its matched-pattern label."""
    assert router.is_brief_or_status_confirm(message) == expected_label


@pytest.mark.parametrize(
    "message",
    [
        # The exact incident phrasing — leads with "Check", so the TIGHT
        # regex deliberately does NOT match (the §1b prompt block owns this
        # fuzzy shape). Pinned so a future regex-widening can't silently
        # start eating it without updating this expectation.
        "Check Vera gh#7 confirmed closed in peer digest",
        # Legit peer work — must NOT be eaten by the guard.
        "KAL-LE, run pytest on the new branch",
        "run the tests and tell me what fails",
        "why is the transport scheduler firing twice",
        "refactor the dispatch helper",
        # Verb mid-sentence (not leading) — must NOT match the status rule.
        "I want KAL-LE to confirm the build passed",
        "ask kal-le whether the test is done",
        # Longer word starting with a guard verb — \b prevents a match.
        "confirmation needed on the deploy",
        "closure of the ticket is pending",
        # "T1" not followed by a guard verb.
        "T1 is the imminent tier, right?",
        # Empty / whitespace.
        "",
        "   ",
    ],
)
def test_confirm_guard_does_not_eat_legit_peer_work(message: str) -> None:
    """Non-confirm phrasings (incl. the incident text) return None.

    The incident text leads with "Check" so the TIGHT §1a regex skips it
    on purpose — the §1b ``_ROUTER_PROMPT`` exclusion block is the layer
    that catches that fuzzy class. This pin documents the deliberate
    boundary.
    """
    assert router.is_brief_or_status_confirm(message) is None


def test_confirm_guard_log_emission_in_handle_message_path() -> None:
    """The matched-pattern label is the correction-signal substrate.

    ``is_brief_or_status_confirm`` returns the LABEL (not a bare bool) so
    the caller can log WHICH pattern fired. Pin the label values so a
    refactor can't silently collapse them to a bool.
    """
    assert router.is_brief_or_status_confirm("T1 confirm") == "tier_grammar"
    assert router.is_brief_or_status_confirm("done") == "status_verb"
    assert router.is_brief_or_status_confirm("hello there") is None


# --- §1a wiring: force_local_note bypasses the router ----------------------


@pytest.mark.asyncio
async def test_force_local_note_skips_router_and_opens_note(
    state_mgr, talker_config,
) -> None:
    """``force_local_note=True`` opens a note WITHOUT an LLM router call.

    A FakeAnthropicClient records every ``messages.create`` call. With the
    confirm-guard short-circuit, the router must never be invoked — so
    ``client.messages.calls`` stays empty and the session is a plain note.
    """
    from tests.telegram.conftest import FakeAnthropicClient

    client = FakeAnthropicClient([])  # any call would be recorded

    sess = await bot._open_routed_session(
        state_mgr,
        talker_config,
        client,
        chat_id=1,
        first_message="T1 confirm",
        force_local_note=True,
    )

    # Router was never called — the whole point of the deterministic guard.
    assert client.messages.calls == []

    active = state_mgr.get_active(1)
    assert active is not None
    assert active["_session_type"] == "note"
    # No peer-route target could ever be stashed on a forced-local open.
    assert "_peer_route_target" not in active


@pytest.mark.asyncio
async def test_force_local_note_emits_forced_flag_log(
    state_mgr, talker_config,
) -> None:
    """The forced-local open emits ``routed_open`` with ``forced_local_note``."""
    from tests.telegram.conftest import FakeAnthropicClient

    client = FakeAnthropicClient([])
    with structlog.testing.capture_logs() as captured:
        await bot._open_routed_session(
            state_mgr, talker_config, client,
            chat_id=1, first_message="done", force_local_note=True,
        )
    matches = [
        c for c in captured
        if c.get("event") == "talker.bot.routed_open"
        and c.get("forced_local_note") is True
    ]
    assert len(matches) == 1
    assert matches[0]["session_type"] == "note"


# --- §2 helper: _is_reply_to_peer_relay -----------------------------------


@pytest.mark.parametrize(
    "parent_text,expected",
    [
        ("[KAL-LE] tests pass, 22 green", True),
        ("[STAY-C] done", True),
        ("[K.A.L.L.E.] hmm", True),  # dots allowed in the token
        ("[KAL-LE]no space after bracket", False),  # needs trailing space
        ("Salem's normal reply, not a relay", False),
        ("[lowercase] not a relay", False),  # relay token is uppercase
        ("", False),
        (None, False),
    ],
)
def test_is_reply_to_peer_relay(parent_text, expected: bool) -> None:
    """Generic ``[PEER] `` relay-prefix detection over any peer name."""
    parent = (
        None if parent_text is None
        else SimpleNamespace(text=parent_text)
    )
    assert bot._is_reply_to_peer_relay(parent) is expected


def test_is_reply_to_peer_relay_tolerates_missing_text_attr() -> None:
    """A parent without a usable ``text`` attribute returns False, not raises."""
    # Photo-only reply shape: text is None.
    assert bot._is_reply_to_peer_relay(SimpleNamespace(text=None)) is False
    # MagicMock-ish object with a non-str text.
    assert bot._is_reply_to_peer_relay(SimpleNamespace(text=123)) is False


# --- §2: handle_message stickiness harness --------------------------------


def _make_update(
    text: str,
    parent_text: str | None = None,
    chat_id: int = 100,
    user_id: int = 1,
) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.voice = None
    update.message.reply_text = AsyncMock()
    if parent_text is None:
        update.message.reply_to_message = None
    else:
        update.message.reply_to_message = SimpleNamespace(text=parent_text)
    return update


def _make_ctx(config, state_mgr, client) -> MagicMock:
    ctx = MagicMock()
    ctx.application.bot_data = {
        "config": config,
        "state_mgr": state_mgr,
        "anthropic_client": client,
        "system_prompt": "sys",
        "vault_context_str": "",
        "chat_locks": {},
        # No "raw_config" → _instance_peer_targets returns None (router
        # falls back to its global peer set). Fine: these tests seed
        # _peer_route_target directly and stub the dispatcher.
    }
    ctx.bot.send_chat_action = AsyncMock()
    return ctx


def _seed_peer_route_active(
    state_mgr,
    chat_id: int,
    *,
    target: str = "kal-le",
    age_seconds: float = 0.0,
) -> None:
    """Seed an active session with a stashed peer-route target + TTL anchor.

    ``age_seconds`` back-dates the ``_peer_route_target_ts`` so TTL-expiry
    can be exercised without sleeping.
    """
    now = datetime.now(timezone.utc)
    ts = now.timestamp() - age_seconds
    state_mgr.set_active(chat_id, {
        "session_id": f"chat{chat_id}-peerroute",
        "chat_id": chat_id,
        "started_at": now.isoformat(),
        "last_message_at": now.isoformat(),
        "model": bot._SONNET_MODEL,
        "opening_model": bot._SONNET_MODEL,
        "transcript": [],
        "vault_ops": [],
        "_vault_path_root": "",
        "_user_vault_path": "person/Test",
        "_stt_model_used": "whisper-large-v3",
        "_session_type": "peer_route",
        "_continues_from": None,
        "_peer_route_target": target,
        "_peer_route_target_ts": ts,
    })
    state_mgr.save()


@pytest.mark.asyncio
async def test_open_peer_route_followup_confirm_diverts_and_clears(
    state_mgr, talker_config, fake_client,
) -> None:
    """A confirm follow-up on an open peer session → local + stash cleared.

    Pins §2 step 1+3: the 2026-06-16 swallowing bug. ``_dispatch_peer_route``
    must NOT be called; ``run_turn`` handles the turn locally; the
    ``_peer_route_target`` stash is cleared (close-on-topic-change).
    """
    chat_id = 201
    _seed_peer_route_active(state_mgr, chat_id, age_seconds=0.0)
    update = _make_update("T2 confirm", chat_id=chat_id)
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    dispatch = AsyncMock(return_value=True)
    run_turn = AsyncMock(return_value="noted, T2 confirmed")
    orig_dispatch = bot._dispatch_peer_route
    orig_run = conversation_mod.run_turn
    try:
        bot._dispatch_peer_route = dispatch
        conversation_mod.run_turn = run_turn
        await bot.handle_message(update, ctx, text="T2 confirm", voice=False)
    finally:
        bot._dispatch_peer_route = orig_dispatch
        conversation_mod.run_turn = orig_run

    # Forwarding suppressed; handled locally.
    dispatch.assert_not_called()
    run_turn.assert_awaited()
    # Stash cleared so the topic switch sticks.
    active = state_mgr.get_active(chat_id)
    assert active is not None
    assert "_peer_route_target" not in active
    assert "_peer_route_target_ts" not in active


@pytest.mark.asyncio
async def test_peer_route_followup_within_ttl_forwards(
    state_mgr, talker_config, fake_client,
) -> None:
    """A non-confirm follow-up within the TTL still force-forwards.

    Pins §2 step 2 (sticky branch): a rapid back-and-forth stays sticky.
    """
    chat_id = 202
    _seed_peer_route_active(state_mgr, chat_id, age_seconds=5.0)  # well inside 90s
    update = _make_update("now check the lint output", chat_id=chat_id)
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    dispatch = AsyncMock(return_value=True)
    orig_dispatch = bot._dispatch_peer_route
    try:
        bot._dispatch_peer_route = dispatch
        await bot.handle_message(
            update, ctx, text="now check the lint output", voice=False,
        )
    finally:
        bot._dispatch_peer_route = orig_dispatch

    dispatch.assert_awaited_once()
    # The forward refreshes the TTL anchor (still stashed, ts bumped).
    active = state_mgr.get_active(chat_id)
    assert active is not None
    assert active["_peer_route_target"] == "kal-le"


@pytest.mark.asyncio
async def test_peer_route_followup_ttl_expiry_reenters_local(
    state_mgr, talker_config, fake_client,
) -> None:
    """Past the TTL with no relay-anchor → stop forwarding, handle locally.

    Pins §2 step 2 (expiry branch): a context switch after the window does
    NOT keep force-forwarding. The stash is cleared and ``run_turn`` runs.
    """
    chat_id = 203
    _seed_peer_route_active(state_mgr, chat_id, age_seconds=120.0)  # > 90s TTL
    update = _make_update("remind me to call the vet", chat_id=chat_id)
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    dispatch = AsyncMock(return_value=True)
    run_turn = AsyncMock(return_value="will do")
    orig_dispatch = bot._dispatch_peer_route
    orig_run = conversation_mod.run_turn
    try:
        bot._dispatch_peer_route = dispatch
        conversation_mod.run_turn = run_turn
        await bot.handle_message(
            update, ctx, text="remind me to call the vet", voice=False,
        )
    finally:
        bot._dispatch_peer_route = orig_dispatch
        conversation_mod.run_turn = orig_run

    dispatch.assert_not_called()
    run_turn.assert_awaited()
    active = state_mgr.get_active(chat_id)
    assert active is not None
    assert "_peer_route_target" not in active


@pytest.mark.asyncio
async def test_peer_route_followup_reply_anchored_overrides_ttl(
    state_mgr, talker_config, fake_client,
) -> None:
    """An explicit reply to a ``[KAL-LE] …`` relay forwards even past the TTL.

    Pins §2 step 2 (reply-anchored): an unambiguous "still talking to the
    peer" signal beats the elapsed-time window.
    """
    chat_id = 204
    _seed_peer_route_active(state_mgr, chat_id, age_seconds=600.0)  # way past TTL
    update = _make_update(
        "explain the second failure",
        parent_text="[KAL-LE] 2 tests failed: foo, bar",
        chat_id=chat_id,
    )
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    dispatch = AsyncMock(return_value=True)
    orig_dispatch = bot._dispatch_peer_route
    try:
        bot._dispatch_peer_route = dispatch
        await bot.handle_message(
            update, ctx, text="explain the second failure", voice=False,
        )
    finally:
        bot._dispatch_peer_route = orig_dispatch

    dispatch.assert_awaited_once()


# --- §2 log-emission pins -------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_divert_emits_both_ilb_logs(
    state_mgr, talker_config, fake_client,
) -> None:
    """Confirm-divert emits divert + clear logs, each with matched_pattern.

    Per ``feedback_log_emission_test_pattern.md``: drive the production
    path and assert the ILB events + key fields (the self-correcting
    correction-signal substrate).
    """
    chat_id = 205
    _seed_peer_route_active(state_mgr, chat_id, age_seconds=0.0)
    update = _make_update("T1 confirm", chat_id=chat_id)
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    run_turn = AsyncMock(return_value="ok")
    orig_dispatch = bot._dispatch_peer_route
    orig_run = conversation_mod.run_turn
    try:
        bot._dispatch_peer_route = AsyncMock(return_value=True)
        conversation_mod.run_turn = run_turn
        with structlog.testing.capture_logs() as captured:
            await bot.handle_message(
                update, ctx, text="T1 confirm", voice=False,
            )
    finally:
        bot._dispatch_peer_route = orig_dispatch
        conversation_mod.run_turn = orig_run

    forced = [
        c for c in captured
        if c.get("event") == "talker.router.confirm_guard_forced_local"
    ]
    diverted = [
        c for c in captured
        if c.get("event") == "talker.bot.peer_route_followup_diverted_to_local"
    ]
    cleared = [
        c for c in captured
        if c.get("event") == "talker.bot.peer_route_target_cleared_on_topic_change"
    ]
    assert len(forced) == 1
    assert forced[0]["matched_pattern"] == "tier_grammar"
    assert len(diverted) == 1
    assert diverted[0]["matched_pattern"] == "tier_grammar"
    assert diverted[0]["target"] == "kal-le"
    assert len(cleared) == 1
    assert cleared[0]["matched_pattern"] == "tier_grammar"
    assert cleared[0]["target"] == "kal-le"


@pytest.mark.asyncio
async def test_ttl_expiry_emits_stickiness_expired_log(
    state_mgr, talker_config, fake_client,
) -> None:
    """TTL expiry emits ``peer_route_stickiness_expired`` with the age."""
    chat_id = 206
    _seed_peer_route_active(state_mgr, chat_id, age_seconds=200.0)
    update = _make_update("something unrelated entirely", chat_id=chat_id)
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    orig_dispatch = bot._dispatch_peer_route
    orig_run = conversation_mod.run_turn
    try:
        bot._dispatch_peer_route = AsyncMock(return_value=True)
        conversation_mod.run_turn = AsyncMock(return_value="ok")
        with structlog.testing.capture_logs() as captured:
            await bot.handle_message(
                update, ctx, text="something unrelated entirely", voice=False,
            )
    finally:
        bot._dispatch_peer_route = orig_dispatch
        conversation_mod.run_turn = orig_run

    expired = [
        c for c in captured
        if c.get("event") == "talker.bot.peer_route_stickiness_expired"
    ]
    assert len(expired) == 1
    assert expired[0]["target"] == "kal-le"
    # age_seconds is populated (the back-dated anchor is ~200s old).
    assert isinstance(expired[0]["age_seconds"], (int, float))
    assert expired[0]["age_seconds"] >= 90.0
