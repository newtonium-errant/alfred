"""Unit tests for alfred.health.types."""

from __future__ import annotations

import json

import pytest

from alfred.health.types import CheckResult, HealthReport, Status, ToolHealth


class TestStatus:
    def test_values_are_lowercase_strings(self) -> None:
        # str-inherited enum — value should serialize cleanly in JSON.
        assert Status.OK.value == "ok"
        assert Status.WARN.value == "warn"
        assert Status.FAIL.value == "fail"
        assert Status.SKIP.value == "skip"

    def test_status_is_json_serializable(self) -> None:
        # json.dumps on a str-enum works without a custom encoder.
        assert json.dumps(Status.OK) == '"ok"'

    def test_worst_of_empty_list_is_ok(self) -> None:
        assert Status.worst([]) == Status.OK

    def test_worst_prefers_fail_over_warn(self) -> None:
        assert Status.worst([Status.WARN, Status.FAIL, Status.OK]) == Status.FAIL

    def test_worst_prefers_warn_over_skip(self) -> None:
        assert Status.worst([Status.OK, Status.SKIP, Status.WARN]) == Status.WARN

    def test_worst_prefers_skip_over_ok(self) -> None:
        # SKIP ranks above OK so the user sees "didn't check" before
        # seeing a misleading green rollup.
        assert Status.worst([Status.OK, Status.SKIP]) == Status.SKIP

    def test_worst_with_only_ok_returns_ok(self) -> None:
        assert Status.worst([Status.OK, Status.OK, Status.OK]) == Status.OK


class TestCheckResult:
    def test_minimal_construction(self) -> None:
        r = CheckResult(name="foo", status=Status.OK)
        assert r.name == "foo"
        assert r.status == Status.OK
        assert r.detail == ""
        assert r.latency_ms is None
        assert r.data == {}

    def test_all_fields_settable(self) -> None:
        r = CheckResult(
            name="ollama",
            status=Status.WARN,
            detail="slow response",
            latency_ms=1234.5,
            data={"endpoint": "http://localhost:11434"},
        )
        assert r.detail == "slow response"
        assert r.latency_ms == pytest.approx(1234.5)
        assert r.data["endpoint"] == "http://localhost:11434"


class TestToolHealth:
    def test_minimal_construction(self) -> None:
        th = ToolHealth(tool="curator", status=Status.OK)
        assert th.tool == "curator"
        assert th.status == Status.OK
        assert th.results == []
        assert th.detail == ""
        assert th.elapsed_ms is None

    def test_with_results(self) -> None:
        th = ToolHealth(
            tool="janitor",
            status=Status.WARN,
            results=[
                CheckResult(name="vault-path", status=Status.OK),
                CheckResult(name="anthropic-auth", status=Status.WARN, detail="low budget"),
            ],
            detail="degraded",
            elapsed_ms=42.0,
        )
        assert len(th.results) == 2
        assert th.elapsed_ms == pytest.approx(42.0)
        assert th.detail == "degraded"


class TestHealthReport:
    def test_minimal_construction(self) -> None:
        hr = HealthReport(
            mode="quick",
            started_at="2026-04-19T00:00:00+00:00",
            finished_at="2026-04-19T00:00:01+00:00",
            overall_status=Status.OK,
        )
        assert hr.tools == []
        assert hr.mode == "quick"
