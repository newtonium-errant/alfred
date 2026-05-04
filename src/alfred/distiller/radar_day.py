"""Daily synthesis radar — KAL-LE distiller-radar Phase 3a.

Reuses Phase 2's :func:`synthesis_ranker.rank_synthesis_records` on a
1-day window. Writes the day's top-N to a markdown file under the
configured digests dir and stamps surfaced records into a stateful
JSONL log so the weekly digest can dedup against today's already-seen
items.

Design ratified in ``project_kalle_radar_phase3.md`` (2026-05-02).

## Output channel

- File: ``<digests_dir>/daily/YYYY-MM-DD.md`` — one file per day.
  Empty days still emit a file with an explicit "no radar items
  today" line per ``feedback_intentionally_left_blank.md``: silence
  is ambiguous, an explicit empty-state is observable.

- Surfaced log: ``<state_dir>/radar_surfaced.jsonl`` — append-only
  JSONL. One row per item ever rendered, keyed by record path.
  Doubles as audit trail ("what radar items has KAL-LE flagged this
  month?") AND as the dedup substrate for Phase 3a + Phase 2's
  weekly digest.

## Dedup contract

The weekly synthesis digest (Phase 2, ``rank_synthesis_records``) does
NOT itself read the surfaced log. The integration point is the digest
WRITER (``digest/writer.py``), which calls
:func:`load_surfaced_paths` before assembling section 4 and filters
the ranker's output. Phase 3a writes the log; Phase 2's writer is
updated in a follow-up commit to read it.

Phase 3a's CLI is the inverse: it reads the log to skip records
already surfaced in earlier daily fires (so a record that appeared on
Tuesday doesn't re-surface every day for the rest of the week, even
if its score stays in the top-5).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from .synthesis_ranker import (
    RankedRecord,
    rank_synthesis_records,
    summary_from_record,
)

log = structlog.get_logger(__name__)

# Default top-N for the daily window. Smaller than weekly's 12 because
# one day's signals are sparser; surfacing 12 from a day's worth of
# corpus produces noise. Per Phase 3 memo.
_DEFAULT_TOP_N: int = 5

# Default ``min_score`` floor — when set, items below this don't surface
# even if they fit under top_n. Prevents "best-of-nothing" days where
# the highest-ranked item is still trivially low. ``None`` disables.
_DEFAULT_MIN_SCORE: float | None = None

# Standard subpath under the surfaced-log's parent that the daily
# render lands in. Mirrors Phase 2's digest writer convention.
_DAILY_SUBDIR: str = "daily"

# Surfaced-log filename. Lives in the state dir so it's sibling to
# distiller_state.json + distiller_backfill_state.json.
_SURFACED_LOG_NAME: str = "radar_surfaced.jsonl"


@dataclass
class DailyRadarResult:
    """Outcome summary returned by :func:`run_daily_radar`.

    Used by the CLI to render a one-screen ops summary AND by tests to
    assert on the rendered file path / item count without re-reading
    the file. ``items`` is the post-dedup list actually rendered;
    ``ranker_count`` is the pre-dedup count from the ranker (so an
    operator can see when dedup is heavily filtering).
    """

    date: str  # ISO YYYY-MM-DD
    items: list[RankedRecord] = field(default_factory=list)
    ranker_count: int = 0
    output_path: Path | None = None
    surfaced_log_path: Path | None = None
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Surfaced-log helpers
# ---------------------------------------------------------------------------


def load_surfaced_paths(surfaced_log: Path) -> set[str]:
    """Read the surfaced log and return the set of vault-relative or
    absolute record paths already surfaced.

    Returns an empty set when the log is missing — first-run behavior
    is "nothing surfaced yet, surface everything that ranks."

    Per-row schema::

        {"date": "2026-05-02", "path": "/abs/path/to/record.md",
         "score": 9.3, "type": "synthesis"}

    Malformed rows are skipped (logged at info, not warning) — a
    corrupt single line shouldn't poison the whole dedup gate.
    """
    if not surfaced_log.is_file():
        return set()
    paths: set[str] = set()
    try:
        with surfaced_log.open("r", encoding="utf-8") as fh:
            for line_num, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    log.info(
                        "radar_day.surfaced_log_skip",
                        line=line_num, error=str(exc),
                    )
                    continue
                p = row.get("path")
                if isinstance(p, str) and p:
                    paths.add(p)
    except OSError as exc:
        log.info(
            "radar_day.surfaced_log_read_failed",
            path=str(surfaced_log), error=str(exc),
        )
        return set()
    return paths


def append_surfaced(
    surfaced_log: Path,
    items: list[RankedRecord],
    today: date,
) -> None:
    """Append one row per item to the surfaced log.

    Atomic-ish: opens append mode, writes one line per item, flushes.
    The log is one writer (the daily radar daemon/CLI) so we don't
    need fsync — losing a row at the crash boundary just causes a
    duplicate surface on the next run, which is preferable to losing
    audit trail.

    Creates the parent dir if missing.
    """
    if not items:
        return
    surfaced_log.parent.mkdir(parents=True, exist_ok=True)
    today_iso = today.isoformat()
    rows: list[str] = []
    for r in items:
        row = {
            "date": today_iso,
            "path": str(r.path),
            "score": round(r.score, 4),
            "type": r.record_type,
            "surfaced_at": datetime.now(timezone.utc).isoformat(),
        }
        rows.append(json.dumps(row, separators=(",", ":")))
    with surfaced_log.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(row + "\n")


# ---------------------------------------------------------------------------
# Ranker reuse + dedup gate
# ---------------------------------------------------------------------------


def rank_day(
    vault_path: Path,
    *,
    top_n: int = _DEFAULT_TOP_N,
    min_score: float | None = _DEFAULT_MIN_SCORE,
    surfaced_paths: set[str] | None = None,
    weights: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[list[RankedRecord], int]:
    """Rank one day's worth of records, dedup against ``surfaced_paths``,
    apply ``min_score`` floor, return ``(items, raw_ranker_count)``.

    ``surfaced_paths`` is the set of record path strings already
    surfaced in prior daily fires (read from the surfaced log via
    :func:`load_surfaced_paths`). When ``None`` or empty, no dedup
    happens — first-run / fresh-install behavior.

    ``min_score`` is the optional floor below which items are dropped
    even if ``top_n`` permits. ``None`` disables the floor.

    Returns the ranked list AFTER dedup and floor; the second element
    is the pre-dedup ranker count so callers can log the filter ratio.

    Note: ``window_days=1`` is hardcoded — this function exists
    specifically to be the Phase 3a daily wrapper. Callers that want
    other windows go straight to :func:`rank_synthesis_records`.
    """
    raw = rank_synthesis_records(
        vault_path,
        # Pull a wider candidate pool than top_n so dedup leaves us a
        # full ``top_n`` after filtering. 4× is generous for a daily
        # window (most days won't have 20 ranked items).
        window_days=1,
        top_n=max(top_n * 4, top_n),
        weights=weights,
        now=now,
    )
    raw_count = len(raw)

    surfaced = surfaced_paths or set()
    filtered: list[RankedRecord] = []
    for r in raw:
        if str(r.path) in surfaced:
            continue
        if min_score is not None and r.score < min_score:
            continue
        filtered.append(r)
        if len(filtered) >= top_n:
            break
    return filtered, raw_count


# ---------------------------------------------------------------------------
# Render template
# ---------------------------------------------------------------------------


def render_daily_file(
    items: list[RankedRecord],
    today: date,
    *,
    ranker_count: int = 0,
) -> str:
    """Render the daily radar markdown file.

    Empty case (zero items): emits an explicit "no radar items today"
    block per ``feedback_intentionally_left_blank.md`` — silence is
    ambiguous, observable empty-state lets the operator distinguish
    "the daemon ran" from "the daemon broke."

    Format mirrors Phase 2's section 4 style:

        # Daily radar — 2026-05-02

        ## Top 3 (4 ranked, 1 deduped)

        ### 1. Synthesis: "Andrew prefers..." (score 9.30)
            type: synthesis  src: 3  ent: 4  age: 0.42d
            path: /abs/path/to/record.md
            cross_source=9.00  entity_diversity=8.00  recency=1.00  type_weight=3.00

        ### 2. ...
    """
    today_iso = today.isoformat()
    lines: list[str] = [f"# Daily radar — {today_iso}", ""]

    if not items:
        lines.append(
            "no radar items today (corpus checked: "
            "synthesis/, decision/, contradiction/)"
        )
        lines.append("")
        lines.append(f"_ranker scanned {ranker_count} candidate(s)._")
        return "\n".join(lines).rstrip() + "\n"

    deduped = max(0, ranker_count - len(items))
    suffix = f", {deduped} deduped" if deduped > 0 else ""
    lines.append(f"## Top {len(items)} ({ranker_count} ranked{suffix})")
    lines.append("")

    for i, r in enumerate(items, start=1):
        summary = summary_from_record(r.frontmatter, r.body)
        # Strip newlines + collapse whitespace for the heading. Long
        # summaries land on the metadata lines below where they get
        # full-width room; keep the heading scannable.
        if summary:
            heading_summary = " ".join(summary.split())
            if len(heading_summary) > 100:
                heading_summary = heading_summary[:97] + "..."
        else:
            heading_summary = "(no claim)"
        record_type_title = r.record_type.title() if r.record_type else "Record"
        lines.append(
            f'### {i}. {record_type_title}: "{heading_summary}" '
            f"(score {r.score:.2f})"
        )
        age = "-" if r.age_days is None else f"{r.age_days:.2f}d"
        lines.append(
            f"    type: {r.record_type}  src: {r.source_count}  "
            f"ent: {r.entity_count}  age: {age}"
        )
        lines.append(f"    path: {r.path}")
        b = r.breakdown
        lines.append(
            f"    cross_source={b.cross_source:.2f}  "
            f"entity_diversity={b.entity_diversity:.2f}  "
            f"recency={b.recency:.2f}  type_weight={b.type_weight:.2f}"
        )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _atomic_write(path: Path, content: str) -> None:
    """Atomic-ish write: tmp → rename. Creates parent dirs first.

    Using NamedTemporaryFile with delete=False so the rename completes
    even if the process is killed between write + rename — at worst a
    leftover .tmp file lands under daily/, which is harmless.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup of the tmp file on any failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Top-level orchestration — used by the CLI + the daemon (when added)
