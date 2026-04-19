"""Tests for the `alfred check` CLI + the `--preflight` flag on `alfred up`.

These tests invoke the handler functions directly (not the argparse
entry) with a fake ``argparse.Namespace`` — the parser wiring is
simple enough that unit-testing via a subprocess is overkill.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from alfred import cli
from alfred.health import aggregator as agg
from alfred.health.types import CheckResult, HealthReport, Status, ToolHealth


def _write_config(tmp_path: Path, extra: dict | None = None) -> Path:
    """Write a minimal YAML config and return its path."""
    import yaml
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "inbox").mkdir()
    raw = {
        "vault": {"path": str(vault)},
        "agent": {"backend": "zo"},
        "logging": {"dir": str(tmp_path / "data")},
    }
    (tmp_path / "data").mkdir(exist_ok=True)
    if extra:
        raw.update(extra)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return cfg_path


def _make_args(**kwargs) -> argparse.Namespace:  # noqa: ANN003
    """Build an argparse Namespace with sane defaults for ``check``."""
    return argparse.Namespace(
        config=kwargs.pop("config", "config.yaml"),
        full=kwargs.pop("full", False),
        json=kwargs.pop("json", False),
        tools=kwargs.pop("tools", None),
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Isolate tests from the module-level registry state."""
    agg.clear_registry()
    yield
    agg.clear_registry()


def _install_stub(tool: str, status: Status, detail: str = "stub") -> None:
    async def _check(raw, mode):  # noqa: ANN001
        return ToolHealth(
            tool=tool,
            status=status,
            detail=detail,
            results=[CheckResult(name="stub", status=status, detail=detail)],
        )
    agg.register_check(tool, _check)


class TestCmdCheck:
    def test_human_output_exits_zero_on_ok(self, tmp_path: Path, capsys) -> None:
        cfg = _write_config(tmp_path)
        _install_stub("fake", Status.OK, "all good")

        args = _make_args(config=str(cfg))
        # Suppress the actual auto-load in run_all_checks so only our
        # stub registers; we do this by patching the aggregator call.
        with patch(
            "alfred.health.aggregator._load_tool_checks",
            lambda: None,
        ):
            with pytest.raises(SystemExit) as ei:
                cli.cmd_check(args)
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "fake" in out
        assert "[ OK ]" in out

    def test_exit_code_one_on_fail(self, tmp_path: Path, capsys) -> None:
        cfg = _write_config(tmp_path)
        _install_stub("broken", Status.FAIL, "nope")
        args = _make_args(config=str(cfg))
        with patch("alfred.health.aggregator._load_tool_checks", lambda: None):
            with pytest.raises(SystemExit) as ei:
                cli.cmd_check(args)
        assert ei.value.code == 1

    def test_warn_does_not_block_exit_zero(self, tmp_path: Path, capsys) -> None:
        """Per plan Part 11 Q3 — WARN continues; only FAIL aborts."""
        cfg = _write_config(tmp_path)
        _install_stub("warny", Status.WARN, "degraded")
        args = _make_args(config=str(cfg))
        with patch("alfred.health.aggregator._load_tool_checks", lambda: None):
            with pytest.raises(SystemExit) as ei:
                cli.cmd_check(args)
        assert ei.value.code == 0

    def test_json_output_is_valid_json(self, tmp_path: Path, capsys) -> None:
        cfg = _write_config(tmp_path)
        _install_stub("fake", Status.OK)
        args = _make_args(config=str(cfg), json=True)
        with patch("alfred.health.aggregator._load_tool_checks", lambda: None):
            with pytest.raises(SystemExit):
                cli.cmd_check(args)
        out = capsys.readouterr().out
        parsed = json.loads(out)
        assert parsed["overall_status"] == "ok"
        assert len(parsed["tools"]) == 1

    def test_tools_filter_restricts_probed_set(self, tmp_path: Path, capsys) -> None:
        cfg = _write_config(tmp_path)
        _install_stub("alpha", Status.OK)
        _install_stub("bravo", Status.OK)
        args = _make_args(config=str(cfg), tools="alpha")
        with patch("alfred.health.aggregator._load_tool_checks", lambda: None):
            with pytest.raises(SystemExit):
                cli.cmd_check(args)
        out = capsys.readouterr().out
        # Only alpha should appear in the body
        assert "alpha" in out
        assert "[ OK ] bravo" not in out

    def test_full_mode_is_passed_through(self, tmp_path: Path) -> None:
        cfg = _write_config(tmp_path)
        captured = {}

        async def _probe(raw, mode):  # noqa: ANN001
            captured["mode"] = mode
            return ToolHealth(tool="introspect", status=Status.OK)

        agg.register_check("introspect", _probe)
        args = _make_args(config=str(cfg), full=True)
        with patch("alfred.health.aggregator._load_tool_checks", lambda: None):
            with pytest.raises(SystemExit):
                cli.cmd_check(args)
        assert captured["mode"] == "full"


