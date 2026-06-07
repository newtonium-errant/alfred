"""Vault snapshot/diff tracking and inbox file processing.

The agent writes files directly — this module handles:
- Pre-agent vault snapshot (checksums)
- Post-agent diff (what changed)
- Marking inbox files as processed
"""

from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from .utils import get_logger

log = get_logger(__name__)


def snapshot_vault(vault_path: Path, ignore_dirs: list[str] | None = None) -> dict[str, str]:
    """Capture SHA-256 checksums of all .md files in the vault.

    Returns {relative_path: sha256_hex}.
    """
    ignore = set(ignore_dirs or [])
    checksums: dict[str, str] = {}

    for md_file in vault_path.rglob("*.md"):
        # Skip ignored directories
        rel = md_file.relative_to(vault_path)
        if any(part in ignore for part in rel.parts):
            continue
        try:
            content = md_file.read_bytes()
            checksums[str(rel)] = hashlib.sha256(content).hexdigest()
        except OSError:
            continue

    log.info("writer.snapshot", file_count=len(checksums))
    return checksums


def diff_vault(
    before: dict[str, str],
    after: dict[str, str],
) -> tuple[list[str], list[str]]:
    """Compare two vault snapshots.

    Returns (files_created, files_modified).
    """
    created: list[str] = []
    modified: list[str] = []

    for path, checksum in after.items():
        if path not in before:
            created.append(path)
        elif before[path] != checksum:
            modified.append(path)

    log.info("writer.diff", created=len(created), modified=len(modified))
    return created, modified


def _atomic_write(path: Path, content: str) -> None:
    """Write via temp file + replace for atomicity."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _is_binary(path: Path) -> bool:
    """Check if a file is binary by trying to decode the first 8KB as UTF-8."""
    try:
        with open(path, "rb") as f:
            f.read(8192).decode("utf-8")
        return False
    except (UnicodeDecodeError, OSError):
        return True


def mark_processed(
    inbox_file: Path,
    processed_dir: Path,
) -> Path:
    """Set status: processed in frontmatter and move to processed_dir.

    Returns the new path of the moved file.
    For binary files, skips frontmatter parsing and just moves the file.
    """
    if not _is_binary(inbox_file):
        # Update frontmatter only for text files — best-effort, never block the move
        try:
            post = frontmatter.load(str(inbox_file))
            post.metadata["status"] = "processed"
            post.metadata["processed_at"] = datetime.now(timezone.utc).isoformat()
            _atomic_write(inbox_file, frontmatter.dumps(post))
        except Exception:
            log.warning("writer.frontmatter_failed", file=str(inbox_file))

    # Move to processed dir
    processed_dir.mkdir(parents=True, exist_ok=True)
    dest = processed_dir / inbox_file.name

    # Handle name collisions
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        counter = 1
        while dest.exists():
            dest = processed_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    shutil.move(str(inbox_file), str(dest))
    log.info("writer.marked_processed", src=str(inbox_file), dest=str(dest))
    return dest


def mark_filtered(
    inbox_file: Path,
    processed_dir: Path,
    *,
    preference_slug: str,
    reason: str,
) -> Path:
    """Mark an inbox file as filtered-by-preference and move to processed_dir.

    Parallel to :func:`mark_processed` for the P10 / Ship 3 inbox-stage
    preference filter. Differences:

    - ``status`` set to ``filtered_by_preference`` (not ``processed``)
      so the operator-grep workflow distinguishes "LLM said this is
      done" from "filter dropped this before any LLM call."
    - Sidecar frontmatter fields ``filtered_at`` (ISO UTC timestamp),
      ``filtered_by_preference`` (slug of the matching pref), and
      ``filtered_reason`` (the matcher's grep-able motivation string)
      land alongside ``status`` so a single ``grep -l "status:
      filtered_by_preference"`` over ``processed/`` lists every filtered
      file, and a grep on ``filtered_by_preference: <slug>`` enumerates
      drops per pref.

    Reuses ``processed_dir`` (no separate ``filtered/`` directory) per
    operator decision 2026-06-07: the access pattern is grep on the
    sidecar field, not a separate-directory enumeration.

    Returns the new path of the moved file. Best-effort frontmatter
    augmentation matches the ``mark_processed`` contract: frontmatter
    parse failure logs a warning and still completes the file move —
    the audit trail (processed_dir move) is more important than the
    sidecar field landing.

    Binary files (the same heuristic as ``mark_processed``) skip the
    frontmatter step and just move. Filtered binary inbox files are
    rare-to-impossible (the sender extraction would have returned None
    upstream), but defensible to handle uniformly.
    """
    if not _is_binary(inbox_file):
        try:
            post = frontmatter.load(str(inbox_file))
            post.metadata["status"] = "filtered_by_preference"
            post.metadata["filtered_at"] = datetime.now(timezone.utc).isoformat()
            post.metadata["filtered_by_preference"] = preference_slug
            post.metadata["filtered_reason"] = reason
            _atomic_write(inbox_file, frontmatter.dumps(post))
        except Exception:
            log.warning("writer.filter_frontmatter_failed", file=str(inbox_file))

    processed_dir.mkdir(parents=True, exist_ok=True)
    dest = processed_dir / inbox_file.name

    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        counter = 1
        while dest.exists():
            dest = processed_dir / f"{stem}_{counter}{suffix}"
            counter += 1

    shutil.move(str(inbox_file), str(dest))
    log.info(
        "writer.marked_filtered",
        src=str(inbox_file),
        dest=str(dest),
        preference_slug=preference_slug,
    )
    return dest
