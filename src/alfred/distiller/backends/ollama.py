"""Ollama backend for the non-agentic distiller v2 extractor.

Path C Phase 1 spike (2026-05-06) — sibling to ``anthropic_sdk.py``
with the SAME ``(text, metadata)`` return contract so
``extractor.py``'s dispatcher can call either backend interchangeably.

Ollama exposes an OpenAI-compatible ``/v1/chat/completions`` endpoint
on top of its native API. We hit it directly via httpx (no SDK
dependency) for a few reasons:

  * The spike's surface area is single-call; the OpenAI SDK's
    auth / retry / streaming machinery would be dead weight.
  * Local Ollama needs no auth — direct httpx makes that obvious
    in the call site (no ``api_key`` field to leave empty).
  * httpx is already a dep (``pyproject.toml:14``).
  * Future: if a second spike consumer surfaces, lifting the call
    into a shared OpenAI-compatible-via-httpx helper costs nothing.
    Premature genericization avoided per
    ``feedback_intentionally_left_blank.md``-style discipline.

OpenAI response shape mapped back to Anthropic-style metadata so
``extractor.py``'s ``meta.get("stop_reason")`` works unchanged:

  * ``choices[0].message.content`` → returned text
  * ``choices[0].finish_reason`` → ``meta["stop_reason"]``
    (Ollama emits ``"stop"`` / ``"length"`` / ``"tool_calls"``;
    Anthropic emits ``"end_turn"`` / ``"max_tokens"`` / ``"tool_use"``.
    The names differ but the diagnostic intent is identical — both
    answer "did the model finish naturally or hit a cap?")

Per the spike spec: NO silent fallback to Anthropic. If Ollama is
unreachable, raise so the operator sees the failure immediately.
"""

from __future__ import annotations

import httpx

from ..utils import get_logger

log = get_logger(__name__)


# Per-call timeout. Local Ollama on a 72B q4_K_M model on a Framework
# Desktop (the spike's target hardware) takes 30-90s for a 4K-token
# extraction. 600s matches the existing legacy-distiller backend
# timeout (``ClaudeBackendConfig.timeout``); generous enough to cover
# cold-start model load + multi-turn extraction.
_OLLAMA_REQUEST_TIMEOUT_SECONDS = 600.0


