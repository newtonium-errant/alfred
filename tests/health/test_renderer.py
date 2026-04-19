"""Unit tests for alfred.health.renderer."""

from __future__ import annotations

import json

from alfred.health.renderer import render_human, render_json
from alfred.health.types import CheckResult, HealthReport, Status, ToolHealth


def _sample_report() -> HealthReport:
    return HealthReport(
        mode="quick",
        started_at="2026-04-19T00:00:00+00:00",
        finished_at="2026-04-19T00:00:01+00:00",
        overall_status=Status.WARN,
        elapsed_ms=1234.5,
        tools=[
            ToolHealth(
                tool="curator",
                status=Status.OK,
                elapsed_ms=42.0,
                results=[
                    CheckResult(name="vault", status=Status.OK),
                    CheckResult(
                        name="anthropic-auth",
                        status=Status.OK,
                        latency_ms=120.5,
                        detail="count_tokens ok",
                    ),
                ],
            ),
            ToolHealth(
                tool="janitor",
                status=Status.WARN,
                elapsed_ms=55.0,
                detail="stub enrichment backing off",
                results=[
                    CheckResult(name="vault", status=Status.OK),
                    CheckResult(name="backoff", status=Status.WARN, detail="3 retries"),
                ],
            ),
            ToolHealth(
                tool="surveyor",
                status=Status.SKIP,
                detail="no surveyor section in config",
            ),
        ],
    )


class TestRenderHuman:
    def test_iterator_yields_tool_lines(self) -> None:
        lines = list(render_human(_sample_report()))
        body = "\n".join(lines)
        # Header includes mode and overall status
        assert "BIT (quick)" in body
        assert "[WARN]" in body
        # Every tool appears
        for name in ("curator", "janitor", "surveyor"):
            assert name in body

    def test_tool_status_glyphs_appear(self) -> None:
        lines = list(render_human(_sample_report()))
        body = "\n".join(lines)
        assert "[ OK ] curator" in body
        assert "[WARN] janitor" in body
        assert "[SKIP] surveyor" in body

    def test_check_results_indented(self) -> None:
        lines = list(render_human(_sample_report()))
        # Individual CheckResult lines start with four spaces
        assert any(line.startswith("    [ OK ] vault") for line in lines)
        assert any(line.startswith("    [WARN] backoff") for line in lines)

    def test_latency_included_when_present(self) -> None:
        lines = list(render_human(_sample_report()))
        body = "\n".join(lines)
        assert "120" in body  # 120.5ms shows up in output

    def test_totals_line_at_bottom(self) -> None:
        lines = list(render_human(_sample_report()))
        totals_line = [line for line in lines if line.startswith("Totals:")]
        assert len(totals_line) == 1
        # One ok (curator), one warn (janitor), one skip (surveyor), zero fail
        assert "ok=1" in totals_line[0]
        assert "warn=1" in totals_line[0]
        assert "skip=1" in totals_line[0]
        assert "fail=0" in totals_line[0]

    def test_empty_report_lists_none_tools(self) -> None:
        report = HealthReport(
            mode="quick",
            started_at="x",
            finished_at="y",
            overall_status=Status.OK,
        )
        lines = list(render_human(report))
        body = "\n".join(lines)
        assert "no tools checked" in body

    def test_write_callable_receives_lines(self) -> None:
        buffer: list[str] = []
        result = render_human(_sample_report(), write=buffer.append)
        assert result is None  # writer mode returns None
        assert len(buffer) > 0
        assert any("curator" in line for line in buffer)


class TestRenderJson:
    def test_returns_valid_json(self) -> None:
        report = _sample_report()
        output = render_json(report)
        parsed = json.loads(output)
        assert parsed["mode"] == "quick"
        assert parsed["overall_status"] == "warn"
        assert len(parsed["tools"]) == 3

    def test_status_enum_serialized_as_value(self) -> None:
        report = _sample_report()
        parsed = json.loads(render_json(report))
        assert parsed["tools"][0]["status"] == "ok"
        assert parsed["tools"][1]["status"] == "warn"
        assert parsed["tools"][2]["status"] == "skip"

    def test_nested_check_results_preserved(self) -> None:
        report = _sample_report()
        parsed = json.loads(render_json(report))
        curator = parsed["tools"][0]
        assert len(curator["results"]) == 2
        assert curator["results"][0]["name"] == "vault"
        assert curator["results"][0]["status"] == "ok"

    def test_empty_report_json(self) -> None:
        report = HealthReport(
            mode="quick",
            started_at="x",
            finished_at="y",
            overall_status=Status.OK,
        )
        parsed = json.loads(render_json(report))
        assert parsed["tools"] == []
        assert parsed["overall_status"] == "ok"