class TestCmdUpPreflight:
    def test_preflight_aborts_on_fail(self, tmp_path: Path, capsys) -> None:
        cfg = _write_config(tmp_path)
        _install_stub("bad", Status.FAIL, "broken")

        args = argparse.Namespace(
            config=str(cfg),
            only=None,
            foreground=False,
            _internal_foreground=False,
            live=False,
            preflight=True,
        )
        # check_already_running has to return None so we get past it
        with patch("alfred.daemon.check_already_running", return_value=None), \
             patch("alfred.health.aggregator._load_tool_checks", lambda: None), \
             patch("alfred.daemon.spawn_daemon") as spawn_mock:
            with pytest.raises(SystemExit) as ei:
                cli.cmd_up(args)
        assert ei.value.code == 1
        # spawn must NOT be called — preflight aborted
        assert spawn_mock.call_count == 0
        out = capsys.readouterr().out
        assert "Preflight FAILED" in out

    def test_preflight_ok_proceeds(self, tmp_path: Path, capsys) -> None:
        cfg = _write_config(tmp_path)
        _install_stub("fine", Status.OK)

        args = argparse.Namespace(
            config=str(cfg),
            only=None,
            foreground=False,
            _internal_foreground=False,
            live=False,
            preflight=True,
        )
        with patch("alfred.daemon.check_already_running", return_value=None), \
             patch("alfred.health.aggregator._load_tool_checks", lambda: None), \
             patch("alfred.daemon.spawn_daemon", return_value=12345) as spawn_mock:
            cli.cmd_up(args)
        # Preflight passed, spawn gets called
        assert spawn_mock.call_count == 1
        out = capsys.readouterr().out
        assert "Preflight passed" in out

    def test_preflight_warn_does_not_abort(self, tmp_path: Path, capsys) -> None:
        """WARN must not block `alfred up --preflight` — plan Part 11 Q3."""
        cfg = _write_config(tmp_path)
        _install_stub("wobbly", Status.WARN, "meh")

        args = argparse.Namespace(
            config=str(cfg),
            only=None,
            foreground=False,
            _internal_foreground=False,
            live=False,
            preflight=True,
        )
        with patch("alfred.daemon.check_already_running", return_value=None), \
             patch("alfred.health.aggregator._load_tool_checks", lambda: None), \
             patch("alfred.daemon.spawn_daemon", return_value=12345) as spawn_mock:
            cli.cmd_up(args)
        assert spawn_mock.call_count == 1

    def test_no_preflight_flag_skips_check(self, tmp_path: Path, capsys) -> None:
        """Backwards compat — `alfred up` without --preflight runs no BIT."""
        cfg = _write_config(tmp_path)
        # Install a FAIL stub — if preflight ran, spawn would NOT be called.
        _install_stub("would-fail", Status.FAIL)

        args = argparse.Namespace(
            config=str(cfg),
            only=None,
            foreground=False,
            _internal_foreground=False,
            live=False,
            preflight=False,
        )
        with patch("alfred.daemon.check_already_running", return_value=None), \
             patch("alfred.health.aggregator._load_tool_checks", lambda: None), \
             patch("alfred.daemon.spawn_daemon", return_value=999) as spawn_mock:
            cli.cmd_up(args)
        assert spawn_mock.call_count == 1
