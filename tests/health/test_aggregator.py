"""Unit tests for alfred.health.aggregator.

The aggregator fans out to registered per-tool checks concurrently with
timeouts and converts exceptions into ``FAIL`` results.  These tests
stub the registry directly rather than importing real tool modules so
each test can focus on one behavior.
"""

from __future__ import annotations

import asyncio

import pytest

from alfred.health import aggregator as agg
from alfred.health.types import CheckResult, Status, ToolHealth


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Reset the aggregator registry around every test so stubs don't leak."""
    agg.clear_registry()
    yield
    agg.clear_registry()


async def _ok_check(raw, mode):  # noqa: ANN001 — test stub
    return ToolHealth(
        tool="fake",
        status=Status.OK,
        results=[CheckResult(name="ping", status=Status.OK)],
    )


async def _warn_check(raw, mode):  # noqa: ANN001
    return ToolHealth(tool="warny", status=Status.WARN, detail="degraded")


async def _fail_check(raw, mode):  # noqa: ANN001
    return ToolHealth(tool="bad", status=Status.FAIL, detail="nope")


async def _boom_check(raw, mode):  # noqa: ANN001
    raise RuntimeError("exploded")


async def _sleeper_check(raw, mode):  # noqa: ANN001
    await asyncio.sleep(10)
    return ToolHealth(tool="slow", status=Status.OK)


class TestRegistry:
    def test_register_and_clear(self) -> None:
        agg.register_check("fake", _ok_check)
        assert "fake" in agg._REGISTRY
        agg.clear_registry()
        assert agg._REGISTRY == {}


class TestAggregator:
    async def test_empty_registry_returns_ok_with_no_tools(self) -> None:
        report = await agg.run_all_checks({}, mode="quick", _auto_load=False)
        assert report.overall_status == Status.OK
        assert report.tools == []
        assert report.mode == "quick"
        # elapsed_ms must always be populated so callers can show timing
        assert report.elapsed_ms is not None

    async def test_single_ok_check(self) -> None:
        agg.register_check("fake", _ok_check)
        report = await agg.run_all_checks({}, mode="quick", _auto_load=False)
        assert len(report.tools) == 1
        assert report.tools[0].tool == "fake"
        assert report.overall_status == Status.OK
        # Aggregator fills in elapsed_ms when the check doesn't
        assert report.tools[0].elapsed_ms is not None

    async def test_overall_status_is_worst_of_all_tools(self) -> None:
        agg.register_check("a", _ok_check)
        agg.register_check("b", _warn_check)
        agg.register_check("c", _fail_check)
        report = await agg.run_all_checks({}, mode="quick", _auto_load=False)
        assert report.overall_status == Status.FAIL

    async def test_exception_in_check_becomes_fail(self) -> None:
        agg.register_check("boom", _boom_check)
        report = await agg.run_all_checks({}, mode="quick", _auto_load=False)
        assert len(report.tools) == 1
        assert report.tools[0].status == Status.FAIL
        assert "exploded" in report.tools[0].detail
        # Inner CheckResult carries the exception type
        assert any(r.name == "exception" for r in report.tools[0].results)

    async def test_timeout_becomes_fail(self) -> None:
        agg.register_check("slow", _sleeper_check)
        # Force a very small timeout by monkeypatching the constant
        original = agg.QUICK_TIMEOUT_SECONDS
        agg.QUICK_TIMEOUT_SECONDS = 0.05
        try:
            report = await agg.run_all_checks({}, mode="quick", _auto_load=False)
        finally:
            agg.QUICK_TIMEOUT_SECONDS = original
        assert report.tools[0].status == Status.FAIL
        assert "timed out" in report.tools[0].detail.lower()

    async def test_bit_is_excluded_from_targets(self) -> None:
        # Even if a caller explicitly requests "bit", the aggregator
        # filters it out — recursion guard per plan Part 7.
        def _named(tool_name):  # noqa: ANN001 — test helper
            async def _impl(raw, mode):  # noqa: ANN001
                return ToolHealth(tool=tool_name, status=Status.OK)
            return _impl

        agg.register_check("bit", _named("bit"))
        agg.register_check("other", _named("other"))
        report = await agg.run_all_checks({}, mode="quick", tools=["bit", "other"], _auto_load=False)
        assert [t.tool for t in report.tools] == ["other"]

    async def test_unknown_tool_in_filter_is_dropped(self) -> None:
        async def _real(raw, mode):  # noqa: ANN001
            return ToolHealth(tool="real", status=Status.OK)
        agg.register_check("real", _real)
        report = await agg.run_all_checks({}, mode="quick", tools=["real", "ghost"], _auto_load=False)
        assert [t.tool for t in report.tools] == ["real"]

    async def test_full_mode_uses_full_timeout(self) -> None:
        # Not directly observable in the report, but we can check that
        # ``mode`` is propagated through to the HealthReport.
        agg.register_check("fake", _ok_check)
        report = await agg.run_all_checks({}, mode="full", _auto_load=False)
        assert report.mode == "full"

    async def test_check_results_run_concurrently(self) -> None:
        # If checks ran serially, two 0.1s sleepers would take ~0.2s;
        # concurrently it's ~0.1s. Assert we're clearly under 0.3s to
        # allow wiggle room for test-machine jitter.
        async def _slow_ok(raw, mode):  # noqa: ANN001
            await asyncio.sleep(0.1)
            return ToolHealth(tool="one", status=Status.OK)

        async def _slow_ok_two(raw, mode):  # noqa: ANN001
            await asyncio.sleep(0.1)
            return ToolHealth(tool="two", status=Status.OK)

        agg.register_check("one", _slow_ok)
        agg.register_check("two", _slow_ok_two)
        report = await agg.run_all_checks({}, mode="quick", _auto_load=False)
        assert report.elapsed_ms is not None
        assert report.elapsed_ms < 300  # < 0.3s


# --- P16 budget-constant contract pins (2026-06-07) -----------------------
#
# Pin the literal values of the per-tool budget constants so a future
# tweaker has to deliberately update BOTH the constant in
# ``aggregator.py`` AND these pins together. The motivation for the
# 5.0 → 10.0 widen is documented in the aggregator.py block comment
# above ``QUICK_TIMEOUT_SECONDS``; a future tweaker reading the
# constant sees the rationale without commit-message archaeology.
#
# These are NOT timing-based tests (those would be flaky on slow CI);
# they're contract pins on the module-level constants.


def test_quick_timeout_constant_widened_to_10s() -> None:
    """P16 (2026-06-07): QUICK budget widened from 5s to 10s.

    The widen was driven by ``claude-haiku-4-5`` API latency variance
    — curator / janitor / distiller probes routinely run 3–4s on the
    haiku auth path, leaving 1–2s headroom on the old 5s budget that
    broke under a regional Anthropic slowdown 2026-06-07 morning
    (curator probe at 6.2s on the 5s budget). See the block comment
    above ``QUICK_TIMEOUT_SECONDS`` in
    ``src/alfred/health/aggregator.py`` for the latency table and
    incident detail.
    """
    assert agg.QUICK_TIMEOUT_SECONDS == 10.0


def test_full_timeout_constant_unchanged_at_15s() -> None:
    """P16: ``FULL_TIMEOUT_SECONDS`` stays at 15s.

    The haiku-using tools have 10s+ headroom over their baselines on
    the ``full`` budget — no widen needed. Pinning the value here
    surfaces any accidental ``full`` change in the same code review.
    """
    assert agg.FULL_TIMEOUT_SECONDS == 15.0
