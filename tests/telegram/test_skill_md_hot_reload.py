"""Tests for SKILL.md per-conversation hot-reload (P1 from QA 2026-05-04).

Closes the operational gap behind the "same-cycle SKILL ship"
convention: SKILL.md edits must take effect on the next conversation
start, not require a daemon restart. Hypatia's 22:27 UTC conversation
on 2026-05-04 ran with the OLD SKILL after the new SKILL committed
but before restart — defeating the same-cycle ship discipline.

Coverage:
  * ``_load_system_prompt`` reads fresh from disk on each call
    (regression guard against accidental caching)
  * ``build_system_prompt_provider`` returns a callable that
    re-reads SKILL.md on every invocation; mid-stream SKILL edits
    are visible in subsequent calls
  * Provider applies templating fresh per call (so the same per-
    instance substitutions happen)
  * Missing SKILL.md degrades to empty string (preserves existing
    behaviour); operator gets a structured warning
  * Per-load debug log fires with file path + char count so
    operators can confirm the reload IS happening
  * ``bot.build_app`` accepts EITHER a callable or a static string
    (legacy compat); the string case is wrapped in a constant-
    returning lambda so the message handler's call shape stays
    uniform
  * Bot's message handler invokes the provider per turn (verified
    via the bot_data shape — direct dispatcher invocation requires
    full Telegram update plumbing out of scope here)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import structlog


def _write_skill(skills_dir: Path, bundle: str, content: str) -> Path:
    """Seed a SKILL.md under ``skills_dir/<bundle>/SKILL.md``."""
    bundle_dir = skills_dir / bundle
    bundle_dir.mkdir(parents=True, exist_ok=True)
    skill_path = bundle_dir / "SKILL.md"
    skill_path.write_text(content, encoding="utf-8")
    return skill_path


def _build_min_config(skill_bundle: str = "vault-talker"):
    """Minimal TalkerConfig for templating tests. Bypasses the full
    config-load path so the test stays focused on SKILL hot-reload."""
    from alfred.telegram.config import (
        AnthropicConfig, InstanceConfig, LoggingConfig,
        SessionConfig, STTConfig, TalkerConfig, VaultConfig,
    )
    return TalkerConfig(
        bot_token="test-token",
        allowed_users=[1],
        primary_users=[],
        anthropic=AnthropicConfig(
            api_key="test-key",
            model="claude-sonnet-4-6",
            max_tokens=1024,
            temperature=1.0,
        ),
        stt=STTConfig(api_key="test-stt", model="whisper-large-v3"),
        session=SessionConfig(
            gap_timeout_seconds=1800,
            state_path="/tmp/state.json",
        ),
        vault=VaultConfig(path="/tmp/vault"),
        logging=LoggingConfig(file="/tmp/talker.log"),
        instance=InstanceConfig(
            name="Hypatia", canonical="Hypatia",
            skill_bundle=skill_bundle,
        ),
    )


# ---------------------------------------------------------------------------
# _load_system_prompt — fresh-read contract
# ---------------------------------------------------------------------------


class TestLoadSystemPromptFreshRead:
    def test_reads_skill_md_from_disk(self, tmp_path: Path):
        from alfred.telegram.daemon import _load_system_prompt
        _write_skill(tmp_path, "vault-talker", "Initial SKILL content.")
        result = _load_system_prompt(tmp_path, skill_bundle="vault-talker")
        assert result == "Initial SKILL content."

    def test_each_call_re_reads_disk(self, tmp_path: Path):
        """The function MUST NOT cache. Modify SKILL.md between two
        calls; the second call must see the new content."""
        from alfred.telegram.daemon import _load_system_prompt
        skill_path = _write_skill(tmp_path, "vault-talker", "First.")
        first = _load_system_prompt(tmp_path, skill_bundle="vault-talker")
        skill_path.write_text("Second.", encoding="utf-8")
        second = _load_system_prompt(tmp_path, skill_bundle="vault-talker")
        assert first == "First."
        assert second == "Second."

    def test_missing_skill_returns_empty_string(self, tmp_path: Path):
        """Preserve the legacy behaviour — missing SKILL.md degrades
        to empty string with a structured warning, not a raise."""
        from alfred.telegram.daemon import _load_system_prompt
        with structlog.testing.capture_logs() as captured:
            result = _load_system_prompt(
                tmp_path, skill_bundle="vault-missing",
            )
        assert result == ""
        warnings = [
            c for c in captured
            if c.get("event") == "talker.daemon.skill_missing"
        ]
        assert len(warnings) == 1
        assert warnings[0]["log_level"] == "warning"
        assert warnings[0]["skill_bundle"] == "vault-missing"

    def test_per_load_debug_log_fires(self, tmp_path: Path):
        """Per-conversation reload must emit a structured log so an
        operator can grep ``talker.conversation.skill_md_loaded`` and
        confirm the hot-reload IS happening per turn."""
        from alfred.telegram.daemon import _load_system_prompt
        _write_skill(tmp_path, "vault-talker", "Body.")
        with structlog.testing.capture_logs() as captured:
            _load_system_prompt(tmp_path, skill_bundle="vault-talker")
        load_logs = [
            c for c in captured
            if c.get("event") == "talker.conversation.skill_md_loaded"
        ]
        assert len(load_logs) == 1
        entry = load_logs[0]
        assert entry["log_level"] == "debug"
        assert entry["skill_bundle"] == "vault-talker"
        assert entry["char_count"] == len("Body.")
        assert entry["path"].endswith("vault-talker/SKILL.md")


# ---------------------------------------------------------------------------
# build_system_prompt_provider — closure semantics
# ---------------------------------------------------------------------------


class TestBuildSystemPromptProvider:
    def test_provider_returns_templated_skill(self, tmp_path: Path):
        """First-call: returns the SKILL with instance templating
        substitutions applied (instance_name + instance_canonical)."""
        from alfred.telegram.daemon import build_system_prompt_provider
        _write_skill(
            tmp_path, "vault-talker",
            "Hello {{instance_name}} ({{instance_canonical}}).",
        )
        config = _build_min_config(skill_bundle="vault-talker")
        provider = build_system_prompt_provider(tmp_path, config)
        assert provider() == "Hello Hypatia (Hypatia)."

    def test_provider_re_reads_on_each_call(self, tmp_path: Path):
        """The headline contract: SKILL.md edited mid-stream is
        visible on the NEXT provider call. No restart needed."""
        from alfred.telegram.daemon import build_system_prompt_provider
        skill_path = _write_skill(
            tmp_path, "vault-talker", "Original {{instance_name}}.",
        )
        config = _build_min_config(skill_bundle="vault-talker")
        provider = build_system_prompt_provider(tmp_path, config)
        first = provider()
        # Operator commits a SKILL update.
        skill_path.write_text(
            "Updated {{instance_name}} with new guidance.",
            encoding="utf-8",
        )
        # Next conversation start picks up the new SKILL — no restart.
        second = provider()
        assert first == "Original Hypatia."
        assert second == "Updated Hypatia with new guidance."

    def test_provider_handles_missing_skill_gracefully(
        self, tmp_path: Path,
    ):
        """Missing SKILL.md → empty string (no raise). Daemon stays
        up; turn proceeds with no instance-specific guidance."""
        from alfred.telegram.daemon import build_system_prompt_provider
        config = _build_min_config(skill_bundle="vault-nonexistent")
        provider = build_system_prompt_provider(tmp_path, config)
        # Templating on empty string is a noop → stays empty.
        assert provider() == ""

    def test_provider_uses_configs_skill_bundle(self, tmp_path: Path):
        """Different instances point at different bundles. Provider
        reads the bundle from config.instance.skill_bundle, not a
        hardcoded default."""
        from alfred.telegram.daemon import build_system_prompt_provider
        _write_skill(tmp_path, "vault-kalle", "KAL-LE-specific.")
        _write_skill(tmp_path, "vault-talker", "Salem-default.")
        kalle_config = _build_min_config(skill_bundle="vault-kalle")
        provider = build_system_prompt_provider(tmp_path, kalle_config)
        assert provider() == "KAL-LE-specific."


# ---------------------------------------------------------------------------
# bot.build_app — accepts callable OR static string
# ---------------------------------------------------------------------------


class TestBuildAppProviderWiring:
    """Verify build_app stashes a CALLABLE in bot_data[_KEY_SYSTEM]
    regardless of whether the caller passed a callable (production
    path) or a static string (legacy + test path)."""

    def _build(self, prompt_arg, monkeypatch):
        """Helper — build a minimal app with the given prompt arg.

        We monkeypatch Application.builder to return a stub whose
        ``build()`` produces a MagicMock with a ``bot_data`` dict so
        we can inspect the wiring without spinning up a real Telegram
        application.
        """
        from alfred.telegram import bot as bot_mod

        fake_app = MagicMock()
        fake_app.bot_data = {}
        # bot.build_app calls .add_handler / .add_error_handler /
        # .post_init in addition to setting bot_data; the MagicMock
        # absorbs all of those without raising.

        fake_builder = MagicMock()
        fake_builder.token.return_value = fake_builder
        fake_builder.build.return_value = fake_app
        monkeypatch.setattr(
            bot_mod.Application, "builder",
            lambda: fake_builder,
        )

        config = _build_min_config()
        state_mgr = MagicMock()
        client = MagicMock()
        bot_mod.build_app(
            config=config,
            state_mgr=state_mgr,
            anthropic_client=client,
            system_prompt_provider=prompt_arg,
            vault_context_str="vault-context",
        )
        return fake_app.bot_data

    def test_callable_arg_stashed_directly(self, monkeypatch):
        """Production path: daemon passes a provider closure; build_app
        stashes it as-is."""
        from alfred.telegram.bot import _KEY_SYSTEM

        called = {"count": 0}

        def my_provider() -> str:
            called["count"] += 1
            return "fresh-content"

        bot_data = self._build(my_provider, monkeypatch)
        stashed = bot_data[_KEY_SYSTEM]
        assert callable(stashed)
        # Invoking the stashed value calls our provider.
        assert stashed() == "fresh-content"
        assert called["count"] == 1
        # Second invocation re-runs (the whole point of hot-reload).
        assert stashed() == "fresh-content"
        assert called["count"] == 2

    def test_string_arg_wrapped_in_constant_lambda(self, monkeypatch):
        """Legacy compat: caller passes a string; build_app wraps it
        in a constant-returning callable so the read-site at the
        message handler's call shape stays uniform."""
        from alfred.telegram.bot import _KEY_SYSTEM

        bot_data = self._build("static-prompt-content", monkeypatch)
        stashed = bot_data[_KEY_SYSTEM]
        assert callable(stashed)
        assert stashed() == "static-prompt-content"
        # Multiple invocations all return the same string (no reload).
        assert stashed() == "static-prompt-content"


