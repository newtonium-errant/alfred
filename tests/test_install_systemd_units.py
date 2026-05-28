"""Tests for the Algernon systemd-units installer (2026-05-29 autonomy ship).

Covers:
  * Template substitution — render_service_unit replaces all three
    placeholders + raises on leftovers
  * Target rendering — render_algernon_target injects the per-instance
    Wants= line + handles the empty-registry sentinel
  * build_plan end-to-end — given a fake registry, renders every
    unit file
  * apply_plan idempotency — re-running writes the same content on the
    second pass (unchanged counter increments)
  * CLI dispatch — main(['--dry-run']) returns 0 without writes;
    main(['--registry', <bad>]) returns 2
  * Linger check — mocked loginctl returning ``Linger=yes`` short-circuits

Test surface lives at ``tests/test_install_systemd_units.py`` to match
the existing migration-script test convention (siblings:
``tests/test_migrate_tier_phase1.py``, ``tests/test_migrate_meditations_zettels.py``).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from alfred.instance_set import Instance
from alfred.scripts import install_systemd_units as installer
from alfred.scripts.install_systemd_units import (
    apply_plan,
    build_plan,
    get_install_dir,
    main,
    render_algernon_target,
    render_service_unit,
)


# ---------------------------------------------------------------------------
# Registry fixtures
# ---------------------------------------------------------------------------


def _write_registry(path: Path, instances: list[dict]) -> None:
    """Write a registry YAML file. Mirrors the helper in
    ``tests/test_instance_set.py`` so test fixtures stay consistent."""
    import yaml
    path.write_text(
        yaml.safe_dump({"instances": instances}, sort_keys=False),
        encoding="utf-8",
    )


def _three_instance_registry(tmp_path: Path) -> Path:
    """Write a Salem/KAL-LE/Hypatia registry. Same shape as the
    canonical Phase 1 starter."""
    reg = tmp_path / "instances.yaml"
    _write_registry(reg, [
        {"name": "salem", "display": "Salem",
         "config": "/path/to/config.yaml", "enabled": True},
        {"name": "kal-le", "display": "KAL-LE",
         "config": "/path/to/config.kalle.yaml", "enabled": True},
        {"name": "hypatia", "display": "Hypatia",
         "config": "/path/to/config.hypatia.yaml", "enabled": True},
    ])
    return reg


# ---------------------------------------------------------------------------
# render_service_unit — placeholder substitution
# ---------------------------------------------------------------------------


def test_render_service_unit_substitutes_all_placeholders() -> None:
    """All three placeholders (<DISPLAY>, <ALFRED_REPO>, <CONFIG_PATH>)
    are replaced + the rendered output carries operator-readable
    values."""
    template = (
        "Description=Algernon (<DISPLAY>)\n"
        "WorkingDirectory=<ALFRED_REPO>\n"
        "ExecStart=<ALFRED_REPO>/.venv/bin/python -m alfred "
        "--config <CONFIG_PATH> up\n"
    )
    inst = Instance(
        name="salem",
        display="Salem",
        config="/home/andrew/alfred/config.yaml",
        enabled=True,
    )
    out = render_service_unit(
        template, inst, Path("/home/andrew/alfred"),
    )
    assert "<DISPLAY>" not in out
    assert "<ALFRED_REPO>" not in out
    assert "<CONFIG_PATH>" not in out
    assert "Algernon (Salem)" in out
    assert "WorkingDirectory=/home/andrew/alfred" in out
    assert "--config /home/andrew/alfred/config.yaml" in out


def test_render_service_unit_raises_on_unsubstituted_placeholder() -> None:
    """If a future template adds a placeholder without updating the
    substitution dict, ``render_service_unit`` raises ValueError. Pin
    so the sentinel check stays load-bearing.

    Simulated by patching ``_SERVICE_PLACEHOLDERS`` to include an extra
    placeholder name that the render function's ``.replace`` chain
    never touches — the leftover-check at the end of
    ``render_service_unit`` then catches it.
    """
    extra_placeholders = installer._SERVICE_PLACEHOLDERS | {"<UNTOUCHED>"}
    template_with_extra = (
        "Description=<DISPLAY> <UNTOUCHED>\n"
        "WorkingDirectory=<ALFRED_REPO>\n"
        "Config=<CONFIG_PATH>\n"
    )
    inst = Instance(
        name="salem", display="Salem",
        config="/x.yaml", enabled=True,
    )
    with patch.object(
        installer, "_SERVICE_PLACEHOLDERS", extra_placeholders,
    ):
        with pytest.raises(ValueError, match="placeholders after substitution"):
            render_service_unit(
                template_with_extra, inst, Path("/repo"),
            )


def test_render_service_unit_preserves_systemd_directives() -> None:
    """The shipped bundled template has the canonical restart policy +
    Type=simple + RestartPreventExitStatus=78 lines. Render preserves
    them — pinned so a future refactor that breaks the template
    syntax surfaces here."""
    from alfred._data import get_systemd_dir
    template_path = get_systemd_dir() / "alfred-instance.service.template"
    template = template_path.read_text(encoding="utf-8")
    inst = Instance(
        name="salem", display="Salem",
        config="./config.yaml", enabled=True,
    )
    out = render_service_unit(template, inst, Path("/home/andrew/alfred"))
    # Canonical lines from the dispatch's ratified defaults.
    assert "Type=simple" in out
    assert "Restart=on-failure" in out
    assert "RestartSec=30s" in out
    assert "StartLimitIntervalSec=300" in out
    assert "StartLimitBurst=5" in out
    assert "RestartPreventExitStatus=78" in out
    assert "WantedBy=default.target algernon.target" in out
    assert "PartOf=algernon.target" in out


# ---------------------------------------------------------------------------
# render_algernon_target
# ---------------------------------------------------------------------------


def test_render_algernon_target_injects_wants_line() -> None:
    """The per-instance Wants= line carries one service per enabled
    instance in registry order."""
    template = (
        "[Unit]\n"
        "Description=Algernon platform\n"
        "<WANTS_LINE>\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    enabled = [
        Instance(name="salem", display="Salem",
                 config="/x.yaml", enabled=True),
        Instance(name="kal-le", display="KAL-LE",
                 config="/y.yaml", enabled=True),
    ]
    out = render_algernon_target(template, enabled)
    assert "<WANTS_LINE>" not in out
    assert (
        "Wants=alfred-salem.service alfred-kal-le.service" in out
    )


def test_render_algernon_target_empty_registry_emits_sentinel() -> None:
    """Empty enabled-list → sentinel comment (intentionally-left-blank
    discipline). Target file stays syntactically valid (systemd
    permits a target with no Wants)."""
    template = "Description=x\n<WANTS_LINE>\n"
    out = render_algernon_target(template, [])
    assert "<WANTS_LINE>" not in out
    assert "no enabled instances" in out


def test_render_algernon_target_raises_without_placeholder() -> None:
    """A future template refactor that drops the placeholder while
    keeping the file structure surfaces here."""
    template_no_placeholder = "[Unit]\nDescription=x\n"
    with pytest.raises(ValueError, match="missing the.*placeholder"):
        render_algernon_target(
            template_no_placeholder,
            [Instance(name="s", display="S", config="/x", enabled=True)],
        )


# ---------------------------------------------------------------------------
# build_plan + apply_plan end-to-end
# ---------------------------------------------------------------------------


def test_build_plan_three_instances_renders_three_services_plus_target(
    tmp_path: Path,
) -> None:
    """End-to-end: 3-instance registry → 3 service unit renders + 1
    target render. Filenames use lowercase slug per the dispatch
    ratification."""
    reg = _three_instance_registry(tmp_path)
    install_dir = tmp_path / "systemd_user"
    alfred_repo = tmp_path / "alfred_repo"
    alfred_repo.mkdir()

    plan = build_plan(reg, alfred_repo, install_dir)

    assert len(plan.enabled_instances) == 3
    assert set(plan.service_files.keys()) == {
        "alfred-salem.service",
        "alfred-kal-le.service",
        "alfred-hypatia.service",
    }
    # Wants line carries all three in registry order.
    assert (
        "Wants=alfred-salem.service alfred-kal-le.service "
        "alfred-hypatia.service" in plan.target_file
    )


def test_build_plan_disabled_instance_excluded(tmp_path: Path) -> None:
    """``enabled: false`` keeps an instance out of both the service-
    file set AND the target's Wants= line."""
    reg = tmp_path / "instances.yaml"
    _write_registry(reg, [
        {"name": "salem", "display": "Salem",
         "config": "/a.yaml", "enabled": True},
        {"name": "drained", "display": "Drained",
         "config": "/d.yaml", "enabled": False},
    ])
    plan = build_plan(reg, tmp_path / "repo", tmp_path / "sys")
    assert set(plan.service_files.keys()) == {"alfred-salem.service"}
    assert "alfred-drained.service" not in plan.target_file


