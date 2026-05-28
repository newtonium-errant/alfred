"""Tests for the Algernon instance-set orchestrator (Phase 1, 2026-05-28).

Covers:
  * Registry loader — valid YAML, enabled filter, missing-file error
  * ``run_verb`` subprocess command shape pin
  * Summary sentinel — all-OK + partial-failure shapes
  * Already-running idempotency on ``up``
  * ``down`` summary discrimination (stopped vs was-not-running)
  * ``status`` default one-line shape
  * CLI dispatch — top-level suppressed alias ``up-all`` AND canonical
    ``instance up`` both route to the same handler

Per ``feedback_regression_pin_unconditional``: no
``pytest.importorskip``; all tests run unconditionally.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from alfred.instance_set import (
    DEFAULT_REGISTRY_PATH,
    Instance,
    STARTER_REGISTRY_YAML,
    format_summary_sentinel,
    iter_enabled,
    load_registry,
    run_verb,
    run_verb_across_set,
)


# ---------------------------------------------------------------------------
# Registry loader
# ---------------------------------------------------------------------------


def _write_registry(path: Path, instances: list[dict]) -> None:
    """Write a registry YAML file to ``path``."""
    import yaml
    path.write_text(
        yaml.safe_dump({"instances": instances}, sort_keys=False),
        encoding="utf-8",
    )


def test_load_registry_parses_three_instances(tmp_path: Path) -> None:
    """The canonical Phase 1 starter shape — three rows, all enabled."""
    registry = tmp_path / "instances.yaml"
    _write_registry(registry, [
        {"name": "salem", "display": "Salem",
         "config": "/path/to/config.yaml", "enabled": True},
        {"name": "kal-le", "display": "KAL-LE",
         "config": "/path/to/config.kalle.yaml", "enabled": True},
        {"name": "hypatia", "display": "Hypatia",
         "config": "/path/to/config.hypatia.yaml", "enabled": True},
    ])
    instances = load_registry(registry)
    assert len(instances) == 3
    assert instances[0].name == "salem"
    assert instances[0].display == "Salem"
    assert instances[0].config == "/path/to/config.yaml"
    assert instances[0].enabled is True
    # Ordering preserved from the YAML.
    assert [i.name for i in instances] == ["salem", "kal-le", "hypatia"]


def test_iter_enabled_filters_disabled(tmp_path: Path) -> None:
    """``enabled: false`` drains an instance from fan-out without
    deleting the row."""
    registry = tmp_path / "instances.yaml"
    _write_registry(registry, [
        {"name": "salem", "display": "Salem",
         "config": "/a.yaml", "enabled": True},
        {"name": "kal-le", "display": "KAL-LE",
         "config": "/b.yaml", "enabled": False},
        {"name": "hypatia", "display": "Hypatia",
         "config": "/c.yaml", "enabled": True},
    ])
    instances = load_registry(registry)
    enabled = list(iter_enabled(instances))
    assert len(enabled) == 2
    assert [i.name for i in enabled] == ["salem", "hypatia"]


def test_load_registry_default_enabled_is_true(tmp_path: Path) -> None:
    """``enabled:`` field omitted → default True."""
    registry = tmp_path / "instances.yaml"
    _write_registry(registry, [
        {"name": "salem", "display": "Salem", "config": "/a.yaml"},
    ])
    instances = load_registry(registry)
    assert instances[0].enabled is True


def test_load_registry_missing_file_raises_actionable_error() -> None:
    """Missing registry → clear FileNotFoundError naming the path
    + hint at the bootstrap command."""
    nonexistent = Path("/tmp/__nonexistent_alfred_instances__.yaml")
    with pytest.raises(FileNotFoundError) as exc_info:
        load_registry(nonexistent)
    msg = str(exc_info.value)
    assert str(nonexistent) in msg
    # Operator-actionable hint surface.
    assert "Phase 1 docs" in msg or "--registry" in msg or "registry" in msg


def test_load_registry_missing_instances_key_raises(tmp_path: Path) -> None:
    """Malformed YAML lacking the top-level ``instances:`` key surfaces
    a clear ValueError, not a NoneType crash."""
    registry = tmp_path / "instances.yaml"
    registry.write_text("# empty\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc_info:
        load_registry(registry)
    assert "instances" in str(exc_info.value)


def test_load_registry_missing_required_field_raises(tmp_path: Path) -> None:
    """An instance entry missing ``config`` → ValueError naming the
    field + the entry index."""
    registry = tmp_path / "instances.yaml"
    _write_registry(registry, [
        {"name": "salem", "display": "Salem"},  # missing ``config``
    ])
    with pytest.raises(ValueError) as exc_info:
        load_registry(registry)
    msg = str(exc_info.value)
    assert "config" in msg
    assert "#0" in msg


def test_default_registry_path_is_home_alfred_instances() -> None:
    """Pin the canonical default — operators reading the docs trust
    ``~/.alfred/instances.yaml`` and a refactor that silently moves
    it would surface here."""
    assert DEFAULT_REGISTRY_PATH == Path.home() / ".alfred" / "instances.yaml"


def test_starter_registry_yaml_parses_with_three_instances(
    tmp_path: Path,
) -> None:
    """The shipped starter registry text MUST parse via load_registry
    so an operator's ``cp instances.yaml.example ~/.alfred/instances.yaml``
    one-shot works without further hand-editing for a default-shape
    install."""
    registry = tmp_path / "instances.yaml"
    registry.write_text(STARTER_REGISTRY_YAML, encoding="utf-8")
    instances = load_registry(registry)
    assert len(instances) == 3
    names = {i.name for i in instances}
    assert names == {"salem", "kal-le", "hypatia"}


# ---------------------------------------------------------------------------
# run_verb — subprocess command shape + per-verb summary
# ---------------------------------------------------------------------------


def _fake_proc(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Build a fake CompletedProcess for subprocess.run mocking."""
    class _P:
        pass
    p = _P()
    p.stdout = stdout
    p.stderr = stderr
    p.returncode = returncode
    return p


