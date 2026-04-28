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

    Mirror of :func:`alfred.telegram.conversation._agent_slug` — promoted
    here so non-talker writers (audit sweep, capture_batch, calibration,
    Daily Sync proposal-confirm) can derive their attribution slug
    without importing the talker module. Using the same shape everywhere
    avoids a future second copy drifting.

    Accepts any object exposing ``config.instance.name`` (TalkerConfig,
    DailySyncConfig, raw scaffolds with the InstanceConfig dataclass).
    Returns ``"talker"`` when ``config`` is ``None`` or when
    ``instance.name`` is unset / empty — matches the legacy fallback in
    :func:`alfred.telegram.conversation._agent_slug`. Lowercase-only
    because the marker_id contract expects ``[\\w-]+`` and downstream
    surfacers will group by agent.
    """
    if config is None:
        return "talker"
    instance = getattr(config, "instance", None)
    if instance is None:
        return "talker"
    name = getattr(instance, "name", None) or ""
    name = str(name).strip().lower()
    return name or "talker"


__all__ = [
    "InferMarkerCandidate",
    "InferMarkerResult",
    "sweep_paths",
    "agent_slug_for",
]
