"""Brief integration — render the "Open Tasks by Tier" section.

Scans ``vault/task/*.md`` at brief-render time, filters open tasks
(status in ``OPEN_STATUSES``), computes ``effective_tier`` per task via
``alfred.tier.compute``, and renders three buckets (T1 / T2 / T3) with
per-task annotations.

Unlike the routine section (which reads a derivative file written by
the routine daemon), the tier section is a **live vault scan**. Tier
is a pure projection of current task records + current time — no
aggregation state, no daily handoff file.

Per ``feedback_intentionally_left_blank.md`` every bucket emits its
header unconditionally; empty buckets emit a sentinel string so the
operator can distinguish "no tasks at this tier" from "broken render."

Render shape per task:

  - Plain (base tier, no annotation):
      ``- [ ] [[task/Reading]] — T3``
  - Escalated (deadline-driven):
      ``- [ ] [[task/RRTS Payroll]] — T2→T1 (due 2026-05-28, 18h)``
  - Priority-derived base (pre-migration tasks):
      ``- [ ] [[task/Some Task]] — T2 (from priority)``
  - Overdue:
      ``- [ ] [[task/Late Task]] — T2→T1 (overdue 2d)``
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import frontmatter  # type: ignore[import-untyped]
import structlog

from alfred.tier.compute import (
    OPEN_STATUSES,
    TierResult,
    compute_effective_tier,
)

log = structlog.get_logger(__name__)


# Section header constant — referenced from the brief daemon's section
# list. Single source of truth so a rename here propagates without
# search-and-replace in daemon.py.
SECTION_HEADER = "Open Tasks by Tier"

_NO_TASKS_OVERALL = (
    "*(no open tasks at any tier — `task/` directory is empty or all "
    "tasks are done/cancelled)*"
)


def _iter_task_records(vault_path: Path) -> list[tuple[Path, dict, str]]:
    """Yield ``(path, frontmatter_dict, name)`` for every task record.

    Walks ``<vault>/task/*.md`` in sorted order. Skips files that fail
    to parse — emits a single log line per failure so operators see
    the skip rather than a silent drop. Does NOT filter by status
    here; status filtering is done at the bucket-population step so
    a future caller (e.g. a CLI ``alfred tier list``) can scan ALL
    tasks without re-walking.
    """
    task_dir = vault_path / "task"
    if not task_dir.is_dir():
        log.info(
            "brief.tier_section.no_task_dir",
            path=str(task_dir),
            detail=(
                "vault/task/ directory does not exist — emitting the "
                "no-tasks-overall sentinel."
            ),
        )
        return []

    out: list[tuple[Path, dict, str]] = []
    for path in sorted(task_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(path))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "brief.tier_section.parse_failed",
                path=str(path),
                error=str(exc),
            )
            continue
        fm = dict(post.metadata or {})
        name = str(fm.get("name") or path.stem)
        out.append((path, fm, name))
    return out


def _is_open(fm: dict[str, Any]) -> bool:
    """Return True if the task's status is in ``OPEN_STATUSES``.

    Missing ``status`` is treated as ``"todo"`` (forward-compat with
    operator-authored task records that omit the field — todo is the
    safest default for "show in queue").
    """
    status = fm.get("status") or "todo"
    if not isinstance(status, str):
        return False
    return status.lower() in OPEN_STATUSES


def _format_due_distance(due: Any, now: datetime) -> str:
    """Render a human-readable time-to-due string.

    Returns ``"3d"``, ``"18h"``, ``"overdue 2d"``, or ``""`` if ``due``
    is missing / unparseable. Hours only surface when ``due`` is today
    (days == 0); otherwise we render days (the operator-facing
    granularity for task deadlines).
    """
    from alfred.tier.compute import _coerce_due_date

    parsed = _coerce_due_date(due)
    if parsed is None:
        return ""
    delta_days = (parsed - now.date()).days
    if delta_days < 0:
        return f"overdue {abs(delta_days)}d"
    if delta_days == 0:
        # Same-day — show hours-to-end-of-day as the granularity. We
        # treat ``due`` as "end of day" for hours calculation since the
        # field is date-only in the vault.
        end_of_day = datetime.combine(parsed, datetime.max.time()).replace(
            tzinfo=now.tzinfo,
        )
        hours = max(0, int((end_of_day - now).total_seconds() // 3600))
        return f"{hours}h"
    return f"{delta_days}d"


def _format_task_line(
    name: str,
    result: TierResult,
    fm: dict[str, Any],
    now: datetime,
) -> str:
    """Render one ``- [ ] [[task/Name]] — T<n> (annotation)`` line.

    The annotation shape depends on the tier-computation outcome:

    - ``base_tier == effective_tier`` AND base was "set" → bare
      ``T<n>`` (no parenthetical).
    - ``base_tier == effective_tier`` AND base was derived from
      priority → ``T<n> (from priority)``.
    - ``base_tier != effective_tier`` → ``T<base>→T<eff> (due <date>,
      <distance>)`` or ``T<base>→T<eff> (overdue <n>d)``.
    """
    wikilink = f"[[task/{name}]]"

    if result.base_tier == result.effective_tier:
        # Non-escalated case. Decide if the base derivation deserves
        # an annotation (priority-fallback) or stays bare.
        if "from priority" in result.reason:
            return f"- [ ] {wikilink} — T{result.base_tier} (from priority)"
        return f"- [ ] {wikilink} — T{result.base_tier}"

    # Escalated case. Compose the deadline-relative annotation.
    distance = _format_due_distance(fm.get("due"), now)
    due_raw = fm.get("due")
    due_str = ""
    if due_raw is not None:
        # Render the date portion only (the field is date-shaped per
        # the schema; if a string slipped through, slice to ISO).
        if isinstance(due_raw, str):
            due_str = due_raw.strip()[:10]
        else:
            due_str = str(due_raw)[:10]

    parts: list[str] = []
    if "overdue" in distance:
        parts.append(distance)
    else:
        if due_str:
            parts.append(f"due {due_str}")
        if distance:
            parts.append(distance)

    annotation = ", ".join(parts) if parts else ""
    arrow = f"T{result.base_tier}→T{result.effective_tier}"
    if annotation:
        return f"- [ ] {wikilink} — {arrow} ({annotation})"
    return f"- [ ] {wikilink} — {arrow}"


def _render_bucket(tier: int, lines: list[str]) -> str:
    """Compose ``### Tier <n>\n\n- [ ] ...\n`` for one bucket.

    Always emits the header — per intentionally-left-blank, the
    operator sees three section headers every brief so an empty bucket
    is distinguishable from a broken render.
    """
    out = [f"### Tier {tier}", ""]
    if not lines:
        out.append(f"*(no open tasks at Tier {tier})*")
        out.append("")
        return "\n".join(out)
    out.extend(lines)
    out.append("")
    return "\n".join(out)


def render_tier_section(
    vault_path: Path,
    now: datetime,
) -> str:
    """Scan ``vault/task/*.md`` and render the brief's tier section.

    ``now`` is the reference instant for tier computation — pass
    ``datetime.now(tz)`` from the brief daemon; tests pass a fixture.
    Always returns a non-empty string per intentionally-left-blank.
    """
    records = _iter_task_records(vault_path)

    open_records: list[tuple[str, TierResult, dict[str, Any]]] = []
    for _path, fm, name in records:
        if not _is_open(fm):
            continue
        result = compute_effective_tier(fm, now)
        open_records.append((name, result, fm))

    # Sort within bucket: by due-date ascending (overdue first, then
    # soonest), then by name. Deterministic + operator-useful — the
    # next deadline surfaces at the top of each bucket.
    def _sort_key(item: tuple[str, TierResult, dict[str, Any]]):
        name, _result, fm = item
        from alfred.tier.compute import _coerce_due_date
        due = _coerce_due_date(fm.get("due"))
        # None-due sorts last within bucket; (1, "") trick — bool
        # comparison: due-present (False == 0) < due-absent (True == 1).
        return (due is None, due or date.max, name.lower())

    buckets: dict[int, list[str]] = {1: [], 2: [], 3: []}
    for name, result, fm in sorted(open_records, key=_sort_key):
        line = _format_task_line(name, result, fm, now)
        # Defensive: an out-of-range effective_tier shouldn't reach
        # this branch (compute clamps to 1/2/3) but if it does, drop
        # the line into the closest valid bucket and log.
        target = result.effective_tier
        if target not in buckets:
            log.warning(
                "brief.tier_section.invalid_effective_tier",
                name=name,
                effective_tier=target,
                base_tier=result.base_tier,
                reason=result.reason,
            )
            target = 3
        buckets[target].append(line)

    if not open_records:
        # Three empty bucket headers + top-level sentinel per
        # intentionally-left-blank so "ran, nothing to do" is
        # distinguishable from "broken."
        log.info(
            "brief.tier_section.no_open_tasks",
            scanned=len(records),
            detail=(
                "task/ directory scanned but no records have status in "
                "{todo, active, blocked}. Emitting unconditional buckets "
                "with sentinels."
            ),
        )
        body = (
            f"{_NO_TASKS_OVERALL}\n\n"
            f"{_render_bucket(1, [])}\n"
            f"{_render_bucket(2, [])}\n"
            f"{_render_bucket(3, [])}"
        )
        return body

    sections = [
        _render_bucket(1, buckets[1]),
        _render_bucket(2, buckets[2]),
        _render_bucket(3, buckets[3]),
    ]
    body = "\n".join(sections)

    log.info(
        "brief.tier_section.rendered",
        scanned=len(records),
        open_count=len(open_records),
        t1=len(buckets[1]),
        t2=len(buckets[2]),
        t3=len(buckets[3]),
    )
    return body


__all__ = ["SECTION_HEADER", "render_tier_section"]