def test_run_verb_subprocess_command_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the subprocess command shape per dispatch spec:
    ``[sys.executable, "-m", "alfred", "--config", X, "up"]``.
    Module path is ``alfred`` (NOT ``alfred.cli``; verified by the
    2026-05-28 tier migration silent-CLI-failure incident).

    Exercised via ``up`` with the pre-check stubbed to return None so
    the idempotency short-circuit doesn't fire and the subprocess
    path runs.
    """
    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _fake_proc(stdout="Alfred started.\n", returncode=0)

    monkeypatch.setattr("alfred.instance_set.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "alfred.instance_set.check_running",
        lambda inst: None,
    )

    inst = Instance(
        name="salem", display="Salem",
        config="/path/to/config.yaml", enabled=True,
    )
    run_verb(inst, "up", [])
    cmd = captured["cmd"]
    # Find ``-m`` flag position.
    m_idx = cmd.index("-m")
    assert cmd[m_idx + 1] == "alfred", (
        f"Module path must be ``alfred``, NOT ``alfred.cli`` — see "
        f"2026-05-28 incident. Got {cmd[m_idx + 1]!r}."
    )
    # ``--config`` + path follows.
    config_idx = cmd.index("--config")
    assert cmd[config_idx + 1] == "/path/to/config.yaml"
    # Verb after config.
    assert "up" in cmd[config_idx + 2:]


def test_run_verb_up_already_running_short_circuits_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per ratified Phase 1 decision #1: already-running counts as OK
    on ``up``. The pre-check on PID file presence skips the
    subprocess invocation entirely — cleaner and faster than letting
    the subprocess fail-with-message and parsing stderr."""
    subprocess_called = {"yes": False}

    def _fake_run(cmd, **kwargs):
        subprocess_called["yes"] = True
        return _fake_proc(returncode=0)

    monkeypatch.setattr("alfred.instance_set.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "alfred.instance_set.check_running",
        lambda inst: 41450,
    )

    inst = Instance(
        name="salem", display="Salem", config="/a.yaml", enabled=True,
    )
    rc, summary = run_verb(inst, "up", [])

    # Subprocess never invoked.
    assert subprocess_called["yes"] is False
    # OK + canonical summary shape.
    assert rc == 0
    assert summary == "Salem: already-running (pid 41450)"


