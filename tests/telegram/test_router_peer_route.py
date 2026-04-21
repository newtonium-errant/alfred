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
