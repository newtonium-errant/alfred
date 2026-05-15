"""Tests for ``alfred.cloudflared.daemon.run``.

The daemon is a Pattern-A wrapper around the cloudflared binary. We
test the wrapper's exit-code semantics, command construction, and
SIGTERM forwarding behavior — without ever actually spawning the
real cloudflared binary.

Mocking strategy: replace ``subprocess.Popen`` with a fake that
records the invocation args + lets the test drive ``proc.wait()`` /
``proc.terminate()`` / ``proc.kill()``. The fake exposes the same
interface ``daemon.run`` uses so the wrapper code is exercised as in
production.

Log emission pinning: per
``feedback_log_emission_test_pattern.md``, every production log
event we want the operator to be able to grep gets a
``structlog.testing.capture_logs`` assertion. Drops in observability
go red.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path
from typing import Any

import pytest
import structlog

from alfred.cloudflared import daemon as cf_daemon


# ---------------------------------------------------------------------------
# Fake subprocess.Popen
# ---------------------------------------------------------------------------

class _FakeProc:
    """Stand-in for ``subprocess.Popen`` with controllable wait + signals.

    Default behavior: ``wait(timeout)`` raises ``TimeoutExpired`` until
    ``self.set_exit_code(N)`` is called; once set, ``wait()`` returns N.
    """

    def __init__(self, pid: int = 12345, exit_code: int | None = None) -> None:
        self.pid = pid
        self._exit_code = exit_code
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self._exit_code is None:
            import subprocess as _sp
            raise _sp.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        return self._exit_code

    def terminate(self) -> None:
        self.terminated = True
        # Schedule exit on the next wait — simulates graceful shutdown.
        if self._exit_code is None:
            self._exit_code = 0

    def kill(self) -> None:
        self.killed = True
        if self._exit_code is None:
            self._exit_code = -9

    def set_exit_code(self, code: int) -> None:
        self._exit_code = code


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Install a fake ``subprocess.Popen`` and return a record dict.

    The fake records the ``cmd`` arg + the kwargs (stdout, stderr,
    start_new_session) so tests can assert on command construction.
    """
    record: dict[str, Any] = {"calls": [], "proc": None}

    def _fake(cmd, **kwargs):
        record["calls"].append({"cmd": cmd, "kwargs": kwargs})
        proc = _FakeProc()
        # Auto-exit immediately so wait() doesn't loop in tests that
        # don't care about the timing path.
        proc.set_exit_code(0)
        record["proc"] = proc
        return proc

    monkeypatch.setattr(cf_daemon.subprocess, "Popen", _fake)
    return record


@pytest.fixture
def make_executable(tmp_path: Path):
    """Return a factory that creates an executable shim file.

    The daemon's ``os.access(binary_path, os.X_OK)`` check needs a
    real file with the X bit set; ``tmp_path`` files default to 0o644.
    """

    def _make(name: str = "cloudflared") -> Path:
        binary = tmp_path / name
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        return binary

    return _make


# ---------------------------------------------------------------------------
# Missing-binary / missing-tunnel branches
# ---------------------------------------------------------------------------

def test_run_missing_binary_path_returns_78() -> None:
    """Empty ``binary_path`` → exit 78 (no spawn attempt)."""
    with structlog.testing.capture_logs() as captured:
        ret = cf_daemon.run(binary_path="", tunnel_id="abc")
    assert ret == cf_daemon._MISSING_DEPS_EXIT == 78
    events = [c.get("event") for c in captured]
    assert "cloudflared.binary_missing" in events


def test_run_nonexistent_binary_returns_78(tmp_path: Path) -> None:
    """Binary file does not exist → exit 78, no spawn attempt."""
    fake_path = str(tmp_path / "does-not-exist")
    with structlog.testing.capture_logs() as captured:
        ret = cf_daemon.run(binary_path=fake_path, tunnel_id="abc")
    assert ret == 78
    matches = [c for c in captured if c.get("event") == "cloudflared.binary_missing"]
    assert len(matches) == 1
    assert matches[0]["binary_path"] == fake_path


def test_run_non_executable_binary_returns_78(tmp_path: Path) -> None:
    """Binary exists but isn't ``+x`` → exit 78."""
    binary = tmp_path / "cloudflared"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    # Deliberately do NOT chmod +x — exercises the os.access(X_OK) gate.
    binary.chmod(0o644)
    ret = cf_daemon.run(binary_path=str(binary), tunnel_id="abc")
    assert ret == 78


def test_run_empty_tunnel_id_returns_78(make_executable) -> None:
    """No tunnel_id → exit 78, no spawn."""
    binary = make_executable()
    with structlog.testing.capture_logs() as captured:
        ret = cf_daemon.run(binary_path=str(binary), tunnel_id="")
    assert ret == 78
    events = [c.get("event") for c in captured]
    assert "cloudflared.tunnel_id_missing" in events


# ---------------------------------------------------------------------------
# Happy-path spawn + command construction
# ---------------------------------------------------------------------------

def test_run_spawns_with_tunnel_run_subcommand(
    make_executable, fake_popen,
) -> None:
    """Default invocation: ``<binary> tunnel run <tunnel_id>``."""
    binary = make_executable()
    ret = cf_daemon.run(
        binary_path=str(binary), tunnel_id="5e44e541-b24c-4caa-8246",
    )
    assert ret == 0
    assert len(fake_popen["calls"]) == 1
    cmd = fake_popen["calls"][0]["cmd"]
    assert cmd == [str(binary), "tunnel", "run", "5e44e541-b24c-4caa-8246"]


