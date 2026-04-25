"""Deterministic digest assembly.

Sections (each renders even when empty so idle is distinguishable from
broken):

1. Decisions made — KAL-LE-authored reviews with status=addressed
   whose ``addressed`` timestamp falls within the digest window.
2. Promotions to canonical — git-log mining for commits whose
   subject/body matches a promotion keyword AND that add files under
   ``architecture/``, ``stack/``, or ``principles/`` in the window.
3. Open questions — KAL-LE-authored reviews with status=open across
   ALL configured projects (no window filter; open is open).
4. Cross-project patterns — emits a literal HTML-comment TODO marker;
   the LLM synthesis layer fills this in later.
5. Recurrences — KAL-LE reviews whose topic resurfaces and which have
   a sibling ``*—claude-disagreement.md`` archive.

Empty sections 1, 2, 5 emit an explicit "what we checked" line plus a
"last detected" pointer (the unbounded-history fallback) so a quiet
week is unambiguously distinct from a broken pipeline. Sections 3 and
4 keep their existing empty behavior (open-questions has no window;
cross-project is a literal LLM-TODO marker).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

from alfred.reviews.config import resolve_projects
from alfred.reviews.store import (
    KALLE_AUTHOR,
    REVIEWS_SUBPATH,
    STATUS_ADDRESSED,
    STATUS_OPEN,
    ReviewRecord,
    list_all_kalle_reviews_with_paths,
)

log = structlog.get_logger(__name__)


_PROMOTION_TARGET_DIRS: tuple[str, ...] = ("architecture", "stack", "principles")

# Keyword regex hits any commit whose subject/body talks about
# promoting/canonicalizing/curating. Used as the first half of the
# AND-gate; the second half checks that ≥1 file added by the commit
# actually lands in a canonical target dir. Keyword-only matches (e.g.
# "Expand standing orders" mentioning "curated context") are dropped
# as noise.
_PROMOTION_KEYWORD_RE = re.compile(
    r"\b(promot\w*|canonical\w*|curat\w*)\b",
    re.IGNORECASE,
)


@dataclass
class Promotion:
    repo: str
    sha: str           # short 7-char hash
    subject: str
    files: list[str] = field(default_factory=list)


@dataclass
class Recurrence:
    project: str
    filename: str
    topic: str
    disagreement_filename: str


@dataclass
class LastAddressed:
    project: str
    filename: str
    date: str


@dataclass
class LastPromotion:
    repo: str
    sha: str
    date: str


@dataclass
class LastRecurrence:
    project: str
    filename: str
    topic: str
    date: str


@dataclass
class DigestPayload:
    today: datetime
    window_start: datetime
    window_end: datetime
    decisions: list[ReviewRecord] = field(default_factory=list)
    decision_projects: dict[str, str] = field(default_factory=dict)
    promotions: list[Promotion] = field(default_factory=list)
    open_questions: list[ReviewRecord] = field(default_factory=list)
    open_question_projects: dict[str, str] = field(default_factory=dict)
    recurrences: list[Recurrence] = field(default_factory=list)
    project_names: list[str] = field(default_factory=list)
    repo_names: list[str] = field(default_factory=list)
    last_addressed: LastAddressed | None = None
    last_promotion: LastPromotion | None = None
    last_recurrence: LastRecurrence | None = None


def _parse_iso(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if len(s) == 10 and s.count("-") == 2:
            s = s + "T00:00:00+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def collect_decisions(
    project_paths: dict[str, Path],
    *,
    window_start: datetime,
    window_end: datetime,
) -> tuple[list[ReviewRecord], dict[str, str]]:
    """Addressed reviews whose ``addressed`` falls in the window.

    Returns ``(records, filename → project_name)``. Records are sorted
    most-recent-first by ``addressed`` timestamp.
    """
    rows = list_all_kalle_reviews_with_paths(list(project_paths.values()))
    path_to_project: dict[str, str] = {
        str(p): name for name, p in project_paths.items()
    }
    out: list[tuple[datetime, ReviewRecord, str]] = []
    project_map: dict[str, str] = {}
    for vault_str, rec in rows:
        if rec.frontmatter.get("status") != STATUS_ADDRESSED:
            continue
        addressed_dt = _parse_iso(rec.frontmatter.get("addressed"))
        if addressed_dt is None:
            continue
        if not (window_start <= addressed_dt <= window_end):
            continue
        project = path_to_project.get(vault_str, vault_str)
        out.append((addressed_dt, rec, project))
        project_map[rec.filename] = project
    out.sort(key=lambda t: t[0], reverse=True)
    return [r for _, r, _ in out], project_map


def collect_open_questions(
    project_paths: dict[str, Path],
) -> tuple[list[ReviewRecord], dict[str, str]]:
    """Every status=open KAL-LE review, across all projects, no window."""
    rows = list_all_kalle_reviews_with_paths(list(project_paths.values()))
    path_to_project: dict[str, str] = {
        str(p): name for name, p in project_paths.items()
    }
    out: list[tuple[datetime, ReviewRecord, str]] = []
    project_map: dict[str, str] = {}
    for vault_str, rec in rows:
        if rec.frontmatter.get("status") != STATUS_OPEN:
            continue
        created = _parse_iso(rec.frontmatter.get("created")) or datetime.min.replace(
            tzinfo=timezone.utc,
        )
        project = path_to_project.get(vault_str, vault_str)
        out.append((created, rec, project))
        project_map[rec.filename] = project
    out.sort(key=lambda t: t[0])
    return [r for _, r, _ in out], project_map


def _is_canonical_add(file_path: str) -> bool:
    """File path is added under one of the canonical target dirs.

    The path must be repo-root-relative (no leading slash). A path of
    ``architecture/foo.md`` matches; ``foo/architecture/x.md`` does not.
    """
    parts = file_path.split("/")
    return bool(parts) and parts[0] in _PROMOTION_TARGET_DIRS


def _git_log_keyword_commits(
    repo_path: Path,
    *,
    since_arg: str | None,
) -> list[tuple[str, str]]:
    """Return ``[(sha, subject), ...]`` for keyword-matching commits.

    ``since_arg`` is forwarded as ``--since=<value>`` if provided; pass
    ``None`` to scan all of history (used by the "last detected"
    fallback). The body is included in the keyword check via
    ``--pretty=format:`` — we serialize subject and body separated by a
    tab, with a NUL terminator between commits so multiline bodies
    don't split the record.
    """
    cmd = ["git", "log", "--pretty=format:%H%x09%s%x09%b%x00"]
    if since_arg:
        cmd.append(f"--since={since_arg}")
    try:
        result = subprocess.run(
            cmd, cwd=str(repo_path), capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.info(
            "digest.promotions.git_log_failed",
            repo=str(repo_path), error=str(exc),
        )
        return []
    if result.returncode != 0:
        log.info(
            "digest.promotions.git_log_nonzero",
            repo=str(repo_path),
            code=result.returncode,
            stderr=(result.stderr or "")[:500],
            stdout_tail=(result.stdout or "")[-2000:],
        )
        return []
    out: list[tuple[str, str]] = []
    raw = result.stdout or ""
    for record in raw.split("\x00"):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split("\t", 2)
        if len(parts) < 2:
            continue
        sha = parts[0].strip()
        subject = parts[1] if len(parts) >= 2 else ""
        body = parts[2] if len(parts) >= 3 else ""
        if not _PROMOTION_KEYWORD_RE.search(f"{subject}\n{body}"):
            continue
        out.append((sha, subject))
    return out


def _git_show_added_files(repo_path: Path, sha: str) -> list[str]:
    """Files added (``--diff-filter=A``) by ``sha``, repo-root-relative."""
    try:
        result = subprocess.run(
            [
                "git", "show", "--diff-filter=A", "--name-only",
                "--pretty=format:", sha,
            ],
            cwd=str(repo_path),
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.info(
            "digest.promotions.git_show_failed",
            repo=str(repo_path), sha=sha, error=str(exc),
        )
        return []
    if result.returncode != 0:
        log.info(
            "digest.promotions.git_show_nonzero",
            repo=str(repo_path), sha=sha,
            code=result.returncode,
            stderr=(result.stderr or "")[:500],
            stdout_tail=(result.stdout or "")[-2000:],
        )
        return []
    return [
        ln.strip() for ln in (result.stdout or "").splitlines()
        if ln.strip()
    ]


def collect_promotions(
    repo_paths: dict[str, Path],
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[Promotion]:
    """Hybrid promotion detection: keyword AND canonical-dir ADD.

    Keyword-only matches and canonical-dir-only commits both fail the
    AND-gate; only commits that satisfy both surface as promotions.
    Repos without a ``.git`` directory or whose git invocation fails
    are skipped silently — the digest must not crash because one repo
    isn't a clone.
    """
    out: list[Promotion] = []
    since_iso = window_start.isoformat()
    until_dt = window_end
    for name, path in repo_paths.items():
        if not (path / ".git").exists() and not (path / ".git").is_file():
            continue
        # We pass --since and post-filter against window_end ourselves
        # because git log's --until rounds inconsistently for tz-aware
        # ISO timestamps; cheaper to just keep what's in window.
        candidates = _git_log_keyword_commits(path, since_arg=since_iso)
        for sha, subject in candidates:
            commit_dt = _git_commit_datetime(path, sha)
            if commit_dt is not None and commit_dt > until_dt:
                continue
            files = _git_show_added_files(path, sha)
            canonical_files = [f for f in files if _is_canonical_add(f)]
            if not canonical_files:
                continue
            short_sha = sha[:7]
            out.append(Promotion(
                repo=name, sha=short_sha, subject=subject,
                files=canonical_files,
            ))
    return out


def _git_commit_datetime(repo_path: Path, sha: str) -> datetime | None:
    """Author date of ``sha`` as a UTC datetime, or None on failure."""
    try:
        result = subprocess.run(
            ["git", "show", "-s", "--format=%aI", sha],
            cwd=str(repo_path),
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return _parse_iso((result.stdout or "").strip())


def find_last_addressed(
    project_paths: dict[str, Path],
) -> LastAddressed | None:
    """Most-recent KAL-LE review with status=addressed, all-time, all projects.

    The empty-state caller uses this to print "Last addressed: <X>"
    when the current window has no decisions — so the reader can tell
    "we looked, found nothing this week" from "we never look".
    """
    rows = list_all_kalle_reviews_with_paths(list(project_paths.values()))
    path_to_project: dict[str, str] = {
        str(p): name for name, p in project_paths.items()
    }
    best: tuple[datetime, str, str, str] | None = None
    for vault_str, rec in rows:
        if rec.frontmatter.get("status") != STATUS_ADDRESSED:
            continue
        addressed_dt = _parse_iso(rec.frontmatter.get("addressed"))
        if addressed_dt is None:
            continue
        project = path_to_project.get(vault_str, vault_str)
        if best is None or addressed_dt > best[0]:
            best = (addressed_dt, project, rec.filename, addressed_dt.date().isoformat())
    if best is None:
        return None
    _, project, filename, date_str = best
    return LastAddressed(project=project, filename=filename, date=date_str)


def find_last_promotion(
    repo_paths: dict[str, Path],
) -> LastPromotion | None:
    """Most-recent commit (all-time) satisfying the promotion AND-gate.

    Unbounded git history scan per repo. The digest runs weekly so the
    cost is acceptable; if it ever isn't, a result cache lives at this
    seam.
    """
    best: tuple[datetime, str, str] | None = None
    for name, path in repo_paths.items():
        if not (path / ".git").exists() and not (path / ".git").is_file():
            continue
        candidates = _git_log_keyword_commits(path, since_arg=None)
        for sha, _subject in candidates:
            files = _git_show_added_files(path, sha)
            if not any(_is_canonical_add(f) for f in files):
                continue
            commit_dt = _git_commit_datetime(path, sha)
            if commit_dt is None:
                continue
            if best is None or commit_dt > best[0]:
                best = (commit_dt, name, sha[:7])
    if best is None:
        return None
    dt, repo, short_sha = best
    return LastPromotion(repo=repo, sha=short_sha, date=dt.date().isoformat())


_TOPIC_NORMALIZE = re.compile(r"[^a-z0-9]+")


def _normalize_topic(topic: str) -> str:
    return _TOPIC_NORMALIZE.sub(" ", topic.lower()).strip()


def collect_recurrences(
    project_paths: dict[str, Path],
    *,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> list[Recurrence]:
    """Find KAL-LE reviews with a sibling ``*—claude-disagreement.md`` file.

    The em-dash is the literal character per the disagreement-archive
    convention. Each match is paired with the underlying KAL-LE review
    so the digest reader knows which thread recurred.

    When ``window_start``/``window_end`` are set, only reviews whose
    ``addressed`` (or fallback ``created``) timestamp falls in the
    window are considered. ``find_last_recurrence`` calls this with
    no window for the unbounded "last detected" lookup.
    """
    rows = list_all_kalle_reviews_with_paths(list(project_paths.values()))
    path_to_project: dict[str, str] = {
        str(p): name for name, p in project_paths.items()
    }
    out: list[Recurrence] = []
    seen: set[tuple[str, str]] = set()
    for vault_str, rec in rows:
        if window_start is not None and window_end is not None:
            ts = (
                _parse_iso(rec.frontmatter.get("addressed"))
                or _parse_iso(rec.frontmatter.get("created"))
            )
            if ts is None or not (window_start <= ts <= window_end):
                continue
        project = path_to_project.get(vault_str, vault_str)
        reviews_dir = Path(vault_str) / REVIEWS_SUBPATH
        stem = rec.filename[:-3] if rec.filename.endswith(".md") else rec.filename
        candidates = [
            reviews_dir / f"{stem}—claude-disagreement.md",
            reviews_dir / f"{stem}--claude-disagreement.md",
            reviews_dir / f"{stem}-claude-disagreement.md",
        ]
        for candidate in candidates:
            if candidate.is_file():
                key = (project, rec.filename)
                if key in seen:
                    continue
                seen.add(key)
                out.append(Recurrence(
                    project=project,
                    filename=rec.filename,
                    topic=str(rec.frontmatter.get("topic") or ""),
                    disagreement_filename=candidate.name,
                ))
                break
    out.sort(key=lambda r: (r.project, r.filename))
    return out


def find_last_recurrence(
    project_paths: dict[str, Path],
) -> LastRecurrence | None:
    """Most-recent recurrence across all-time KAL-LE reviews.

    Definition of "recurrence" matches :func:`collect_recurrences` plus
    a topic-substring overlap check across the corpus: the normalized
    topic of one review must appear inside the normalized topic of
    another distinct review. That guards against single-occurrence
    disagreements being labeled "recurrent".
    """
    rows = list_all_kalle_reviews_with_paths(list(project_paths.values()))
    path_to_project: dict[str, str] = {
        str(p): name for name, p in project_paths.items()
    }
    normalized_topics: list[tuple[str, ReviewRecord, str]] = []
    for vault_str, rec in rows:
        topic = str(rec.frontmatter.get("topic") or "")
        norm = _normalize_topic(topic)
        if not norm:
            continue
        normalized_topics.append((vault_str, rec, norm))

    def topic_recurs(target_norm: str, self_vault: str, self_filename: str) -> bool:
        for other_vault, other_rec, other_norm in normalized_topics:
            if other_vault == self_vault and other_rec.filename == self_filename:
                continue
            if target_norm in other_norm or other_norm in target_norm:
                return True
        return False

    best: tuple[datetime, str, str, str] | None = None
    for vault_str, rec, norm in normalized_topics:
        if not topic_recurs(norm, vault_str, rec.filename):
            continue
        reviews_dir = Path(vault_str) / REVIEWS_SUBPATH
        stem = rec.filename[:-3] if rec.filename.endswith(".md") else rec.filename
        siblings = [
            reviews_dir / f"{stem}—claude-disagreement.md",
            reviews_dir / f"{stem}--claude-disagreement.md",
            reviews_dir / f"{stem}-claude-disagreement.md",
        ]
        if not any(s.is_file() for s in siblings):
            continue
        # Use addressed if present, else created — whatever puts it on
        # a timeline. Files lacking both are skipped.
        ts = (
            _parse_iso(rec.frontmatter.get("addressed"))
            or _parse_iso(rec.frontmatter.get("created"))
        )
        if ts is None:
            continue
        project = path_to_project.get(vault_str, vault_str)
        topic = str(rec.frontmatter.get("topic") or rec.filename)
        if best is None or ts > best[0]:
            best = (ts, project, rec.filename, topic)
    if best is None:
        return None
    ts, project, filename, topic = best
    return LastRecurrence(
        project=project, filename=filename, topic=topic,
        date=ts.date().isoformat(),
    )


def build_payload(
    *,
    project_paths: dict[str, Path],
    today: datetime,
    window_days: int = 7,
) -> DigestPayload:
    """Gather every section's data for ``today`` minus ``window_days``."""
    if today.tzinfo is None:
        today = today.replace(tzinfo=timezone.utc)
    window_end = today
    window_start = today - timedelta(days=window_days)
    decisions, decision_projects = collect_decisions(
        project_paths, window_start=window_start, window_end=window_end,
    )
    promotions = collect_promotions(
        project_paths, window_start=window_start, window_end=window_end,
    )
    open_q, open_q_projects = collect_open_questions(project_paths)
    recurrences = collect_recurrences(
        project_paths,
        window_start=window_start, window_end=window_end,
    )
    project_names = sorted(project_paths.keys())

    last_addressed = None
    if not decisions:
        last_addressed = find_last_addressed(project_paths)
    last_promotion = None
    if not promotions:
        last_promotion = find_last_promotion(project_paths)
    last_recurrence = None
    if not recurrences:
        last_recurrence = find_last_recurrence(project_paths)

    return DigestPayload(
        today=today,
        window_start=window_start,
        window_end=window_end,
        decisions=decisions,
        decision_projects=decision_projects,
        promotions=promotions,
        open_questions=open_q,
        open_question_projects=open_q_projects,
        recurrences=recurrences,
        project_names=project_names,
        repo_names=project_names,
        last_addressed=last_addressed,
        last_promotion=last_promotion,
        last_recurrence=last_recurrence,
    )


