"""Regression pin for ``build_app``'s ``system_prompt=`` legacy kwarg.

Background (#49 → 13 residual telegram failures, fixed 2026-05-09 in
Batch A): when ``build_app`` was renamed in 9904730 to take
``system_prompt_provider`` instead of ``system_prompt``, 13 telegram-
suite tests across multiple files kept calling the old kwarg name and
broke with::

    TypeError: build_app() got an unexpected keyword argument 'system_prompt'

Failing call sites (sampled by team-lead 2026-05-09):
  - tests/telegram/test_idle_tick.py:244
  - tests/telegram/test_implicit_escalation.py:291
  - tests/telegram/test_fiction_command.py:356
  - tests/telegram/test_model_switch.py:194
  - tests/telegram/test_silent_capture.py + 8 more

Rather than sweep 13 fixture files, ``build_app`` now accepts BOTH
kwargs as aliases for the same input slot. The legacy ``system_prompt=``
kwarg is keyword-only (``*,`` separator) so it can't collide with
positional usage. Mirrors the defensive-accept-both shape already used
by ``_resolve_system_prompt`` at the read site — symmetry at the write
site.

Pinned contracts:
  1. ``system_prompt="text"`` works (legacy kwarg)
  2. ``system_prompt_provider=callable`` works (canonical kwarg)
  3. ``system_prompt_provider="text"`` works (canonical kwarg accepts string too)
  4. Both kwargs simultaneously raises ValueError
  5. Neither kwarg falls back to empty-string default (useful for
     handler-introspection tests that don't need a real prompt)

Per ``feedback_regression_pin_unconditional.md``: this file is a
regression-pin home. NO ``pytest.importorskip`` at module level.
Imports are stdlib + ``alfred`` + ``unittest.mock`` only.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from alfred.telegram import bot as bot_mod
from alfred.telegram.bot import _KEY_SYSTEM
from alfred.telegram.config import (
    AnthropicConfig,
    InstanceConfig,
    LoggingConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
)


def _build_min_config(tmp_path) -> TalkerConfig:
    """Return the minimum TalkerConfig that ``build_app`` accepts.

    Mirrors the ``talker_config`` fixture in conftest.py but inlined
    here so this regression-pin file has zero hidden test-helper
    dependencies (single-file readable).
    """
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    return TalkerConfig(
        bot_token="test-token",
        allowed_users=[1],
        primary_users=["person/Andrew Newton"],
        anthropic=AnthropicConfig(
            api_key="test-key",
            model="claude-sonnet-4-6",
        ),
        stt=STTConfig(api_key="test-stt", model="whisper-large-v3"),
        session=SessionConfig(
            gap_timeout_seconds=1800,
            state_path=str(tmp_path / "talker_state.json"),
        ),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name="Salem", canonical="S.A.L.E.M."),
    )


def _patch_application_builder(monkeypatch):
    """Stub ``Application.builder()`` so build_app works without a real
    Telegram token + network. Returns the fake app's bot_data dict.
    """
    fake_app = MagicMock()
    fake_app.bot_data = {}
    fake_builder = MagicMock()
    fake_builder.token.return_value = fake_builder
    fake_builder.build.return_value = fake_app
    monkeypatch.setattr(
        bot_mod.Application, "builder",
        lambda: fake_builder,
    )
    return fake_app


# ---------------------------------------------------------------------------
# 1. Legacy ``system_prompt=`` kwarg works (the headline #49 fix)
# ---------------------------------------------------------------------------


def test_build_app_accepts_system_prompt_legacy_kwarg(tmp_path, monkeypatch):
    """Legacy ``system_prompt="..."`` kwarg path was load-bearing for
    13 test files when ``build_app``'s signature was renamed. Both
    shapes must continue to work.

    The string is normalised to a constant-returning callable + stashed
    in ``bot_data[_KEY_SYSTEM]``, matching the post-rename canonical
    behaviour for the string branch of ``system_prompt_provider``.
    """
    fake_app = _patch_application_builder(monkeypatch)
    config = _build_min_config(tmp_path)
    state_mgr = MagicMock()
    client = MagicMock()

    bot_mod.build_app(
        config=config,
        state_mgr=state_mgr,
        anthropic_client=client,
        system_prompt="hello-legacy-prompt",
        vault_context_str="",
    )

    stashed = fake_app.bot_data[_KEY_SYSTEM]
    assert callable(stashed), (
        "build_app's contract is that bot_data[_KEY_SYSTEM] always "
        "holds a callable, regardless of which kwarg the caller used"
    )
    assert stashed() == "hello-legacy-prompt"
    # Multiple invocations return the same value (no caching needed
    # for the static-string path).
    assert stashed() == "hello-legacy-prompt"


# ---------------------------------------------------------------------------
# 2. Canonical ``system_prompt_provider=`` kwarg still works
# ---------------------------------------------------------------------------


def test_build_app_accepts_system_prompt_provider_callable(
    tmp_path, monkeypatch,
):
    """Canonical kwarg with a callable — the production daemon path.

    Pinned separately here so a future refactor that drops
    ``system_prompt_provider`` (e.g. by inverting which is "canonical")
    breaks loudly rather than silently breaking the daemon.
    """
    fake_app = _patch_application_builder(monkeypatch)
    config = _build_min_config(tmp_path)
    state_mgr = MagicMock()
    client = MagicMock()

    call_count = {"n": 0}

    def my_provider() -> str:
        call_count["n"] += 1
        return f"fresh-content-{call_count['n']}"

    bot_mod.build_app(
        config=config,
        state_mgr=state_mgr,
        anthropic_client=client,
        system_prompt_provider=my_provider,
        vault_context_str="",
    )

    stashed = fake_app.bot_data[_KEY_SYSTEM]
    assert callable(stashed)
    # Each invocation re-runs the provider — the hot-reload contract.
    assert stashed() == "fresh-content-1"
    assert stashed() == "fresh-content-2"
    assert call_count["n"] == 2


def test_build_app_accepts_system_prompt_provider_string(
    tmp_path, monkeypatch,
):
    """Canonical kwarg with a string — same shape as the legacy kwarg
    but spelled with the new name. Wrapped in a constant lambda."""
    fake_app = _patch_application_builder(monkeypatch)
    config = _build_min_config(tmp_path)
    state_mgr = MagicMock()
    client = MagicMock()

    bot_mod.build_app(
        config=config,
        state_mgr=state_mgr,
        anthropic_client=client,
        system_prompt_provider="static-via-canonical-kwarg",
        vault_context_str="",
    )

    stashed = fake_app.bot_data[_KEY_SYSTEM]
    assert callable(stashed)
    assert stashed() == "static-via-canonical-kwarg"


# ---------------------------------------------------------------------------
# 3. Double-spec rejected
# ---------------------------------------------------------------------------


def test_build_app_rejects_double_provider_spec(tmp_path, monkeypatch):
    """Passing BOTH kwargs simultaneously must raise ValueError.

    The two are aliases for the same input slot — silently picking one
    would leave the precedence ambiguous and bury a real config bug
    (e.g., a daemon that accidentally sets ``system_prompt`` alongside
    its ``system_prompt_provider`` closure). Fail loud at construction
    time.
    """
    _patch_application_builder(monkeypatch)
    config = _build_min_config(tmp_path)
    state_mgr = MagicMock()
    client = MagicMock()

    with pytest.raises(ValueError) as exc_info:
        bot_mod.build_app(
            config=config,
            state_mgr=state_mgr,
            anthropic_client=client,
            system_prompt_provider=lambda: "from-callable",
            system_prompt="from-legacy-kwarg",
            vault_context_str="",
        )

    msg = str(exc_info.value)
    # Field-level pin: the error names BOTH kwargs so the operator
    # reading the trace knows which to drop.
    assert "system_prompt_provider" in msg
    assert "system_prompt" in msg


# ---------------------------------------------------------------------------
# 4. Neither kwarg → empty-string default
# ---------------------------------------------------------------------------


def test_build_app_neither_kwarg_defaults_to_empty(tmp_path, monkeypatch):
    """When neither kwarg is supplied, fall back to an empty static
    prompt. Useful for handler-introspection tests that build an app
    purely to assert command-registration shape and never actually run
    the LLM turn.

    Pre-2026-05-09 ``system_prompt_provider`` was required (no default)
    — passing nothing raised TypeError. Post-fix the default is empty
    string, the bot_data slot still holds a callable (just one that
    returns ``""``), and the read site at handle_message handles empty
    prompts gracefully (the system block is just empty, the LLM gets
    only the vault-context + user message).
    """
    fake_app = _patch_application_builder(monkeypatch)
    config = _build_min_config(tmp_path)
    state_mgr = MagicMock()
    client = MagicMock()

    bot_mod.build_app(
        config=config,
        state_mgr=state_mgr,
        anthropic_client=client,
        # No system_prompt_provider, no system_prompt.
        vault_context_str="",
    )

    stashed = fake_app.bot_data[_KEY_SYSTEM]
    assert callable(stashed)
    assert stashed() == ""


# ---------------------------------------------------------------------------
# 5. Signature introspection — ``system_prompt`` is keyword-only
# ---------------------------------------------------------------------------


def test_build_app_signature_includes_legacy_kwarg() -> None:
    """The legacy kwarg lives on the signature as keyword-only.

    Keyword-only via the ``*,`` separator — there's no positional
    ambiguity with ``system_prompt_provider`` and no risk of a
    positional caller silently binding the wrong slot.
    """
    import inspect

    sig = inspect.signature(bot_mod.build_app)
    assert "system_prompt" in sig.parameters
    assert "system_prompt_provider" in sig.parameters
    # ``system_prompt`` must be keyword-only.
    sp = sig.parameters["system_prompt"]
    assert sp.kind is inspect.Parameter.KEYWORD_ONLY, (
        "system_prompt must be KEYWORD_ONLY to prevent positional-bind "
        f"ambiguity. Got kind={sp.kind!r}"
    )
    # Default is None — sentinel meaning "not specified".
    assert sp.default is None