async def call_ollama_no_tools(
    prompt: str,
    system: str | None = None,
    model: str = "qwen2.5:72b-instruct-q4_K_M",
    # Bumped 2026-05-31 from 4096 → 16384 to mirror the Anthropic +
    # Together backends after the Path C Phase 1.5 truncation bug
    # surfaced. See ``AnthropicConfig.max_tokens`` in config.py for
    # the full rationale. Local Ollama doesn't have per-call billing,
    # so the higher cap has no spend impact (only memory / latency
    # under heavy load).
    max_tokens: int = 16384,
    endpoint: str = "http://localhost:11434",
    options: dict | None = None,
) -> tuple[str, dict]:
    """Call Ollama's chat-completions endpoint.

    Two transport paths, selected by ``options``:

      * ``options is None`` (the distiller's use, UNCHANGED): the
        OpenAI-compatible ``/v1/chat/completions`` endpoint with
        ``max_tokens``. Byte-for-byte the historical path.
      * ``options`` provided (a dict of Ollama runtime options, e.g.
        ``{"num_ctx": 8192, "temperature": 0}``): the NATIVE ``/api/chat``
        endpoint with an ``options`` block. The OpenAI-compat endpoint does
        NOT honor ``num_ctx`` (it silently defaults to 2048 → context
        truncation on long inputs); ``/api/chat`` DOES. Used by the sovereign
        scribe note-gen (#46, live-box A/B) to force ``num_ctx >= 8192`` +
        ``temperature = 0`` for a faithfulness-critical clinical task.

    Mirrors :func:`call_anthropic_no_tools`'s signature so the
    extractor's dispatcher can call either backend without
    per-backend conditionals at the call site.

    Args:
        prompt: User-turn content.
        system: Optional system-prompt content. ``None`` skips the
            system message entirely (mirrors the Anthropic backend's
            optional ``system`` kwarg).
        model: Ollama model tag (e.g. ``"qwen2.5:72b-instruct-q4_K_M"``).
            Must already be pulled on the target Ollama instance —
            this function does NOT trigger model downloads.
        max_tokens: Cap on response tokens. Maps to OpenAI's
            ``max_tokens`` field which Ollama honors as a soft cap
            on ``num_predict``.
        endpoint: Ollama server base URL. Default matches the
            standard local install (``http://localhost:11434``);
            spike configs override to point at a different host.

    Returns:
        ``(text, metadata)`` — same shape as
        :func:`call_anthropic_no_tools`. ``metadata["stop_reason"]``
        is Ollama's ``finish_reason`` (``"stop"`` / ``"length"`` /
        ``"tool_calls"``); the diagnostic intent matches
        Anthropic's ``stop_reason``.

    Raises:
        ``RuntimeError`` if the Ollama endpoint is unreachable or
        returns a non-2xx status. Per the spike spec, NO silent
        fallback to another backend — the operator must see the
        failure to act on it.
    """
    messages: list[dict] = []
    if system is not None:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    if options is not None:
        # NATIVE /api/chat — honors options.num_ctx (the OpenAI-compat path
        # does not). ``stream: False`` for the same full-response-parse reason.
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": dict(options),
        }
        url = f"{endpoint.rstrip('/')}/api/chat"
    else:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            # ``stream: False`` because the extractor consumes the full
            # response before validating against Pydantic. Streaming
            # would complicate the parse path for zero spike benefit.
            "stream": False,
        }
        url = f"{endpoint.rstrip('/')}/v1/chat/completions"

    try:
        async with httpx.AsyncClient(timeout=_OLLAMA_REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload)
    except httpx.RequestError as exc:
        # Network-layer failure (DNS, connection refused, timeout) —
        # surface immediately so the operator sees Ollama is down.
        # Daemon's per-batch try/except catches the propagated
        # RuntimeError and logs ``extractor.batch_failed``.
        log.warning(
            "ollama.request_failed",
            endpoint=endpoint,
            model=model,
            error_type=exc.__class__.__name__,
            error=str(exc),
        )
        raise RuntimeError(
            f"Ollama unreachable at {url}: "
            f"{exc.__class__.__name__}: {exc}"
        ) from exc

    if response.status_code != 200:
        # Application-layer failure (model not pulled, OOM, malformed
        # request). Include response body in the diagnostic so the
        # operator can see Ollama's own error message.
        body_preview = response.text[:500]
        log.warning(
            "ollama.http_error",
            endpoint=endpoint,
            model=model,
            status=response.status_code,
            body_preview=body_preview,
        )
        raise RuntimeError(
            f"Ollama returned HTTP {response.status_code} from {url}: "
            f"{body_preview}"
        )

    data = response.json()
    if options is not None:
        text, finish_reason = _extract_native_chat(data)
    else:
        text, finish_reason = _extract_first_choice(data)
    metadata: dict = {"stop_reason": finish_reason}

    if not text:
        # Per ``feedback_intentionally_left_blank.md``: explicit
        # "ran, nothing to do" log. Empty response is unusual but
        # not necessarily a failure — the extractor's repair-retry
        # path will handle it the same way it handles Anthropic
        # empty responses.
        log.info(
            "ollama.empty_response",
            model=model,
            stop_reason=finish_reason or "unknown",
        )

    return text, metadata


def _extract_first_choice(data: dict) -> tuple[str, str | None]:
    """Pull ``(content, finish_reason)`` from an OpenAI-shaped response.

    Defensive: any structural deviation from
    ``{"choices": [{"message": {"content": str}, "finish_reason": str}]}``
    returns ``("", None)`` rather than raising. The extractor's
    Pydantic validation will fail on empty string and trigger the
    repair-retry path — same shape as the Anthropic backend's
    handling of malformed responses.
    """
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        return "", None
    first = choices[0]
    if not isinstance(first, dict):
        return "", None
    message = first.get("message")
    if not isinstance(message, dict):
        return "", None
    content = message.get("content")
    finish_reason = first.get("finish_reason")
    if not isinstance(content, str):
        content = ""
    if finish_reason is not None and not isinstance(finish_reason, str):
        finish_reason = None
    return content, finish_reason


def _extract_native_chat(data: dict) -> tuple[str, str | None]:
    """Pull ``(content, done_reason)`` from Ollama's NATIVE ``/api/chat``
    response shape ``{"message": {"content": str}, "done_reason": str}``.

    Defensive (mirrors :func:`_extract_first_choice`): any structural deviation
    returns ``("", None)`` rather than raising, so the caller's empty-string
    handling / repair-retry path fires the same way.
    """
    if not isinstance(data, dict):
        return "", None
    message = data.get("message")
    if not isinstance(message, dict):
        return "", None
    content = message.get("content")
    done_reason = data.get("done_reason")
    if not isinstance(content, str):
        content = ""
    if done_reason is not None and not isinstance(done_reason, str):
        done_reason = None
    return content, done_reason


__all__ = [
    "call_ollama_no_tools",
]
