"""Anthropic SDK compatibility shims — model-family quirks + the in-process caller, in one place.

This is the PACKAGE-ROOT home for Anthropic-SDK glue, owned by no single tool (per
``feedback_sdk_quirk_centralization.md``: "promote the helper to ``src/alfred/_anthropic_compat.py``
… Don't import across packages from telegram/"). It was promoted here from the telegram package
(its original home) when the second non-telegram consumer — the ``email_filing`` LLM fallback —
joined ``email_classifier`` in duplicating the same construct-client-and-call block. The original
telegram re-export shim has since been retired and every importer migrated to this module.

Two surfaces:

* :func:`messages_create_kwargs` — the model-family quirk gate. Opus 4.x deprecated the
  ``temperature`` parameter on ``messages.create``; sending it produces
  ``400 'temperature' is deprecated for this model.`` Sonnet, Haiku, and older Claude families
  still accept it. Every site that builds ``messages.create`` kwargs routes through here so the rule
  lives in ONE place and a future SDK quirk (a Sonnet-side restriction, a new family) lands once.

* :func:`call_anthropic_text` — the whole in-process "system + user → assistant text" call:
  construct the client, gate on a real api_key, call ``messages.create`` (via
  :func:`messages_create_kwargs`, so the quirk applies BY CONSTRUCTION even for callers that pass no
  ``temperature`` today), concatenate the response ``text`` blocks, and fail SILENT (return ``""``)
  on a missing SDK / missing key / any SDK error. Takes PRIMITIVES, not a config object, so it
  depends on NEITHER consumer's config type — it is the decoupling seam that lets ``email_classifier``
  and ``email_filing`` share the call without importing each other. ``log`` + ``log_prefix`` preserve
  each caller's own logger binding and its exact ``<prefix>.*`` event strings.
"""
from __future__ import annotations

from typing import Any


def _opus_rejects_temperature(model: str) -> bool:
    # Opus 4.x and later reject ``temperature``. The check is prefix-based
    # so any future ``claude-opus-4-N`` / ``claude-opus-5-...`` alias
    # inherits the rule without a code edit.
    return model.startswith("claude-opus-")


def messages_create_kwargs(
    *,
    model: str,
    temperature: float | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Build kwargs for ``client.messages.create`` with model-family quirks applied.

    Pass every other parameter through unchanged. ``temperature`` is
    dropped when ``model`` belongs to a family that rejects it. Pass
    ``temperature=None`` (or omit it) to never request a temperature.
    """
    out: dict[str, Any] = {"model": model, **kwargs}
    if temperature is not None and not _opus_rejects_temperature(model):
        out["temperature"] = temperature
    return out


def call_anthropic_text(
    *,
    api_key: str,
    model: str,
    max_tokens: int,
    system: str,
    user: str,
    log: Any,
    log_prefix: str,
    temperature: float | None = None,
) -> str:
    """Run one in-process Anthropic ``messages.create`` (a ``system`` + ``user`` turn) and return the
    concatenated assistant ``text`` blocks — or ``""`` on ANY failure.

    FAIL-SILENT by construction (the callers treat ``""`` as their sentinel-fallback: 'no answer'):
    a missing ``anthropic`` package, a missing / unresolved-``${VAR}`` ``api_key``, or any SDK error
    each return ``""`` after a ``<log_prefix>.*`` warning. ``log`` is the caller's own structlog
    logger (preserves the logger binding); ``log_prefix`` namespaces the events
    (``anthropic_not_installed`` / ``no_api_key`` / ``llm_call_failed``) so each consumer keeps its
    exact event strings. The ``temperature`` quirk-drop is applied via :func:`messages_create_kwargs`.
    """
    try:
        import anthropic
    except ImportError:
        log.warning(f"{log_prefix}.anthropic_not_installed")
        return ""

    if not api_key or api_key.startswith("${"):
        log.warning(f"{log_prefix}.no_api_key")
        return ""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(**messages_create_kwargs(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            temperature=temperature,
        ))
    except Exception as exc:  # noqa: BLE001 — must not crash the caller's post-processor
        log.warning(f"{log_prefix}.llm_call_failed", error=str(exc), model=model)
        return ""

    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        block_text = getattr(block, "text", None)
        if isinstance(block_text, str):
            parts.append(block_text)
    return "".join(parts)


__all__ = ["messages_create_kwargs", "call_anthropic_text"]
