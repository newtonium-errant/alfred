"""Local "Captured Tasks" view (clinic-capture Piece 3).

A read-only markdown surface listing the ``task/`` records emitted by capture
(``created_by_capture: true``), written to ``process/Captured Tasks.md`` in the
CAPTURING instance's OWN vault. Mirrors ``pending_items/view.py`` (frontmatter +
atomic tmp->rename write) but is regenerated INLINE on capture — no daemon, no
debounce, no push. This is what makes captured tasks visible in the operator's
flow rather than Obsidian-only.

LOCAL ONLY BY CONSTRUCTION: this module reads the local vault and writes a local
markdown file. It has NO transport / pending_items / peer-push dependency — a
captured (potentially clinic/PHI) task never crosses an instance boundary here.
Cross-instance surfacing is the deferred de-ID arc, not this.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter

from .utils import get_logger

log = get_logger(__name__)

#: Where the view lands, relative to the vault root.
CAPTURED_TASKS_VIEW_REL = "process/Captured Tasks.md"

# Open statuses shown at the top (mirrors the task lifecycle order).
_OPEN_STATUSES = ("todo", "active", "blocked")


def _iter_captured_tasks(vault_path: Path) -> list[tuple[dict[str, Any], str]]:
    """Return ``(frontmatter, stem)`` for every ``task/*.md`` with
    ``created_by_capture: true``. Best-effort: an unparseable record is skipped,
    never fatal."""
    task_dir = Path(vault_path) / "task"
    out: list[tuple[dict[str, Any], str]] = []
    if not task_dir.is_dir():
        return out
    for path in sorted(task_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(path))
        except Exception:  # noqa: BLE001 — a broken record must not sink the view
            continue
        if post.metadata.get("created_by_capture") is True:
            out.append((post.metadata, path.stem))
    return out


def render_captured_tasks_view(
    vault_path: Path, *, generated_at: str | None = None,
) -> str:
    """Render the ``process/Captured Tasks.md`` markdown. Pure given the vault
    contents. Empty → an explicit ``(none)`` line (intentionally-left-blank:
    "ran, nothing captured" is distinguishable from a broken generator)."""
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    rows = _iter_captured_tasks(Path(vault_path))
    lines: list[str] = [
        "---",
        "type: process",
        'name: "Captured Tasks"',
        "source: capture_action_items",
        f'generated_at: "{generated_at}"',
        f"item_count: {len(rows)}",
        'tags: ["captured-tasks", "auto-generated"]',
        "---",
        "",
        "# Captured Tasks",
        "",
        "Action items emitted from voice/text captures "
        "(`created_by_capture: true`). Review, then set owner / due / priority.",
        "",
    ]

    def _bullet(fm: dict[str, Any], stem: str) -> str:
        name = str(fm.get("name") or stem)
        status = str(fm.get("status") or "todo")
        due = str(fm.get("due") or "").strip()
        conf = str(fm.get("capture_confidence") or "").strip()
        extra = []
        if due:
            extra.append(f"due {due}")
        if conf:
            extra.append(f"confidence: {conf}")
        suffix = f" — {', '.join(extra)}" if extra else ""
        return f"- [[task/{stem}]] — {name}{suffix}"

    open_rows = [(fm, s) for (fm, s) in rows
                 if str(fm.get("status") or "todo") in _OPEN_STATUSES]
    done_rows = [(fm, s) for (fm, s) in rows if (fm, s) not in open_rows]

    lines.append("## Open")
    if open_rows:
        lines += [_bullet(fm, s) for (fm, s) in open_rows]
    else:
        lines.append("(none)")
    lines.append("")

    if done_rows:
        lines.append("## Closed")
        lines += [_bullet(fm, s) for (fm, s) in done_rows]
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def regenerate_captured_tasks_view(vault_path: Path) -> bool:
    """Write ``process/Captured Tasks.md`` from the current vault. Atomic
    (tmp->rename). Returns True iff written. Best-effort: a write failure is
    observability leakage, not data loss (the task records are the source of
    truth) — logged, never raised."""
    target = Path(vault_path) / CAPTURED_TASKS_VIEW_REL
    try:
        text = render_captured_tasks_view(Path(vault_path))
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
        return True
    except OSError as exc:
        log.warning(
            "talker.capture.captured_tasks_view_failed",
            vault_path=str(vault_path), error=str(exc),
        )
        return False


__all__ = [
    "CAPTURED_TASKS_VIEW_REL",
    "render_captured_tasks_view",
    "regenerate_captured_tasks_view",
]
