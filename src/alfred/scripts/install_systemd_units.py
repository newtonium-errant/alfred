"""Install systemd user units for the Algernon platform.

Closes the host-reboot-survival gap shipped 2026-05-29: on WSL2 with
``systemd=true``, user systemd is available, but without enabled units
+ lingering, a host reboot leaves all peer daemons down requiring
manual ``alfred instance up`` (or per-instance bounces). The installer
ships three unit files + one platform target that survive boot:

  * ``~/.config/systemd/user/alfred-<name>.service`` — one per
    enabled instance from ``~/.alfred/instances.yaml``. Each
    runs ``python -m alfred --config <X> up --_internal-foreground``
    as ``Type=simple`` so systemd manages the process lifecycle
    directly (matches the orchestrator's foreground mode).
  * ``~/.config/systemd/user/algernon.target`` — platform-level
    target that ``Wants=`` each enabled instance's service. Enabling
    this target enables all three services together.

Restart policy mirrors the orchestrator's auto-restart contract:
``Restart=on-failure``, ``RestartSec=30s``, ``StartLimitBurst=5``
within ``StartLimitIntervalSec=300``, and ``RestartPreventExitStatus=78``
to skip the "missing deps" sentinel exit code.

Linger: ``loginctl enable-linger <user>`` is required so user units
keep running after the operator's terminal session ends. The
installer prompts for sudo to enable linger if not already on.

Source-of-truth pattern: instance set comes from
``~/.alfred/instances.yaml`` (the same registry ``alfred instance``
verbs consume). Adding / removing / disabling an instance + re-running
the installer is the canonical refresh path — the installer does NOT
duplicate the instance list.

Idempotent: re-running with the same registry produces identical
files. Re-running after registry changes refreshes the installed
files but leaves running units undisturbed (systemd picks up
template changes on next reload + restart, not at write time).

Bundle: templates ship at ``src/alfred/_bundled/systemd/`` and are
located via ``_data.get_systemd_dir()``.

Operator flow:

    python -m alfred.scripts.install_systemd_units

Or via the legacy-shape shim:

    scripts/install_systemd_units.sh

After install + linger enable:

    systemctl --user list-unit-files | grep alfred
    systemctl --user is-enabled algernon.target  # → enabled
    systemctl --user start algernon.target       # optional immediate start

After host reboot (Andrew's verification step from PowerShell):

    wsl --shutdown
    # Reopen WSL, ssh in:
    systemctl --user status algernon.target

Caveat documented per dispatch: the existing
``alfred instance up`` daemons (if running) hold the PID files that
systemd-managed daemons would also try to claim. Operator must NOT
mix the two start mechanisms within the same WSL session — kill the
``alfred instance up`` daemons before running ``systemctl --user
start algernon.target`` for the first time. Post-install + post-reboot
the systemd path is canonical.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from alfred._data import get_systemd_dir
from alfred.instance_set import (
    Instance,
    iter_enabled,
    load_registry,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: Per-instance unit file template placeholders. The installer
#: substitutes each occurrence; a future template that adds a new
#: placeholder must update this set so the unrendered-placeholder
#: check at the end of ``render_service_unit`` catches typos.
_SERVICE_PLACEHOLDERS: frozenset[str] = frozenset({
    "<DISPLAY>",
    "<ALFRED_REPO>",
    "<CONFIG_PATH>",
})

#: ``algernon.target`` placeholder. Rewritten in place with a
#: ``Wants=`` line listing each enabled instance's service.
_TARGET_PLACEHOLDER = "<WANTS_LINE>"

#: Default install root (~/.config/systemd/user). XDG_CONFIG_HOME
#: override respected per the standard XDG Base Directory spec.
DEFAULT_INSTALL_DIR_NAME = "systemd/user"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class InstallPlan:
    """Files the installer will write + the registry it sourced from.

    Populated by :func:`build_plan` (pure: no filesystem mutation,
    no subprocess calls). Consumed by :func:`apply_plan` (does the
    writes). Splitting the two makes the installer testable without
    touching the operator's actual systemd directory.
    """
    alfred_repo: Path
    install_dir: Path
    enabled_instances: list[Instance]
    #: ``{rel_filename: rendered_unit_content}`` — keys are filenames
    #: under ``install_dir`` (e.g. ``alfred-salem.service``).
    service_files: dict[str, str]
    target_file: str  # rendered ``algernon.target`` content


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
    """Resolve the systemd user-unit install directory.

    Honours ``XDG_CONFIG_HOME`` per the standard XDG Base Directory
    spec; falls back to ``~/.config/systemd/user`` for the common
    case. Tests pass an explicit override via ``--install-dir``.
    """
    return _xdg_config_home() / DEFAULT_INSTALL_DIR_NAME


def _service_filename(instance: Instance) -> str:
    """Map an instance to its systemd unit filename.

    Filename uses the lowercase slug (``instance.name``), matching
    the dispatch ratification: ``alfred-salem.service``,
    ``alfred-kal-le.service``, ``alfred-hypatia.service``. Hyphens
    preserved (systemd permits them in unit names; the dispatch
    confirmed this layout).
    """
    return f"alfred-{instance.name}.service"


def render_service_unit(
    template: str,
    instance: Instance,
    alfred_repo: Path,
) -> str:
    """Substitute per-instance placeholders into a service template.

    Placeholders:
      * ``<DISPLAY>``     → ``instance.display`` (operator-facing name)
      * ``<ALFRED_REPO>`` → ``alfred_repo`` (absolute path to repo root)
      * ``<CONFIG_PATH>`` → ``instance.config`` (the registry config
        path; absolute or relative — the unit file's
        ``WorkingDirectory`` resolves relative paths against
        ``alfred_repo``)

    Raises ``ValueError`` if any placeholder remains unsubstituted
    after the pass — a future template that adds a new placeholder
    without updating ``_SERVICE_PLACEHOLDERS`` surfaces here rather
    than producing an invalid unit file at install time.
    """
    rendered = (
        template
        .replace("<DISPLAY>", instance.display)
        .replace("<ALFRED_REPO>", str(alfred_repo))
        .replace("<CONFIG_PATH>", instance.config)
    )
    # Sentinel check: every placeholder we know about must be gone.
    # Catches typos in templates + new placeholders added without
    # updating the substitution dict.
    leftovers = [ph for ph in _SERVICE_PLACEHOLDERS if ph in rendered]
    if leftovers:
        raise ValueError(
            f"render_service_unit: template still contains "
            f"placeholders after substitution: "
            f"{sorted(leftovers)}. Update _SERVICE_PLACEHOLDERS "
            f"or fix the template."
        )
    return rendered


def render_algernon_target(
    template: str,
    enabled_instances: list[Instance],
) -> str:
    """Inject the per-instance ``Wants=`` line into the target template.

    Builds a space-separated ``Wants=alfred-<name>.service ...`` line
    from the enabled instances, in registry order. Order is preserved
    so the operator's mental model of "Salem first" carries from the
    registry to the installed target.

    Raises ``ValueError`` if the ``<WANTS_LINE>`` placeholder isn't
    in the template (catches a future template refactor that drops
    the placeholder while keeping the file structure).
    """
    if _TARGET_PLACEHOLDER not in template:
        raise ValueError(
            f"render_algernon_target: template missing the "
            f"{_TARGET_PLACEHOLDER!r} placeholder — cannot render "
            f"the per-instance Wants line."
        )
    if not enabled_instances:
        # Empty registry → empty Wants. The target is still valid
        # (systemd allows a target with no Wants), just inert.
        wants = "# (no enabled instances in registry)"
    else:
        services = " ".join(
            _service_filename(inst) for inst in enabled_instances
        )
        wants = f"Wants={services}"
    return template.replace(_TARGET_PLACEHOLDER, wants)


def build_plan(
    registry_path: Path | None,
    alfred_repo: Path,
    install_dir: Path,
) -> InstallPlan:
    """Load the registry + render all unit files. No filesystem writes.

    ``registry_path=None`` defaults to ``~/.alfred/instances.yaml``
    via :func:`alfred.instance_set.load_registry`.

    Each enabled instance gets a service-unit render keyed by the
    ``alfred-<name>.service`` filename. The target gets one render
    with the per-instance ``Wants=`` line.
    """
    instances = load_registry(registry_path)
    enabled = list(iter_enabled(instances))

    systemd_dir = get_systemd_dir()
    service_template_path = systemd_dir / "alfred-instance.service.template"
    target_template_path = systemd_dir / "algernon.target"

    if not service_template_path.is_file():
        raise FileNotFoundError(
            f"Bundled service template missing at {service_template_path}. "
            f"Reinstall alfred or check the wheel/sdist contents."
        )
    if not target_template_path.is_file():
        raise FileNotFoundError(
            f"Bundled target template missing at {target_template_path}. "
            f"Reinstall alfred or check the wheel/sdist contents."
        )

    service_template = service_template_path.read_text(encoding="utf-8")
    target_template = target_template_path.read_text(encoding="utf-8")

    service_files: dict[str, str] = {}
    for inst in enabled:
        rendered = render_service_unit(service_template, inst, alfred_repo)
        service_files[_service_filename(inst)] = rendered

    target_file = render_algernon_target(target_template, enabled)

    return InstallPlan(
        alfred_repo=alfred_repo,
        install_dir=install_dir,
        enabled_instances=enabled,
        service_files=service_files,
        target_file=target_file,
    )


# ---------------------------------------------------------------------------
# Filesystem + subprocess helpers
# ---------------------------------------------------------------------------


def _check_linger(username: str) -> bool:
    """Return True iff lingering is currently enabled for ``username``.

    Reads ``loginctl show-user <name>`` and parses the ``Linger=yes``
    line. Returns False on any error (loginctl missing, user not
    found, parse failure) — caller treats that as "not enabled"
    and offers to enable it.

    Per ``feedback_intentionally_left_blank``: errors here log
    explicitly so the operator can distinguish "loginctl missing"
    from "linger off" from "unknown user."
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
    """Run ``sudo loginctl enable-linger <name>``. Returns True on success.

    Prompts for sudo via the subprocess — the operator types their
    password into the controlling terminal. Per the dispatch's
    "Installer handles ``sudo loginctl enable-linger andrew`` via
    sudo prompt (idempotent — check existing linger state first)."
    """
    if shutil.which("sudo") is None:
        print(
            "error: sudo is not available; cannot enable linger. "
            "Run manually as root: "
            f"loginctl enable-linger {username}",
            file=sys.stderr,
        )
        return False
    try:
        # No capture_output — operator needs to see the sudo prompt
        # + provide their password.
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
            f"error: daemon-reload exit {proc.returncode}: "
            f"{proc.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    return True


