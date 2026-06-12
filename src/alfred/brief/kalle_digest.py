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

**Ticket pipeline** (deterministic, pipeline c5):
    The VERA→KAL-LE→GitHub ticket pipeline's morning surface +
    effectiveness-loop capture. Assembled by
    :func:`assemble_ticket_pipeline_section` (async — the github_ops
    client is async) and appended to the digest by the pusher daemon's
    ``fire_once``. Rendered EVERY digest — an idle pipeline renders an
    explicit quiet line per ``feedback_intentionally_left_blank.md``.
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


# ---------------------------------------------------------------------------
# Ticket pipeline section (pipeline c5) — effectiveness-loop capture + render
# ---------------------------------------------------------------------------
#
# The VERA→KAL-LE→GitHub ticket pipeline's morning surface (ratified
# 2026-06-11 incl. the effectiveness-loop amendment — the PAT carries
# Pull requests: Read precisely for this). Two halves:
#
#   1. CAPTURE — for every intake entry with a GitHub issue whose
#      disposition isn't terminal, read the linked-PR state via
#      github_ops (caller="digest", the read-only allowlist rows) and
#      fill pr_number / pr_state / disposition /
#      ticket_to_pr_latency_days / outcome_checked_at on the intake
#      state. CAPTURE ONLY: the distiller-mining / meta-issue-proposal
#      half is a LATER phase by design — nothing here proposes anything.
#   2. RENDER — counts, per-ticket status lines, and the auto-fix
#      scoreboard, all purely from the (just-updated) state.
#
# PR-DISCOVERY STRATEGY (one deterministic strategy, documented): scan
# the issue timeline (GitHub returns it oldest-first) for the FIRST
# ``cross-referenced`` event whose source is a pull request
# (``source.issue.pull_request`` present) and take that PR's number.
# ``issue_get``'s ``pull_request`` field was REJECTED as the primary
# strategy: that field only exists when the issue itself IS a PR, never
# when a PR merely references the issue — useless for "which PR fixes
# this ticket". Once a PR number is captured in state, later checks
# skip timeline discovery and go straight to pr_get (cheaper, and
# stable against later unrelated cross-references).
#
# Concurrency note: the intake handler (talker process) and this
# assembler (brief_digest_push process) share the state file via
# load-modify-save; a push landing inside the seconds-long check pass
# could be clobbered. Tolerated by design — intake state is deletable
# bookkeeping (CLAUDE.md) and the intake's marker-search guard recovers
# issue linkage on VERA's next re-push, so the worst case is a
# redundant GitHub search, never a duplicate issue.

# Terminal dispositions LATCH (watches.py terminal-latch idiom): once
# set, the entry is never queried again and renders only on digests
# within 24h of the flip (outcome_checked_at freezes at flip time).
TICKET_TERMINAL_DISPOSITIONS = frozenset({
    "merged_clean",
    "merged_after_rework",
    "closed_unmerged",
})

# "stalled" threshold: issue open with no merged/closed PR for at least
# this many nights. NON-terminal — a stalled ticket keeps being
# re-checked every digest and can still upgrade to merged_*.
TICKET_STALLED_NIGHTS = 3


