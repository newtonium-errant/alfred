"""Tier — V2 daily curation ritual (2026-05-29).

Tier-V2 reframes tier as a daily curation ritual stored in
``vault/daily/<date>.md`` rather than persistent per-task attributes.
The operator picks each day's T1 / T2 / T3 shortlists in the morning
via talker; the brief renders the curated lists going forward that day.

Two layers:

  * :mod:`alfred.tier.compute` — auto-T1 candidate discovery (which
    open tasks should the operator be prompted to confirm as T1 today)
    + the ``coerce_due_date`` / ``OPEN_STATUSES`` primitives.
  * :mod:`alfred.tier.daily_curation` — the data layer for the
    ``tier_curation`` frontmatter block (typed dataclasses +
    load/save helpers).

The render layer lives at :mod:`alfred.brief.tier_section` (composes
auto-T1 + curated shortlists + T2 selection pool + rollover).

V1 (per-task ``base_tier`` / ``escalate_to`` / priority-fallback
projection) was retired in Ship 3 (2026-05-29). The migration script
``scripts/migrate_tier_phase1.py`` is preserved for the deferred
backfill of the 24 existing ``base_tier`` records (Ship 5).
"""

from .compute import (
    OPEN_STATUSES,
    AutoT1Candidate,
    coerce_due_date,
    compute_auto_t1_candidates,
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
    "DailyCuration",
    "OPEN_STATUSES",
    "T1T2Entry",
    "T1_T2_SOURCES",
    "T3Entry",
    "T3_SOURCES",
    "coerce_due_date",
    "compute_auto_t1_candidates",
    "load_daily_curation",
    "save_tier_curation",
]