# ---------------------------------------------------------------------------


def run_daily_radar(
    vault_path: Path,
    digests_dir: Path,
    state_dir: Path,
    *,
    top_n: int = _DEFAULT_TOP_N,
    min_score: float | None = _DEFAULT_MIN_SCORE,
    today: date | None = None,
    dry_run: bool = False,
    weights: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> DailyRadarResult:
    """End-to-end daily radar: rank → render → write file → append log.

    Args:
        vault_path: KAL-LE's vault root (typically ``aftermath-lab/``).
            Passed straight to :func:`rank_synthesis_records`.
        digests_dir: Where ``daily/YYYY-MM-DD.md`` lands. Typically
            ``aftermath-lab/digests/``. The function creates
            ``digests_dir/daily/`` if missing.
        state_dir: Where ``radar_surfaced.jsonl`` lives. Typically
            KAL-LE's data dir (``/home/andrew/.alfred/kalle/data``).
        top_n: Max items to surface (default 5).
        min_score: Optional score floor (default None — no floor).
        today: Override for tests. Defaults to ``date.today()``.
        dry_run: When True, computes and renders the markdown but
            does NOT write the file or append the surfaced log. The
            returned :class:`DailyRadarResult` carries the rendered
            content path that WOULD have been written.

    Returns: :class:`DailyRadarResult` summary.
    """
    today = today or date.today()
    today_iso = today.isoformat()

    surfaced_log = state_dir / _SURFACED_LOG_NAME
    surfaced = load_surfaced_paths(surfaced_log)

    items, ranker_count = rank_day(
        vault_path,
        top_n=top_n,
        min_score=min_score,
        surfaced_paths=surfaced,
        weights=weights,
        now=now,
    )

    output_path = digests_dir / _DAILY_SUBDIR / f"{today_iso}.md"
    rendered = render_daily_file(items, today, ranker_count=ranker_count)

    if not dry_run:
        _atomic_write(output_path, rendered)
        append_surfaced(surfaced_log, items, today)
        log.info(
            "radar_day.fired",
            date=today_iso,
            items_count=len(items),
            ranker_count=ranker_count,
            deduped=max(0, ranker_count - len(items)),
            output=str(output_path),
        )
    else:
        log.info(
            "radar_day.dry_run",
            date=today_iso,
            items_count=len(items),
            ranker_count=ranker_count,
            would_write=str(output_path),
        )

    return DailyRadarResult(
        date=today_iso,
        items=items,
        ranker_count=ranker_count,
        output_path=output_path,
        surfaced_log_path=surfaced_log,
        dry_run=dry_run,
    )


def latest_daily_path(digests_dir: Path, today: date | None = None) -> Path | None:
    """Return the path to today's daily-radar file, or None if absent.

    Used by Phase 3b's section provider to read today's already-rendered
    items without re-running the ranker. Returns ``None`` when the
    file isn't there (e.g. the daemon hasn't fired yet today, or it's
    explicitly disabled).
    """
    today = today or date.today()
    candidate = digests_dir / _DAILY_SUBDIR / f"{today.isoformat()}.md"
    return candidate if candidate.is_file() else None


__all__ = [
    "DailyRadarResult",
    "append_surfaced",
    "latest_daily_path",
    "load_surfaced_paths",
    "rank_day",
    "render_daily_file",
    "run_daily_radar",
]
