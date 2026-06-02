"""``/calibration_ok [tier [value]]`` slash-command handler tests.

Task #57 (2026-06-02). The handler historically accepted only the
two forms ``/calibration_ok`` (list) and ``/calibration_ok <tier>``
(flip to True). c5 (Email high-priority Telegram push) shipped
2026-06-01 (commit ``8f01640``); the prompt-tuner SKILL audit
(commit ``f7e27df`` on a sibling branch) advertised
``/calibration_ok high false`` as the disable path — but the
handler hardcoded ``True`` and ignored ``parts[2]``. This file pins
the widened third-arg behaviour:

  * ``/calibration_ok high false`` → ``set_confidence(...False)``.
  * ``/calibration_ok high off``   → same.
  * ``/calibration_ok high``       → ``set_confidence(...True)``
    (idempotent forward-path preserved).
  * ``/calibration_ok high frobnicate`` → friendly reject, no state
    change.
  * Reply text differentiates enable / disable so the operator sees
    which direction the flip went.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.telegram import bot


# --- Fixtures + mock builders --------------------------------------------


def _make_update_mock(
    *,
    text: str,
    chat_id: int = 42,
    user_id: int = 42,
) -> MagicMock:
    """Build a Telegram-Update-shape mock for the slash-command handler.

    Required surface: ``message.text`` (the slash command text),
    ``message.reply_text`` (awaitable), ``effective_user.id`` (for the
    ``_is_allowed`` gate), ``effective_chat.id``.
    """
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    return update


def _make_ctx_mock(
    *,
    raw_config: dict | None = None,
    allowed_user_id: int = 42,
) -> MagicMock:
    """Build a Telegram-context-shape mock for ``on_calibration_ok``.

    Required surface: ``application.bot_data[_KEY_CONFIG]`` (a
    TalkerConfig-shaped MagicMock with ``allowed_users``) and
    ``application.bot_data["raw_config"]`` (the unified config dict
    the handler passes to ``load_from_unified``).
    """
    config = MagicMock()
    config.allowed_users = [allowed_user_id]
    ctx = MagicMock()
    ctx.application.bot_data = {
        bot._KEY_CONFIG: config,
        "raw_config": raw_config or {"daily_sync": {"enabled": True}},
    }
    return ctx


@pytest.fixture
def state_path(tmp_path: Path) -> str:
    """Return a tmp_path-rooted daily_sync state path; the handler
    persists into this via set_confidence."""
    return str(tmp_path / "daily_sync_state.json")


@pytest.fixture
def raw_config_with_state_path(state_path: str) -> dict:
    """Build a unified config dict whose ``daily_sync.state.path``
    points at the test's tmp state file. Required so the handler's
    set_confidence call persists to a tmp location rather than the
    default ``./data/daily_sync_state.json`` (which would leak across
    tests and into the dev environment)."""
    return {
        "daily_sync": {
            "enabled": True,
            "state": {"path": state_path},
        },
    }


# --- Enable-path (preserved behaviour) ------------------------------------


@pytest.mark.asyncio
async def test_calibration_ok_no_arg_defaults_true(
    raw_config_with_state_path: dict,
    state_path: str,
) -> None:
    """``/calibration_ok high`` (no third arg) still flips True. The
    forward path must remain idempotent after the widening — SKILL
    docs across the codebase reference the bare-tier form."""
    update = _make_update_mock(text="/calibration_ok high")
    ctx = _make_ctx_mock(raw_config=raw_config_with_state_path)

    await bot.on_calibration_ok(update, ctx)

    # Verify state side-effect: confidence.high flipped to True.
    from alfred.daily_sync.confidence import load_state
    state = load_state(state_path)
    assert state["confidence"]["high"] is True

    # Verify the reply mentions the enable verb.
    update.message.reply_text.assert_awaited_once()
    reply = update.message.reply_text.await_args.args[0]
    assert "enabled" in reply.lower()


# --- Disable tokens (Task #57 new behaviour) ------------------------------


@pytest.mark.asyncio
async def test_calibration_ok_disable_token_flips_false(
    raw_config_with_state_path: dict,
    state_path: str,
) -> None:
    """``/calibration_ok high false`` → ``set_confidence(value=False)``.

    Seed the state first so we can distinguish "flag was True, got
    flipped to False" from "flag was already False."
    """
    # Seed: high=True via the bare-tier form first.
    pre_update = _make_update_mock(text="/calibration_ok high")
    pre_ctx = _make_ctx_mock(raw_config=raw_config_with_state_path)
    await bot.on_calibration_ok(pre_update, pre_ctx)

    from alfred.daily_sync.confidence import load_state
    assert load_state(state_path)["confidence"]["high"] is True

    # Now disable.
    update = _make_update_mock(text="/calibration_ok high false")
    ctx = _make_ctx_mock(raw_config=raw_config_with_state_path)

    await bot.on_calibration_ok(update, ctx)

    state = load_state(state_path)
    assert state["confidence"]["high"] is False


@pytest.mark.asyncio
async def test_calibration_ok_off_token_flips_false(
    raw_config_with_state_path: dict,
    state_path: str,
) -> None:
    """``/calibration_ok high off`` is equivalent to ``high false``."""
    # Seed: enable first.
    pre_update = _make_update_mock(text="/calibration_ok high")
    pre_ctx = _make_ctx_mock(raw_config=raw_config_with_state_path)
    await bot.on_calibration_ok(pre_update, pre_ctx)

    # Disable via ``off``.
    update = _make_update_mock(text="/calibration_ok high off")
    ctx = _make_ctx_mock(raw_config=raw_config_with_state_path)
    await bot.on_calibration_ok(update, ctx)

    from alfred.daily_sync.confidence import load_state
    state = load_state(state_path)
    assert state["confidence"]["high"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("disable_token", [
    "false", "off", "0", "no", "disable", "disabled",
    "FALSE", "Off", "DISABLE",  # case-insensitive
])
async def test_calibration_ok_all_disable_tokens(
    raw_config_with_state_path: dict,
    state_path: str,
    disable_token: str,
) -> None:
    """Every token in ``_CALIBRATION_OK_DISABLE_TOKENS`` flips False —
    plus case-insensitive variants. Pins the vocab so a token rename
    surfaces here rather than as a silent operator-UX regression."""
    # Seed enable first.
    pre_update = _make_update_mock(text="/calibration_ok medium")
    pre_ctx = _make_ctx_mock(raw_config=raw_config_with_state_path)
    await bot.on_calibration_ok(pre_update, pre_ctx)

    update = _make_update_mock(text=f"/calibration_ok medium {disable_token}")
    ctx = _make_ctx_mock(raw_config=raw_config_with_state_path)
    await bot.on_calibration_ok(update, ctx)

    from alfred.daily_sync.confidence import load_state
    state = load_state(state_path)
    assert state["confidence"]["medium"] is False, (
        f"disable token {disable_token!r} did not flip medium to False"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enable_token", [
    "true", "on", "1", "yes", "enable", "enabled",
    "TRUE", "On", "YES",
])
async def test_calibration_ok_all_enable_tokens(
    raw_config_with_state_path: dict,
    state_path: str,
    enable_token: str,
) -> None:
    """Every token in ``_CALIBRATION_OK_ENABLE_TOKENS`` flips True.

    Seed False first (via known-good disable token ``off``) so a
    no-op flip can't make this test trivially pass.
    """
    # Seed disabled first (assume off token works; covered by another test).
    pre_update = _make_update_mock(text="/calibration_ok low off")
    pre_ctx = _make_ctx_mock(raw_config=raw_config_with_state_path)
    await bot.on_calibration_ok(pre_update, pre_ctx)

    update = _make_update_mock(text=f"/calibration_ok low {enable_token}")
    ctx = _make_ctx_mock(raw_config=raw_config_with_state_path)
    await bot.on_calibration_ok(update, ctx)

    from alfred.daily_sync.confidence import load_state
    state = load_state(state_path)
    assert state["confidence"]["low"] is True, (
        f"enable token {enable_token!r} did not flip low to True"
    )


# --- Reject path (unknown value arg) --------------------------------------


@pytest.mark.asyncio
async def test_calibration_ok_unknown_value_arg_rejected(
    raw_config_with_state_path: dict,
    state_path: str,
) -> None:
    """``/calibration_ok high frobnicate`` → friendly error reply,
    NO state change. Per the dispatch: silently flipping True on an
    unknown third arg would mislead an operator who typed something
    they meant as disable."""
    # Seed disabled so we can detect a stray flip.
    pre_update = _make_update_mock(text="/calibration_ok high off")
    pre_ctx = _make_ctx_mock(raw_config=raw_config_with_state_path)
    await bot.on_calibration_ok(pre_update, pre_ctx)

    from alfred.daily_sync.confidence import load_state
    assert load_state(state_path)["confidence"]["high"] is False

    # Send the unknown value.
    update = _make_update_mock(text="/calibration_ok high frobnicate")
    ctx = _make_ctx_mock(raw_config=raw_config_with_state_path)
    await bot.on_calibration_ok(update, ctx)

    # State unchanged.
    assert load_state(state_path)["confidence"]["high"] is False

    # Reply names the unknown token + offers the legal vocab.
    update.message.reply_text.assert_awaited_once()
    reply = update.message.reply_text.await_args.args[0]
    assert "frobnicate" in reply.lower()
    # Reply mentions at least one legal token from each side so the
    # operator can correct without consulting docs.
    assert "off" in reply.lower() or "false" in reply.lower()
    assert "on" in reply.lower() or "true" in reply.lower()


# --- Reply-text differentiation -------------------------------------------


@pytest.mark.asyncio
async def test_calibration_ok_disable_reply_text(
    raw_config_with_state_path: dict,
    state_path: str,
) -> None:
    """Enable reply mentions ``enabled``; disable reply mentions
    ``disabled``. Operator UX: the success line must differ so a
    glance at the Telegram chat history shows the direction of each
    flip without reading the per-tier flag table."""
    # Enable first.
    enable_update = _make_update_mock(text="/calibration_ok spam on")
    enable_ctx = _make_ctx_mock(raw_config=raw_config_with_state_path)
    await bot.on_calibration_ok(enable_update, enable_ctx)
    enable_reply = enable_update.message.reply_text.await_args.args[0]

    # Disable.
    disable_update = _make_update_mock(text="/calibration_ok spam off")
    disable_ctx = _make_ctx_mock(raw_config=raw_config_with_state_path)
    await bot.on_calibration_ok(disable_update, disable_ctx)
    disable_reply = disable_update.message.reply_text.await_args.args[0]

    assert "enabled" in enable_reply.lower()
    assert "disabled" in disable_reply.lower()
    # Negative cross-check: the enable reply must NOT contain the
    # disabled verb (catches a regex-style template that always
    # interpolates both words).
    assert "disabled" not in enable_reply.lower()


# --- List path (preserved behaviour) --------------------------------------


@pytest.mark.asyncio
async def test_calibration_ok_no_tier_lists_flags(
    raw_config_with_state_path: dict,
    state_path: str,
) -> None:
    """``/calibration_ok`` (no tier) lists current flags — preserved.
    A regression on the list-path would surface here even though
    this test isn't strictly about the Task #57 widening."""
    update = _make_update_mock(text="/calibration_ok")
    ctx = _make_ctx_mock(raw_config=raw_config_with_state_path)

    await bot.on_calibration_ok(update, ctx)

    update.message.reply_text.assert_awaited_once()
    reply = update.message.reply_text.await_args.args[0]
    # The report mentions every tier from _VALID_TIERS.
    for tier in ("high", "medium", "low", "spam"):
        assert tier in reply.lower()