def test_apply_plan_writes_all_files_first_pass(tmp_path: Path) -> None:
    """First-pass apply writes every file + zero unchanged."""
    reg = _three_instance_registry(tmp_path)
    install_dir = tmp_path / "systemd_user"
    plan = build_plan(reg, tmp_path / "repo", install_dir)
    counters = apply_plan(plan)
    assert counters["services_written"] == 3
    assert counters["target_written"] == 1
    assert counters["unchanged"] == 0

    # All four files exist on disk.
    assert (install_dir / "alfred-salem.service").is_file()
    assert (install_dir / "alfred-kal-le.service").is_file()
    assert (install_dir / "alfred-hypatia.service").is_file()
    assert (install_dir / "algernon.target").is_file()


def test_apply_plan_idempotent_second_pass(tmp_path: Path) -> None:
    """Re-running the installer with the same registry produces zero
    writes on the second pass. Pin per the dispatch's 'idempotent
    re-run, no diff' contract."""
    reg = _three_instance_registry(tmp_path)
    install_dir = tmp_path / "systemd_user"
    plan = build_plan(reg, tmp_path / "repo", install_dir)
    apply_plan(plan)  # first pass

    counters2 = apply_plan(plan)
    assert counters2["services_written"] == 0
    assert counters2["target_written"] == 0
    # All 4 (3 services + 1 target) are unchanged.
    assert counters2["unchanged"] == 4


