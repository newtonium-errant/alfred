"""Message-bus config — the ``message_bus:`` section (mirror
``load_ticket_forward_config``).

KAL-LE (the broker) sets ``enabled: true`` + the spool + the project
registry. Other instances carry the same ``projects`` block read-only (so
the brief section renders) and leave ``enabled`` off.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .registry import ProjectEntry, ProjectRegistry, load_registry


# Tool-scoped default per the CLAUDE.md state-path rule.
DEFAULT_MESSAGE_BUS_STATE_PATH = "./data/message_bus_state.json"

# Neutral per-user spool default (operator decision: NOT tied to one
# project). Computed per-user via expanduser rather than a hardcoded
# ``/home/<x>`` literal, so it's correct on any box.
DEFAULT_SPOOL_PATH = os.path.expanduser("~/.msgbus/spool")


@dataclass
class MessageBusConfig:
    """Typed view of the ``message_bus:`` config section."""

    enabled: bool = False
    # Default project for ``alfred msg inbox`` when no project arg is given.
    self_project: str = ""
    interval_minutes: int = 5
    spool_path: str = DEFAULT_SPOOL_PATH
    state_path: str = DEFAULT_MESSAGE_BUS_STATE_PATH
    notify_telegram: bool = False
    projects: list[ProjectEntry] = field(default_factory=list)

    def registry(self) -> ProjectRegistry:
        return ProjectRegistry(self.projects)


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_message_bus_config(raw: dict[str, Any]) -> MessageBusConfig:
    """Build :class:`MessageBusConfig` from the unified config dict.

    Tolerant of an absent block (returns all-default, ``enabled=False``) so
    the code is byte-inert on a box without the section. ``state.path``
    nests under a ``state:`` sub-block (the tool-state convention);
    ``notify.telegram`` under ``notify:``; the registry under ``projects:``.
    Hand-rolled (not routed through the generic ``_build``) to avoid the
    ``_DATACLASS_MAP`` collision footgun on ``state``.
    """
    section = raw.get("message_bus") or {}
    if not isinstance(section, dict):
        return MessageBusConfig()

    state_raw = section.get("state") or {}
    state_path = ""
    if isinstance(state_raw, dict):
        state_path = str(state_raw.get("path", "") or "")

    notify_raw = section.get("notify") or {}
    notify_telegram = bool(
        notify_raw.get("telegram", False)
        if isinstance(notify_raw, dict)
        else False
    )

    projects = load_registry(section.get("projects")).entries()

    return MessageBusConfig(
        enabled=bool(section.get("enabled", False)),
        self_project=str(section.get("self_project", "") or ""),
        interval_minutes=_coerce_int(section.get("interval_minutes", 5), 5),
        spool_path=str(section.get("spool_path", "") or "") or DEFAULT_SPOOL_PATH,
        state_path=state_path or DEFAULT_MESSAGE_BUS_STATE_PATH,
        notify_telegram=notify_telegram,
        projects=projects,
    )


__all__ = [
    "DEFAULT_MESSAGE_BUS_STATE_PATH",
    "DEFAULT_SPOOL_PATH",
    "MessageBusConfig",
    "load_message_bus_config",
]