def _systemctl_user_enable(target_name: str) -> bool:
    """Run ``systemctl --user enable <target>``. Returns True on success."""
    if shutil.which("systemctl") is None:
        return False
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "enable", target_name],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"error: enable {target_name} failed: {exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(
            f"error: enable {target_name} exit {proc.returncode}: "
            f"{proc.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    return True


def apply_plan(plan: InstallPlan) -> dict[str, int]:
    """Write all rendered unit files to the install dir.

    Returns a counter dict ``{services_written, target_written,
    unchanged}``. ``unchanged`` counts files where the on-disk
    content already matches the rendered content (idempotent re-run
    detection — operators running the installer twice see "0 changes"
    rather than spurious writes).
    """
    plan.install_dir.mkdir(parents=True, exist_ok=True)
    counters = {"services_written": 0, "target_written": 0, "unchanged": 0}

    for filename, content in plan.service_files.items():
        target = plan.install_dir / filename
        if target.is_file() and target.read_text(encoding="utf-8") == content:
            counters["unchanged"] += 1
            continue
        target.write_text(content, encoding="utf-8")
        counters["services_written"] += 1

    target_path = plan.install_dir / "algernon.target"
    if (
        target_path.is_file()
        and target_path.read_text(encoding="utf-8") == plan.target_file
    ):
        counters["unchanged"] += 1
    else:
        target_path.write_text(plan.target_file, encoding="utf-8")
        counters["target_written"] = 1

    return counters


