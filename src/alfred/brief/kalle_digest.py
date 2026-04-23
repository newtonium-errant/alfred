"""KAL-LE morning digest assembler.

Runs ON the KAL-LE instance (not Salem). Builds a one-slide markdown
digest summarising what KAL-LE did yesterday, what's queued today, and
its overall posture. The result gets pushed to Salem's
``/peer/brief_digest`` endpoint by the scheduled pusher (c4) and
appears in Salem's morning brief as the ``### KAL-LE Update`` section.

Code lives under ``brief/`` because it is brief content, even though
it runs on KAL-LE — keeping it in shared code lets STAY-C (and any
future specialist instance) reuse the same shape via its own
``stayc_digest.py`` sibling.

Sections
--------
**Yesterday** (deterministic):
    Up to 5 bullets summarising activity:
      - bash_exec.jsonl event count + cwd breakdown
      - executed instructor directives (from instructor_state.json)
      - git activity in aftermath-lab + aftermath-alfred (commit count)
    Per project_deterministic_writers.md, no LLM call here — the
    counts + names speak for themselves and an LLM "summary" would
    introduce hallucination risk for what's effectively a count of
    log entries.

**Today** (deterministic + soft):
    Up to 3 bullets — pending directives, partial work, queued tasks.
    v1 keeps this deterministic too: list pending instructor directives
    + retry_counts > 0 entries (those will retry today). LLM-assisted
    forward-looking phrasing is a deferred follow-up — easy to slot in
    behind a config flag once the v1 shape is dogfooded.

**Posture** (deterministic):
    - green: no BIT data OR all BIT checks passed yesterday
    - yellow: any BIT WARN
    - red: any BIT FAIL
    KAL-LE doesn't run BIT in v1 (per the rollout ledger), so the
    typical posture line is "green — no BIT data on this instance".
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .utils import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DigestData:
    """Aggregated raw inputs the assembler renders into markdown.

    Exposed for tests so we can assert on the data layer separately
    from the markdown shape (which the brief renderer cares about).
    """

    bash_exec_count: int = 0
    bash_exec_cwd_counts: dict[str, int] = field(default_factory=dict)
    instructor_executed: list[str] = field(default_factory=list)
    instructor_pending: list[str] = field(default_factory=list)
    instructor_retrying: list[str] = field(default_factory=list)
    git_commits_by_repo: dict[str, list[str]] = field(default_factory=dict)
    bit_overall_status: str = ""  # "ok" | "warn" | "fail" | "" (unknown)
    bit_summary_line: str = ""


# ---------------------------------------------------------------------------
# Source readers — each returns a slice of DigestData
# ---------------------------------------------------------------------------


def _yesterday_bounds(today: date) -> tuple[datetime, datetime]:
    """ISO-string-comparable UTC bounds for yesterday (inclusive start,
    exclusive end). Cheap timezone discipline: comparisons happen on
    the .isoformat() prefix so we sidestep the timezone-of-event vs
    timezone-of-bounds edge case for a quick daily pass."""
    yesterday = today - timedelta(days=1)
    start = datetime(
        yesterday.year, yesterday.month, yesterday.day,
        tzinfo=timezone.utc,
    )
    end = start + timedelta(days=1)
    return start, end


def read_bash_exec_log(
    path: Path,
    today: date,
) -> tuple[int, dict[str, int]]:
    """Count bash_exec entries and cwd breakdown for yesterday.

    Returns (total_count, {cwd_basename: count}). Resilient to bad
    JSONL lines — the audit log must never crash the digest assembler.
    """
    if not path.exists():
        return 0, {}
    start, end = _yesterday_bounds(today)
    start_iso = start.isoformat()
    end_iso = end.isoformat()

    total = 0
    cwds: Counter[str] = Counter()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = str(entry.get("ts") or "")
            if ts < start_iso or ts >= end_iso:
                continue
            total += 1
            cwd_raw = str(entry.get("cwd") or "")
            cwd_name = Path(cwd_raw).name if cwd_raw else "(unknown)"
            cwds[cwd_name] += 1
    except OSError as exc:
        log.warning(
            "kalle_digest.bash_exec_read_failed",
            path=str(path),
            error=str(exc),
        )
        return 0, {}
    return total, dict(cwds)


def read_instructor_state(
    path: Path,
    today: date,
) -> tuple[list[str], list[str], list[str]]:
    """Pull (executed_yesterday, pending_today, retrying_today) from
    the instructor state file.

    The instructor_state.json format used today carries:
      - last_run_ts (latest poll wake)
      - retry_counts (dict of file_path → int) — non-zero means retry
        is queued for the next poll
      - file_hashes (dict of file → hash) — not directly useful here,
        but provides the universe of known directive sources

    Executed-yesterday history isn't durably tracked in the state
    file today — the instructor logs each execution to its log file.
    For v1 we approximate executed-yesterday as: directive files
    whose ``last_run_ts`` falls within yesterday's window. When the
    state shape doesn't yet expose that, we return an empty list (the
    Yesterday section degrades gracefully).
    """
    if not path.exists():
        return [], [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return [], [], []
    if not isinstance(data, dict):
        return [], [], []

    # Pending today = retry_counts entries with count > 0.
    retry_counts = data.get("retry_counts") or {}
    retrying: list[str] = []
    if isinstance(retry_counts, dict):
        for file_path, count in retry_counts.items():
            try:
                if int(count) > 0:
                    retrying.append(str(file_path))
            except (TypeError, ValueError):
                continue
    retrying.sort()

    # Pending = directive files in file_hashes that are NOT in any
    # executed/skipped record. The current state schema doesn't track
    # this distinction, so v1 keeps pending as empty unless a future
    # state-shape change populates an explicit list. Returning [] here
    # is correct — the Today section won't lie about pending work.
    pending: list[str] = []

    # Executed-yesterday — same caveat. v1 returns [] until the
    # instructor records per-directive execution timestamps. Documented
    # as a follow-up in the section docstring above.
    executed: list[str] = []

    return executed, pending, retrying


def read_git_commits(
    repo_paths: list[Path],
    today: date,
) -> dict[str, list[str]]:
    """Count commits + collect short messages per repo for yesterday.

    Uses ``git log --since/--until`` against each repo; missing repos
    are silently skipped (e.g. when the dev hasn't checked out
    aftermath-alfred yet). Returns {repo_basename: [short_msg, ...]}.

    Window: bounded by explicit UTC timestamps for the local-yesterday
    interpreted as a 24h UTC slice. Keeps the test + production
    behaviour stable across TZs without needing the caller to set TZ
    in the environment.
    """
    yesterday = today - timedelta(days=1)
    # Explicit UTC bounds — git's --since accepts "YYYY-MM-DD HH:MM Z"
    # form; passing the timezone marker prevents it from re-interpreting
    # the date in the caller's local TZ, which made the test suite
    # flaky in CI vs. dev (one in UTC, one in ADT).
    since = f"{yesterday.isoformat()} 00:00 +0000"
    until = f"{today.isoformat()} 00:00 +0000"
    out: dict[str, list[str]] = {}
    for repo in repo_paths:
        if not (repo / ".git").exists():
            continue
        try:
            proc = subprocess.run(
                [
                    "git", "log",
                    f"--since={since}",
                    f"--until={until}",
                    "--pretty=format:%s",
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.warning(
                "kalle_digest.git_log_failed",
                repo=str(repo),
                error=str(exc),
            )
            continue
        if proc.returncode != 0:
            log.warning(
                "kalle_digest.git_log_nonzero",
                repo=str(repo),
                code=proc.returncode,
                stderr=proc.stderr[:200] if proc.stderr else "",
                stdout_tail=proc.stdout[-500:] if proc.stdout else "",
            )
            continue
        msgs = [m.strip() for m in (proc.stdout or "").splitlines() if m.strip()]
        if msgs:
            out[repo.name] = msgs
    return out


def read_bit_state(path: Path | None) -> tuple[str, str]:
    """Read BIT state file for posture computation.

    Returns (overall_status, summary_line). overall_status is one of
    ``"ok"``, ``"warn"``, ``"fail"``, or ``""`` when no data is
    available (e.g. KAL-LE doesn't run BIT in v1).
    """
    if path is None or not path.exists():
        return "", ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "", ""
    runs = data.get("runs") if isinstance(data, dict) else None
    if not isinstance(runs, list) or not runs:
        return "", ""
    latest = runs[-1]
    if not isinstance(latest, dict):
        return "", ""
    status = str(latest.get("overall_status", "")).lower()
    counts = latest.get("tool_counts") or {}
    summary = ""
    if isinstance(counts, dict) and counts:
        summary = ", ".join(
            f"{v} {k}" for k, v in sorted(counts.items()) if v
        )
    return status, summary


# ---------------------------------------------------------------------------
# Aggregation + render
# ---------------------------------------------------------------------------


def gather_digest_data(
    *,
    today: date,
    bash_exec_log: Path,
    instructor_state: Path,
    repo_paths: list[Path],
    bit_state: Path | None = None,
) -> DigestData:
    """Pull all source data into one ``DigestData`` value object."""
    bash_count, bash_cwds = read_bash_exec_log(bash_exec_log, today)
    executed, pending, retrying = read_instructor_state(instructor_state, today)
    commits = read_git_commits(repo_paths, today)
    bit_status, bit_summary = read_bit_state(bit_state)
    return DigestData(
        bash_exec_count=bash_count,
        bash_exec_cwd_counts=bash_cwds,
        instructor_executed=executed,
        instructor_pending=pending,
        instructor_retrying=retrying,
        git_commits_by_repo=commits,
        bit_overall_status=bit_status,
        bit_summary_line=bit_summary,
    )


def _render_yesterday_section(data: DigestData) -> str:
    """Up to 5 bullets summarising what KAL-LE did yesterday."""
    bullets: list[str] = []

    # Git activity — most-impactful first.
    for repo, msgs in sorted(data.git_commits_by_repo.items()):
        n = len(msgs)
        word = "commit" if n == 1 else "commits"
        # Show first message as a representative; cap length so the
        # bullet stays one line.
        sample = msgs[0]
        if len(sample) > 70:
            sample = sample[:67] + "..."
        bullets.append(f"- {n} {word} in `{repo}` (latest: {sample!r})")

    # bash_exec activity.
    if data.bash_exec_count > 0:
        cwd_summary = ", ".join(
            f"{count} in `{cwd}`"
            for cwd, count in sorted(
                data.bash_exec_cwd_counts.items(),
                key=lambda kv: (-kv[1], kv[0]),
            )[:3]
        )
        bullets.append(
            f"- {data.bash_exec_count} bash_exec runs ({cwd_summary})"
        )

    # Executed instructor directives.
    if data.instructor_executed:
        n = len(data.instructor_executed)
        bullets.append(
            f"- Executed {n} instructor directive{'s' if n != 1 else ''}"
        )

    # Cap at 5 to keep the slide tight.
    bullets = bullets[:5]
    if not bullets:
        return "- No tracked activity yesterday."
    return "\n".join(bullets)


def _render_today_section(data: DigestData) -> str:
    """Up to 3 bullets — what's queued for today."""
    bullets: list[str] = []

    if data.instructor_pending:
        n = len(data.instructor_pending)
        bullets.append(
            f"- {n} instructor directive{'s' if n != 1 else ''} pending"
        )
    if data.instructor_retrying:
        n = len(data.instructor_retrying)
        bullets.append(
            f"- {n} directive{'s' if n != 1 else ''} queued for retry"
        )

    bullets = bullets[:3]
    if not bullets:
        # Honest blank-line: nothing scheduled, ready for new work.
        return "- No queued work; standing by for new directives."
    return "\n".join(bullets)


def _render_posture_line(data: DigestData) -> str:
    """One line — ``green / yellow / red`` + a short phrase."""
    status = data.bit_overall_status
    if status == "fail":
        phrase = data.bit_summary_line or "BIT FAIL recorded"
        return f"**Posture:** red — {phrase}."
    if status == "warn":
        phrase = data.bit_summary_line or "BIT WARN recorded"
        return f"**Posture:** yellow — {phrase}."
    if status == "ok":
        phrase = data.bit_summary_line or "all BIT checks passed"
        return f"**Posture:** green — {phrase}."
    # No BIT data — typical for instances that don't run BIT (KAL-LE
    # in v1 per the rollout ledger). Default to green so the absence
    # of a BIT signal isn't misread as a problem.
    return "**Posture:** green — no BIT data on this instance."


def render_digest_markdown(data: DigestData) -> str:
    """Compose the final one-slide markdown digest body.

    Target: ~200-400 words. Format pinned so Salem's brief renderer
    can rely on the section heading shape.
    """
    parts = [
        "**Yesterday:**",
        _render_yesterday_section(data),
        "",
        "**Today:**",
        _render_today_section(data),
        "",
        _render_posture_line(data),
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Top-level entry — used by c4's scheduled pusher
# ---------------------------------------------------------------------------


def assemble_digest(
    *,
    today: date | None = None,
    data_dir: Path,
    repo_paths: list[Path],
    bit_state_path: Path | None = None,
) -> str:
    """Build today's digest markdown.

    Args:
        today: Date to anchor the digest. ``None`` resolves to
            ``date.today()`` — caller usually passes its tz-aware
            ``date`` so the daemon's timezone discipline stays
            local-pinned.
        data_dir: KAL-LE's data directory (``/home/andrew/.alfred/
            kalle/data/`` in production). Holds bash_exec.jsonl and
            instructor_state.json.
        repo_paths: List of repos to scan for yesterday's commits.
        bit_state_path: Optional BIT state file. When ``None`` (KAL-LE
            v1 default), posture defaults to green with the "no BIT
            data" note.

    Returns:
        The one-slide markdown body. Caller wraps it in a
        ``/peer/brief_digest`` push to Salem.
    """
    today = today or date.today()
    data = gather_digest_data(
        today=today,
        bash_exec_log=data_dir / "bash_exec.jsonl",
        instructor_state=data_dir / "instructor_state.json",
        repo_paths=repo_paths,
        bit_state=bit_state_path,
    )
    return render_digest_markdown(data)
