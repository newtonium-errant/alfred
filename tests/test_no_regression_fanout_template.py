"""GROUND #6 byte-identity guard for the salem/kal-le/hypatia fan-out (#42).

STAY-C ships as a STANDALONE unit + a SEPARATE installer. This test proves the
fan-out surface is untouched: the fan-out template carries NONE of the new
hardening directives, and ``install_systemd_units.build_plan`` never emits an
``alfred-stayc-clinical.service`` (STAY-C is not a registry row).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred._data import get_systemd_dir
from alfred.scripts import install_systemd_units as fanout

#: Directives introduced by #42's hardened STAY-C unit. NONE may appear in the
#: fan-out template (they are STAY-C-only). ``RestartPreventExitStatus=78 79``
#: is STAY-C's; the fan-out keeps its original ``RestartPreventExitStatus=78``.
_HARDENING_DIRECTIVES = (
    "UnsetEnvironment=",
    "ProtectSystem=",
    "ProtectHome=",
    "ReadWritePaths=",
    "ReadOnlyPaths=",
    "IPAddressDeny=",
    "IPAddressAllow=",
    "RestrictAddressFamilies=",
    "NoNewPrivileges=",
    "UMask=",
    "PrivateTmp=",
    "ProtectKernelTunables=",
    "LockPersonality=",
    "RestartPreventExitStatus=78 79",
    "HF_HUB_OFFLINE",
)


def _fanout_template() -> str:
    return (get_systemd_dir() / "alfred-instance.service.template").read_text(encoding="utf-8")


def test_fanout_template_has_no_hardening_directives() -> None:
    """The fan-out per-instance template stays byte-identical to its shipped
    shape — none of #42's hardening directives leaked into it."""
    template = _fanout_template()
    for directive in _HARDENING_DIRECTIVES:
        assert directive not in template, (
            f"fan-out template must NOT carry STAY-C hardening directive "
            f"{directive!r} (GROUND #6 byte-identity)"
        )
    # Positive sanity: the fan-out keeps its ORIGINAL shape.
    assert "PartOf=algernon.target" in template
    assert "RestartPreventExitStatus=78" in template
    assert "WantedBy=default.target algernon.target" in template


def test_fanout_registry_never_lists_stayc(tmp_path: Path) -> None:
    """``install_systemd_units.build_plan`` over a representative registry never
    emits an alfred-stayc-clinical.service, and no rendered fan-out unit carries
    the STAY-C hardening directives. STAY-C is not a registry row."""
    registry = tmp_path / "instances.yaml"
    registry.write_text(
        "instances:\n"
        "  - name: salem\n"
        "    display: Salem\n"
        "    config: /data/algernon/salem/config.salem.yaml\n"
        "    enabled: true\n"
        "  - name: kal-le\n"
        "    display: KAL-LE\n"
        "    config: /data/algernon/kal-le/config.kalle.yaml\n"
        "    enabled: true\n"
        "  - name: hypatia\n"
        "    display: Hypatia\n"
        "    config: /data/algernon/hypatia/config.hypatia.yaml\n"
        "    enabled: true\n",
        encoding="utf-8",
    )

    plan = fanout.build_plan(
        registry_path=registry,
        alfred_repo=Path("/home/andrew/alfred"),
        install_dir=tmp_path / "systemd",
    )

    # No STAY-C unit is ever produced by the fan-out installer.
    assert "alfred-stayc-clinical.service" not in plan.service_files
    assert all("stayc" not in fn for fn in plan.service_files), (
        f"fan-out emitted a stayc-shaped unit: {list(plan.service_files)}"
    )

    # And none of the rendered fan-out units carry the hardening directives.
    for filename, content in plan.service_files.items():
        for directive in _HARDENING_DIRECTIVES:
            assert directive not in content, (
                f"{filename} unexpectedly carries {directive!r}"
            )

    # The algernon target render never Wants the STAY-C unit.
    assert "stayc" not in plan.target_file.lower()


def test_real_registry_if_present_excludes_stayc() -> None:
    """If a real ~/.alfred/instances.yaml exists on this box, it must NOT list
    stayc-clinical (STAY-C is standalone). Skipped when absent (CI / fresh
    checkout) — the synthetic-registry test above is the deterministic pin."""
    registry_path = Path.home() / ".alfred" / "instances.yaml"
    if not registry_path.is_file():
        pytest.skip("no ~/.alfred/instances.yaml on this box (expected in CI)")
    instances = fanout.load_registry(registry_path)
    names = {inst.name for inst in instances}
    assert not any("stayc" in n for n in names), (
        f"real registry unexpectedly lists a stayc instance: {names}"
    )
