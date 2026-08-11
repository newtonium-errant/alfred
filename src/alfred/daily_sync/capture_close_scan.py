"""Finding the evidence that a capture-born task was fulfilled (#64).

THE HALF THAT WAS MISSING. The capture pipeline files a ``task/`` when the
operator says he is going to do something. Nothing ever looked for the moment
he did it. This module is the looking: open capture-born tasks on one side,
records that arrived after them on the other, scored against each other by
:mod:`.capture_close_match`.

It reads the vault and NOTHING ELSE — no queue, no cooldown, no card. The
policy about which scored pairs deserve a card lives in
:mod:`.capture_close_proposals`, so this module can be exercised against a
fixture vault without a queue and that one can be exercised without a vault.

## What counts as evidence, and the four directories that never do

Any record whose ``created`` date falls inside the task's window is a
candidate — EXCEPT four directories, excluded by construction rather than by
scoring luck:

``task/``
    A promise is not evidence of its own fulfilment. Worse, the task would
    match ITSELF at 1.0 and every proposal would be a task asking to close
    itself.

``session/``
    THE DANGEROUS ONE. The capture transcript is where the promise was spoken,
    so it contains the task's own words nearly verbatim, and it is created
    moments BEFORE the task. Left in, the single strongest match for every
    capture-born task would be the session that created it — the machine would
    propose closing every task the day it was filed, and be wrong every time.
    That failure would look like the feature working.

``process/`` and ``digests/``
    Auto-generated views. ``process/Captured Tasks.md`` lists every captured
    task's name verbatim, so it matches all of them perfectly and forever. A
    generated index of the promises is not evidence about any of them.

The exclusion is a DENYLIST rather than an allowlist of evidence types on
purpose: a record type nobody has thought of yet should be able to serve as
evidence (that is the whole point of a fuzzy matcher), while these four are
structurally incapable of it.

## Why same-day records count

``created`` is a DATE, not a timestamp — ``vault_create`` stamps
``date.today()``. So a record created hours after the promise carries the same
value as the task. The window is therefore INCLUSIVE at the low end: "he said
he would attach the screenshots and then attached them that afternoon" is the
motivating case, and a strict ``>`` would exclude exactly it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import frontmatter
import structlog

from .capture_close_match import (
    Glossary,
    PendingMatch,
    best_match,
    now_iso,
    query_key,
)
from .capture_close_proposals import CloseCandidate

log = structlog.get_logger(__name__)

#: The ONE grep-able scan event. Fires on EVERY scan including the empty one —
#: this section renders nothing on most days, so this line is what separates
#: "looked, found nothing" from "the daily pass stopped running".
SCAN_EVENT = "daily_sync.capture_close.scan"

#: Statuses that mean a task is still outstanding.
#:
#: MUST AGREE WITH ``telegram.captured_tasks_view._OPEN_STATUSES``. That module
#: renders the operator's ``process/Captured Tasks.md`` view; this one decides
#: which tasks the machine looks at. If they drift, a task sits under the view's
#: "## Open" heading while the scanner does not consider it open — the operator
#: sees a stale promise the machine claims not to see, which is the exact
#: complaint that opened #64. A cross-surface drift pin holds the two together.
OPEN_TASK_STATUSES = frozenset({"todo", "active", "blocked"})

#: Directories whose records can never be fulfilment evidence — see the module
#: docstring for why each one is here. ``session`` is the load-bearing entry.
EXCLUDED_EVIDENCE_DIRS = frozenset({"task", "session", "process", "digests"})


@dataclass(frozen=True)
class OpenTask:
    """One open capture-born task, with the text the matcher scores."""

    rel_path: str
    #: The promise, as text. The ``name`` frontmatter — that is the sentence
    #: capture distilled from what he said — falling back to the file stem.
    text: str
    created: date
    #: True when ``created`` came from the file's mtime because the record
    #: carried no parseable ``created`` field. Counted, not hidden.
    dated_from_mtime: bool = False


@dataclass
class ScanResult:
    """What one pass over the vault found. Every field is reported in the log.

    ``candidates`` carries every pair scoring at or above the FLOOR — including
    the ones below the threshold. That is deliberate: the threshold decision
    belongs to :func:`~.capture_close_proposals.maybe_propose_closes`, which is
    the one place the policy lives and which logs its own reason for each
    candidate it declines. Filtering here would leave that module's
    ``below_threshold`` branch permanently dead in production while its unit
    test kept passing.
    """

    candidates: list[CloseCandidate] = field(default_factory=list)
    #: Near misses, ready to append to the pending store. Written by the
    #: caller so this stays a read-only pass over the vault.
    near_misses: list[PendingMatch] = field(default_factory=list)
    tasks_total: int = 0
    tasks_scanned: int = 0
    tasks_without_evidence: int = 0
    evidence_pool: int = 0
    dated_from_mtime: int = 0


def _as_date(value: Any) -> date | None:
    """Parse a frontmatter date value. ``None`` when it cannot be placed."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _record_date(path: Path, fm: dict[str, Any]) -> tuple[date, bool]:
    """``(created_date, came_from_mtime)``.

    The mtime fallback is deliberate rather than skipping the record. A task
    with no parseable ``created`` is still a real outstanding promise, and
    dropping it silently would make the feature quietly incomplete on exactly
    the records that are already malformed. mtime is a worse date, not a
    missing one — and the fallback is counted in the scan log so a vault where
    it fires constantly is diagnosable.
    """
    parsed = _as_date(fm.get("created"))
    if parsed is not None:
        return parsed, False
    try:
        return datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc,
        ).date(), True
    except OSError:
        return datetime.now(timezone.utc).date(), True


