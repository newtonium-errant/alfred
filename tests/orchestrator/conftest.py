"""Shared fixtures for the orchestrator test package.

Philosophy mirrors ``tests/curator/conftest.py``:

- Build throwaway directories under ``tmp_path`` for pid, log, data files.
- Swap ``TOOL_RUNNERS`` at test time via ``monkeypatch.setitem`` so the
  orchestrator spawns fake processes instead of the real daemons.
- Shrink ``time.sleep`` calls inside ``alfred.orchestrator`` to a small
  fraction so the 10-second stagger + 5-second poll loop finish in
  sub-second test wall time.

Tests that need the orchestrator to actually observe a long-lived child
(for SIGTERM-handler and teardown paths) override the speed factor back
to something slower so processes don't race the main loop.
"""

from __future__ import annotations

import time as _time
from pathlib import Path
from typing import Callable

import pytest

import alfred.orchestrator as orchestrator

# Capture the original un-patched sleep BEFORE monkeypatch can shadow it.
# ``alfred.orchestrator.time`` is the canonical ``time`` module object, so
# replacing ``orchestrator.time.sleep`` replaces it globally — our own
# fixture code would recurse into the patched version unless we stash the
# real function up front.
_REAL_SLEEP = _time.sleep


@pytest.fixture
def orch_dirs(tmp_path: Path) -> dict[str, Path]:
    """Per-test tmp dirs for data, logs, pid, sentinel, skills."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    pid_path = data_dir / "alfred.pid"
    return {
        "data": data_dir,
        "skills": skills_dir,
        "pid_path": pid_path,
        "sentinel_path": data_dir / "alfred.stop",
    }


@pytest.fixture
def orchestrator_raw_config(orch_dirs: dict[str, Path]) -> dict:
    """Minimal unified config dict the orchestrator uses.

    Only ``logging.dir`` is strictly required — everything else is optional
    and gets added per-test to drive the auto-start gating.
    """
    return {
        "logging": {"level": "INFO", "dir": str(orch_dirs["data"])},
        "vault": {"path": str(orch_dirs["data"].parent / "vault")},
        # "_fake_runner" is how tests wire behavior into fake runners
        # spawned by multiprocessing. See tests/orchestrator/_fakes.py.
        "_fake_runner": {"exit_code": 0, "delay": 0.1, "tool": "curator"},
    }


class _NoOpSignalShim:
    """Shim that stands in for ``signal`` when run_all executes on a
    non-main thread. ``signal.signal`` raises ValueError off the main
    thread, which is fine in production but breaks thread-based tests
    that observe process state concurrently.

    Forwards ``SIGTERM`` / ``SIGINT`` constants but swallows the
    ``signal()`` registration call.
    """

    SIGTERM = 15
    SIGINT = 2

    def __getattr__(self, name: str):
        import signal as _real
        return getattr(_real, name)

    def signal(self, signum, handler):  # noqa: A003 — matches stdlib name
        return handler


class _FastTimeShim:
    """Shim that replaces ``orchestrator.time`` at test time.

    Forwards everything to the real ``time`` module EXCEPT ``sleep``,
    which shrinks to ~1ms so the 10-second stagger + 5-second monitor
    poll finish in sub-second test wall time. ``time.monotonic()`` still
    advances correctly — just wall-clock advance, no simulated time.
    """

    def __getattr__(self, name: str):
        return getattr(_time, name)

    def sleep(self, _seconds: float) -> None:
        _REAL_SLEEP(0.001)


@pytest.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap ``alfred.orchestrator.time`` for a no-op-sleep shim.

    We replace the module-attribute rather than monkeypatching
    ``time.sleep`` globally — the latter recurses back into our own
    fixture code because fixtures share the ``time`` module object.
    """
    monkeypatch.setattr(orchestrator, "time", _FastTimeShim())
    return None


@pytest.fixture
def noop_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap ``alfred.orchestrator.signal`` for a no-op shim.

    Required when running ``run_all`` in a non-main thread — the stdlib
    ``signal.signal`` raises ValueError off the main thread. Tests that
    observe running processes from the main thread need run_all on a
    worker thread and therefore need to skip signal registration.
    """
    monkeypatch.setattr(orchestrator, "signal", _NoOpSignalShim())
    return None


@pytest.fixture
def install_fake_runners(monkeypatch: pytest.MonkeyPatch):
    """Return a callable that installs fake runners for given tools.

    Tests call ``install_fake_runners({"curator": fake_runner_3arg})`` to
    override specific entries in ``TOOL_RUNNERS`` without globally mutating
    the dict. ``monkeypatch.setitem`` restores it at test teardown.
    """

    def _install(mapping: dict[str, Callable]) -> None:
        for tool, runner in mapping.items():
            monkeypatch.setitem(orchestrator.TOOL_RUNNERS, tool, runner)

    return _install


@pytest.fixture
def fire_sentinel_after(
    orch_dirs: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    """Return a callable that makes the sentinel appear after N seconds.

    The orchestrator polls the sentinel file in its main monitor loop;
    writing the file is the clean way to request shutdown without raising
    signals from pytest.

    We schedule the write on a daemon thread — fires and forgets.
    """
    import threading

    threads: list[threading.Thread] = []

    def _arm(seconds: float) -> None:
        def _fire() -> None:
            # Use the real sleep — the orchestrator's monkeypatched
            # ``time.sleep`` is a no-op, and we need actual wall-clock
            # delay here to let the main loop progress.
            _REAL_SLEEP(seconds)
            try:
                orch_dirs["sentinel_path"].write_text("stop", encoding="utf-8")
            except OSError:
                pass

        t = threading.Thread(target=_fire, daemon=True)
        t.start()
        threads.append(t)

    yield _arm

    # Best-effort: let daemon threads finish so their writes don't leak
    # into the next test's tmp_path (different dir, but be paranoid).
    for t in threads:
        t.join(timeout=1.0)
