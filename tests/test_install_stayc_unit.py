"""STAY-C hardened sovereign systemd SYSTEM unit installer (#42, hardened #67).

Pins the render → build_plan → apply_plan idempotency contract, the SYSTEM-unit
shape (#67 F6: User/Group + multi-user.target + /etc/systemd/system, NO --user),
the STAY-C-venv ExecStart (#67 F1), the optional EnvironmentFile (#67 F2), the
offline STT-model staging check (#67 F3), the config-derived ReadWritePaths
input_dir (#67 F4), the sandbox directive set, the env-scrub parity (against the
single source of truth ``CLOUD_KEY_ENV_VARS``), and the standalone-not-in-fanout
guarantee.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from alfred.scripts import install_stayc_unit as installer
from alfred.sovereign.boundary import CLOUD_KEY_ENV_VARS

#: The shipped config example — deployed on-box to the STAY-C root. Its
#: ``scribe.input_dir`` MUST land under a ReadWritePaths root (see the
#: writability pin below).
_CONFIG_EXAMPLE = Path(__file__).resolve().parents[1] / "config.stayc-clinical.yaml.example"

#: The on-box input_dir + STT model as they appear in the config example —
#: injected into the general helper so path/directive tests don't need to read a
#: live config (the config-read path is exercised separately).
_EXAMPLE_INPUT_DIR = Path("/data/algernon/stayc-clinical/data/inbox")
_EXAMPLE_STT_MODEL = "distil-large-v3"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build(tmp_path: Path, **overrides) -> installer.StaycInstallPlan:
    """Build a plan against the REAL bundled template with a tmp install dir.

    ``input_dir`` + ``stt_model`` are injected so build_plan does NOT read a live
    config (the config-read path is exercised by dedicated tests below)."""
    kwargs = dict(
        stayc_root=Path("/data/algernon/stayc-clinical"),
        install_dir=tmp_path / "systemd",
        unit_user="andrew",
        unit_group="andrew",
        input_dir=_EXAMPLE_INPUT_DIR,
        stt_model=_EXAMPLE_STT_MODEL,
    )
    kwargs.update(overrides)
    return installer.build_plan(**kwargs)


def _directive_lines(unit: str) -> list[str]:
    """Active systemd directives only — drop comments, blanks, [Section] headers.

    So assertions target real directives, not the explanatory comment text
    (which deliberately NAMES algernon.target / MemoryDenyWriteExecute /
    SystemCallFilter / --user to document why they are absent)."""
    out = []
    for raw in unit.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        out.append(line)
    return out


def _readwrite_roots(unit_content: str) -> list[Path]:
    rwp = [l for l in _directive_lines(unit_content) if l.startswith("ReadWritePaths=")]
    assert len(rwp) == 1, "expected exactly one ReadWritePaths directive"
    return [Path(p) for p in rwp[0].split("=", 1)[1].split()]


# ---------------------------------------------------------------------------
# Render sentinel
# ---------------------------------------------------------------------------


def test_render_sentinel_rejects_unsubstituted_placeholder() -> None:
    """A template carrying a placeholder the substitution does not cover raises
    ValueError (the residual ``<UPPER>`` sweep), so a template typo surfaces at
    render time, not as an invalid installed unit."""
    bogus_template = "[Service]\nExecStart=<STAYC_PYTHON>/x --config <STAYC_UNKNOWN>\n"
    with pytest.raises(ValueError, match="placeholder"):
        installer.render_stayc_unit(
            bogus_template,
            unit_user="andrew",
            unit_group="andrew",
            python=Path("/venv/python"),
            workdir=Path("/wd"),
            config_path=Path("/cfg"),
            secrets_env=Path("/sec"),
            hf_home=Path("/hf"),
            vault=Path("/vault"),
            data=Path("/data"),
            input_dir=Path("/inbox"),
        )


def test_render_clean_template_has_no_leftover_placeholders(tmp_path: Path) -> None:
    """The REAL bundled template renders with every placeholder substituted."""
    plan = _build(tmp_path)
    assert "<" not in plan.unit_content or ">" not in plan.unit_content
    # Explicit: none of the known placeholders survive.
    for ph in installer._STAYC_PLACEHOLDERS:
        assert ph not in plan.unit_content


# ---------------------------------------------------------------------------
# 2c — env-scrub parity: UnsetEnvironment token set == CLOUD_KEY_ENV_VARS
# ---------------------------------------------------------------------------


def test_rendered_unit_scrubs_all_cloud_keys_exact_parity(tmp_path: Path) -> None:
    """The UnsetEnvironment token set EXACTLY equals ``set(CLOUD_KEY_ENV_VARS)``
    — the single source of truth in alfred.sovereign.boundary. If boundary.py
    ever adds a 12th cloud key (or renames one), this FAILS and forces the
    template to be updated in lockstep (not a subset check — exact equality)."""
    plan = _build(tmp_path)
    unset_lines = [
        l for l in _directive_lines(plan.unit_content)
        if l.startswith("UnsetEnvironment=")
    ]
    assert len(unset_lines) == 1, "expected exactly one UnsetEnvironment directive"
    tokens = set(unset_lines[0].split("=", 1)[1].split())
    assert tokens == set(CLOUD_KEY_ENV_VARS), (
        "UnsetEnvironment must scrub EXACTLY the boundary's cloud keys — "
        f"missing={set(CLOUD_KEY_ENV_VARS) - tokens} "
        f"extra={tokens - set(CLOUD_KEY_ENV_VARS)}"
    )


# ---------------------------------------------------------------------------
# #67 F6 — SYSTEM unit, not --user
# ---------------------------------------------------------------------------


def test_rendered_unit_is_a_system_unit(tmp_path: Path) -> None:
    """#67 F6: the unit is a SYSTEM unit — it carries ``User=``/``Group=`` (the
    system manager drops to them AFTER applying the hardening), installs to
    multi-user.target, and contains NO ``--user`` / ``default.target`` shape
    that a --user manager (which cannot apply the hardening) would imply."""
    plan = _build(tmp_path)
    directives = _directive_lines(plan.unit_content)

    assert "User=andrew" in directives, "SYSTEM unit must set User= (drop target)"
    assert "Group=andrew" in directives, "SYSTEM unit must set Group="

    wantedby = [l for l in directives if l.startswith("WantedBy=")]
    assert wantedby == ["WantedBy=multi-user.target"], (
        f"a SYSTEM unit installs to multi-user.target; got {wantedby}"
    )

    # No --user manager artefacts leak into an ACTIVE directive (they may still
    # be NAMED in the explanatory comment, which is why we scan directives only).
    for line in directives:
        assert "--user" not in line, f"active directive names --user: {line!r}"
        assert "default.target" not in line, (
            f"a SYSTEM unit must not target default.target: {line!r}"
        )


def test_install_dir_is_the_system_dir() -> None:
    """#67 F6: the default install dir is /etc/systemd/system (a --user dir
    cannot apply this unit's hardening)."""
    assert installer.get_install_dir() == Path("/etc/systemd/system")
    assert installer.SYSTEM_INSTALL_DIR == Path("/etc/systemd/system")


