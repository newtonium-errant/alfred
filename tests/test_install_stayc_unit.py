"""STAY-C hardened sovereign systemd unit installer (#42).

Pins the render → build_plan → apply_plan idempotency contract, the sandbox
directive set, the env-scrub parity (against the single source of truth
``CLOUD_KEY_ENV_VARS``), and the standalone-not-in-fanout guarantee.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.scripts import install_stayc_unit as installer
from alfred.sovereign.boundary import CLOUD_KEY_ENV_VARS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build(tmp_path: Path, **overrides) -> installer.StaycInstallPlan:
    """Build a plan against the REAL bundled template with a tmp install dir."""
    kwargs = dict(
        alfred_repo=Path("/home/andrew/alfred"),
        stayc_root=Path("/data/algernon/stayc-clinical"),
        install_dir=tmp_path / "systemd",
    )
    kwargs.update(overrides)
    return installer.build_plan(**kwargs)


def _directive_lines(unit: str) -> list[str]:
    """Active systemd directives only — drop comments, blanks, [Section] headers.

    So assertions target real directives, not the explanatory comment text
    (which deliberately NAMES algernon.target / MemoryDenyWriteExecute /
    SystemCallFilter to document why they are absent)."""
    out = []
    for raw in unit.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        out.append(line)
    return out


# ---------------------------------------------------------------------------
# Render sentinel
# ---------------------------------------------------------------------------


def test_render_sentinel_rejects_unsubstituted_placeholder() -> None:
    """A template carrying a placeholder the substitution does not cover raises
    ValueError (the residual ``<UPPER>`` sweep), so a template typo surfaces at
    render time, not as an invalid installed unit."""
    bogus_template = "[Service]\nExecStart=<ALFRED_REPO>/x --config <STAYC_UNKNOWN>\n"
    with pytest.raises(ValueError, match="placeholder"):
        installer.render_stayc_unit(
            bogus_template,
            alfred_repo=Path("/repo"),
            config_path=Path("/cfg"),
            secrets_env=Path("/sec"),
            hf_home=Path("/hf"),
            vault=Path("/vault"),
            data=Path("/data"),
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

    # ReadWritePaths covers vault + data + HF cache (single directive, 3 paths).
    rwp = [l for l in directives if l.startswith("ReadWritePaths=")]
    assert len(rwp) == 1
    for needed in (
        "/data/algernon/stayc-clinical/vault",
        "/data/algernon/stayc-clinical/data",
        "/data/algernon/stayc-clinical/models/hf",
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


def test_execstart_is_internal_foreground(tmp_path: Path) -> None:
    """The unit runs the plain foreground monitor loop (--_internal-foreground),
    which is the path that carries the exit-79 propagation (non-live)."""
    plan = _build(tmp_path)
    directives = _directive_lines(plan.unit_content)
    execstart = [l for l in directives if l.startswith("ExecStart=")]
    assert len(execstart) == 1
    assert "--_internal-foreground" in execstart[0]
    assert "--config /data/algernon/stayc-clinical/config.stayc-clinical.yaml" in execstart[0]
    assert "/home/andrew/alfred/.venv/bin/python" in execstart[0]


# ---------------------------------------------------------------------------
# Standalone — NOT in the algernon fan-out
# ---------------------------------------------------------------------------


def test_unit_is_standalone_not_in_fanout(tmp_path: Path) -> None:
    """No PartOf= directive, no WantedBy=algernon.target — the ONLY WantedBy is
    default.target. (algernon.target appears only in the doc comment.)"""
    plan = _build(tmp_path)
    directives = _directive_lines(plan.unit_content)

    assert not any(l.startswith("PartOf=") for l in directives), (
        "STAY-C must not be PartOf= any target (standalone lifecycle)"
    )
    wantedby = [l for l in directives if l.startswith("WantedBy=")]
    assert wantedby == ["WantedBy=default.target"], (
        f"the only install target must be default.target; got {wantedby}"
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


def test_per_path_override_wins(tmp_path: Path) -> None:
    """A per-path flag overrides the stayc_root-derived default."""
    plan = _build(tmp_path, hf_home=Path("/mnt/fastdisk/hf"))
    assert plan.hf_home == Path("/mnt/fastdisk/hf")
    assert "ReadWritePaths=" in plan.unit_content
    assert "/mnt/fastdisk/hf" in plan.unit_content


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
