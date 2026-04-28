"""Telegram-package compatibility shims — cross-module helpers that
multiple call sites need but should not duplicate.

Currently houses the instance-name normaliser shared between
:mod:`alfred.telegram.bot` (peer-route self-target check) and
:mod:`alfred.telegram.speed_pref` (per-instance TTS speed lookup). The
two modules used to carry independent copies; the bug surface that
created (any divergence between the two normalisations would break the
``(instance, user)`` key match across the dispatch path) is exactly the
sort of drift the canonical-helper pattern exists to prevent.

Distinct from :mod:`alfred.telegram._anthropic_compat`, which targets
SDK-level model-family quirks. This module is for *internal* shared
helpers; the SDK shim stays where it is.
"""

from __future__ import annotations


def _normalize_instance_name(s: str) -> str:
    """Return the canonical peer-key form of an instance name.

    Lowercases, strips dots, and maps spaces to dashes. The legacy
    ``alfred`` → ``salem`` mapping is preserved so a default-configured
    install (``InstanceConfig(name="Alfred")``) still matches the
    ``salem`` peer key in ``config.transport.peers`` and the
    canonical-person-record preference tables.

    The legacy mapping is intentionally retained — removing it would
    break edge-case migration paths (default-name installs, older
    fixtures) that the multi-instance roster never explicitly retired.
    See ``project_hardcoding_followups.md`` item 5: extraction is safe,
    deletion is deferred.
    """
    normalized = (s or "").lower().replace(".", "").replace(" ", "-")
    if normalized == "alfred":
        return "salem"
    return normalized


__all__ = ["_normalize_instance_name"]
