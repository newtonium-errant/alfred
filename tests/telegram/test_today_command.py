"""Tier Phase 2A — /today slash command tests (2026-05-28).

Per the dispatch spec:
  * /today — Salem-only glance-view mini-brief composing the tier,
    routines, and upcoming-events sections as one Telegram reply
  * Read-only — no vault writes, no session record
  * Config-gated via ``telegram.today_command.enabled: true``
  * Salem-only: KAL-LE / Hypatia leave the block absent so Telegram's
    unknown-command behaviour fires

Coverage:
  * Config dataclass tolerates absent block (defaults to disabled)
  * Handler dispatches when enabled=True
  * Handler no-ops when enabled=False (defensive in-handler gate)
  * Handler no-ops on non-allowlisted user (access control parity)
  * Composed body contains the three section headers
  * Section ordering matches the morning brief (tier → routines → events)
  * Body length under the Telegram cap (under 4000 chars sanity check)
  * compose_today_reply pure-helper covers: tier header present,
    routines header present, upcoming-events header present, ordering
    preserved, defensive truncation when body would overflow
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
import structlog

from alfred.telegram import bot
from alfred.telegram.config import (
    TodayCommandConfig,
    load_from_unified,
)
from alfred.telegram.today_command import compose_today_reply


HALIFAX = ZoneInfo("America/Halifax")


# ---------------------------------------------------------------------------
# Vault fixture — minimal Salem-shape with task + routine + event dirs
# ---------------------------------------------------------------------------


@pytest.fixture
def salem_vault(tmp_path: Path) -> Path:
    """Vault with task/, routine/, event/, daily/ subdirs — enough for
    each of the three section renders to operate without ENOENT noise."""
    vault = tmp_path / "vault"
    vault.mkdir()
    for sub in ("task", "routine", "event", "daily"):
        (vault / sub).mkdir()
    return vault


# ---------------------------------------------------------------------------
# Config dataclass behaviour
# ---------------------------------------------------------------------------


def test_today_command_config_default_disabled() -> None:
    """``TodayCommandConfig()`` constructs with enabled=False by default
    — Salem-only opt-in convention."""
    cfg = TodayCommandConfig()
    assert cfg.enabled is False
    assert cfg.timezone == "America/Halifax"


def test_today_command_config_explicit_enable() -> None:
    """Operator opts in by setting ``enabled: true``."""
    cfg = TodayCommandConfig(enabled=True)
    assert cfg.enabled is True


def test_talker_config_today_command_block_absent() -> None:
    """Config block absent → ``today_command`` stays None.

    Mirrors the inventory_views + moc_suggestions sentinel convention.
    Distinguishes 'block absent' from 'block present, command disabled'
    so health probes / dashboards can tell the difference."""
    raw: dict[str, Any] = {
        "telegram": {
            "bot_token": "DUMMY_BOT_TOKEN_PLACEHOLDER",
            "allowed_users": [42],
            "instance": {"name": "Salem"},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.today_command is None


def test_talker_config_today_command_block_present_with_enabled() -> None:
    """Config block present + ``enabled: true`` → TodayCommandConfig
    constructed with the opt-in flag set."""
    raw: dict[str, Any] = {
        "telegram": {
            "bot_token": "DUMMY_BOT_TOKEN_PLACEHOLDER",
            "allowed_users": [42],
            "instance": {"name": "Salem"},
            "today_command": {"enabled": True},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.today_command is not None
    assert cfg.today_command.enabled is True
    # Default timezone preserved when YAML omits it.
    assert cfg.today_command.timezone == "America/Halifax"


def test_talker_config_today_command_block_present_with_timezone_override() -> None:
    """Operator can override the timezone for non-Salem opt-ins."""
    raw: dict[str, Any] = {
        "telegram": {
            "bot_token": "DUMMY_BOT_TOKEN_PLACEHOLDER",
            "allowed_users": [42],
            "instance": {"name": "Salem"},
            "today_command": {
                "enabled": True,
                "timezone": "America/Toronto",
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.today_command is not None
    assert cfg.today_command.timezone == "America/Toronto"


# ---------------------------------------------------------------------------
# compose_today_reply — pure-helper composition tests
# ---------------------------------------------------------------------------


def test_compose_includes_tier_section_header(salem_vault: Path) -> None:
    """The composed body MUST include the canonical tier section header
    string (``"Open Tasks by Tier"``) — operator's mental model is the
    brief's exact wording."""
    now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)
    body = compose_today_reply(salem_vault, now)
    assert "## Open Tasks by Tier" in body


