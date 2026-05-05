"""Pre-deploy Anthropic tool-schema validator.

Closes the bug class surfaced 2026-05-05 by the ``oneOf``-at-top-level
P0 (commit ``0d7e7a6`` shipped May 4, surfaced 16:49 UTC May 5 = ~36hr
silent gap). The schema passed every local test but Anthropic's
server-side request validator rejected with HTTP 400 on first real
conversation. Reviewer's pattern flag: *"Code that compiles + ships
clean tests, fails only when external API is actually called."*

This module exposes a probe that hits Anthropic's request validator
WITHOUT invoking the model — using ``client.messages.count_tokens``
(zero cost) rather than ``client.messages.create`` (one token per
probe). The validator runs the SAME ``tools.*.input_schema`` checks
that ``messages.create`` does, so any structural rejection
(``oneOf`` / ``allOf`` / ``anyOf`` at top level, missing required
fields, malformed enums, etc.) surfaces here pre-deploy.

Per-tool isolation: probe each tool individually rather than
batching. Anthropic's error message names the failed tool by INDEX
(``tools.3.custom.input_schema: ...``), which forces the operator to
manually map the index back to the tool name. Probing per-tool means
the operator gets the tool name directly + the validator's exact
error text in one line.

Costs: zero on schema-invalid (4xx before model runs); zero on
schema-valid (count_tokens doesn't burn tokens). Total: free,
regardless of outcome.

Public surface:
  * :class:`ToolValidationResult` — per-tool outcome dataclass
  * :class:`ValidationReport` — aggregate report
  * :func:`validate_tool_schemas` — async entry point
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


# Probe payload — minimal user message so count_tokens has something
# to count. The actual content is irrelevant; the validator runs
# regardless of message body. Match the existing ``health/anthropic_auth.py``
# pattern.
_PROBE_MESSAGES: list[dict[str, Any]] = [{"role": "user", "content": "ping"}]


@dataclass
class ToolValidationResult:
    """Per-tool validation outcome."""

    tool_name: str
    accepted: bool
    error_text: str = ""  # Anthropic's exact error message on rejection
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "accepted": self.accepted,
            "error_text": self.error_text,
            "latency_ms": round(self.latency_ms, 1),
        }


@dataclass
class ValidationReport:
    """Aggregate report from one ``validate_tool_schemas`` call."""

    instance_name: str
    tool_set: str
    model: str
    results: list[ToolValidationResult] = field(default_factory=list)
    fatal_error: str = ""  # set when the probe couldn't run at all
    # (network failure, missing api_key, missing SDK)

    @property
    def all_accepted(self) -> bool:
        """True only when every tool was accepted AND no fatal error."""
        return not self.fatal_error and all(r.accepted for r in self.results)

    @property
    def rejected_count(self) -> int:
        return sum(1 for r in self.results if not r.accepted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_name": self.instance_name,
            "tool_set": self.tool_set,
            "model": self.model,
            "fatal_error": self.fatal_error,
            "all_accepted": self.all_accepted,
            "rejected_count": self.rejected_count,
            "results": [r.to_dict() for r in self.results],
        }


async def validate_tool_schemas(
    *,
    api_key: str,
    model: str,
    tools: list[dict[str, Any]],
    instance_name: str = "",
    tool_set: str = "",
) -> ValidationReport:
    """Validate each tool schema against Anthropic's request validator.

    Sends one ``count_tokens`` probe per tool (with the tool list
    containing only that tool). Each probe hits the server-side
    ``tools.*.input_schema`` validator. Schema-invalid tools surface
    as ``accepted=False`` with the exact Anthropic error text;
    schema-valid tools surface as ``accepted=True``.

    Per ``feedback_intentionally_left_blank.md``: empty tools list →
    report with empty ``results`` + no fatal_error (operator's
    explicit "this instance has no tools" signal, not a validation
    failure).

    Args:
        api_key: Anthropic API key.
        model: Model id to reference in count_tokens (the cheapest
            haiku-class id is fine — request validation runs
            independent of the chosen model). Pass the actual
            production model so the validation matches what
            ``messages.create`` would do.
        tools: The tool list to validate (typically the output of
            ``conversation.tools_for_set(...)``).
        instance_name: Human-readable instance name for the report.
        tool_set: Tool-set identifier (talker / kalle / hypatia)
            for the report.

    Returns:
        :class:`ValidationReport` with per-tool results + aggregate
        ``all_accepted`` flag.
    """
    report = ValidationReport(
        instance_name=instance_name,
        tool_set=tool_set,
        model=model,
    )

    if not api_key:
        report.fatal_error = "no api_key configured"
        return report

    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:
        report.fatal_error = "anthropic SDK not installed"
        return report

    try:
        client = anthropic.Anthropic(api_key=api_key)
    except Exception as exc:  # noqa: BLE001
        report.fatal_error = (
            f"client init failed: {exc.__class__.__name__}: {exc}"
        )
        return report

    count_fn = getattr(client.messages, "count_tokens", None)
    if count_fn is None:
        report.fatal_error = (
            "anthropic SDK does not expose messages.count_tokens "
            "(requires anthropic >= 0.50); upgrade or fall back to "
            "messages.create probing"
        )
        return report

    if not tools:
        # Empty tool list → "ran, nothing to do" per
        # feedback_intentionally_left_blank.md. No fatal error;
        # caller can distinguish via report.results == [].
        return report

    # Per-tool isolation: one probe per tool so the operator sees
    # tool NAMES in errors, not Anthropic's "tools.N" index.
    for tool in tools:
        tool_name = (
            tool.get("name", "") if isinstance(tool, dict) else ""
        ) or "(unnamed)"
        result = await _validate_one_tool(count_fn, model, tool, tool_name)
        report.results.append(result)

    return report


async def _validate_one_tool(
    count_fn,  # noqa: ANN001 — anthropic SDK type
    model: str,
    tool: dict[str, Any],
    tool_name: str,
) -> ToolValidationResult:
    """Run one count_tokens probe with a single-tool tool list."""
    started = time.monotonic()
    try:
        await asyncio.to_thread(
            count_fn,
            model=model,
            messages=_PROBE_MESSAGES,
            tools=[tool],
        )
    except Exception as exc:  # noqa: BLE001
        latency = (time.monotonic() - started) * 1000.0
        # Extract the human-readable message. Anthropic's BadRequestError
        # carries the validator detail in ``str(exc)`` directly; other
        # exception classes may have ``message`` attribute. Prefer the
        # most-detailed string available.
        error_text = _extract_error_text(exc)
        return ToolValidationResult(
            tool_name=tool_name,
            accepted=False,
            error_text=error_text,
            latency_ms=latency,
        )
    latency = (time.monotonic() - started) * 1000.0
    return ToolValidationResult(
        tool_name=tool_name,
        accepted=True,
        latency_ms=latency,
    )


def _extract_error_text(exc: BaseException) -> str:
    """Best-effort error-text extraction from an Anthropic exception.

    Anthropic's ``BadRequestError`` carries the structured error in
    ``exc.body["error"]["message"]`` AND in ``str(exc)``; other SDK
    exceptions vary. Prefer the deepest-known nested form for accuracy
    + fall back to ``str(exc)`` so we never return an empty string.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str) and msg:
                return msg
    # Fallback: stringify. Includes the exception class for a future
    # operator who needs to grep for the type.
    return f"{exc.__class__.__name__}: {exc}"


__all__ = [
    "ToolValidationResult",
    "ValidationReport",
    "validate_tool_schemas",
]
