"""Shared dataclasses for the Alfred health / BIT system.

Three primitives:
    Status        — one of OK / WARN / FAIL / SKIP
    CheckResult   — the result of one individual probe (one line in output)
    ToolHealth    — a tool-level rollup: status + list of CheckResult
    HealthReport  — the whole run: started_at / finished_at / per-tool

The aggregator builds a ``HealthReport`` by calling each registered
``health_check(raw) -> ToolHealth`` and collating. Renderers consume
the ``HealthReport`` — there's no human/JSON logic in the dataclasses
themselves.

Design note: we keep these plain dataclasses (no ``from alfred.something
import everything``) so ``alfred/health/types.py`` can be imported by
each tool's ``health.py`` without setting off a circular import. The
types layer depends on nothing except stdlib.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class Status(str, enum.Enum):
    """Four-state health status.

    Inherits from ``str`` so the value serializes cleanly to JSON
    without a custom encoder — ``json.dumps(result)`` turns
    ``Status.OK`` into ``"ok"``.
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"

    @classmethod
    def worst(cls, statuses: list["Status"]) -> "Status":
        """Return the most severe status in the list.

        Order of severity (worst first): FAIL > WARN > SKIP > OK.
        SKIP ranks above OK because a SKIP means "we didn't check" —
        the user should see that before they see a green ok.
        Empty list returns OK (nothing checked, nothing broken).
        """
        if not statuses:
            return cls.OK
        order = {cls.FAIL: 3, cls.WARN: 2, cls.SKIP: 1, cls.OK: 0}
        return max(statuses, key=lambda s: order.get(s, 0))


@dataclass
class CheckResult:
    """Result of one probe within a tool.

    ``name`` is the short label shown in human output (e.g. "anthropic auth",
    "ollama reachable"). ``status`` is one of the four Status values.
    ``detail`` is a one-line human-readable explanation.  ``latency_ms``
    is optional — populated for network probes, omitted for static checks.
    ``data`` is an open dict for anything else the renderer might want
    (endpoint URL, model name, etc.) — it serializes straight into the
    JSON output.
    """

    name: str
    status: Status
    detail: str = ""
    latency_ms: float | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolHealth:
    """Aggregate of one tool's checks.

    ``status`` is typically derived from ``Status.worst([r.status for r in
    results])`` — but callers can set it explicitly (e.g., a tool whose
    config section is absent might return ``Status.SKIP`` with an empty
    ``results`` list and a ``detail`` that explains why).

    ``elapsed_ms`` is the total wall-clock time spent in the tool's
    ``health_check`` callable, set by the aggregator — individual tool
    checks don't need to populate it themselves.
    """

    tool: str
    status: Status
    results: list[CheckResult] = field(default_factory=list)
    detail: str = ""
    elapsed_ms: float | None = None


@dataclass
class HealthReport:
    """The full output of one health run (``alfred check`` or BIT run).

    ``mode`` is ``"quick"`` or ``"full"`` — determined by the caller
    (the CLI or the BIT daemon) and carried through to the output so
    readers know whether this is the pre-brief sanity check or the
    evening deep run. ``overall_status`` is derived from the worst
    tool status, set by the aggregator.
    """

    mode: str
    started_at: str
    finished_at: str
    overall_status: Status
    tools: list[ToolHealth] = field(default_factory=list)
    elapsed_ms: float | None = None