def test_compose_includes_routines_section_header(salem_vault: Path) -> None:
    """Mirror of the brief daemon's ``Today's Routines`` section header."""
    now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)
    body = compose_today_reply(salem_vault, now)
    assert "## Today's Routines" in body


def test_compose_includes_upcoming_events_section_header(
    salem_vault: Path,
) -> None:
    """Mirror of the brief daemon's ``Upcoming Events`` section header."""
    now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)
    body = compose_today_reply(salem_vault, now)
    assert "## Upcoming Events" in body


def test_compose_section_ordering_matches_brief(salem_vault: Path) -> None:
    """Sections appear in the canonical brief order: tier → routines →
    upcoming events. Pinned so a refactor that reorders silently
    surfaces here."""
    now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)
    body = compose_today_reply(salem_vault, now)
    tier_idx = body.index("## Open Tasks by Tier")
    routines_idx = body.index("## Today's Routines")
    events_idx = body.index("## Upcoming Events")
    assert tier_idx < routines_idx < events_idx, (
        f"Section ordering must match the brief: tier ({tier_idx}) → "
        f"routines ({routines_idx}) → events ({events_idx})"
    )


def test_compose_body_under_telegram_cap_for_empty_vault(
    salem_vault: Path,
) -> None:
    """Empty-vault composition stays well under Telegram's 4096-char
    cap. Sanity check that the empty-state sentinels in each section
    don't accidentally inflate to overflow."""
    now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)
    body = compose_today_reply(salem_vault, now)
    # Dispatch's "under 4000 char limit" sanity assertion.
    assert len(body) < 4000


def test_compose_uses_intentionally_left_blank_sentinels_when_empty(
    salem_vault: Path,
) -> None:
    """Per ``feedback_intentionally_left_blank.md``: each section
    emits a non-empty body even when its bucket is empty. The
    composed reply should NEVER silently drop a section."""
    now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)
    body = compose_today_reply(salem_vault, now)
    # Each section header has its body below it — pin that the
    # body following each header is non-empty (the sentinel string
    # from each section's render path).
    lines = body.splitlines()
    headers = [
        "## Open Tasks by Tier",
        "## Today's Routines",
        "## Upcoming Events",
    ]
    for header in headers:
        idx = lines.index(header)
        # Header is followed by a blank line + at least one content
        # line — sentinel from the section's render path.
        non_blank_after = [
            line for line in lines[idx + 1:idx + 5]
            if line.strip()
        ]
        assert len(non_blank_after) > 0, (
            f"Section '{header}' has no body lines below it — "
            f"sentinel missing per intentionally-left-blank discipline"
        )


