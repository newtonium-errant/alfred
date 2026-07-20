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
from typing import Any, Callable

from .utils import SectionReadStatus, get_logger, safe_read_section_file

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

    # Defensive read via the shared helper — the old bare ``except OSError``
    # missed UnicodeDecodeError (a ValueError subclass), so a non-UTF-8
    # bash_exec log escaped and aborted the WHOLE KAL-LE digest (fire_once's
    # daemon-level catch eats it as a generic fire_error). Degrade to (0, {})
    # + a warning; the per-line json.loads below stays line-level resilient.
    # (N2: ``read.detail`` carries only errno/position/path, never file
    # content — safe to log; this is an alfred-written system JSONL anyway.
    # A future renderer over PERSONAL vault content must keep payloads
    # content-free.)
    read = safe_read_section_file(path)
    if read.status is not SectionReadStatus.OK:
        log.warning(
            "kalle_digest.bash_exec_read_failed",
            path=str(path),
            error=read.detail,
            error_type=read.error_type,
        )
        return 0, {}

    total = 0
    cwds: Counter[str] = Counter()
    for line in read.text.splitlines():
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
    # Defensive read via the shared helper — the old ``(json.JSONDecodeError,
    # OSError)`` catch missed UnicodeDecodeError (a SIBLING of JSONDecodeError
    # under ValueError, not a subclass), so a non-UTF-8 state file escaped and
    # aborted the whole KAL-LE digest. Degrade to ([], [], []); signal the
    # failure (ILB) so a corrupt state file isn't mistaken for "no directives".
    # (N2: payload fields carry only errno/position/path, never file content —
    # safe for this alfred-written system JSON. Future PERSONAL-content
    # renderers must keep log payloads content-free.)
    read = safe_read_section_file(path)
    if read.status is not SectionReadStatus.OK:
        log.warning(
            "kalle_digest.instructor_state_read_failed",
            path=str(path),
            stage="read",
            error=read.detail,
            error_type=read.error_type,
        )
        return [], [], []
    try:
        data = json.loads(read.text)
    except json.JSONDecodeError as exc:
        log.warning(
            "kalle_digest.instructor_state_read_failed",
            path=str(path),
            stage="json",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
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
    # Defensive read via the shared helper — same UnicodeDecodeError gap as
    # the other kalle_digest readers: a non-UTF-8 BIT state file escaped the
    # ``(json.JSONDecodeError, OSError)`` catch. Degrade to ("", "") + signal
    # the failure (ILB). (N2: content-free payload; alfred-written BIT metadata.)
    read = safe_read_section_file(path)
    if read.status is not SectionReadStatus.OK:
        log.warning(
            "kalle_digest.bit_state_read_failed",
            path=str(path),
            stage="read",
            error=read.detail,
            error_type=read.error_type,
        )
        return "", ""
    try:
        data = json.loads(read.text)
    except json.JSONDecodeError as exc:
        log.warning(
            "kalle_digest.bit_state_read_failed",
            path=str(path),
            stage="json",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
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
# the issue timeline for the FIRST cross-reference whose referenced
# entity is a pull request. FORGE-AWARE — the timeline shape differs:
#   * GitHub  — an event with ``event == "cross-referenced"`` and a
#     ``source.issue`` that is a PR (``source.issue.pull_request``
#     present).
#   * Forgejo — a comment with a cross-ref ``type``
#     (``pull_ref|issue_ref|comment_ref|commit_ref``) and a ``ref_issue``
#     (a full Issue; a PR carries a truthy ``pull_request`` field).
# In both, take the PR's ``number``. ``issue_get``'s ``pull_request``
# field was REJECTED as the primary strategy: that field only exists when
# the issue itself IS a PR, never when a PR merely references the issue —
# useless for "which PR fixes this ticket". Once a PR number is captured
# in state, later checks skip timeline discovery and go straight to
# pr_get (cheaper, and stable against later unrelated cross-references).
# forge_type threads from the github_ops client's config to the
# dispatcher (``_first_cross_referenced_pr``).
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

# Pipeline c7 — KAL-LE disposition → VERA write-back (status,
# ticket_disposition) mapping. The status flips the ticket out of
# VERA's open worklist (both values are outside OPEN_TICKET_STATUSES);
# ticket_disposition records the outcome on VERA's copy. Pinned in
# tests/test_kalle_digest.py (status-mapping pin) and reused by the
# /peer/ticket_outcome wire schema gate.
TICKET_OUTCOME_WRITEBACK_MAP: dict[str, tuple[str, str]] = {
    "merged_clean": ("resolved", "merged"),
    "merged_after_rework": ("resolved", "merged_after_rework"),
    "closed_unmerged": ("closed", "closed_no_merge"),
}


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


# Forgejo timeline cross-reference comment types (the entity that
# references this issue). A PR reference is distinguished from a plain
# issue reference by ``ref_issue["pull_request"]`` being truthy, not by
# the type alone — so accept any cross-ref type and gate on the
# ref_issue shape.
_FORGEJO_CROSS_REF_TYPES = frozenset({
    "pull_ref", "issue_ref", "comment_ref", "commit_ref",
})


def _first_cross_referenced_pr_github(timeline: Any) -> int | None:
    """GitHub PR-discovery: first ``cross-referenced`` event whose
    ``source.issue`` is a PR (``pull_request`` present). Pre-port
    behavior, unchanged."""
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


def _first_cross_referenced_pr_forgejo(timeline: Any) -> int | None:
    """Forgejo PR-discovery: first cross-ref comment (``type`` in the
    cross-ref set) whose ``ref_issue`` is a PR (``pull_request`` truthy)."""
    if not isinstance(timeline, list):
        return None
    for event in timeline:
        if not isinstance(event, dict):
            continue
        if event.get("type") not in _FORGEJO_CROSS_REF_TYPES:
            continue
        ref_issue = event.get("ref_issue")
        if not isinstance(ref_issue, dict):
            continue
        if not ref_issue.get("pull_request"):
            continue  # the referencing entity is a plain issue — not a PR
        number = ref_issue.get("number")
        if isinstance(number, int) and not isinstance(number, bool):
            return number
    return None


def _first_cross_referenced_pr(timeline: Any, forge_type: str = "github") -> int | None:
    """Dispatch the PR-discovery strategy by forge type (see the section
    comment above). GitHub and Forgejo expose different timeline shapes;
    ``forge_type`` threads from the github_ops client's config."""
    if forge_type == "forgejo":
        return _first_cross_referenced_pr_forgejo(timeline)
    return _first_cross_referenced_pr_github(timeline)


# Review states meaning "changes were requested": GitHub uses
# ``CHANGES_REQUESTED``, Forgejo uses ``REQUEST_CHANGES``. Accept BOTH
# (a set membership check, no forge branch) so the merged_after_rework
# derivation works on either forge.
_REWORK_REVIEW_STATES = frozenset({"CHANGES_REQUESTED", "REQUEST_CHANGES"})


async def _check_one_ticket_outcome(
    client: Any,
    entry: Any,
    *,
    now: datetime,
    pr_links: dict[int, dict[str, Any]] | None = None,
    app_client_factory: Callable[[str, str], Any] | None = None,
) -> bool:
    """Evaluate one non-terminal issue-bearing entry against the forge.

    Mutates the entry in place (the caller saves state) and returns
    True when the disposition CHANGED. Disposition rules (ratified):
      * PR merged + zero changes-requested reviews → ``merged_clean``
      * PR merged + any changes-requested review → ``merged_after_rework``
        (``CHANGES_REQUESTED`` on GitHub, ``REQUEST_CHANGES`` on Forgejo —
        both accepted)
      * PR closed without merge → ``closed_unmerged``
      * no PR (or PR still open) AND the issue is
        ``TICKET_STALLED_NIGHTS``+ nights old → ``stalled`` (non-terminal)
      * else → leave as-is (``""`` stays ``""``)

    Forge-aware: ``forge_type`` (from the client's config) selects the
    timeline-shape parser; the review-state check accepts both forges'
    enums without branching.

    CROSS-REPO (Option B, C4/C4b): ``pr_links`` (issue_number → the
    drafter's linkage ``{pr_number, app_repo, app_forge_type, ...}`` from
    :func:`alfred.transport.fix_drafter.load_pr_links`, used for FIRST
    discovery) plus ``app_client_factory`` ((app_repo, app_forge_type) → a
    per-APP-repo read client) let this loop find a fix PR that lives on a
    DIFFERENT app repo than the central tracker. On the first cross-repo
    resolution the origin repo/forge is stamped onto the entry
    (``pr_app_repo``/``pr_app_forge_type``); thereafter routing is driven by
    that self-describing marker, INDEPENDENT of ``pr_links`` — so a wiped
    drafter-state file can never leave an app-repo ``pr_number`` to be
    polled against the central tracker. Both params absent AND no marker →
    the same-repo ``issue_timeline`` path runs BYTE-IDENTICALLY to before.
    """
    forge_type = str(
        getattr(getattr(client, "config", None), "forge_type", "github")
        or "github"
    )
    central_repo = str(
        getattr(getattr(client, "config", None), "repo", "") or ""
    )
    pr_number = entry.pr_number

    # CROSS-REPO routing (Option B, C4b) — SELF-DESCRIBING. When the fix PR
    # lives on a DIFFERENT app repo than the central tracker, ``pr_number``
    # is the APP repo's number and is MEANINGLESS against the central client
    # — polling it there could hit an unrelated same-numbered central PR and
    # latch a WRONG terminal outcome. The origin repo/forge is recorded on
    # the intake entry itself (``pr_app_repo`` / ``pr_app_forge_type``), so a
    # cross-repo ticket ALWAYS routes to the app repo regardless of whether
    # the drafter's (separately wipeable) ``load_pr_links`` state is present.
    #
    # Binding source, in precedence order:
    #   1. the entry's OWN persisted marker (authoritative; survives a
    #      drafter-state wipe),
    #   2. else the drafter's transient link for FIRST discovery (a link
    #      naming the central repo is a same-repo ticket → left to timeline).
    app_repo = str(getattr(entry, "pr_app_repo", "") or "")
    app_forge = str(getattr(entry, "pr_app_forge_type", "") or "")
    link = (pr_links or {}).get(entry.issue_number)
    if not app_repo and link:
        link_repo = str(link.get("app_repo") or "")
        if link_repo and link_repo != central_repo:
            app_repo = link_repo
            app_forge = str(link.get("app_forge_type") or "github")
            if pr_number is None and link.get("pr_number") is not None:
                pr_number = link.get("pr_number")

    is_cross_repo = bool(app_repo) and app_repo != central_repo

    poll_client = client
    if is_cross_repo:
        app_client = (
            app_client_factory(app_repo, app_forge or "github")
            if app_client_factory is not None
            else None
        )
        if app_client is None:
            # Can't reach the app repo THIS pass (no factory / transient
            # missing-token / unknown repo). NEVER central-poll an app-repo
            # PR number and never run the central timeline for a known
            # cross-repo entry — leave the persisted number, marker, and
            # disposition untouched and retry next pass (self-correcting once
            # the client builds). Only ``outcome_checked_at`` advances so
            # staleness stays visible. Emits an explicit signal so a stuck
            # token is distinguishable from an idle ticket (ILB).
            log.info(
                "kalle.digest.ticket_pipeline_cross_repo_poll_unavailable",
                issue_number=entry.issue_number,
                app_repo=app_repo,
            )
            entry.outcome_checked_at = now.isoformat()
            return False
        poll_client = app_client
        # Stamp the self-describing marker so later passes route to the app
        # repo from the ENTRY, independent of the drafter's link state.
        entry.pr_app_repo = app_repo
        entry.pr_app_forge_type = app_forge or "github"
        log.info(
            "kalle.digest.ticket_pipeline_cross_repo_link",
            issue_number=entry.issue_number,
            app_repo=app_repo,
            app_forge_type=app_forge or "github",
            pr_number=pr_number,
        )

    if pr_number is None and not is_cross_repo:
        # SAME-REPO path (GitHub-Action drafter / single-repo Option-B):
        # discover the linked PR via the tracker issue's timeline. Skipped
        # for a cross-repo entry — its PR is never cross-referenced on the
        # central tracker, and a central-discovered number must never be
        # polled on the app client.
        timeline = await client.issue_timeline(
            number=entry.issue_number, caller="digest",
        )
        pr_number = _first_cross_referenced_pr(timeline, forge_type)

    old_disposition = entry.disposition
    new_disposition = old_disposition
    pr_state = entry.pr_state
    latency = entry.ticket_to_pr_latency_days

    if pr_number is None:
        nights = _nights_since(entry.issue_created_at, now)
        if nights is not None and nights >= TICKET_STALLED_NIGHTS:
            new_disposition = "stalled"
    else:
        # ``poll_client`` is the APP-repo client for a cross-repo link, else
        # the central client — so ``pr_get``/``pr_reviews`` hit the forge the
        # PR actually lives on.
        pr = await poll_client.pr_get(number=pr_number, caller="digest")
        pr = pr if isinstance(pr, dict) else {}
        merged_at = pr.get("merged_at")
        if merged_at:
            pr_state = "merged"
            reviews = await poll_client.pr_reviews(
                number=pr_number, caller="digest",
            )
            reviews = reviews if isinstance(reviews, list) else []
            # Accept both forges' "changes requested" enums (see
            # _REWORK_REVIEW_STATES) — no forge branch needed here.
            rework = any(
                isinstance(review, dict)
                and review.get("state") in _REWORK_REVIEW_STATES
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


async def _maybe_push_ticket_outcome(
    uid: str,
    entry: Any,
    *,
    outcome_config: Any,
    transport_config: Any,
    now: datetime,
) -> bool:
    """Push one terminal ticket's outcome to its origin instance (c7).

    Fires the KAL-LE→VERA write-back ONCE per ticket, on the
    open→terminal transition. Idempotency guard: ``entry.outcome_pushed_
    at`` is set on the FIRST successful push; a failed push leaves it
    empty so the next nightly pass retries (the latch refinement in
    :func:`check_ticket_outcomes` keeps a terminal-but-unpushed entry
    eligible for THIS path without re-querying GitHub).

    Returns True iff a push succeeded this call (the caller sets
    ``outcome_pushed_at`` + saves state). Fail-closed: any transport
    error is logged + contained — KAL-LE's own state stays authoritative
    and the digest pass never crashes on a push failure.

    Skips (each with a distinct log) when: the pusher is disabled, the
    entry isn't terminal, it was already pushed, the origin instance is
    unknown, or the disposition isn't in the write-back map.
    """
    from alfred.transport.client import peer_send_ticket_outcome
    from alfred.transport.exceptions import TransportError, TransportRejected

    if not getattr(outcome_config, "enabled", False):
        return False
    if entry.disposition not in TICKET_TERMINAL_DISPOSITIONS:
        return False
    if entry.outcome_pushed_at:
        return False  # already propagated — idempotent

    mapped = TICKET_OUTCOME_WRITEBACK_MAP.get(entry.disposition)
    if mapped is None:
        # Defensive: a terminal disposition with no write-back mapping
        # is a code bug, not a runtime condition — surface it loudly
        # rather than silently skipping.
        log.warning(
            "kalle.digest.ticket_outcome_unmapped_disposition",
            uid=uid,
            disposition=entry.disposition,
        )
        return False
    status, ticket_disposition = mapped

    self_name = str(getattr(outcome_config, "self_name", "") or "")
    if not self_name:
        # Fail-loud per feedback_hardcoding_and_alfred_naming.md — never
        # default the sender identity; an unset self_name is a config
        # bug the operator must fix (VERA's allowed_clients can't match
        # an empty client).
        log.warning(
            "kalle.digest.ticket_outcome_push_no_self_name",
            uid=uid,
            detail=(
                "ticket_outcome.enabled is true but self_name is empty — "
                "set ticket_outcome.self_name so the receiver's "
                "allowed_clients can match"
            ),
        )
        return False
    target_peer = str(getattr(outcome_config, "target_peer", "") or "")

    try:
        ack = await peer_send_ticket_outcome(
            target_peer,
            ticket_uid=uid,
            status=status,
            disposition=ticket_disposition,
            self_name=self_name,
            pr_number=entry.pr_number,
            resolved_at=entry.outcome_checked_at or now.isoformat(),
            config=transport_config,
        )
    except (TransportError, TransportRejected) as exc:
        # Transport down / 4xx / unknown peer (_resolve_peer raises
        # TransportError). Contained: leave outcome_pushed_at empty so
        # the next pass retries; KAL-LE's state stays authoritative.
        http_status = getattr(exc, "status_code", None)
        body_head = str(getattr(exc, "body", "") or "")[:200]
        detail = (
            f"HTTP {http_status}: {body_head or '(no body)'}"
            if http_status is not None
            else f"{exc.__class__.__name__}: {str(exc)[:200] or '(no detail)'}"
        )
        log.warning(
            "kalle.digest.ticket_outcome_push_failed",
            uid=uid,
            target_peer=target_peer,
            status=status,
            disposition=ticket_disposition,
            error=str(exc),
            error_type=exc.__class__.__name__,
            http_status=http_status,
            detail=detail,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — never crash the digest on a push
        log.warning(
            "kalle.digest.ticket_outcome_push_failed",
            uid=uid,
            target_peer=target_peer,
            status=status,
            disposition=ticket_disposition,
            error=str(exc),
            error_type=exc.__class__.__name__,
            http_status=None,
            detail=f"{exc.__class__.__name__}: {str(exc)[:200] or '(no detail)'}",
        )
        return False

    applied = bool(ack.get("applied")) if isinstance(ack, dict) else False
    log.info(
        "kalle.digest.ticket_outcome_pushed",
        uid=uid,
        target_peer=target_peer,
        status=status,
        disposition=ticket_disposition,
        pr_number=entry.pr_number,
        applied=applied,
        relpath=str(ack.get("relpath", "")) if isinstance(ack, dict) else "",
    )
    return True


async def check_ticket_outcomes(
    state: Any,
    client: Any,
    *,
    now: datetime,
    audit_log_path: str,
    outcome_config: Any = None,
    transport_config: Any = None,
    pr_links: dict[int, dict[str, Any]] | None = None,
    app_client_factory: Callable[[str, str], Any] | None = None,
) -> int:
    """The effectiveness-loop capture pass. Returns the changed count.

    Per-ticket containment: one bad issue's API failure logs + skips
    that entry, never the rest. Each entry whose disposition CHANGED
    gets ONE summarising audit row (the client already audits every
    underlying HTTP call; this row carries the derived effectiveness
    fields via ``extra``).

    Pipeline c7 — the KAL-LE→VERA outcome write-back fires from this
    same loop. Two terminal-entry cases now reach the push (both gated
    by ``outcome_config.enabled``):
      * a NON-terminal entry that newly flips terminal this pass, AND
      * a terminal entry whose prior push FAILED (``outcome_pushed_at``
        still empty) — re-attempted WITHOUT re-querying GitHub (the
        disposition is already final, so no API cost).
    A terminal entry already pushed (``outcome_pushed_at`` set) is fully
    latched — neither re-queried nor re-pushed.

    ``outcome_config`` / ``transport_config`` default None — when either
    is absent (or the pusher is disabled) the loop runs exactly as
    before (capture only, no push). ILB: a completed pass that found no
    terminal outcome to propagate emits an explicit
    ``kalle.digest.no_ticket_outcomes_to_propagate`` so an idle night is
    distinguishable from a broken push path.

    ``pr_links`` / ``app_client_factory`` default None — the Option B
    cross-repo consumer (C4). When present, a ticket whose drafter link
    names an app repo other than the central tracker is polled on a
    per-app-repo client instead of the tracker timeline. Both absent →
    every ticket resolves via ``issue_timeline`` exactly as before.
    """
    from alfred.integrations.github_ops import append_github_audit

    push_enabled = bool(getattr(outcome_config, "enabled", False))
    changed_count = 0
    pushed_count = 0
    for uid in sorted(state.entries):
        entry = state.entries[uid]
        if entry.issue_number is None:
            continue  # no issue yet — nothing to check against

        if entry.disposition in TICKET_TERMINAL_DISPOSITIONS:
            # Terminal latch — never re-query GitHub. But a terminal
            # entry whose push never succeeded still needs the write-back
            # retried (no GitHub call — the disposition is already
            # final). A fully-pushed terminal entry skips both.
            if push_enabled and not entry.outcome_pushed_at:
                if await _maybe_push_ticket_outcome(
                    uid, entry,
                    outcome_config=outcome_config,
                    transport_config=transport_config,
                    now=now,
                ):
                    entry.outcome_pushed_at = now.isoformat()
                    state.save()
                    pushed_count += 1
            continue
        try:
            changed = await _check_one_ticket_outcome(
                client, entry, now=now,
                pr_links=pr_links, app_client_factory=app_client_factory,
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
        # A non-terminal entry that flipped terminal THIS pass gets its
        # write-back here (gated + idempotent inside the helper).
        if push_enabled and not entry.outcome_pushed_at:
            if await _maybe_push_ticket_outcome(
                uid, entry,
                outcome_config=outcome_config,
                transport_config=transport_config,
                now=now,
            ):
                entry.outcome_pushed_at = now.isoformat()
                state.save()
                pushed_count += 1

    # ILB: idle is distinguishable from broken. Emit the no-op line when
    # the pusher is enabled but no outcome was propagated this pass
    # (every terminal entry already pushed, or no terminal entries yet).
    if push_enabled and pushed_count == 0:
        log.info(
            "kalle.digest.no_ticket_outcomes_to_propagate",
            checked=len(state.entries),
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
        if (
            entry.retry_count > 0
            and entry.issue_number is None
            and not entry.intake_abandoned
        ):
            # KAL-LE flag FIX 2: an ``intake_abandoned`` entry is a stale
            # counter for a terminal ticket (wont_fix/closed/resolved
            # with no issue) — reconciled, NOT an active retry, so it no
            # longer counts toward "pending retry" (which drove yellow
            # posture). See reconcile_intake_against_tickets.
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
            GitHubOpsError,
            build_github_client,
        )
        from alfred.transport.ticket_intake import (
            TicketIntakeState,
            load_ticket_intake_config,
            load_ticket_outcome_config,
            reconcile_intake_against_tickets,
        )

        now = now or datetime.now(timezone.utc)
        raw = raw if isinstance(raw, dict) else {}
        intake_config = load_ticket_intake_config(raw)
        outcome_config = load_ticket_outcome_config(raw)
        state = TicketIntakeState.load(intake_config.state_path)
        if not state.entries:
            # ILB: the quiet line + a grep-able log — idle, not broken.
            log.info(
                "kalle.digest.ticket_pipeline_idle",
                state_path=intake_config.state_path,
            )
            return render_ticket_pipeline_section(state, now=now)

        # Reconcile pending-retry entries against their ticket records
        # (KAL-LE flag FIX 2): a still-pending entry whose ticket flipped
        # terminal (wont_fix / closed / resolved) without ever needing a
        # GitHub issue is a STALE counter, not an active retry — mark it
        # abandoned so the digest stops counting it as "pending retry."
        # Runs UNCONDITIONALLY (local vault reads, no GitHub call) BEFORE
        # the github-client branch, so it works even on the no-credential
        # path. The sweep auto-clears existing stale entries on the next
        # pass — no manual state edit needed.
        vault_path = Path((raw.get("vault") or {}).get("path") or ".")
        reconciled = reconcile_intake_against_tickets(state, vault_path)
        if reconciled:
            state.save()

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
            except GitHubOpsError as exc:
                # Widened to the PARENT class (was NotConfigured/WrongInstance
                # only): a GitHubOpsError from a bad forge_type now yields a
                # TARGETED "outcome check unavailable" note + the rest of the
                # section still renders, instead of falling through to the
                # §-boundary and degrading the WHOLE ticket-pipeline section.
                # exc.__class__.__name__ keeps the specific subclass in the log.
                log.info(
                    "kalle.digest.ticket_pipeline_outcome_check_unavailable",
                    reason=exc.__class__.__name__,
                )
                outcome_note = (
                    "outcome check unavailable: github config error"
                )

        if client is not None:
            # C4/C4b — Option B cross-repo PR-link consumer. Load the
            # drafter's linkage (issue_number → {pr_number, app_repo,
            # app_forge_type}) for FIRST discovery + a factory that builds a
            # per-app-repo READ client, so a fix PR living on a DIFFERENT app
            # repo than the central tracker resolves to its real disposition
            # instead of a false ``stalled``. Engaged when the drafter has
            # ``projects`` configured (Option B multi-repo); single-repo
            # instances build no factory → the same-repo timeline path is
            # untouched. The factory is built whenever ``projects`` is set —
            # NOT gated on ``pr_links`` being non-empty — because a ticket's
            # self-describing ``pr_app_repo`` marker must still route to the
            # app repo AFTER the drafter state file is wiped (C4b). Best-
            # effort: any failure logs + falls back to the timeline path,
            # never crashing the pass.
            pr_links: dict[int, dict[str, Any]] = {}
            app_client_factory: Callable[[str, str], Any] | None = None
            try:
                from alfred.transport.fix_drafter import (
                    load_fix_drafter_config,
                    load_pr_links,
                    poll_client_for_app_repo,
                )

                drafter_config = load_fix_drafter_config(raw)
                if drafter_config.projects:
                    pr_links = load_pr_links(drafter_config.state_path)
                    # ILB: idle (no cross-repo links yet) is distinguishable
                    # from broken — emit the count on every enabled pass.
                    log.info(
                        "kalle.digest.cross_repo_links_loaded",
                        count=len(pr_links),
                        state_path=drafter_config.state_path,
                    )
                    _poll_audit = client.config.audit_log_path

                    def app_client_factory(
                        app_repo: str, app_forge_type: str,
                    ) -> Any:
                        return poll_client_for_app_repo(
                            drafter_config,
                            app_repo,
                            app_forge_type,
                            audit_log_path=_poll_audit,
                        )
            except Exception as exc:  # noqa: BLE001 — best-effort consumer
                log.warning(
                    "kalle.digest.cross_repo_link_load_failed",
                    error=str(exc),
                    error_type=exc.__class__.__name__,
                    detail=(
                        "cross-repo PR linkage unavailable this pass; the "
                        "same-repo timeline path is unaffected"
                    ),
                )
                pr_links = {}
                app_client_factory = None

            # Build the transport config only when the c7 pusher is
            # enabled — a KAL-LE instance NOT opted into the write-back
            # runs capture-only exactly as before (no transport load).
            transport_config = None
            if outcome_config.enabled:
                try:
                    from alfred.transport.config import (
                        load_from_unified as load_transport_config,
                    )
                    transport_config = load_transport_config(raw)
                except Exception as exc:  # noqa: BLE001 — push is best-effort
                    log.warning(
                        "kalle.digest.ticket_outcome_transport_config_failed",
                        error=str(exc),
                        error_type=exc.__class__.__name__,
                        detail=(
                            "ticket_outcome.enabled is true but the "
                            "transport config could not be loaded — "
                            "write-back skipped this pass"
                        ),
                    )
            await check_ticket_outcomes(
                state,
                client,
                now=now,
                audit_log_path=client.config.audit_log_path,
                outcome_config=outcome_config,
                transport_config=transport_config,
                pr_links=pr_links,
                app_client_factory=app_client_factory,
            )
            # Atomic save — outcome_checked_at moved even when no
            # disposition flipped (check_ticket_outcomes also saves on
            # each successful c7 push to persist outcome_pushed_at).
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
