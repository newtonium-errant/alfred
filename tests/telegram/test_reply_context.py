"""Tests for the Telegram reply-context consumer.

When a user long-presses one of Salem's earlier messages and hits "Reply,"
the Bot API attaches the parent message via ``Message.reply_to_message``.
The talker prepends a machine-generated ``[You are replying to Salem's
earlier message at <ts>: "..."]`` prefix to the turn text so downstream
paths (router + Anthropic turn) see the reply attribution inline.

Covers:
    * ``_build_reply_context_prefix`` — the pure helper that renders the
      prefix from a PTB-shaped parent message object.
    * ``handle_message`` integration — prefix reaches ``conversation.run_turn``
      as the ``user_message`` arg.
    * Router hint — ``has_reply_context`` threads through the open-session
      path into ``classify_opening_cue``.
    * Active-session fast path — a reply with an active session skips the
      router entirely, falls into the existing run-turn flow.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.telegram import bot
from alfred.telegram import conversation as conversation_mod
from alfred.telegram import router as router_mod


# --- Helpers --------------------------------------------------------------


def _fake_parent_message(
    text: str | None = None,
    caption: str | None = None,
    from_bot: bool = True,
    date: datetime | None = None,
) -> SimpleNamespace:
    """Build a PTB-shaped parent message stand-in for the helper tests.

    We use SimpleNamespace rather than a MagicMock so ``getattr`` with a
    default behaves identically to the real ``telegram.Message`` (the
    helper reads ``text``, ``caption``, ``from_user``, ``date``).
    """
    if date is None:
        date = datetime(2026, 4, 21, 10, 30, 0, tzinfo=timezone.utc)
    from_user = SimpleNamespace(is_bot=from_bot, id=42)
    return SimpleNamespace(
        text=text,
        caption=caption,
        from_user=from_user,
        date=date,
    )


# --- _build_reply_context_prefix pure tests -------------------------------


def test_short_parent_text_produces_prefix() -> None:
    """A short bot reply-parent yields a clean ``[You are replying...]`` prefix."""
    parent = _fake_parent_message(
        text="Greenwood METAR: VFR, wind 270@8, vis 10SM.",
    )
    prefix = bot._build_reply_context_prefix(parent)
    assert prefix is not None
    assert prefix.startswith("[You are replying to Salem's earlier message at ")
    assert '"Greenwood METAR: VFR, wind 270@8, vis 10SM."]' in prefix
    # Prefix must end with a blank-line separator so the user's text
    # lands below the prefix, not appended to its closing bracket.
    assert prefix.endswith("\n\n")


def test_long_parent_is_truncated_with_suffix() -> None:
    """Parent longer than 500 chars → truncated + ``... (truncated)`` suffix."""
    long_text = "x" * 800
    parent = _fake_parent_message(text=long_text)
    prefix = bot._build_reply_context_prefix(parent)
    assert prefix is not None
    # The quoted segment should contain exactly 500 x's then the suffix.
    assert "x" * 500 + "... (truncated)" in prefix
    # And must NOT contain 501 consecutive x's (the limit is load-bearing).
    assert "x" * 501 not in prefix


def test_no_reply_returns_none() -> None:
    """``reply_to_message is None`` → helper returns ``None`` (no prefix)."""
    assert bot._build_reply_context_prefix(None) is None


def test_reply_to_bot_uses_salem_attribution() -> None:
    """Bot-authored parent → ``Salem's earlier message`` attribution.

    The default ``instance_name`` arg is ``"Salem"`` so existing
    direct-call sites (and this test) keep the original prefix shape.
    The configured-instance path is exercised separately by
    ``test_reply_prefix_uses_instance_name_*``.
    """
    parent = _fake_parent_message(text="Morning brief: Tuesday.", from_bot=True)
    prefix = bot._build_reply_context_prefix(parent)
    assert prefix is not None
    assert "Salem's earlier message" in prefix
    assert "your earlier message" not in prefix


@pytest.mark.parametrize(
    "instance_name",
    ["Salem", "Hypatia", "KAL-LE"],
)
def test_reply_prefix_uses_instance_name_argument(instance_name: str) -> None:
    """The bot-attribution branch interpolates the configured instance name.

    Each multi-instance bot needs its own attribution literal so the
    Anthropic prompt context matches who's actually replying. The pure
    helper takes the name as an argument; the integration plumbing
    (config.instance.name → bot.handle_message) is covered in the
    handle_message-level case below.
    """
    parent = _fake_parent_message(text="hello there", from_bot=True)
    prefix = bot._build_reply_context_prefix(
        parent, instance_name=instance_name,
    )
    assert prefix is not None
    assert f"{instance_name}'s earlier message" in prefix
    # No other instance leaks into the prefix.
    for other in {"Salem", "Hypatia", "KAL-LE"} - {instance_name}:
        assert f"{other}'s earlier message" not in prefix


def test_reply_to_own_message_uses_your_attribution() -> None:
    """Non-bot parent → ``your earlier message`` attribution.

    Multi-user future work; today's single-allowlisted-user case covers
    replies to one's own earlier messages for reference.
    """
    parent = _fake_parent_message(text="a prior thought", from_bot=False)
    prefix = bot._build_reply_context_prefix(parent)
    assert prefix is not None
    assert "your earlier message" in prefix
    assert "Salem's earlier message" not in prefix


def test_photo_only_reply_returns_none() -> None:
    """Parent with no text and no caption (photo-only) → helper returns ``None``."""
    parent = _fake_parent_message(text=None, caption=None)
    prefix = bot._build_reply_context_prefix(parent)
    assert prefix is None


def test_photo_with_caption_uses_caption() -> None:
    """Caption is used as the quoted body when ``text`` is absent."""
    parent = _fake_parent_message(text=None, caption="RRTS route map PDF")
    prefix = bot._build_reply_context_prefix(parent)
    assert prefix is not None
    assert '"RRTS route map PDF"' in prefix


def test_parent_with_embedded_quotes_survives() -> None:
    """Parent text containing literal ``"`` renders verbatim, no escaping artifacts."""
    parent = _fake_parent_message(
        text='He said "book it" and I need confirmation.',
    )
    prefix = bot._build_reply_context_prefix(parent)
    assert prefix is not None
    # Literal inner quotes should appear unmodified. The surrounding
    # prefix uses double quotes too — the Anthropic SDK serialises turn
    # content via JSON so quote-escaping is the SDK's problem, not ours.
    assert 'He said "book it" and I need confirmation.' in prefix