def test_apply_plan_rewrites_on_registry_change(tmp_path: Path) -> None:
    """After a registry change (e.g. operator added an instance),
    re-running the installer refreshes the changed files. The
    unchanged files stay unchanged.

    Defensive against a regression where the idempotency check fires
    too aggressively and misses a real diff."""
    reg = _three_instance_registry(tmp_path)
    install_dir = tmp_path / "systemd_user"
    plan_v1 = build_plan(reg, tmp_path / "repo", install_dir)
    apply_plan(plan_v1)

    # Mutate the registry: drain KAL-LE.
    _write_registry(reg, [
        {"name": "salem", "display": "Salem",
         "config": "/path/to/config.yaml", "enabled": True},
        {"name": "kal-le", "display": "KAL-LE",
         "config": "/path/to/config.kalle.yaml", "enabled": False},
        {"name": "hypatia", "display": "Hypatia",
         "config": "/path/to/config.hypatia.yaml", "enabled": True},
    ])
    plan_v2 = build_plan(reg, tmp_path / "repo", install_dir)
    counters = apply_plan(plan_v2)

    # KAL-LE service file is no longer generated (enabled=False); the
    # apply_plan's service_files dict only has 2 services to write —
    # both already on disk from v1 → 0 services_written, 2 unchanged.
    # The target file changed (KAL-LE dropped from Wants=) → 1 target_written.
    assert counters["services_written"] == 0
    assert counters["target_written"] == 1
    assert counters["unchanged"] == 2


# ---------------------------------------------------------------------------
# Linger check — mocked loginctl
# ---------------------------------------------------------------------------


def test_check_linger_returns_true_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock ``loginctl show-user`` to emit ``Linger=yes`` → check
    returns True. Pin so a future refactor that parses other
    delimiters surfaces here."""
    class _FakeProc:
        returncode = 0
        stdout = (
            "UID=1000\nGID=1000\nName=andrew\n"
            "Timestamp=...\nTimestampMonotonic=...\n"
            "RuntimePath=...\nService=...\n"
            "Slice=user-1000.slice\n"
            "Display=...\nState=active\n"
            "Sessions=...\n"
            "IdleHint=no\n"
            "IdleSinceHint=...\n"
            "IdleSinceHintMonotonic=...\n"
            "Linger=yes\n"
        )
        stderr = ""

    monkeypatch.setattr(installer.shutil, "which", lambda x: "/usr/bin/loginctl")
    monkeypatch.setattr(
        installer.subprocess, "run", lambda *a, **kw: _FakeProc(),
    )
    assert installer._check_linger("andrew") is True


def test_check_linger_returns_false_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Linger=no`` → check returns False (the installer offers to
    enable it)."""
    class _FakeProc:
        returncode = 0
        stdout = (
            "UID=1000\nName=andrew\n"
            "Linger=no\n"
        )
        stderr = ""

    monkeypatch.setattr(installer.shutil, "which", lambda x: "/usr/bin/loginctl")
    monkeypatch.setattr(
        installer.subprocess, "run", lambda *a, **kw: _FakeProc(),
    )
    assert installer._check_linger("andrew") is False


