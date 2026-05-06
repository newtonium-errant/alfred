"""Tests for the v2 distiller's Ollama backend + dispatcher.

Path C Phase 1 spike (2026-05-06). Covers:

  * Config field defaults — ``extraction.backend`` stays ``"anthropic"``;
    new ``ollama_endpoint`` / ``ollama_model`` carry sensible defaults.
  * ``call_ollama_no_tools`` happy path — mocked Ollama returns an
    OpenAI-shaped response; helper returns ``(text, metadata)`` with
    ``stop_reason`` mapped from ``finish_reason``.
  * ``call_ollama_no_tools`` error paths:
      - HTTP 503 → ``RuntimeError`` (NO silent fallback per spike spec)
      - ``httpx.RequestError`` (connection refused, timeout) →
        ``RuntimeError`` with the underlying error class named
      - Empty/malformed response → ``("", None)`` so the extractor's
        repair-retry path engages
  * ``_extract_first_choice`` defensive parser — unexpected shapes
    return ``("", None)``, never raise
  * Dispatcher ``_call_extraction_llm``:
      - ``backend="anthropic"`` (default) → calls anthropic backend
      - ``backend="ollama"`` → calls ollama backend with the configured
        model + endpoint
      - ``backend="rogue"`` → ``RuntimeError`` (NO silent fallback)
      - Backend value is case-insensitive (``"OLLAMA"`` works)

Tests do NOT call real Ollama OR real Anthropic. ``httpx.MockTransport``
mocks the Ollama HTTP layer (mirrors ``tests/test_transport_client.py``);
the anthropic SDK is monkeypatched at ``call_anthropic_no_tools`` to
avoid pulling in the SDK's auth probe.
"""

from __future__ import annotations

import json

import httpx
import pytest
import structlog

from alfred.distiller import extractor as extractor_mod
from alfred.distiller.backends import ollama as ollama_mod
from alfred.distiller.config import (
    AnthropicConfig,
    DistillerConfig,
    ExtractionConfig,
)


# --- Helpers ---------------------------------------------------------------


def _config(
    *,
    backend: str = "anthropic",
    ollama_endpoint: str = "http://localhost:11434",
    ollama_model: str = "qwen2.5:72b-instruct-q4_K_M",
    anthropic_model: str = "claude-opus-4-7",
) -> DistillerConfig:
    """Build a DistillerConfig with the requested backend selection.

    Bypasses ``load_from_unified`` because the dispatcher only reads
    ``config.extraction.backend`` / ``config.extraction.ollama_*`` /
    ``config.anthropic.*`` — no other fields touched.
    """
    return DistillerConfig(
        extraction=ExtractionConfig(
            backend=backend,
            ollama_endpoint=ollama_endpoint,
            ollama_model=ollama_model,
        ),
        anthropic=AnthropicConfig(
            api_key="DUMMY_ANTHROPIC_TEST_KEY",
            model=anthropic_model,
            max_tokens=4096,
        ),
    )


