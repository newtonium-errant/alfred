"""Brief integration — render the "Today's Routines" section.

The routine daemon writes ``vault/daily/<today>.md`` at 05:59 Halifax;
the brief reads that file at 06:00 and inlines its body as the
``## Today's Routines`` section. Loose-coupling-via-filesystem mirrors
the BIT → brief health-section pattern.

Per ``feedback_intentionally_left_blank.md`` the renderer ALWAYS returns
a non-empty string when the section is enabled:
  - File exists + parses → return the body markdown.
  - File missing → return the "no routines due today" sentinel so the
    brief still has a visible Routines header.
  - File exists but body parse failed → emit a warning log + return the
    sentinel so the brief doesn't crash on a malformed derivative.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import frontmatter  # type: ignore[import-untyped]
import structlog

log = structlog.get_logger(__name__)


# Canonical header text for the brief's routines section. Hoisted to a
# module-level constant 2026-05-28 (Tier Phase 2A code-reviewer NOTE)
# so the ``/today`` slash command composer + any other downstream
# surface that mirrors the brief's section headers reads from one
# source of truth. A future refactor that renames the header (e.g.
# adding day-of-week prefix) updates this constant and every
# consumer follows.
SECTION_HEADER = "Today's Routines"


_SENTINEL = "*(no routines due today — the routine daemon either has not run yet or no routines fire on this day)*"


def render_routine_section(vault_path: Path, today: date) -> str:
    """Return the body markdown for the brief's "Today's Routines" section.

    Reads ``<vault_path>/daily/<today.iso>.md`` (the aggregator's output)
    and extracts its body. The frontmatter routines_contributing list is
    informational only; the brief consumer cares about the visible
    checklist sections (Critical / Tracked / Aspirational).

    Always returns a non-empty string per intentionally-left-blank.
    """
    iso = today.isoformat()
    daily_file = vault_path / "daily" / f"{iso}.md"

    if not daily_file.exists():
        log.info(
            "brief.routine_section.no_daily_note",
            path=str(daily_file),
            date=iso,
            detail=(
                "routine daemon has not written today's daily aggregator "
                "note yet (fires at 05:59 Halifax). Emitting the "
                "intentionally-left-blank sentinel."
            ),
        )
        return _SENTINEL

    try:
        post = frontmatter.load(str(daily_file))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "brief.routine_section.parse_failed",
            path=str(daily_file),
            error=str(exc),
        )
        return _SENTINEL

    body = (post.content or "").strip()
    if not body:
        log.info(
            "brief.routine_section.empty_body",
            path=str(daily_file),
            date=iso,
        )
        return _SENTINEL

    log.info(
        "brief.routine_section.rendered",
        path=str(daily_file),
        date=iso,
        body_chars=len(body),
    )
    return body + "\n"


__all__ = ["SECTION_HEADER", "render_routine_section"]