def test_timestamp_normalised_to_utc() -> None:
    """Non-UTC tz-aware parent date → prefix shows UTC ISO timestamp."""
    # 10:30 UTC-4 is 14:30 UTC. Use a fixed offset to make the expected
    # string deterministic.
    naive_eastern = datetime(
        2026, 4, 21, 10, 30, 0,
        tzinfo=timezone(timedelta(hours=-4)),
    )
    parent = _fake_parent_message(text="hi", date=naive_eastern)
    prefix = bot._build_reply_context_prefix(parent)
    assert prefix is not None
    # UTC rendering should produce "14:30:00+00:00".
    assert "2026-04-21T14:30:00+00:00" in prefix


def test_whitespace_only_parent_returns_none() -> None:
    """A parent message whose text is all whitespace → no prefix."""
    parent = _fake_parent_message(text="   \n\t  ")
    prefix = bot._build_reply_context_prefix(parent)
    assert prefix is None


def test_multiline_user_reply_preserved_when_concatenated() -> None:
    """The helper returns a ``\\n\\n``-separator prefix; the user's multiline
    reply body stays intact when the caller concatenates.

    The helper itself only renders the prefix — verify the concatenation
    shape the caller uses (``prefix + user_text``) preserves newlines in
    the reply body.
    """
    parent = _fake_parent_message(text="RRTS brief")
    prefix = bot._build_reply_context_prefix(parent)
    assert prefix is not None

    user_reply = "line one\nline two\nline three"
    combined = f"{prefix}{user_reply}"

    # All three user lines should be present after the prefix.
    assert combined.endswith(user_reply)
    # The prefix and the reply are separated by a blank line.
    assert "\n\n" + "line one" in combined