def test_run_includes_config_flag_when_set(
    make_executable, fake_popen,
) -> None:
    """``config_path`` non-empty → ``--config <path>`` inserted before subcommand."""
    binary = make_executable()
    cf_daemon.run(
        binary_path=str(binary),
        tunnel_id="abc",
        config_path="/etc/cloudflared/config.yml",
    )
    cmd = fake_popen["calls"][0]["cmd"]
    assert cmd == [
        str(binary),
        "--config",
        "/etc/cloudflared/config.yml",
        "tunnel",
        "run",
        "abc",
    ]


def test_run_uses_start_new_session(make_executable, fake_popen) -> None:
    """``start_new_session=True`` → cloudflared gets its own pgrp.

    Prevents Ctrl-C in an attached operator shell from killing
    cloudflared directly. Shutdown must route through our SIGTERM
    handler → ``proc.terminate()``.
    """
    binary = make_executable()
    cf_daemon.run(binary_path=str(binary), tunnel_id="abc")
    kwargs = fake_popen["calls"][0]["kwargs"]
    assert kwargs.get("start_new_session") is True


def test_run_logs_started_event(make_executable, fake_popen) -> None:
    """``cloudflared.started`` log event fires with pid + tunnel_id."""
    binary = make_executable()
    with structlog.testing.capture_logs() as captured:
        cf_daemon.run(
            binary_path=str(binary),
            tunnel_id="tunnel-abc",
            config_path="/etc/cloudflared/config.yml",
        )
    matches = [c for c in captured if c.get("event") == "cloudflared.started"]
    assert len(matches) == 1
    assert matches[0]["tunnel_id"] == "tunnel-abc"
    assert matches[0]["binary_path"] == str(binary)


def test_run_logs_exited_event(make_executable, fake_popen) -> None:
    """``cloudflared.exited`` log event fires with exit_code + shutdown flag."""
    binary = make_executable()
    with structlog.testing.capture_logs() as captured:
        cf_daemon.run(binary_path=str(binary), tunnel_id="abc")
    matches = [c for c in captured if c.get("event") == "cloudflared.exited"]
    assert len(matches) == 1
    assert matches[0]["exit_code"] == 0
    # No shutdown was requested in this test path.
    assert matches[0]["shutdown_requested"] is False


def test_run_propagates_child_exit_code(
    make_executable, fake_popen, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Child exits with N → wrapper returns N (so orchestrator restart-counts).

    We override the fake to exit with 42 instead of 0.
    """

    def _fake_with_42(cmd, **kwargs):
        proc = _FakeProc()
        proc.set_exit_code(42)
        fake_popen["calls"].append({"cmd": cmd, "kwargs": kwargs})
        fake_popen["proc"] = proc
        return proc

    monkeypatch.setattr(cf_daemon.subprocess, "Popen", _fake_with_42)
    binary = make_executable()
    ret = cf_daemon.run(binary_path=str(binary), tunnel_id="abc")
    assert ret == 42


# ---------------------------------------------------------------------------
# Log file handling
# ---------------------------------------------------------------------------

def test_run_creates_log_file_directory(
    make_executable, fake_popen, tmp_path: Path,
) -> None:
    """Missing parent dir for log_path → created on the fly."""
    binary = make_executable()
    nested = tmp_path / "deep" / "nested" / "logs" / "cloudflared.log"
    assert not nested.parent.exists()
    cf_daemon.run(
        binary_path=str(binary), tunnel_id="abc", log_path=str(nested),
    )
    assert nested.parent.exists()


def test_run_passes_log_file_as_child_stdout(
    make_executable, fake_popen, tmp_path: Path,
) -> None:
    """Child's stdout kwarg is a writable file when log_path is set."""
    binary = make_executable()
    log_path = tmp_path / "cf.log"
    cf_daemon.run(
        binary_path=str(binary), tunnel_id="abc", log_path=str(log_path),
    )
    stdout_arg = fake_popen["calls"][0]["kwargs"]["stdout"]
    # Whatever was passed should be a writable file-like obj (not DEVNULL).
    assert stdout_arg is not None
    # _FakeProc doesn't write — but the file should exist now (opened append).
    assert log_path.exists()


def test_run_log_file_open_failure_falls_back_to_devnull(
    make_executable, fake_popen, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If log file can't be opened, fall through to DEVNULL — daemon
    still starts.

    Reason: a transient FS error opening cloudflared.log shouldn't
    kill the tunnel. Better to lose cloudflared's own log output for
    one supervision cycle than fail-loud and break the network.
    """
    import builtins
    real_open = builtins.open

    def _failing_open(path, *args, **kwargs):
        # Fail open() on the cloudflared log path, allow everything else.
        if str(path).endswith("cf.log"):
            raise OSError("simulated disk full")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _failing_open)
    binary = make_executable()
    log_path = tmp_path / "cf.log"

    with structlog.testing.capture_logs() as captured:
        ret = cf_daemon.run(
            binary_path=str(binary), tunnel_id="abc", log_path=str(log_path),
        )

    # Daemon should still start, but the log-open warning should fire.
    assert ret == 0
    events = [c.get("event") for c in captured]
    assert "cloudflared.log_file_open_failed" in events
