"""Unit tests for alfred.health.tool_schema_validator.

Mirrors ``tests/health/test_anthropic_auth.py`` shape: monkeypatch
``anthropic.Anthropic`` to return a fake client whose
``messages.count_tokens`` we control. Tests cover:

  * No api_key → fatal_error + empty results
  * SDK not installed → fatal_error
  * Constructor raises → fatal_error
  * count_tokens missing on messages → fatal_error (SDK too old)
  * Empty tool list → empty results, no fatal_error (per
    feedback_intentionally_left_blank.md)
  * All tools accepted → all_accepted=True; results have accepted=True
  * One tool rejected → that tool has accepted=False + error_text;
    other tools accepted; all_accepted=False
  * Per-tool isolation: count_tokens called once per tool, each call
    receives a single-tool ``tools=[...]`` list
  * Error-text extraction: prefer body["error"]["message"]; fall back
    to str(exc); never empty
  * ``ValidationReport.to_dict`` round-trips
  * ``ToolValidationResult.to_dict`` round-trips
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from alfred.health import tool_schema_validator as mod


# --- Fake Anthropic client + helpers ---------------------------------------


class _FakeMessages:
    """Stand-in for client.messages with pluggable count_tokens."""

    def __init__(
        self,
        count_tokens=None,  # noqa: ANN001
        absent: bool = False,
    ):
        # When ``absent`` is True, do NOT set count_tokens at all so
        # ``getattr(client.messages, "count_tokens", None)`` returns None.
        if not absent and count_tokens is not None:
            self.count_tokens = count_tokens


class _FakeClient:
    def __init__(self, messages: _FakeMessages):
        self.messages = messages


def _install_fake_anthropic(monkeypatch, fake_client, raises=None):  # noqa: ANN001
    """Patch ``anthropic.Anthropic`` to return ``fake_client``.

    Mirrors ``test_anthropic_auth._install_fake_anthropic``: injects a
    minimal fake module into ``sys.modules`` so the lazy
    ``import anthropic`` inside ``validate_tool_schemas`` sees our
    shim.
    """
    def _ctor(*args, **kwargs):  # noqa: ANN001
        if raises is not None:
            raise raises
        return fake_client

    fake_mod = SimpleNamespace(Anthropic=_ctor)
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)


def _make_anthropic_bad_request_error(message: str, *, body: dict | None = None):
    """Build a fake exception that mimics
    ``anthropic.BadRequestError``'s shape: carries an optional
    ``body`` dict so ``_extract_error_text`` can read the nested
    ``error.message``."""
    exc = RuntimeError(message)
    if body is not None:
        exc.body = body  # type: ignore[attr-defined]
    return exc


def _tool(name: str) -> dict[str, Any]:
    """Minimal tool schema — enough for the validator to identify it
    by ``name``. The actual input_schema content doesn't matter
    because the FakeMessages stub doesn't enforce it; the test
    asserts on what gets PASSED to count_tokens, not what
    count_tokens does with it."""
    return {
        "name": name,
        "description": f"test tool {name}",
        "input_schema": {"type": "object", "properties": {}},
    }


# --- Fatal-error paths -----------------------------------------------------


class TestFatalErrors:
    async def test_no_api_key_sets_fatal_error(self) -> None:
        report = await mod.validate_tool_schemas(
            api_key="",
            model="claude-haiku-4-5",
            tools=[_tool("vault_read")],
        )
        assert report.fatal_error == "no api_key configured"
        assert report.all_accepted is False
        assert report.results == []

    async def test_sdk_import_failure_sets_fatal_error(
        self, monkeypatch,
    ) -> None:
        # Force ``import anthropic`` to raise ImportError.
        monkeypatch.setitem(sys.modules, "anthropic", None)
        report = await mod.validate_tool_schemas(
            api_key="test-anthropic-key",
            model="claude-haiku-4-5",
            tools=[_tool("vault_read")],
        )
        assert "not installed" in report.fatal_error
        assert report.all_accepted is False

    async def test_constructor_failure_sets_fatal_error(
        self, monkeypatch,
    ) -> None:
        _install_fake_anthropic(
            monkeypatch,
            fake_client=None,
            raises=ValueError("bad key format"),
        )
        report = await mod.validate_tool_schemas(
            api_key="garbage-anthropic-key",
            model="claude-haiku-4-5",
            tools=[_tool("vault_read")],
        )
        assert "client init failed" in report.fatal_error
        assert "bad key format" in report.fatal_error

    async def test_count_tokens_missing_on_sdk_sets_fatal_error(
        self, monkeypatch,
    ) -> None:
        """Older SDK without count_tokens — fatal because we can't
        probe without burning tokens."""
        client = _FakeClient(_FakeMessages(absent=True))
        _install_fake_anthropic(monkeypatch, client)
        report = await mod.validate_tool_schemas(
            api_key="test-anthropic-key",
            model="claude-haiku-4-5",
            tools=[_tool("vault_read")],
        )
        assert "count_tokens" in report.fatal_error
        assert report.all_accepted is False


# --- Empty tool list ------------------------------------------------------


class TestEmptyToolList:
    async def test_empty_tools_returns_no_fatal_no_results(
        self, monkeypatch,
    ) -> None:
        """Per feedback_intentionally_left_blank.md: empty tools list
        is a legitimate "this instance has no tools" signal, NOT a
        validation failure. Operator can distinguish via
        ``report.results == []``."""
        client = _FakeClient(_FakeMessages(count_tokens=lambda **kw: None))
        _install_fake_anthropic(monkeypatch, client)
        report = await mod.validate_tool_schemas(
            api_key="test-anthropic-key",
            model="claude-haiku-4-5",
            tools=[],
        )
        assert report.fatal_error == ""
        assert report.results == []
        # all_accepted is True trivially when there's nothing to fail.
        assert report.all_accepted is True


# --- Happy path ------------------------------------------------------------


class TestAllAccepted:
    async def test_all_tools_accepted(self, monkeypatch) -> None:
        calls: list[dict] = []

        def _count_tokens(*, model, messages, tools):
            calls.append({"model": model, "tools": list(tools)})
            return {"input_tokens": 5}

        client = _FakeClient(_FakeMessages(count_tokens=_count_tokens))
        _install_fake_anthropic(monkeypatch, client)
        report = await mod.validate_tool_schemas(
            api_key="test-anthropic-key",
            model="claude-haiku-4-5",
            tools=[_tool("vault_search"), _tool("vault_read"), _tool("vault_edit")],
            instance_name="Salem",
            tool_set="talker",
        )
        assert report.fatal_error == ""
        assert report.all_accepted is True
        assert report.rejected_count == 0
        assert len(report.results) == 3
        assert all(r.accepted for r in report.results)
        # Per-tool isolation: count_tokens called THREE times, each
        # with a single-tool list.
        assert len(calls) == 3
        assert all(len(c["tools"]) == 1 for c in calls)
        # Tool order preserved.
        assert [c["tools"][0]["name"] for c in calls] == [
            "vault_search", "vault_read", "vault_edit",
        ]


# --- Rejection path --------------------------------------------------------


class TestRejection:
    async def test_one_tool_rejected_others_accepted(
        self, monkeypatch,
    ) -> None:
        """Mixed batch: 3 tools, 1 invalid → 2 accepted + 1 rejected
        with the exact Anthropic error text."""
        bad_msg = (
            "tools.0.custom.input_schema: input_schema does not "
            "support oneOf, allOf, or anyOf at the top level"
        )

        def _count_tokens(*, model, messages, tools):
            tool = tools[0]
            if tool["name"] == "vault_edit":
                # Mimic Anthropic's BadRequestError shape: ``body``
                # carries the structured error.
                raise _make_anthropic_bad_request_error(
                    f"Error code: 400 - {bad_msg}",
                    body={
                        "type": "error",
                        "error": {"type": "invalid_request_error", "message": bad_msg},
                    },
                )
            return {"input_tokens": 5}

        client = _FakeClient(_FakeMessages(count_tokens=_count_tokens))
        _install_fake_anthropic(monkeypatch, client)
        report = await mod.validate_tool_schemas(
            api_key="test-anthropic-key",
            model="claude-haiku-4-5",
            tools=[
                _tool("vault_search"),
                _tool("vault_edit"),
                _tool("vault_read"),
            ],
            instance_name="Salem",
            tool_set="talker",
        )
        assert report.fatal_error == ""
        assert report.all_accepted is False
        assert report.rejected_count == 1
        # Per-tool granularity preserved — vault_edit is the only one
        # rejected; it carries the exact validator message.
        accepted_names = [r.tool_name for r in report.results if r.accepted]
        rejected_names = [r.tool_name for r in report.results if not r.accepted]
        assert accepted_names == ["vault_search", "vault_read"]
        assert rejected_names == ["vault_edit"]
        # Error text extracted from ``body.error.message`` — preferred
        # over ``str(exc)`` because it's the canonical structured form.
        rejected = next(r for r in report.results if not r.accepted)
        assert "input_schema does not support oneOf" in rejected.error_text


# --- Error-text extraction --------------------------------------------------


class TestErrorTextExtraction:
    def test_extract_prefers_body_error_message(self) -> None:
        exc = _make_anthropic_bad_request_error(
            "Error code: 400 - long error string",
            body={"error": {"message": "the canonical message"}},
        )
        text = mod._extract_error_text(exc)
        assert text == "the canonical message"

    def test_extract_falls_back_to_str_when_no_body(self) -> None:
        exc = RuntimeError("plain runtime error")
        text = mod._extract_error_text(exc)
        # Includes class name for grep-ability.
        assert "RuntimeError" in text
        assert "plain runtime error" in text

    def test_extract_falls_back_when_body_not_dict(self) -> None:
        """Defensive: some SDK exceptions carry ``body`` as a non-dict
        (e.g. raw bytes); extractor must not crash."""
        exc = RuntimeError("boom")
        exc.body = "not a dict"  # type: ignore[attr-defined]
        text = mod._extract_error_text(exc)
        assert "RuntimeError" in text
        assert "boom" in text

    def test_extract_falls_back_when_message_not_str(self) -> None:
        """Defensive: malformed body where error.message is non-string."""
        exc = RuntimeError("boom")
        exc.body = {"error": {"message": None}}  # type: ignore[attr-defined]
        text = mod._extract_error_text(exc)
        # Falls back to str(exc) because message wasn't a usable string.
        assert "boom" in text

    def test_extract_never_returns_empty_string(self) -> None:
        """Even on a totally bare exception, the extractor returns
        SOMETHING the operator can grep."""
        exc = Exception()
        text = mod._extract_error_text(exc)
        assert text  # non-empty
        assert "Exception" in text


# --- to_dict shape --------------------------------------------------------


class TestDataclassRoundTrip:
    def test_tool_validation_result_to_dict(self) -> None:
        result = mod.ToolValidationResult(
            tool_name="vault_edit",
            accepted=False,
            error_text="schema rejection",
            latency_ms=42.7,
        )
        out = result.to_dict()
        assert out == {
            "tool_name": "vault_edit",
            "accepted": False,
            "error_text": "schema rejection",
            "latency_ms": 42.7,
        }

    def test_validation_report_to_dict(self) -> None:
        report = mod.ValidationReport(
            instance_name="Salem",
            tool_set="talker",
            model="claude-haiku-4-5",
            results=[
                mod.ToolValidationResult(
                    tool_name="vault_read", accepted=True, latency_ms=10.0,
                ),
                mod.ToolValidationResult(
                    tool_name="vault_edit",
                    accepted=False,
                    error_text="oneOf rejected",
                    latency_ms=12.0,
                ),
            ],
        )
        out = report.to_dict()
        assert out["instance_name"] == "Salem"
        assert out["tool_set"] == "talker"
        assert out["model"] == "claude-haiku-4-5"
        assert out["all_accepted"] is False
        assert out["rejected_count"] == 1
        assert out["fatal_error"] == ""
        assert len(out["results"]) == 2
        assert out["results"][0]["tool_name"] == "vault_read"
        assert out["results"][1]["accepted"] is False
