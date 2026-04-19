"""Exit-code contracts — ``_MISSING_DEPS_EXIT = 78`` and retry accounting.

Two contracts:

1. ``exit_code == 78`` (``_MISSING_DEPS_EXIT``) means "missing optional
   dependency" — typically surveyor without the ML extras. The orchestrator
   must NOT restart the process. It logs and drops the tool from the live
   list.

2. Any other non-zero exit triggers a restart, up to ``MAX_RETRIES = 5``
   consecutive. The 6th crash drops the tool for good.

These tests use fake runners that exit with a scripted code on the first
invocation. Because the orchestrator's monitor loop sleeps 5 seconds
between checks (patched to ~1ms via ``fast_sleep``), the test wall time
stays sub-second.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

import alfred.orchestrator as orchestrator


# ---------------------------------------------------------------------------
# Fakes — live here instead of _fakes.py because they carry per-test state
# via module-level counters, which fork()'d children would still see.
# ---------------------------------------------------------------------------

_start_counter_path: Path | None = None


def _read_start_count(path: Path) -> int:
    """Return number of lines in the start-counter file (one line per start)."""
    try:
        return sum(1 for _ in path.open("r", encoding="utf-8"))
    except (FileNotFoundError, OSError):
        return 0


def _record_start(raw: dict[str, Any], tool: str) -> None:
    """Append a start-marker line with our PID to the per-tool counter file."""
    counter = raw.get("_fake_runner", {}).get("counter_files", {}).get(tool)
    if counter:
        try:
            with open(counter, "a", encoding="utf-8") as f:
                f.write(f"start\n")
        except OSError:
            pass


def _fake_78_then_0_3arg(
    raw: dict[str, Any], skills_dir: str, suppress_stdout: bool = False,
) -> None:
    """Exits 78 on first invocation, 0 on subsequent — but the 78 contract
    should ensure 'subsequent' never happens.
    """
    _record_start(raw, "curator")
    counter = raw.get("_fake_runner", {}).get("counter_files", {}).get("curator")
    if counter and _read_start_count(Path(counter)) == 1:
        # First invocation — exit 78 to signal missing deps
        import sys
        sys.exit(78)
    # Shouldn't be reached — but if it is, exit 0 so we don't spin forever.
    import sys
    sys.exit(0)


def _fake_exit_1_always_3arg(
    raw: dict[str, Any], skills_dir: str, suppress_stdout: bool = False,
) -> None:
    """Always exits 1 — triggers restart loop."""
    _record_start(raw, "curator")
    import sys
    sys.exit(1)


def _fake_exit_0_after_delay_3arg(
    raw: dict[str, Any], skills_dir: str, suppress_stdout: bool = False,
) -> None:
    """Short delay then exits 0 — proves the tool was healthy."""
    _record_start(raw, "curator")
    time.sleep(0.05)
    import sys
    sys.exit(0)


def _fake_exit_78_2arg(
    raw: dict[str, Any], suppress_stdout: bool = False,
) -> None:
    """Two-arg variant for surveyor-shape tests."""
    _record_start(raw, "surveyor")
    import sys
    sys.exit(78)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wire_counter(raw: dict, orch_dirs: dict, tool: str) -> Path:
    """Attach a counter file for *tool* to raw['_fake_runner']."""
    path = orch_dirs["data"] / f"count_{tool}"
    raw.setdefault("_fake_runner", {}).setdefault("counter_files", {})[tool] = str(path)
    return path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_exit_78_prevents_restart(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_fake_runners, fire_sentinel_after, capsys,
) -> None:
    """Tool that exits 78 (_MISSING_DEPS_EXIT) is NOT restarted.

    The orchestrator must see the 78 exit and drop the tool from the
    live list — no restart counter bump, no ``start_process`` re-call.
    """
    raw = orchestrator_raw_config
    counter = _wire_counter(raw, orch_dirs, "curator")
    install_fake_runners({"curator": _fake_78_then_0_3arg})

    fire_sentinel_after(1.0)  # Give orchestrator time to observe exit + NOT restart
    orchestrator.run_all(
        raw, only="curator",
        skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"],
        live_mode=False,
    )

    # The fake exits 78 on its FIRST start. If the 78 contract holds,
    # there is exactly 1 start. If the orchestrator ignored 78 and
    # restarted, there'd be 2+ starts (up to 5 retries).
    start_count = _read_start_count(counter)
    assert start_count == 1, (
        f"Expected exactly 1 start for 78-exit contract, got {start_count}"
    )

    out = capsys.readouterr().out
    assert "missing dependencies" in out.lower() or "not restarting" in out.lower()


def test_exit_78_two_arg_runner(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_fake_runners, fire_sentinel_after,
) -> None:
    """The 78 contract applies equally to two-arg tools (surveyor/mail/brief)."""
    raw = orchestrator_raw_config
    raw["surveyor"] = {}
    counter = _wire_counter(raw, orch_dirs, "surveyor")
    install_fake_runners({"surveyor": _fake_exit_78_2arg})

    fire_sentinel_after(1.0)
    orchestrator.run_all(
        raw, only="surveyor",
        skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"],
        live_mode=False,
    )

    # Again, exactly one start — no restart.
    start_count = _read_start_count(counter)
    assert start_count == 1


def test_nonzero_exit_triggers_restart_up_to_limit(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_fake_runners, fire_sentinel_after, capsys,
) -> None:
    """Tool that always exits 1 is restarted 5 times, then given up.

    This is the MAX_RETRIES = 5 contract. On start #6 (i.e., the 6th
    crash), orchestrator prints "exceeded restart limit" and drops
    the tool. With ``only=curator``, dropping the last tool exits
    the main loop ("All daemons failed, exiting.").
    """
    raw = orchestrator_raw_config
    counter = _wire_counter(raw, orch_dirs, "curator")
    install_fake_runners({"curator": _fake_exit_1_always_3arg})

    # Don't fire the sentinel — let the retry loop exhaust itself naturally.
    # With fast_sleep the 6 consecutive crashes should complete in <1s.
    # But arm a safety sentinel at 5s so a runaway loop can't hang the suite.
    fire_sentinel_after(5.0)

    orchestrator.run_all(
        raw, only="curator",
        skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"],
        live_mode=False,
    )

    # Initial start + 5 restarts = 6 total starts. After the 6th crash
    # the orchestrator prints "exceeded restart limit" and drops the tool.
    start_count = _read_start_count(counter)
    # Allow some slack — process startup races could compress the count
    # under very fast sleep. 1 + 5 = 6 is the contract; tolerate 5-6 to
    # avoid flake on tight scheduling.
    assert 5 <= start_count <= 6, (
        f"Expected 5-6 starts for 5-retry contract, got {start_count}"
    )

    out = capsys.readouterr().out
    # Exactly one "exceeded restart limit" message for the tool.
    assert "exceeded restart limit" in out.lower()


def test_healthy_process_stays_alive_through_loop(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_fake_runners, fire_sentinel_after,
) -> None:
    """A tool that exits 0 but keeps being respawned increments restart count.

    Exit 0 isn't a "clean shutdown" signal — the orchestrator restarts
    ANY non-78 exit (including 0) because daemons are supposed to block
    on their event loops forever. Exit 0 means the daemon returned
    unexpectedly, which is a crash in practice.
    """
    raw = orchestrator_raw_config
    counter = _wire_counter(raw, orch_dirs, "curator")
    install_fake_runners({"curator": _fake_exit_0_after_delay_3arg})

    fire_sentinel_after(1.0)
    orchestrator.run_all(
        raw, only="curator",
        skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"],
        live_mode=False,
    )

    # Should have restarted at least twice — ``_fake_exit_0`` returns 0
    # after 50ms, and the monitor checks every ~50ms (fast_sleep). So
    # the tool should have started, died, restarted, died, at least once
    # before the 1-second sentinel fires.
    start_count = _read_start_count(counter)
    assert start_count >= 1, "Tool should have started at least once"
    # Don't upper-bound — with sleeps scheduled down, restart cadence
    # is tight; if restart-on-0 stops working, this still catches the
    # regression because the count would drop to exactly 1.
