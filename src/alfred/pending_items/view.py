"""Vault markdown view regenerator for the pending-items queue.

Produces ``vault/process/Pending Items.md`` — a read-only view of
the current pending items so Andrew can see them at a glance in
Obsidian without opening a Telegram conversation. Regenerated on
every queue mutation, debounced to ≤1 write per
``view_debounce_seconds``.

Read-only: this module never writes to the JSONL queue. A regression
that wrote to the markdown file would NOT update the queue — the
JSONL is the source of truth and the markdown is purely decorative.

The frontmatter mirrors the convention from
:mod:`alfred.transport.peer_handlers._handle_peer_brief_digest`
("type: process", "source: pending_items_queue") so a Bases view can
filter on the source field.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .queue import PendingItem, list_pending


# Module-level debounce state. Per-path so two parallel queues (in
# tests, e.g.) don't trample each other.
_LAST_WRITE_BY_PATH: dict[str, float] = {}
_DEBOUNCE_LOCK = threading.Lock()


def _now_monotonic() -> float:
    """Monotonic clock — wraps for tests."""
    return time.monotonic()


def render_view(items: Iterable[PendingItem], *, generated_at: str | None = None) -> str:
    """Render the pending-items markdown view.

    The format groups by category, lists each item's context + the
    label of each resolution option. Each item is a Markdown bullet
    that includes the item id (truncated) so operators can correlate
    with the JSONL audit trail by grep.
    """
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    items_list = list(items)

    lines: list[str] = [
        "---",
        "type: process",
        'name: "Pending Items"',
        "source: pending_items_queue",
        f'generated_at: "{generated_at}"',
        f"item_count: {len(items_list)}",
        'tags: ["pending-items", "auto-generated"]',
        "---",
        "",
        "# Pending Items",
        "",
        (
            "Auto-generated read-only view of the local pending-items "
            "queue. Source of truth: `data/pending_items.jsonl`. "
            "Resolutions happen via Salem's Daily Sync — do not edit "
            "this file."
        ),
        "",
    ]

    if not items_list:
        lines.append("No pending items.")
        lines.append("")
        return "\n".join(lines)

    by_category: dict[str, list[PendingItem]] = {}
    for item in items_list:
        by_category.setdefault(item.category, []).append(item)

    for category in sorted(by_category.keys()):
        bucket = by_category[category]
        lines.append(f"## {category} ({len(bucket)})")
        lines.append("")
        for item in bucket:
            short_id = item.id[:8] if item.id else "????????"
            lines.append(
                f"- **{short_id}** — {item.context.strip() or '(no context)'}"
            )
            if item.created_by_instance:
                lines.append(
                    f"    - originating instance: `{item.created_by_instance}`"
                )
            if item.session_id:
                lines.append(
                    f"    - session: `{item.session_id}`"
                )
            lines.append(f"    - created: `{item.created_at}`")
            if item.resolution_options:
                opts = ", ".join(o.label for o in item.resolution_options if o.label)
                if opts:
                    lines.append(f"    - options: {opts}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def regenerate_view(
    queue_path: str | Path,
    view_path: str | Path,
    *,
    debounce_seconds: int = 30,
    force: bool = False,
) -> bool:
    """Regenerate the markdown view from the queue, debounced.

    Returns True iff a fresh file was written. ``force=True`` skips
    the debounce gate (used at daemon startup + tests).

    Debounce is per-view-path so concurrent regenerations of two
    different paths in the same process don't block each other. The
    gate is monotonic-clock-based so it's robust against system clock
    adjustments.
    """
    view_path_str = str(view_path)
    queue_path_str = str(queue_path)
    if not view_path_str:
        return False

    if not force and debounce_seconds > 0:
        with _DEBOUNCE_LOCK:
            last = _LAST_WRITE_BY_PATH.get(view_path_str, 0.0)
            now = _now_monotonic()
            if (now - last) < debounce_seconds:
                return False
            # Reserve the slot so a second concurrent caller within
            # the window doesn't also write.
            _LAST_WRITE_BY_PATH[view_path_str] = now

    items = list_pending(queue_path_str)
    text = render_view(items)
    target = Path(view_path_str)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write — temp + rename — so a crash mid-write doesn't
        # leave a partial markdown file that Obsidian renders as
        # garbage.
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target)
        return True
    except OSError:
        # Best-effort: failing to regenerate the view is observability
        # leakage, not data loss. The queue itself is unaffected.
        return False


def reset_debounce_for_tests(view_path: str | Path | None = None) -> None:
    """Clear the debounce state — test helper, never called in production.

    Without this, two test cases that exercise the same view path
    serially can hit the debounce window and skip the second write.
    """
    with _DEBOUNCE_LOCK:
        if view_path is None:
            _LAST_WRITE_BY_PATH.clear()
        else:
            _LAST_WRITE_BY_PATH.pop(str(view_path), None)


__all__ = [
    "regenerate_view",
    "render_view",
    "reset_debounce_for_tests",
]
