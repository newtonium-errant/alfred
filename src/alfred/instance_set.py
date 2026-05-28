"""Algernon instance-set orchestrator — up/down/status across all instances.

Phase 1 (2026-05-28) of the Algernon platform wrapper. One operator
verb fans out across every enabled instance in the registry:

  * ``alfred instance up`` — start every enabled instance
  * ``alfred instance down`` — stop every enabled instance
  * ``alfred instance status`` — report running state per instance

Each verb shells out to ``python -m alfred --config <X> <verb>`` per
instance (subprocess invocation, NOT direct library calls — the
existing daemon spawn/stop logic stays canonical at the per-instance
level; this wrapper layers fan-out on top).

Registry: ``~/.alfred/instances.yaml`` — operator-editable, ships with
the three current instances (Salem, KAL-LE, Hypatia) pre-populated.
Each entry carries ``name`` (slug), ``display`` (operator-facing),
``config`` (path to per-instance config.yaml), and ``enabled``.
``enabled: false`` drains the instance from the fan-out without
deleting its config.

Per ``feedback_intentionally_left_blank.md``: every verb emits a
summary sentinel — ``instance up: 3/3 OK`` is the canary that says
"ran, here's the count." Per-line distinguishers (``already-running``
vs ``started``, ``stopped`` vs ``was not running``) let the operator
see the exact state distribution across instances.

Per the ratified Phase 1 decision (2026-05-28): ``already-running``
counts as OK on ``up`` — idempotent re-runs are safe and read as
success. The pre-check on PID file presence (via the per-instance
config's ``daemon.pid_path`` or default ``logging.dir/alfred.pid``)
avoids the subprocess-spawn-then-detect-error round-trip the naïve
shape would otherwise produce.

Subprocess invocation form: ``[sys.executable, "-m", "alfred",
"--config", instance.config, verb]``. Same module path as the tier
migration script after the 2026-05-28 silent-CLI-failure incident
(``-m alfred`` dispatches via ``src/alfred/__main__.py`` →
``cli.main()``). ``-m alfred.cli`` is the broken shape and must
never be used.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import yaml


# Canonical default registry path. The operator can override per-call
# via ``load_registry(path=...)``; tests pass an explicit path so a
# user's real registry doesn't pollute the test environment.
DEFAULT_REGISTRY_PATH = Path.home() / ".alfred" / "instances.yaml"


@dataclass
class Instance:
    """One row of the instance registry.

    Fields:
      * ``name`` — short slug, lowercase. Operator types this if a
        future verb takes a per-instance argument (out of scope for
        Phase 1; reserved for Phase 2 ``restart-all --only X``).
      * ``display`` — operator-facing display name. Preserves
        capitalization + non-alphanumeric formatting (``KAL-LE``).
      * ``config`` — path to the instance's config.yaml. Resolved
        relative to the current working directory at run-time
        (matches the existing ``--config`` flag's resolution).
      * ``enabled`` — True if this instance participates in
        instance-set verbs. ``False`` to drain without deleting
        the row (e.g. a wedged instance the operator wants to
        skip until it's diagnosed).
    """
    name: str
    display: str
    config: str
    enabled: bool = True


def load_registry(path: Path | None = None) -> list[Instance]:
    """Load the instance registry from ``path`` (default:
    ``~/.alfred/instances.yaml``).

    Returns a list of ``Instance`` objects in the order they appear
    in the YAML. The CLI handlers preserve this ordering when
    rendering per-instance lines so the operator sees a stable
    layout across runs.

    Raises:
      * ``FileNotFoundError`` (re-raised with a clear message) if
        the registry doesn't exist. The operator-facing CLI catches
        this and surfaces an actionable error.
      * ``ValueError`` if the YAML is malformed at a structural
        level (missing ``instances`` key, or an instance entry
        missing one of the required fields).
    """
    target = path or DEFAULT_REGISTRY_PATH
    if not target.exists():
        raise FileNotFoundError(
            f"Instance registry not found at {target}. "
            f"Create it (see Phase 1 docs) or run with --registry "
            f"pointing at a valid path."
        )
    with open(target, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    instances_raw = raw.get("instances")
    if instances_raw is None:
        raise ValueError(
            f"Registry at {target} is missing the top-level "
            f"``instances:`` key."
        )
    if not isinstance(instances_raw, list):
        raise ValueError(
            f"Registry at {target}: ``instances`` must be a list, "
            f"got {type(instances_raw).__name__}."
        )

    instances: list[Instance] = []
    for idx, entry in enumerate(instances_raw):
        if not isinstance(entry, dict):
            raise ValueError(
                f"Registry at {target}: instance entry #{idx} is not "
                f"a dict (got {type(entry).__name__})."
            )
        for required in ("name", "display", "config"):
            if required not in entry:
                raise ValueError(
                    f"Registry at {target}: instance entry #{idx} "
                    f"missing required field ``{required}``. "
                    f"Got keys: {sorted(entry.keys())}."
                )
        instances.append(Instance(
            name=str(entry["name"]),
            display=str(entry["display"]),
            config=str(entry["config"]),
            enabled=bool(entry.get("enabled", True)),
        ))
    return instances


def iter_enabled(instances: list[Instance]) -> Iterator[Instance]:
    """Yield only instances where ``enabled is True``.

    Separated from ``load_registry`` so the CLI can distinguish
    ``X/Y enabled`` from ``X/Y registered`` in status output if
    we ever want that distinction. Phase 1 just uses the
    enabled-subset for fan-out.
    """
    for inst in instances:
        if inst.enabled:
            yield inst


def _resolve_pid_path_for_config(config_path: str) -> Path | None:
    """Resolve the PID-file path for an instance's config.

    Mirrors ``cli._resolve_pid_path`` priority:
      1. ``daemon.pid_path`` explicit override
      2. ``logging.dir`` + ``alfred.pid``

    Returns ``None`` if the config can't be loaded (file missing,
    YAML parse failure). The caller treats ``None`` as "can't
    pre-check; defer to subprocess" — a config that can't be loaded
    means subprocess invocation will also fail, surfacing the real
    error to the operator.
    """
    try:
        path = Path(config_path)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None

    daemon_cfg = raw.get("daemon") or {}
    explicit = daemon_cfg.get("pid_path")
    if explicit:
        return Path(explicit)
    log_cfg = raw.get("logging") or {}
    log_dir = log_cfg.get("dir", "./data")
    return Path(log_dir) / "alfred.pid"


def check_running(instance: Instance) -> int | None:
    """Return the running PID for ``instance``, or None if not running.

    Defers to the canonical ``alfred.daemon.check_already_running``
    helper so the PID-resolution + stale-PID cleanup logic stays in
    one place (the operator sees the same "stale PID found, cleaned
    up" behavior whether they invoke ``alfred status`` per instance
    or ``alfred instance status`` across all of them).
    """
    pid_path = _resolve_pid_path_for_config(instance.config)
    if pid_path is None:
        return None
    from alfred.daemon import check_already_running
    return check_already_running(pid_path)


def _build_subprocess_cmd(
    instance: Instance, verb: str, extra_args: list[str],
) -> list[str]:
    """Build the canonical subprocess command for ``instance`` + ``verb``.

    Form: ``[sys.executable, "-m", "alfred", "--config", <X>, <verb>, *extra_args]``.

    The module path is ``alfred`` (NOT ``alfred.cli``). The
    canonical-reproducible form dispatches via
    ``src/alfred/__main__.py`` which calls ``cli.main()``. The
    ``alfred.cli`` shape silently no-ops because the module has no
    ``if __name__ == "__main__"`` guard — verified by the 2026-05-28
    tier migration script incident.
    """
    return [
        sys.executable, "-m", "alfred",
        "--config", instance.config,
        verb, *extra_args,
    ]


def run_verb(
    instance: Instance, verb: str, extra_args: list[str] | None = None,
) -> tuple[int, str]:
    """Execute ``verb`` against ``instance`` via subprocess.

    Returns ``(returncode, summary_line)``:
      * ``returncode`` is the subprocess exit code (0 on success;
        non-zero on failure).
      * ``summary_line`` is one operator-facing line shaped like
        ``"<Display>: <state>"`` for the verb's normal outcome, or
        ``"<Display>: FAILED — <short stderr>"`` on failure.

    Per the ratified Phase 1 idempotency decision: ``up`` against an
    already-running instance returns ``(0, "<Display>: already-running
    (pid X)")`` WITHOUT invoking the subprocess. The pre-check on PID
    file presence is the canonical idempotency gate; subprocess
    invocation would otherwise produce a non-zero exit and a
    "Alfred is already running" stderr line the wrapper would have
    to parse. Pre-check is cleaner + faster + avoids subprocess
    spawn overhead.

    ``down`` and ``status`` always shell out — both are safe to
    invoke against an idle instance (``down`` reports "not running",
    ``status`` reports the same).

    The summary string is the operator-facing CONTRACT — pinned by
    tests (no per-line shape drift across refactors).
    """
    extra_args = extra_args or []

    # Idempotency pre-check for ``up`` — already-running counts as OK
    # per the ratified Phase 1 decision. Skip the subprocess spawn.
    if verb == "up":
        existing_pid = check_running(instance)
        if existing_pid is not None:
            return (
                0,
                f"{instance.display}: already-running (pid {existing_pid})",
            )

    cmd = _build_subprocess_cmd(instance, verb, extra_args)
    # Subprocess timeout — bounds the wrapper against a wedged
    # config-load on one instance hanging the whole fan-out. 30s is
    # generous for the normal case: ``up`` daemonizes + returns in
    # <1s, ``down``/``status`` reads PID file + returns in <1s. A
    # timeout firing means something is genuinely wrong (corrupt
    # config, locked file, network call inside config load) — the
    # operator wants to know NOW rather than wait indefinitely. The
    # best-effort fan-out shape continues to the next instance after
    # this one's timeout summary is returned.
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        # Distinct from the FAILED line — the operator-grep target
        # for "which instance is wedged?" workflows. Carries the
        # configured timeout value + config path so the operator can
        # diagnose without re-running with --verbose.
        return (
            1,
            f"{instance.display}: TIMEOUT "
            f"(30s wedge — check {instance.config})",
        )

    if proc.returncode != 0:
        # Failure path — short stderr excerpt for the summary line.
        # Per builder.md "Subprocess Failure Logging": surface
        # enough detail that the operator can diagnose without
        # re-running with --verbose.
        stderr = (proc.stderr or "").strip()
        first_line = stderr.splitlines()[0] if stderr else f"exit code {proc.returncode}"
        # Truncate so the summary stays one operator-facing line.
        if len(first_line) > 200:
            first_line = first_line[:200] + "..."
        return (
            proc.returncode,
            f"{instance.display}: FAILED — {first_line}",
        )

    # Success path. Discriminate the outcome based on verb.
    if verb == "up":
        # Subprocess succeeded; we know we pre-checked and there was
        # no existing PID, so this is a fresh start. Try to extract
        # the started PID by re-reading the PID file post-spawn.
        pid = check_running(instance)
        pid_str = f" (pid {pid})" if pid is not None else ""
        return (0, f"{instance.display}: started{pid_str}")

    if verb == "down":
        # Distinguish "stopped" (running → not running) from "was not
        # running" (idle → no change). The cmd_down handler prints
        # "Alfred stopped." or "Alfred is not running." on stdout
        # for those two cases; parse to disambiguate.
        stdout = (proc.stdout or "").lower()
        if "is not running" in stdout:
            return (0, f"{instance.display}: was not running")
        return (0, f"{instance.display}: stopped")

    if verb == "status":
        # Status returns a multi-line summary; reduce to a one-line
        # operator-facing form: "<Display>: <state> [(pid X)]
        # [vault=<path>]". Defer the full multi-line aggregation
        # to the --verbose branch in the CLI handler.
        pid = check_running(instance)
        if pid is None:
            return (0, f"{instance.display}: stopped")
        vault_path = _extract_vault_path(instance.config)
        vault_str = f"  vault={vault_path}" if vault_path else ""
        return (
            0,
            f"{instance.display}: running (pid {pid}){vault_str}",
        )

    # Unknown verb — pass through with a generic success line. The
    # caller is responsible for restricting verbs to the supported
    # set (the CLI handlers only call up/down/status).
    return (0, f"{instance.display}: {verb} OK")


def _extract_vault_path(config_path: str) -> str | None:
    """Pull the vault path from an instance's config (best effort).

    Returns ``None`` if the config can't be loaded or doesn't carry
    a ``vault.path`` key. Used by the ``status`` summary line so
    the operator can see at a glance which vault each instance is
    serving.
    """
    try:
        path = Path(config_path)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None
    vault_cfg = raw.get("vault") or {}
    vp = vault_cfg.get("path")
    return str(vp) if vp else None


def run_verb_across_set(
    instances: list[Instance], verb: str,
    extra_args: list[str] | None = None,
) -> tuple[list[tuple[int, str]], int]:
    """Run ``verb`` against every enabled instance in ``instances``.

    Returns ``(per_instance_results, exit_code)``:
      * ``per_instance_results`` is a list of ``(returncode, summary)``
        tuples, one per enabled instance, in registry order.
      * ``exit_code`` is 0 if every instance succeeded, 1 if any
        failed. Per the ratified Phase 1 decision, ``already-running``
        on ``up`` counts as success (returncode 0).

    Each instance runs sequentially (no parallel fan-out in Phase 1
    — the operator wants deterministic ordering and clear per-line
    output; parallel daemon-spawn is a Phase 2 candidate if startup
    time becomes a real friction).

    Per ``feedback_intentionally_left_blank.md``: the caller is
    responsible for emitting the summary sentinel
    (``instance <verb>: N/M OK``) even when every instance succeeded
    — the count itself is the "ran, here's what happened" signal.
    """
    enabled = list(iter_enabled(instances))
    results: list[tuple[int, str]] = []
    any_failed = False
    for inst in enabled:
        rc, summary = run_verb(inst, verb, extra_args)
        results.append((rc, summary))
        if rc != 0:
            any_failed = True
    return results, (1 if any_failed else 0)


def format_summary_sentinel(
    verb: str, results: list[tuple[int, str]],
) -> str:
    """Build the summary sentinel line for the operator.

    Shape:
      * All-OK: ``"instance <verb>: <N>/<N> OK"``
      * Partial: ``"instance <verb>: <ok>/<N> OK — <failed names>"``

    For status: ``"instance status: <running>/<N> running"``.

    Per ``feedback_intentionally_left_blank.md``: this line ALWAYS
    fires after the per-instance fan-out. Even an all-success state
    needs the explicit ``N/N OK`` signal so the operator sees the
    count and knows the wrapper completed.
    """
    total = len(results)

    if verb == "status":
        running = sum(
            1 for rc, summary in results
            if rc == 0 and "running" in summary and "FAILED" not in summary
        )
        return f"instance status: {running}/{total} running"

    ok = sum(1 for rc, _ in results if rc == 0)
    if ok == total:
        return f"instance {verb}: {ok}/{total} OK"
    # Partial — name the failed instances. Extract display name from
    # the FAILED summary line (which is shaped "<Display>: FAILED — ...").
    failed_names: list[str] = []
    for rc, summary in results:
        if rc != 0 and ": FAILED" in summary:
            # "<Display>: FAILED — <detail>" → "<Display>"
            failed_names.append(summary.split(":", 1)[0])
    failed_str = ", ".join(failed_names) if failed_names else "see per-line output"
    return f"instance {verb}: {ok}/{total} OK — {failed_str} failed"


# ---------------------------------------------------------------------------
# Starter registry — used by quickstart / first-run / docs.
# ---------------------------------------------------------------------------
#
# Shipped as the Phase 1 default so the registry file can be created
# without forcing the operator to hand-author YAML. The CLI handlers
# DO NOT auto-create the registry — that's an explicit operator
# action via ``alfred instance scaffold-registry`` (out of scope for
# Phase 1; flagged for Phase 2). For now we ship the starter content
# in the project root as ``instances.yaml.example`` and document the
# one-shot copy ``cp instances.yaml.example ~/.alfred/instances.yaml``
# in the help text.

STARTER_REGISTRY_YAML = """\
# Algernon instance registry — fan-out target for
# ``alfred instance up | down | status``.
#
# Each entry needs ``name`` (lowercase slug), ``display`` (operator-
# facing name), and ``config`` (path to the instance's config.yaml).
# Set ``enabled: false`` to drain an instance from fan-out without
# deleting the row.
#
# Config paths are relative by convention — they resolve against the
# cwd at the moment ``alfred instance ...`` runs. Typical launch:
# ``cd <project-root> && alfred instance up``. Operators with a
# non-cwd launch convention should rewrite these as absolute paths.
#
# Order is preserved in operator-facing output, so put the most-
# critical instance first.

instances:
  - name: salem
    display: Salem
    config: ./config.yaml
    enabled: true
  - name: kal-le
    display: KAL-LE
    config: ./config.kalle.yaml
    enabled: true
  - name: hypatia
    display: Hypatia
    config: ./config.hypatia.yaml
    enabled: true
"""


__all__ = [
    "DEFAULT_REGISTRY_PATH",
    "Instance",
    "STARTER_REGISTRY_YAML",
    "check_running",
    "format_summary_sentinel",
    "iter_enabled",
    "load_registry",
    "run_verb",
    "run_verb_across_set",
]