def test_no_linger_logic_remains() -> None:
    """#67 F6: lingering is a --user concept — the linger check/enable helpers
    are removed for a SYSTEM unit (no silent leftover)."""
    assert not hasattr(installer, "_check_linger")
    assert not hasattr(installer, "_enable_linger")


# ---------------------------------------------------------------------------
# Sandbox directive set
# ---------------------------------------------------------------------------


def test_rendered_unit_sandbox_directives(tmp_path: Path) -> None:
    """The hardening directive set is present, and the two DELIBERATELY-OMITTED
    directives (MemoryDenyWriteExecute, SystemCallFilter) are ABSENT as active
    directives (they may appear in the explanatory comment)."""
    plan = _build(tmp_path)
    directives = _directive_lines(plan.unit_content)

    # Present, exact.
    for expected in (
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "UMask=0077",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "IPAddressAllow=localhost",
        "IPAddressDeny=any",
        "RestartPreventExitStatus=78 79",
        "ReadOnlyPaths=/data/algernon/stayc-clinical/config.stayc-clinical.yaml",
    ):
        assert expected in directives, f"missing directive: {expected!r}"

    # ReadWritePaths covers vault + data + HF cache + input_dir (single directive).
    rwp = [l for l in directives if l.startswith("ReadWritePaths=")]
    assert len(rwp) == 1
    for needed in (
        "/data/algernon/stayc-clinical/vault",
        "/data/algernon/stayc-clinical/data",
        "/data/algernon/stayc-clinical/models/hf",
        "/data/algernon/stayc-clinical/data/inbox",
    ):
        assert needed in rwp[0], f"ReadWritePaths missing {needed}"

    # Offline HF env belt.
    assert "Environment=HF_HOME=/data/algernon/stayc-clinical/models/hf" in directives
    assert "Environment=HF_HUB_OFFLINE=1" in directives
    assert "Environment=TRANSFORMERS_OFFLINE=1" in directives

    # DELIBERATELY OMITTED as active directives (would break real-need iii).
    assert not any(l.startswith("MemoryDenyWriteExecute") for l in directives), (
        "MemoryDenyWriteExecute must NOT be an active directive (kills STT W+X JIT)"
    )
    assert not any(l.startswith("SystemCallFilter") for l in directives), (
        "SystemCallFilter must NOT be an active directive (risks killing native STT)"
    )


