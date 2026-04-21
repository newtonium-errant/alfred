"""Tests for per-instance persona templating in the talker.

The talker's SKILL.md contains two placeholders — ``{{instance_name}}``
and ``{{instance_canonical}}`` — which the daemon substitutes at load
time using plain :func:`str.replace`. Only persona references are
templated; product/codebase mentions of ``Alfred`` (wikilinks, the
code framework name, other-instance names like "Knowledge Alfred")
stay literal.

See:
    - ``memory/project_multi_instance_design.md`` for naming rationale
      (S.A.L.E.M., STAY-C, KAL-LE).
    - ``vault/session/Talker instance name templating (Salem) 2026-04-19.md``
      for the bundled-commit context.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.telegram import bot, daemon
from alfred.telegram.config import (
    AnthropicConfig,
    InstanceConfig,
    LoggingConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
    load_from_unified,
)


# --- Helpers ---------------------------------------------------------------


def _skill_path() -> Path:
    """Return the packaged vault-talker SKILL.md path.

    We read the real SKILL directly so the tests guard against a future
    prompt-tuner edit that accidentally drops the placeholder tokens
    (e.g., swapping ``{{instance_name}}`` for a literal "Alfred" again).
    """
    return (
        Path(__file__).resolve().parents[2]
        / "src"
        / "alfred"
        / "_bundled"
        / "skills"
        / "vault-talker"
        / "SKILL.md"
    )


def _load_templated(config: TalkerConfig) -> str:
    """Mirror the daemon's load path end-to-end (read + substitute)."""
    raw = _skill_path().read_text(encoding="utf-8")
    return daemon._apply_instance_templating(raw, config)


# --- Default-config (Alfred) ----------------------------------------------


def test_default_config_is_alfred(talker_config: TalkerConfig) -> None:
    """Without an override, the SKILL loads as ``Alfred`` everywhere.

    ``talker_config`` from conftest uses the :class:`InstanceConfig`
    defaults (name="Alfred", canonical="Alfred"), so both placeholders
    collapse to "Alfred" and every persona reference reads naturally.
    """
    prompt = _load_templated(talker_config)

    # Placeholders are fully substituted.
    assert "{{instance_name}}" not in prompt, (
        "instance_name placeholder still present after substitution"
    )
    assert "{{instance_canonical}}" not in prompt, (
        "instance_canonical placeholder still present after substitution"
    )

    # Persona references resolve to "Alfred".
    assert "You are **Alfred**" in prompt, (
        "identity paragraph should render the canonical form"
    )
    assert "# Alfred — Talker" in prompt, (
        "section header should render the casual name"
    )

    # Product references remain literal.
    assert "[[project/Alfred]]" in prompt
    assert "Knowledge Alfred" in prompt


# --- Salem override --------------------------------------------------------


def test_salem_instance_substitution(talker_config: TalkerConfig) -> None:
    """With Salem config, persona lines use Salem/S.A.L.E.M.; product stays Alfred.

    This is the core multi-instance contract: flipping
    ``telegram.instance`` in config.yaml swaps the talker's self-identity
    without touching any wikilink, framework name, or other-instance
    reference.
    """
    talker_config.instance = InstanceConfig(
        name="Salem",
        canonical="S.A.L.E.M.",
        aliases=["Salem"],
    )
    prompt = _load_templated(talker_config)

    # Persona substitutions landed.
    assert "You are **S.A.L.E.M.**" in prompt, (
        "canonical should appear in the identity paragraph"
    )
    assert "# Salem — Talker" in prompt, (
        "casual name should appear in the section header"
    )
    # Neither placeholder token should survive.
    assert "{{instance_name}}" not in prompt
    assert "{{instance_canonical}}" not in prompt

    # Critical: product references stay literal "Alfred" — wikilinks,
    # codebase references, and other-instance names are all unchanged.
    assert "[[project/Alfred]]" in prompt, (
        "project/Alfred wikilink must not be rewritten — it's a vault path"
    )
    assert "project/Alfred" in prompt  # appears in the privacy section example
    assert "Knowledge Alfred" in prompt, (
        "Knowledge Alfred names another instance; don't rewrite as Knowledge Salem"
    )
    assert "work on Alfred itself" in prompt, (
        "'Alfred itself' refers to the codebase — leave literal"
    )

    # Belt + braces: the talker should NOT be introduced as "**Alfred**"
    # anywhere in the Salem-configured prompt. We allow literal "Alfred"
    # elsewhere, but the bolded-identity form is persona-only.
    assert "You are **Alfred**" not in prompt


# --- Aliases roundtrip -----------------------------------------------------


