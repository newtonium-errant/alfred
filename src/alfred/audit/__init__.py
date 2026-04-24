"""Audit sweeps over the vault — retroactive attribution-marker tooling.

Companion to :mod:`alfred.vault.attribution` (the marker primitives)
and the per-write wrappings in c2 + c4. The c3 sweep promotes
pre-existing soft ``_source:`` annotations on calibration entries
into the structured BEGIN_INFERRED / attribution_audit contract so the
Daily Sync confirmation flow can surface them.
"""

from .sweep import (
    InferMarkerCandidate,
    InferMarkerResult,
    sweep_paths,
)

__all__ = [
    "InferMarkerCandidate",
    "InferMarkerResult",
    "sweep_paths",
]
