"""Routine aggregator — scan active routine records, write the daily note.

Pure module — no daemon loop, no scheduling. The daemon calls
``run_aggregator_once(config, today)`` once per fire; the brief reads
the resulting file at 06:00. Same loose-coupling pattern as the BIT →
brief handoff: filesystem is the contract.

Output shape (``vault/daily/<date>.md``):

    ---
    type: daily
    date: 2026-05-26
    routines_contributing: [Core Daily, For Self Health, Mondays]
    critical_pending: [Kiki Insulin @ 12:00, ...]
    ---

    ## Critical
    - [ ] Kiki Insulin @ 12:00
    ...

    ## Tracked
    - [ ] Dog Walk *(last: 4 days ago — past 3-day threshold)*
    ...

    ## Aspirational
    - [ ] Reading for pleasure
    ...

Section headers are emitted UNCONDITIONALLY (intentionally-left-blank
principle): if no routines fire today, the file still has all three
headers + a "no routines due today" sentinel, so the operator can
distinguish "ran, nothing to do" from "broken."

Note: ``daily/`` is added to ``vault.dont_scan_dirs`` in the operator
config so the janitor skips this derivative file.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter  # type: ignore[import-untyped]
import structlog

from alfred.brief.renderer import serialize_record

from .cadence import CadenceError, is_due
from .config import RoutineConfig
from .state import RoutineRun, StateManager

log = structlog.get_logger(__name__)


# Priority ordering — Critical surfaces first (medication, time-critical
# care), Tracked next (habits that should be done), Aspirational last
# (nice-to-have). Maps the operator-facing string to a sort key for
# deterministic section ordering.
_PRIORITY_ORDER = {"critical": 0, "tracked": 1, "aspirational": 2}

# Default gap threshold for tracked items when the record omits
# ``warn_after_gap_days``. 5 days is the dispatch-ratified default —
# tunable per-item via the frontmatter field.
DEFAULT_TRACKED_GAP_DAYS = 5


def _iter_routine_records(vault_path: Path) -> list[tuple[Path, dict, str]]:
    """Yield ``(path, frontmatter_dict, name)`` for every active routine.

    Walks ``<vault>/routine/`` (deterministic order via sorted iteration).
    Skips files that fail to parse — emits a single log line per failure
    so operators see the skip rather than a silent drop.

    Records with ``status: archived`` (or anything other than ``active``,
    or missing status — treated as active by default for forward compat
    with operator-authored files) are skipped if explicitly archived.
    """
    routine_dir = vault_path / "routine"
    if not routine_dir.is_dir():
        # Per feedback_intentionally_left_blank: emit signal so absence
        # is distinguishable from broken. ``no_routine_dir`` is what
        # the operator sees on a fresh install before any routines exist.
        log.info("routine.aggregator.no_routine_dir", path=str(routine_dir))
        return []

    out: list[tuple[Path, dict, str]] = []
    for path in sorted(routine_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(path))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "routine.aggregator.parse_failed",
                path=str(path),
                error=str(exc),
            )
            continue
        fm = dict(post.metadata or {})
        if fm.get("status") == "archived":
            continue
        name = str(fm.get("name") or path.stem)
        out.append((path, fm, name))
    return out


def _parse_log_dates(values: Any) -> list[date]:
    """Parse a list of YAML date / ISO-string values into date objects.

    Silently skips entries we can't parse — operator hand-edits sometimes
    introduce bad strings, and dropping just the bad entry is the
    forgiving choice. Each drop emits a debug-level log so the trail
    exists if needed.
    """
    out: list[date] = []
    if not isinstance(values, list):
        return out
    for v in values:
        if isinstance(v, date):
            out.append(v)
            continue
        if isinstance(v, str):
            try:
                out.append(date.fromisoformat(v))
                continue
            except ValueError:
                pass
        log.debug("routine.aggregator.skipping_bad_log_entry", value=repr(v))
    return out


def _format_tracked_annotation(
    item_text: str,
    completion_log: dict,
    warn_threshold: int,
    today: date,
) -> str | None:
    """Compose the gap-annotation string for a tracked item.

    Returns ``"*(last: N days ago — past T-day threshold)*"`` when the
    gap exceeds the threshold; ``"*(last: N days ago)*"`` when within
    threshold; ``"*(no completions yet)*"`` when the log is empty; or
    ``None`` when the threshold is non-positive (operator opted out).

    Annotation is always emitted for tracked items so the operator
    sees the recency state at a glance — per intentionally-left-blank,
    silent "no annotation" is ambiguous with "operator didn't check yet."
    """
    log_dates = _parse_log_dates(completion_log.get(item_text, []))
    if not log_dates:
        return "*(no completions yet)*"
    most_recent = max(log_dates)
    days_since = (today - most_recent).days
    if days_since == 0:
        return "*(done today)*"
    if warn_threshold <= 0:
        return f"*(last: {days_since} days ago)*"
    if days_since > warn_threshold:
        return (
            f"*(last: {days_since} days ago — past "
            f"{warn_threshold}-day threshold)*"
        )
    return f"*(last: {days_since} days ago)*"


def _collect_items_for_today(
    records: list[tuple[Path, dict, str]],
    today: date,
) -> tuple[list[dict], list[str], list[str]]:
    """Group items by priority for today.

    Returns ``(items, routines_contributing, critical_pending)``:
      - ``items``: list of dicts ``{text, priority, annotation, time}``
        — DEDUPLICATED by ``text`` (first occurrence wins; subsequent
        appearances are dropped, preserving the originating routine's
        priority). Same text appearing under different routines is
        common (operator splits a habit across daily + weekly routines).
      - ``routines_contributing``: routine names that fired today.
        Deterministic order — sorted alphabetically.
      - ``critical_pending``: list of "Kiki Insulin @ 12:00" formatted
        strings for the frontmatter ``critical_pending`` field. Sorted
        by time, then text.
    """
    items_by_text: dict[str, dict] = {}
    contributing: set[str] = set()

    for path, fm, name in records:
        cadence = fm.get("cadence")
        try:
            if not is_due(cadence, today):
                continue
        except CadenceError as exc:
            log.warning(
                "routine.aggregator.malformed_cadence",
                path=str(path),
                name=name,
                error=str(exc),
            )
            continue

        contributing.add(name)
        completion_log = fm.get("completion_log") or {}
        if not isinstance(completion_log, dict):
            completion_log = {}

        raw_items = fm.get("items") or []
        if not isinstance(raw_items, list):
            log.warning(
                "routine.aggregator.items_not_list",
                path=str(path),
                name=name,
                items_type=type(raw_items).__name__,
            )
            continue

        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                log.debug(
                    "routine.aggregator.skipping_non_dict_item",
                    path=str(path),
                    name=name,
                    item=repr(raw_item),
                )
                continue
            text = str(raw_item.get("text") or "").strip()
            if not text:
                continue
            if text in items_by_text:
                # First-occurrence-wins dedup; cite the duplicate so the
                # operator can resolve it if intentional.
                continue
            priority = str(raw_item.get("priority") or "tracked").lower()
            if priority not in _PRIORITY_ORDER:
                log.warning(
                    "routine.aggregator.unknown_priority",
                    path=str(path),
                    name=name,
                    priority=priority,
                    fallback="tracked",
                )
                priority = "tracked"

            time_str = ""
            if priority == "critical":
                raw_time = raw_item.get("time")
                if isinstance(raw_time, str) and raw_time.strip():
                    time_str = raw_time.strip()

            annotation: str | None = None
            if priority == "tracked":
                gap_raw = raw_item.get("warn_after_gap_days", DEFAULT_TRACKED_GAP_DAYS)
                try:
                    gap = int(gap_raw)
                except (TypeError, ValueError):
                    gap = DEFAULT_TRACKED_GAP_DAYS
                annotation = _format_tracked_annotation(
                    text, completion_log, gap, today,
                )

            items_by_text[text] = {
                "text": text,
                "priority": priority,
                "annotation": annotation,
                "time": time_str,
            }

    items = list(items_by_text.values())

    critical_pending: list[str] = []
    for item in items:
        if item["priority"] != "critical":
            continue
        if item["time"]:
            critical_pending.append(f"{item['text']} @ {item['time']}")
        else:
            critical_pending.append(item["text"])
    # Stable sort: time-bearing first (sorted by HH:MM string), then text.
    critical_pending.sort(key=lambda s: (0 if "@" in s else 1, s))

    return items, sorted(contributing), critical_pending


def _format_item_line(item: dict) -> str:
    """Render one ``- [ ] ...`` checklist line."""
    text = item["text"]
    suffix_parts: list[str] = []
    if item["priority"] == "critical" and item["time"]:
        suffix_parts.append(f"@ {item['time']}")
    line = f"- [ ] {text}"
    if suffix_parts:
        line += " " + " ".join(suffix_parts)
    if item["annotation"]:
        line += " " + item["annotation"]
    return line


def _render_section(items: list[dict], header: str) -> str:
    """Compose ``## {header}\n\n- [ ] ...`` for one priority bucket.

    Always emits the header — per intentionally-left-blank, the operator
    sees three section headers every day so absence-of-items is
    distinguishable from absence-of-section.
    """
    lines = [f"## {header}", ""]
    if not items:
        lines.append(f"*(no {header.lower()} routines today)*")
        lines.append("")
        return "\n".join(lines)
    for item in items:
        lines.append(_format_item_line(item))
    lines.append("")
    return "\n".join(lines)


def render_daily_body(
    items: list[dict],
    no_routines_overall: bool,
) -> str:
    """Render the body markdown — three sections (Critical / Tracked /
    Aspirational), header always emitted, sentinel when no routines
    are due at all."""
    if no_routines_overall:
        # Three empty section headers + top-level sentinel so the brief
        # reader sees "ran, nothing to do" rather than "broken."
        body = (
            "*(no routines due today)*\n\n"
            "## Critical\n\n"
            "*(no critical routines today)*\n\n"
            "## Tracked\n\n"
            "*(no tracked routines today)*\n\n"
            "## Aspirational\n\n"
            "*(no aspirational routines today)*\n"
        )
        return body

    critical = [i for i in items if i["priority"] == "critical"]
    tracked = [i for i in items if i["priority"] == "tracked"]
    aspirational = [i for i in items if i["priority"] == "aspirational"]
    sections = [
        _render_section(critical, "Critical"),
        _render_section(tracked, "Tracked"),
        _render_section(aspirational, "Aspirational"),
    ]
    return "\n".join(sections)


def run_aggregator_once(
    config: RoutineConfig,
    today: date,
    state_mgr: StateManager | None = None,
) -> str:
    """Scan active routines, write today's daily aggregator note, return
    the vault-relative path.

    ``state_mgr`` is optional — when provided, the run is recorded in
    state. Callers that just want to render (e.g. tests) may pass None.
    """
    vault_path = Path(config.vault_path)
    iso = today.isoformat()
    records = _iter_routine_records(vault_path)

    if not records:
        # Per intentionally-left-blank: emit signal so a stable "no
        # routines configured" state is distinguishable from "broken."
        log.info(
            "routine.aggregator.no_active_routines",
            date=iso,
            scanned_dir=str(vault_path / "routine"),
        )

    items, contributing, critical_pending = _collect_items_for_today(
        records, today,
    )
    no_routines_overall = not items
    if no_routines_overall and records:
        # Records existed but none fired today — still useful signal.
        log.info(
            "routine.aggregator.no_routines_due_today",
            date=iso,
            scanned=len(records),
        )

    body = render_daily_body(items, no_routines_overall)
    fm = {
        "type": "daily",
        "date": iso,
        "routines_contributing": contributing,
        "critical_pending": critical_pending,
    }
    content = serialize_record(fm, body)

    # Write the file (overwrite on stale-tolerated re-runs; the daemon
    # only fires once per day, but CLI re-runs may stomp).
    name = config.output.name_template.replace("{date}", iso)
    rel_path = f"{config.output.directory}/{name}.md"
    file_path = vault_path / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")

    log.info(
        "routine.aggregator.written",
        path=rel_path,
        item_count=len(items),
        critical_count=len(critical_pending),
        routines_contributing=contributing,
    )

    if state_mgr is not None:
        state_mgr.state.add_run(
            RoutineRun(
                date=iso,
                generated_at=datetime.now(timezone.utc).isoformat(),
                vault_path=rel_path,
                routines_contributing=contributing,
                item_count=len(items),
                critical_pending=len(critical_pending),
            ),
            max_history=config.state.max_history,
        )
        state_mgr.save()

    return rel_path


__all__ = [
    "DEFAULT_TRACKED_GAP_DAYS",
    "render_daily_body",
    "run_aggregator_once",
]