_LLM_PLACEHOLDER = "<!-- TODO: LLM synthesis layer not yet implemented -->"


def _format_project_list(names: list[str]) -> str:
    """Comma-separated for the empty-state "Checked: ..." suffix.

    A configured-but-empty deployment (zero projects) collapses to
    ``(none configured)`` so the line still parses unambiguously.
    """
    return ", ".join(names) if names else "(none configured)"


def render(payload: DigestPayload) -> str:
    """Render the digest markdown. Empty sections still render."""
    parts: list[str] = []
    today_iso = payload.today.date().isoformat()
    start_iso = payload.window_start.date().isoformat()
    end_iso = payload.window_end.date().isoformat()
    window_days = max(1, (payload.window_end - payload.window_start).days)
    parts.append(f"# Weekly digest — {today_iso}")
    parts.append("")
    parts.append(f"Window: {start_iso} → {end_iso}")
    parts.append("")

    parts.append("## Decisions made")
    parts.append("")
    if not payload.decisions:
        checked = _format_project_list(payload.project_names)
        if payload.last_addressed is not None:
            last = (
                f"{payload.last_addressed.project}@"
                f"{payload.last_addressed.filename} "
                f"on {payload.last_addressed.date}"
            )
        else:
            last = "never"
        parts.append(
            f"No KAL-LE reviews flipped to addressed in the last "
            f"{window_days} days. Checked: {checked}. Last addressed: {last}."
        )
    else:
        for rec in payload.decisions:
            project = payload.decision_projects.get(rec.filename, "?")
            topic = str(rec.frontmatter.get("topic") or rec.filename)
            addressed = str(rec.frontmatter.get("addressed") or "")
            parts.append(
                f"- [{project}] {topic} — addressed {addressed} "
                f"({rec.filename})"
            )
    parts.append("")

    parts.append("## Promotions to canonical")
    parts.append("")
    if not payload.promotions:
        checked_repos = _format_project_list(payload.repo_names)
        target_dirs = ",".join(_PROMOTION_TARGET_DIRS)
        if payload.last_promotion is not None:
            last = (
                f"{payload.last_promotion.repo}@{payload.last_promotion.sha} "
                f"on {payload.last_promotion.date}"
            )
        else:
            last = "never"
        parts.append(
            f"No canonical promotions detected in the last {window_days} "
            f"days. Checked: keyword /\\b(promot|canonical|curat)/i + ADDs "
            f"in {{{target_dirs}}}/ across {checked_repos}. "
            f"Last detected: {last}."
        )
    else:
        for promo in payload.promotions:
            parts.append(
                f"- {promo.repo}@{promo.sha} — {promo.subject}"
            )
            for f in promo.files:
                parts.append(f"  - {f}")
    parts.append("")

    parts.append("## Open questions")
    parts.append("")
    if not payload.open_questions:
        parts.append("None this week.")
    else:
        for rec in payload.open_questions:
            project = payload.open_question_projects.get(rec.filename, "?")
            topic = str(rec.frontmatter.get("topic") or rec.filename)
            created = str(rec.frontmatter.get("created") or "")
            parts.append(
                f"- [{project}] {topic} — open since {created} "
                f"({rec.filename})"
            )
    parts.append("")

    parts.append("## Cross-project patterns")
    parts.append("")
    parts.append(_LLM_PLACEHOLDER)
    parts.append("")

    parts.append("## Recurrences")
    parts.append("")
    if not payload.recurrences:
        checked = _format_project_list(payload.project_names)
        if payload.last_recurrence is not None:
            last = (
                f"{payload.last_recurrence.topic} on "
                f"{payload.last_recurrence.date}"
            )
        else:
            last = "never"
        parts.append(
            f"No recurring topics with sibling disagreement archives in "
            f"the last {window_days} days. Checked: {checked}. "
            f"Last recurrence: {last}."
        )
    else:
        for rec in payload.recurrences:
            parts.append(
                f"- [{rec.project}] {rec.topic or rec.filename} — "
                f"sibling: {rec.disagreement_filename}"
            )
    parts.append("")

    return "\n".join(parts).rstrip() + "\n"


