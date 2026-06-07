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


# Known tools mapped to the Python module path that hosts their
# ``health_check`` callable. Most tools live under ``alfred.<tool>``,
# but the talker's module is historically ``alfred.telegram.*`` —
# we surface it to users as ``talker`` but the health module lives
# alongside the rest of the telegram code. Absent modules are silently
# skipped (the tool may not be installed, e.g. surveyor without its
# optional deps). ``bit`` is deliberately omitted — see module docstring.
KNOWN_TOOL_MODULES: dict[str, str] = {
    "curator": "alfred.curator.health",
    "janitor": "alfred.janitor.health",
    "distiller": "alfred.distiller.health",
    "instructor": "alfred.instructor.health",
    "surveyor": "alfred.surveyor.health",
    "brief": "alfred.brief.health",
    "mail": "alfred.mail.health",
    "talker": "alfred.telegram.health",
    "transport": "alfred.transport.health",
    "daily_sync": "alfred.daily_sync.health",
    "cloudflared": "alfred.cloudflared.health",
    "gcal": "alfred.integrations.gcal_health",
}


# Per-tool timeout for ``quick`` mode in seconds. ``full`` mode gets a
# 15s budget per tool (plan Part 11 Q2). These are per-tool, not per-check —
# the tool's ``health_check`` is responsible for subdividing its own time
# budget across the probes it runs.
#
# 2026-06-07 widen 5.0 → 10.0 (P16): the original 5s value was tuned
# against sonnet-fast probes (talker probe baseline ~1.3s). Three tools
# — curator, janitor, distiller — use ``claude-haiku-4-5`` for their
# shared anthropic-auth probe (see ``alfred.health.anthropic_auth``)
# and routinely run 3–4s on a healthy day:
#
#     Tool       | Baseline   | Old 5s headroom
#     -----------|-----------|------------------
#     curator    | ~3.3s     | 1.7s good, 0 on a bad day
#     janitor    | ~2.9s     | 2.1s good, 0.5s on a bad day
#     distiller  | ~3.0s     | 2.0s good, 0.4s on a bad day
#     talker     | ~1.3s     | 3.7s comfortable
#
# 2026-06-07 morning: a regional Anthropic ``claude-haiku-4-5``
# latency spike drove the curator probe to 6.2s on the 5s budget,
# producing a false-positive timeout FAIL on a BIT cycle. Six minutes
# later a fresh probe ran in 125ms — root cause was Anthropic-side
# latency variance, not a bug in any curator code path. The 10s
# budget gives ~2.5x current haiku-baseline headroom even on a bad
# API day, while keeping a reasonable cap on a genuinely-stuck health
# check. ``FULL_TIMEOUT_SECONDS`` stays at 15s — ample headroom there
# for the haiku-using tools (10s+ over haiku baselines).
#
# Future widen / narrow: change the literal AND
# ``tests/health/test_aggregator.py::test_quick_timeout_constant_widened_to_10s``
# together so a deliberate code-review touch ratifies the new value.
QUICK_TIMEOUT_SECONDS = 10.0
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
    """Ensure every known tool's health check is registered.

    We import each tool's ``health`` module (lazily — absent or broken
    modules are silently skipped) and, as a belt-and-braces step,
    introspect the imported module for a ``health_check`` callable
    and register it under the tool name. The introspection step makes
    the aggregator robust to ``clear_registry()`` being called between
    imports (e.g. in tests) — import side-effects only fire the first
    time a module is loaded, but this function needs to work on every
    call.
    """
    for tool, mod_name in KNOWN_TOOL_MODULES.items():
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            # Tool not installed or health module not present — that's fine,
            # the aggregator simply won't run a check for it.
            continue
        except Exception:  # noqa: BLE001
            # A broken health module shouldn't take down the aggregator.
            # We swallow and continue; the missing entry is visible to
            # callers (the tool won't appear in the report).
            continue
        # Re-register on every call so ``clear_registry()`` between runs
        # doesn't leave an empty registry when module top-level code
        # (the register_check call) has already fired.
        check_fn = getattr(mod, "health_check", None)
        if check_fn is not None and tool not in _REGISTRY:
            _REGISTRY[tool] = check_fn


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
    _auto_load: bool = True,
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
        _auto_load: Internal flag — when False, skip the implicit
            ``_load_tool_checks`` pass. Useful for tests that want to
            stub the registry directly without having real tool modules
            register themselves. Production callers always leave this
            at True.

    Returns:
        A ``HealthReport`` with one ``ToolHealth`` per tool that was
        probed (tools whose config section is missing typically return
        ``Status.SKIP`` from their own check; see the per-tool health
        modules for the convention).
    """
    if _auto_load:
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
