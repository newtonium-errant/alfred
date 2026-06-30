"""Project registry — the name → inbox-dir map the router places into.

Built from the ``message_bus.projects`` config block. KAL-LE (the broker)
uses it to route; Salem (the operator-facing principal) carries the same
block read-only so the brief section can render unread counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ProjectEntry:
    """One registered project: a slug + its inbox directory."""

    name: str
    inbox_path: str


class ProjectRegistry:
    """Name → :class:`ProjectEntry` lookup. ``read_dir_for`` is the
    drained-message archive (``<inbox>/read``)."""

    def __init__(self, entries: list[ProjectEntry]) -> None:
        # Preserve declaration order; last entry wins on a duplicate name.
        self._by_name: dict[str, ProjectEntry] = {}
        for entry in entries:
            if entry.name:
                self._by_name[entry.name] = entry

    def get(self, name: str) -> ProjectEntry | None:
        return self._by_name.get(name)

    def names(self) -> list[str]:
        return list(self._by_name.keys())

    def entries(self) -> list[ProjectEntry]:
        return list(self._by_name.values())

    def inbox_for(self, name: str) -> Path | None:
        entry = self._by_name.get(name)
        return Path(entry.inbox_path) if entry is not None else None

    def read_dir_for(self, name: str) -> Path | None:
        inbox = self.inbox_for(name)
        return inbox / "read" if inbox is not None else None


def load_registry(raw_projects: Any) -> ProjectRegistry:
    """Build a :class:`ProjectRegistry` from the raw ``projects`` list.

    Each item is ``{name, inbox_path}``. Items missing either field are
    skipped (a partial config never crashes the loader — same tolerance as
    the rest of the config layer)."""
    entries: list[ProjectEntry] = []
    if isinstance(raw_projects, list):
        for item in raw_projects:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "") or "")
            inbox_path = str(item.get("inbox_path", "") or "")
            if name and inbox_path:
                entries.append(ProjectEntry(name=name, inbox_path=inbox_path))
    return ProjectRegistry(entries)


__all__ = ["ProjectEntry", "ProjectRegistry", "load_registry"]