def write_digest(
    *,
    output_dir: Path,
    project_paths: dict[str, Path],
    today: datetime,
    window_days: int = 7,
) -> tuple[Path, str, DigestPayload]:
    """Build and persist the digest file. Returns (path, body, payload)."""
    payload = build_payload(
        project_paths=project_paths, today=today, window_days=window_days,
    )
    body = render(payload)
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{today.date().isoformat()}-weekly-digest.md"
    out_path = output_dir / filename
    out_path.write_text(body, encoding="utf-8")
    return out_path, body, payload


def resolve_repo_paths(raw: dict[str, Any]) -> dict[str, Path]:
    """Reuse the ``kalle.projects`` map for both vault paths and git roots.

    For now the project vault and the git repo are the same directory
    (each project is a single git repo whose root is its vault). When
    that diverges, this function is the seam to override.
    """
    return resolve_projects(raw)


__all__ = [
    "DigestPayload",
    "LastAddressed",
    "LastPromotion",
    "LastRecurrence",
    "Promotion",
    "Recurrence",
    "build_payload",
    "collect_decisions",
    "collect_open_questions",
    "collect_promotions",
    "collect_recurrences",
    "find_last_addressed",
    "find_last_promotion",
    "find_last_recurrence",
    "render",
    "resolve_repo_paths",
    "write_digest",
]