# ---------------------------------------------------------------------------
# #67 F1 — ExecStart uses the STAY-C venv, WorkingDirectory is the stayc root
# ---------------------------------------------------------------------------


def test_execstart_uses_stayc_venv_not_shared(tmp_path: Path) -> None:
    """#67 F1: the ExecStart python is the STAY-C OWN venv (where faster-whisper
    lives) + WorkingDirectory is the stayc root — decoupled from the shared
    alfred repo/venv (which has no faster-whisper → STT import fails)."""
    plan = _build(tmp_path)
    directives = _directive_lines(plan.unit_content)

    execstart = [l for l in directives if l.startswith("ExecStart=")]
    assert len(execstart) == 1
    assert "--_internal-foreground" in execstart[0]
    assert "--config /data/algernon/stayc-clinical/config.stayc-clinical.yaml" in execstart[0]
    # The STAY-C venv python — NOT the shared repo venv.
    assert "ExecStart=/data/algernon/stayc-clinical/.venv/bin/python " in execstart[0]
    assert "/home/andrew/alfred/.venv" not in plan.unit_content, (
        "ExecStart must not point at the shared alfred venv (no faster-whisper)"
    )

    workdir = [l for l in directives if l.startswith("WorkingDirectory=")]
    assert workdir == ["WorkingDirectory=/data/algernon/stayc-clinical"]
    assert plan.python == Path("/data/algernon/stayc-clinical/.venv/bin/python")
    assert plan.workdir == Path("/data/algernon/stayc-clinical")


def test_python_override_wins(tmp_path: Path) -> None:
    """A --python override (build_plan ``python=``) replaces the venv default."""
    plan = _build(tmp_path, python=Path("/opt/stayc-venv/bin/python"))
    assert plan.python == Path("/opt/stayc-venv/bin/python")
    assert "ExecStart=/opt/stayc-venv/bin/python " in plan.unit_content


# ---------------------------------------------------------------------------
# #67 F2 — EnvironmentFile is OPTIONAL (leading dash)
# ---------------------------------------------------------------------------


def test_environmentfile_is_optional(tmp_path: Path) -> None:
    """#67 F2: EnvironmentFile carries a leading ``-`` so a missing salt file is
    ignored, not fatal (the salt is usually inline in config)."""
    plan = _build(tmp_path)
    directives = _directive_lines(plan.unit_content)
    envfile = [l for l in directives if l.startswith("EnvironmentFile=")]
    assert len(envfile) == 1
    assert envfile[0] == "EnvironmentFile=-/data/algernon/stayc-clinical/secrets/scribe.env", (
        f"EnvironmentFile must be optional (leading dash); got {envfile[0]!r}"
    )


# ---------------------------------------------------------------------------
# Standalone — NOT in the algernon fan-out
# ---------------------------------------------------------------------------


def test_unit_is_standalone_not_in_fanout(tmp_path: Path) -> None:
    """No PartOf= directive, no WantedBy=algernon.target — the ONLY WantedBy is
    multi-user.target. (algernon.target appears only in the doc comment.)"""
    plan = _build(tmp_path)
    directives = _directive_lines(plan.unit_content)

    assert not any(l.startswith("PartOf=") for l in directives), (
        "STAY-C must not be PartOf= any target (standalone lifecycle)"
    )
    wantedby = [l for l in directives if l.startswith("WantedBy=")]
    assert wantedby == ["WantedBy=multi-user.target"], (
        f"the only install target must be multi-user.target; got {wantedby}"
    )


# ---------------------------------------------------------------------------
# Path derivation + overrides
# ---------------------------------------------------------------------------