def _ollama_response(content: str, finish_reason: str = "stop") -> dict:
    """Build an OpenAI-shaped chat-completions response dict."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": "qwen2.5:72b-instruct-q4_K_M",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            },
        ],
    }


def _patch_httpx(monkeypatch, dispatch):  # noqa: ANN001
    """Replace ``ollama_mod.httpx.AsyncClient`` so each instantiation
    returns an AsyncClient backed by a MockTransport that calls
    ``dispatch(request)`` to produce the response.

    Mirrors the pattern in ``tests/test_transport_client.py:_make_client``.
    """
    real_async_client = httpx.AsyncClient

    def _make_client(*args, **kwargs):  # noqa: ANN001
        return real_async_client(
            transport=httpx.MockTransport(dispatch),
            timeout=kwargs.get("timeout"),
        )

    monkeypatch.setattr(ollama_mod.httpx, "AsyncClient", _make_client)


# --- Config field defaults -------------------------------------------------


class TestConfigFieldDefaults:
    def test_extraction_backend_defaults_to_anthropic(self) -> None:
        """Existing config.yaml files (no ``backend`` field) must keep
        the Anthropic path unchanged."""
        cfg = ExtractionConfig()
        assert cfg.backend == "anthropic"

    def test_ollama_endpoint_default_is_localhost(self) -> None:
        cfg = ExtractionConfig()
        assert cfg.ollama_endpoint == "http://localhost:11434"

    def test_ollama_model_default_is_qwen_72b(self) -> None:
        """Spike defaults to the 72B q4_K_M target; spike harness
        overrides per-run."""
        cfg = ExtractionConfig()
        assert cfg.ollama_model == "qwen2.5:72b-instruct-q4_K_M"


# --- call_ollama_no_tools — happy path -------------------------------------


class TestOllamaHappyPath:
    async def test_returns_text_and_stop_reason(self, monkeypatch) -> None:
        captured: list[httpx.Request] = []

        def _dispatch(req: httpx.Request) -> httpx.Response:
            captured.append(req)
            return httpx.Response(
                200,
                json=_ollama_response('{"learnings": []}', "stop"),
            )

        _patch_httpx(monkeypatch, _dispatch)

        text, meta = await ollama_mod.call_ollama_no_tools(
            prompt="extract this",
            system="you are an extractor",
            model="qwen2.5:7b-instruct",
            max_tokens=2048,
            endpoint="http://example.invalid:11434",
        )
        assert text == '{"learnings": []}'
        assert meta == {"stop_reason": "stop"}

        # URL was the OpenAI-compatible chat-completions endpoint.
        assert len(captured) == 1
        req = captured[0]
        assert str(req.url) == "http://example.invalid:11434/v1/chat/completions"
        assert req.method == "POST"

        # Payload shape matches OpenAI chat.completion contract.
        payload = json.loads(req.content)
        assert payload["model"] == "qwen2.5:7b-instruct"
        assert payload["max_tokens"] == 2048
        assert payload["stream"] is False
        assert payload["messages"] == [
            {"role": "system", "content": "you are an extractor"},
            {"role": "user", "content": "extract this"},
        ]

    async def test_no_system_omits_system_message(self, monkeypatch) -> None:
        """``system=None`` must not inject an empty system message — the
        request payload should carry the user message only."""
        captured: list[dict] = []

        def _dispatch(req: httpx.Request) -> httpx.Response:
            captured.append(json.loads(req.content))
            return httpx.Response(200, json=_ollama_response("{}"))

        _patch_httpx(monkeypatch, _dispatch)

        await ollama_mod.call_ollama_no_tools(prompt="hi")
        assert captured[0]["messages"] == [{"role": "user", "content": "hi"}]

    async def test_endpoint_trailing_slash_normalized(
        self, monkeypatch,
    ) -> None:
        """Endpoint with trailing slash should not double-slash the path."""
        captured: list[str] = []

        def _dispatch(req: httpx.Request) -> httpx.Response:
            captured.append(str(req.url))
            return httpx.Response(200, json=_ollama_response(""))

        _patch_httpx(monkeypatch, _dispatch)

        await ollama_mod.call_ollama_no_tools(
            prompt="hi", endpoint="http://localhost:11434/",
        )
        assert captured[0] == "http://localhost:11434/v1/chat/completions"

    async def test_finish_reason_length_mapped_to_stop_reason(
        self, monkeypatch,
    ) -> None:
        """``finish_reason: length`` (Ollama's max_tokens hit) maps to
        ``stop_reason: length``. Diagnostic intent matches Anthropic's
        ``stop_reason: max_tokens``."""
        def _dispatch(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=_ollama_response("partial", "length"),
            )

        _patch_httpx(monkeypatch, _dispatch)

        text, meta = await ollama_mod.call_ollama_no_tools(prompt="hi")
        assert text == "partial"
        assert meta["stop_reason"] == "length"


# --- call_ollama_no_tools — error paths ------------------------------------


class TestOllamaErrorPaths:
    async def test_http_503_raises_runtime_error(self, monkeypatch) -> None:
        """Per spike spec: NO silent fallback. HTTP 503 → RuntimeError
        with the response body included so the operator can see
        Ollama's error message."""
        def _dispatch(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                503,
                text='{"error":"model qwen2.5:72b not found, run `ollama pull qwen2.5:72b`"}',
            )

        _patch_httpx(monkeypatch, _dispatch)

        with structlog.testing.capture_logs() as captured_logs:
            with pytest.raises(RuntimeError) as exc_info:
                await ollama_mod.call_ollama_no_tools(prompt="hi")
        msg = str(exc_info.value)
        assert "503" in msg
        assert "model qwen2.5:72b not found" in msg
        # Operator-actionable: the message names ollama pull.
        assert "ollama pull" in msg

        # Log-emission contract — WARN 1 from the d8c224d review (4th
        # session-recurring instance of the log-emission test gap
        # pattern). The dispatcher MUST emit ``ollama.http_error``
        # with the status + body_preview fields so a future refactor
        # that drops the log line fails this test instead of silently
        # losing operator-visible signal.
        http_errors = [
            c for c in captured_logs
            if c.get("event") == "ollama.http_error"
        ]
        assert len(http_errors) == 1, (
            f"expected exactly one ollama.http_error event; "
            f"got {[c.get('event') for c in captured_logs]}"
        )
        ev = http_errors[0]
        assert ev["status"] == 503
        assert "model qwen2.5:72b not found" in ev["body_preview"]
        # Endpoint default reaches the log entry intact.
        assert ev["endpoint"] == "http://localhost:11434"

    async def test_connection_refused_raises_runtime_error(
        self, monkeypatch,
    ) -> None:
        """Network-layer failure (Ollama not running) → RuntimeError
        with the underlying httpx error class named in the message
        for grep-ability."""
        def _dispatch(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Connection refused")

        _patch_httpx(monkeypatch, _dispatch)

        with structlog.testing.capture_logs() as captured_logs:
            with pytest.raises(RuntimeError) as exc_info:
                await ollama_mod.call_ollama_no_tools(
                    prompt="hi", endpoint="http://localhost:11434",
                )
        msg = str(exc_info.value)
        assert "unreachable" in msg
        assert "ConnectError" in msg
        assert "http://localhost:11434/v1/chat/completions" in msg

        # Log-emission contract — WARN 1 from the d8c224d review.
        # ``ollama.request_failed`` MUST fire on httpx.RequestError so
        # the operator can grep for "Ollama unreachable" before seeing
        # the propagated RuntimeError in daemon logs.
        request_fails = [
            c for c in captured_logs
            if c.get("event") == "ollama.request_failed"
        ]
        assert len(request_fails) == 1, (
            f"expected exactly one ollama.request_failed event; "
            f"got {[c.get('event') for c in captured_logs]}"
        )
        ev = request_fails[0]
        assert ev["error_type"] == "ConnectError"
        assert ev["endpoint"] == "http://localhost:11434"
        # Underlying error message preserved for diagnostic grep.
        assert "Connection refused" in ev["error"]

    async def test_empty_choices_returns_empty_text(self, monkeypatch) -> None:
        """Defensive: malformed Ollama response with empty ``choices``
        list returns ``("", None)`` so the extractor's repair-retry
        path can handle it the same way as Anthropic empty responses."""
        def _dispatch(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": []})

        _patch_httpx(monkeypatch, _dispatch)

        with structlog.testing.capture_logs() as captured_logs:
            text, meta = await ollama_mod.call_ollama_no_tools(prompt="hi")
        assert text == ""
        assert meta == {"stop_reason": None}

        # Log-emission contract — WARN 1 from the d8c224d review.
        # Per ``feedback_intentionally_left_blank.md``, the empty case
        # MUST emit ``ollama.empty_response`` so a zero-text response
        # stays distinguishable from a missed dispatch in operator
        # log review. Pinned so a future refactor that drops the log
        # line fails this test instead of silently losing the signal.
        empty_events = [
            c for c in captured_logs
            if c.get("event") == "ollama.empty_response"
        ]
        assert len(empty_events) == 1, (
            f"expected exactly one ollama.empty_response event; "
            f"got {[c.get('event') for c in captured_logs]}"
        )
        ev = empty_events[0]
        # Per the source: stop_reason key uses the literal "unknown"
        # string when finish_reason was None (defensive default).
        assert ev["stop_reason"] == "unknown"

    async def test_missing_message_returns_empty_text(
        self, monkeypatch,
    ) -> None:
        """Defensive: response with choice but no ``message`` field."""
        def _dispatch(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"choices": [{"index": 0, "finish_reason": "stop"}]},
            )

        _patch_httpx(monkeypatch, _dispatch)

        text, meta = await ollama_mod.call_ollama_no_tools(prompt="hi")
        assert text == ""


# --- _extract_first_choice — defensive parser unit tests ------------------


class TestExtractFirstChoice:
    def test_well_formed_response(self) -> None:
        text, fr = ollama_mod._extract_first_choice(
            _ollama_response("hello", "stop"),
        )
        assert text == "hello"
        assert fr == "stop"

    def test_empty_dict_returns_empty(self) -> None:
        assert ollama_mod._extract_first_choice({}) == ("", None)

    def test_choices_not_a_list(self) -> None:
        assert ollama_mod._extract_first_choice(
            {"choices": "not a list"}
        ) == ("", None)

    def test_first_choice_not_a_dict(self) -> None:
        assert ollama_mod._extract_first_choice(
            {"choices": ["string-instead-of-dict"]}
        ) == ("", None)

    def test_message_content_not_a_string(self) -> None:
        """Some malformed responses have ``message.content`` as a list of
        content blocks (Anthropic-style); we don't unpack those, just
        coerce to empty so the repair-retry path handles it."""
        text, fr = ollama_mod._extract_first_choice(
            {"choices": [{"message": {"content": [{"type": "text"}]}, "finish_reason": "stop"}]}
        )
        assert text == ""
        assert fr == "stop"

    def test_finish_reason_not_a_string(self) -> None:
        text, fr = ollama_mod._extract_first_choice(
            {"choices": [{"message": {"content": "hi"}, "finish_reason": 42}]}
        )
        assert text == "hi"
        assert fr is None

    def test_non_dict_input_returns_empty(self) -> None:
        """The contract takes a dict; defensive check handles a non-dict
        slipping through (e.g. JSON parsed into a list at the top level)."""
        assert ollama_mod._extract_first_choice("not a dict") == ("", None)  # type: ignore[arg-type]


# --- Dispatcher: _call_extraction_llm --------------------------------------


class TestExtractionDispatcher:
    async def test_default_backend_is_anthropic(self, monkeypatch) -> None:
        """``backend="anthropic"`` (default) routes to the Anthropic
        backend with config.anthropic.* values."""
        captured: dict = {}

        async def _fake_anthropic(*, prompt, system, model, max_tokens, api_key):
            captured["prompt"] = prompt
            captured["system"] = system
            captured["model"] = model
            captured["max_tokens"] = max_tokens
            captured["api_key"] = api_key
            return ("anthropic-text", {"stop_reason": "end_turn"})

        monkeypatch.setattr(extractor_mod, "call_anthropic_no_tools", _fake_anthropic)

        cfg = _config(backend="anthropic", anthropic_model="claude-opus-4-7")
        text, meta = await extractor_mod._call_extraction_llm(
            prompt="hello", system="be brief", config=cfg,
        )
        assert text == "anthropic-text"
        assert meta == {"stop_reason": "end_turn"}
        # Anthropic-specific kwargs threaded through.
        assert captured["model"] == "claude-opus-4-7"
        assert captured["max_tokens"] == 4096
        assert captured["api_key"] == "DUMMY_ANTHROPIC_TEST_KEY"

    async def test_ollama_backend_routes_to_ollama(self, monkeypatch) -> None:
        """``backend="ollama"`` routes to the Ollama backend with
        config.extraction.ollama_* values."""
        captured: dict = {}

        async def _fake_ollama(*, prompt, system, model, max_tokens, endpoint):
            captured["prompt"] = prompt
            captured["system"] = system
            captured["model"] = model
            captured["max_tokens"] = max_tokens
            captured["endpoint"] = endpoint
            return ("ollama-text", {"stop_reason": "stop"})

        monkeypatch.setattr(extractor_mod, "call_ollama_no_tools", _fake_ollama)

        cfg = _config(
            backend="ollama",
            ollama_endpoint="http://example.invalid:11434",
            ollama_model="qwen2.5:14b-instruct",
        )
        text, meta = await extractor_mod._call_extraction_llm(
            prompt="hello", system="be brief", config=cfg,
        )
        assert text == "ollama-text"
        assert meta == {"stop_reason": "stop"}
        # Ollama-specific kwargs threaded through.
        assert captured["model"] == "qwen2.5:14b-instruct"
        assert captured["endpoint"] == "http://example.invalid:11434"
        # max_tokens is shared with the Anthropic config (the spike
        # doesn't introduce a separate ollama_max_tokens knob).
        assert captured["max_tokens"] == 4096

    async def test_unknown_backend_raises_runtime_error(self) -> None:
        """Per spike spec: NO silent fallback. Typo'd backend → loud
        RuntimeError naming the valid choices so the operator sees it
        immediately."""
        cfg = _config(backend="rogue")
        with pytest.raises(RuntimeError) as exc_info:
            await extractor_mod._call_extraction_llm(
                prompt="x", system="y", config=cfg,
            )
        msg = str(exc_info.value)
        assert "rogue" in msg
        assert "anthropic" in msg
        assert "ollama" in msg

    async def test_backend_value_case_insensitive(self, monkeypatch) -> None:
        """Operator who writes ``backend: OLLAMA`` (uppercase, common
        config typo) gets the same routing as lowercase ``ollama``.
        Defensive normalization in the dispatcher."""
        called = []

        async def _fake_ollama(**kwargs):
            called.append(kwargs)
            return ("ok", {"stop_reason": "stop"})

        monkeypatch.setattr(extractor_mod, "call_ollama_no_tools", _fake_ollama)

        cfg = _config(backend="OLLAMA")
        await extractor_mod._call_extraction_llm(
            prompt="x", system="y", config=cfg,
        )
        assert len(called) == 1

    async def test_empty_backend_string_falls_back_to_anthropic(
        self, monkeypatch,
    ) -> None:
        """Defensive: a YAML config with ``backend: ""`` (operator
        cleared the value) defaults to anthropic per the helper's
        ``or "anthropic"`` fallback. NOT a silent-routing failure —
        the empty value is unambiguously "no backend chosen,
        use the default."
        """
        called = []

        async def _fake_anthropic(**kwargs):
            called.append(kwargs)
            return ("ok", {"stop_reason": "end_turn"})

        monkeypatch.setattr(extractor_mod, "call_anthropic_no_tools", _fake_anthropic)

        cfg = _config(backend="")
        await extractor_mod._call_extraction_llm(
            prompt="x", system="y", config=cfg,
        )
        assert len(called) == 1
