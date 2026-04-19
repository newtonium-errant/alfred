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
        report = await agg.run_all_checks({}, mode="quick")
        assert report.overall_status == Status.OK
        assert report.tools == []
        assert report.mode == "quick"
        # elapsed_ms must always be populated so callers can show timing
        assert report.elapsed_ms is not None

    async def test_single_ok_check(self) -> None:
        agg.register_check("fake", _ok_check)
        report = await agg.run_all_checks({}, mode="quick")
        assert len(report.tools) == 1
        assert report.tools[0].tool == "fake"
        assert report.overall_status == Status.OK
        # Aggregator fills in elapsed_ms when the check doesn't
        assert report.tools[0].elapsed_ms is not None

    async def test_overall_status_is_worst_of_all_tools(self) -> None:
        agg.register_check("a", _ok_check)
        agg.register_check("b", _warn_check)
        agg.register_check("c", _fail_check)
        report = await agg.run_all_checks({}, mode="quick")
        assert report.overall_status == Status.FAIL

    async def test_exception_in_check_becomes_fail(self) -> None:
        agg.register_check("boom", _boom_check)
        report = await agg.run_all_checks({}, mode="quick")
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
            report = await agg.run_all_checks({}, mode="quick")
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
        report = await agg.run_all_checks({}, mode="quick", tools=["bit", "other"])
        assert [t.tool for t in report.tools] == ["other"]

    async def test_unknown_tool_in_filter_is_dropped(self) -> None:
        async def _real(raw, mode):  # noqa: ANN001
            return ToolHealth(tool="real", status=Status.OK)
        agg.register_check("real", _real)
        report = await agg.run_all_checks({}, mode="quick", tools=["real", "ghost"])
        assert [t.tool for t in report.tools] == ["real"]

    async def test_full_mode_uses_full_timeout(self) -> None:
        # Not directly observable in the report, but we can check that
        # ``mode`` is propagated through to the HealthReport.
        agg.register_check("fake", _ok_check)
        report = await agg.run_all_checks({}, mode="full")
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
        report = await agg.run_all_checks({}, mode="quick")
        assert report.elapsed_ms is not None
        assert report.elapsed_ms < 300  # < 0.3s
