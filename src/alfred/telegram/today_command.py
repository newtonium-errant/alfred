"""Composition helper for the ``/today`` slash command (Tier-V2 Ship 3,
2026-05-29; originally shipped Phase 2A 2026-05-28).

The ``/today`` command is a Salem-only glance-view mini-brief composing
the brief's TIER section (open tasks by curated shortlist + selection
pool + rollover) and the UPCOMING EVENTS section into one Telegram
reply. Read-only; no vault writes, no session record.

**Scope refinement (Ship 3, 2026-05-29).** The routines section was
dropped from ``/today``. Routines live in the morning brief or via
the routine system surface (``alfred routine show``) — duplicating
them in ``/today`` confused the glance-view's purpose. The tier
section + events are the two surfaces the operator actually wants
on demand: "what's still on my plate today" + "what's coming up."
If you're looking for routines, read this morning's brief or open
Obsidian; ``/today`` no longer surfaces them.

Why ``/today`` instead of just re-using the brief file:

  1. **Glance-view from anywhere.** Operator can ``/today`` from a phone
     without opening Obsidian to read this morning's brief. Especially
     valuable mid-afternoon when "what's still on the list?" is a more
     useful surface than "what was the brief at 06:00?"
  2. **Live data, not snapshot.** Each section re-renders from the
     current vault state. A task closed at 14:00 is gone from the tier
     bucket by ``/today`` at 14:01.
  3. **Same render functions as the brief.** The composition imports
     ``brief.tier_section.render_tier_section`` and
     ``brief.upcoming_events.render_upcoming_events_section`` directly
     — single source of truth for "what does the tier render look
     like?" If the brief changes the render, ``/today`` changes too.

Operator-facing section ordering matches the morning brief: tier first
(deadline-driven actionable), upcoming events second (forward-looking
calendar). Each section is preceded by its ``## Header`` line so the
operator's mental model of the brief carries over.

Telegram has a ~4096 character body limit. The composer caps the reply
defensively at 4000 characters; if both sections together exceed
that, the overflow is truncated with a notice. Empirical from spot
checks on the morning brief: typical Salem composition runs ~1500-2500
characters, well under cap. Truncation is a defensive guard for edge
cases (40+ open tasks across all tiers + a packed calendar week).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import structlog

from alfred.brief.config import UpcomingEventsConfig
from alfred.brief.tier_section import (
    SECTION_HEADER as TIER_SECTION_HEADER,
    render_tier_section,
)
from alfred.brief.upcoming_events import (
    SECTION_HEADER as UPCOMING_EVENTS_SECTION_HEADER,
    render_upcoming_events_section,
)

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

    Calls the two brief render functions in the canonical ordering
    (tier → upcoming events) and joins them with Markdown
    ``## Header`` lines so the visual shape matches the morning
    brief's top sections.

    ``now`` is the reference instant for the tier section's deadline
    math + the date selection for events. Caller (the bot handler)
    computes ``datetime.now(ZoneInfo(config.today_command.timezone))``.

    Cap: returns at most ``_TELEGRAM_BODY_CAP`` characters. Overflow
    is truncated with an explicit notice per intentionally-left-blank
    so the operator knows the reply was cut.

    Per ``feedback_intentionally_left_blank.md``: each section's
    render function returns a non-empty body (with a sentinel when
    its bucket is empty), so the composed reply never silently
    drops a section. The composer adds the section headers
    unconditionally — two section headers always emit, two
    sentinel bodies render below them when appropriate.

    **Routines NOT included** (Ship 3 scope refinement, 2026-05-29).
    Routines live in the morning brief or via ``alfred routine show``
    — duplicating them here muddled the glance-view purpose. If the
    operator asks about routines via talker, the response should
    point at the brief or the routine CLI surface, not ``/today``.

    Read-only — no vault writes, no state mutation. Pure
    composition over the two section renders.
    """
    today_local = now.date()

    # Tier section — full datetime instant for deadline math.
    try:
        tier_body = render_tier_section(vault_path, now)
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
    # brief. Defaults to the brief's V1 forward window (7 days) —
    # matches operator expectations on the brief.
    try:
        upcoming_body = render_upcoming_events_section(
            UpcomingEventsConfig(enabled=True),
            vault_path,
            today_local,
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