def _parse_iso_ts(value: Any) -> datetime | None:
    """Defensive ISO-8601 parse → aware-UTC datetime, or None.

    Handles both this codebase's ``+00:00`` suffixes and GitHub's
    ``Z`` suffixes (``datetime.fromisoformat`` only learned ``Z`` in
    3.11 — normalise rather than assume the interpreter).
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _nights_since(ts: Any, now: datetime) -> int | None:
    """Whole midnights-crossed since ``ts`` (UTC dates), or None."""
    parsed = _parse_iso_ts(ts)
    if parsed is None:
        return None
    return max(0, (now.astimezone(timezone.utc).date() - parsed.date()).days)


def _first_cross_referenced_pr(timeline: Any) -> int | None:
    """The PR-discovery strategy (see the section comment above)."""
    if not isinstance(timeline, list):
        return None
    for event in timeline:
        if not isinstance(event, dict):
            continue
        if event.get("event") != "cross-referenced":
            continue
        source = event.get("source")
        if not isinstance(source, dict):
            continue
        issue = source.get("issue")
        if not isinstance(issue, dict):
            continue
        if not issue.get("pull_request"):
            continue  # cross-reference from a plain issue — not a PR
        number = issue.get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            return number
    return None


async def _check_one_ticket_outcome(
    client: Any,
    entry: Any,
    *,
    now: datetime,
) -> bool:
    """Evaluate one non-terminal issue-bearing entry against GitHub.

    Mutates the entry in place (the caller saves state) and returns
    True when the disposition CHANGED. Disposition rules (ratified):
      * PR merged + zero CHANGES_REQUESTED reviews → ``merged_clean``
      * PR merged + any CHANGES_REQUESTED review → ``merged_after_rework``
      * PR closed without merge → ``closed_unmerged``
      * no PR (or PR still open) AND the issue is
        ``TICKET_STALLED_NIGHTS``+ nights old → ``stalled`` (non-terminal)
      * else → leave as-is (``""`` stays ``""``)
    """
    pr_number = entry.pr_number
    if pr_number is None:
        timeline = await client.issue_timeline(
            number=entry.issue_number, caller="digest",
        )
        pr_number = _first_cross_referenced_pr(timeline)

    old_disposition = entry.disposition
    new_disposition = old_disposition
    pr_state = entry.pr_state
    latency = entry.ticket_to_pr_latency_days

    if pr_number is None:
        nights = _nights_since(entry.issue_created_at, now)
        if nights is not None and nights >= TICKET_STALLED_NIGHTS:
            new_disposition = "stalled"
    else:
        pr = await client.pr_get(number=pr_number, caller="digest")
        pr = pr if isinstance(pr, dict) else {}
        merged_at = pr.get("merged_at")
        if merged_at:
            pr_state = "merged"
            reviews = await client.pr_reviews(
                number=pr_number, caller="digest",
            )
            reviews = reviews if isinstance(reviews, list) else []
            rework = any(
                isinstance(review, dict)
                and review.get("state") == "CHANGES_REQUESTED"
                for review in reviews
            )
            new_disposition = (
                "merged_after_rework" if rework else "merged_clean"
            )
            created_dt = _parse_iso_ts(entry.issue_created_at)
            merged_dt = _parse_iso_ts(merged_at)
            if created_dt is not None and merged_dt is not None:
                latency = round(
                    (merged_dt - created_dt).total_seconds() / 86400.0, 2,
                )
        elif str(pr.get("state") or "") == "closed":
            pr_state = "closed"
            new_disposition = "closed_unmerged"
        else:
            pr_state = "open"
            nights = _nights_since(entry.issue_created_at, now)
            if nights is not None and nights >= TICKET_STALLED_NIGHTS:
                new_disposition = "stalled"

    entry.pr_number = pr_number
    entry.pr_state = pr_state
    entry.disposition = new_disposition
    entry.ticket_to_pr_latency_days = latency
    # Set on every COMPLETED evaluation (changed or not); a failed check
    # leaves the old value so staleness stays visible.
    entry.outcome_checked_at = now.isoformat()
    return new_disposition != old_disposition


async def check_ticket_outcomes(
    state: Any,
    client: Any,
    *,
    now: datetime,
    audit_log_path: str,
) -> int:
    """The effectiveness-loop capture pass. Returns the changed count.

    Per-ticket containment: one bad issue's API failure logs + skips
    that entry, never the rest. Each entry whose disposition CHANGED
    gets ONE summarising audit row (the client already audits every
    underlying HTTP call; this row carries the derived effectiveness
    fields via ``extra``).
    """
    from alfred.integrations.github_ops import append_github_audit

    changed_count = 0
    for uid in sorted(state.entries):
        entry = state.entries[uid]
        if entry.issue_number is None:
            continue  # no issue yet — nothing to check against
        if entry.disposition in TICKET_TERMINAL_DISPOSITIONS:
            continue  # terminal latch — never queried again
        try:
            changed = await _check_one_ticket_outcome(
                client, entry, now=now,
            )
        except Exception as exc:  # noqa: BLE001 — per-ticket containment
            log.warning(
                "kalle.digest.ticket_pipeline_outcome_check_failed",
                uid=uid,
                issue_number=entry.issue_number,
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            continue
        log.info(
            "kalle.digest.ticket_pipeline_outcome_checked",
            uid=uid,
            disposition=entry.disposition,
            pr_number=entry.pr_number,
            pr_state=entry.pr_state,
            changed=changed,
        )
        if changed:
            changed_count += 1
            append_github_audit(
                audit_log_path,
                op="issue_get",
                repo=str(getattr(client.config, "repo", "")),
                caller="digest",
                outcome="ok",
                ticket_uid=uid,
                issue_number=entry.issue_number,
                extra={
                    "pr_number": entry.pr_number,
                    "pr_state": entry.pr_state,
                    "disposition": entry.disposition,
                    "ticket_to_pr_latency_days": (
                        entry.ticket_to_pr_latency_days
                    ),
                },
            )
    return changed_count


def render_ticket_pipeline_section(
    state: Any,
    *,
    now: datetime,
    outcome_note: str = "",
) -> str:
    """Render the Ticket pipeline section purely from intake state.

    Counts are state-derived only: dedupe hits live solely in the
    github_ops audit JSONL (``outcome="exists"`` rows) and parsing that
    log here isn't worth it — deliberately skipped (c5 decision).
    """
    if not state.entries:
        return "Ticket pipeline: no tickets received yet; GitHub ops idle."

    cutoff = now - timedelta(hours=24)
    received_24h = 0
    created_24h = 0
    pending_retry = 0
    last_activity: datetime | None = None
    for entry in state.entries.values():
        recorded = _parse_iso_ts(entry.recorded_at)
        created = _parse_iso_ts(entry.issue_created_at)
        if recorded is not None and recorded >= cutoff:
            received_24h += 1
        if created is not None and created >= cutoff:
            created_24h += 1
        if entry.retry_count > 0 and entry.issue_number is None:
            pending_retry += 1
        for ts in (recorded, created):
            if ts is not None and (last_activity is None or ts > last_activity):
                last_activity = ts

    lines = ["**Ticket pipeline:**"]
    counts_line = (
        f"- Last 24h: {received_24h} received, {created_24h} "
        f"issue{'s' if created_24h != 1 else ''} created"
    )
    if received_24h == 0 and created_24h == 0 and last_activity is not None:
        counts_line += f" (idle since {last_activity.date().isoformat()})"
    lines.append(counts_line)
    if pending_retry:
        lines.append(
            f"- {pending_retry} issue post(s) pending retry — "
            "see github_ops_audit"
        )
    if outcome_note:
        lines.append(f"- ({outcome_note})")

    # Per-ticket status lines, deterministic issue-number order.
    issue_entries = sorted(
        (e for e in state.entries.values() if e.issue_number is not None),
        key=lambda e: e.issue_number,
    )
    for entry in issue_entries:
        if entry.disposition in TICKET_TERMINAL_DISPOSITIONS:
            # Loud-once: terminal entries render only while
            # outcome_checked_at (frozen at flip time) is <24h old.
            checked = _parse_iso_ts(entry.outcome_checked_at)
            if checked is None or checked < cutoff:
                continue
            if entry.disposition == "closed_unmerged":
                lines.append(
                    f"- GH#{entry.issue_number} → PR#{entry.pr_number} "
                    "CLOSED unmerged"
                )
            else:
                latency = (
                    f" ({entry.ticket_to_pr_latency_days:.1f}d)"
                    if entry.ticket_to_pr_latency_days is not None
                    else ""
                )
                lines.append(
                    f"- GH#{entry.issue_number} → PR#{entry.pr_number} "
                    f"MERGED ✓{latency}"
                )
        elif entry.pr_number is not None:
            lines.append(
                f"- GH#{entry.issue_number} → PR#{entry.pr_number} OPEN — "
                "review this morning"
            )
        else:
            nights = _nights_since(entry.issue_created_at, now)
            age = (
                f"{nights} night{'s' if nights != 1 else ''}"
                if nights is not None
                else "age unknown"
            )
            stalled = " — stalled" if entry.disposition == "stalled" else ""
            lines.append(
                f"- GH#{entry.issue_number} → no PR yet "
                f"(issue open {age}){stalled}"
            )

    # Scoreboard over ALL terminal entries, split by ticket_type (the
    # c3-captured state field; pre-c5 entries carry "" → "unspecified").
    terminal = [
        e for e in state.entries.values()
        if e.disposition in TICKET_TERMINAL_DISPOSITIONS
    ]
    if not terminal:
        lines.append("- Auto-fix scoreboard: no outcomes yet.")
    else:
        by_type: dict[str, list[int]] = {}
        for entry in terminal:
            bucket = by_type.setdefault(
                getattr(entry, "ticket_type", "") or "unspecified", [0, 0],
            )
            bucket[1] += 1
            if entry.disposition in ("merged_clean", "merged_after_rework"):
                bucket[0] += 1
        parts = [
            f"{ticket_type} {merged}/{total} merged"
            for ticket_type, (merged, total) in sorted(by_type.items())
        ]
        lines.append("- Auto-fix scoreboard: " + " · ".join(parts))
    return "\n".join(lines)


def _instance_name_from_raw(raw: dict[str, Any]) -> str:
    """Tolerant read of ``telegram.instance.name`` (the identity the
    github_ops instance gate compares against)."""
    telegram = raw.get("telegram")
    telegram = telegram if isinstance(telegram, dict) else {}
    instance = telegram.get("instance")
    instance = instance if isinstance(instance, dict) else {}
    return str(instance.get("name") or "")


async def assemble_ticket_pipeline_section(
    raw: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> str:
    """Build the Ticket pipeline digest section (check + render).

    §-boundary containment (the weather/watches idiom): ANY failure in
    here renders an explicit "section unavailable" line and never kills
    the digest. The no-credential path (``github:`` absent, or the
    instance gate rejecting — e.g. a non-KAL-LE instance running this
    assembler) still renders the state-derived counts/lines, plus one
    quiet note that the PR outcome check was skipped.
    """
    try:
        from alfred.integrations.github_ops import (
            GitHubOpsNotConfigured,
            GitHubOpsWrongInstance,
            build_github_client,
        )
        from alfred.transport.ticket_intake import (
            TicketIntakeState,
            load_ticket_intake_config,
        )

        now = now or datetime.now(timezone.utc)
        raw = raw if isinstance(raw, dict) else {}
        intake_config = load_ticket_intake_config(raw)
        state = TicketIntakeState.load(intake_config.state_path)
        if not state.entries:
            # ILB: the quiet line + a grep-able log — idle, not broken.
            log.info(
                "kalle.digest.ticket_pipeline_idle",
                state_path=intake_config.state_path,
            )
            return render_ticket_pipeline_section(state, now=now)

        outcome_note = ""
        client = None
        if not isinstance(raw.get("github"), dict):
            # No github: section at all (every non-KAL-LE instance) —
            # skip the client build entirely rather than spraying
            # outcome="denied" audit rows into the default audit path
            # on every digest.
            log.info(
                "kalle.digest.ticket_pipeline_outcome_check_unavailable",
                reason="github_section_absent",
            )
            outcome_note = "outcome check unavailable: github not configured"
        else:
            try:
                client = build_github_client(
                    raw, _instance_name_from_raw(raw),
                )
            except (GitHubOpsNotConfigured, GitHubOpsWrongInstance) as exc:
                log.info(
                    "kalle.digest.ticket_pipeline_outcome_check_unavailable",
                    reason=exc.__class__.__name__,
                )
                outcome_note = (
                    "outcome check unavailable: github not configured"
                )

        if client is not None:
            await check_ticket_outcomes(
                state,
                client,
                now=now,
                audit_log_path=client.config.audit_log_path,
            )
            # Atomic save — outcome_checked_at moved even when no
            # disposition flipped.
            state.save()

        return render_ticket_pipeline_section(
            state, now=now, outcome_note=outcome_note,
        )
    except Exception as exc:  # noqa: BLE001 — §-boundary: never kill the digest
        log.warning(
            "kalle.digest.ticket_pipeline_section_failed",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return (
            f"Ticket pipeline: section unavailable "
            f"({exc.__class__.__name__})"
        )
