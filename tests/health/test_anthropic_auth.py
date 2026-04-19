"""Unit tests for alfred.health.anthropic_auth.

We never call the real Anthropic API here.  Instead we monkeypatch the
``anthropic.Anthropic`` constructor to return a fake client whose
``messages.count_tokens`` and ``messages.create`` we control.  Tests
cover:

  * no api_key → SKIP
  * count_tokens success → OK
  * count_tokens raises → FAIL
  * messages.create fallback (when count_tokens is missing) → WARN
  * messages.create raises → FAIL
  * Anthropic() constructor raises → FAIL
  * resolve_api_key precedence (env vs config)
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from alfred.health import anthropic_auth as mod
from alfred.health.types import Status


class _FakeMessages:
    """Stand-in for client.messages with pluggable count_tokens / create."""

    def __init__(
        self,
        count_tokens=None,  # noqa: ANN001
        create=None,  # noqa: ANN001
    ):
        if count_tokens is not None:
            self.count_tokens = count_tokens  # attribute-present check
        if create is not None:
            self.create = create


class _FakeClient:
    def __init__(self, messages: _FakeMessages):
        self.messages = messages


def _install_fake_anthropic(monkeypatch, fake_client, raises=None):  # noqa: ANN001
    """Patch ``anthropic.Anthropic`` to return ``fake_client``.

    If ``raises`` is provided, the constructor will raise that exception
    instead. We inject a minimal fake module into ``sys.modules`` so the
    ``import anthropic`` inside ``check_anthropic_auth`` sees our shim.
    """
    def _ctor(*args, **kwargs):  # noqa: ANN001
        if raises is not None:
            raise raises
        return fake_client

    fake_mod = SimpleNamespace(Anthropic=_ctor)
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)


class TestCheckAnthropicAuth:
    async def test_no_api_key_returns_skip(self) -> None:
        result = await mod.check_anthropic_auth(api_key=None)
        assert result.status == Status.SKIP
        assert result.name == "anthropic-auth"
        assert "no api_key" in result.detail

    async def test_empty_api_key_returns_skip(self) -> None:
        result = await mod.check_anthropic_auth(api_key="")
        assert result.status == Status.SKIP

    async def test_count_tokens_success_returns_ok(self, monkeypatch) -> None:
        calls: list[dict] = []

        def _count_tokens(*, model, messages):
            calls.append({"model": model, "messages": messages})
            return {"input_tokens": 1}

        client = _FakeClient(_FakeMessages(count_tokens=_count_tokens))
        _install_fake_anthropic(monkeypatch, client)

        result = await mod.check_anthropic_auth("sk-test", model="claude-foo")
        assert result.status == Status.OK
        assert result.data["probe"] == "count_tokens"
        assert result.data["model"] == "claude-foo"
        assert result.latency_ms is not None
        # The stub was actually called
        assert len(calls) == 1
        assert calls[0]["model"] == "claude-foo"

    async def test_count_tokens_failure_returns_fail(self, monkeypatch) -> None:
        def _count_tokens(*, model, messages):
            raise RuntimeError("401 unauthorized")

        client = _FakeClient(_FakeMessages(count_tokens=_count_tokens))
        _install_fake_anthropic(monkeypatch, client)

        result = await mod.check_anthropic_auth("sk-bad")
        assert result.status == Status.FAIL
        assert "401 unauthorized" in result.detail

    async def test_fallback_used_when_count_tokens_missing(self, monkeypatch) -> None:
        calls: list[dict] = []

        def _create(*, model, max_tokens, messages):
            calls.append({"model": model, "max_tokens": max_tokens})
            return {"content": [{"type": "text", "text": "hi"}]}

        # No count_tokens passed → attribute absent on messages
        client = _FakeClient(_FakeMessages(create=_create))
        _install_fake_anthropic(monkeypatch, client)

        result = await mod.check_anthropic_auth("sk-test")
        assert result.status == Status.WARN  # fallback surfaces as WARN
        assert "count_tokens unavailable" in result.detail
        assert result.data["probe"] == "messages.create"
        assert calls[0]["max_tokens"] == 1

    async def test_fallback_failure_returns_fail(self, monkeypatch) -> None:
        def _create(*, model, max_tokens, messages):
            raise RuntimeError("quota exceeded")

        client = _FakeClient(_FakeMessages(create=_create))
        _install_fake_anthropic(monkeypatch, client)

        result = await mod.check_anthropic_auth("sk-test")
        assert result.status == Status.FAIL
        assert "quota exceeded" in result.detail

    async def test_constructor_failure_returns_fail(self, monkeypatch) -> None:
        _install_fake_anthropic(
            monkeypatch,
            fake_client=None,
            raises=ValueError("bad key format"),
        )

        result = await mod.check_anthropic_auth("sk-garbage")
        assert result.status == Status.FAIL
        assert "bad key format" in result.detail

    async def test_sdk_import_failure_returns_fail(self, monkeypatch) -> None:
        # Force ``import anthropic`` to raise ImportError.
        monkeypatch.setitem(sys.modules, "anthropic", None)

        result = await mod.check_anthropic_auth("sk-test")
        assert result.status == Status.FAIL
        assert "not installed" in result.detail


class TestResolveApiKey:
    def test_env_var_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
        raw = {"telegram": {"anthropic": {"api_key": "sk-config"}}}
        assert mod.resolve_api_key(raw) == "sk-env"

    def test_falls_back_to_telegram_config(self, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        raw = {"telegram": {"anthropic": {"api_key": "sk-config"}}}
        assert mod.resolve_api_key(raw) == "sk-config"

    def test_ignores_unresolved_placeholder(self, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        # config.yaml env-var syntax that didn't resolve — treat as absent
        raw = {"telegram": {"anthropic": {"api_key": "${ANTHROPIC_API_KEY}"}}}
        assert mod.resolve_api_key(raw) is None

    def test_no_env_no_config_returns_none(self, monkeypatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert mod.resolve_api_key({}) is None
