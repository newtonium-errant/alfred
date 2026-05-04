"""Radar section provider — KAL-LE distiller-radar Phase 3b.

Reads today's daily-radar markdown file (written by Phase 3a's
:func:`alfred.distiller.radar_day.run_daily_radar`) and surfaces it
as a Daily Sync section.

Phase 3a writes to ``<digests_dir>/daily/YYYY-MM-DD.md``. Phase 3b
reads that same file, lifts the rendered items, and renders them
into the morning Daily Sync conversation. The provider is
read-only — the daily file is the source of truth; replies feed back
through the regular Daily Sync reply parser when smart-routing for
radar items is added (deferred — radar items currently surface as
informational, not actionable).

Priority: 22 — between canonical proposals (15) and attribution
audit (25). Radar items are inspectable signals, not decisions
Andrew has to make per item, so they belong below proposals (which
require explicit confirm/reject) but above the attribution audit
(which is a long tail of low-stakes verification).

Design ratified in ``project_kalle_radar_phase3.md`` (2026-05-02).

## Read path

The daemon calls :func:`set_digests_dir` once at startup to point
the provider at the configured digests directory (default:
``<vault>/digests``). On each fire, the provider:

  1. Resolves today's expected file path via
     :func:`alfred.distiller.radar_day.latest_daily_path`.
  2. Returns ``None`` (omit the section) when the file is missing —
     this is the "Phase 3a daemon hasn't fired yet today" / "radar
     disabled" case. The empty-Daily-Sync header already handles
     full-empty days.
  3. When present, lifts the file's body verbatim into a Daily Sync
     section with a recap header.

Why verbatim: Phase 3a already does the heavy lifting (ranking,
dedup, render). Phase 3b is glue; re-rendering would duplicate the
formatting contract and risk drift between the standalone daily
file and the Daily Sync section.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import structlog

from alfred.distiller.radar_day import latest_daily_path

from .config import DailySyncConfig

log = structlog.get_logger(__name__)


@dataclass
class RadarItemSummary:
    """Lightweight summary of a radar item for state-file persistence.

    The Daily Sync state file records ``last_batch.radar_items`` as a
    parallel list to email/attribution/proposal items so the reply
    dispatcher (when smart-routing for radar items lands) can resolve
    "item N" → record path. Phase 3b ships read-only — items are
    informational; the dispatcher routing is deferred. The data shape
    is locked here so the dispatcher hook can land without re-shaping
    state-file rows.
    """

    item_number: int  # 1-indexed, GLOBAL across Daily Sync sections
    record_type: str
    record_path: str
    score: float
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_number": self.item_number,
            "record_type": self.record_type,
            "record_path": self.record_path,
            "score": self.score,
            "summary": self.summary,
        }


# Module-level holders mirror email/attribution/proposals shape — the
# section provider callable signature ``(config, today)`` doesn't take
# a digests-dir or vault-path arg, so the daemon stashes the path here
# at startup.
_DIGESTS_DIR_HOLDER: dict[str, Path] = {}


def set_digests_dir(digests_dir: Path) -> None:
    """Configure the digests directory the section provider reads from.

    Daemon calls this once at startup. Tests may call it before
    invoking :func:`radar_section` directly. Idempotent.
    """
    _DIGESTS_DIR_HOLDER["path"] = digests_dir


def get_digests_dir() -> Path | None:
    """Return the configured digests dir, or ``None`` when unset."""
    return _DIGESTS_DIR_HOLDER.get("path")


# Module-level batch holder so the daemon can read the items back
# after the assembler runs. Mirrors email/attribution/proposals.
_LAST_BATCH_HOLDER: dict[str, list[RadarItemSummary]] = {"items": []}


def consume_last_batch() -> list[RadarItemSummary]:
    """Return and clear the most recently-built batch.

    Called by the daemon after :func:`assemble_message` so it can
    persist the radar items into the Daily Sync state file under
    ``last_batch.radar_items``.
    """
    items = _LAST_BATCH_HOLDER.get("items", [])
    _LAST_BATCH_HOLDER["items"] = []
    return items


def peek_last_batch_count() -> int:
    """Non-destructive count for the assembler's
    ``item_count_after`` hook.

    Lets the next section provider's items number continuously after
    radar items.
    """
    return len(_LAST_BATCH_HOLDER.get("items", []))


def _parse_daily_file_items(
    body: str,
    *,
    start_index: int,
) -> list[RadarItemSummary]:
    """Parse Phase 3a's daily-radar markdown into RadarItemSummary
    rows for state-file persistence.

    Phase 3a's render_daily_file emits per-item blocks of the shape::

        ### 1. Synthesis: "summary text" (score 9.30)
            type: synthesis  src: 3  ent: 4  age: 0.42d
            path: /abs/path/to/record.md
            cross_source=...

    We extract item index, summary, score, type, path. The renumber-
    against-start_index step lets the assembler keep numbering
    continuous across sections (radar item 1 in the daily file
    becomes Daily Sync item N when N-1 items already rendered above).

    Returns ``[]`` when the body has no item blocks (i.e. the
    "no radar items today" empty-state file).
    """
    if not body:
        return []
    items: list[RadarItemSummary] = []
    lines = body.splitlines()
    pending_summary: str = ""
    pending_score: float = 0.0
    pending_type: str = ""
    pending_path: str = ""
    in_item: bool = False

    def _flush() -> None:
        nonlocal in_item, pending_summary, pending_type, pending_path, pending_score
        if not in_item:
            return
        items.append(
            RadarItemSummary(
                item_number=start_index + len(items),
                record_type=pending_type or "unknown",
                record_path=pending_path,
                score=pending_score,
                summary=pending_summary,
            ),
        )
        in_item = False
        pending_summary = ""
        pending_type = ""
        pending_path = ""
        pending_score = 0.0

    for raw in lines:
        line = raw.rstrip()
        # Item heading: "### N. Type: \"summary\" (score X.XX)"
        if line.startswith("### "):
            # Close prior item before starting a new one.
            _flush()
            in_item = True
            # Parse heading: drop "### N. " prefix, split off score tail.
            heading = line[4:].lstrip()
            # Strip "N. " if present.
            if "." in heading:
                first_dot = heading.find(". ")
                if first_dot != -1 and heading[:first_dot].strip().isdigit():
                    heading = heading[first_dot + 2:]
            # Pull the trailing "(score X.XX)" off if present.
            score_marker = " (score "
            if score_marker in heading:
                head, _, tail = heading.rpartition(score_marker)
                heading = head
                score_str = tail.rstrip(")").strip()
                try:
                    pending_score = float(score_str)
                except ValueError:
                    pending_score = 0.0
            # heading is now "Type: \"summary\"".
            type_part, _, summary_part = heading.partition(": ")
            pending_type = type_part.strip().lower() or "unknown"
            pending_summary = summary_part.strip().strip('"').strip()
            continue

        if not in_item:
            continue

        stripped = line.strip()
        # Path line: "path: /abs/..."
        if stripped.startswith("path: "):
            pending_path = stripped[len("path: "):].strip()
            continue
        # Type line: "type: synthesis  src: ..." — refine record_type
        # from the canonical metadata (the heading-derived value is
        # title-cased and may not exactly match the on-disk type
        # registry; the metadata line is authoritative).
        if stripped.startswith("type: "):
            rest = stripped[len("type: "):].strip()
            # Take the first space-separated token.
            tok = rest.split()[0] if rest else ""
            if tok:
                pending_type = tok
            continue

    _flush()
    return items


def build_batch(
    config: DailySyncConfig,
    today: date,
    *,
    start_index: int = 1,
) -> tuple[str | None, list[RadarItemSummary]]:
    """Return the daily-radar file body + parsed item summaries.

    Returns ``(None, [])`` when:
      - The digests dir hasn't been set (provider not wired by the
        daemon — shouldn't happen in production, defensive for tests).
      - Today's daily file is missing (Phase 3a daemon hasn't fired
        yet today, OR Phase 3a is disabled for this instance).
      - The daily file is empty / malformed.

    On the empty-state daily file (Phase 3a wrote it but with the
    "no radar items today" body), returns ``(body, [])`` — the
    section still renders so the operator sees radar ran-and-
    found-nothing rather than radar-didn't-run. Per
    ``feedback_intentionally_left_blank.md``.
    """
    digests_dir = get_digests_dir()
    if digests_dir is None:
        log.info("daily_sync.radar.digests_dir_unset")
        return None, []
    daily_path = latest_daily_path(digests_dir, today=today)
    if daily_path is None:
        log.info(
            "daily_sync.radar.daily_file_missing",
            digests_dir=str(digests_dir),
            date=today.isoformat(),
        )
        return None, []
    try:
        body = daily_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.info(
            "daily_sync.radar.read_failed",
            path=str(daily_path), error=str(exc),
        )
        return None, []
    items = _parse_daily_file_items(body, start_index=start_index)
    return body, items


def render_batch(
    body: str | None,
    items: list[RadarItemSummary],
    today: date,
) -> str | None:
    """Render the radar section.

    Phase 3a's daily file already carries a ``# Daily radar`` header.
    For the Daily Sync section we strip Phase 3a's header (the
    Daily Sync banner already includes the date) and prepend a
    section-style header.

    Returns ``None`` when ``body`` is ``None`` (no daily file at all).
    Returns the rendered section even when ``items`` is empty — the
    "no radar items today" case is observable, not silent.
    """
    if body is None:
        return None
    # Strip the daily file's top header line "# Daily radar — DATE"
    # if present. Keep the rest verbatim.
    lines = body.splitlines()
    while lines and (
        lines[0].startswith("# Daily radar")
        or lines[0].strip() == ""
    ):
        lines.pop(0)
    inner = "\n".join(lines).rstrip()

    if items:
        header = f"## Distiller radar ({len(items)} item{'s' if len(items) != 1 else ''})"
    else:
        header = "## Distiller radar"
    return f"{header}\n\n{inner}".rstrip()


# ---------------------------------------------------------------------------
# Section provider entry point + registration
# ---------------------------------------------------------------------------


def radar_section(
    config: DailySyncConfig,
    today: date,
    *,
    start_index: int = 1,
) -> str | None:
    """Section provider — reads today's radar file and renders the section.

    Registered with priority 22 (between canonical proposals at 15
    and attribution at 25). Returns ``None`` when no radar file is
    present today so the Daily Sync stays concise on instances that
    don't run the radar.
    """
    body, items = build_batch(config, today, start_index=start_index)
    _LAST_BATCH_HOLDER["items"] = items
    return render_batch(body, items, today)


def register() -> None:
    """Idempotent provider registration. Safe to call multiple times."""
    from . import assembler
    if "radar" in assembler.registered_providers():
        return
    assembler.register_provider(
        "radar",
        priority=22,
        provider=radar_section,
        item_count_after=peek_last_batch_count,
    )


__all__ = [
    "RadarItemSummary",
    "build_batch",
    "consume_last_batch",
    "get_digests_dir",
    "peek_last_batch_count",
    "radar_section",
    "register",
    "render_batch",
    "set_digests_dir",
]
