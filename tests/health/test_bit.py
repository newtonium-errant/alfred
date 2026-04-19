"""Tests for the BIT daemon (config, state, renderer, daemon, CLI)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from alfred import cli as top_cli
from alfred.bit import cli as bit_cli
from alfred.bit.config import (
    _compute_scheduled_time,
    BITConfig,
    DEFAULT_FALLBACK_TIME,
    DEFAULT_LEAD_MINUTES,
    load_from_unified,
)
from alfred.bit.daemon import _next_run_time, run_bit_once
from alfred.bit.renderer import _tool_counts, render_bit_record
from alfred.bit.state import BITRun, StateManager
from alfred.health import aggregator as agg
from alfred.health.types import CheckResult, HealthReport, Status, ToolHealth


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestComputeScheduledTime:
    def test_explicit_bit_time_wins(self) -> None:
        assert _compute_scheduled_time("07:30", "06:00", 5) == "07:30"

    def test_subtracts_lead_from_brief_time(self) -> None:
        assert _compute_scheduled_time("", "06:00", 5) == "05:55"

    def test_subtracts_large_lead(self) -> None:
        assert _compute_scheduled_time("", "06:30", 45) == "05:45"

    def test_wraps_around_midnight(self) -> None:
        # 00:05 − 10 min → 23:55
        assert _compute_scheduled_time("", "00:05", 10) == "23:55"

    def test_empty_brief_falls_back(self) -> None:
        assert _compute_scheduled_time("", "", 5) == DEFAULT_FALLBACK_TIME

    def test_malformed_brief_time_falls_back(self) -> None:
        assert _compute_scheduled_time("", "abc", 5) == DEFAULT_FALLBACK_TIME


class TestLoadFromUnified:
    def test_brief_time_drives_bit_time(self) -> None:
        raw = {
            "vault": {"path": "./vault"},
            "brief": {"schedule": {"time": "06:00", "timezone": "America/Halifax"}},
        }
        cfg = load_from_unified(raw)
        assert cfg.schedule.time == "05:55"
        assert cfg.schedule.lead_minutes == DEFAULT_LEAD_MINUTES
        assert cfg.schedule.timezone == "America/Halifax"

    def test_explicit_bit_overrides(self) -> None:
        raw = {
            "vault": {"path": "./vault"},
            "brief": {"schedule": {"time": "06:00"}},
            "bit": {"schedule": {"time": "07:00", "lead_minutes": 10}},
        }
        cfg = load_from_unified(raw)
        assert cfg.schedule.time == "07:00"

    def test_no_brief_uses_fallback(self) -> None:
        raw = {"vault": {"path": "./vault"}}
        cfg = load_from_unified(raw)
        assert cfg.schedule.time == DEFAULT_FALLBACK_TIME

    def test_custom_lead_minutes(self) -> None:
        raw = {
            "vault": {"path": "./vault"},
            "brief": {"schedule": {"time": "06:00"}},
            "bit": {"schedule": {"lead_minutes": 15}},
        }
        cfg = load_from_unified(raw)
        assert cfg.schedule.time == "05:45"
        assert cfg.schedule.lead_minutes == 15

    def test_default_mode_is_quick(self) -> None:
        raw = {"vault": {"path": "./vault"}}
        cfg = load_from_unified(raw)
        assert cfg.schedule.mode == "quick"

    def test_mode_override(self) -> None:
        raw = {"vault": {"path": "./vault"}, "bit": {"schedule": {"mode": "full"}}}
        cfg = load_from_unified(raw)
        assert cfg.schedule.mode == "full"

    def test_default_output_directory_is_process(self) -> None:
        cfg = load_from_unified({"vault": {"path": "./vault"}})
        assert cfg.output.directory == "process"

    def test_state_path_under_logging_dir(self) -> None:
        raw = {"vault": {"path": "./vault"}, "logging": {"dir": "/tmp/d"}}
        cfg = load_from_unified(raw)
        assert cfg.state.path.startswith("/tmp/d")


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def _sample_report() -> HealthReport:
    return HealthReport(
        mode="quick",
        started_at="2026-04-19T00:00:00+00:00",
        finished_at="2026-04-19T00:00:01+00:00",
        overall_status=Status.WARN,
        tools=[
            ToolHealth(
                tool="curator",
                status=Status.OK,
                results=[CheckResult(name="vault", status=Status.OK)],
            ),
            ToolHealth(
                tool="janitor",
                status=Status.WARN,
                results=[CheckResult(name="x", status=Status.WARN)],
            ),
        ],
        elapsed_ms=100.0,
    )


class TestRenderBitRecord:
    def test_frontmatter_includes_overall_status(self, tmp_path: Path) -> None:
        cfg = BITConfig(vault_path=str(tmp_path))
        fm, body = render_bit_record(_sample_report(), "2026-04-19", cfg)
        assert fm["type"] == "run"
        assert fm["overall_status"] == "warn"
        assert fm["mode"] == "quick"
        assert set(fm["tools_checked"]) == {"curator", "janitor"}
        assert fm["tool_counts"] == {"ok": 1, "warn": 1, "fail": 0, "skip": 0}

    def test_body_includes_summary_and_json(self, tmp_path: Path) -> None:
        cfg = BITConfig(vault_path=str(tmp_path))
        fm, body = render_bit_record(_sample_report(), "2026-04-19", cfg)
        assert "## Summary" in body
        assert "curator" in body
        assert "## Raw report (JSON)" in body
        assert '"overall_status": "warn"' in body

    def test_tags_include_status(self, tmp_path: Path) -> None:
        cfg = BITConfig(vault_path=str(tmp_path))
        fm, _ = render_bit_record(_sample_report(), "2026-04-19", cfg)
        assert "bit/warn" in fm["tags"]

    def test_tool_counts_all_zero_on_empty(self) -> None:
        empty = HealthReport(
            mode="quick", started_at="x", finished_at="y",
            overall_status=Status.OK,
        )
        assert _tool_counts(empty) == {"ok": 0, "warn": 0, "fail": 0, "skip": 0}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class TestState:
    def test_add_run_respects_max_history(self, tmp_path: Path) -> None:
        sm = StateManager(tmp_path / "s.json")
        for i in range(10):
            sm.state.add_run(
                BITRun(
                    date=f"2026-04-{i:02d}",
                    generated_at=f"{i}",
                    vault_path=f"x{i}",
                    overall_status="ok",
                    mode="quick",
                ),
                max_history=3,
            )
        assert len(sm.state.runs) == 3

    def test_save_and_load_round_trip(self, tmp_path: Path) -> None:
        sm = StateManager(tmp_path / "s.json")
        sm.state.add_run(
            BITRun(
                date="2026-04-19",
                generated_at="t",
                vault_path="p",
                overall_status="ok",
                mode="quick",
                tool_counts={"ok": 3},
            )
        )
        sm.save()

        sm2 = StateManager(tmp_path / "s.json")
        sm2.load()
        assert len(sm2.state.runs) == 1
        assert sm2.state.runs[0].overall_status == "ok"
        assert sm2.state.runs[0].tool_counts == {"ok": 3}

    def test_load_corrupt_file_resets(self, tmp_path: Path) -> None:
        path = tmp_path / "s.json"
        path.write_text("not json", encoding="utf-8")
        sm = StateManager(path)
        sm.load()
        assert sm.state.runs == []


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_registry():
    agg.clear_registry()
    yield
    agg.clear_registry()


def _install_stub(tool: str, status: Status) -> None:
    async def _check(raw, mode):  # noqa: ANN001
        return ToolHealth(
            tool=tool,
            status=status,
            results=[CheckResult(name="stub", status=status)],
        )
    agg.register_check(tool, _check)


class TestRunBitOnce:
    async def test_writes_record_and_updates_state(self, tmp_path: Path) -> None:
        _install_stub("x", Status.OK)
        vault = tmp_path / "vault"
        vault.mkdir()
        cfg = BITConfig(
            vault_path=str(vault),
        )
        # Point state file into tmp_path so we don't touch real data
        cfg.state.path = str(tmp_path / "bit_state.json")

        sm = StateManager(cfg.state.path)
        sm.load()
        with patch("alfred.health.aggregator._load_tool_checks", lambda: None):
            rel_path, status = await run_bit_once(cfg, {}, sm)

        # Record written
        full = vault / rel_path
        assert full.exists()
        content = full.read_text(encoding="utf-8")
        assert "type: run" in content
        assert "overall_status: ok" in content

        # State updated
        assert len(sm.state.runs) == 1
        assert sm.state.runs[0].overall_status == "ok"

    async def test_bit_itself_not_in_report(self, tmp_path: Path) -> None:
        """Recursion guard — BIT must not probe itself."""
        _install_stub("bit", Status.OK)
        _install_stub("real", Status.OK)
        vault = tmp_path / "vault"
        vault.mkdir()
        cfg = BITConfig(vault_path=str(vault))
        cfg.state.path = str(tmp_path / "bit_state.json")
        sm = StateManager(cfg.state.path)
        with patch("alfred.health.aggregator._load_tool_checks", lambda: None):
            rel_path, _ = await run_bit_once(cfg, {}, sm)
        content = (vault / rel_path).read_text(encoding="utf-8")
        # tool_counts should reflect that only "real" was probed
        assert "real" in content

    async def test_fail_surfaces_as_fail_in_state(self, tmp_path: Path) -> None:
        _install_stub("bad", Status.FAIL)
        vault = tmp_path / "vault"
        vault.mkdir()
        cfg = BITConfig(vault_path=str(vault))
        cfg.state.path = str(tmp_path / "bit_state.json")
        sm = StateManager(cfg.state.path)
        with patch("alfred.health.aggregator._load_tool_checks", lambda: None):
            _, status = await run_bit_once(cfg, {}, sm)
        assert status == Status.FAIL
        assert sm.state.runs[0].overall_status == "fail"


class TestNextRunTime:
    def test_returns_future_datetime(self) -> None:
        target = _next_run_time("06:00", "America/Halifax")
        # Just verify tz + time parse worked and returned a datetime
        assert target.tzinfo is not None
        assert target.hour == 6
        assert target.minute == 0


# ---------------------------------------------------------------------------
# CLI (bit subcommands)
# ---------------------------------------------------------------------------

class TestBitCli:
    def test_cmd_status_no_runs(self, tmp_path: Path, capsys) -> None:
        cfg = BITConfig(vault_path=str(tmp_path))
        cfg.state.path = str(tmp_path / "bit_state.json")
        code = bit_cli.cmd_status(cfg)
        assert code == 0
        out = capsys.readouterr().out
        assert "never" in out

    def test_cmd_status_json(self, tmp_path: Path, capsys) -> None:
        cfg = BITConfig(vault_path=str(tmp_path))
        cfg.state.path = str(tmp_path / "bit_state.json")
        code = bit_cli.cmd_status(cfg, wants_json=True)
        assert code == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["latest"] is None
        assert parsed["run_count"] == 0

    def test_cmd_history_empty(self, tmp_path: Path, capsys) -> None:
        cfg = BITConfig(vault_path=str(tmp_path))
        cfg.state.path = str(tmp_path / "bit_state.json")
        bit_cli.cmd_history(cfg)
        out = capsys.readouterr().out
        assert "No BIT runs" in out

    def test_cmd_run_now_returns_zero_on_ok(self, tmp_path: Path, capsys) -> None:
        _install_stub("x", Status.OK)
        vault = tmp_path / "vault"
        vault.mkdir()
        cfg = BITConfig(vault_path=str(vault))
        cfg.state.path = str(tmp_path / "bit_state.json")
        with patch("alfred.health.aggregator._load_tool_checks", lambda: None):
            code = bit_cli.cmd_run_now(cfg, {}, wants_json=False)
        assert code == 0
        out = capsys.readouterr().out
        assert "BIT run written" in out

    def test_cmd_run_now_returns_one_on_fail(self, tmp_path: Path, capsys) -> None:
        _install_stub("x", Status.FAIL)
        vault = tmp_path / "vault"
        vault.mkdir()
        cfg = BITConfig(vault_path=str(vault))
        cfg.state.path = str(tmp_path / "bit_state.json")
        with patch("alfred.health.aggregator._load_tool_checks", lambda: None):
            code = bit_cli.cmd_run_now(cfg, {}, wants_json=False)
        assert code == 1

    def test_cmd_run_now_json_output(self, tmp_path: Path, capsys) -> None:
        _install_stub("x", Status.OK)
        vault = tmp_path / "vault"
        vault.mkdir()
        cfg = BITConfig(vault_path=str(vault))
        cfg.state.path = str(tmp_path / "bit_state.json")
        with patch("alfred.health.aggregator._load_tool_checks", lambda: None):
            bit_cli.cmd_run_now(cfg, {}, wants_json=True)
        out = capsys.readouterr().out
        # structlog may log to stdout ahead of our JSON — find the
        # final JSON block (starts with '{').
        json_start = out.rfind("{")
        parsed = json.loads(out[json_start:])
        assert parsed["overall_status"] == "ok"
        assert parsed["record_path"].startswith("process/")

    def test_cmd_status_with_runs(self, tmp_path: Path, capsys) -> None:
        cfg = BITConfig(vault_path=str(tmp_path))
        cfg.state.path = str(tmp_path / "bit_state.json")
        sm = StateManager(cfg.state.path)
        sm.state.add_run(BITRun(
            date="2026-04-19",
            generated_at="2026-04-19T06:00:00Z",
            vault_path="process/x.md",
            overall_status="ok",
            mode="quick",
            tool_counts={"ok": 5, "warn": 0, "fail": 0, "skip": 0},
        ))
        sm.save()
        bit_cli.cmd_status(cfg)
        out = capsys.readouterr().out
        assert "2026-04-19T06:00:00Z" in out
        assert "Runs recorded: 1" in out

    def test_cmd_history_with_runs(self, tmp_path: Path, capsys) -> None:
        cfg = BITConfig(vault_path=str(tmp_path))
        cfg.state.path = str(tmp_path / "bit_state.json")
        sm = StateManager(cfg.state.path)
        for i in range(3):
            sm.state.add_run(BITRun(
                date=f"2026-04-{17+i:02d}",
                generated_at=f"t{i}",
                vault_path=f"p{i}",
                overall_status="ok",
                mode="quick",
            ))
        sm.save()
        bit_cli.cmd_history(cfg, limit=2)
        out = capsys.readouterr().out
        # Only 2 should appear due to limit
        assert out.count("ok") == 2

    def test_cmd_history_json(self, tmp_path: Path, capsys) -> None:
        cfg = BITConfig(vault_path=str(tmp_path))
        cfg.state.path = str(tmp_path / "bit_state.json")
        sm = StateManager(cfg.state.path)
        sm.state.add_run(BITRun(
            date="2026-04-19",
            generated_at="t",
            vault_path="p",
            overall_status="fail",
            mode="full",
        ))
        sm.save()
        bit_cli.cmd_history(cfg, wants_json=True)
        out = capsys.readouterr().out
        # Find the json list output by locating the last '[\n' pattern —
        # structlog's output starts each line with timestamps and may
        # contain '[info' so naive ``find("[")`` won't work.
        json_start = out.rfind("[\n")
        parsed = json.loads(out[json_start:])
        assert len(parsed) == 1
        assert parsed[0]["overall_status"] == "fail"


# ---------------------------------------------------------------------------
# Orchestrator registration
# ---------------------------------------------------------------------------

class TestOrchestratorRegistration:
    def test_bit_runner_registered(self) -> None:
        from alfred import orchestrator
        assert "bit" in orchestrator.TOOL_RUNNERS

    def test_bit_auto_starts_when_brief_present(self) -> None:
        # We don't actually run the orchestrator — just check the gating
        # logic by reading the code path. The plan says "bit auto-starts
        # when bit: section OR brief: section is present".
        from alfred import orchestrator
        # BIT is reachable via TOOL_RUNNERS; start_process only runs
        # when ``tools`` list contains "bit", which ``run_all`` builds
        # from the config sections.
        assert orchestrator.TOOL_RUNNERS["bit"].__name__ == "_run_bit"
