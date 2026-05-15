"""Cloudflared supervised-subprocess daemon (Pattern A — thin Python wrapper).

The orchestrator forks this as the ``_run_cloudflared`` entry point.
We spawn the ``cloudflared`` Go binary as a child subprocess with
stdout/stderr piped to a flat log file, block on ``proc.wait()``, then
exit with the child's exit code. The orchestrator's auto-restart loop
(max 5 retries) then applies naturally — same as every other daemon.

Why Pattern A (not a separate supervisor process / not exec-replace):

1. **Python parent process needed for structured logging.** We want
   ``cloudflared.started`` / ``cloudflared.exited`` events in the
   structlog stream so an operator grepping ``data/alfred.log`` can
   confirm tunnel-spawn lifecycle without reading the binary's
   stdout. ``os.execvp`` would replace the Python process with the
   Go binary and lose the structlog handle.
2. **SIGTERM cleanly forwards.** The orchestrator's shutdown phase
   sends SIGTERM to each tool's PID, then SIGKILL 1s later. Our
   Python parent receives SIGTERM, forwards ``proc.terminate()`` to
   the cloudflared child, waits up to 5s for graceful exit, escalates
   to SIGKILL. Cloudflared shuts the tunnel gracefully on SIGTERM.
3. **Auto-restart on crash for free.** The Python wrapper exits with
   the child's exit code; orchestrator sees a non-zero exit and
   restarts up to 5 times. Same crash-loop protection as every other
   daemon.
4. **Missing-binary handled with exit 78.** When ``cloudflared`` isn't
   installed (or ``binary_path`` is wrong), we exit ``_MISSING_DEPS_EXIT``
   (78). The orchestrator's ``if exit_code == _MISSING_DEPS_EXIT``
   branch then skips restart — same contract surveyor uses for
   missing ML extras.

**Operator handoff note:** if a manually-started cloudflared instance
is already running (e.g. ``nohup /usr/local/bin/cloudflared tunnel
run <id> &``), it will conflict with the orchestrator-spawned one
on the same tunnel ID. v1 does NOT auto-kill the manual one — the
operator must ``kill`` it before running ``alfred up``. Detection +
takeover is a future enhancement (would query the metrics endpoint
at ``localhost:20241/metrics`` to see if a tunnel is already alive).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import structlog


# Exit code reserved for "binary or config missing — don't bother
# restarting." Mirrors :data:`alfred.orchestrator._MISSING_DEPS_EXIT`
# (78). We re-export the constant rather than importing it to avoid a
# circular import; the test suite pins both values to 78.
_MISSING_DEPS_EXIT = 78

# Grace period (seconds) between SIGTERM-to-child and SIGKILL-to-child
# on shutdown. Cloudflared usually closes its connections within ~1s;
# 5s leaves slack for slow network teardown.
_SHUTDOWN_GRACE_SECONDS = 5.0


def run(
    binary_path: str,
    tunnel_id: str,
    config_path: str = "",
    log_path: str = "",
) -> int:
    """Spawn cloudflared and supervise it until exit.

    Returns the cloudflared child's exit code, or ``_MISSING_DEPS_EXIT``
    when the binary is missing / non-executable.

    Args:
        binary_path: Absolute path to the cloudflared binary.
        tunnel_id: UUID-shaped tunnel ID (positional argument to
            ``cloudflared tunnel run``).
        config_path: Optional config file path. Empty → cloudflared
            uses its own default (``~/.cloudflared/config.yml``).
        log_path: Path where the binary's stdout/stderr are appended.
            Empty disables file logging (output goes to inherited
            stdout — typically the orchestrator's silenced-stdio
            null-sink in production).
    """
    log = structlog.get_logger(__name__)

    if not binary_path:
        # Empty binary_path on a daemon that's been registered means
        # the operator pointed ``binary_path: ""`` explicitly — treat
        # as a missing-binary case so we don't try to fork the empty
        # string.
        log.error(
            "cloudflared.binary_missing",
            binary_path=binary_path,
            detail=(
                "cloudflared.binary_path is empty. Set it in config.yaml "
                "(default: /usr/local/bin/cloudflared) or omit the field "
                "to use the default. Exiting 78 — orchestrator will not "
                "restart."
            ),
        )
        return _MISSING_DEPS_EXIT

    if not Path(binary_path).is_file() or not os.access(binary_path, os.X_OK):
        log.error(
            "cloudflared.binary_missing",
            binary_path=binary_path,
            detail=(
                f"cloudflared binary at {binary_path} is missing or not "
                "executable. Install via "
                "`curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared` "
                "or override ``cloudflared.binary_path`` in config.yaml. "
                "Exiting 78 — orchestrator will not restart."
            ),
        )
        return _MISSING_DEPS_EXIT

    if not tunnel_id:
        # No tunnel ID configured. Cloudflared can run without it if a
        # ``config.yml`` declares a default tunnel, but explicit-is-better
        # than implicit; require the ID at config layer.
        log.error(
            "cloudflared.tunnel_id_missing",
            detail=(
                "cloudflared.tunnel_id is empty. Set it in config.yaml "
                "to the UUID of the tunnel to run. Exiting 78."
            ),
        )
        return _MISSING_DEPS_EXIT

    # Build the cloudflared command. ``--config`` is positional-before-
    # subcommand on cloudflared's CLI; the ``tunnel run <id>`` form
    # matches what the operator was running manually.
    cmd = [binary_path]
    if config_path:
        cmd.extend(["--config", config_path])
    cmd.extend(["tunnel", "run", tunnel_id])

    # Open log file for child's stdout/stderr. ``ab`` ≡ append-binary so
    # repeated restarts don't truncate.
    log_fh = None
    if log_path:
        try:
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(log_path, "ab")  # noqa: SIM115 — closed in finally
        except OSError as exc:
            log.warning(
                "cloudflared.log_file_open_failed",
                log_path=log_path,
                error=str(exc),
                detail=(
                    "Could not open cloudflared log file for write; "
                    "child stdout/stderr will go to parent's stdio."
                ),
            )
            log_fh = None

    # Spawn. ``start_new_session=True`` puts cloudflared in its own
    # process group — this prevents Ctrl-C in a foreground operator
    # shell from killing cloudflared directly; instead SIGINT goes to
    # the orchestrator, which then signals us via SIGTERM, and we
    # forward to the child via ``proc.terminate()``. Cleaner shutdown
    # path.
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_fh if log_fh is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        log.error(
            "cloudflared.spawn_failed",
            binary_path=binary_path,
            error=str(exc),
            detail=(
                "subprocess.Popen failed to spawn cloudflared. Usually "
                "an OS-level problem (out of file descriptors, fork "
                "failed). Exiting 78 to avoid restart-loop."
            ),
        )
        if log_fh is not None:
            log_fh.close()
        return _MISSING_DEPS_EXIT

    log.info(
        "cloudflared.started",
        pid=proc.pid,
        binary_path=binary_path,
        tunnel_id=tunnel_id,
        config_path=config_path or "(cloudflared default ~/.cloudflared/config.yml)",
        log_path=log_path or "(inherit parent stdio)",
        detail=(
            "cloudflared tunnel started under alfred up supervision. "
            "Operator can tail the log_path file for cloudflared's "
            "own output; structlog events here cover lifecycle only."
        ),
    )

    # Install SIGTERM/SIGINT handler that forwards to the child. The
    # orchestrator's shutdown phase sends SIGTERM to our PID; we want
    # to propagate that to cloudflared so it shuts the tunnel
    # gracefully, then exit ourselves.
    shutdown_requested = {"flag": False}

    def _handle_shutdown(signum, frame):  # noqa: ARG001
        shutdown_requested["flag"] = True
        # Forward to child — cloudflared shuts down on SIGTERM.
        # ``proc.terminate()`` sends SIGTERM on POSIX.
        try:
            proc.terminate()
        except ProcessLookupError:
            # Already exited.
            pass

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # Block on the child. We poll instead of ``wait()`` so the SIGTERM
    # handler's grace-period semantics can kick in cleanly: once
    # shutdown is requested, give the child _SHUTDOWN_GRACE_SECONDS to
    # exit, then SIGKILL.
    deadline: float | None = None
    while True:
        try:
            ret = proc.wait(timeout=1.0)
            break
        except subprocess.TimeoutExpired:
            if shutdown_requested["flag"] and deadline is None:
                deadline = time.monotonic() + _SHUTDOWN_GRACE_SECONDS
            if deadline is not None and time.monotonic() >= deadline:
                # Grace exhausted — escalate to SIGKILL.
                log.warning(
                    "cloudflared.shutdown_kill",
                    pid=proc.pid,
                    detail=(
                        f"cloudflared did not exit within "
                        f"{_SHUTDOWN_GRACE_SECONDS}s after SIGTERM; "
                        "escalating to SIGKILL."
                    ),
                )
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                ret = proc.wait()
                break

    if log_fh is not None:
        try:
            log_fh.close()
        except OSError:
            pass

    log.info(
        "cloudflared.exited",
        pid=proc.pid,
        exit_code=ret,
        shutdown_requested=shutdown_requested["flag"],
        detail=(
            "cloudflared subprocess exited. Non-zero exit will trigger "
            "the orchestrator's auto-restart (max 5). Exit 0 with "
            "shutdown_requested=True is a clean SIGTERM teardown."
        ),
    )

    return ret
