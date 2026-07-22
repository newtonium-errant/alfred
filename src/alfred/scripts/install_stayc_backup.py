"""Install the STAY-C dedicated-backup systemd timer + service (task #13, slice 13d-4b).

OPERATOR-GATED + INERT. This installer renders + writes two units — ``stayc-backup.service``
(a oneshot running ``alfred scribe retention backup-run``) and ``stayc-backup.timer`` — to
``/etc/systemd/system`` and (unless ``--skip-systemctl``) daemon-reloads + enables the TIMER. It
NEVER runs ``restic init`` and NEVER starts a backup: creating the dedicated repo + starting the timer
are the operator's real-data-gate steps (mirrors ``install_stayc_unit.py``'s inert, operator-run
posture — nothing about STAY-C backup activates from a plain checkout/deploy).

The dedicated repo (recon 2026-07-21 §4): its OWN restic repo + tag + 10-yr keep policy, SEPARATE from
the general ``algernon-backup`` nightly (whose 2-yr cap would prune the 10-yr archive). Credentials come
from the ``EnvironmentFile`` (``STAYC_RESTIC_REPO`` + ``STAYC_RESTIC_PASSWORD_FILE``) — never hardcoded,
never the general repo (``scribe.backup._restic_env`` fails closed).

Operator flow (after provisioning the dedicated repo + the creds env-file + running keygen):

    sudo /data/algernon/stayc-clinical/.venv/bin/python \\
        -m alfred.scripts.install_stayc_backup
    restic -r "$STAYC_RESTIC_REPO" init          # operator, ONCE — NOT done by this installer
    sudo systemctl start stayc-backup.timer      # operator gates the schedule live
"""
from __future__ import annotations

import argparse
import grp
import os
import pwd
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from alfred._data import get_systemd_dir

SERVICE_FILENAME = "stayc-backup.service"
TIMER_FILENAME = "stayc-backup.timer"
SERVICE_TEMPLATE = "stayc-backup.service.template"
TIMER_TEMPLATE = "stayc-backup.timer.template"

DEFAULT_STAYC_ROOT = Path("/data/algernon/stayc-clinical")
SYSTEM_INSTALL_DIR = Path("/etc/systemd/system")
#: Default backup cadence — daily at 04:20 UTC (offset from the general algernon-backup's 04:00 so the
#: two never contend for IO). Operator-overridable via --on-calendar.
DEFAULT_ONCALENDAR = "*-*-* 04:20:00"

_PLACEHOLDERS: frozenset[str] = frozenset({
    "<STAYC_BACKUP_USER>",
    "<STAYC_BACKUP_GROUP>",
    "<STAYC_BACKUP_PYTHON>",
    "<STAYC_BACKUP_CONFIG>",
    "<STAYC_BACKUP_ENV>",
    "<STAYC_BACKUP_ONCALENDAR>",
})
_RESIDUAL_PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_]*>")


@dataclass
class StaycBackupPlan:
    stayc_root: Path
    install_dir: Path
    unit_user: str
    unit_group: str
    python: Path
    config_path: Path
    creds_env: Path
    on_calendar: str
    service_content: str
    timer_content: str


def get_install_dir() -> Path:
    return SYSTEM_INSTALL_DIR


def _render(template: str, subs: dict[str, str]) -> str:
    rendered = template
    for ph, val in subs.items():
        rendered = rendered.replace(ph, val)
    leftovers = sorted(ph for ph in _PLACEHOLDERS if ph in rendered)
    residual = sorted(set(_RESIDUAL_PLACEHOLDER_RE.findall(rendered)))
    if leftovers or residual:
        raise ValueError(
            f"stayc-backup render: placeholders survived substitution: known={leftovers} "
            f"residual={residual}. Update _PLACEHOLDERS + the templates in lockstep.")
    return rendered


def render_backup_units(
    *, unit_user: str, unit_group: str, python: Path, config_path: Path, creds_env: Path,
    on_calendar: str,
) -> tuple[str, str]:
    """Render the (service, timer) unit bodies from the bundled templates, with a residual-placeholder
    sentinel so a template typo surfaces here, not as an invalid installed unit."""
    systemd = get_systemd_dir()
    subs = {
        "<STAYC_BACKUP_USER>": unit_user,
        "<STAYC_BACKUP_GROUP>": unit_group,
        "<STAYC_BACKUP_PYTHON>": str(python),
        "<STAYC_BACKUP_CONFIG>": str(config_path),
        "<STAYC_BACKUP_ENV>": str(creds_env),
        "<STAYC_BACKUP_ONCALENDAR>": on_calendar,
    }
    service = _render((systemd / SERVICE_TEMPLATE).read_text(encoding="utf-8"), subs)
    timer = _render((systemd / TIMER_TEMPLATE).read_text(encoding="utf-8"), subs)
    return service, timer


def build_plan(
    *, stayc_root: Path, install_dir: Path, unit_user: str, unit_group: str,
    python: Path | None = None, config_path: Path | None = None, creds_env: Path | None = None,
    on_calendar: str = DEFAULT_ONCALENDAR,
) -> StaycBackupPlan:
    """Derive deploy paths from ``stayc_root`` (each overridable) + render both units. NO FS mutation,
    NO subprocess, NO restic. ``creds_env`` defaults to ``<stayc_root>/secrets/stayc-restic.env`` (the
    operator provisions it with STAYC_RESTIC_REPO + STAYC_RESTIC_PASSWORD_FILE)."""
    python = python or (stayc_root / ".venv" / "bin" / "python")
    config_path = config_path or (stayc_root / "config.stayc-clinical.yaml")
    creds_env = creds_env or (stayc_root / "secrets" / "stayc-restic.env")
    service, timer = render_backup_units(
        unit_user=unit_user, unit_group=unit_group, python=python, config_path=config_path,
        creds_env=creds_env, on_calendar=on_calendar)
    return StaycBackupPlan(
        stayc_root=stayc_root, install_dir=install_dir, unit_user=unit_user, unit_group=unit_group,
        python=python, config_path=config_path, creds_env=creds_env, on_calendar=on_calendar,
        service_content=service, timer_content=timer)