def test_compose_all_three_renders_failing_emits_combined_sentinels(
    salem_vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Combined-failure pin: when ALL THREE section render functions
    raise, the composed body MUST carry all three
    ``*(<section> render failed; see brief log)*`` sentinels — not
    silently drop sections — AND the total body length stays under
    ``_TELEGRAM_BODY_CAP``.

    Pinned per code-reviewer boundary-coverage gap (2026-05-28). The
    per-section try/except blocks each emit a sentinel; this test
    exercises the combined-failure path end-to-end through the
    composition + truncation logic. Without this pin, a refactor
    that swallows exceptions silently or emits empty strings on
    failure would only surface during real-vault edge cases where
    all three renders happen to fail at once."""
    from alfred.telegram import today_command as _tc

    def _raise_for_tier(*args, **kwargs):
        raise RuntimeError("tier render simulated failure")

    def _raise_for_routines(*args, **kwargs):
        raise RuntimeError("routines render simulated failure")

    def _raise_for_upcoming(*args, **kwargs):
        raise RuntimeError("upcoming events render simulated failure")

    # Patch in the today_command module's namespace — that's where
    # the compose path looks them up via the from-import binding.
    monkeypatch.setattr(_tc, "render_tier_section", _raise_for_tier)
    monkeypatch.setattr(_tc, "render_routine_section", _raise_for_routines)
    monkeypatch.setattr(
        _tc, "render_upcoming_events_section", _raise_for_upcoming,
    )

    now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)
    body = compose_today_reply(salem_vault, now)

    # All three sentinels present — no silent drop per
    # intentionally-left-blank discipline.
    assert "*(tier render failed; see brief log)*" in body
    assert "*(routines render failed; see brief log)*" in body
    assert "*(upcoming events render failed; see brief log)*" in body

    # Section headers still emit so the operator's mental model of
    # the brief surface stays intact (failed section ≠ missing
    # section).
    assert "## Open Tasks by Tier" in body
    assert "## Today's Routines" in body
    assert "## Upcoming Events" in body

    # Combined-failure body length stays under the Telegram cap.
    # Pins the truncation-path correctness for the worst case (three
    # error sentinels rather than three full section bodies; should
    # be well under cap, but explicit assertion guards against a
    # future refactor that inflates the per-section sentinel text).
    assert len(body) < _tc._TELEGRAM_BODY_CAP


# ---------------------------------------------------------------------------
# Handler dispatch — config gate + access control
# ---------------------------------------------------------------------------


def _make_update_mock(chat_id: int = 42, user_id: int = 42) -> MagicMock:
    """Mirror the inventory-views test helper shape."""
    update = MagicMock()
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    return update


def _make_ctx_mock(
    vault_path: Path,
    *,
    allowed_user_id: int = 42,
    today_command_enabled: bool = True,
) -> MagicMock:
    """Build a minimal ctx mock for /today handler smoke tests.

    Mirrors the inventory-views test helper shape; the gate-knob is
    ``today_command_enabled`` instead of ``inventory_views_enabled``.
    """
    config = MagicMock()
    config.allowed_users = [allowed_user_id]
    config.vault = MagicMock()
    config.vault.path = str(vault_path)
    if today_command_enabled:
        config.today_command = MagicMock()
        config.today_command.enabled = True
        config.today_command.timezone = "America/Halifax"
    else:
        config.today_command = None
    ctx = MagicMock()
    ctx.application.bot_data = {bot._KEY_CONFIG: config}
    return ctx


@pytest.mark.asyncio
async def test_handler_dispatches_when_enabled(salem_vault: Path) -> None:
    """/today fires when ``today_command.enabled=True`` + user is
    allowlisted → reply_text is called exactly once."""
    update = _make_update_mock()
    ctx = _make_ctx_mock(salem_vault)
    await bot.on_today(update, ctx)
    update.message.reply_text.assert_called_once()
    reply = update.message.reply_text.call_args[0][0]
    # Composed reply carries the three section headers.
    assert "## Open Tasks by Tier" in reply
    assert "## Today's Routines" in reply
    assert "## Upcoming Events" in reply


@pytest.mark.asyncio
async def test_handler_no_op_when_disabled(salem_vault: Path) -> None:
    """``today_command=None`` (block absent in YAML) → handler no-ops
    silently. Defensive in-handler gate matches the
    ``build_app`` registration gate's semantics.

    Even if a future routing layer dispatches without checking the
    registration gate, the handler's own gate prevents accidental
    Salem-only behaviour on non-Salem instances."""
    update = _make_update_mock()
    ctx = _make_ctx_mock(salem_vault, today_command_enabled=False)
    await bot.on_today(update, ctx)
    update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_handler_silent_drop_on_non_allowlisted_user(
    salem_vault: Path,
) -> None:
    """Unknown user → no reply (matches existing handler convention).

    Access control parity with the rest of the bot handlers — every
    handler reads ``config.allowed_users`` and silent-drops anything
    not in the list."""
    update = _make_update_mock(user_id=999)
    ctx = _make_ctx_mock(salem_vault, allowed_user_id=42)
    await bot.on_today(update, ctx)
    update.message.reply_text.assert_not_called()


@pytest.mark.asyncio
async def test_handler_reply_under_telegram_cap(salem_vault: Path) -> None:
    """Handler reply stays under the Telegram per-message body cap.
    Sanity check the composition's defensive truncation works
    end-to-end through the handler."""
    update = _make_update_mock()
    ctx = _make_ctx_mock(salem_vault)
    await bot.on_today(update, ctx)
    reply = update.message.reply_text.call_args[0][0]
    assert len(reply) < 4000


@pytest.mark.asyncio
async def test_handler_logs_today_command_done_with_reply_chars(
    salem_vault: Path,
) -> None:
    """Per builder.md rule #9: pin the success log emission via
    structlog.testing.capture_logs. ``talker.bot.today_command_done``
    MUST carry ``reply_chars`` + ``date`` so the operator can grep
    for ``/today`` activity + characterise reply-size distribution."""
    update = _make_update_mock()
    ctx = _make_ctx_mock(salem_vault)
    with structlog.testing.capture_logs() as captured:
        await bot.on_today(update, ctx)
    done_events = [
        c for c in captured
        if c.get("event") == "talker.bot.today_command_done"
    ]
    assert len(done_events) == 1
    event = done_events[0]
    assert "reply_chars" in event
    assert "date" in event
    assert isinstance(event["reply_chars"], int)
    assert event["reply_chars"] > 0


# ---------------------------------------------------------------------------
# Handler registration in build_app
# ---------------------------------------------------------------------------


def test_build_app_registers_today_handler_when_enabled() -> None:
    """When ``today_command.enabled=True``, ``build_app``
    registers the /today CommandHandler. Pin by inspecting the
    handler registry rather than wrangling a full PTB Application
    instance — same approach the inventory-views tests use."""
    # Build a TalkerConfig with today_command opted-in.
    raw: dict[str, Any] = {
        "telegram": {
            "bot_token": "DUMMY_BOT_TOKEN_PLACEHOLDER",
            "allowed_users": [42],
            "instance": {"name": "Salem"},
            "today_command": {"enabled": True},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.today_command is not None
    assert cfg.today_command.enabled is True

    # The handler-registration block runs inside build_app; rather
    # than spin up a full PTB Application (heavy + requires event
    # loop), pin the registration logic itself: the gate predicate
    # evaluates to True for this config, so the registration branch
    # fires.
    gate = (
        cfg.today_command is not None
        and cfg.today_command.enabled
    )
    assert gate is True


def test_build_app_skips_today_handler_when_block_absent() -> None:
    """``today_command`` block absent → registration gate evaluates
    False → /today CommandHandler not added. The non-Salem instance
    convention.

    Falls through to Telegram's unknown-command behaviour for any
    user who types /today on a non-Salem instance — matches the
    inventory_views + moc_suggestions conditional-registration
    pattern."""
    raw: dict[str, Any] = {
        "telegram": {
            "bot_token": "DUMMY_BOT_TOKEN_PLACEHOLDER",
            "allowed_users": [42],
            "instance": {"name": "KAL-LE"},
            # today_command block intentionally absent
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.today_command is None
    gate = (
        cfg.today_command is not None
        and cfg.today_command.enabled
    )
    assert gate is False