def test_run_verb_up_fresh_start_summary_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-running instance + successful subprocess up → ``started``
    summary with PID extracted from the post-spawn PID file."""
    monkeypatch.setattr(
        "alfred.instance_set.subprocess.run",
        lambda cmd, **kw: _fake_proc(stdout="ok\n", returncode=0),
    )
    # First check_running (pre-flight) → None; second (post-spawn) → PID.
    call_count = {"n": 0}

    def _check(inst):
        call_count["n"] += 1
        return None if call_count["n"] == 1 else 47891

    monkeypatch.setattr("alfred.instance_set.check_running", _check)

    inst = Instance(
        name="kal-le", display="KAL-LE", config="/a.yaml", enabled=True,
    )
    rc, summary = run_verb(inst, "up", [])
    assert rc == 0
    assert summary == "KAL-LE: started (pid 47891)"


def test_run_verb_down_stopped_vs_was_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``down`` discrimination: cmd_down prints either ``Alfred
    stopped.`` or ``Alfred is not running.`` — wrapper parses to
    distinguish."""
    # Case 1: was running → stopped
    monkeypatch.setattr(
        "alfred.instance_set.subprocess.run",
        lambda cmd, **kw: _fake_proc(stdout="Alfred stopped.\n", returncode=0),
    )
    inst = Instance(
        name="salem", display="Salem", config="/a.yaml", enabled=True,
    )
    rc, summary = run_verb(inst, "down", [])
    assert rc == 0
    assert summary == "Salem: stopped"

    # Case 2: was idle → was not running
    monkeypatch.setattr(
        "alfred.instance_set.subprocess.run",
        lambda cmd, **kw: _fake_proc(
            stdout="Alfred is not running.\n", returncode=0,
        ),
    )
    rc, summary = run_verb(inst, "down", [])
    assert rc == 0
    assert summary == "Salem: was not running"


