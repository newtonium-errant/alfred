"""Install the STAY-C hardened sovereign systemd SYSTEM unit (#42, hardened #67).

STAY-C is a STANDALONE sovereign clinical scribe — NOT a row in
``~/.alfred/instances.yaml`` and NOT part of the salem/kal-le/hypatia
fan-out. This installer is therefore fully SEPARATE from
``install_systemd_units.py`` (the fan-out installer over the registry): it
reads NO ``instances.yaml``, never renders or touches ``algernon.target``,
and installs exactly ONE unit —
``/etc/systemd/system/alfred-stayc-clinical.service`` — that is
``WantedBy=multi-user.target`` only. Keeping the two installers independent is
the point: touching the fan-out installer can never affect STAY-C, and vice
versa (GROUND #6 byte-identity of the fan-out is protected structurally).

SYSTEM unit, not --user (#67 F6): the hardening (ProtectSystem=strict, capability
drops, namespaces) can ONLY be applied by a privileged system manager. A
``systemctl --user`` manager is unprivileged → status=218/CAPABILITIES
crash-loop that never boots. This installer therefore writes to
``/etc/systemd/system`` and drives the SYSTEM systemctl (``daemon-reload`` /
``enable``), which requires root — run it via ``sudo``. The unit runs as an
unprivileged ``User=``/``Group=`` (the operator), which the system manager drops
to AFTER applying the sandbox.

It mirrors the proven render → build_plan → apply_plan idempotency contract:

  * ``render_stayc_unit`` — pure placeholder substitution with a
    post-substitution sentinel check (``ValueError`` if any ``<...>``
    placeholder survives, so a template typo surfaces here rather than
    producing an invalid unit at install time).
  * ``build_plan`` — derive all deploy paths from a single ``<STAYC_ROOT>``
    (overridable per-path by flags), READ the deployed config for
    ``scribe.input_dir`` (#67 F4) + ``scribe.stt.model`` (#67 F3), read the
    bundled template, render. NO filesystem mutation, NO subprocess.
  * ``apply_plan`` — idempotent write-if-changed to the install dir.
  * ``verify_or_stage_model`` — verify the offline HF STT cache holds the
    configured model (#67 F3); optionally stage it from the operator's default
    cache; fail LOUD when missing so the daemon never boots into a silent
    offline model-not-found STT failure.

INERT IN REPO: the ``.service.template`` ships full of ``<STAYC_*>``
placeholders and this installer is NEVER invoked by ``alfred instance up``,
never referenced by ``instances.yaml``, never in ``algernon.target`` — nothing
about STAY-C activates from a plain checkout/deploy. It is operator-run, on-box,
and fully reversible (see the config header + the frozen spec's rollback).

Operator flow (after staging config + secrets + HF cache — see the config
example header), run via sudo with the STAY-C venv so the ExecStart python is
correct even if the running interpreter differs:

    sudo /data/algernon/stayc-clinical/.venv/bin/python \\
        -m alfred.scripts.install_stayc_unit --stage-model

Then:

    sudo systemctl start alfred-stayc-clinical.service
    journalctl -u alfred-stayc-clinical -f
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

import yaml

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

#: SYSTEM systemd install dir (#67 F6). A system manager (root) is the only
#: manager that can apply this unit's hardening, so the unit lives here, not in
#: ``$XDG_CONFIG_HOME/systemd/user``.
SYSTEM_INSTALL_DIR = Path("/etc/systemd/system")

#: Placeholders the template carries. Every one MUST be substituted; the
#: sentinel check in ``render_stayc_unit`` raises if any survives. A future
#: template that adds a placeholder without updating this set surfaces there.
_STAYC_PLACEHOLDERS: frozenset[str] = frozenset({
    "<STAYC_USER>",
    "<STAYC_GROUP>",
    "<STAYC_WORKDIR>",
    "<STAYC_PYTHON>",
    "<STAYC_CONFIG>",
    "<STAYC_SECRETS_ENV>",
    "<STAYC_HF_HOME>",
    "<STAYC_VAULT>",
    "<STAYC_DATA>",
    "<STAYC_INPUT_DIR>",
})

#: Generic residual-placeholder sweep — catches an UNKNOWN ``<UPPER_CASE>``
#: token the known-set above missed (belt on the sentinel).
_RESIDUAL_PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_]*>")


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class StaycInstallPlan:
    """What the installer will write. Populated by :func:`build_plan` (pure),
    consumed by :func:`apply_plan` (does the single write)."""
    stayc_root: Path
    install_dir: Path
    #: Identity the system manager drops to AFTER applying the sandbox (#67 F6).
    unit_user: str
    unit_group: str
    #: The STAY-C venv python that runs ExecStart (#67 F1) + the WorkingDirectory.
    python: Path
    workdir: Path
    #: Resolved per-path deploy targets (each overridable by a flag).
    config_path: Path
    secrets_env: Path
    hf_home: Path
    vault: Path
    data: Path
    #: Read from config.scribe.input_dir (#67 F4) — a ReadWritePaths root.
    input_dir: Path
    #: Read from config.scribe.stt.model (#67 F3) — drives the HF-cache check.
    stt_model: str
    unit_filename: str
    unit_content: str


# ---------------------------------------------------------------------------
# Pure helpers (no filesystem writes; no subprocess)
# ---------------------------------------------------------------------------


def get_install_dir() -> Path:
    """Resolve the systemd SYSTEM-unit install directory (#67 F6)."""
    return SYSTEM_INSTALL_DIR


def _load_scribe_config(config_path: Path) -> tuple[Path, str]:
    """Read ``(input_dir, stt_model)`` from the DEPLOYED STAY-C config.

    #67 F4: ReadWritePaths must include ``scribe.input_dir`` — on-box it is a
    SIBLING of the data dir, not under it, so the layout must be DERIVED from
    the config, never assumed. #67 F3: ``scribe.stt.model`` drives the offline
    HF-cache staging check. Fail LOUD if the config is missing/malformed or the
    fields are absent — the installer must never silently guess the layout.
    """
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FileNotFoundError(
            f"STAY-C config not readable at {config_path}: {exc}. Stage the "
            f"deployed config first (copy config.stayc-clinical.yaml.example → "
            f"there) or pass --config. The installer READS scribe.input_dir + "
            f"scribe.stt.model from it (#67 F3/F4) — it must not assume the layout."
        ) from exc
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(
            f"STAY-C config at {config_path} is not valid YAML: {exc}"
        ) from exc
    scribe = raw.get("scribe") if isinstance(raw, dict) else None
    scribe = scribe if isinstance(scribe, dict) else {}

    input_dir_raw = scribe.get("input_dir")
    if not input_dir_raw:
        raise ValueError(
            f"STAY-C config at {config_path} has no scribe.input_dir — required "
            f"to grant it write access under ProtectSystem=strict (#67 F4)."
        )
    input_dir = Path(str(input_dir_raw))
    if not input_dir.is_absolute():
        raise ValueError(
            f"scribe.input_dir must be an absolute path for a systemd "
            f"ReadWritePaths root; got {input_dir_raw!r} in {config_path}."
        )

    stt = scribe.get("stt") if isinstance(scribe.get("stt"), dict) else {}
    stt_model = stt.get("model")
    if not stt_model:
        raise ValueError(
            f"STAY-C config at {config_path} has no scribe.stt.model — required "
            f"to verify the offline HF model cache (#67 F3)."
        )
    return input_dir, str(stt_model)


def render_stayc_unit(
    template: str,
    *,
    unit_user: str,
    unit_group: str,
    python: Path,
    workdir: Path,
    config_path: Path,
    secrets_env: Path,
    hf_home: Path,
    vault: Path,
    data: Path,
    input_dir: Path,
) -> str:
    """Substitute the STAY-C placeholders into the unit template.

    Raises ``ValueError`` if any known placeholder — or any residual
    ``<UPPER_CASE>`` token — survives the pass, so a template typo or an
    un-mapped placeholder never produces an invalid unit at install time.
    """
    rendered = (
        template
        .replace("<STAYC_USER>", unit_user)
        .replace("<STAYC_GROUP>", unit_group)
        .replace("<STAYC_PYTHON>", str(python))
        .replace("<STAYC_WORKDIR>", str(workdir))
        .replace("<STAYC_CONFIG>", str(config_path))
        .replace("<STAYC_SECRETS_ENV>", str(secrets_env))
        .replace("<STAYC_HF_HOME>", str(hf_home))
        .replace("<STAYC_VAULT>", str(vault))
        .replace("<STAYC_DATA>", str(data))
        .replace("<STAYC_INPUT_DIR>", str(input_dir))
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
    stayc_root: Path,
    install_dir: Path,
    unit_user: str,
    unit_group: str,
    python: Path | None = None,
    config_path: Path | None = None,
    secrets_env: Path | None = None,
    hf_home: Path | None = None,
    vault: Path | None = None,
    data: Path | None = None,
    input_dir: Path | None = None,
    stt_model: str | None = None,
) -> StaycInstallPlan:
    """Derive deploy paths from ``stayc_root``, read the config, render the unit.

    Every per-path argument defaults to the canonical layout under
    ``stayc_root`` but is independently overridable (e.g. an HF cache on a
    different volume). ``input_dir`` + ``stt_model`` default to the DEPLOYED
    config's ``scribe.input_dir`` / ``scribe.stt.model`` (#67 F3/F4) — read once
    unless both are supplied explicitly. Reads the bundled template + the
    deployed config only — no ``instances.yaml``, no ``algernon.target``. NO FS
    mutation, NO subprocess.
    """
    config_path = config_path or (stayc_root / "config.stayc-clinical.yaml")
    secrets_env = secrets_env or (stayc_root / "secrets" / "scribe.env")
    hf_home = hf_home or (stayc_root / "models" / "hf")
    vault = vault or (stayc_root / "vault")
    data = data or (stayc_root / "data")
    # #67 F1: the ExecStart python is the STAY-C OWN venv (faster-whisper lives
    # there), decoupled from wherever this installer module happens to live.
    python = python or (stayc_root / ".venv" / "bin" / "python")

    # #67 F3/F4: input_dir + stt_model come from the DEPLOYED config unless
    # BOTH are overridden (tests inject them to avoid reading a live config).
    if input_dir is None or stt_model is None:
        cfg_input_dir, cfg_stt_model = _load_scribe_config(config_path)
        input_dir = input_dir if input_dir is not None else cfg_input_dir
        stt_model = stt_model if stt_model is not None else cfg_stt_model

    template_path = get_systemd_dir() / STAYC_TEMPLATE_FILENAME
    if not template_path.is_file():
        raise FileNotFoundError(
            f"Bundled STAY-C unit template missing at {template_path}. "
            f"Reinstall alfred or check the wheel/sdist contents."
        )
    template = template_path.read_text(encoding="utf-8")

    unit_content = render_stayc_unit(
        template,
        unit_user=unit_user,
        unit_group=unit_group,
        python=python,
        workdir=stayc_root,
        config_path=config_path,
        secrets_env=secrets_env,
        hf_home=hf_home,
        vault=vault,
        data=data,
        input_dir=input_dir,
    )

    return StaycInstallPlan(
        stayc_root=stayc_root,
        install_dir=install_dir,
        unit_user=unit_user,
        unit_group=unit_group,
        python=python,
        workdir=stayc_root,
        config_path=config_path,
        secrets_env=secrets_env,
        hf_home=hf_home,
        vault=vault,
        data=data,
        input_dir=input_dir,
        stt_model=stt_model,
        unit_filename=STAYC_UNIT_FILENAME,
        unit_content=unit_content,
    )


# ---------------------------------------------------------------------------
# STT model staging (#67 F3)
# ---------------------------------------------------------------------------


def hf_model_cache_dirname(model: str) -> str | None:
    """Map a bare faster-whisper model id to its HF hub cache dir name.

    faster-whisper resolves bare ids to Systran repos and huggingface_hub caches
    a repo ``org/name`` as ``models--org--name``::

        distil-large-v3 -> Systran/faster-distil-whisper-large-v3
                        -> models--Systran--faster-distil-whisper-large-v3
        large-v3        -> Systran/faster-whisper-large-v3
                        -> models--Systran--faster-whisper-large-v3

    Returns ``None`` when ``model`` is an explicit repo id or a filesystem path
    (contains a ``/``), where staging-by-name does not apply — the operator
    manages that cache/path directly.
    """
    if "/" in model:
        return None
    if model.startswith("distil-"):
        repo = f"Systran/faster-distil-whisper-{model[len('distil-'):]}"
    else:
        repo = f"Systran/faster-whisper-{model}"
    return "models--" + repo.replace("/", "--")


def model_cache_target(plan: StaycInstallPlan) -> Path | None:
    """Expected offline HF cache dir for the plan's STT model, or ``None`` if
    the model is an explicit repo/path (operator-managed)."""
    sub = hf_model_cache_dirname(plan.stt_model)
    if sub is None:
        return None
    return plan.hf_home / "hub" / sub


def _default_hf_source_cache(unit_user: str) -> Path | None:
    """The operator's default HF hub cache to stage FROM — the unit user's
    ``~/.cache/huggingface/hub``.

    Resolved via ``pwd`` (the unit user's real home) so it is correct even under
    ``sudo``, where ``$HOME`` is root's. ``None`` if the user can't be resolved.
    """
    try:
        home = Path(pwd.getpwnam(unit_user).pw_dir)
    except (KeyError, AttributeError):
        return None
    return home / ".cache" / "huggingface" / "hub"


def _chown_tree_to_unit_user(target: Path, unit_user: str) -> None:
    """When staging as root (sudo), the copied tree is root-owned; the unit runs
    as the unprivileged ``unit_user``, so hand ownership over or STT can't read
    it. Best-effort — a failure surfaces at the next verify / boot."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    try:
        pw = pwd.getpwnam(unit_user)
    except (KeyError, AttributeError):
        return
    try:
        os.chown(target, pw.pw_uid, pw.pw_gid)
        for root_dir, dirs, files in os.walk(target):
            for name in dirs + files:
                os.chown(os.path.join(root_dir, name), pw.pw_uid, pw.pw_gid)
    except OSError:
        pass


def verify_or_stage_model(
    plan: StaycInstallPlan,
    *,
    stage: bool,
    source_cache: Path | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Verify the STT model exists at the relocated offline HF cache; optionally
    stage it from the operator's default cache (#67 F3).

    Returns a status dict (``status`` ∈ {present, staged, would-stage, skipped}).
    Raises ``RuntimeError`` (fail-loud) when the model is missing and cannot or
    must-not be staged — the installer aborts rather than let the daemon boot
    into a silent offline model-not-found STT failure. In ``dry_run`` mode it
    never copies (returns ``would-stage`` / still raises so the caller can
    report the gap without mutating anything).
    """
    target = model_cache_target(plan)
    if target is None:
        return {
            "status": "skipped",
            "reason": "model is an explicit repo/path (operator-managed)",
            "model": plan.stt_model,
        }
    if target.is_dir():
        return {"status": "present", "target": target, "model": plan.stt_model}

    # Missing at the relocated offline cache.
    src = source_cache if source_cache is not None else _default_hf_source_cache(plan.unit_user)
    src_dir = (src / target.name) if src is not None else None
    if stage and src_dir is not None and src_dir.is_dir():
        if dry_run:
            return {
                "status": "would-stage",
                "source": src_dir,
                "target": target,
                "model": plan.stt_model,
            }
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_dir, target)
        _chown_tree_to_unit_user(target, plan.unit_user)
        return {
            "status": "staged",
            "source": src_dir,
            "target": target,
            "model": plan.stt_model,
        }

    # Cannot stage — fail loud with a precise, copy-pasteable instruction.
    hint_src = str(src_dir) if src_dir is not None else (
        f"<operator HF cache>/hub/{target.name}"
    )
    raise RuntimeError(
        f"STT model {plan.stt_model!r} is NOT present at the offline HF cache "
        f"{target}, and the unit sets HF_HUB_OFFLINE=1 — the scribe would fail "
        f"STT at runtime with a silent model-not-found. Stage it: "
        f"cp -a {hint_src} {target}   (or re-run with --stage-model once the "
        f"model is in the operator's default cache, or pass --skip-model-check "
        f"to bypass this guard deliberately)."
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


def _systemctl_reload() -> bool:
    """Run ``systemctl daemon-reload`` (SYSTEM manager). Returns True on success.

    #67 F6: SYSTEM, not ``--user`` — a --user manager cannot own this hardened
    unit. Requires root (the installer is run via sudo)."""
    if shutil.which("systemctl") is None:
        print(
            "error: systemctl is not available — is systemd installed?",
            file=sys.stderr,
        )
        return False
    try:
        proc = subprocess.run(
            ["systemctl", "daemon-reload"],
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


def _systemctl_enable(unit_name: str) -> bool:
    """Run ``systemctl enable <unit>`` (SYSTEM manager). Returns True on success."""
    if shutil.which("systemctl") is None:
        return False
    try:
        proc = subprocess.run(
            ["systemctl", "enable", unit_name],
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
    print("STAY-C sovereign systemd installer plan (SYSTEM unit)")
    print(f"  STAY-C root: {plan.stayc_root}")
    print(f"  Runs as:     User={plan.unit_user} Group={plan.unit_group}")
    print(f"  Python:      {plan.python}")
    print(f"  WorkingDir:  {plan.workdir}")
    print(f"  Install dir: {plan.install_dir}")
    print()
    print("--- Standalone unit (NOT in the algernon fan-out) ---")
    print(f"  {plan.unit_filename}  (WantedBy=multi-user.target only)")
    print(f"    config:    {plan.config_path}")
    print(f"    secrets:   {plan.secrets_env}  (optional EnvironmentFile)")
    print(f"    HF cache:  {plan.hf_home}")
    print(f"    vault:     {plan.vault}")
    print(f"    data:      {plan.data}")
    print(f"    input_dir: {plan.input_dir}  (from config; a ReadWritePaths root)")
    print(f"    STT model: {plan.stt_model}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_unit_user() -> str:
    """The user the SYSTEM unit runs as.

    Under ``sudo``, ``$SUDO_USER`` is the invoking (unprivileged) operator — the
    correct ``User=`` target (``getpass.getuser()`` / ``$USER`` would return
    ``root`` under sudo). Falls back to ``$USER`` / ``$LOGNAME`` when not under
    sudo.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        return sudo_user
    return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


def _default_unit_group(user: str) -> str:
    """The unit's ``Group=`` — the user's primary group name, falling back to
    the username (single-user boxes name the primary group after the user)."""
    try:
        gid = pwd.getpwnam(user).pw_gid
        return grp.getgrgid(gid).gr_name
    except (KeyError, AttributeError):
        return user


def _geteuid() -> int:
    """Effective uid, or 0 where ``os.geteuid`` is unavailable (non-POSIX)."""
    return os.geteuid() if hasattr(os, "geteuid") else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Install the STAY-C hardened sovereign systemd SYSTEM unit. "
            "Standalone — reads NO instances.yaml, never touches "
            "algernon.target. Writes exactly one "
            f"{STAYC_UNIT_FILENAME} to /etc/systemd/system/ and drives the "
            "SYSTEM systemctl (requires root — run via sudo). Idempotent — "
            "safe to re-run."
        ),
    )
    parser.add_argument(
        "--stayc-root",
        type=Path,
        default=DEFAULT_STAYC_ROOT,
        help=(
            "Deploy root every STAY-C path derives from "
            f"(default: {DEFAULT_STAYC_ROOT}). Override per-path with the "
            "--config/--secrets-env/--hf-home/--vault/--data/--python flags."
        ),
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=None,
        help=(
            "Override the ExecStart python (the STAY-C venv where faster-whisper "
            "lives). Default: <stayc-root>/.venv/bin/python. Decoupled from the "
            "interpreter running this installer (#67 F1)."
        ),
    )
    parser.add_argument(
        "--unit-user",
        default=None,
        help=(
            "User the SYSTEM unit runs as (systemd drops to it AFTER applying "
            "the sandbox). Default: $SUDO_USER (the operator running sudo), else "
            "$USER/$LOGNAME."
        ),
    )
    parser.add_argument(
        "--unit-group",
        default=None,
        help="Group the SYSTEM unit runs as. Default: the unit user's primary group.",
    )
    parser.add_argument(
        "--install-dir",
        type=Path,
        default=None,
        help=(
            "Override the systemd SYSTEM-unit install directory. Default: "
            f"{SYSTEM_INSTALL_DIR} (#67 F6 — a --user dir cannot apply the hardening)."
        ),
    )
    parser.add_argument("--config", type=Path, default=None, help="Override the config path (default: <stayc-root>/config.stayc-clinical.yaml). READ for scribe.input_dir + scribe.stt.model.")
    parser.add_argument("--secrets-env", type=Path, default=None, help="Override the salt-only EnvironmentFile (default: <stayc-root>/secrets/scribe.env). Optional at runtime — a missing file is ignored.")
    parser.add_argument("--hf-home", type=Path, default=None, help="Override HF_HOME / STT cache (default: <stayc-root>/models/hf).")
    parser.add_argument("--vault", type=Path, default=None, help="Override the PHI vault path (default: <stayc-root>/vault).")
    parser.add_argument("--data", type=Path, default=None, help="Override the data dir — logs/audit/encounters/pid (default: <stayc-root>/data).")
    parser.add_argument("--input-dir", type=Path, default=None, help="Override scribe.input_dir (default: read from the config). Added to ReadWritePaths (#67 F4).")
    parser.add_argument("--stt-model", default=None, help="Override scribe.stt.model (default: read from the config). Drives the offline HF-cache check (#67 F3).")
    parser.add_argument(
        "--stage-model",
        action="store_true",
        help=(
            "If the STT model is missing at the relocated offline HF cache, copy "
            "it from the operator's default ~/.cache/huggingface/hub (#67 F3)."
        ),
    )
    parser.add_argument(
        "--hf-source-cache",
        type=Path,
        default=None,
        help="Override the HF hub cache to stage the model FROM (default: the unit user's ~/.cache/huggingface/hub).",
    )
    parser.add_argument(
        "--skip-model-check",
        action="store_true",
        help="Skip the offline STT-model presence check + staging (#67 F3) deliberately.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan + model-check without writing files, copying, or calling systemctl.",
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

    stayc_root = args.stayc_root.expanduser()
    install_dir = (args.install_dir or get_install_dir()).expanduser().resolve()
    python = args.python.expanduser() if args.python else None

    unit_user = args.unit_user or _default_unit_user()
    if not unit_user:
        print(
            "error: cannot determine the unit user ($SUDO_USER / $USER / "
            "$LOGNAME unset); pass --unit-user explicitly.",
            file=sys.stderr,
        )
        return 2
    if unit_user == "root":
        # PHI-safety: running the sovereign clinical scribe as root defeats the
        # whole point of the User=/Group= privilege drop. This fires on a
        # root-DIRECT install (no sudo → $SUDO_USER unset, $USER=root → the
        # default resolves to root), or an explicit --unit-user root.
        print(
            "error: refusing to render User=root — running the sovereign PHI "
            "scribe as root defeats the systemd privilege drop. Run the "
            "installer via sudo AS THE OPERATOR (so $SUDO_USER is the "
            "unprivileged user), or pass --unit-user <name> explicitly.",
            file=sys.stderr,
        )
        return 2
    unit_group = args.unit_group or _default_unit_group(unit_user)

    try:
        plan = build_plan(
            stayc_root=stayc_root,
            install_dir=install_dir,
            unit_user=unit_user,
            unit_group=unit_group,
            python=python,
            config_path=args.config,
            secrets_env=args.secrets_env,
            hf_home=args.hf_home,
            vault=args.vault,
            data=args.data,
            input_dir=args.input_dir,
            stt_model=args.stt_model,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print_plan(plan)

    # #67 F3: verify (and optionally stage) the offline STT model BEFORE we
    # touch /etc/systemd/system, so a missing model aborts LOUD rather than
    # enabling a unit that fails STT silently at runtime.
    if not args.skip_model_check:
        print("--- STT model (offline HF cache) ---")
        try:
            status = verify_or_stage_model(
                plan,
                stage=args.stage_model,
                source_cache=args.hf_source_cache,
                dry_run=args.dry_run,
            )
        except RuntimeError as exc:
            if args.dry_run:
                print(f"  WOULD FAIL: {exc}")
            else:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        else:
            detail = status.get("target") or status.get("reason") or ""
            print(f"  {status['status']}: model={status['model']}  {detail}")
        print()

    if args.dry_run:
        print("--- DRY-RUN — no changes written. ---")
        return 0

    # #67 F6: installing to the SYSTEM dir + driving the SYSTEM systemctl needs
    # root — fail loud early with the canonical sudo invocation.
    needs_root = install_dir == SYSTEM_INSTALL_DIR or not args.skip_systemctl
    if needs_root and _geteuid() != 0:
        print(
            "error: installing a SYSTEM unit to /etc/systemd/system + running "
            "the system systemctl requires root. Re-run via sudo, e.g.:\n"
            f"  sudo {plan.python} -m alfred.scripts.install_stayc_unit --stage-model",
            file=sys.stderr,
        )
        return 2

    print("--- Writing unit file ---")
    counters = apply_plan(plan)
    print(
        f"  {plan.unit_filename}: "
        f"{'written' if counters['written'] else 'unchanged from prior install'}"
    )
    print()

    if not args.skip_systemctl:
        print("--- systemctl (system) ---")
        if not _systemctl_reload():
            print("error: daemon-reload failed", file=sys.stderr)
            return 1
        print("  daemon-reload: OK")
        if not _systemctl_enable(plan.unit_filename):
            print(f"error: enable {plan.unit_filename} failed", file=sys.stderr)
            return 1
        print(f"  enable {plan.unit_filename}: OK")
        print()

    unit_stem = plan.unit_filename.removesuffix(".service")
    print("--- Start + verify ---")
    print(f"  sudo systemctl start {plan.unit_filename}")
    print(f"  journalctl -u {unit_stem} -f")
    print(
        "  # expect scribe.daemon.up + scribe.egress_firewall.enforced OR "
        ".unverified (unverified is EXPECTED + fine on a WSL2 kernel lacking "
        "cgroup-v2 BPF — the Python guard remains the verified control)"
    )
    print()
    print("--- Rollback ---")
    print(
        f"  sudo systemctl disable --now {plan.unit_filename} && "
        f"sudo rm {plan.install_dir / plan.unit_filename} && "
        f"sudo systemctl daemon-reload"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