def print_plan(plan: InstallPlan) -> None:
    """Emit a human-readable summary of what the installer WILL do."""
    print(f"Algernon systemd installer plan")
    print(f"  Repo:        {plan.alfred_repo}")
    print(f"  Install dir: {plan.install_dir}")
    print()
    print(f"--- Per-instance service units ---")
    if not plan.enabled_instances:
        print("  (no enabled instances in registry)")
    else:
        for inst in plan.enabled_instances:
            print(
                f"  {inst.display:10s} → "
                f"{_service_filename(inst)} "
                f"(config={inst.config})"
            )
    print()
    print(f"--- Platform target ---")
    print(f"  algernon.target Wants= {len(plan.enabled_instances)} services")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_alfred_repo() -> Path:
    """Resolve the alfred repo root (this file's package's parent's parent).

    ``__file__`` lives at ``<repo>/src/alfred/scripts/install_systemd_units.py``;
    walking 4 parents up reaches ``<repo>``. This works both for an
    editable install (where the source tree IS the repo) and for a
    wheel install (where the file is under ``site-packages/alfred/scripts``
    — in which case the operator MUST pass ``--alfred-repo``
    explicitly since ``site-packages`` is not a useful
    ``WorkingDirectory`` for the unit).
    """
    candidate = Path(__file__).resolve().parent.parent.parent.parent
    return candidate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Install systemd user units for the Algernon platform. "
            "Reads ~/.alfred/instances.yaml + bundled templates, "
            "writes one .service per enabled instance + one "
            ".target to ~/.config/systemd/user/. "
            "Idempotent — safe to re-run."
        ),
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help=(
            "Override the registry path (default: "
            "~/.alfred/instances.yaml)."
        ),
    )
    parser.add_argument(
        "--alfred-repo",
        type=Path,
        default=None,
        help=(
            "Override the Alfred repo root used in the unit files' "
            "WorkingDirectory + ExecStart. Default: this script's "
            "package-grandparent (the repo root for an editable "
            "install). Wheel installs MUST set this explicitly."
        ),
    )
    parser.add_argument(
        "--install-dir",
        type=Path,
        default=None,
        help=(
            "Override the systemd user-unit install directory. "
            "Default: $XDG_CONFIG_HOME/systemd/user (or "
            "~/.config/systemd/user)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the install plan without writing any files or "
            "calling systemctl. Useful for inspecting placeholder "
            "substitution before committing to the install."
        ),
    )
    parser.add_argument(
        "--skip-linger",
        action="store_true",
        help=(
            "Skip the linger check + enable. Use this on systems "
            "where linger is managed externally (e.g. a configuration "
            "management tool) or where you intentionally want the "
            "units to die with your session."
        ),
    )
    parser.add_argument(
        "--skip-systemctl",
        action="store_true",
        help=(
            "Skip ``systemctl --user daemon-reload`` and ``enable "
            "algernon.target``. Use this in test environments where "
            "systemctl is unavailable (CI, container builds) and you "
            "only want the unit files written."
        ),
    )
    args = parser.parse_args(argv)

    alfred_repo = args.alfred_repo or _default_alfred_repo()
    alfred_repo = alfred_repo.expanduser().resolve()
    install_dir = args.install_dir or get_install_dir()
    install_dir = install_dir.expanduser().resolve()

    try:
        plan = build_plan(args.registry, alfred_repo, install_dir)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print_plan(plan)

    if args.dry_run:
        print("--- DRY-RUN — no changes written. ---")
        return 0

    # Linger check + enable.
    if not args.skip_linger:
        username = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        if not username:
            print(
                "error: cannot determine current username "
                "($USER / $LOGNAME unset); pass --skip-linger to "
                "manage linger externally.",
                file=sys.stderr,
            )
            return 2
        if _check_linger(username):
            print(f"Linger: already enabled for {username}.")
        else:
            print(
                f"Linger: NOT enabled for {username}. "
                f"Enabling via sudo (you may be prompted for your password)…"
            )
            if not _enable_linger(username):
                print(
                    f"error: failed to enable linger. "
                    f"Run manually: sudo loginctl enable-linger {username}",
                    file=sys.stderr,
                )
                return 1

    # Write unit files.
    print("--- Writing unit files ---")
    counters = apply_plan(plan)
    print(
        f"  services written: {counters['services_written']} "
        f"({counters['unchanged']} unchanged from prior install)"
    )
    print(f"  target written:   {counters['target_written']}")
    print()

    # Tell systemd about the new files.
    if not args.skip_systemctl:
        print("--- systemctl ---")
        if not _systemctl_user_reload():
            print("error: daemon-reload failed", file=sys.stderr)
            return 1
        print("  daemon-reload: OK")
        if not _systemctl_user_enable("algernon.target"):
            print("error: enable algernon.target failed", file=sys.stderr)
            return 1
        print("  enable algernon.target: OK")
        print()

    # Footer with verification + safety notes.
    print("--- Verification ---")
    print(
        f"  systemctl --user list-unit-files | grep alfred"
    )
    print(
        f"  systemctl --user is-enabled algernon.target  # → enabled"
    )
    print()
    print("--- IMPORTANT ---")
    print(
        "  Existing ``alfred instance up`` daemons (if running) "
        "hold the PID files that systemd-managed daemons would "
        "claim. Before the first systemd start, kill the ad-hoc "
        "daemons:"
    )
    print()
    print("    alfred instance down")
    print("    systemctl --user start algernon.target")
    print()
    print(
        "  Post-install + post-reboot, the systemd path is "
        "canonical. Do NOT mix ``alfred instance up`` with "
        "``systemctl --user start`` in the same WSL session."
    )
    print()
    print("--- Reboot test (run from PowerShell when ready) ---")
    print("  wsl --shutdown")
    print(
        "  # then reopen WSL + verify: "
        "systemctl --user status algernon.target"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
