"""Anthropic SDK compatibility shims — model-family quirks in one place.

Opus 4.x deprecated the ``temperature`` parameter on ``messages.create``;
sending it produces ``400 'temperature' is deprecated for this model.``
Sonnet, Haiku, and older Claude families still accept it.

Every site that builds ``messages.create`` kwargs must drop ``temperature``
when the target model is an Opus family member. Concentrating that rule
here means new call sites get the behaviour by construction and a future
SDK quirk (a Sonnet-side restriction, a new family) lands in one place.
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


__all__ = ["messages_create_kwargs"]