def _display_name(fm: dict[str, Any], stem: str) -> str:
    name = fm.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return stem


def iter_open_capture_tasks(vault_path: str | Path) -> list[OpenTask]:
    """Every open ``created_by_capture`` task, OLDEST FIRST.

    Oldest-first is the ordering the whole bounded pass depends on: both the
    scan cap and the per-run card budget take from the front, so the stalest
    promises — the ones most likely to have been quietly fulfilled — are the
    ones that get looked at and asked about.
    """
    task_dir = Path(vault_path) / "task"
    if not task_dir.is_dir():
        return []
    out: list[OpenTask] = []
    for path in sorted(task_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(path))
        except Exception:  # noqa: BLE001 — one broken record must not sink the pass
            continue
        fm = post.metadata
        if fm.get("created_by_capture") is not True:
            continue
        if str(fm.get("status") or "todo") not in OPEN_TASK_STATUSES:
            continue
        created, from_mtime = _record_date(path, fm)
        out.append(OpenTask(
            rel_path=f"task/{path.name}",
            text=_display_name(fm, path.stem),
            created=created,
            dated_from_mtime=from_mtime,
        ))
    out.sort(key=lambda t: (t.created, t.rel_path))
    return out


def iter_evidence_records(vault_path: str | Path) -> list[tuple[str, str, date]]:
    """``(rel_path, display_name, created)`` for every possible evidence record.

    Read ONCE per scan and re-filtered per task by date, rather than re-walked
    for each task: the windows overlap heavily, and a vault walk per open task
    is the difference between a bounded daily pass and one that scales with the
    product of two growing numbers.
    """
    root = Path(vault_path)
    if not root.is_dir():
        return []
    out: list[tuple[str, str, date]] = []
    for path in sorted(root.rglob("*.md")):
        try:
            rel = path.relative_to(root)
        except ValueError:  # pragma: no cover — rglob cannot leave the root
            continue
        parts = rel.parts
        if not parts or parts[0] in EXCLUDED_EVIDENCE_DIRS:
            continue
        if any(p.startswith(".") or p.startswith("_") for p in parts):
            # Dotfiles and Obsidian/template scaffolding (``_templates``,
            # ``_bases``) are machinery, not records.
            continue
        try:
            post = frontmatter.load(str(path))
        except Exception:  # noqa: BLE001
            continue
        created, _ = _record_date(path, post.metadata)
        out.append((rel.as_posix(), _display_name(post.metadata, path.stem), created))
    return out


