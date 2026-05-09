"""Tests for peer-route dispatch and classifier cues.

Coverage:

- c1 self-target guard: ``_dispatch_peer_route`` returns False without
  sending the ack when the router classifies ``peer_route`` with a
  target matching our own instance.
- Normalization helper: upper-case / dotted / spaced forms all collapse
  to the lower-dashed canonical key. Legacy ``alfred`` → ``salem``
  mapping still fires.
- c3 classifier cues + self-awareness: new cues produce ``peer_route``;
  self-addressed messages strip the address; the classifier refuses
  to emit ``peer_route target=<self>`` (fallback to ``note`` with a
  warning at parse time).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.telegram import bot, router
from alfred.telegram.config import (
    AnthropicConfig,
    InstanceConfig,
    LoggingConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
)
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


# --- Normalization helper -------------------------------------------------


def test_normalize_instance_name_lowercases() -> None:
    assert bot._normalize_instance_name("KAL-LE") == "kal-le"
    assert bot._normalize_instance_name("Salem") == "salem"


def test_normalize_instance_name_strips_dots() -> None:
    """Canonical forms with dots collapse to the dashed peer key."""
    assert bot._normalize_instance_name("K.A.L.L.E.") == "kalle"
    assert bot._normalize_instance_name("S.A.L.E.M.") == "salem"


def test_normalize_instance_name_maps_spaces_to_dashes() -> None:
    assert bot._normalize_instance_name("Stay C") == "stay-c"


def test_normalize_instance_name_legacy_alfred_to_salem() -> None:
    """The default ``Alfred`` name maps to the ``salem`` peer key."""
    assert bot._normalize_instance_name("Alfred") == "salem"
    assert bot._normalize_instance_name("alfred") == "salem"


def test_normalize_instance_name_handles_empty_input() -> None:
    """Missing / None input degrades cleanly instead of raising."""
    assert bot._normalize_instance_name("") == ""
    assert bot._normalize_instance_name(None) == ""  # type: ignore[arg-type]


# --- c1 self-target guard --------------------------------------------------


def _build_update_ctx(
    talker_config: TalkerConfig,
    chat_id: int = 1,
    text: str = "",
) -> tuple[MagicMock, MagicMock]:
    """Build a mock (update, ctx) pair wired for _dispatch_peer_route."""
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock()

    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.application.bot_data = {
        "config": talker_config,
        "state_mgr": MagicMock(),
        "anthropic_client": MagicMock(),
        "system_prompt": "sys",
        "vault_context_str": "",
        "chat_locks": {},
        "raw_config": {},
    }
    return update, ctx


@pytest.mark.asyncio
async def test_self_target_returns_false_without_ack(
    talker_config: TalkerConfig,
) -> None:
    """``target=='kal-le'`` on a KAL-LE instance returns False, no ack sent."""
    talker_config.instance = InstanceConfig(
        name="KAL-LE", canonical="K.A.L.L.E.",
    )
    update, ctx = _build_update_ctx(talker_config)

    result = await bot._dispatch_peer_route(
        update, ctx,
        target="kal-le",
        text="run pytest",
        chat_id=1,
        originating_session_id="sess-abc",
    )

    assert result is False, "self-target should fall through to local handling"
    update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_self_target_casefolded_and_dotted(
    talker_config: TalkerConfig,
) -> None:
    """``target='KAL-LE'`` still matches ``self_name='kal-le'`` via normalization."""
    talker_config.instance = InstanceConfig(
        name="KAL-LE", canonical="K.A.L.L.E.",
    )
    update, ctx = _build_update_ctx(talker_config)

    result = await bot._dispatch_peer_route(
        update, ctx,
        target="KAL-LE",
        text="run pytest",
        chat_id=1,
        originating_session_id="sess-abc",
    )

    assert result is False
    update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_self_target_legacy_alfred_to_salem(
    talker_config: TalkerConfig,
) -> None:
    """A default-configured (``Alfred``) instance treats target=salem as self."""
    # talker_config already uses InstanceConfig defaults (Alfred / Alfred).
    update, ctx = _build_update_ctx(talker_config)

    result = await bot._dispatch_peer_route(
        update, ctx,
        target="salem",
        text="hi",
        chat_id=1,
        originating_session_id="sess-abc",
    )

    assert result is False
    update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_cross_instance_target_dispatches_past_guard(
    talker_config: TalkerConfig,
) -> None:
    """A real cross-instance target is NOT caught by the self guard.

    Dispatch still fails later (no raw_config has ``transport.peers``
    entries), but it must NOT short-circuit in the self-target branch —
    the ack should fire and the code path should proceed to the
    transport-config load.
    """
    talker_config.instance = InstanceConfig(name="Salem", canonical="S.A.L.E.M.")
    update, ctx = _build_update_ctx(talker_config)

    # Deliberately leave raw_config empty so transport load fails
    # downstream of the guard — what we assert here is the ack fires,
    # confirming the self-check didn't short-circuit.
    result = await bot._dispatch_peer_route(
        update, ctx,
        target="kal-le",
        text="run pytest",
        chat_id=1,
        originating_session_id="sess-abc",
    )

    assert result is False  # transport config load returns False too, but…
    # …the arrow ack should have been attempted before the failure path.
    update.message.reply_text.assert_called_once()
    sent = update.message.reply_text.call_args.args[0]
    assert "KAL-LE" in sent or "kal-le" in sent.lower()


# --- c3 classifier cues ---------------------------------------------------


def _peer_route_response(target: str = "kal-le") -> FakeResponse:
    """Build a FakeResponse carrying a valid peer_route JSON payload."""
    payload = (
        f'{{"session_type": "peer_route", "continues_from": null, '
        f'"target": "{target}", "peer_route_hint": "coding work", '
        f'"reasoning": "matched coding cue"}}'
    )
    return FakeResponse(content=[FakeBlock(type="text", text=payload)])


def _note_response() -> FakeResponse:
    payload = (
        '{"session_type": "note", "continues_from": null, '
        '"target": null, "reasoning": "generic note"}'
    )
    return FakeResponse(content=[FakeBlock(type="text", text=payload)])


@pytest.mark.parametrize(
    "message",
    [
        "run pytest",
        "run the tests please",
        "check the output of pytest tests/transport/",
        "check the output of tests",
        "pytest tests/transport/ -x",
        "npm test",
        "npm run lint",
        "fix the broken test in talker",
        "debug this test",
        "trace the failure on the router suite",
        "write a function to normalize peer names",
        "refactor this module",
        "add a test for the self-target guard",
        "git status",
        "git diff",
        "what's on this branch",
        "review the last three commits",
        "look at the diff on this branch",
    ],
)
@pytest.mark.asyncio
async def test_coding_cues_classify_peer_route(message: str) -> None:
    """Each new coding cue should be classified as peer_route on a non-self instance.

    The classifier is faked here, but the assertion that the cues reach
    the classifier in the first place + that the prompt templates around
    them is enough to catch a regression where the cue block gets dropped.
    """
    client = FakeAnthropicClient([_peer_route_response("kal-le")])
    decision = await router.classify_opening_cue(
        client,
        first_message=message,
        recent_sessions=[],
        self_name="salem",
        self_display_name="Salem",
    )
    assert decision.session_type == "peer_route"
    assert decision.target == "kal-le"
    # Ensure the cue block actually made it into the prompt by checking
    # a sample of new cue strings appear in the prompt passed to the SDK.
    prompt_body = client.messages.calls[0]["messages"][0]["content"]
    assert "run pytest" in prompt_body
    assert "npm test" in prompt_body
    assert "git status" in prompt_body


@pytest.mark.asyncio
async def test_direct_address_from_salem_routes_to_kalle() -> None:
    """On Salem, "KAL-LE, run pytest" should classify as peer_route target=kal-le."""
    client = FakeAnthropicClient([_peer_route_response("kal-le")])
    decision = await router.classify_opening_cue(
        client,
        first_message="KAL-LE, run pytest on the transport module",
        recent_sessions=[],
        self_name="salem",
        self_display_name="Salem",
    )
    assert decision.session_type == "peer_route"
    assert decision.target == "kal-le"


@pytest.mark.asyncio
async def test_direct_address_from_kalle_self_coerces_to_note() -> None:
    """On KAL-LE, a classifier emitting target=kal-le must degrade to note.

    The prompt instructs the classifier never to self-target, but it can
    still do it. Parse-time guard (`_decision_from_parsed` with
    `self_name='kal-le'`) catches the phantom and degrades to note with a
    warning — Salem would never see this path, only KAL-LE.
    """
    client = FakeAnthropicClient([_peer_route_response("kal-le")])
    decision = await router.classify_opening_cue(
        client,
        first_message="KAL-LE, run pytest on the transport module",
        recent_sessions=[],
        self_name="kal-le",
        self_display_name="K.A.L.L.E.",
    )
    # Phantom self-target → degrade to note.
    assert decision.session_type == "note"
    assert decision.target is None


@pytest.mark.asyncio
async def test_self_address_on_salem_stripped_and_classified_normally() -> None:
    """"S.A.L.E.M., tell me a joke" on Salem → NOT peer_route.

    Self-addressed-to-self should strip and classify the content normally.
    Here the classifier returns note, which stays note — there's no
    phantom self-route to coerce.
    """
    client = FakeAnthropicClient([_note_response()])
    decision = await router.classify_opening_cue(
        client,
        first_message="S.A.L.E.M., tell me a joke",
        recent_sessions=[],
        self_name="salem",
        self_display_name="S.A.L.E.M.",
    )
    assert decision.session_type == "note"
    assert decision.target is None
    # And the prompt should carry the self-awareness instruction.
    prompt_body = client.messages.calls[0]["messages"][0]["content"]
    assert "salem" in prompt_body.lower()
    assert "NEVER classify peer_route" in prompt_body


@pytest.mark.asyncio
async def test_self_name_parameter_renders_in_prompt() -> None:
    """The classifier prompt must carry the local instance's self_name.

    Load-bearing contract with the prompt template. If a future refactor
    accidentally drops the ``{self_name}`` placeholder, this catches it.
    """
    client = FakeAnthropicClient([_note_response()])
    await router.classify_opening_cue(
        client,
        first_message="just a quick note",
        recent_sessions=[],
        self_name="kal-le",
        self_display_name="K.A.L.L.E.",
    )
    prompt_body = client.messages.calls[0]["messages"][0]["content"]
    # The self-name appears in the instruction block.
    assert 'instance "kal-le"' in prompt_body
    assert "K.A.L.L.E." in prompt_body


# --- #62: per-instance peer-set validation -------------------------------


@pytest.mark.asyncio
async def test_per_instance_peer_set_accepts_configured_target() -> None:
    """On Salem with ``valid_peer_targets={kal-le, hypatia}``, ``target=kal-le``
    is accepted (regression-pin: this is Salem's normal flow today).

    Confirms the new per-instance gate doesn't break the install where the
    classifier's emitted target IS in the local ``transport.peers``.
    """
    client = FakeAnthropicClient([_peer_route_response("kal-le")])
    decision = await router.classify_opening_cue(
        client,
        first_message="run pytest",
        recent_sessions=[],
        self_name="salem",
        self_display_name="Salem",
        valid_peer_targets={"kal-le", "hypatia"},
    )
    assert decision.session_type == "peer_route"
    assert decision.target == "kal-le"


@pytest.mark.asyncio
async def test_per_instance_peer_set_rejects_unconfigured_target() -> None:
    """On Hypatia (peers={salem}), ``target=kal-le`` degrades to note (#62).

    The exact reproduction of #62: Hypatia's classifier emits ``peer_route
    target=kal-le`` on a vault-cleanup message containing technical
    vocabulary. ``kal-le`` is a globally-known peer name but NOT in
    Hypatia's ``transport.peers``. Old behaviour: routed, transport
    raised, error came back. New behaviour: degraded to note at parse
    time so Hypatia just answers the message herself.
    """
    client = FakeAnthropicClient([_peer_route_response("kal-le")])
    decision = await router.classify_opening_cue(
        client,
        first_message="quick vault cleanup, bugs ship via vault_edit",
        recent_sessions=[],
        self_name="hypatia",
        self_display_name="H.Y.P.A.T.I.A.",
        valid_peer_targets={"salem"},  # Hypatia's transport.peers minus self/local
    )
    assert decision.session_type == "note"
    assert decision.target is None


@pytest.mark.asyncio
async def test_per_instance_peer_set_omitted_falls_back_to_global() -> None:
    """``valid_peer_targets=None`` (default) preserves legacy hardcoded-set
    behaviour for tests and any caller that hasn't been updated.

    This is the backwards-compatibility pin: the existing test corpus +
    in-process callers without per-instance plumbing must keep working.
    """
    client = FakeAnthropicClient([_peer_route_response("kal-le")])
    decision = await router.classify_opening_cue(
        client,
        first_message="run pytest",
        recent_sessions=[],
        self_name="salem",
        self_display_name="Salem",
        # valid_peer_targets omitted on purpose
    )
    assert decision.session_type == "peer_route"
    assert decision.target == "kal-le"


@pytest.mark.asyncio
async def test_per_instance_rejection_logs_distinct_event() -> None:
    """The new rejection path logs ``peer_route_target_not_configured``
    with the offending target + the per-instance valid-peer set.

    Distinct event from ``peer_route_missing_target`` (the old
    hardcoded-set rejection) so future debugging can tell the two
    failure modes apart at a glance: "classifier hallucinated an
    unknown peer name" vs "valid global name but not on this instance".
    """
    from structlog.testing import capture_logs

    client = FakeAnthropicClient([_peer_route_response("kal-le")])
    with capture_logs() as captured:
        await router.classify_opening_cue(
            client,
            first_message="quick vault cleanup",
            recent_sessions=[],
            self_name="hypatia",
            self_display_name="H.Y.P.A.T.I.A.",
            valid_peer_targets={"salem"},
        )

    matching = [
        c for c in captured
        if c.get("event") == "talker.router.peer_route_target_not_configured"
    ]
    assert len(matching) == 1
    assert matching[0]["raw_target"] == "kal-le"
    assert matching[0]["valid_peers"] == ["salem"]
    assert matching[0]["log_level"] == "warning"


@pytest.mark.asyncio
async def test_per_instance_unknown_global_target_uses_legacy_log() -> None:
    """A globally-unknown target with a per-instance set still logs the
    old ``peer_route_missing_target`` event — not the new one.

    The new ``_target_not_configured`` event is only for the case
    "valid global name, not on this instance." A target that's neither
    global nor local should keep firing the original event so log
    consumers don't need to learn two grep patterns for the same
    "rubbish target" condition.
    """
    from structlog.testing import capture_logs

    payload = (
        '{"session_type": "peer_route", "continues_from": null, '
        '"target": "phantom-instance", "reasoning": "made it up"}'
    )
    client = FakeAnthropicClient(
        [FakeResponse(content=[FakeBlock(type="text", text=payload)])]
    )
    with capture_logs() as captured:
        decision = await router.classify_opening_cue(
            client,
            first_message="route to nowhere",
            recent_sessions=[],
            self_name="hypatia",
            self_display_name="H.Y.P.A.T.I.A.",
            valid_peer_targets={"salem"},
        )

    assert decision.session_type == "note"
    assert decision.target is None
    legacy_events = [
        c for c in captured
        if c.get("event") == "talker.router.peer_route_missing_target"
    ]
    not_configured_events = [
        c for c in captured
        if c.get("event") == "talker.router.peer_route_target_not_configured"
    ]
    assert len(legacy_events) == 1
    assert not_configured_events == [], (
        "phantom-instance is not a globally-known peer; the new event "
        "should only fire for valid global names that aren't local"
    )


# --- #62: TransportError clears the peer-route stash ---------------------


@pytest.mark.asyncio
async def test_transport_error_clears_peer_route_stash(
    talker_config: TalkerConfig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the transport raises mid-route, the stash gets cleared so the
    next message in the same session won't keep hitting the dead peer.

    Reproduces the second half of #62: Hypatia's first cleanup message
    routed to kal-le, transport failed, error returned. A SECOND
    cleanup message in the same session re-routed to kal-le and failed
    identically because ``_peer_route_target`` was still stashed. The
    fix: pop the stash inline on TransportError before returning True.
    """
    from alfred.transport.exceptions import TransportError

    talker_config.instance = InstanceConfig(name="Salem", canonical="S.A.L.E.M.")

    # Build a state_mgr that holds a stashed _peer_route_target.
    state_mgr = MagicMock()
    state_mgr.get_active = MagicMock(
        return_value={
            "_peer_route_target": "kal-le",
            "session_id": "sess-abc",
        }
    )
    state_mgr.set_active = MagicMock()
    state_mgr.save = MagicMock()

    # raw_config has a transport.peers entry so the dispatch path proceeds
    # past the "no raw_config" early return — the failure mode we want
    # to drive is ``peer_send`` raising TransportError, not config-load.
    raw_config = {
        "transport": {
            "peers": {
                "kal-le": {
                    "base_url": "http://127.0.0.1:8892",
                    "token": "DUMMY_TEST_TOKEN",
                },
            },
        },
    }

    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat.id = 1
    update.message.text = "run pytest"
    update.message.reply_text = AsyncMock()

    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    ctx.application.bot_data = {
        "config": talker_config,
        "state_mgr": state_mgr,
        "anthropic_client": MagicMock(),
        "system_prompt": "sys",
        "vault_context_str": "",
        "chat_locks": {},
        "raw_config": raw_config,
    }

    # Force ``peer_send`` to raise the TransportError that the bug path
    # triggers in production (network problem, 5xx, connection reset…).
    async def _raise_transport_error(*_args, **_kwargs) -> None:
        raise TransportError("simulated upstream failure")

    monkeypatch.setattr(
        "alfred.transport.client.peer_send", _raise_transport_error
    )

    result = await bot._dispatch_peer_route(
        update, ctx,
        target="kal-le",
        text="run pytest",
        chat_id=1,
        originating_session_id="sess-abc",
    )

    # TransportError branch: handler returns True (error already
    # surfaced to the operator).
    assert result is True
    # User saw the reply for the ack ("→ KAL-LE") and the failure
    # message. The exact count is ack + failure = 2.
    assert update.message.reply_text.await_count == 2
    failure_call = update.message.reply_text.await_args_list[-1]
    assert "Couldn't reach KAL-LE" in failure_call.args[0]

    # The fix: stash gets popped inline.
    state_mgr.set_active.assert_called_once()
    set_active_args = state_mgr.set_active.call_args
    assert set_active_args.args[0] == 1  # chat_id
    cleared_active = set_active_args.args[1]
    assert "_peer_route_target" not in cleared_active, (
        "TransportError must pop _peer_route_target so a follow-up "
        "message in the same session doesn't keep routing to a dead peer"
    )
    state_mgr.save.assert_called_once()


@pytest.mark.asyncio
async def test_default_self_name_preserves_legacy_behaviour() -> None:
    """Calling without self_name/self_display_name still works (defaults).

    Tests and any future in-process caller that doesn't know about
    stage-3.5 plumbing can still call the router. Default is
    ``self_name=""`` (empty) per
    ``feedback_hardcoding_and_alfred_naming.md`` — the parse-time
    self-target guard treats empty as "no check" so the router still
    returns a decision, but the prompt body renders ``instance ""``
    as a loud-failure signal for misconfigured callers (vs the prior
    silent ``"salem"`` fallback that hid single-instance assumptions
    on multi-instance installs).
    """
    client = FakeAnthropicClient([_note_response()])
    decision = await router.classify_opening_cue(
        client,
        first_message="just a quick note",
        recent_sessions=[],
    )
    assert decision.session_type == "note"
