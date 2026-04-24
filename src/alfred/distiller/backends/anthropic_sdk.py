"""Direct Anthropic SDK backend for the non-agentic distiller rebuild.

OpenClaw was confirmed 2026-04-24 to have no tool-less mode (Explore
agent check — no flag, no API option). The rebuild's thesis requires
the LLM to return plain text that Python then Pydantic-validates, so
we bypass OpenClaw entirely and call the Anthropic Messages API direct.

Pattern borrowed from ``src/alfred/instructor/executor.py`` — lazy-load
``AsyncAnthropic``, structured-log failures, let the caller own retry
policy. Key differences:

- **No ``tools=`` parameter.** That's the whole point of tool-less mode.
- **Return type is a single string**, not a list of tool_use blocks.
- **No BaseBackend registration ceremony.** This is a direct-call
  helper that ``extractor.py`` imports; the existing
  ``backends/__init__.py::BaseBackend`` contract is designed around
  agent-mode subprocess backends (CLI / HTTP / OpenClaw) and doesn't
  fit a structured-output path.

Config lives on ``DistillerConfig.anthropic`` — see
``src/alfred/distiller/config.py::AnthropicConfig``.
"""

from __future__ import annotations

import os

import anthropic
from anthropic import AsyncAnthropic

from ..utils import get_logger

log = get_logger(__name__)


async def call_anthropic_no_tools(
    prompt: str,
    system: str | None = None,
    model: str = "claude-opus-4-7",
    max_tokens: int = 4096,
    api_key: str | None = None,
) -> str:
    """Call the Anthropic Messages API without any tools.

    Returns the raw text of the first ``text`` block in the response.
    Callers (specifically ``extractor.py``) are expected to
    ``model_validate_json`` that text against ``ExtractionResult``.

    ``api_key`` falls back to the ``ANTHROPIC_API_KEY`` environment
    variable if not given — mirrors the SDK default so unit-test
    fixtures and production both use the same resolution path.

    Errors:
      - ``anthropic.APIError`` propagates to the caller. The rebuild's
        retry policy lives in ``extractor.py`` (one repair retry on
        Pydantic ValidationError), not here; we don't wrap network
        failures because they're a different failure class.
      - Empty / non-text responses return ``""`` and log
        ``anthropic_sdk.empty_response``. The caller's Pydantic
        validation will fail on empty string, which then triggers the
        repair-retry path — the right loop for handling garbage.
    """
    resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not resolved_key:
        # Fail loudly — the AsyncAnthropic constructor would also
        # raise, but our error is more actionable.
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set and api_key= not provided. "
            "Configure distiller.anthropic.api_key in config.yaml or "
            "export ANTHROPIC_API_KEY."
        )

    client = AsyncAnthropic(api_key=resolved_key)

    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system is not None:
        kwargs["system"] = system

    try:
        response = await client.messages.create(**kwargs)
    except anthropic.APIError as exc:
        # Structured-log and re-raise; the extractor's retry logic
        # and the daemon's catch-all are the right places to decide
        # what to do next.
        log.warning(
            "anthropic_sdk.api_error",
            model=model,
            error=str(exc),
        )
        raise

    # Extract the first text block. In tool-less mode the response is
    # typically a single ``text`` block; take the first one defensively.
    text = _first_text_block(response.content)
    if not text:
        log.info(
            "anthropic_sdk.empty_response",
            model=model,
            stop_reason=getattr(response, "stop_reason", "unknown"),
        )
    return text


def _first_text_block(content) -> str:
    """Return the text of the first ``text`` block, or ``""`` if none."""
    if not content:
        return ""
    for block in content:
        if getattr(block, "type", None) == "text":
            return getattr(block, "text", "") or ""
    return ""