def test_paths_derive_from_stayc_root(tmp_path: Path) -> None:
    """All deploy paths derive from a single stayc_root by default."""
    plan = _build(tmp_path, stayc_root=Path("/srv/box"))
    assert plan.config_path == Path("/srv/box/config.stayc-clinical.yaml")
    assert plan.secrets_env == Path("/srv/box/secrets/scribe.env")
    assert plan.hf_home == Path("/srv/box/models/hf")
    assert plan.vault == Path("/srv/box/vault")
    assert plan.data == Path("/srv/box/data")
    assert plan.python == Path("/srv/box/.venv/bin/python")
    assert plan.workdir == Path("/srv/box")


def test_per_path_override_wins(tmp_path: Path) -> None:
    """A per-path flag overrides the stayc_root-derived default."""
    plan = _build(tmp_path, hf_home=Path("/mnt/fastdisk/hf"))
    assert plan.hf_home == Path("/mnt/fastdisk/hf")
    assert "ReadWritePaths=" in plan.unit_content
    assert "/mnt/fastdisk/hf" in plan.unit_content


# ---------------------------------------------------------------------------
# #67 F4 — ReadWritePaths includes config.scribe.input_dir (DERIVED, not assumed)
# ---------------------------------------------------------------------------


def test_build_plan_reads_input_dir_from_config_into_readwrite(tmp_path: Path) -> None:
    """#67 F4: build_plan READS scribe.input_dir from the deployed config and
    puts it in ReadWritePaths — even when it is a SIBLING of the data dir (the
    exact on-box shape that EROFS-broke every encounter during #60). The layout
    is DERIVED from the config, never assumed."""
    cfg = tmp_path / "config.stayc-clinical.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "scribe": {
                    # SIBLING of data, NOT under it — the #60 break.
                    "input_dir": "/data/algernon/stayc-clinical/inbox",
                    "stt": {"model": "distil-large-v3"},
                }
            }
        ),
        encoding="utf-8",
    )
    plan = installer.build_plan(
        stayc_root=Path("/data/algernon/stayc-clinical"),
        install_dir=tmp_path / "systemd",
        unit_user="andrew",
        unit_group="andrew",
        config_path=cfg,
    )
    assert plan.input_dir == Path("/data/algernon/stayc-clinical/inbox")
    assert plan.stt_model == "distil-large-v3"
    roots = _readwrite_roots(plan.unit_content)
    assert Path("/data/algernon/stayc-clinical/inbox") in roots, (
        f"the sibling input_dir must be a ReadWritePaths root; got {roots}"
    )


def test_build_plan_missing_config_fails_loud(tmp_path: Path) -> None:
    """#67 F3/F4: no deployed config + no explicit input_dir/stt_model → fail
    LOUD (never silently assume the layout)."""
    with pytest.raises(FileNotFoundError, match="scribe.input_dir"):
        installer.build_plan(
            stayc_root=Path("/data/algernon/stayc-clinical"),
            install_dir=tmp_path / "systemd",
            unit_user="andrew",
            unit_group="andrew",
            config_path=tmp_path / "does-not-exist.yaml",
        )


