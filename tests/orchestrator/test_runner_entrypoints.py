"""Coverage for the per-tool ``_run_*`` process entry points.

These functions are the orchestrator's multiprocess targets. In
production they run in a fresh child process and block on ``asyncio.run``
forever. For coverage we call them IN-PROCESS with the heavy imports
monkeypatched to lightweight stubs. This exercises:

- Log file path derivation from ``raw["logging"]["dir"]``
- ``_silence_stdio`` opt-in via ``suppress_stdout`` flag (skipped here —
  would steal pytest's captured stdout)
- Lazy-import order (if any config/utils import drifts, the signature
  assertions blow up)
- Surveyor's missing-deps → ``sys.exit(78)`` branch

We do NOT actually run the daemons. Each stub ``run()`` / ``run_watch()``
/ ``Daemon.run()`` returns immediately.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import alfred.orchestrator as orchestrator


@dataclass
class _StubStateConfig:
    path: str = ""
    max_sweep_history: int = 20
    max_run_history: int = 20


@dataclass
class _StubIdleTickConfig:
    """Stand-in for each tool's :class:`IdleTickConfig`.

    The orchestrator's ``_run_mail_webhook`` reads
    ``config.idle_tick.{enabled,interval_seconds}`` to forward to
    ``run_webhook``. Other tools also have an ``idle_tick`` field but
    don't read it from the orchestrator side — only mail does, because
    mail's webhook server runs in a sync HTTPServer and needs the
    config injected at start.
    """

    enabled: bool = True
    interval_seconds: int = 60


@dataclass
class _StubConfig:
    """Minimal stand-in for every tool's Config dataclass.

    The per-tool orchestrator runners only touch ``.state.path`` and
    ``.state.max_sweep_history`` / ``.max_run_history``. Nothing else
    is accessed before the stub ``run()`` returns.
    """

    state: _StubStateConfig = field(default_factory=_StubStateConfig)
    vault: Any = None
    inbox_dir: str = "inbox"
    idle_tick: _StubIdleTickConfig = field(default_factory=_StubIdleTickConfig)


# ---------------------------------------------------------------------------
# Helper — install lightweight stubs for a tool's lazy-imported modules
# ---------------------------------------------------------------------------

def _install_stub_module(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    attrs: dict[str, Any],
) -> types.ModuleType:
    """Create a dummy module at ``sys.modules[name]`` with ``attrs`` set.

    Covers the lazy-import pattern ``from alfred.curator.config import
    load_from_unified`` — as long as the resolved module has the named
    attribute, the import succeeds without triggering the real module
    code.
    """
    mod = types.ModuleType(name)
    for attr, value in attrs.items():
        setattr(mod, attr, value)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def _install_stub_tool_modules(
    monkeypatch: pytest.MonkeyPatch, tool: str, state_path: str,
) -> dict[str, Any]:
    """Install stubs for a tool's config/utils/state/daemon modules.

    Returns a dict with captured invocation records for post-test assertions.
    """
    captured: dict[str, Any] = {
        "setup_logging_called": False,
        "run_args": None,
    }

    def _load_from_unified(raw: dict[str, Any]) -> _StubConfig:
        return _StubConfig(state=_StubStateConfig(path=state_path))

    def _setup_logging(level: str = "INFO", log_file: str = "", suppress_stdout: bool = False) -> None:
        captured["setup_logging_called"] = True
        captured["setup_logging_args"] = {
            "level": level,
            "log_file": log_file,
            "suppress_stdout": suppress_stdout,
        }

    async def _daemon_run(*args, **kwargs) -> None:
        captured["run_args"] = (args, kwargs)

    _install_stub_module(
        monkeypatch, f"alfred.{tool}.config",
        {"load_from_unified": _load_from_unified},
    )
    _install_stub_module(
        monkeypatch, f"alfred.{tool}.utils",
        {"setup_logging": _setup_logging},
    )

    # State stub — janitor/distiller also import State classes.
    class _StubStateCls:
        def __init__(self, *args, **kwargs) -> None:
            captured.setdefault("state_ctor_args", []).append((args, kwargs))

        def load(self) -> None:
            captured["state_loaded"] = True

    state_mod_attrs: dict[str, Any] = {}
    if tool == "janitor":
        state_mod_attrs["JanitorState"] = _StubStateCls
    elif tool == "distiller":
        state_mod_attrs["DistillerState"] = _StubStateCls
    if state_mod_attrs:
        _install_stub_module(
            monkeypatch, f"alfred.{tool}.state", state_mod_attrs,
        )

    # Daemon stub — curator has ``run``, janitor/distiller have ``run_watch``.
    daemon_attrs: dict[str, Any] = {"run": _daemon_run, "run_watch": _daemon_run}
    _install_stub_module(
        monkeypatch, f"alfred.{tool}.daemon", daemon_attrs,
    )

    return captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_run_curator_invokes_config_and_daemon(
    orch_dirs, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_run_curator`` loads config, sets up logging, runs the daemon."""
    captured = _install_stub_tool_modules(
        monkeypatch, "curator",
        state_path=str(orch_dirs["data"] / "curator_state.json"),
    )

    raw = {"logging": {"level": "INFO", "dir": str(orch_dirs["data"])}}
    orchestrator._run_curator(raw, str(orch_dirs["skills"]), suppress_stdout=False)

    assert captured["setup_logging_called"]
    assert captured["setup_logging_args"]["log_file"].endswith("/curator.log")
    assert captured["run_args"] is not None


