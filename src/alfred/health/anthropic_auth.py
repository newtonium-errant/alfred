"""Shared Anthropic SDK auth probe.

The curator, janitor, distiller, and talker all talk to Anthropic at some
point — either via the CLI backend (``claude -p`` subprocess) or via the
Python SDK (talker). Rather than duplicating auth-check logic across four
tools, this module exposes one async probe that:

  1. Tries ``client.messages.count_tokens(...)`` — the zero-cost SDK
     endpoint that validates credentials without burning tokens.
  2. Falls back to ``client.messages.create(max_tokens=1, ...)`` if
     ``count_tokens`` isn't available in the installed SDK version.
     **This path does cost a few tokens** — emit a note so operators
     can see why. In anthropic==0.96 (what's shipped today),
     ``count_tokens`` is present.

The probe is deliberately single-shot and cheap. If an operator wants
a richer auth + quota test, the distiller / curator backends already
log rate-limit errors at runtime; we don't duplicate that here.

**Why not ``claude -p "ping"``?** The plan Part 11 Q8 explicitly
rules out a CLI probe that burns tokens. The SDK's ``count_tokens``
endpoint is free and deterministic.
"""

from __future__ import annotations

import asyncio
import os
import time

from .types import CheckResult, Status


# Short request for the fallback path — one user message, one token.
# We don't really care about the completion, only that the API accepts
# the request with our credentials.
_FALLBACK_MESSAGES = [{"role": "user", "content": "hi"}]


async def check_anthropic_auth(
    api_key: str | None,
    model: str = "claude-haiku-4-5",
) -> CheckResult:
    """Probe Anthropic credentials.

    Args:
        api_key: The key to probe. ``None`` or empty string → SKIP
            (no key configured, nothing to check).
        model: Model id to reference in the count_tokens call. The
            cheapest available haiku is a sensible default — the
            endpoint only needs the id to exist.

    Returns:
        A ``CheckResult`` named ``"anthropic-auth"``. Status is:
          - SKIP  if no api_key is set
          - OK    on a successful SDK call (count_tokens preferred)
          - FAIL  on authentication / SDK import errors
          - WARN  when we had to fall back to messages.create
    """
    if not api_key:
        return CheckResult(
            name="anthropic-auth",
            status=Status.SKIP,
            detail="no api_key configured",
        )

    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:
        return CheckResult(
            name="anthropic-auth",
            status=Status.FAIL,
            detail="anthropic SDK not installed",
        )

    started = time.monotonic()
    # ``Anthropic()`` picks up the key from the constructor arg rather
    # than ANTHROPIC_API_KEY so we don't accidentally use a stale env var.
    try:
        client = anthropic.Anthropic(api_key=api_key)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="anthropic-auth",
            status=Status.FAIL,
            detail=f"client init failed: {exc.__class__.__name__}: {exc}",
        )

    # Prefer the free count_tokens endpoint. anthropic >= 0.50 exposes it;
    # older SDKs don't and we fall back to a minimal messages.create.
    count_fn = getattr(client.messages, "count_tokens", None)
    if count_fn is not None:
        return await _run_count_tokens(count_fn, model, started)

    return await _run_create_fallback(client, model, started)


async def _run_count_tokens(count_fn, model: str, started: float) -> CheckResult:  # noqa: ANN001
    """Execute the preferred count_tokens probe.

    The SDK's ``count_tokens`` is synchronous — we wrap it in
    ``asyncio.to_thread`` so the aggregator's ``gather`` doesn't block
    on network I/O.
    """
    try:
        await asyncio.to_thread(
            count_fn,
            model=model,
            messages=_FALLBACK_MESSAGES,
        )
    except Exception as exc:  # noqa: BLE001
        return _auth_error_to_result(exc, started, path="count_tokens")

    latency = (time.monotonic() - started) * 1000.0
    return CheckResult(
        name="anthropic-auth",
        status=Status.OK,
        detail="count_tokens ok",
        latency_ms=latency,
        data={"model": model, "probe": "count_tokens"},
    )


async def _run_create_fallback(client, model: str, started: float) -> CheckResult:  # noqa: ANN001
    """Fallback path — messages.create with max_tokens=1.

    This costs a small number of tokens (one user message + one
    completion token). Emitted as WARN so operators notice. The plan
    (Part 11 Q8) calls out that the fallback is acceptable but should
    be flagged.
    """
    try:
        await asyncio.to_thread(
            client.messages.create,
            model=model,
            max_tokens=1,
            messages=_FALLBACK_MESSAGES,
        )
    except Exception as exc:  # noqa: BLE001
        return _auth_error_to_result(exc, started, path="messages.create")

    latency = (time.monotonic() - started) * 1000.0
    return CheckResult(
        name="anthropic-auth",
        status=Status.WARN,
        detail="count_tokens unavailable; used messages.create fallback (costs tokens)",
        latency_ms=latency,
        data={"model": model, "probe": "messages.create"},
    )


def _auth_error_to_result(exc: BaseException, started: float, path: str) -> CheckResult:
    """Translate an anthropic exception into a CheckResult.

    We keep the classification coarse: anything from the API is FAIL.
    The ``detail`` carries the exception class name + first 200 chars
    of the message so operators can triage without reading logs.
    """
    latency = (time.monotonic() - started) * 1000.0
    msg = str(exc)[:200]
    return CheckResult(
        name="anthropic-auth",
        status=Status.FAIL,
        detail=f"{path} failed: {exc.__class__.__name__}: {msg}",
        latency_ms=latency,
    )


def resolve_api_key(raw: dict) -> str | None:
    """Extract an Anthropic API key from the unified config or env.

    The Alfred CLI backend reads ``ANTHROPIC_API_KEY`` from the
    environment — we mirror that here. The talker's ``telegram.anthropic.api_key``
    is the other common location. Returns the first non-empty match,
    or ``None``.
    """
    # Env var wins — matches runtime behavior of every tool that shells
    # out to ``claude -p``.
    env = os.environ.get("ANTHROPIC_API_KEY")
    if env:
        return env
    # Fallback: talker config section (voice/ extras path)
    tel = raw.get("telegram", {}) or {}
    ant = tel.get("anthropic", {}) or {}
    key = ant.get("api_key")
    if key and not key.startswith("${"):
        return key
    return None
