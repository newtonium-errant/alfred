"""Tier — 3-tier task system with deadline-relative escalation.

Phase 1 (2026-05-28). Salem-only by virtue of brief integration (each
instance's brief scans its own vault; non-Salem instances have no tasks
using these fields).

This module is a **pure read-side projection** over ``vault/task/*.md``
records. ``effective_tier`` is computed at brief-render time from
``base_tier``, ``due``, ``escalate_at_days``, and ``escalate_to`` — it is
**never written back** to the record. See ``compute.py`` for the
computation contract and ``alfred.brief.tier_section`` for the render
layer.
"""

from .compute import (
    DEFAULT_ESCALATION_GAP,
    OPEN_STATUSES,
    PRIORITY_TO_BASE_TIER,
    AutoT1Candidate,
    TierResult,
    coerce_due_date,
    compute_auto_t1_candidates,
    compute_effective_tier,
    derive_base_tier_from_priority,
)
from .daily_curation import (
    DailyCuration,
    T1T2Entry,
    T1_T2_SOURCES,
    T3Entry,
    T3_SOURCES,
    load_daily_curation,
    save_tier_curation,
)

__all__ = [
    "AutoT1Candidate",
    "DEFAULT_ESCALATION_GAP",
    "DailyCuration",
    "OPEN_STATUSES",
    "PRIORITY_TO_BASE_TIER",
    "T1T2Entry",
    "T1_T2_SOURCES",
    "T3Entry",
    "T3_SOURCES",
    "TierResult",
    "coerce_due_date",
    "compute_auto_t1_candidates",
    "compute_effective_tier",
    "derive_base_tier_from_priority",
    "load_daily_curation",
    "save_tier_curation",
]