# --- Bad-tier path (preserved behaviour) ----------------------------------


@pytest.mark.asyncio
async def test_calibration_ok_unknown_tier_rejected(
    raw_config_with_state_path: dict,
    state_path: str,
) -> None:
    """``/calibration_ok xyz`` (unknown tier) → friendly error from
    ``set_confidence``. Bad-tier rejection is set_confidence's
    contract, not the handler's; this test pins that the handler
    forwards the ValueError as a Telegram reply rather than letting
    it propagate."""
    update = _make_update_mock(text="/calibration_ok xyz")
    ctx = _make_ctx_mock(raw_config=raw_config_with_state_path)

    await bot.on_calibration_ok(update, ctx)

    update.message.reply_text.assert_awaited_once()
    reply = update.message.reply_text.await_args.args[0]
    assert "xyz" in reply.lower()


# --- Bad-tier + bad-value combo -------------------------------------------


@pytest.mark.asyncio
async def test_calibration_ok_unknown_value_takes_priority_over_unknown_tier(
    raw_config_with_state_path: dict,
    state_path: str,
) -> None:
    """``/calibration_ok xyz frobnicate`` — both args invalid. The
    value gate runs BEFORE set_confidence, so the operator sees the
    value-format error first. This pin documents the order so a
    future refactor that swaps the gates surfaces here.
    """
    update = _make_update_mock(text="/calibration_ok xyz frobnicate")
    ctx = _make_ctx_mock(raw_config=raw_config_with_state_path)

    await bot.on_calibration_ok(update, ctx)

    update.message.reply_text.assert_awaited_once()
    reply = update.message.reply_text.await_args.args[0]
    # The reply should be the value-format complaint, NOT the
    # tier-unknown complaint.
    assert "frobnicate" in reply.lower()


# --- Token-vocab regression pin -------------------------------------------


def test_calibration_ok_token_vocabs_disjoint() -> None:
    """The enable and disable token sets must not overlap. Same lower-
    case literal in both would create a parse ambiguity (the enable
    arm runs after the disable arm; whichever comes first wins,
    silently).
    """
    overlap = bot._CALIBRATION_OK_ENABLE_TOKENS & bot._CALIBRATION_OK_DISABLE_TOKENS
    assert overlap == set(), f"overlap: {overlap}"


def test_calibration_ok_disable_tokens_cover_spec() -> None:
    """The disable vocab includes every token the dispatch spec named.
    Adding a token is fine (expands operator UX); removing one is a
    breaking change that should surface here.
    """
    spec_tokens = {"false", "off", "0", "no", "disable", "disabled"}
    assert spec_tokens.issubset(bot._CALIBRATION_OK_DISABLE_TOKENS)


def test_calibration_ok_enable_tokens_cover_spec() -> None:
    """Mirror of the disable-vocab pin for the enable side."""
    spec_tokens = {"true", "on", "1", "yes", "enable", "enabled"}
    assert spec_tokens.issubset(bot._CALIBRATION_OK_ENABLE_TOKENS)
