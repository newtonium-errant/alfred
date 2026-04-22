"""Upcoming Events section — forward-looking calendar slice for the Morning Brief.

Phase 1 (intentionally rule-free): scan all ``event`` and ``task`` records,
bucket by date relative to today (Halifax), drop anything more than 30 days
out. Filter rules grow inline as real-data patterns reveal what's noise.

Sources:
- ``event`` records via frontmatter ``date`` (required ISO date string).
- ``task`` records via frontmatter ``due`` (optional; tasks without ``due``
  are excluded entirely).

``remind_at`` is intentionally NOT a source here — it already drives the
outbound transport scheduler and would create duplicate user-visible noise.

Buckets (relative to ``today``):
- **Today** — ``date == today``
- **This Week** — ``today < date <= today + 7d``
- **Later** — ``today + 7d < date <= today + max_days_ahead``

Empty buckets are omitted. If all three are empty, the renderer emits a
literal "No upcoming events." marker so operators know the section ran
rather than crashing silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import frontmatter

from .config import UpcomingEventsConfig
from .utils import get_logger

log = get_logger(__name__)


# Directories never worth scanning. Mirrors the conservative defaults the
# Operations section's vault counter uses.
_IGNORE_DIRS: frozenset[str] = frozenset(
    {"_templates", "_bases", "_docs", ".obsidian", "view", "session", "inbox"}
)


@dataclass(frozen=True)
class _UpcomingItem:
    """One row in the rendered section. Sortable by (date_iso, name)."""

    date_iso: str
    name: str
    location: str | None
    description: str | None


def _coerce_date(value: Any) -> date | None:
    """Best-effort coerce a frontmatter ``date``/``due`` value to a ``date``.

    ``python-frontmatter`` returns either a ``datetime.date`` (when YAML
    parsed it as a date scalar) or a string (when the value was quoted).
    Anything else (None, list, malformed) -> None and the record is skipped.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _iter_records(vault_path: Path) -> list[tuple[Path, dict]]:
    """Walk the vault and return (path, frontmatter_dict) for every .md file
    that isn't in an ignored directory. Inline frontmatter read because
    Phase 1 needs full metadata access and ``vault_list`` only returns
    name/path/type/status."""
    out: list[tuple[Path, dict]] = []
    if not vault_path.exists():
        return out
    for md_file in vault_path.rglob("*.md"):
        try:
            rel = md_file.relative_to(vault_path)
        except ValueError:
            continue
        if any(part in _IGNORE_DIRS for part in rel.parts):
            continue
        try:
            post = frontmatter.load(str(md_file))
        except Exception:
            continue
        out.append((md_file, dict(post.metadata)))
    return out


def _collect_items(
    vault_path: Path,
    today: date,
    max_days_ahead: int,
) -> list[_UpcomingItem]:
    """Pull events + tasks whose date/due falls in [today, today+max_days_ahead]."""
    cutoff = today.toordinal() + max_days_ahead
    items: list[_UpcomingItem] = []
    for path, fm in _iter_records(vault_path):
        rec_type = fm.get("type")
        if rec_type == "event":
            d = _coerce_date(fm.get("date"))
        elif rec_type == "task":
            d = _coerce_date(fm.get("due"))
        else:
            continue
        if d is None:
            continue
        if d.toordinal() < today.toordinal():
            continue
        if d.toordinal() > cutoff:
            continue
        name = (
            fm.get("name")
            or fm.get("subject")
            or path.stem
        )
        location = fm.get("location")
        description = fm.get("description")
        items.append(
            _UpcomingItem(
                date_iso=d.isoformat(),
                name=str(name),
                location=str(location) if location else None,
                description=str(description) if description else None,
            )
        )
    return items


def _bucket(items: list[_UpcomingItem], today: date) -> dict[str, list[_UpcomingItem]]:
    """Split items into Today / This Week / Later buckets."""
    buckets: dict[str, list[_UpcomingItem]] = {
        "Today": [],
        "This Week": [],
        "Later": [],
    }
    today_ord = today.toordinal()
    week_ord = today_ord + 7
    for item in items:
        item_ord = date.fromisoformat(item.date_iso).toordinal()
        if item_ord == today_ord:
            buckets["Today"].append(item)
        elif item_ord <= week_ord:
            buckets["This Week"].append(item)
        else:
            buckets["Later"].append(item)
    for key in buckets:
        buckets[key].sort(key=lambda x: (x.date_iso, x.name))
    return buckets


def _render_item(item: _UpcomingItem) -> str:
    """Render one item as one or two markdown lines."""
    head = f"- {item.date_iso} — {item.name}"
    if item.location:
        head += f" ({item.location})"
    if item.description:
        head += f"\n  *{item.description}*"
    return head


def render_upcoming_events_section(
    config: UpcomingEventsConfig,
    vault_path: str | Path,
    today: date,
) -> str:
    """Render the Upcoming Events section body markdown.

    Returns an empty string if the section is disabled in config — the
    daemon uses that as a signal to omit the section entirely. A
    populated string (including the "No upcoming events." sentinel) means
    the section header should be emitted.
    """
    if not config.enabled:
        return ""

    vault = Path(vault_path)
    items = _collect_items(vault, today, config.max_days_ahead)
    buckets = _bucket(items, today)

    section_parts: list[str] = []
    for bucket_name in ("Today", "This Week", "Later"):
        bucket_items = buckets[bucket_name]
        if not bucket_items:
            continue
        section_parts.append(f"### {bucket_name}")
        for item in bucket_items:
            section_parts.append(_render_item(item))
        section_parts.append("")

    if not section_parts:
        return "No upcoming events."

    # Drop trailing blank line for cleanliness.
    while section_parts and section_parts[-1] == "":
        section_parts.pop()
    return "\n".join(section_parts)
