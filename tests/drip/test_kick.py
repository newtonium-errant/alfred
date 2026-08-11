"""#83 item 5 — the on-submit kick.

The kick is an OPTIONAL head start: it turns "up to an hour of nothing" into
"results in minutes", and every failure path must degrade to the timer rather
than to a failed submission. So these pin two things that are easy to get
backwards — that the kick never raises into an already-durable submission, and
that it never runs without an explicit ``--config`` (which would drain a
different instance's campaigns and spend a different instance's budget).

``subprocess.Popen`` is stubbed rather than really spawned: what matters is the
COMMAND, and actually launching ``alfred drip run`` from a unit test would run a
real campaign. The one thing a stub cannot prove — that the composed argv is
accepted by the real parser — is pinned separately by parsing it with the
production argparse tree.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import structlog

from alfred.drip.kick import DRIP_LOG_NAME, kick_drip_run


class _FakeProc:
    pid = 4242


def _stub_popen(monkeypatch) -> list[dict]:
    """Capture Popen calls instead of spawning. Returns the capture list."""
    calls: list[dict] = []

    def _fake(cmd, **kwargs):
        calls.append({"cmd": cmd, "kwargs": kwargs})
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", _fake)
    return calls


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def test_the_kick_runs_the_same_entry_point_the_timer_runs(
    monkeypatch, tmp_path: Path,
) -> None:
    """One ignition mechanism, invoked two ways.

    An in-process runner call would be a SECOND path to the same work, free to
    drift in config resolution, budgets and error handling. Pinning the argv is
    what keeps the kick and the timer the same thing.
    """
    calls = _stub_popen(monkeypatch)
    pid = kick_drip_run(
        config_path="/etc/alfred/config.vera.yaml",
        campaign="batch_image",
        data_dir=tmp_path,
    )
    assert pid == 4242
    assert calls[0]["cmd"] == [
        sys.executable, "-m", "alfred",
        "--config", "/etc/alfred/config.vera.yaml",
        "drip", "run",
        "--campaign", "batch_image",
    ]


def test_the_composed_command_parses_against_the_real_cli(monkeypatch, tmp_path: Path) -> None:
    """The argv must survive the PRODUCTION parser, not just look plausible.

    This is the half a Popen stub cannot cover. ``drip run`` applies by default
    (``apply = not args.dry_run``), so an added ``--apply`` would be an argparse
    error raised inside a detached child — invisible except as a batch that
    silently never drains.
    """
    calls = _stub_popen(monkeypatch)
    kick_drip_run(
        config_path="config.vera.yaml", campaign="batch_image", data_dir=tmp_path,
    )
    from alfred.cli import build_parser

    # Drop [python, -m, alfred]; the parser sees what follows.
    args = build_parser().parse_args(calls[0]["cmd"][3:])
    assert args.config == "config.vera.yaml"
    assert args.drip_cmd == "run"
    assert args.campaign == "batch_image"
    assert args.dry_run is False, "a kicked run must APPLY, not dry-run"


def test_the_child_is_detached_and_never_inherits_stdin(
    monkeypatch, tmp_path: Path,
) -> None:
    """It must outlive the request and survive a daemon restart."""
    calls = _stub_popen(monkeypatch)
    kick_drip_run(config_path="c.yaml", campaign="batch_image", data_dir=tmp_path)
    kwargs = calls[0]["kwargs"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.STDOUT


def test_child_output_lands_in_the_drip_log(monkeypatch, tmp_path: Path) -> None:
    """Where the operator already looks — not a second place invented here."""
    calls = _stub_popen(monkeypatch)
    kick_drip_run(config_path="c.yaml", campaign="batch_image", data_dir=tmp_path)
    stdout = calls[0]["kwargs"]["stdout"]
    assert stdout is not subprocess.DEVNULL
    assert Path(stdout.name) == tmp_path / DRIP_LOG_NAME


def test_the_parent_does_not_leak_a_descriptor_per_submission(
    monkeypatch, tmp_path: Path,
) -> None:
    """The child dups the fd; the parent's copy must be closed.

    One leaked descriptor per submission is a slow daemon death that only shows
    up under real use, which is exactly the class of bug a unit test should own.
    """
    calls = _stub_popen(monkeypatch)
    kick_drip_run(config_path="c.yaml", campaign="batch_image", data_dir=tmp_path)
    assert calls[0]["kwargs"]["stdout"].closed is True


# ---------------------------------------------------------------------------
# Refusals and degradation — the kick must never break a saved submission
# ---------------------------------------------------------------------------


def test_no_config_path_declines_to_kick(monkeypatch, tmp_path: Path) -> None:
    """THE cross-instance pin, and it asserts NOTHING WAS SPAWNED.

    ``alfred`` with no ``--config`` reads ``config.yaml``. A Hypatia daemon
    kicking without one would drain SALEM's campaigns against SALEM's vault on
    SALEM's budget. Declining is correct; the timer still covers the batch.
    """
    calls = _stub_popen(monkeypatch)
    with structlog.testing.capture_logs() as captured:
        pid = kick_drip_run(config_path="", campaign="batch_image", data_dir=tmp_path)

    assert pid is None
    assert calls == [], "kicked a run without an explicit --config"
    declined = [c for c in captured if c.get("event") == "drip.kick.no_config_path"]
    assert len(declined) == 1
    assert "DIFFERENT instance" in declined[0]["detail"]


def test_a_failed_spawn_returns_none_instead_of_raising(
    monkeypatch, tmp_path: Path,
) -> None:
    """The submission is already on disk — it must not be reported as failed.

    Asserts the LOG explains the degradation, not just that no exception
    escaped: an operator whose kick silently stopped working needs to find out
    from the log rather than from a batch that drains an hour late forever.
    """
    def _boom(cmd, **kwargs):
        raise OSError("no fork for you")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    with structlog.testing.capture_logs() as captured:
        pid = kick_drip_run(
            config_path="c.yaml", campaign="batch_image", data_dir=tmp_path,
        )

    assert pid is None
    failed = [c for c in captured if c.get("event") == "drip.kick.spawn_failed"]
    assert len(failed) == 1
    assert failed[0]["error_type"] == "OSError"
    assert "hourly timer" in failed[0]["note"]


def test_an_unwritable_log_degrades_to_devnull_and_still_kicks(
    monkeypatch, tmp_path: Path,
) -> None:
    """Losing the child's stdout is bad; not draining the batch is worse."""
    calls = _stub_popen(monkeypatch)
    # A FILE where the data dir should be — mkdir/open both fail.
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("x")

    with structlog.testing.capture_logs() as captured:
        pid = kick_drip_run(
            config_path="c.yaml", campaign="batch_image",
            data_dir=blocked / "data",
        )

    assert pid == 4242, "an unwritable log must not stop the run"
    assert calls[0]["kwargs"]["stdout"] is subprocess.DEVNULL
    assert [c for c in captured if c.get("event") == "drip.kick.log_unavailable"]


def test_a_successful_kick_is_logged_with_its_pid(
    monkeypatch, tmp_path: Path,
) -> None:
    """ILB: a detached child is invisible unless its pid is recorded."""
    _stub_popen(monkeypatch)
    with structlog.testing.capture_logs() as captured:
        kick_drip_run(
            config_path="c.yaml", campaign="batch_image", data_dir=tmp_path,
        )
    spawned = [c for c in captured if c.get("event") == "drip.kick.spawned"]
    assert len(spawned) == 1
    assert spawned[0]["pid"] == 4242
    assert spawned[0]["campaign"] == "batch_image"