# --- handle_message integration: prefix reaches run_turn ------------------


def _make_update_with_reply(
    text: str,
    parent_text: str | None = "earlier bot reply",
    parent_from_bot: bool = True,
    parent_date: datetime | None = None,
    chat_id: int = 100,
    user_id: int = 1,
) -> MagicMock:
    """Build an Update whose ``message.reply_to_message`` is populated."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.voice = None
    update.message.reply_text = AsyncMock()
    if parent_text is None:
        update.message.reply_to_message = None
    else:
        update.message.reply_to_message = _fake_parent_message(
            text=parent_text,
            from_bot=parent_from_bot,
            date=parent_date,
        )
    return update


def _make_update_no_reply(
    text: str,
    chat_id: int = 100,
    user_id: int = 1,
) -> MagicMock:
    """Build an Update with NO ``reply_to_message`` (the legacy shape)."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.voice = None
    update.message.reply_text = AsyncMock()
    update.message.reply_to_message = None
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
    }
    ctx.bot.send_chat_action = AsyncMock()
    return ctx


def _seed_active_session(
    state_mgr, chat_id: int, model: str | None = None,
) -> None:
    """Seed an active session dict so handle_message skips the router."""
    now = datetime.now(timezone.utc).isoformat()
    state_mgr.set_active(chat_id, {
        "session_id": f"chat{chat_id}-reply-test",
        "chat_id": chat_id,
        "started_at": now,
        "last_message_at": now,
        "model": model or bot._SONNET_MODEL,
        "opening_model": model or bot._SONNET_MODEL,
        "transcript": [],
        "vault_ops": [],
        "_vault_path_root": "",
        "_user_vault_path": "person/Test",
        "_stt_model_used": "whisper-large-v3",
        "_session_type": "note",
        "_continues_from": None,
    })
    state_mgr.save()


@pytest.mark.asyncio
async def test_reply_prefix_reaches_run_turn_captured(
    state_mgr, talker_config, fake_client,
) -> None:
    """The ``user_message`` kwarg to run_turn begins with the
    ``[You are replying...]`` prefix and ends with the user's own text.
    """
    chat_id = 102
    _seed_active_session(state_mgr, chat_id)

    update = _make_update_with_reply(
        text="book it",
        parent_text="Flight SWA2941 available at 0730 for $189",
        parent_from_bot=True,
        chat_id=chat_id,
    )
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    captured: dict[str, object] = {}

    async def fake_run_turn(**kwargs):
        captured.update(kwargs)
        return "ok, booking."

    original = conversation_mod.run_turn
    try:
        conversation_mod.run_turn = fake_run_turn
        await bot.handle_message(update, ctx, text="book it", voice=False)
    finally:
        conversation_mod.run_turn = original

    user_message = captured.get("user_message")
    assert isinstance(user_message, str)
    # The default ``talker_config`` fixture uses InstanceConfig defaults
    # (name="Alfred"); the prefix attribution mirrors the configured
    # instance name, so we expect "Alfred's earlier message" here. The
    # Salem / Hypatia attributions are exercised explicitly in the
    # ``test_reply_prefix_uses_instance_name_*`` cases below.
    assert user_message.startswith("[You are replying to Alfred's earlier message at ")
    assert 'Flight SWA2941 available at 0730 for $189' in user_message
    # The user's own text is present AFTER the prefix (trailing position).
    assert user_message.endswith("book it")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "instance_name",
    ["Salem", "Hypatia"],
)
async def test_reply_prefix_threads_config_instance_name(
    state_mgr, talker_config, fake_client, instance_name: str,
) -> None:
    """``handle_message`` reads ``config.instance.name`` for the prefix.

    Without this plumbing the prefix would still say "Salem's earlier
    message" on a Hypatia-configured bot — wrong attribution, wrong
    Anthropic context. The integration shape is the contract: change
    ``config.instance.name``, change the prefix.
    """
    from alfred.telegram.config import InstanceConfig

    talker_config.instance = InstanceConfig(
        name=instance_name,
        canonical=instance_name,
        aliases=[instance_name],
    )

    chat_id = 200 + hash(instance_name) % 100
    _seed_active_session(state_mgr, chat_id)

    update = _make_update_with_reply(
        text="continue please",
        parent_text="earlier output from the bot",
        parent_from_bot=True,
        chat_id=chat_id,
    )
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    captured: dict[str, object] = {}

    async def fake_run_turn(**kwargs):
        captured.update(kwargs)
        return "ok."

    original = conversation_mod.run_turn
    try:
        conversation_mod.run_turn = fake_run_turn
        await bot.handle_message(
            update, ctx, text="continue please", voice=False,
        )
    finally:
        conversation_mod.run_turn = original

    user_message = captured.get("user_message")
    assert isinstance(user_message, str)
    assert f"{instance_name}'s earlier message" in user_message


