"""Friction queue section provider — KAL-LE Daily Sync K3 c2.

Reads the friction-event JSONL written by the K3 c1 analyzer
(``friction_analyzer.run_friction_analysis``) and renders unsurfaced
events as a Daily Sync section grouped by category.

Priority: 23 — between radar (22) and attribution audit (25).
Friction items are signal-rich but not actionable per-item in the
same way canonical proposals (15) are; they sit between the daily
radar (informational) and the attribution audit (long-tail
verification).

## Read path

The daemon calls :func:`set_friction_log_path` once at startup to
point the provider at the configured log file (default
``/home/andrew/.alfred/<instance>/data/<instance>_friction_log.jsonl``).
On each fire, the provider:

  1. Loads every event from the friction log.
  2. Loads every event_id from the SIDE INDEX
     ``<friction_log>.surfaced.jsonl`` (a separate file so the events
     log stays immutable / append-only).
  3. Filters events to those NOT in the surfaced index.
  4. Renders by category.
  5. After render, appends the surfaced event_ids + timestamp to the
     side index so the next fire skips them.

Why a side index rather than mutating the events log: the JSONL
events log is append-only by design (audit trail). Mutating rows in
place to set ``surfaced_at`` would break that contract and require
re-writing the whole file each fire. Side index is purely additive.

## Empty state

Per ``feedback_intentionally_left_blank.md``: when there are zero
unsurfaced events, the section still renders with "No friction items
today" so the operator distinguishes "analyzer found nothing new"
from "section provider didn't fire".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from .config import DailySyncConfig

log = structlog.get_logger(__name__)


@dataclass
class FrictionItemSummary:
    """Lightweight summary of a friction event for state-file persistence.

    Mirror of RadarItemSummary's shape — recorded under
    ``last_batch.friction_items`` in the Daily Sync state file so a
    future smart-routing dispatcher can resolve "item N" → event_id.
    Friction items are informational today (no resolution flow); the
    data shape is locked here so the dispatcher hook can land later
    without re-shaping state-file rows.
    """

    item_number: int  # 1-indexed, GLOBAL across Daily Sync sections
    event_id: str
    kind: str
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_number": self.item_number,
            "event_id": self.event_id,
            "kind": self.kind,
            "summary": self.summary,
        }


# Module-level holder for the friction-log path the daemon set at
# startup. Mirrors radar_section's _DIGESTS_DIR_HOLDER.
_LOG_PATH_HOLDER: dict[str, Path] = {}


def set_friction_log_path(log_path: Path) -> None:
    """Configure the friction-log path the section provider reads.

    Daemon calls this once at startup. Tests may call it before
    invoking :func:`friction_section` directly. Idempotent.
    """
    _LOG_PATH_HOLDER["path"] = log_path


def get_friction_log_path() -> Path | None:
    """Return the configured friction-log path, or ``None`` when unset."""
    return _LOG_PATH_HOLDER.get("path")


# Module-level batch holder so the daemon can read items back after
# the assembler runs. Mirrors radar_section / email_section.
_LAST_BATCH_HOLDER: dict[str, list[FrictionItemSummary]] = {"items": []}


def consume_last_batch() -> list[FrictionItemSummary]:
    """Return and clear the most recently-built batch."""
    items = _LAST_BATCH_HOLDER.get("items", [])
    _LAST_BATCH_HOLDER["items"] = []
    return items


def peek_last_batch_count() -> int:
    """Non-destructive count for the assembler's ``item_count_after``
    hook so the next section provider's items number continuously
    after friction items."""
    return len(_LAST_BATCH_HOLDER.get("items", []))


# ---------------------------------------------------------------------------
# Surfaced-side-index helpers
# ---------------------------------------------------------------------------


def _surfaced_index_path(friction_log_path: Path) -> Path:
    """Return the side-index path next to the friction log.

    For ``kalle_friction_log.jsonl`` returns
    ``kalle_friction_log.surfaced.jsonl`` — sibling pair so an
    operator inspecting the data dir sees them together. The
    ``.jsonl`` suffix is dropped if present (so we don't end up with
    ``...jsonl.surfaced.jsonl``); otherwise we just append.
    """
    name = friction_log_path.name
    if name.endswith(".jsonl"):
        stem = name[:-len(".jsonl")]
    else:
        stem = name
    return friction_log_path.with_name(f"{stem}.surfaced.jsonl")


def load_surfaced_event_ids(friction_log_path: Path) -> set[str]:
    """Read the surfaced side-index → set of already-rendered event_ids.

    Returns empty set when the side index doesn't exist (first-fire
    case). Malformed rows skipped.
    """
    side = _surfaced_index_path(friction_log_path)
    if not side.is_file():
        return set()
    ids: set[str] = set()
    try:
        with side.open("r", encoding="utf-8") as fh:
            for line_num, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    log.info(
                        "friction_section.surfaced_skip",
                        line=line_num, error=str(exc),
                    )
                    continue
                eid = row.get("event_id")
                if isinstance(eid, str) and eid:
                    ids.add(eid)
    except OSError as exc:
        log.warning(
            "friction_section.surfaced_read_failed",
            path=str(side), error=str(exc),
        )
        return set()
    return ids


def append_surfaced(
    friction_log_path: Path,
    event_ids: list[str],
) -> None:
    """Append ``{event_id, surfaced_at}`` rows to the side index.

    No-op when ``event_ids`` is empty. Side-index rows carry a
    timestamp so an operator can grep "when did X first surface?"
    without re-running the analyzer.
    """
    if not event_ids:
        return
    side = _surfaced_index_path(friction_log_path)
    side.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    with side.open("a", encoding="utf-8") as fh:
        for eid in event_ids:
            row = {"event_id": eid, "surfaced_at": now_iso}
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# Friction event reader — schema-tolerant
# ---------------------------------------------------------------------------


def load_friction_events(friction_log_path: Path) -> list[dict[str, Any]]:
    """Return every friction event from the log, oldest-first.

    Returns ``[]`` when the log is missing (first-fire case before
    the analyzer has ever run). Malformed rows skipped.
    """
    if not friction_log_path.is_file():
        return []
    events: list[dict[str, Any]] = []
    try:
        with friction_log_path.open("r", encoding="utf-8") as fh:
            for line_num, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    log.info(
                        "friction_section.log_skip",
                        line=line_num, error=str(exc),
                    )
                    continue
                if isinstance(row, dict) and row.get("event_id"):
                    events.append(row)
    except OSError as exc:
        log.warning(
            "friction_section.log_read_failed",
            path=str(friction_log_path), error=str(exc),
        )
        return []
    return events


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def _format_failed_pattern(event: dict[str, Any]) -> str:
    """One-line bullet for a failed_pattern event."""
    prefix = event.get("prefix", "?")
    count = event.get("count", "?")
    suggestion = event.get("suggestion") or ""
    if suggestion:
        return f"- `{prefix}` failed {count} times — {suggestion}"
    return f"- `{prefix}` failed {count} times"


def _format_repeated_pattern(event: dict[str, Any]) -> str:
    """One-line bullet for a repeated_pattern event."""
    command = event.get("command", "?")
    count = event.get("count", "?")
    suggestion = event.get("suggestion") or ""
    if suggestion:
        return f"- You ran `{command}` {count} times — {suggestion}"
    return f"- You ran `{command}` {count} times"


def _format_missing_tool(event: dict[str, Any]) -> str:
    """One-line bullet for a missing_tool event."""
    tool = event.get("tool", "?")
    failed = event.get("failed_command", "")
    if failed:
        return f"- `{tool}` not installed (failed: `{failed}`)"
    return f"- `{tool}` not installed"


def render_batch(
    events: list[dict[str, Any]],
    today: date,
    *,
    start_index: int = 1,
) -> tuple[str, list[FrictionItemSummary]]:
    """Render the friction-queue section.

    Returns ``(rendered_section_text, item_summaries)``. The text is
    always non-empty — empty events list still renders the
    "No friction items today" line per intentionally-left-blank.
    The summary list mirrors the rendered items 1:1 in render order
    so item_number stays consistent with what Andrew sees.

    Categories rendered in fixed order (failed_pattern → repeated_
    pattern → missing_tool) so consecutive Daily Syncs feel
    consistent. Category headers omitted when that category has zero
    events for the day — keeps the bubble compact.
    """
    by_kind: dict[str, list[dict[str, Any]]] = {
        "failed_pattern": [],
        "repeated_pattern": [],
        "missing_tool": [],
    }
    for ev in events:
        kind = ev.get("kind")
        if kind in by_kind:
            by_kind[kind].append(ev)

    if not events:
        # Per intentionally-left-blank: explicit empty-state line.
        return ("## Friction queue\n\nNo friction items today.", [])

    lines: list[str] = ["## Friction queue", ""]
    summaries: list[FrictionItemSummary] = []
    item_no = start_index

    if by_kind["failed_pattern"]:
        lines.append("### Failed-pattern signals")
        for ev in by_kind["failed_pattern"]:
            lines.append(_format_failed_pattern(ev))
            summaries.append(FrictionItemSummary(
                item_number=item_no,
                event_id=str(ev.get("event_id") or ""),
                kind="failed_pattern",
                summary=str(ev.get("prefix") or ""),
            ))
            item_no += 1
        lines.append("")

    if by_kind["repeated_pattern"]:
        lines.append("### Repeated-pattern signals")
        for ev in by_kind["repeated_pattern"]:
            lines.append(_format_repeated_pattern(ev))
            summaries.append(FrictionItemSummary(
                item_number=item_no,
                event_id=str(ev.get("event_id") or ""),
                kind="repeated_pattern",
                summary=str(ev.get("command") or ""),
            ))
            item_no += 1
        lines.append("")

    if by_kind["missing_tool"]:
        lines.append("### Missing tooling")
        for ev in by_kind["missing_tool"]:
            lines.append(_format_missing_tool(ev))
            summaries.append(FrictionItemSummary(
                item_number=item_no,
                event_id=str(ev.get("event_id") or ""),
                kind="missing_tool",
                summary=str(ev.get("tool") or ""),
            ))
            item_no += 1
        lines.append("")

    return ("\n".join(lines).rstrip(), summaries)


# ---------------------------------------------------------------------------
# Section provider entry point + registration
# ---------------------------------------------------------------------------


def friction_section(
    config: DailySyncConfig,
    today: date,
    *,
    start_index: int = 1,
) -> str | None:
    """Section provider — reads friction log, renders unsurfaced events,
    marks them surfaced.

    Registered with priority 23 (between radar at 22 and attribution
    at 25). Returns the rendered section text — even on empty days
    (per intentionally-left-blank). Returns ``None`` only when the
    daemon hasn't wired ``set_friction_log_path`` (defensive guard
    for tests that exercise the provider without going through
    daemon setup).
    """
    log_path = get_friction_log_path()
    if log_path is None:
        log.info("daily_sync.friction.log_path_unset")
        return None

    all_events = load_friction_events(log_path)
    surfaced_ids = load_surfaced_event_ids(log_path)
    fresh = [e for e in all_events if e.get("event_id") not in surfaced_ids]

    rendered, summaries = render_batch(
        fresh, today, start_index=start_index,
    )
    _LAST_BATCH_HOLDER["items"] = summaries

    # Mark these events as surfaced so the next fire skips them.
    # Only the events we actually rendered get marked — empty-state
    # render writes nothing to the side index.
    if summaries:
        append_surfaced(
            log_path,
            [s.event_id for s in summaries if s.event_id],
        )

    return rendered


def register() -> None:
    """Idempotent provider registration. Safe to call multiple times."""
    from . import assembler
    if "friction" in assembler.registered_providers():
        return
    assembler.register_provider(
        "friction",
        priority=23,
        provider=friction_section,
        item_count_after=peek_last_batch_count,
    )


__all__ = [
    "FrictionItemSummary",
    "append_surfaced",
    "consume_last_batch",
    "friction_section",
    "get_friction_log_path",
    "load_friction_events",
    "load_surfaced_event_ids",
    "peek_last_batch_count",
    "register",
    "render_batch",
    "set_friction_log_path",
]
