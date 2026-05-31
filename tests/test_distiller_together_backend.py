"""Tests for the v2 distiller's Together.ai backend + dispatcher branch.

Path C Phase 1.5 spike (2026-05-31). Sibling to
``test_distiller_ollama_backend.py``; covers the surface added by the
Together backend ship:

  * ``call_together_no_tools`` happy path — mocked Together returns
    an OpenAI-shaped response; helper returns ``(text, metadata)``
    with ``stop_reason`` mapped from ``finish_reason``; the request
    carries the ``Authorization: Bearer <key>`` header and posts to
    the hard-coded ``https://api.together.xyz/v1/chat/completions``
    endpoint.
  * Dispatcher routes ``backend="together"`` to the Together
    backend with the configured api_key + model.
  * Dispatcher raises ``RuntimeError`` (NO silent fallback) when
    ``backend="together"`` but ``together_api_key`` is empty.

Tests do NOT call real Together OR real Anthropic. ``httpx.MockTransport``
mocks the Together HTTP layer (mirrors the Ollama test pattern).

Per ``builder.md`` discipline: test fixtures for secret-shaped values
use obviously-fake placeholders (``DUMMY_TOGETHER_TEST_KEY``) so
scanners (GitGuardian, etc.) don't fire on the literal.
"""

from __future__ import annotations

import json

import httpx
import pytest

from alfred.distiller import extractor as extractor_mod
from alfred.distiller.backends import together as together_mod
from alfred.distiller.config import (
    AnthropicConfig,
    DistillerConfig,
    ExtractionConfig,
)


# --- Helpers ---------------------------------------------------------------


def _config(
    *,
    backend: str = "anthropic",
    together_api_key: str = "DUMMY_TOGETHER_TEST_KEY",
    together_model: str = "Qwen/Qwen2.5-72B-Instruct-Turbo",
    anthropic_model: str = "claude-opus-4-7",
) -> DistillerConfig:
    """Build a DistillerConfig with the Together backend selected.

    Bypasses ``load_from_unified`` because the dispatcher only reads
    ``config.extraction.backend`` / ``config.extraction.together_*`` /
    ``config.anthropic.*`` — no other fields touched.
    """
    return DistillerConfig(
        extraction=ExtractionConfig(
            backend=backend,
            together_api_key=together_api_key,
            together_model=together_model,
        ),
        anthropic=AnthropicConfig(
            api_key="DUMMY_ANTHROPIC_TEST_KEY",
            model=anthropic_model,
            max_tokens=4096,
        ),
    )


def _together_response(content: str, finish_reason: str = "stop") -> dict:
    """Build an OpenAI-shaped chat-completions response dict."""
    return {
        "id": "chatcmpl-together-test",
        "object": "chat.completion",
        "model": "Qwen/Qwen2.5-72B-Instruct-Turbo",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            },
        ],
    }


def _patch_httpx(monkeypatch, dispatch):  # noqa: ANN001
    """Replace ``together_mod.httpx.AsyncClient`` so each instantiation
    returns an AsyncClient backed by a MockTransport that calls
    ``dispatch(request)`` to produce the response.

    Mirrors ``_patch_httpx`` in ``test_distiller_ollama_backend.py``.
    """
    real_async_client = httpx.AsyncClient

    def _make_client(*args, **kwargs):  # noqa: ANN001
        return real_async_client(
            transport=httpx.MockTransport(dispatch),
            timeout=kwargs.get("timeout"),
        )

    monkeypatch.setattr(together_mod.httpx, "AsyncClient", _make_client)


# --- call_together_no_tools — happy path -----------------------------------