def test_build_plan_config_missing_input_dir_fails_loud(tmp_path: Path) -> None:
    """A config present but without scribe.input_dir fails loud (F4)."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({"scribe": {"stt": {"model": "distil-large-v3"}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="scribe.input_dir"):
        installer.build_plan(
            stayc_root=Path("/data/algernon/stayc-clinical"),
            install_dir=tmp_path / "systemd",
            unit_user="andrew",
            unit_group="andrew",
            config_path=cfg,
        )


def test_config_example_input_dir_is_under_a_readwrite_root(tmp_path: Path) -> None:
    """B1 regression guard: the config example's ``scribe.input_dir`` MUST
    resolve UNDER one of the unit's ReadWritePaths roots. ProtectSystem=strict
    makes everything else read-only, so an inbox outside the writable tree would
    be EROFS → save_ledger mkdir/write fails → every encounter silently never
    folds (the daemon boots idle-fine, so `systemctl start` looks healthy while
    nothing works). Built FROM the real config example (exercises the read)."""
    raw = yaml.safe_load(_CONFIG_EXAMPLE.read_text(encoding="utf-8"))
    input_dir = Path(raw["scribe"]["input_dir"])

    plan = installer.build_plan(
        stayc_root=installer.DEFAULT_STAYC_ROOT,
        install_dir=tmp_path / "systemd",
        unit_user="andrew",
        unit_group="andrew",
        config_path=_CONFIG_EXAMPLE,
    )
    assert plan.input_dir == input_dir
    roots = _readwrite_roots(plan.unit_content)
    assert any(input_dir == r or input_dir.is_relative_to(r) for r in roots), (
        f"scribe.input_dir {input_dir} is NOT under any ReadWritePaths root "
        f"{roots} — ProtectSystem=strict would make it read-only and every "
        f"encounter would fail EROFS."
    )


# ---------------------------------------------------------------------------
# #67 F3 — offline STT-model staging / verify
# ---------------------------------------------------------------------------


def test_hf_model_cache_dirname_maps_distil_and_plain() -> None:
    """Bare faster-whisper ids map to their Systran HF hub cache dir; explicit
    repo/path ids are operator-managed (None)."""
    assert (
        installer.hf_model_cache_dirname("distil-large-v3")
        == "models--Systran--faster-distil-whisper-large-v3"
    )
    assert (
        installer.hf_model_cache_dirname("large-v3")
        == "models--Systran--faster-whisper-large-v3"
    )
    assert installer.hf_model_cache_dirname("base") == "models--Systran--faster-whisper-base"
    # Explicit repo id or filesystem path → None (operator manages that cache).
    assert installer.hf_model_cache_dirname("Systran/faster-whisper-large-v3") is None
    assert installer.hf_model_cache_dirname("/models/local-whisper") is None


def test_model_cache_target_under_hf_home(tmp_path: Path) -> None:
    plan = _build(tmp_path, hf_home=tmp_path / "hf")
    target = installer.model_cache_target(plan)
    assert target == tmp_path / "hf" / "hub" / "models--Systran--faster-distil-whisper-large-v3"


def test_verify_model_present(tmp_path: Path) -> None:
    """A model already at the relocated offline cache verifies 'present'."""
    plan = _build(tmp_path, hf_home=tmp_path / "hf")
    installer.model_cache_target(plan).mkdir(parents=True)
    status = installer.verify_or_stage_model(plan, stage=False)
    assert status["status"] == "present"


def test_verify_model_missing_fails_loud(tmp_path: Path) -> None:
    """#67 F3: a model missing at the offline cache (with no staging) fails LOUD
    so the daemon never boots into a silent model-not-found STT failure."""
    plan = _build(tmp_path, hf_home=tmp_path / "hf")  # empty HF home → missing
    with pytest.raises(RuntimeError, match="NOT present at the offline HF cache"):
        installer.verify_or_stage_model(plan, stage=False)


def test_verify_model_stage_copies_from_source(tmp_path: Path) -> None:
    """#67 F3: --stage-model copies the model from the operator's default cache
    to the relocated offline cache."""
    plan = _build(tmp_path, hf_home=tmp_path / "hf")
    src_cache = tmp_path / "src"
    src_model = src_cache / installer.model_cache_target(plan).name
    src_model.mkdir(parents=True)
    (src_model / "model.bin").write_text("weights", encoding="utf-8")

    status = installer.verify_or_stage_model(plan, stage=True, source_cache=src_cache)
    assert status["status"] == "staged"
    target = installer.model_cache_target(plan)
    assert target.is_dir()
    assert (target / "model.bin").read_text(encoding="utf-8") == "weights"


def test_verify_model_dry_run_would_stage_but_copies_nothing(tmp_path: Path) -> None:
    """dry_run reports 'would-stage' without mutating the filesystem."""
    plan = _build(tmp_path, hf_home=tmp_path / "hf")
    src_cache = tmp_path / "src"
    (src_cache / installer.model_cache_target(plan).name).mkdir(parents=True)

    status = installer.verify_or_stage_model(
        plan, stage=True, source_cache=src_cache, dry_run=True
    )
    assert status["status"] == "would-stage"
    assert not installer.model_cache_target(plan).exists(), "dry-run must not copy"


def test_verify_model_skipped_for_explicit_repo_or_path(tmp_path: Path) -> None:
    """A model given as a path/explicit repo is operator-managed → skipped, not
    a hard failure."""
    plan = _build(tmp_path, stt_model="/models/local-whisper", hf_home=tmp_path / "hf")
    status = installer.verify_or_stage_model(plan, stage=False)
    assert status["status"] == "skipped"


# ---------------------------------------------------------------------------
# apply_plan — idempotent write-if-changed
# ---------------------------------------------------------------------------


def test_apply_plan_writes_then_is_idempotent(tmp_path: Path) -> None:
    """First apply writes the unit; a second apply with unchanged inputs writes
    nothing (idempotent write-if-changed)."""
    plan = _build(tmp_path)

    first = installer.apply_plan(plan)
    assert first == {"written": 1, "unchanged": 0}
    unit_path = plan.install_dir / installer.STAYC_UNIT_FILENAME
    assert unit_path.is_file()
    assert unit_path.read_text(encoding="utf-8") == plan.unit_content

    second = installer.apply_plan(plan)
    assert second == {"written": 0, "unchanged": 1}, "re-apply must not rewrite"


def test_apply_plan_rewrites_on_change(tmp_path: Path) -> None:
    """A changed render (different stayc_root) rewrites the unit."""
    plan1 = _build(tmp_path)
    installer.apply_plan(plan1)
    plan2 = _build(tmp_path, stayc_root=Path("/srv/other"))
    result = installer.apply_plan(plan2)
    assert result == {"written": 1, "unchanged": 0}


# ---------------------------------------------------------------------------
# main() — dry-run renders from the real config (SYSTEM unit, model-skip)
# ---------------------------------------------------------------------------


def test_main_dry_run_renders_from_real_config(tmp_path: Path, capsys) -> None:
    """main() reads the real config (input_dir/model), renders the SYSTEM unit,
    and returns 0 in dry-run without writing or calling systemctl."""
    rc = installer.main([
        "--config", str(_CONFIG_EXAMPLE),
        "--unit-user", "andrew",
        "--unit-group", "andrew",
        "--install-dir", str(tmp_path / "sysd"),
        "--skip-model-check",
        "--dry-run",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "SYSTEM unit" in out
    assert not (tmp_path / "sysd").exists(), "dry-run must not write the unit"


# ---------------------------------------------------------------------------
# #67 cr-67a — unit-user resolution + root/privilege-drop guards
# ---------------------------------------------------------------------------


def test_default_unit_user_prefers_sudo_user(monkeypatch) -> None:
    """Under sudo, $SUDO_USER (the invoking operator) wins over $USER=root."""
    monkeypatch.setenv("SUDO_USER", "andrew")
    monkeypatch.setenv("USER", "root")
    assert installer._default_unit_user() == "andrew"


def test_default_unit_user_root_direct_resolves_root(monkeypatch) -> None:
    """A root-DIRECT install (no sudo → $SUDO_USER unset, $USER=root) resolves to
    'root' — which main() then rejects (see below)."""
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.setenv("USER", "root")
    monkeypatch.delenv("LOGNAME", raising=False)
    assert installer._default_unit_user() == "root"


def test_default_unit_user_all_unset_is_empty(monkeypatch) -> None:
    """All identity env unset → '' (main() then errors, telling the operator to
    pass --unit-user)."""
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    assert installer._default_unit_user() == ""


def test_main_rejects_resolved_root_user(tmp_path: Path, capsys) -> None:
    """cr-67a: refuse to render User=root (running the PHI scribe as root defeats
    the systemd privilege drop). Explicit --unit-user root is rejected too."""
    rc = installer.main([
        "--config", str(_CONFIG_EXAMPLE),
        "--unit-user", "root",
        "--install-dir", str(tmp_path / "sysd"),
        "--skip-model-check",
        "--dry-run",
    ])
    assert rc == 2
    assert "User=root" in capsys.readouterr().err


def test_main_all_identity_unset_errors(tmp_path: Path, monkeypatch, capsys) -> None:
    """No --unit-user and no identity env → main() errors (return 2) rather than
    rendering an empty User=."""
    monkeypatch.delenv("SUDO_USER", raising=False)
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)
    rc = installer.main([
        "--config", str(_CONFIG_EXAMPLE),
        "--install-dir", str(tmp_path / "sysd"),
        "--skip-model-check",
        "--dry-run",
    ])
    assert rc == 2
    assert "cannot determine the unit user" in capsys.readouterr().err


def test_main_root_gate_requires_euid_zero(tmp_path: Path, monkeypatch, capsys) -> None:
    """cr-67a: writing the unit / driving systemctl needs root — a non-root euid
    fails loud (return 2) with the sudo hint, before writing anything."""
    monkeypatch.setattr(installer, "_geteuid", lambda: 1000)
    sysd = tmp_path / "sysd"
    rc = installer.main([
        "--config", str(_CONFIG_EXAMPLE),
        "--unit-user", "andrew",
        "--unit-group", "andrew",
        "--install-dir", str(sysd),  # non-system dir, but --skip-systemctl absent → needs root
        "--skip-model-check",
    ])
    assert rc == 2
    assert "requires root" in capsys.readouterr().err
    assert not sysd.exists(), "must not write the unit when the root gate fails"
