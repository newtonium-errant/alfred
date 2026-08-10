"""Attribution quality — the consumer side of #63a's corpus (#72 items 2 + 6).

#63a made attribution cards auto-confirm after 24h and captured every operator
contest into an append-only corpus. Nothing read it. That left the platform's
self-correcting standard half-satisfied: the correction signal was durable and
no behaviour anywhere improved from it.

This module is the reading half. It answers two DIFFERENT questions off the same
rows, and keeping them separate is the substance of item 6:

    1. "Is auto-confirm letting wrong inferences through?"  -> demotion_contests
    2. "Which section produces bad inferences?"             -> contests_by_section

## Which contests count toward demotion

The demotion proposal asks the operator to put attribution cards BACK under
needs-you review. The only evidence that bears on it is a contest that review
would have PREVENTED:

  ``timeout_24h``   the machine confirmed it unreviewed and it was wrong.
                    COUNTS — this is the entire case for demotion.

  unconfirmed       contested while still inside its window: the operator caught
                    it in the FYI tier before the clock ran out. That is the
                    current design working. Counting it would demote the tier
                    for succeeding.

  ``backfill``      swept in at deploy, never offered under these rules (the
                    ratified #63a argument). Evidence about historical inference
                    quality, not about the policy.

  ``operator``      he reviewed it and approved it, then changed his mind.
                    Review is exactly what already happened, so returning these
                    to review would not have caught it — arguably evidence
                    AGAINST demotion.

The dispatch's prior excluded only ``backfill``. This is tighter, on the same
principle. Flagged for overrule: it decides what the operator is being asked to
approve, so it is a judgement, not a detail.

Per-section counting deliberately includes EVERY contest regardless of via —
"which section is weak" does not care how the entry was later confirmed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from .attribution_corpus import CorpusReadStats, iter_attribution_rows

log = structlog.get_logger(__name__)

# Default trailing window. Config-backed at the call site (contract item 3);
# this is the fallback, not the value.
DEFAULT_WINDOW_DAYS = 14

# The ONE grep-able quality event.
QUALITY_EVENT = "daily_sync.attribution.quality"

# The confirmed_via values whose contests bear on the demotion question. A SET
# rather than an inline comparison so the rule has one home — the same reason
# the health layer has QUIET_HEALTH_STATUSES rather than scattered ``!= "ok"``.
DEMOTION_COUNTING_VIA = frozenset({"timeout_24h"})

# #72 item (c), NOT YET BUILT — the cooldown decision, recorded here because
# this is the count the trigger will read.
#
# When the operator REJECTS a demotion proposal, wait ONE FULL
# ``quality_window_days`` from the rejection before proposing again — not a
# fixed number of days, and not "until the count rises again".
#
# The reason is the window's own arithmetic. The trigger fires on contests
# inside a trailing window; a rejected proposal leaves those same contests
# sitting in that window. Any cooldown shorter than the window re-proposes off
# evidence the operator has just declined to act on, so the second card is not
# a new signal — it is the same one, re-asked. That is how an operator learns
# to dismiss a card without reading it, which costs far more than a late
# demotion: it burns the propose-then-approve channel that the self-correcting
# standard depends on.
#
# Waiting one window guarantees the next proposal is built from contests that
# are entirely new since the rejection.
#
# Corollary for the trigger: only ONE proposal may be live at a time, so a
# pending proposal suppresses re-proposal regardless of cooldown.

# What a contest with no operator-named section is filed under. Card-level
# contest stays allowed (contract item 4), and it must remain visible in the
# stats rather than silently dropping out of the denominator.
SECTION_UNKNOWN = "unknown"

# ---------------------------------------------------------------------------
# Successor pointers — where the two unbuilt halves of item 4 start.
#
# 1. THE CONTROLLED SECTION VOCABULARY (what the operator taps).
#    It exists already, as inline string literals in the capture summary
#    renderer: ``alfred/telegram/capture_batch.py``, in
#    ``render_summary_markdown``. EIGHT headings, and note they are not one
#    uniform block — SEVEN go through the local ``_section(...)`` helper
#    (Topics, Decisions, Open Questions, Action Items, Key Insights, Raw
#    Contradictions at lines 369-374, then Discarded Noise at 378, separated
#    from the first six by a comment), and Re-encounters is appended directly
#    at line 382 rather than through the helper. A ninth site, the brief-mode
#    recap near line 1483, emits a two-heading SUBSET (Topics, Key Insights).
#
#    LIFT into one shared constant and have every site render from it; do NOT
#    copy the list here. Two hand-maintained copies of a vocabulary drift, and
#    when they do the stats key on headings the card no longer shows — the
#    snooze-ladder precedent. Grepping only the contiguous 369-374 block is the
#    specific way to get this wrong: it silently omits two live headings.
#
# 2. CARRYING THE TAPPED SECTION ON THE CONTEST ACT.
#    The per-request payload precedent is ``correction_target`` —
#    ``alfred/daily_sync/action_router.py`` line 1244, a keyword-only
#    ``str | None = None`` on the act entry point. Thread ``section`` the same
#    way; it lands on the corpus row's ``section`` field, which already exists
#    and is already read below.
#
#    Whatever plumbs it must thread it at EVERY production call site in the
#    same commit — an optional gate parameter that only tests pass is a live
#    write side with a dead read side, and every pin stays green.
# ---------------------------------------------------------------------------


@dataclass
class AttributionQuality:
    """Counts over a trailing window. Every field is also logged."""

    window_days: int = DEFAULT_WINDOW_DAYS
    auto_confirmed: int = 0
    contested: int = 0
    #: Contests that bear on the demotion question — see the module docstring.
    demotion_contests: int = 0
    #: Every contest, keyed by operator-named section (``unknown`` when none).
    #: INTERNAL accumulator — read it through :meth:`per_section_counts`, which
    #: refuses while the tap is dark. A cross-module drift pin asserts nothing
    #: outside this module touches the attribute directly.
    contests_by_section: Counter = field(default_factory=Counter)
    #: True once ANY row in the window carried a non-empty section.
    #:
    #: #72 item 4's guarantee, and the reason it is a field rather than a
    #: convention: between the corpus half shipping and the operator tap
    #: shipping, EVERY contest filed under ``unknown``. The breakdown was
    #: computed on every call and was structurally 100% one bucket, so any
    #: surface that rendered it would have shown a clean-looking histogram
    #: carrying no information — and it would have looked healthy, which is the
    #: worst kind of wrong. The refusal is mechanical so it does not depend on
    #: whoever writes the next consumer noticing.
    section_tap_live: bool = False
    #: Rows in the corpus the READER declined — corrupt JSON, not an object,
    #: or missing a field the entry cannot be built without. Counted because
    #: the alternative is a metric whose denominator shrinks in silence: a
    #: broken writer and a quiet fortnight produce the same low numbers, and
    #: only this field tells them apart.
    unreadable_rows: int = 0
    #: Rows that read fine but whose ``action_at`` will not parse, so they
    #: cannot be placed in or out of the window. Kept separate from
    #: ``unreadable_rows`` because the two mean different things to whoever
    #: reads the log — a writer emitting bad JSON is not a timestamp format
    #: drifting.
    undated_rows: int = 0

    def per_section_counts(self) -> dict[str, int] | None:
        """The per-section breakdown, or ``None`` while the tap is dark.

        THE accessor. Every consumer goes through here rather than reading
        ``contests_by_section``, so "is this breakdown meaningful yet" is
        answered once, here, instead of at each surface that might forget to
        ask.

        ``None`` is deliberately not an empty dict: a caller that treats
        falsy-as-empty renders "no sections contested", which is a different
        and false claim. ``None`` means "not measurable yet".
        """
        if not self.section_tap_live:
            return None
        return dict(self.contests_by_section)

    def to_dict(self) -> dict:
        # ILB: ``sections`` is null rather than absent while the tap is dark,
        # and ``section_tap_live`` says which of the two zero-shaped answers
        # this is — "nobody has tapped a section yet" or "sections exist and
        # none were contested".
        return {
            "window_days": self.window_days,
            "auto_confirmed": self.auto_confirmed,
            "contested": self.contested,
            "demotion_contests": self.demotion_contests,
            "sections": self.per_section_counts(),
            "section_tap_live": self.section_tap_live,
            "unreadable_rows": self.unreadable_rows,
            "undated_rows": self.undated_rows,
        }


def _parse(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def attribution_quality_stats(
    corpus_path: str | Path,
    *,
    now: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> AttributionQuality:
    """Count corpus activity over the trailing ``window_days``.

    Reads through :func:`iter_attribution_rows` — the ONE corpus reader — so
    this, the demotion trigger and the per-section rates can never disagree
    about what the corpus says.

    A row whose ``action_at`` will not parse is skipped rather than raised: the
    same posture as the reader, for the same reason (a metric that dies on one
    bad row stops being computed when it matters most). Both kinds of skip are
    COUNTED and logged — skipping quietly is what would make this metric lie.
    """
    when = now or datetime.now(timezone.utc)
    cutoff = when - timedelta(days=window_days)
    stats = AttributionQuality(window_days=window_days)
    read = CorpusReadStats()

    for row in iter_attribution_rows(corpus_path, stats=read):
        acted = _parse(row.action_at)
        if acted is None:
            stats.undated_rows += 1
            continue
        if acted < cutoff:
            continue
        action = (row.andrew_action or "").strip()
        if action == "auto_confirm":
            stats.auto_confirmed += 1
        elif action == "contest":
            stats.contested += 1
            named = (getattr(row, "section", "") or "").strip()
            if named:
                # One non-empty section anywhere in the window is what makes the
                # breakdown mean something; see AttributionQuality.section_tap_live.
                stats.section_tap_live = True
            stats.contests_by_section[named or SECTION_UNKNOWN] += 1
            if (row.confirmed_via or "").strip() in DEMOTION_COUNTING_VIA:
                stats.demotion_contests += 1

    # Safe to read only now: the generator fills this in as rows are consumed
    # and the loop above always runs it to exhaustion.
    stats.unreadable_rows = read.skipped

    # ILB: fires on EVERY call, including the all-zero quiet window that is the
    # healthy steady state. A metric that logs only when it has news is
    # indistinguishable from one that stopped running.
    log.info(QUALITY_EVENT, **stats.to_dict())
    return stats


def render_quality_line(stats: AttributionQuality) -> str:
    """One operator-facing line. Rendered on EVERY sync, zero included.

    Says the plain counts rather than a rate: "3 of 40" is a number the operator
    can act on, and a percentage of a small denominator would overstate a quiet
    fortnight.
    """
    return (
        f"Attribution quality: {stats.auto_confirmed} auto-confirmed, "
        f"{stats.contested} contested ({stats.window_days}-day)."
    )


__all__ = [
    "DEFAULT_WINDOW_DAYS",
    "DEMOTION_COUNTING_VIA",
    "QUALITY_EVENT",
    "SECTION_UNKNOWN",
    "AttributionQuality",
    "attribution_quality_stats",
    "render_quality_line",
]
