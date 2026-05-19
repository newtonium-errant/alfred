"""Phase 5 Sub-arc D2 — slash command registration gate (2026-05-19).

Pins the Hypatia-only registration gate for ``/moc-suggestions`` +
``/accept-moc`` + ``/reject-moc``. The three commands are registered
ONLY when ``telegram.moc_suggestions.command_enabled: true`` is set in
the instance config. Salem + KAL-LE leave the block absent so the
commands aren't registered.

Mirror of ``test_inventory_views.py``'s command-registration testing
pattern (Phase 4 Sub-arc C). The pattern protects against silent
cross-instance leakage — e.g., Salem accidentally exposing
``/accept-moc`` and writing to its operational vault when no
MOC-suggestion queue exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.telegram.config import (
    AnthropicConfig,
    InstanceConfig,
    LoggingConfig,
    MocSuggestionsConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
)


def _make_config(
    *,
    tmp_path: Path,
    moc_suggestions: MocSuggestionsConfig | None,
    instance_name: str = "Salem",
) -> TalkerConfig:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    return TalkerConfig(
        bot_token="test-token",
        allowed_users=[1],
        anthropic=AnthropicConfig(api_key="test-key", model="claude-sonnet-4-6"),
        stt=STTConfig(api_key="test-stt", model="whisper-large-v3"),
        session=SessionConfig(state_path=str(tmp_path / "talker_state.json")),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name=instance_name, canonical=instance_name.upper()),
        moc_suggestions=moc_suggestions,
    )


def _registered_command_names(app) -> set[str]:
    """Extract registered command names from a PTB Application."""
    out: set[str] = set()
    for handlers in app.handlers.values():
        for handler in handlers:
            cmds = getattr(handler, "commands", None)
            if cmds:
                out.update(cmds)
    return out


@pytest.fixture
def fake_anthropic_client():
    """Build_application requires an anthropic client; this is a stand-in."""
    from unittest.mock import MagicMock
    return MagicMock()


# ---------------------------------------------------------------------------
# Registration gate — defaults
# ---------------------------------------------------------------------------


def test_moc_suggestions_block_absent_no_commands_registered(
    tmp_path: Path, fake_anthropic_client,
) -> None:
    """Block absent (None sentinel) → no MOC commands registered. Salem's
    default state."""
    config = _make_config(tmp_path=tmp_path, moc_suggestions=None)
    from alfred.telegram import bot
    app = bot.build_app(
        config=config,
        state_mgr=None,  # not exercised in registration path
        anthropic_client=fake_anthropic_client,
        system_prompt="",
        vault_context_str="",
    )
    commands = _registered_command_names(app)
    assert "moc_suggestions" not in commands
    assert "accept_moc" not in commands
    assert "reject_moc" not in commands


def test_moc_suggestions_command_enabled_false_no_commands_registered(
    tmp_path: Path, fake_anthropic_client,
) -> None:
    """Block present but ``command_enabled=False`` → still no registration.
    Same gate semantics as inventory_views (Phase 4 Sub-arc C)."""
    config = _make_config(
        tmp_path=tmp_path,
        moc_suggestions=MocSuggestionsConfig(command_enabled=False),
    )
    from alfred.telegram import bot
    app = bot.build_app(
        config=config,
        state_mgr=None,
        anthropic_client=fake_anthropic_client,
        system_prompt="",
        vault_context_str="",
    )
    commands = _registered_command_names(app)
    assert "moc_suggestions" not in commands
    assert "accept_moc" not in commands
    assert "reject_moc" not in commands


def test_moc_suggestions_command_enabled_true_registers_all_three(
    tmp_path: Path, fake_anthropic_client,
) -> None:
    """Hypatia config shape — block present with ``command_enabled=true``
    registers all three handlers."""
    config = _make_config(
        tmp_path=tmp_path,
        moc_suggestions=MocSuggestionsConfig(
            command_enabled=True,
            queue_path=str(tmp_path / "moc_suggestions.jsonl"),
        ),
        instance_name="Hypatia",
    )
    from alfred.telegram import bot
    app = bot.build_app(
        config=config,
        state_mgr=None,
        anthropic_client=fake_anthropic_client,
        system_prompt="",
        vault_context_str="",
    )
    commands = _registered_command_names(app)
    assert "moc_suggestions" in commands, (
        "moc_suggestions command must register when command_enabled=true"
    )
    assert "accept_moc" in commands
    assert "reject_moc" in commands


# ---------------------------------------------------------------------------
# Config loading — load_from_unified plumbing
# ---------------------------------------------------------------------------


def test_load_from_unified_picks_up_moc_suggestions_block() -> None:
    """``telegram.moc_suggestions:`` block in YAML lands on
    ``TalkerConfig.moc_suggestions`` after load_from_unified."""
    from alfred.telegram.config import load_from_unified
    raw = {
        "telegram": {
            "instance": {"name": "Hypatia"},
            "bot_token": "x",
            "anthropic": {"api_key": "x"},
            "stt": {"api_key": "x"},
            "moc_suggestions": {
                "command_enabled": True,
                "queue_path": "/home/andrew/.alfred/hypatia/data/moc_suggestions.jsonl",
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.moc_suggestions is not None
    assert cfg.moc_suggestions.command_enabled is True
    assert cfg.moc_suggestions.queue_path == "/home/andrew/.alfred/hypatia/data/moc_suggestions.jsonl"


def test_load_from_unified_block_absent_leaves_none() -> None:
    """Block absent → ``moc_suggestions=None`` (the default sentinel)."""
    from alfred.telegram.config import load_from_unified
    raw = {
        "telegram": {
            "instance": {"name": "Salem"},
            "bot_token": "x",
            "anthropic": {"api_key": "x"},
            "stt": {"api_key": "x"},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.moc_suggestions is None


def test_load_from_unified_block_empty_dict_leaves_none() -> None:
    """Block present but empty dict → still None (matches inventory_views shape)."""
    from alfred.telegram.config import load_from_unified
    raw = {
        "telegram": {
            "instance": {"name": "Salem"},
            "bot_token": "x",
            "anthropic": {"api_key": "x"},
            "stt": {"api_key": "x"},
            "moc_suggestions": {},
        },
    }
    cfg = load_from_unified(raw)
    # Same pattern as inventory_views: empty dict treated as "not opt-in".
    # If you want it on, set command_enabled explicitly.
    assert cfg.moc_suggestions is None or cfg.moc_suggestions.command_enabled is False


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_moc_suggestions_config_defaults() -> None:
    """Default state matches the disabled-by-default convention used by
    inventory_views / fiction / voice_train."""
    cfg = MocSuggestionsConfig()
    assert cfg.command_enabled is False
    assert cfg.queue_path is None