def test_run_verb_status_running_summary_carries_pid_and_vault(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``status`` summary names the PID and the vault path so the
    operator sees at a glance which vault each running instance
    is serving."""
    # Write a minimal config the wrapper can read for the vault path.
    config = tmp_path / "config.yaml"
    config.write_text(
        "vault:\n  path: /home/andrew/alfred/vault\nlogging:\n  dir: /tmp\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "alfred.instance_set.subprocess.run",
        lambda cmd, **kw: _fake_proc(stdout="status output\n", returncode=0),
    )
    monkeypatch.setattr(
        "alfred.instance_set.check_running", lambda inst: 41450,
    )

    inst = Instance(
        name="salem", display="Salem",
        config=str(config), enabled=True,
    )
    rc, summary = run_verb(inst, "status", [])
    assert rc == 0
    assert "Salem: running (pid 41450)" in summary
    assert "vault=/home/andrew/alfred/vault" in summary


def test_run_verb_failure_carries_short_stderr_in_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure: non-zero subprocess exit → ``FAILED — <first line of
    stderr>`` summary so the operator gets diagnostic detail without
    re-running with --verbose."""
    monkeypatch.setattr(
        "alfred.instance_set.subprocess.run",
        lambda cmd, **kw: _fake_proc(
            stderr="ImportError: cannot import name 'broken_thing'\n",
            returncode=1,
        ),
    )
    monkeypatch.setattr(
        "alfred.instance_set.check_running", lambda inst: None,
    )

    inst = Instance(
        name="kal-le", display="KAL-LE", config="/a.yaml", enabled=True,
    )
    rc, summary = run_verb(inst, "down", [])
    assert rc == 1
    assert "KAL-LE: FAILED" in summary
    assert "ImportError" in summary


# ---------------------------------------------------------------------------
# Summary sentinel — per intentionally-left-blank
# ---------------------------------------------------------------------------


def test_summary_sentinel_all_ok_shape() -> None:
    """All-OK: ``instance <verb>: N/N OK``. Pinned because the
    summary line is the canonical 'ran, here's the count' canary
    per feedback_intentionally_left_blank.md."""
    results = [
        (0, "Salem: already-running (pid 41450)"),
        (0, "KAL-LE: started (pid 47891)"),
        (0, "Hypatia: started (pid 47892)"),
    ]
    assert format_summary_sentinel("up", results) == "instance up: 3/3 OK"


def test_summary_sentinel_partial_failure_names_failed(
) -> None:
    """Partial failure: ``X/N OK — <Display> failed`` so the operator
    sees the per-failure-instance distribution in the summary line
    without scrolling back through the per-line output."""
    results = [
        (0, "Salem: already-running (pid 41450)"),
        (1, "KAL-LE: FAILED — ImportError: cannot import name 'X'"),
        (0, "Hypatia: started (pid 47892)"),
    ]
    sentinel = format_summary_sentinel("up", results)
    assert "2/3 OK" in sentinel
    assert "KAL-LE" in sentinel
    assert "failed" in sentinel


def test_summary_sentinel_status_format_running_count() -> None:
    """Status sentinel: ``instance status: X/N running``. Distinct
    from up/down because ``running`` is the operator's mental model
    for status, not ``OK``."""
    results = [
        (0, "Salem: running (pid 41450)  vault=/home/andrew/alfred/vault"),
        (0, "KAL-LE: running (pid 28050)  vault=/home/andrew/.alfred/kalle/vault"),
        (0, "Hypatia: stopped"),
    ]
    assert (
        format_summary_sentinel("status", results)
        == "instance status: 2/3 running"
    )


# ---------------------------------------------------------------------------
# Cross-set fan-out
# ---------------------------------------------------------------------------


def test_run_verb_across_set_returns_per_instance_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the cross-set fan-out: results list mirrors the input
    instance list in order, with one tuple per enabled instance."""
    instances = [
        Instance(name="salem", display="Salem", config="/a.yaml", enabled=True),
        Instance(name="kal-le", display="KAL-LE", config="/b.yaml", enabled=True),
        Instance(name="hypatia", display="Hypatia", config="/c.yaml", enabled=True),
    ]
    # All pre-flight check_running → None → exercise subprocess path.
    monkeypatch.setattr(
        "alfred.instance_set.check_running", lambda inst: None,
    )
    monkeypatch.setattr(
        "alfred.instance_set.subprocess.run",
        lambda cmd, **kw: _fake_proc(stdout="Alfred stopped.\n", returncode=0),
    )

    results, exit_code = run_verb_across_set(instances, "down")
    assert exit_code == 0
    assert len(results) == 3
    # Order preserved from input list.
    assert "Salem" in results[0][1]
    assert "KAL-LE" in results[1][1]
    assert "Hypatia" in results[2][1]


def test_run_verb_across_set_skips_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``enabled: false`` excluded from fan-out — disabled instances
    don't appear in the results list."""
    instances = [
        Instance(name="salem", display="Salem", config="/a.yaml", enabled=True),
        Instance(name="kal-le", display="KAL-LE", config="/b.yaml", enabled=False),
    ]
    monkeypatch.setattr(
        "alfred.instance_set.check_running", lambda inst: None,
    )
    monkeypatch.setattr(
        "alfred.instance_set.subprocess.run",
        lambda cmd, **kw: _fake_proc(stdout="Alfred stopped.\n", returncode=0),
    )

    results, exit_code = run_verb_across_set(instances, "down")
    assert len(results) == 1
    assert "Salem" in results[0][1]


def test_run_verb_across_set_any_failure_returns_exit_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed-result fan-out: one instance fails → overall exit code
    is 1; per-instance results preserve the rc=0 entries."""
    instances = [
        Instance(name="salem", display="Salem", config="/a.yaml", enabled=True),
        Instance(name="kal-le", display="KAL-LE", config="/b.yaml", enabled=True),
    ]
    monkeypatch.setattr(
        "alfred.instance_set.check_running", lambda inst: None,
    )

    call_count = {"n": 0}

    def _fake_run(cmd, **kw):
        call_count["n"] += 1
        if call_count["n"] == 2:
            return _fake_proc(stderr="ScopeError: denied\n", returncode=1)
        return _fake_proc(stdout="Alfred stopped.\n", returncode=0)

    monkeypatch.setattr("alfred.instance_set.subprocess.run", _fake_run)
    results, exit_code = run_verb_across_set(instances, "down")
    assert exit_code == 1
    assert results[0][0] == 0  # Salem OK
    assert results[1][0] == 1  # KAL-LE failed


# ---------------------------------------------------------------------------
# CLI dispatch — top-level alias AND canonical form both work
# ---------------------------------------------------------------------------


def test_top_level_up_all_dispatches_to_instance_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Pin the suppressed alias ``alfred up-all`` dispatches to the
    SAME handler as ``alfred instance up``. The canonical form is
    documented; the alias preserves muscle-memory typing."""
    from alfred.cli import build_parser, cmd_instance_up_all

    registry = tmp_path / "instances.yaml"
    _write_registry(registry, [
        {"name": "salem", "display": "Salem",
         "config": "/a.yaml", "enabled": True},
    ])

    parser = build_parser()
    args = parser.parse_args(["up-all", "--registry", str(registry)])
    # Handler dict in main() routes ``up-all`` to cmd_instance_up_all;
    # verify by reading the handlers map directly (the same dict the
    # main() dispatcher uses).
    assert args.command == "up-all"
    # Stub the actual run_verb_across_set so we don't fork real
    # subprocesses.
    monkeypatch.setattr(
        "alfred.instance_set.check_running", lambda inst: 12345,
    )
    with pytest.raises(SystemExit) as exc_info:
        cmd_instance_up_all(args)
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "Salem: already-running" in out
    # Summary sentinel fires.
    assert "instance up:" in out


def test_canonical_instance_up_dispatches_via_cmd_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """``alfred instance up`` — canonical form — routes through
    cmd_instance which dispatches to cmd_instance_up_all when the
    subcommand is ``up``."""
    from alfred.cli import cmd_instance

    registry = tmp_path / "instances.yaml"
    _write_registry(registry, [
        {"name": "salem", "display": "Salem",
         "config": "/a.yaml", "enabled": True},
    ])

    monkeypatch.setattr(
        "alfred.instance_set.check_running", lambda inst: 99999,
    )

    args = argparse.Namespace(
        instance_cmd="up",
        registry=str(registry),
    )
    with pytest.raises(SystemExit) as exc_info:
        cmd_instance(args)
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "Salem: already-running (pid 99999)" in out
    assert "instance up:" in out


def test_instance_status_default_renders_one_line_per_instance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Default ``alfred instance status`` (no --verbose, no --json)
    renders one line per instance + the summary sentinel."""
    from alfred.cli import cmd_instance_status_all

    # Configs need to exist for vault-path extraction (best effort
    # falls back to None if absent; vault clause omitted from line).
    config_a = tmp_path / "salem.yaml"
    config_a.write_text(
        "vault:\n  path: /vault/salem\nlogging:\n  dir: /tmp\n",
        encoding="utf-8",
    )
    config_b = tmp_path / "kalle.yaml"
    config_b.write_text(
        "vault:\n  path: /vault/kalle\nlogging:\n  dir: /tmp\n",
        encoding="utf-8",
    )

    registry = tmp_path / "instances.yaml"
    _write_registry(registry, [
        {"name": "salem", "display": "Salem",
         "config": str(config_a), "enabled": True},
        {"name": "kal-le", "display": "KAL-LE",
         "config": str(config_b), "enabled": True},
    ])

    # Pretend Salem is running, KAL-LE is stopped.
    def _check(inst):
        return 41450 if inst.name == "salem" else None
    monkeypatch.setattr("alfred.instance_set.check_running", _check)
    monkeypatch.setattr(
        "alfred.instance_set.subprocess.run",
        lambda cmd, **kw: _fake_proc(stdout="status ok\n", returncode=0),
    )

    args = argparse.Namespace(
        instance_cmd="status",
        registry=str(registry),
        verbose=False,
        json=False,
    )
    cmd_instance_status_all(args)
    out = capsys.readouterr().out
    # Per-line outputs.
    assert "Salem: running (pid 41450)" in out
    assert "KAL-LE: stopped" in out
    # Summary sentinel — counts running instances.
    assert "instance status: 1/2 running" in out


def test_instance_status_verbose_aggregates_with_section_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """``--verbose`` mode concatenates full ``alfred status`` per
    instance with ``=== <Display> ===`` headers so the operator
    can read full diagnostic output for each instance in one shot."""
    from alfred.cli import cmd_instance_status_all

    registry = tmp_path / "instances.yaml"
    _write_registry(registry, [
        {"name": "salem", "display": "Salem",
         "config": "/a.yaml", "enabled": True},
        {"name": "kal-le", "display": "KAL-LE",
         "config": "/b.yaml", "enabled": True},
    ])

    def _fake_run(cmd, **kw):
        # Different output per instance so we can tell them apart
        # in the concatenated stream.
        config_idx = cmd.index("--config")
        config = cmd[config_idx + 1]
        if "a.yaml" in config:
            return _fake_proc(stdout="SALEM_FULL_STATUS\n", returncode=0)
        return _fake_proc(stdout="KALLE_FULL_STATUS\n", returncode=0)

    # The verbose branch uses subprocess.run from cli.py's own import,
    # not the one in instance_set.py — so patch the cli.py reference
    # too. Same shape as the dispatcher env-var test-hygiene contract:
    # tests touching dispatcher paths must mock at the right call
    # site or env-var bleed surfaces as silent test failure.
    monkeypatch.setattr("alfred.cli.subprocess.run", _fake_run)
    monkeypatch.setattr(
        "alfred.instance_set.check_running", lambda inst: None,
    )

    args = argparse.Namespace(
        instance_cmd="status",
        registry=str(registry),
        verbose=True,
        json=False,
    )
    cmd_instance_status_all(args)
    out = capsys.readouterr().out
    # Section headers fire for each instance.
    assert "=== Salem ===" in out
    assert "=== KAL-LE ===" in out
    # Per-instance status content surfaces.
    assert "SALEM_FULL_STATUS" in out
    assert "KALLE_FULL_STATUS" in out