def apply_plan(plan: StaycBackupPlan) -> dict[str, int]:
    """Idempotent write-if-changed of BOTH units. Returns ``{written, unchanged}`` counts."""
    plan.install_dir.mkdir(parents=True, exist_ok=True)
    written = unchanged = 0
    for name, content in ((SERVICE_FILENAME, plan.service_content), (TIMER_FILENAME, plan.timer_content)):
        target = plan.install_dir / name
        if target.is_file() and target.read_text(encoding="utf-8") == content:
            unchanged += 1
        else:
            target.write_text(content, encoding="utf-8")
            written += 1
    return {"written": written, "unchanged": unchanged}


def _default_unit_user() -> str:
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        return sudo_user
    return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


def _default_unit_group(user: str) -> str:
    try:
        return grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name
    except (KeyError, AttributeError):
        return user


def _geteuid() -> int:
    return os.geteuid() if hasattr(os, "geteuid") else 0


def _systemctl(*argv: str) -> bool:
    if shutil.which("systemctl") is None:
        print("error: systemctl not available — is systemd installed?", file=sys.stderr)
        return False
    try:
        proc = subprocess.run(["systemctl", *argv], capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"error: systemctl {' '.join(argv)} failed: {exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(f"error: systemctl {' '.join(argv)} exit {proc.returncode}: {proc.stderr.strip()}",
              file=sys.stderr)
        return False
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=("Install the STAY-C dedicated-backup timer + service (INERT/operator-gated). Writes "
                     f"{SERVICE_FILENAME} + {TIMER_FILENAME} to /etc/systemd/system and enables the "
                     "TIMER. NEVER runs `restic init` or starts a backup — those are operator steps."))
    parser.add_argument("--stayc-root", type=Path, default=DEFAULT_STAYC_ROOT)
    parser.add_argument("--python", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--creds-env", type=Path, default=None,
                        help="EnvironmentFile with STAYC_RESTIC_REPO + STAYC_RESTIC_PASSWORD_FILE "
                             "(default: <stayc-root>/secrets/stayc-restic.env).")
    parser.add_argument("--on-calendar", default=DEFAULT_ONCALENDAR,
                        help=f"systemd OnCalendar cadence (default: {DEFAULT_ONCALENDAR}).")
    parser.add_argument("--unit-user", default=None)
    parser.add_argument("--unit-group", default=None)
    parser.add_argument("--install-dir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-systemctl", action="store_true")
    args = parser.parse_args(argv)

    stayc_root = args.stayc_root.expanduser()
    install_dir = (args.install_dir or get_install_dir()).expanduser().resolve()
    unit_user = args.unit_user or _default_unit_user()
    if not unit_user or unit_user == "root":
        print("error: refusing User=root / undeterminable user — run via sudo AS THE OPERATOR or pass "
              "--unit-user.", file=sys.stderr)
        return 2
    unit_group = args.unit_group or _default_unit_group(unit_user)

    try:
        plan = build_plan(
            stayc_root=stayc_root, install_dir=install_dir, unit_user=unit_user, unit_group=unit_group,
            python=args.python.expanduser() if args.python else None,
            config_path=args.config, creds_env=args.creds_env, on_calendar=args.on_calendar)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print("STAY-C dedicated-backup installer (INERT — no restic init, no backup started)")
    print(f"  runs as: User={plan.unit_user} Group={plan.unit_group}")
    print(f"  ExecStart python: {plan.python}   config: {plan.config_path}")
    print(f"  restic creds env: {plan.creds_env}  (STAYC_RESTIC_REPO + STAYC_RESTIC_PASSWORD_FILE)")
    print(f"  schedule: {plan.on_calendar}")
    print(f"  install dir: {plan.install_dir}   units: {SERVICE_FILENAME}, {TIMER_FILENAME}")
    if args.dry_run:
        print("--- DRY-RUN — nothing written. ---")
        return 0

    needs_root = install_dir == SYSTEM_INSTALL_DIR or not args.skip_systemctl
    if needs_root and _geteuid() != 0:
        print("error: writing to /etc/systemd/system + driving systemctl requires root — re-run via "
              f"sudo:\n  sudo {plan.python} -m alfred.scripts.install_stayc_backup", file=sys.stderr)
        return 2

    counters = apply_plan(plan)
    print(f"  units: {counters['written']} written, {counters['unchanged']} unchanged")
    if not args.skip_systemctl:
        if not _systemctl("daemon-reload"):
            return 1
        if not _systemctl("enable", TIMER_FILENAME):
            return 1
        print(f"  daemon-reload + enable {TIMER_FILENAME}: OK")
    print("\n--- OPERATOR real-data-gate steps (NOT done here) ---")
    print(f"  restic -r \"$STAYC_RESTIC_REPO\" init      # create the dedicated repo, ONCE")
    print(f"  # set a 10-yr keep policy on the dedicated repo (forget --keep-yearly 10 …)")
    print(f"  sudo systemctl start {TIMER_FILENAME}      # gate the schedule live")
    return 0


if __name__ == "__main__":
    sys.exit(main())