@pytest.mark.asyncio
async def test_no_reply_no_prefix(
    state_mgr, talker_config, fake_client,
) -> None:
    """No ``reply_to_message`` → ``run_turn`` sees the original text, unchanged.

    This is the existing behaviour — the reply-context consumer must be
    a pure addition for non-reply messages, byte-for-byte no-op.
    """
    chat_id = 103
    _seed_active_session(state_mgr, chat_id)

    update = _make_update_no_reply(text="hello there", chat_id=chat_id)
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    captured: dict[str, object] = {}

    async def fake_run_turn(**kwargs):
        captured.update(kwargs)
        return "hi"

    original = conversation_mod.run_turn
    try:
        conversation_mod.run_turn = fake_run_turn
        await bot.handle_message(update, ctx, text="hello there", voice=False)
    finally:
        conversation_mod.run_turn = original

    assert captured["user_message"] == "hello there"


@pytest.mark.asyncio
async def test_active_session_skips_router_on_reply(
    state_mgr, talker_config, fake_client,
) -> None:
    """Reply with an active session → no router call, direct to run_turn.

    This is the critical "fast-path" behaviour: when an active session
    exists, the router never runs (same as any turn within a session).
    A reply-to-bot lands in the same flow. ``has_reply_context`` only
    matters when the router IS invoked; for the active-session case the
    signal is "continue what you're already doing."
    """
    chat_id = 104
    _seed_active_session(state_mgr, chat_id)

    update = _make_update_with_reply(
        text="explain the second failure",
        parent_text="[KAL-LE] 2 failures in tests/vault/test_scope.py",
        parent_from_bot=True,
        chat_id=chat_id,
    )
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    # Spy on the router to confirm it's NEVER invoked.
    router_called = False

    async def fake_classify(*args, **kwargs):
        nonlocal router_called
        router_called = True
        return router_mod.RouterDecision(
            session_type="note", model="claude-sonnet-4-6",
            continues_from=None,
        )

    original_classify = router_mod.classify_opening_cue
    original_run_turn = conversation_mod.run_turn
    try:
        router_mod.classify_opening_cue = fake_classify
        conversation_mod.run_turn = AsyncMock(return_value="looking now")
        await bot.handle_message(
            update, ctx, text="explain the second failure", voice=False,
        )
    finally:
        router_mod.classify_opening_cue = original_classify
        conversation_mod.run_turn = original_run_turn

    assert router_called is False, (
        "router must NOT fire when an active session already exists"
    )