def test_aliases_config_roundtrip(tmp_path: Path) -> None:
    """``aliases`` list round-trips through ``load_from_unified`` intact.

    The router will read ``TalkerConfig.instance.aliases`` to normalise
    phone-autocorrected / voice-transcribed instance names back to the
    canonical form. Config loading must preserve the list as-is.
    """
    raw = {
        "vault": {"path": str(tmp_path / "vault")},
        "telegram": {
            "bot_token": "t",
            "allowed_users": [1],
            "primary_users": ["person/Andrew Newton"],
            "anthropic": {"api_key": "k", "model": "claude-sonnet-4-6"},
            "stt": {"api_key": "s"},
            "session": {"state_path": str(tmp_path / "state.json")},
            "instance": {
                "name": "Salem",
                "canonical": "S.A.L.E.M.",
                "aliases": ["Salem", "salem"],
            },
        },
    }

    config = load_from_unified(raw)

    assert config.instance.name == "Salem"
    assert config.instance.canonical == "S.A.L.E.M."
    assert config.instance.aliases == ["Salem", "salem"]

    # A config without any instance block falls back to the Alfred
    # defaults — fresh installs keep working unchanged.
    raw_no_instance = dict(raw)
    raw_no_instance["telegram"] = {
        k: v for k, v in raw["telegram"].items() if k != "instance"
    }
    default_config = load_from_unified(raw_no_instance)
    assert default_config.instance.name == "Alfred"
    assert default_config.instance.canonical == "Alfred"
    assert default_config.instance.aliases == []


# --- Bot greeting ----------------------------------------------------------


# --- Peer-routing addendum (Stage 3.5) ------------------------------------


def test_skill_has_peer_routing_section(talker_config: TalkerConfig) -> None:
    """Salem's SKILL must describe peer routing (KAL-LE handoff pattern).

    The addendum lands between "Making records" and "Altering records" and
    introduces KAL-LE by name. Without this section, Salem has no awareness
    that other instances exist — so when the router classifies ``note`` on
    an ambiguous coding cue, Salem answers without mentioning routing was
    an option. Regression risk is high because prompt-tuner edits are
    free-form markdown.
    """
    prompt = _load_templated(talker_config)

    # Section header and the peer name both land verbatim.
    assert "## Peer routing" in prompt, (
        "peer-routing section header missing from Salem SKILL"
    )
    assert "KAL-LE" in prompt, (
        "KAL-LE peer must be named in the routing section"
    )
    assert "K.A.L.L.E." in prompt, (
        "canonical form should appear at least once for user recognition"
    )
    # The "router decides before your turn" line is the load-bearing contract.
    assert "routing is decided" in prompt.lower() or "above your turn" in prompt, (
        "SKILL must state Salem can't route manually"
    )


def test_skill_peer_routing_survives_salem_templating(
    talker_config: TalkerConfig,
) -> None:
    """The peer-routing section templates correctly on Salem config.

    The section references ``KAL-LE`` as a literal string (product name,
    not persona). The {{instance_name}} / {{instance_canonical}}
    substitution must not accidentally rewrite KAL-LE on either instance.
    """
    talker_config.instance = InstanceConfig(
        name="Salem",
        canonical="S.A.L.E.M.",
        aliases=["Salem"],
    )
    prompt = _load_templated(talker_config)

    assert "KAL-LE" in prompt, "KAL-LE must stay literal on Salem config"
    assert "K.A.L.L.E." in prompt, "KAL-LE canonical must stay literal"
    # Persona swap still lands.
    assert "You are **S.A.L.E.M.**" in prompt


@pytest.mark.asyncio
async def test_bot_start_greeting_uses_instance_name(
    talker_config: TalkerConfig,
) -> None:
    """``/start`` greeting reads from ``config.instance.name``.

    The greeting is the only user-facing string outside the SKILL where
    the persona name appears. A literal "this is Alfred." hard-coded in
    bot.py would contradict the SKILL-level identity on every other
    instance — catch the regression here.
    """
    talker_config.instance = InstanceConfig(
        name="Salem",
        canonical="S.A.L.E.M.",
        aliases=["Salem"],
    )

    update = MagicMock()
    update.effective_user.id = 1
    update.message.reply_text = AsyncMock()

    ctx = MagicMock()
    ctx.application.bot_data = {
        "config": talker_config,
        "state_mgr": MagicMock(),
        "anthropic_client": MagicMock(),
        "system_prompt": "sys",
        "vault_context_str": "",
        "chat_locks": {},
    }

    await bot.on_start(update, ctx)

    update.message.reply_text.assert_called_once()
    reply = update.message.reply_text.call_args.args[0]
    assert "Salem" in reply, f"greeting should mention the instance name: {reply!r}"
    assert "Alfred" not in reply, (
        "Salem greeting should not also include the default Alfred name: "
        f"{reply!r}"
    )