def test_run_janitor_loads_state_and_invokes_run_watch(
    orch_dirs, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_run_janitor`` builds JanitorState, loads it, runs the watch loop."""
    captured = _install_stub_tool_modules(
        monkeypatch, "janitor",
        state_path=str(orch_dirs["data"] / "janitor_state.json"),
    )

    raw = {"logging": {"level": "DEBUG", "dir": str(orch_dirs["data"])}}
    orchestrator._run_janitor(raw, str(orch_dirs["skills"]), suppress_stdout=False)

    assert captured["setup_logging_called"]
    assert captured["setup_logging_args"]["log_file"].endswith("/janitor.log")
    assert captured.get("state_loaded") is True
    assert captured["run_args"] is not None


def test_run_distiller_loads_state_and_invokes_run_watch(
    orch_dirs, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_run_distiller`` builds DistillerState, loads it, runs the watch loop."""
    captured = _install_stub_tool_modules(
        monkeypatch, "distiller",
        state_path=str(orch_dirs["data"] / "distiller_state.json"),
    )

    raw = {"logging": {"level": "INFO", "dir": str(orch_dirs["data"])}}
    orchestrator._run_distiller(raw, str(orch_dirs["skills"]), suppress_stdout=False)

    assert captured["setup_logging_called"]
    assert captured["setup_logging_args"]["log_file"].endswith("/distiller.log")
    assert captured.get("state_loaded") is True
    assert captured["run_args"] is not None


def test_run_surveyor_handles_missing_deps(
    orch_dirs, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Surveyor without ML extras → ``sys.exit(_MISSING_DEPS_EXIT)``.

    We simulate the ImportError by installing a stub module that deliberately
    lacks the attributes the runner tries to import. ``from X import Y``
    raises ImportError when Y isn't on X, which the runner catches and
    exits 78.
    """
    # Install stub surveyor.config/utils, but DROP Daemon from surveyor.daemon
    # so the ``from alfred.surveyor.daemon import Daemon`` fails.
    _install_stub_module(
        monkeypatch, "alfred.surveyor.config",
        {"load_from_unified": lambda raw: _StubConfig()},
    )
    _install_stub_module(
        monkeypatch, "alfred.surveyor.utils",
        {"setup_logging": lambda **_: None},
    )
    # alfred.surveyor.daemon exists but has no ``Daemon`` attribute.
    _install_stub_module(monkeypatch, "alfred.surveyor.daemon", {})

    raw = {"logging": {"level": "INFO", "dir": str(orch_dirs["data"])}}

    with pytest.raises(SystemExit) as exc_info:
        orchestrator._run_surveyor(raw, suppress_stdout=False)

    assert exc_info.value.code == orchestrator._MISSING_DEPS_EXIT == 78


def test_run_surveyor_happy_path(
    orch_dirs, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All surveyor imports resolve → Daemon.run() is invoked."""
    captured = {"run_called": False}

    class _StubDaemon:
        def __init__(self, config): self.config = config
        async def run(self) -> None:
            captured["run_called"] = True

    _install_stub_module(
        monkeypatch, "alfred.surveyor.config",
        {"load_from_unified": lambda raw: _StubConfig()},
    )
    _install_stub_module(
        monkeypatch, "alfred.surveyor.utils",
        {"setup_logging": lambda **_: None},
    )
    _install_stub_module(
        monkeypatch, "alfred.surveyor.daemon",
        {"Daemon": _StubDaemon},
    )

    raw = {"logging": {"level": "INFO", "dir": str(orch_dirs["data"])}}
    orchestrator._run_surveyor(raw, suppress_stdout=False)

    assert captured["run_called"]


def test_run_brief_invokes_run_daemon(
    orch_dirs, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_run_brief`` loads config, sets up logging, calls run_daemon."""
    captured = {"run_called": False}

    async def _run_daemon(config) -> None:
        captured["run_called"] = True

    _install_stub_module(
        monkeypatch, "alfred.brief.config",
        {"load_from_unified": lambda raw: _StubConfig()},
    )
    _install_stub_module(
        monkeypatch, "alfred.brief.utils",
        {"setup_logging": lambda **_: None},
    )
    _install_stub_module(
        monkeypatch, "alfred.brief.daemon",
        {"run_daemon": _run_daemon},
    )

    raw = {"logging": {"level": "INFO", "dir": str(orch_dirs["data"])}}
    orchestrator._run_brief(raw, suppress_stdout=False)

    assert captured["run_called"]


def test_run_mail_webhook_invokes_run_webhook(
    orch_dirs, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_run_mail_webhook`` builds inbox_path + token and calls run_webhook."""
    captured: dict[str, Any] = {"call_args": None, "idle_tick": None}

    def _run_webhook(
        inbox_path,
        token: str = "",
        idle_tick_enabled: bool = True,
        idle_tick_interval_seconds: int = 60,
    ) -> None:
        captured["call_args"] = (inbox_path, token)
        captured["idle_tick"] = (idle_tick_enabled, idle_tick_interval_seconds)

    _install_stub_module(
        monkeypatch, "alfred.mail.config",
        {"load_from_unified": lambda raw: _StubConfig(inbox_dir="custom_inbox")},
    )
    _install_stub_module(
        monkeypatch, "alfred.mail.webhook",
        {"run_webhook": _run_webhook},
    )

    monkeypatch.setenv("MAIL_WEBHOOK_TOKEN", "secret-token-123")

    raw = {
        "logging": {"level": "INFO", "dir": str(orch_dirs["data"])},
        "vault": {"path": str(orch_dirs["data"].parent / "myvault")},
    }
    orchestrator._run_mail_webhook(raw, suppress_stdout=False)

    inbox_path, token = captured["call_args"]
    # inbox_path = vault_path / config.inbox_dir
    assert str(inbox_path).endswith("/myvault/custom_inbox")
    assert token == "secret-token-123"
    # The idle_tick config must be forwarded to run_webhook so the
    # heartbeat thread spawns inside the webhook server process.
    assert captured["idle_tick"] == (True, 60)


def test_run_talker_invokes_telegram_daemon(
    orch_dirs, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_run_talker`` calls alfred.telegram.daemon.run, propagates exit code."""
    captured: dict[str, Any] = {"run_args": None}

    async def _talker_run(raw, skills_dir_str, suppress_stdout) -> int:
        captured["run_args"] = (raw, skills_dir_str, suppress_stdout)
        return 0  # clean exit

    _install_stub_module(
        monkeypatch, "alfred.telegram.daemon",
        {"run": _talker_run},
    )

    raw = {"logging": {"level": "INFO", "dir": str(orch_dirs["data"])}}
    # clean exit → no SystemExit raised
    orchestrator._run_talker(raw, str(orch_dirs["skills"]), suppress_stdout=False)
    assert captured["run_args"] is not None


def test_run_talker_propagates_nonzero_exit_code(
    orch_dirs, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-zero return from telegram daemon → ``sys.exit(code)``.

    Guards the ``if exit_code: sys.exit(exit_code)`` branch — this is how
    the talker signals missing-config to the orchestrator's restart loop.
    """
    async def _talker_run(*args, **kwargs) -> int:
        return 42  # arbitrary non-zero exit

    _install_stub_module(
        monkeypatch, "alfred.telegram.daemon",
        {"run": _talker_run},
    )

    raw = {"logging": {"level": "INFO", "dir": str(orch_dirs["data"])}}
    with pytest.raises(SystemExit) as exc_info:
        orchestrator._run_talker(raw, str(orch_dirs["skills"]), suppress_stdout=False)
    assert exc_info.value.code == 42


# ---------------------------------------------------------------------------
# PID helper coverage — stale-tool cleanup
# ---------------------------------------------------------------------------

def test_tool_pid_path(orch_dirs) -> None:
    """``_tool_pid_path`` returns ``<data_dir>/<tool>.pid``."""
    path = orchestrator._tool_pid_path(orch_dirs["data"], "curator")
    assert path == orch_dirs["data"] / "curator.pid"


def test_record_and_cleanup_tool_pid(orch_dirs) -> None:
    """PID round-trip — record, read, cleanup."""
    pid_path = orchestrator._tool_pid_path(orch_dirs["data"], "janitor")
    orchestrator._record_tool_pid(orch_dirs["data"], "janitor", 12345)
    assert pid_path.exists()
    assert pid_path.read_text(encoding="utf-8").strip() == "12345"

    orchestrator._cleanup_tool_pid(orch_dirs["data"], "janitor")
    assert not pid_path.exists()


def test_kill_stale_tool_noop_when_no_pid_file(orch_dirs) -> None:
    """No PID file → _kill_stale_tool returns cleanly."""
    # No file exists — should not raise.
    orchestrator._kill_stale_tool(orch_dirs["data"], "curator")


def test_kill_stale_tool_removes_self_pointing_pid_file(orch_dirs) -> None:
    """If a stale PID file points at our own PID, remove it and return."""
    import os
    pid_path = orchestrator._tool_pid_path(orch_dirs["data"], "curator")
    orchestrator._record_tool_pid(orch_dirs["data"], "curator", os.getpid())

    orchestrator._kill_stale_tool(orch_dirs["data"], "curator")

    # File should have been removed (the "points at us" self-heal path).
    assert not pid_path.exists()


def test_kill_stale_tool_removes_dead_pid_file(orch_dirs) -> None:
    """If the PID refers to a dead process, clean up the stale file."""
    # PID 99999999 almost certainly doesn't exist — ``is_running`` will
    # return False, and _kill_stale_tool should remove the stale file.
    pid_path = orchestrator._tool_pid_path(orch_dirs["data"], "curator")
    orchestrator._record_tool_pid(orch_dirs["data"], "curator", 99999999)

    orchestrator._kill_stale_tool(orch_dirs["data"], "curator")

    assert not pid_path.exists()


def test_kill_stale_tool_sigterm_live_process(orch_dirs) -> None:
    """Live stale process → SIGTERM'd by ``_kill_stale_tool``.

    Spawns a throwaway subprocess that sleeps for 30 seconds, writes its
    PID to the per-tool file, and verifies ``_kill_stale_tool`` kills it
    within the 3-second grace window.
    """
    import os
    import signal
    import subprocess
    import time

    # Sleep for 30s — long enough that without a SIGTERM the test would
    # fail by timeout, but short enough that if SIGTERM fails the child
    # will eventually exit on its own.
    proc = subprocess.Popen(
        ["python3", "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        # Wait for subprocess to actually start.
        time.sleep(0.1)
        assert proc.poll() is None

        orchestrator._record_tool_pid(orch_dirs["data"], "curator", proc.pid)

        # _kill_stale_tool should SIGTERM the process and wait up to 3s.
        orchestrator._kill_stale_tool(orch_dirs["data"], "curator")

        # PID file should be gone.
        pid_path = orchestrator._tool_pid_path(orch_dirs["data"], "curator")
        assert not pid_path.exists()

        # Process should be dead within a short additional window.
        for _ in range(30):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert proc.poll() is not None, "Stale subprocess should have been killed"
    finally:
        # Defensive cleanup — if the test failed above, make sure the
        # subprocess doesn't linger.
        if proc.poll() is None:
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait(timeout=2)