@pytest.mark.asyncio
async def test_no_active_session_reply_passes_hint_to_router(
    state_mgr, talker_config,
) -> None:
    """Reply with NO active session → router is invoked with ``has_reply_context=True``."""
    chat_id = 105
    # No _seed_active_session call — state is empty.

    update = _make_update_with_reply(
        text="what was the source?",
        parent_text="Greenwood weather forecast: clear, wind calm.",
        parent_from_bot=True,
        chat_id=chat_id,
    )

    captured_kwargs: dict[str, object] = {}

    async def fake_classify(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return router_mod.RouterDecision(
            session_type="note", model="claude-sonnet-4-6",
            continues_from=None,
        )

    # Stub run_turn too so the test completes without an LLM call.
    original_classify = router_mod.classify_opening_cue
    original_run_turn = conversation_mod.run_turn
    try:
        router_mod.classify_opening_cue = fake_classify
        conversation_mod.run_turn = AsyncMock(return_value="aviationweather.gov")
        # Fake client not used because we stubbed classify.
        ctx = _make_ctx(talker_config, state_mgr, MagicMock())
        await bot.handle_message(
            update, ctx, text="what was the source?", voice=False,
        )
    finally:
        router_mod.classify_opening_cue = original_classify
        conversation_mod.run_turn = original_run_turn

    assert captured_kwargs.get("has_reply_context") is True, (
        "router must receive has_reply_context=True when the turn is a "
        "reply and no active session exists"
    )


@pytest.mark.asyncio
async def test_no_active_session_non_reply_does_not_set_hint(
    state_mgr, talker_config,
) -> None:
    """Non-reply with no active session → ``has_reply_context`` omitted or False."""
    chat_id = 106

    update = _make_update_no_reply(text="quick capture", chat_id=chat_id)

    captured_kwargs: dict[str, object] = {}

    async def fake_classify(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return router_mod.RouterDecision(
            session_type="note", model="claude-sonnet-4-6",
            continues_from=None,
        )

    original_classify = router_mod.classify_opening_cue
    original_run_turn = conversation_mod.run_turn
    try:
        router_mod.classify_opening_cue = fake_classify
        conversation_mod.run_turn = AsyncMock(return_value="noted")
        ctx = _make_ctx(talker_config, state_mgr, MagicMock())
        await bot.handle_message(
            update, ctx, text="quick capture", voice=False,
        )
    finally:
        router_mod.classify_opening_cue = original_classify
        conversation_mod.run_turn = original_run_turn

    # Default is False; either omitted-from-kwargs or explicit False is OK.
    hint_value = captured_kwargs.get("has_reply_context", False)
    assert hint_value is False


# --- Router signature-level tests -----------------------------------------


@pytest.mark.asyncio
async def test_router_accepts_has_reply_context_kwarg() -> None:
    """``classify_opening_cue(..., has_reply_context=True)`` is a valid call.

    Smoke test: the router prompt takes the hint as a template variable,
    so a ``KeyError`` on missing format arg would surface immediately.
    Using a BoomClient forces the API-error fallback path, so we don't
    need a full fake response — we just need the prompt-build to not
    raise.
    """

    class BoomMessages:
        async def create(self, **kwargs):
            raise RuntimeError("forced error")

    class BoomClient:
        messages = BoomMessages()

    decision = await router_mod.classify_opening_cue(
        BoomClient(),
        first_message="[You are replying to Salem's earlier message at ...] hi",
        recent_sessions=[],
        has_reply_context=True,
    )
    # Fallback decision on API error; critically, no template-format crash.
    assert decision.session_type == "note"


@pytest.mark.asyncio
async def test_router_has_reply_context_appears_in_prompt() -> None:
    """The ``has_reply_context`` flag is injected into the prompt text.

    We capture the kwargs passed to ``client.messages.create`` and check
    the prompt contains the ``has_reply_context=true`` token so the
    classifier can actually read the signal.
    """
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(
            type="text",
            text='{"session_type": "note", "continues_from": null, '
                 '"reasoning": "reply follow-up"}',
        )]),
    ])

    await router_mod.classify_opening_cue(
        client,
        first_message="[You are replying to Salem's earlier message at "
                      "2026-04-21T10:30:00+00:00: \"brief\"]\n\nwhat's the source?",
        recent_sessions=[],
        has_reply_context=True,
    )

    # The router made exactly one call; the prompt is the first message's content.
    assert len(client.messages.calls) == 1
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "has_reply_context=true" in prompt


@pytest.mark.asyncio
async def test_router_has_reply_context_false_default() -> None:
    """When ``has_reply_context`` is omitted, prompt contains ``=false``."""
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(
            type="text",
            text='{"session_type": "note", "continues_from": null, '
                 '"reasoning": "fresh note"}',
        )]),
    ])

    await router_mod.classify_opening_cue(
        client,
        first_message="quick note",
        recent_sessions=[],
    )

    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "has_reply_context=false" in prompt
