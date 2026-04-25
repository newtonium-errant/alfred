"""Reviews storage — read/write KAL-LE review markdown files.

Storage layout: each project's reviews directory is
``<project-vault>/teams/alfred/reviews/``. KAL-LE-authored files use
the ``author: kal-le`` frontmatter discriminator; the existing
human-authored review format uses ``from``/``to``/``date``/``subject``/
``in_reply_to`` and stays untouched by this module.

All functions raise :class:`ReviewsError` with a clear message on
constraint violation (missing project dir, foreign-author file, etc.).
The CLI translates these into JSON error payloads and exit code 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter


KALLE_AUTHOR = "kal-le"
REVIEWS_SUBPATH = "teams/alfred/reviews"

STATUS_OPEN = "open"
STATUS_ADDRESSED = "addressed"
_VALID_STATUSES = {STATUS_OPEN, STATUS_ADDRESSED}


class ReviewsError(Exception):
    """Raised on any storage-layer constraint failure."""


@dataclass
class ReviewRecord:
    """One KAL-LE-authored review file, parsed."""

    filename: str
    abs_path: Path
    frontmatter: dict[str, Any]
    body: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "path": str(self.abs_path),
            "frontmatter": dict(self.frontmatter),
            "body": self.body,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


_SLUG_KEEP = re.compile(r"[^a-z0-9-]+")


def slugify(topic: str) -> str:
    """Lowercase, ASCII-only, dash-separated. Empty topic → ``review``."""
    if not topic:
        return "review"
    s = topic.strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = _SLUG_KEEP.sub("", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "review"


def reviews_dir(project_vault: Path) -> Path:
    return project_vault / REVIEWS_SUBPATH


def _ensure_reviews_dir(project_vault: Path) -> Path:
    if not project_vault.exists():
        raise ReviewsError(
            f"project vault does not exist: {project_vault}"
        )
    rdir = reviews_dir(project_vault)
    rdir.mkdir(parents=True, exist_ok=True)
    return rdir


def _is_kalle_authored(meta: dict[str, Any]) -> bool:
    return str(meta.get("author") or "").strip().lower() == KALLE_AUTHOR


_TIMESTAMP_FIELDS = ("created", "addressed")


def _coerce_timestamp_strings(meta: dict[str, Any]) -> dict[str, Any]:
    """Force timestamp fields back to ISO 8601 strings.

    PyYAML parses ``2026-04-25T10:00:00+00:00`` into a ``datetime`` and
    re-renders it with a space instead of ``T`` on dump, breaking the
    frontmatter round-trip that downstream filename/grep pipelines
    rely on. We coerce the canonical fields back to strings on load so
    the dump is stable.
    """
    for field_name in _TIMESTAMP_FIELDS:
        if field_name not in meta:
            continue
        value = meta[field_name]
        if isinstance(value, datetime):
            meta[field_name] = value.isoformat()
    return meta


def _load(path: Path) -> tuple[dict[str, Any], str]:
    """Load frontmatter + body. Raises ReviewsError on parse failure."""
    try:
        post = frontmatter.load(str(path))
    except Exception as exc:  # noqa: BLE001
        raise ReviewsError(f"frontmatter parse failed: {path.name}: {exc}") from exc
    meta = _coerce_timestamp_strings(dict(post.metadata or {}))
    return meta, post.content or ""


def _dump(path: Path, meta: dict[str, Any], body: str) -> None:
    post = frontmatter.Post(body, **meta)
    rendered = frontmatter.dumps(post)
    path.write_text(rendered + ("\n" if not rendered.endswith("\n") else ""), encoding="utf-8")


def _next_unique_filename(rdir: Path, base: str) -> str:
    """Suffix ``-2``, ``-3``, ... until the filename is free."""
    candidate = f"{base}.md"
    if not (rdir / candidate).exists():
        return candidate
    n = 2
    while True:
        candidate = f"{base}-{n}.md"
        if not (rdir / candidate).exists():
            return candidate
        n += 1


def write_review(
    project_vault: Path,
    *,
    project: str,
    topic: str,
    body: str,
    today: str | None = None,
    now_iso: str | None = None,
) -> ReviewRecord:
    """Write a new KAL-LE-authored review. Returns the persisted record."""
    rdir = _ensure_reviews_dir(project_vault)
    date_part = today or _today_iso()
    slug = slugify(topic)
    base = f"{date_part}-{slug}"
    filename = _next_unique_filename(rdir, base)
    abs_path = rdir / filename

    meta: dict[str, Any] = {
        "type": "review",
        "author": KALLE_AUTHOR,
        "project": project,
        "status": STATUS_OPEN,
        "created": now_iso or _now_iso(),
        "topic": topic.strip(),
    }
    _dump(abs_path, meta, body)
    return ReviewRecord(
        filename=filename, abs_path=abs_path, frontmatter=meta, body=body,
    )


def list_reviews(
    project_vault: Path,
    *,
    status: str = STATUS_OPEN,
) -> list[ReviewRecord]:
    """List KAL-LE-authored reviews. ``status`` accepts ``open``,
    ``addressed``, or ``all``. Files lacking ``author: kal-le`` are
    skipped (the human-authored convention). Returns oldest-first by
    ``created`` timestamp; falls back to filename for missing values.
    """
    if status not in (*_VALID_STATUSES, "all"):
        raise ReviewsError(
            f"invalid status: {status!r} (expected open|addressed|all)"
        )
    rdir = reviews_dir(project_vault)
    if not rdir.is_dir():
        return []
    out: list[ReviewRecord] = []
    for path in sorted(rdir.glob("*.md")):
        try:
            meta, body = _load(path)
        except ReviewsError:
            continue
        if not _is_kalle_authored(meta):
            continue
        if status != "all":
            entry_status = str(meta.get("status") or "").strip().lower()
            if entry_status != status:
                continue
        out.append(
            ReviewRecord(
                filename=path.name, abs_path=path,
                frontmatter=meta, body=body,
            )
        )
    out.sort(key=lambda r: (str(r.frontmatter.get("created") or ""), r.filename))
    return out


def list_all_kalle_reviews_with_paths(
    project_vault_paths: list[Path],
) -> list[tuple[str, ReviewRecord]]:
    """Return ``(project_vault_str, ReviewRecord)`` rows across many vaults.

    Used by the digest module to enumerate open questions across all
    projects without window filtering. Vaults that don't exist or have
    no reviews dir are skipped silently.
    """
    out: list[tuple[str, ReviewRecord]] = []
    for vault in project_vault_paths:
        if not vault.is_dir():
            continue
        rdir = reviews_dir(vault)
        if not rdir.is_dir():
            continue
        for path in sorted(rdir.glob("*.md")):
            try:
                meta, body = _load(path)
            except ReviewsError:
                continue
            if not _is_kalle_authored(meta):
                continue
            out.append(
                (str(vault), ReviewRecord(
                    filename=path.name, abs_path=path,
                    frontmatter=meta, body=body,
                ))
            )
    return out


def read_review(
    project_vault: Path,
    *,
    filename: str,
) -> ReviewRecord:
    """Read a specific review file. Raises ReviewsError when missing or
    when the file isn't KAL-LE-authored.
    """
    rdir = reviews_dir(project_vault)
    abs_path = rdir / filename
    if not abs_path.is_file():
        raise ReviewsError(f"review not found: {filename}")
    meta, body = _load(abs_path)
    if not _is_kalle_authored(meta):
        actual = meta.get("author") or meta.get("from") or "(no author/from field)"
        raise ReviewsError(
            f"refusing to read non-KAL-LE review: {filename} "
            f"(author={actual!r}); reviews CLI only operates on "
            f"author={KALLE_AUTHOR!r} files"
        )
    return ReviewRecord(
        filename=filename, abs_path=abs_path,
        frontmatter=meta, body=body,
    )


def mark_addressed(
    project_vault: Path,
    *,
    filename: str,
    now_iso: str | None = None,
) -> ReviewRecord:
    """Flip ``status: open`` → ``addressed`` and stamp ``addressed:``.

    Errors loudly on a non-KAL-LE file — the loud failure is the
    discriminator's whole point. Idempotent re-runs on an already-
    addressed file refresh the ``addressed`` timestamp; returning
    early would silently mask a caller bug, so we re-stamp.
    """
    rdir = reviews_dir(project_vault)
    abs_path = rdir / filename
    if not abs_path.is_file():
        raise ReviewsError(f"review not found: {filename}")
    meta, body = _load(abs_path)
    if not _is_kalle_authored(meta):
        actual = meta.get("author") or meta.get("from") or "(no author/from field)"
        raise ReviewsError(
            f"refusing to mark-addressed on non-KAL-LE review: {filename} "
            f"(author={actual!r}); reviews CLI only operates on "
            f"author={KALLE_AUTHOR!r} files"
        )
    meta["status"] = STATUS_ADDRESSED
    meta["addressed"] = now_iso or _now_iso()
    _dump(abs_path, meta, body)
    return ReviewRecord(
        filename=filename, abs_path=abs_path,
        frontmatter=meta, body=body,
    )


__all__ = [
    "KALLE_AUTHOR",
    "REVIEWS_SUBPATH",
    "STATUS_ADDRESSED",
    "STATUS_OPEN",
    "ReviewRecord",
    "ReviewsError",
    "list_all_kalle_reviews_with_paths",
    "list_reviews",
    "mark_addressed",
    "read_review",
    "reviews_dir",
    "slugify",
    "write_review",
]