class TestTogetherHappyPath:
    async def test_call_together_no_tools_includes_bearer_auth_header(
        self, monkeypatch,
    ) -> None:
        """Together requires Bearer-token auth. Assert the
        ``Authorization`` header is built from the api_key and that
        the request lands on the hard-coded Together URL."""
        captured: list[httpx.Request] = []

        def _dispatch(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(
                200,
                json=_together_response('{"learnings": []}', "stop"),
            )

        _patch_httpx(monkeypatch, _dispatch)

        text, meta = await together_mod.call_together_no_tools(
            prompt="extract this",
            system="you are an extractor",
            model="Qwen/Qwen2.5-72B-Instruct-Turbo",
            max_tokens=2048,
            api_key="DUMMY_TOGETHER_TEST_KEY",
        )
        assert text == '{"learnings": []}'
        assert meta == {"stop_reason": "stop"}

        # URL is the hard-coded Together endpoint.
        assert len(captured) == 1
        req = captured[0]
        assert str(req.url) == "https://api.together.xyz/v1/chat/completions"
        assert req.method == "POST"

        # Bearer-token auth header built from api_key.
        assert req.headers["Authorization"] == "Bearer DUMMY_TOGETHER_TEST_KEY"
        # Content-Type explicit on the request (httpx infers but the
        # backend sets it explicitly for protocol clarity).
        assert req.headers["Content-Type"] == "application/json"

        # Payload shape matches OpenAI chat.completion contract.
        payload = json.loads(req.content)
        assert payload["model"] == "Qwen/Qwen2.5-72B-Instruct-Turbo"
        assert payload["max_tokens"] == 2048
        assert payload["stream"] is False
        assert payload["messages"] == [
            {"role": "system", "content": "you are an extractor"},
            {"role": "user", "content": "extract this"},
        ]

    async def test_call_together_no_tools_returns_text_and_metadata(
        self, monkeypatch,
    ) -> None:
        """Sanity-check the ``(text, metadata)`` return contract on a
        body-bearing response with a non-default finish_reason. Mirrors
        the Anthropic / Ollama backends' downstream-parser contract:
        text is the raw assistant message, metadata.stop_reason is
        the OpenAI ``finish_reason`` verbatim."""
        def _dispatch(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_together_response(
                    '{"learnings": [{"type": "decision", "title": "x"}]}',
                    "length",
                ),
            )

        _patch_httpx(monkeypatch, _dispatch)

        text, meta = await together_mod.call_together_no_tools(
            prompt="hi",
            api_key="DUMMY_TOGETHER_TEST_KEY",
        )
        # Text round-trips verbatim (caller is responsible for JSON
        # parsing + Pydantic validation downstream).
        assert text == '{"learnings": [{"type": "decision", "title": "x"}]}'
        # finish_reason "length" → stop_reason "length" (same string;
        # diagnostic intent matches Anthropic's "max_tokens").
        assert meta == {"stop_reason": "length"}


# --- Dispatcher: _call_extraction_llm — Together branch --------------------


class TestExtractionDispatcherTogether:
    async def test_extractor_dispatches_to_together_backend_when_configured(
        self, monkeypatch,
    ) -> None:
        """``backend="together"`` routes to the Together backend with
        config.extraction.together_* values threaded through. Mirrors
        the Ollama-dispatch test in
        ``test_distiller_ollama_backend.py::TestExtractionDispatcher``."""
        captured: dict = {}

        async def _fake_together(*, prompt, system, model, max_tokens, api_key):
            captured["prompt"] = prompt
            captured["system"] = system
            captured["model"] = model
            captured["max_tokens"] = max_tokens
            captured["api_key"] = api_key
            return ("together-text", {"stop_reason": "stop"})

        monkeypatch.setattr(
            extractor_mod, "call_together_no_tools", _fake_together,
        )

        cfg = _config(
            backend="together",
            together_api_key="DUMMY_TOGETHER_TEST_KEY",
            together_model="Qwen/Qwen2.5-72B-Instruct",  # non-Turbo override
        )
        text, meta = await extractor_mod._call_extraction_llm(
            prompt="hello", system="be brief", config=cfg,
        )
        assert text == "together-text"
        assert meta == {"stop_reason": "stop"}
        # Together-specific kwargs threaded through.
        assert captured["model"] == "Qwen/Qwen2.5-72B-Instruct"
        assert captured["api_key"] == "DUMMY_TOGETHER_TEST_KEY"
        # max_tokens shared with the Anthropic config (the spike
        # doesn't introduce a separate together_max_tokens knob).
        assert captured["max_tokens"] == 4096
        # Prompt + system threaded verbatim.
        assert captured["prompt"] == "hello"
        assert captured["system"] == "be brief"

    async def test_extractor_raises_when_together_api_key_missing(
        self,
    ) -> None:
        """Per spike spec: NO silent fallback. ``backend="together"``
        with empty ``together_api_key`` raises RuntimeError at the
        dispatcher gate (operator-actionable layer) with a message
        that names the config field + env var, so the operator sees
        immediately what to fix.

        Distinct from the in-backend api_key guard in together.py —
        this tests the dispatcher's fail-loud layer, which is where
        a config-driven empty-key failure should surface (rather than
        letting Together return a 401 with a less-actionable message)."""
        cfg = _config(backend="together", together_api_key="")
        with pytest.raises(RuntimeError) as exc_info:
            await extractor_mod._call_extraction_llm(
                prompt="x", system="y", config=cfg,
            )
        msg = str(exc_info.value)
        # Message names the config field operator would edit.
        assert "together_api_key" in msg
        # Message names the env-var pattern operator would set.
        assert "TOGETHER_API_KEY" in msg
        # Message names the backend so an operator grepping logs for
        # "together" finds the failure.
        assert "together" in msg.lower()
