"""Composition helper for the ``/today`` slash command.

The ``/today`` command is a Salem-only glance-view mini-brief composing
the curated tier shortlists + the UPCOMING EVENTS section into one
Telegram reply. Read-only; no vault writes, no session record.

**Scope refinement history:**

  * **Ship 3 (2026-05-29)** dropped the routines section from
    ``/today``. Routines live in the morning brief or via the routine
    system surface (``alfred routine show``).
  * **2026-05-30 curated-only refinement.** Tier section narrowed
    from the full morning-brief surface (curated + auto-surface +
    selection pool + rollover + confirm prompts) to the curated-only
    view via :func:`alfred.brief.tier_section.render_curated_tier_section_for_today`.
    Operator-stated purpose: ``/today`` is the operator-committed
    view ("what did I sign up for today"), NOT the materials view
    ("what could I add to today"). The full materials surface lives
    in the morning brief; ``/today`` is the focused-commit glance.

Why ``/today`` instead of just re-using the brief file:

  1. **Glance-view from anywhere.** Operator can ``/today`` from a phone
     without opening Obsidian to read this morning's brief. Especially
     valuable mid-afternoon when "what did I commit to today" is a more
     useful surface than "what was the brief at 06:00?"
  2. **Live data, not snapshot.** Each section re-renders from the
     current vault state. The curated shortlists reflect the latest
     ``tier_curation`` block (e.g. talker edits an entry at 14:00,
     ``/today`` at 14:01 shows the updated list).
  3. **Curated-render reuses the brief's per-entry primitives.** The
     composition imports
     ``brief.tier_section.render_curated_tier_section_for_today``
     which shares the per-entry helpers (``_render_t2_entry`` /
     ``_render_t3_entry``) with the morning brief. A render-shape
     change on the brief side propagates here through the shared
     helpers — single source of truth for "what does a curated entry
     look like."

Operator-facing section ordering matches the morning brief: tier first
(curated commitments), upcoming events second (forward-looking
calendar). Each section is preceded by its ``## Header`` line so the
operator's mental model of the brief carries over.

Telegram has a ~4096 character body limit. The composer caps the reply
defensively at 4000 characters; if both sections together exceed
that, the overflow is truncated with a notice. Empirical from spot
checks on the morning brief: typical Salem composition runs ~1500-2500
characters, well under cap. The curated-only narrowing pushes typical
size DOWN further (~200-500 chars for tier alone vs ~1500+ for the
full materials view) so truncation is now genuinely an edge-case
guard.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import structlog

from alfred.brief.config import UpcomingEventsConfig
from alfred.brief.tier_section import (
    SECTION_HEADER as TIER_SECTION_HEADER,
    render_curated_tier_section_for_today,
)
from alfred.brief.upcoming_events import (
    SECTION_HEADER as UPCOMING_EVENTS_SECTION_HEADER,
    render_upcoming_events_section,
)
from alfred.tier.daily_curation import load_daily_curation

log = structlog.get_logger(__name__)


# Header text comes from the canonical brief section modules — single
# source of truth so the ``/today`` composer + any future surface that
# mirrors the brief sections all read from the same constants. A
# refactor to the brief's section header text propagates here without
# code changes. Ordering matches the brief daemon: tier (deadline-
# driven) first, upcoming events (forward calendar) second. Ship 3
# (2026-05-29) dropped the routines section from /today; routines live
# in the morning brief or via the routine CLI surface.


# Telegram per-message body cap. Plain text only — no MarkdownV2 because
# the wikilink syntax (``[[task/Foo]]``) collides with Markdown's link
# bracket syntax in unpredictable ways, and the operator can click
# wikilinks in Obsidian post-reply. Same rationale as the existing
# inventory-view replies; see ``inventory_views.py`` module docstring.
_TELEGRAM_BODY_CAP = 4000


def compose_today_reply(
    vault_path: Path,
    now: datetime,
) -> str:
    """Compose the ``/today`` mini-brief reply.

    Calls the curated tier render + the upcoming events render in
    the canonical ordering (tier → upcoming events) and joins them
    with Markdown ``## Header`` lines so the visual shape matches
    the morning brief's top sections.

    ``now`` is the reference instant — used for ``today`` date
    selection in the curation load + events render. Caller (the bot
    handler) computes
    ``datetime.now(ZoneInfo(config.today_command.timezone))``.

    Cap: returns at most ``_TELEGRAM_BODY_CAP`` characters. Overflow
    is truncated with an explicit notice per intentionally-left-blank
    so the operator knows the reply was cut.

    Per ``feedback_intentionally_left_blank.md``: each section's
    render function returns a non-empty body (with a sentinel when
    its bucket is empty), so the composed reply never silently
    drops a section. The composer adds the section headers
    unconditionally — two section headers always emit, two
    sentinel bodies render below them when appropriate.

    **Curated-only tier surface** (2026-05-30 refinement). The tier
    section is the operator-committed view — auto-T1 candidates, T2
    selection pool, auto-T2-routine subsection, rollover, and confirm
    prompts all live ONLY in the morning brief. ``/today`` shows
    only what the operator has signed up for today via talker
    curation. Operator workflow gap surfaced 2026-05-30: the full
    materials view in ``/today`` muddled the glance-view's purpose
    ("what's on my plate" vs "what could I add").

    **Today + tomorrow events only** (2026-05-30 refinement). The
    Upcoming Events section is rendered with ``scope="today_tomorrow"``
    — clamps the window to 1 day ahead and reshapes the output into
    Today / Tomorrow buckets. The morning brief retains the full
    7-day window (Today / This Week / Later); only the glance-view
    is narrowed. Same operator workflow framing as the curated-only
    tier surface above — ``/today`` is for "what's on my plate
    immediately," not "what's on the schedule for the next week."

    **Routines NOT included** (Ship 3 scope refinement, 2026-05-29).
    Routines live in the morning brief or via ``alfred routine show``.

    Read-only — no vault writes, no state mutation. Pure
    composition over the curated tier load + events render.
    """
    today_local = now.date()

    # Tier section — curated-only view via the daily curation block.
    # Per 2026-05-30 scope refinement: NOT the full materials view
    # (no auto-T1 / no selection pool / no rollover / no confirm
    # prompts). The curated render is a pure projection over the
    # tier_curation frontmatter block.
    try:
        curation = load_daily_curation(vault_path, today_local)
        # Pass vault_path so the render filters out curated tasks the
        # operator has since CLOSED (2026-06-15) — /today shows only
        # live commitments. Without the path the render skips filtering;
        # the composer always has the path, so /today always filters.
        tier_body = render_curated_tier_section_for_today(
            curation, vault_path=vault_path,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "today_command.tier_render_failed",
            error_type=exc.__class__.__name__,
            error=str(exc),
        )
        tier_body = "*(tier render failed; see brief log)*"

    # Upcoming events — synthesize a default UpcomingEventsConfig with
    # ``enabled=True``. The brief daemon reads the operator's
    # configured ``brief.upcoming_events`` block; ``/today`` deliberately
    # uses a default-on synthesised config so the operator gets the
    # forward calendar slice even if they've never configured the
    # brief. ``scope="today_tomorrow"`` narrows the window to today +
    # tomorrow only (clamps ``max_days_ahead`` to 1 + reshapes the
    # output into Today / Tomorrow buckets) per the 2026-05-30
    # ``/today`` scope refinement — operator wanted only the immediate
    # calendar slice in the glance-view. The morning brief retains the
    # full 7-day window; only the glance-view is narrowed.
    try:
        upcoming_body = render_upcoming_events_section(
            UpcomingEventsConfig(enabled=True),
            vault_path,
            today_local,
            scope="today_tomorrow",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "today_command.upcoming_render_failed",
            error_type=exc.__class__.__name__,
            error=str(exc),
        )
        upcoming_body = "*(upcoming events render failed; see brief log)*"

    # An empty string from render_upcoming_events_section means "section
    # disabled" — defensively treat as the sentinel so the section header
    # still emits.
    if not upcoming_body.strip():
        upcoming_body = "*(no upcoming events)*"

    # Compose with the canonical brief section ordering + headers.
    # Tier first (deadline-driven actionable), upcoming events second
    # (forward-looking calendar). Routines dropped per Ship 3.
    composed = (
        f"## {TIER_SECTION_HEADER}\n\n"
        f"{tier_body.rstrip()}\n\n"
        f"## {UPCOMING_EVENTS_SECTION_HEADER}\n\n"
        f"{upcoming_body.rstrip()}"
    )

    # Telegram body-cap defense. Overflow truncation emits an explicit
    # notice so the operator can see WHY the reply was cut and what
    # to do (re-read the morning brief file for the full surface).
    if len(composed) > _TELEGRAM_BODY_CAP:
        cap_with_notice = _TELEGRAM_BODY_CAP - 100  # room for notice
        composed = (
            composed[:cap_with_notice].rstrip()
            + "\n\n…(truncated; see this morning's brief for the full "
            "view, or open Obsidian for live state)"
        )

    log.info(
        "today_command.composed",
        reply_chars=len(composed),
        date=today_local.isoformat(),
    )
    return composed


__all__ = [
    "compose_today_reply",
]
