"""Health-check aggregator.

Fans out to each registered ``<tool>.health.health_check(raw, mode)``
callable concurrently via ``asyncio.gather``, collates results into a
``HealthReport``, and returns. Timeouts and exceptions in individual
tool checks are caught here so one misbehaving tool can't take down
the whole run.

The registry is lazily populated — each tool's ``health.py`` calls
:func:`register_check` at import time. The aggregator imports
``alfred.<tool>.health`` for every registered tool it knows about,
which triggers the registration. This keeps the dependency direction
sane: ``health/`` depends on nothing, each tool's ``health.py`` depends
only on ``alfred.health`` (the types) and its own tool's internals.

**Recursion guard:** ``bit`` is explicitly excluded from the set of
tools the aggregator probes. Per the plan (Part 7), running the
aggregator from inside the BIT daemon would recurse forever if BIT
checked itself. The BIT daemon's own liveness/state is surfaced
separately (via ``alfred bit status``, not via ``alfred check``).
"""

from __future__ import annotations

import asyncio
import importlib
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from .types import CheckResult, HealthReport, Status, ToolHealth


# Known tools — the aggregator will try to import ``alfred.<tool>.health``
# for each of these. Absent modules are silently skipped (the tool may not
# be installed, e.g. surveyor without its optional deps). ``bit`` is
# deliberately omitted — see module docstring.
KNOWN_TOOLS: tuple[str, ...] = (
    "curator",
    "janitor",
    "distiller",
    "surveyor",
    "brief",
    "mail",
    "talker",
)


# Per-tool timeout for ``quick`` mode in seconds. ``full`` mode gets a
# 15s budget per tool (plan Part 11 Q2). These are per-tool, not per-check —
# the tool's ``health_check`` is responsible for subdividing its own time
# budget across the probes it runs.
QUICK_TIMEOUT_SECONDS = 5.0
FULL_TIMEOUT_SECONDS = 15.0


# Registry: tool_name -> callable(raw, mode) -> Awaitable[ToolHealth]
CheckCallable = Callable[[dict[str, Any], str], Awaitable[ToolHealth]]
_REGISTRY: dict[str, CheckCallable] = {}


def register_check(tool: str, check: CheckCallable) -> None:
    """Register a tool's health check.

    Called by each ``<tool>.health`` module at import time.  Subsequent
    calls overwrite — useful for tests that want to stub a check out.
    """
    _REGISTRY[tool] = check


def clear_registry() -> None:
    """Reset the registry. Intended for tests."""
    _REGISTRY.clear()


def _load_tool_checks() -> None:
    """Attempt to import every known tool's health module.

    Import errors are swallowed — a tool may simply not be installed
    (e.g. surveyor without ML extras) or not yet have a ``health.py``
    written. The side-effect of a successful import is registration via
    :func:`register_check`.
    """
    for tool in KNOWN_TOOLS:
        mod = f"alfred.{tool}.health"
        try:
            importlib.import_module(mod)
        except ImportError:
            # Tool not installed or health module not present — that's fine,
            # the aggregator simply won't run a check for it.
            continue
        except Exception:  # noqa: BLE001
            # A broken health module shouldn't take down the aggregator.
            # We swallow and continue; the missing entry is visible to
            # callers (the tool won't appear in the report).
            continue


async def _run_one(
    tool: str,
    check: CheckCallable,
    raw: dict[str, Any],
    mode: str,
    timeout: float,
) -> ToolHealth:
    """Run one tool's check with a timeout + exception guard.

    Converts exceptions and timeouts into a FAIL ``ToolHealth`` so the
    aggregator's output shape is uniform — callers never have to handle
    partial results. Elapsed wall time is measured here and stored on
    the returned ToolHealth.
    """
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(check(raw, mode), timeout=timeout)
        if result.elapsed_ms is None:
            result.elapsed_ms = (time.monotonic() - started) * 1000.0
        return result
    except asyncio.TimeoutError:
        return ToolHealth(
            tool=tool,
            status=Status.FAIL,
            detail=f"health check timed out after {timeout:.0f}s",
            elapsed_ms=(time.monotonic() - started) * 1000.0,
            results=[
                CheckResult(
                    name="timeout",
                    status=Status.FAIL,
                    detail=f"exceeded {timeout:.0f}s budget in mode={mode}",
                )
            ],
        )
    except Exception as exc:  # noqa: BLE001
        return ToolHealth(
            tool=tool,
            status=Status.FAIL,
            detail=f"health check raised: {exc.__class__.__name__}: {exc}",
            elapsed_ms=(time.monotonic() - started) * 1000.0,
            results=[
                CheckResult(
                    name="exception",
                    status=Status.FAIL,
                    detail=f"{exc.__class__.__name__}: {exc}",
                )
            ],
        )


async def run_all_checks(
    raw: dict[str, Any],
    mode: str = "quick",
    tools: list[str] | None = None,
) -> HealthReport:
    """Run all registered health checks and return a ``HealthReport``.

    Args:
        raw: Unified config dict (the same one ``cli._load_unified_config``
            returns). Each tool's check extracts what it needs from here.
        mode: ``"quick"`` (default, ~5s per tool) or ``"full"`` (~15s).
            The mode is passed through to each tool so it can choose
            lighter probes when time-constrained.
        tools: Optional explicit list of tools to check. ``None`` runs
            every registered tool. ``"bit"`` in the list is silently
            filtered out — see module docstring.

    Returns:
        A ``HealthReport`` with one ``ToolHealth`` per tool that was
        probed (tools whose config section is missing typically return
        ``Status.SKIP`` from their own check; see the per-tool health
        modules for the convention).
    """
    _load_tool_checks()

    timeout = FULL_TIMEOUT_SECONDS if mode == "full" else QUICK_TIMEOUT_SECONDS
    started_dt = datetime.now(timezone.utc)
    started_mono = time.monotonic()

    # Pick the set of tools to actually run.
    if tools is None:
        targets = list(_REGISTRY.keys())
    else:
        targets = [t for t in tools if t in _REGISTRY]

    # Recursion guard — the BIT daemon must not probe itself.
    targets = [t for t in targets if t != "bit"]

    if not targets:
        # Empty registry — return an OK report with no tools. Keeps
        # callers from having to special-case empty output.
        finished_dt = datetime.now(timezone.utc)
        return HealthReport(
            mode=mode,
            started_at=started_dt.isoformat(),
            finished_at=finished_dt.isoformat(),
            overall_status=Status.OK,
            tools=[],
            elapsed_ms=(time.monotonic() - started_mono) * 1000.0,
        )

    # Run all checks concurrently with per-tool timeouts.
    coros = [
        _run_one(tool, _REGISTRY[tool], raw, mode, timeout)
        for tool in targets
    ]
    tool_healths = await asyncio.gather(*coros)

    finished_dt = datetime.now(timezone.utc)
    overall = Status.worst([th.status for th in tool_healths])

    return HealthReport(
        mode=mode,
        started_at=started_dt.isoformat(),
        finished_at=finished_dt.isoformat(),
        overall_status=overall,
        tools=list(tool_healths),
        elapsed_ms=(time.monotonic() - started_mono) * 1000.0,
    )
