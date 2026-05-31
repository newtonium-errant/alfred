"""Together.ai backend for the non-agentic distiller v2 extractor.

Path C Phase 1.5 spike (2026-05-31) — sibling to ``ollama.py`` and
``anthropic_sdk.py`` with the SAME ``(text, metadata)`` return contract
so ``extractor.py``'s dispatcher can call any of the three backends
interchangeably.

**Why Together for Phase 1.5.** Phase 1 (2026-05-07) showed that local
hardware (RTX 5070 Ti 16GB) running qwen2.5:72b at Q4_K_M was
insufficient — the model fit but inference was too slow and quality
suffered from the aggressive quantization. Phase 1.5 answers a
narrower question: *is qwen2.5-72b itself capable when not
memory-starved?* Together.ai hosts qwen2.5-72b on managed cloud GPUs
at higher precision (typically FP8 or bf16) with sub-second latency.
A clean baseline from Together separates "model is wrong for the
task" from "local hardware is wrong for the model" — two very
different conclusions for the Path C decision.

Together exposes an OpenAI-compatible ``/v1/chat/completions``
endpoint, same shape as Ollama, so this module is a near-copy of
``ollama.py`` with three deltas:

  * Hard-coded endpoint (``https://api.together.xyz``) — Together is
    a single managed service, no per-deployment URL variation worth
    parameterizing for the spike.
  * Bearer-token auth via ``Authorization`` header — required, unlike
    Ollama which is unauthenticated on localhost. Empty/missing
    api_key fails-loud at the dispatcher gate; this module also
    fails-loud on empty as a defense-in-depth check.
  * Default model is Together's Turbo variant
    (``Qwen/Qwen2.5-72B-Instruct-Turbo``) — Together's
    speed-optimized inference path. The non-Turbo variant exists
    but Turbo is the spike's target for "is this fast enough at
    cloud-GPU scale to be a viable Anthropic substitute?"

Per the spike spec: NO silent fallback. If Together is unreachable or
the api_key is rejected, raise so the operator sees the failure
immediately.
"""

from __future__ import annotations

import httpx

from ..utils import get_logger

log = get_logger(__name__)


# Per-call timeout. Together's Turbo path returns 72B responses in
# 1-5s typical, but cold-start + queue can push to 30-60s on the
# managed tier. 600s matches the Ollama backend (no reason for the
# two cloud paths to diverge on timeout shape) and the legacy
# distiller backend timeout.
_TOGETHER_REQUEST_TIMEOUT_SECONDS = 600.0

# Together's managed endpoint. Single value — Together is one service,
# unlike Ollama which is per-host. If a self-hosted Together-compatible
# proxy surfaces (none on the roadmap), parameterize then; YAGNI now.
_TOGETHER_BASE_URL = "https://api.together.xyz"


async def call_together_no_tools(
    prompt: str,
    system: str | None = None,
    model: str = "Qwen/Qwen2.5-72B-Instruct-Turbo",
    max_tokens: int = 4096,
    api_key: str = "",
) -> tuple[str, dict]:
    """Call Together.ai's OpenAI-compatible chat-completions endpoint.

    Mirrors :func:`call_anthropic_no_tools` and :func:`call_ollama_no_tools`
    signatures so the extractor's dispatcher can call any backend without
    per-backend conditionals at the call site.

    Args:
        prompt: User-turn content.
        system: Optional system-prompt content. ``None`` skips the
            system message entirely (mirrors the sibling backends'
            optional ``system`` kwarg).
        model: Together model slug (e.g.
            ``"Qwen/Qwen2.5-72B-Instruct-Turbo"``). Must be a model
            Together hosts on the caller's API plan — this function
            does NOT trigger model deployments.
        max_tokens: Cap on response tokens. Maps to OpenAI's
            ``max_tokens`` field which Together honors.
        api_key: Together API key (Bearer token). Required — empty
            string raises immediately rather than letting Together
            return a 401 (the failure mode is the same but the
            local-raise carries a clearer operator message).

    Returns:
        ``(text, metadata)`` — same shape as
        :func:`call_anthropic_no_tools`. ``metadata["stop_reason"]``
        is OpenAI's ``finish_reason`` (``"stop"`` / ``"length"`` /
        ``"tool_calls"``); the diagnostic intent matches Anthropic's
        ``stop_reason``.

    Raises:
        ``RuntimeError`` if the api_key is empty, the endpoint is
        unreachable, or the request returns a non-200 status. Per the
        spike spec, NO silent fallback to another backend — the
        operator must see the failure to act on it.
    """
    if not api_key:
        # Defense-in-depth — the dispatcher should fail-loud on empty
        # api_key before reaching here, but a direct caller (e.g. a
        # future spike harness that bypasses the dispatcher) must
        # also see the explicit failure. NO silent fallback.
        log.warning("together.missing_api_key", model=model)
        raise RuntimeError(
            "Together backend requires a non-empty api_key "
            "(set TOGETHER_API_KEY in environment, or pass api_key="
            "kwarg directly). NO silent fallback — see spike spec."
        )

    messages: list[dict] = []
    if system is not None:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: dict = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        # ``stream: False`` mirrors the Ollama backend — extractor
        # consumes the full response before validating against
        # Pydantic; streaming would complicate parse for zero spike
        # benefit.
        "stream": False,
    }

    url = f"{_TOGETHER_BASE_URL}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=_TOGETHER_REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.RequestError as exc:
        # Network-layer failure (DNS, connection refused, timeout) —
        # surface immediately so the operator sees Together is
        # unreachable. Daemon's per-batch try/except catches the
        # propagated RuntimeError and logs ``extractor.batch_failed``.
        log.warning(
            "together.request_failed",
            endpoint=_TOGETHER_BASE_URL,
            model=model,
            error_type=exc.__class__.__name__,
            error=str(exc),
        )
        raise RuntimeError(
            f"Together unreachable at {url}: "
            f"{exc.__class__.__name__}: {exc}"
        ) from exc

    if response.status_code != 200:
        # Application-layer failure (401 bad key, 429 rate limit, 503
        # capacity, model-not-found). Include response body in the
        # diagnostic so the operator sees Together's own error
        # message — particularly load-bearing for 401 ("invalid api
        # key") vs 429 ("rate limit") which look identical at the
        # daemon-log layer otherwise.
        body_preview = response.text[:500]
        log.warning(
            "together.http_error",
            endpoint=_TOGETHER_BASE_URL,
            model=model,
            status=response.status_code,
            body_preview=body_preview,
        )
        raise RuntimeError(
            f"Together returned HTTP {response.status_code} from {url}: "
            f"{body_preview}"
        )

    data = response.json()
    text, finish_reason = _extract_first_choice(data)
    metadata: dict = {"stop_reason": finish_reason}

    if not text:
        # Per ``feedback_intentionally_left_blank.md``: explicit
        # "ran, nothing to do" log. Empty response is unusual but
        # not necessarily a failure — the extractor's repair-retry
        # path will handle it the same way it handles empty
        # responses from Anthropic or Ollama.
        log.info(
            "together.empty_response",
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
    repair-retry path — same shape as the sibling backends' handling
    of malformed responses.

    Duplicated from ``ollama.py`` intentionally: keeps each backend
    module self-contained (no cross-backend import surface), and the
    helper is small enough that the cost of duplication is lower
    than the cost of a shared-helper-module abstraction during the
    spike. If a third OpenAI-compatible backend lands, lift to a
    shared ``backends/_openai_compat.py``.
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


__all__ = [
    "call_together_no_tools",
]
