"""Compat RE-EXPORT shim — the Anthropic-SDK quirk helper was PROMOTED to the package root
(:mod:`alfred._anthropic_compat`) when a second non-telegram consumer joined (email_filing alongside
email_classifier), per ``feedback_sdk_quirk_centralization.md``. This module re-exports
``messages_create_kwargs`` so the telegram package's existing call sites + tests keep importing
``alfred.telegram._anthropic_compat`` unchanged.

RETIRED by the boarded follow-up (migrate the telegram importers to ``alfred._anthropic_compat`` +
delete this shim). Do NOT add new logic here — put it in the root module.
"""
from __future__ import annotations

from alfred._anthropic_compat import (  # noqa: F401 — re-export for backward compat
    _opus_rejects_temperature,
    messages_create_kwargs,
)

__all__ = ["messages_create_kwargs"]