# ---------------------------------------------------------------------------
# End-to-end — daemon-style provider survives a SKILL edit
# ---------------------------------------------------------------------------


class TestEndToEndHotReload:
    """The headline scenario from the QA finding: SKILL committed →
    next conversation must see the new SKILL without daemon restart."""

    def test_hypatia_dj_tracker_scenario(self, tmp_path: Path):
        """Mirrors Hypatia's 2026-05-04 22:27 UTC conversation. Original
        SKILL says "vault only supports body_append". New SKILL adds
        "body_insert_at and body_replace are now available". Provider
        invocation between commits picks up the new content without
        a restart."""
        from alfred.telegram.daemon import build_system_prompt_provider

        original_skill = (
            "# {{instance_name}}\n\n"
            "Vault edits: use body_append for content additions.\n"
        )
        new_skill = (
            "# {{instance_name}}\n\n"
            "Vault edits: body_append (end-of-doc), body_insert_at "
            "(anchored mid-doc), body_replace (full rewrite). "
            "Mutually exclusive.\n"
        )

        skill_path = _write_skill(tmp_path, "vault-hypatia", original_skill)
        config = _build_min_config(skill_bundle="vault-hypatia")
        provider = build_system_prompt_provider(tmp_path, config)

        # Conversation #1 (22:27 UTC, pre-fix scenario).
        prompt_at_2227 = provider()
        assert "body_append" in prompt_at_2227
        assert "body_insert_at" not in prompt_at_2227

        # Operator commits the body-mutation SKILL update at 22:30 UTC.
        skill_path.write_text(new_skill, encoding="utf-8")

        # Conversation #2 (22:35 UTC) — pre-fix would have seen the
        # OLD prompt; post-fix sees the new SKILL on the very next
        # provider call. NO daemon restart between conversations.
        prompt_at_2235 = provider()
        assert "body_insert_at" in prompt_at_2235
        assert "body_replace" in prompt_at_2235
        # Templating still applies fresh.
        assert "Hypatia" in prompt_at_2235
