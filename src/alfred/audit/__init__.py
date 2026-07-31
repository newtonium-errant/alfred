"""Audit sweeps over the vault — retroactive attribution-marker tooling.

Companion to :mod:`alfred.vault.attribution` (the marker primitives)
and the per-write wrappings in c2 + c4. The c3 sweep promotes
pre-existing soft ``_source:`` annotations on calibration entries
into the structured BEGIN_INFERRED / attribution_audit contract so the
Daily Sync confirmation flow can surface them.
"""

from typing import Any

from .sweep import (
    InferMarkerCandidate,
    InferMarkerResult,
    sweep_paths,
)


def agent_slug_for(config: Any) -> str:
    """Return the lowercased instance slug for attribution markers.

    Canonical helper for attribution-marker slugging — used by the
    talker conversation dispatcher and all non-talker writers (audit
    sweep, capture_batch, calibration, Daily Sync proposal-confirm)
    so the slug shape stays uniform across writers without each module
    growing its own copy.

    Accepts any object exposing ``config.instance.name`` (TalkerConfig,
    DailySyncConfig, raw scaffolds with the InstanceConfig dataclass).
    Returns ``"talker"`` when ``config`` is ``None`` or when
    ``instance.name`` is unset / empty — preserves the legacy fallback
    used by the talker before this helper was promoted out of
    ``alfred.telegram.conversation``. Lowercase-only because the
    marker_id contract expects ``[\\w-]+`` and downstream surfacers
    will group by agent.
    """
    if config is None:
        return "talker"
    instance = getattr(config, "instance", None)
    if instance is None:
        return "talker"
    name = getattr(instance, "name", None) or ""
    name = str(name).strip().lower()
    return name or "talker"


def instance_name_from_raw(raw: Any) -> str:
    """The instance's DISPLAY name from the unified config dict —
    ``telegram.instance.name`` (e.g. "Salem", "KAL-LE"), the canonical identity
    the rest of the instance uses.

    For feed producers that hold the unified ``raw`` config but not a ``*Config``
    exposing ``.instance.name``: the daily_sync feed producer's ``DailySyncConfig``
    has NO ``instance`` field, so ``agent_slug_for`` there always fell back to
    "talker" and mislabelled every sync feed chip (2026-07-31). This mirrors
    ``BriefConfig.instance_name`` so the two feed producers stamp the SAME store
    with the SAME instance value. Returns "" when unset — honest (an empty chip),
    never a wrong default; distinct from :func:`agent_slug_for`, which lowercases
    for the marker_id slug contract.
    """
    if isinstance(raw, dict):
        telegram = raw.get("telegram")
        if isinstance(telegram, dict):
            instance = telegram.get("instance")
            if isinstance(instance, dict):
                name = instance.get("name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
    return ""


__all__ = [
    "InferMarkerCandidate",
    "InferMarkerResult",
    "sweep_paths",
    "agent_slug_for",
    "instance_name_from_raw",
]
