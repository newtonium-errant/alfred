"""STAY-C dedicated-backup installer (task #13 slice 13d-4b).

Pins the render → build_plan → apply_plan contract, the placeholder-sentinel bind, the ExecStart target
(retention backup-run), the dedicated-repo EnvironmentFile, and the INERT posture (no restic init, no
backup started — just the unit files + timer enable).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from alfred.scripts import install_stayc_backup as installer

_ROOT = Path("/data/algernon/stayc-clinical")


def _plan(tmp_path, **over):
    kw = dict(stayc_root=_ROOT, install_dir=tmp_path / "sysd", unit_user="andrew", unit_group="andrew")
    kw.update(over)
    return installer.build_plan(**kw)


def test_renders_both_units_no_residual_placeholder(tmp_path):
    plan = _plan(tmp_path)
    for content in (plan.service_content, plan.timer_content):
        assert not installer._RESIDUAL_PLACEHOLDER_RE.findall(content)
        for ph in installer._PLACEHOLDERS:
            assert ph not in content


def test_service_execstart_is_backup_run_with_config(tmp_path):
    plan = _plan(tmp_path)
    exec_lines = [l for l in plan.service_content.splitlines() if l.startswith("ExecStart=")]
    assert len(exec_lines) == 1
    assert "scribe retention backup-run" in exec_lines[0]
    assert str(_ROOT / "config.stayc-clinical.yaml") in exec_lines[0]
    assert str(_ROOT / ".venv" / "bin" / "python") in exec_lines[0]


def test_service_sources_dedicated_repo_env_no_leading_dash(tmp_path):
    plan = _plan(tmp_path)
    env_lines = [l for l in plan.service_content.splitlines() if l.startswith("EnvironmentFile=")]
    assert len(env_lines) == 1
    # NO leading dash — a missing dedicated-repo creds file must FAIL the backup loudly, not run against
    # an unconfigured repo (scribe.backup._restic_env fails closed, but the unit must not hide it).
    assert not env_lines[0].startswith("EnvironmentFile=-")
    assert str(_ROOT / "secrets" / "stayc-restic.env") in env_lines[0]


def test_timer_has_oncalendar_and_wantedby_timers(tmp_path):
    plan = _plan(tmp_path, on_calendar="*-*-* 05:00:00")
    assert "OnCalendar=*-*-* 05:00:00" in plan.timer_content
    assert "WantedBy=timers.target" in plan.timer_content


def test_placeholder_sentinel_binds(tmp_path):
    # A template that adds a placeholder the render doesn't substitute must raise (the sentinel) — the
    # exact drift the _PLACEHOLDERS lockstep guards.
    with pytest.raises(ValueError, match="placeholder"):
        installer._render("ExecStart=<STAYC_BACKUP_PYTHON> --config <STAYC_BACKUP_UNKNOWN>",
                          {"<STAYC_BACKUP_PYTHON>": "/x"})


def test_apply_plan_writes_both_then_idempotent(tmp_path):
    plan = _plan(tmp_path)
    first = installer.apply_plan(plan)
    assert first == {"written": 2, "unchanged": 0}
    assert (plan.install_dir / installer.SERVICE_FILENAME).is_file()
    assert (plan.install_dir / installer.TIMER_FILENAME).is_file()
    second = installer.apply_plan(plan)
    assert second == {"written": 0, "unchanged": 2}          # idempotent


def test_per_path_overrides(tmp_path):
    plan = _plan(tmp_path, python=Path("/opt/py"), creds_env=Path("/etc/stayc.env"))
    assert "/opt/py" in plan.service_content and "/etc/stayc.env" in plan.service_content
