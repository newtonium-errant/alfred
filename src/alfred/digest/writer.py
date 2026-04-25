"""Deterministic digest assembly.

Sections (each renders even when empty so idle is distinguishable from
broken):

1. Decisions made — KAL-LE-authored reviews with status=addressed
   whose ``addressed`` timestamp falls within the digest window.
2. Promotions to canonical — git-log mining for renames into
   ``stack/``, ``principles/``, ``architecture/`` paths in the window.
3. Open questions — KAL-LE-authored reviews with status=open across
   ALL configured projects (no window filter; open is open).
4. Cross-project patterns — emits a literal HTML-comment TODO marker;
   the LLM synthesis layer fills this in later.
5. Recurrences — KAL-LE reviews whose topic resurfaces and which have
   a sibling ``*—claude-disagreement.md`` archive.

If a section has zero entries, it still renders with "None this week."
text. Section header omission would conflate idle and broken.
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


_PROMOTION_DEST_PREFIXES: tuple[str, ...] = (
    "stack/", "principles/", "architecture/",
)


@dataclass
class Promotion:
    repo: str
    sha: str
    subject: str
    source: str
    destination: str


@dataclass
class Recurrence:
    project: str
    filename: str
    topic: str
    disagreement_filename: str


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


_RENAME_LINE = re.compile(r"^R\d+\s+(.+?)\s+(.+)$")


def collect_promotions(
    repo_paths: dict[str, Path],
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[Promotion]:
    """Mine ``git log --diff-filter=R --name-status`` across all repos.

    Returns one :class:`Promotion` per rename whose destination begins
    with ``stack/``, ``principles/``, or ``architecture/``. Repos that
    don't exist or fail to invoke git are skipped silently — the
    digest must not crash because one repo isn't a clone.
    """
    out: list[Promotion] = []
    since_iso = window_start.isoformat()
    until_iso = window_end.isoformat()
    for name, path in repo_paths.items():
        if not (path / ".git").exists() and not path.joinpath(".git").is_file():
            continue
        try:
            result = subprocess.run(
                [
                    "git", "log",
                    "--diff-filter=R",
                    "--name-status",
                    f"--since={since_iso}",
                    f"--until={until_iso}",
                    "--pretty=format:COMMIT %H %s",
                ],
                cwd=str(path),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.info(
                "digest.promotions.git_log_failed",
                repo=name,
                error=str(exc),
            )
            continue
        if result.returncode != 0:
            log.info(
                "digest.promotions.git_log_nonzero",
                repo=name,
                code=result.returncode,
                stderr=(result.stderr or "")[:200],
                stdout_tail=(result.stdout or "")[-200:],
            )
            continue
        out.extend(_parse_rename_log(name, result.stdout or ""))
    return out


def _parse_rename_log(repo: str, raw: str) -> list[Promotion]:
    """Walk ``git log --diff-filter=R --name-status`` output."""
    promotions: list[Promotion] = []
    current_sha = ""
    current_subject = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("COMMIT "):
            parts = line.split(" ", 2)
            current_sha = parts[1] if len(parts) >= 2 else ""
            current_subject = parts[2] if len(parts) >= 3 else ""
            continue
        m = _RENAME_LINE.match(line)
        if not m:
            continue
        source, destination = m.group(1), m.group(2)
        if not destination.startswith(_PROMOTION_DEST_PREFIXES):
            continue
        promotions.append(Promotion(
            repo=repo,
            sha=current_sha,
            subject=current_subject,
            source=source,
            destination=destination,
        ))
    return promotions


_TOPIC_NORMALIZE = re.compile(r"[^a-z0-9]+")


def _normalize_topic(topic: str) -> str:
    return _TOPIC_NORMALIZE.sub(" ", topic.lower()).strip()


def collect_recurrences(
    project_paths: dict[str, Path],
) -> list[Recurrence]:
    """Find KAL-LE reviews with a sibling ``*—claude-disagreement.md`` file.

    The em-dash is the literal character per the disagreement-archive
    convention. Each match is paired with the underlying KAL-LE review
    so the digest reader knows which thread recurred.
    """
    rows = list_all_kalle_reviews_with_paths(list(project_paths.values()))
    path_to_project: dict[str, str] = {
        str(p): name for name, p in project_paths.items()
    }
    out: list[Recurrence] = []
    seen: set[tuple[str, str]] = set()
    for vault_str, rec in rows:
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
    recurrences = collect_recurrences(project_paths)
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
    )


_LLM_PLACEHOLDER = "<!-- TODO: LLM synthesis layer not yet implemented -->"


def render(payload: DigestPayload) -> str:
    """Render the digest markdown. Empty sections still render."""
    parts: list[str] = []
    today_iso = payload.today.date().isoformat()
    start_iso = payload.window_start.date().isoformat()
    end_iso = payload.window_end.date().isoformat()
    parts.append(f"# Weekly digest — {today_iso}")
    parts.append("")
    parts.append(f"Window: {start_iso} → {end_iso}")
    parts.append("")

    parts.append("## Decisions made")
    parts.append("")
    if not payload.decisions:
        parts.append("None this week.")
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
        parts.append("None this week.")
    else:
        for promo in payload.promotions:
            short_sha = promo.sha[:8] if promo.sha else "?"
            parts.append(
                f"- [{promo.repo}] {promo.source} -> {promo.destination} "
                f"({short_sha}: {promo.subject})"
            )
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
        parts.append("None this week.")
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
    "Promotion",
    "Recurrence",
    "build_payload",
    "collect_decisions",
    "collect_open_questions",
    "collect_promotions",
    "collect_recurrences",
    "render",
    "resolve_repo_paths",
    "write_digest",
]
