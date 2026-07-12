"""Install the STAY-C hardened sovereign systemd user unit (#42).

STAY-C is a STANDALONE sovereign clinical scribe — NOT a row in
``~/.alfred/instances.yaml`` and NOT part of the salem/kal-le/hypatia
fan-out. This installer is therefore fully SEPARATE from
``install_systemd_units.py`` (the fan-out installer over the registry): it
reads NO ``instances.yaml``, never renders or touches ``algernon.target``,
and installs exactly ONE unit —
``~/.config/systemd/user/alfred-stayc-clinical.service`` — that is
``WantedBy=default.target`` only. Keeping the two installers independent is
the point: touching the fan-out installer can never affect STAY-C, and vice
versa (GROUND #6 byte-identity of the fan-out is protected structurally).

It mirrors the proven render → build_plan → apply_plan idempotency contract:

  * ``render_stayc_unit`` — pure placeholder substitution with a
    post-substitution sentinel check (``ValueError`` if any ``<...>``
    placeholder survives, so a template typo surfaces here rather than
    producing an invalid unit at install time).
  * ``build_plan`` — pure: derive all deploy paths from a single
    ``<STAYC_ROOT>`` (overridable per-path by flags), read the bundled
    template, render. NO filesystem mutation, NO subprocess.
  * ``apply_plan`` — idempotent write-if-changed to the install dir.

INERT IN REPO: the ``.service.template`` ships full of ``<STAYC_*>`` /
``<ALFRED_REPO>`` placeholders and this installer is NEVER invoked by
``alfred instance up``, never referenced by ``instances.yaml``, never in
``algernon.target`` — nothing about STAY-C activates from a plain
checkout/deploy. It is operator-run, on-box, and fully reversible (see the
config header + the frozen spec's rollback).

Operator flow (after staging config + secrets + HF cache — see the config
example header):

    python -m alfred.scripts.install_stayc_unit

Then:

    systemctl --user start alfred-stayc-clinical.service
    journalctl --user -u alfred-stayc-clinical -f
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from alfred._data import get_systemd_dir


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: The single unit this installer manages.
STAYC_UNIT_FILENAME = "alfred-stayc-clinical.service"

#: The bundled template basename under ``get_systemd_dir()``.
STAYC_TEMPLATE_FILENAME = "alfred-stayc-clinical.service.template"

#: Default deploy root — every STAY-C path derives from this unless a
#: per-path flag overrides it. A single knob keeps the on-box layout coherent.
DEFAULT_STAYC_ROOT = Path("/data/algernon/stayc-clinical")

#: Placeholders the template carries. Every one MUST be substituted; the
#: sentinel check in ``render_stayc_unit`` raises if any survives. A future
#: template that adds a placeholder without updating this set surfaces there.
_STAYC_PLACEHOLDERS: frozenset[str] = frozenset({
    "<ALFRED_REPO>",
    "<STAYC_CONFIG>",
    "<STAYC_SECRETS_ENV>",
    "<STAYC_HF_HOME>",
    "<STAYC_VAULT>",
    "<STAYC_DATA>",
})

#: Generic residual-placeholder sweep — catches an UNKNOWN ``<UPPER_CASE>``
#: token the known-set above missed (belt on the sentinel).
_RESIDUAL_PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_]*>")

#: Default systemd user-unit install dir name under $XDG_CONFIG_HOME.
DEFAULT_INSTALL_DIR_NAME = "systemd/user"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class StaycInstallPlan:
    """What the installer will write. Populated by :func:`build_plan` (pure),
    consumed by :func:`apply_plan` (does the single write)."""
    alfred_repo: Path
    stayc_root: Path
    install_dir: Path
    #: Resolved per-path deploy targets (each overridable by a flag).
    config_path: Path
    secrets_env: Path
    hf_home: Path
    vault: Path
    data: Path
    unit_filename: str
    unit_content: str


# ---------------------------------------------------------------------------
# Pure helpers (no filesystem writes; no subprocess)
# ---------------------------------------------------------------------------


def _xdg_config_home() -> Path:
    """Return ``XDG_CONFIG_HOME`` or default ``~/.config``."""
    override = os.environ.get("XDG_CONFIG_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config"


def get_install_dir() -> Path:
    """Resolve the systemd user-unit install directory (XDG-aware)."""
    return _xdg_config_home() / DEFAULT_INSTALL_DIR_NAME


def render_stayc_unit(
    template: str,
    *,
    alfred_repo: Path,
    config_path: Path,
    secrets_env: Path,
    hf_home: Path,
    vault: Path,
    data: Path,
) -> str:
    """Substitute the STAY-C placeholders into the unit template.

    Raises ``ValueError`` if any known placeholder — or any residual
    ``<UPPER_CASE>`` token — survives the pass, so a template typo or an
    un-mapped placeholder never produces an invalid unit at install time.
    """
    rendered = (
        template
        .replace("<ALFRED_REPO>", str(alfred_repo))
        .replace("<STAYC_CONFIG>", str(config_path))
        .replace("<STAYC_SECRETS_ENV>", str(secrets_env))
        .replace("<STAYC_HF_HOME>", str(hf_home))
        .replace("<STAYC_VAULT>", str(vault))
        .replace("<STAYC_DATA>", str(data))
    )
    leftovers = sorted(ph for ph in _STAYC_PLACEHOLDERS if ph in rendered)
    residual = sorted(set(_RESIDUAL_PLACEHOLDER_RE.findall(rendered)))
    if leftovers or residual:
        raise ValueError(
            f"render_stayc_unit: template still contains placeholders after "
            f"substitution: known={leftovers} residual={residual}. Update "
            f"_STAYC_PLACEHOLDERS + the substitution above, or fix the template."
        )
    return rendered


def build_plan(
    *,
    alfred_repo: Path,
    stayc_root: Path,
    install_dir: Path,
    config_path: Path | None = None,
    secrets_env: Path | None = None,
    hf_home: Path | None = None,
    vault: Path | None = None,
    data: Path | None = None,
) -> StaycInstallPlan:
    """Derive deploy paths from ``stayc_root`` + render the unit. No FS writes.

    Every per-path argument defaults to the canonical layout under
    ``stayc_root`` but is independently overridable (e.g. an HF cache on a
    different volume). Reads the bundled template only — no ``instances.yaml``,
    no ``algernon.target``.
    """
    config_path = config_path or (stayc_root / "config.stayc-clinical.yaml")
    secrets_env = secrets_env or (stayc_root / "secrets" / "scribe.env")
    hf_home = hf_home or (stayc_root / "models" / "hf")
    vault = vault or (stayc_root / "vault")
    data = data or (stayc_root / "data")

    template_path = get_systemd_dir() / STAYC_TEMPLATE_FILENAME
    if not template_path.is_file():
        raise FileNotFoundError(
            f"Bundled STAY-C unit template missing at {template_path}. "
            f"Reinstall alfred or check the wheel/sdist contents."
        )
    template = template_path.read_text(encoding="utf-8")

    unit_content = render_stayc_unit(
        template,
        alfred_repo=alfred_repo,
        config_path=config_path,
        secrets_env=secrets_env,
        hf_home=hf_home,
        vault=vault,
        data=data,
    )

    return StaycInstallPlan(
        alfred_repo=alfred_repo,
        stayc_root=stayc_root,
        install_dir=install_dir,
        config_path=config_path,
        secrets_env=secrets_env,
        hf_home=hf_home,
        vault=vault,
        data=data,
        unit_filename=STAYC_UNIT_FILENAME,
        unit_content=unit_content,
    )


# ---------------------------------------------------------------------------
# Filesystem + subprocess helpers
# ---------------------------------------------------------------------------


def apply_plan(plan: StaycInstallPlan) -> dict[str, int]:
    """Write the rendered unit to the install dir (idempotent write-if-changed).

    Returns ``{"written": 0|1, "unchanged": 0|1}`` — a second apply with
    unchanged inputs writes nothing (operators re-running see "0 changes"
    rather than a spurious write + reload).
    """
    plan.install_dir.mkdir(parents=True, exist_ok=True)
    target = plan.install_dir / plan.unit_filename
    if target.is_file() and target.read_text(encoding="utf-8") == plan.unit_content:
        return {"written": 0, "unchanged": 1}
    target.write_text(plan.unit_content, encoding="utf-8")
    return {"written": 1, "unchanged": 0}


def _check_linger(username: str) -> bool:
    """Return True iff lingering is currently enabled for ``username``.

    Reads ``loginctl show-user`` and parses ``Linger=yes``. False on any
    error (loginctl missing / user not found / parse failure) — caller then
    offers to enable it. Standalone twin of the fan-out installer's check so
    the two installers stay independent.
    """
    if shutil.which("loginctl") is None:
        return False
    try:
        proc = subprocess.run(
            ["loginctl", "show-user", username],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if proc.returncode != 0:
        return False
    for line in proc.stdout.splitlines():
        if line.startswith("Linger="):
            return line.strip() == "Linger=yes"
    return False


def _enable_linger(username: str) -> bool:
    """Run ``sudo loginctl enable-linger <name>``. Returns True on success."""
    if shutil.which("sudo") is None:
        print(
            "error: sudo is not available; cannot enable linger. "
            f"Run manually as root: loginctl enable-linger {username}",
            file=sys.stderr,
        )
        return False
    try:
        # No capture — operator needs to see the sudo prompt + type a password.
        proc = subprocess.run(
            ["sudo", "loginctl", "enable-linger", username],
            timeout=120,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"error: enable-linger failed: {exc}", file=sys.stderr)
        return False
    return proc.returncode == 0


def _systemctl_user_reload() -> bool:
    """Run ``systemctl --user daemon-reload``. Returns True on success."""
    if shutil.which("systemctl") is None:
        print(
            "error: systemctl is not available — is systemd installed?",
            file=sys.stderr,
        )
        return False
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"error: daemon-reload failed: {exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(
            f"error: daemon-reload exit {proc.returncode}: {proc.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    return True


def _systemctl_user_enable(unit_name: str) -> bool:
    """Run ``systemctl --user enable <unit>``. Returns True on success."""
    if shutil.which("systemctl") is None:
        return False
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "enable", unit_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"error: enable {unit_name} failed: {exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(
            f"error: enable {unit_name} exit {proc.returncode}: {proc.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    return True


def print_plan(plan: StaycInstallPlan) -> None:
    """Emit a human-readable summary of what the installer WILL do."""
    print("STAY-C sovereign systemd installer plan")
    print(f"  Repo:        {plan.alfred_repo}")
    print(f"  STAY-C root: {plan.stayc_root}")
    print(f"  Install dir: {plan.install_dir}")
    print()
    print("--- Standalone unit (NOT in the algernon fan-out) ---")
    print(f"  {plan.unit_filename}  (WantedBy=default.target only)")
    print(f"    config:   {plan.config_path}")
    print(f"    secrets:  {plan.secrets_env}")
    print(f"    HF cache: {plan.hf_home}")
    print(f"    vault:    {plan.vault}")
    print(f"    data:     {plan.data}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_alfred_repo() -> Path:
    """Resolve the alfred repo root (this file's package-grandparent).

    ``__file__`` lives at ``<repo>/src/alfred/scripts/install_stayc_unit.py``;
    four parents up is ``<repo>``. Correct for an editable install; a wheel
    install MUST pass ``--alfred-repo`` explicitly (site-packages is not a
    useful ``WorkingDirectory``).
    """
    return Path(__file__).resolve().parent.parent.parent.parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Install the STAY-C hardened sovereign systemd user unit. "
            "Standalone — reads NO instances.yaml, never touches "
            "algernon.target. Writes exactly one "
            f"{STAYC_UNIT_FILENAME} to ~/.config/systemd/user/. "
            "Idempotent — safe to re-run."
        ),
    )
    parser.add_argument(
        "--stayc-root",
        type=Path,
        default=DEFAULT_STAYC_ROOT,
        help=(
            "Deploy root every STAY-C path derives from "
            f"(default: {DEFAULT_STAYC_ROOT}). Override per-path with the "
            "--config/--secrets-env/--hf-home/--vault/--data flags."
        ),
    )
    parser.add_argument(
        "--alfred-repo",
        type=Path,
        default=None,
        help=(
            "Override the Alfred repo root used in the unit's WorkingDirectory "
            "+ ExecStart. Default: this script's package-grandparent (the repo "
            "root for an editable install). Wheel installs MUST set this."
        ),
    )
    parser.add_argument(
        "--install-dir",
        type=Path,
        default=None,
        help=(
            "Override the systemd user-unit install directory. Default: "
            "$XDG_CONFIG_HOME/systemd/user (or ~/.config/systemd/user)."
        ),
    )
    parser.add_argument("--config", type=Path, default=None, help="Override the config path (default: <stayc-root>/config.stayc-clinical.yaml).")
    parser.add_argument("--secrets-env", type=Path, default=None, help="Override the salt-only EnvironmentFile (default: <stayc-root>/secrets/scribe.env).")
    parser.add_argument("--hf-home", type=Path, default=None, help="Override HF_HOME / STT cache (default: <stayc-root>/models/hf).")
    parser.add_argument("--vault", type=Path, default=None, help="Override the PHI vault path (default: <stayc-root>/vault).")
    parser.add_argument("--data", type=Path, default=None, help="Override the data dir — logs/audit/encounters/inbox/pid (default: <stayc-root>/data).")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan without writing files or calling systemctl.",
    )
    parser.add_argument(
        "--skip-linger",
        action="store_true",
        help="Skip the linger check + enable (linger managed externally).",
    )
    parser.add_argument(
        "--skip-systemctl",
        action="store_true",
        help=(
            "Skip daemon-reload + enable. Use in test/CI environments where "
            "systemctl is unavailable and you only want the unit file written."
        ),
    )
    args = parser.parse_args(argv)

    alfred_repo = (args.alfred_repo or _default_alfred_repo()).expanduser().resolve()
    stayc_root = args.stayc_root.expanduser()
    install_dir = (args.install_dir or get_install_dir()).expanduser().resolve()

    try:
        plan = build_plan(
            alfred_repo=alfred_repo,
            stayc_root=stayc_root,
            install_dir=install_dir,
            config_path=args.config,
            secrets_env=args.secrets_env,
            hf_home=args.hf_home,
            vault=args.vault,
            data=args.data,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print_plan(plan)

    if args.dry_run:
        print("--- DRY-RUN — no changes written. ---")
        return 0

    # Linger check + enable so the unit survives the operator's session ending.
    if not args.skip_linger:
        username = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        if not username:
            print(
                "error: cannot determine current username ($USER / $LOGNAME "
                "unset); pass --skip-linger to manage linger externally.",
                file=sys.stderr,
            )
            return 2
        if _check_linger(username):
            print(f"Linger: already enabled for {username}.")
        else:
            print(
                f"Linger: NOT enabled for {username}. Enabling via sudo "
                f"(you may be prompted for your password)…"
            )
            if not _enable_linger(username):
                print(
                    f"error: failed to enable linger. Run manually: "
                    f"sudo loginctl enable-linger {username}",
                    file=sys.stderr,
                )
                return 1

    print("--- Writing unit file ---")
    counters = apply_plan(plan)
    print(
        f"  {plan.unit_filename}: "
        f"{'written' if counters['written'] else 'unchanged from prior install'}"
    )
    print()

    if not args.skip_systemctl:
        print("--- systemctl ---")
        if not _systemctl_user_reload():
            print("error: daemon-reload failed", file=sys.stderr)
            return 1
        print("  daemon-reload: OK")
        if not _systemctl_user_enable(plan.unit_filename):
            print(f"error: enable {plan.unit_filename} failed", file=sys.stderr)
            return 1
        print(f"  enable {plan.unit_filename}: OK")
        print()

    print("--- Start + verify ---")
    print(f"  systemctl --user start {plan.unit_filename}")
    print(f"  journalctl --user -u {plan.unit_filename.removesuffix('.service')} -f")
    print(
        "  # expect scribe.daemon.up + scribe.egress_firewall.enforced OR "
        ".unverified (unverified is EXPECTED + fine on a WSL2 kernel lacking "
        "cgroup-v2 BPF — the Python guard remains the verified control)"
    )
    print()
    print("--- Rollback ---")
    print(
        f"  systemctl --user disable --now {plan.unit_filename} && "
        f"rm {plan.install_dir / plan.unit_filename} && "
        f"systemctl --user daemon-reload"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