def test_check_linger_returns_false_when_loginctl_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``loginctl`` not in PATH → defensive return False (don't crash
    the installer)."""
    monkeypatch.setattr(installer.shutil, "which", lambda x: None)
    assert installer._check_linger("andrew") is False


def test_check_linger_returns_false_when_loginctl_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User unknown / loginctl error → defensive return False."""
    class _FakeProc:
        returncode = 1
        stdout = ""
        stderr = "no such user"

    monkeypatch.setattr(installer.shutil, "which", lambda x: "/usr/bin/loginctl")
    monkeypatch.setattr(
        installer.subprocess, "run", lambda *a, **kw: _FakeProc(),
    )
    assert installer._check_linger("ghost") is False


# ---------------------------------------------------------------------------
# CLI dispatch — main(...) exit codes
# ---------------------------------------------------------------------------


def test_main_dry_run_returns_zero_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """``--dry-run`` prints the plan + returns 0 without writing any
    files or calling systemctl."""
    reg = _three_instance_registry(tmp_path)
    install_dir = tmp_path / "systemd_user"
    alfred_repo = tmp_path / "alfred_repo"
    alfred_repo.mkdir()

    rc = main([
        "--registry", str(reg),
        "--alfred-repo", str(alfred_repo),
        "--install-dir", str(install_dir),
        "--dry-run",
    ])
    assert rc == 0
    # No files written.
    assert not install_dir.exists() or not any(install_dir.iterdir())
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    # Each instance surfaces in the plan output.
    assert "Salem" in out
    assert "KAL-LE" in out
    assert "Hypatia" in out


def test_main_missing_registry_returns_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    """Missing registry → exit 2 + actionable error to stderr."""
    rc = main([
        "--registry", str(tmp_path / "does-not-exist.yaml"),
        "--dry-run",
    ])
    assert rc == 2
    err = capsys.readouterr().err
    assert "not found" in err.lower() or "error" in err.lower()


def test_main_live_run_writes_files_and_skips_systemctl(
    tmp_path: Path, capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live run with ``--skip-linger --skip-systemctl`` writes the
    unit files without invoking sudo / systemctl. Useful for test
    environments where neither is available."""
    reg = _three_instance_registry(tmp_path)
    install_dir = tmp_path / "systemd_user"
    alfred_repo = tmp_path / "alfred_repo"
    alfred_repo.mkdir()

    rc = main([
        "--registry", str(reg),
        "--alfred-repo", str(alfred_repo),
        "--install-dir", str(install_dir),
        "--skip-linger",
        "--skip-systemctl",
    ])
    assert rc == 0
    assert (install_dir / "alfred-salem.service").is_file()
    assert (install_dir / "alfred-kal-le.service").is_file()
    assert (install_dir / "alfred-hypatia.service").is_file()
    assert (install_dir / "algernon.target").is_file()


# ---------------------------------------------------------------------------
# Install-dir resolution
# ---------------------------------------------------------------------------


def test_get_install_dir_default_uses_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``XDG_CONFIG_HOME`` override resolves correctly."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "custom-xdg"))
    out = get_install_dir()
    assert str(out) == str(tmp_path / "custom-xdg" / "systemd" / "user")


def test_get_install_dir_default_falls_back_to_home_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No XDG override → ``~/.config/systemd/user``."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    out = get_install_dir()
    assert out == Path.home() / ".config" / "systemd" / "user"


# ---------------------------------------------------------------------------
# Bundled-path locator (verifies the wheel ships the templates)
# ---------------------------------------------------------------------------


def test_bundled_systemd_dir_contains_templates() -> None:
    """Verify the bundled templates ship via importlib.resources. A
    future packaging refactor that drops the systemd/ dir from the
    wheel surfaces here."""
    from alfred._data import get_systemd_dir
    systemd_dir = get_systemd_dir()
    assert systemd_dir.is_dir()
    assert (systemd_dir / "alfred-instance.service.template").is_file()
    assert (systemd_dir / "algernon.target").is_file()