def scan_for_closes(
    vault_path: str | Path,
    *,
    threshold: float,
    floor: float,
    window_days: int,
    max_tasks: int,
    glossary: Glossary | None = None,
    now: datetime | None = None,
) -> ScanResult:
    """One bounded pass: open promises against the records that followed them.

    Emits :data:`SCAN_EVENT` exactly once, always — including the empty-vault
    and no-open-tasks cases, which are the common ones.
    """
    when = now or datetime.now(timezone.utc)
    result = ScanResult()

    tasks = iter_open_capture_tasks(vault_path)
    result.tasks_total = len(tasks)
    result.dated_from_mtime = sum(1 for t in tasks if t.dated_from_mtime)
    scanned = tasks[:max_tasks] if max_tasks > 0 else tasks
    result.tasks_scanned = len(scanned)

    if not scanned:
        log.info(
            SCAN_EVENT, tasks_total=result.tasks_total, tasks_scanned=0,
            evidence_pool=0, candidates=0, near_misses=0,
            detail="no open capture-born tasks — nothing to look for evidence of",
        )
        return result

    evidence = iter_evidence_records(vault_path)
    result.evidence_pool = len(evidence)

    ts = now_iso() if now is None else when.isoformat()
    for task in scanned:
        window_end = task.created + timedelta(days=window_days)
        in_window = [
            (rel, name) for (rel, name, created) in evidence
            # Inclusive at BOTH ends: same-day fulfilment is the motivating
            # case (see the module docstring), and the last day of the window
            # is still inside it.
            if task.created <= created <= window_end
        ]
        if not in_window:
            result.tasks_without_evidence += 1
            continue
        match = best_match(task.text, in_window, glossary=glossary)
        if match is None or match.score < floor:
            result.tasks_without_evidence += 1
            continue
        if match.score < threshold:
            # A NEAR MISS. Recorded as evidence that the bar may be too high,
            # never surfaced as a card — and it still goes into ``candidates``
            # so the proposal layer logs its own below-threshold reason.
            result.near_misses.append(PendingMatch(
                ts=ts,
                task_path=task.rel_path,
                task_key=query_key(task.text),
                task_text=task.text,
                evidence_path=match.evidence_path,
                evidence_name=match.evidence_name,
                score=round(match.score, 4),
            ))
        result.candidates.append(CloseCandidate(
            task_path=task.rel_path,
            task_text=task.text,
            evidence_path=match.evidence_path,
            evidence_name=match.evidence_name,
            score=match.score,
            match_source=match.source,
        ))

    log.info(
        SCAN_EVENT,
        tasks_total=result.tasks_total,
        tasks_scanned=result.tasks_scanned,
        evidence_pool=result.evidence_pool,
        candidates=len(result.candidates),
        near_misses=len(result.near_misses),
        tasks_without_evidence=result.tasks_without_evidence,
        dated_from_mtime=result.dated_from_mtime,
        threshold=threshold, floor=floor, window_days=window_days,
    )
    if result.tasks_total > result.tasks_scanned:
        # Named, never silent. A task past the cap is a promise going unnoticed
        # for another day, and this count is how the operator learns the bound
        # is too tight for his backlog. Oldest-first means the ones dropped are
        # the newest, which is the right direction to lose.
        log.info(
            "daily_sync.capture_close.scan_capped",
            tasks_total=result.tasks_total, max_tasks=max_tasks,
            skipped=result.tasks_total - result.tasks_scanned,
            detail="open capture-born tasks beyond the per-run scan cap were "
                   "not looked at this pass",
        )
    return result


__all__ = [
    "EXCLUDED_EVIDENCE_DIRS",
    "OPEN_TASK_STATUSES",
    "OpenTask",
    "SCAN_EVENT",
    "ScanResult",
    "iter_evidence_records",
    "iter_open_capture_tasks",
    "scan_for_closes",
]
