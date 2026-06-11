"""Daemon persistence — spawn, stop, and manage background Alfred processes.

Uses the re-exec pattern: ``alfred up`` re-launches itself as a detached
background process via ``alfred up --_internal-foreground``.  Cross-platform
(Windows + Unix), pure stdlib — no external dependencies.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------

def write_pid(pid_path: Path, pid: int) -> None:
    """Write a PID to the given file."""
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid), encoding="utf-8")


def read_pid(pid_path: Path) -> int | None:
    """Read PID from file.  Returns None if missing or malformed."""
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def remove_pid(pid_path: Path) -> None:
    """Remove a PID file if it exists."""
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Liveness check
# ---------------------------------------------------------------------------

def is_running(pid: int) -> bool:
    """Cross-platform check whether *pid* refers to a running process.

    On Windows ``os.kill(pid, 0)`` actually *kills* the process, so we use
    ctypes ``OpenProcess`` + ``GetExitCodeProcess`` instead.
    """
    if sys.platform == "win32":
        return _is_running_windows(pid)
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but we lack permission — still alive.
            return True


def _is_running_windows(pid: int) -> bool:
    """Windows-specific liveness check via kernel32."""
    import ctypes
    import ctypes.wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False

    try:
        exit_code = ctypes.wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == STILL_ACTIVE
        return False
    finally:
        kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# High-level PID management
# ---------------------------------------------------------------------------

def check_already_running(pid_path: Path) -> int | None:
    """Return PID if Alfred daemon is running, else clean up stale file."""
    pid = read_pid(pid_path)
    if pid is None:
        return None
    # If the PID file points to our own process, it's stale from a previous
    # run (common in containers where PID 1 is reused across restarts).
    if pid == os.getpid():
        remove_pid(pid_path)
        return None
    if is_running(pid):
        return pid
    # Stale PID file — process no longer exists.
    remove_pid(pid_path)
    return None


# ---------------------------------------------------------------------------
# Spawn
# ---------------------------------------------------------------------------


def rotate_capture_log_if_oversized(
    log_path: Path,
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> bool:
    """Spawn-time rollover for the stdout-capture log. Returns True if rolled.

    S5 (2026-06-11): the capture file (alfred.log) is opened fd-level
    for child stdout/stderr — those writes BYPASS the parent's
    ``RotatingFileHandler``, so the handler's size policy never bounds
    them. Worse, when the handler's own first emit DID roll an
    oversized file, the rename happened AFTER this capture fd was
    opened — the fd followed the rename and the whole run's capture
    fattened the ``.1`` sibling instead (observed 2026-06-11: 954MB /
    911MB rotated files against a 100MB policy, ~2.4GB total).

    Rolling HERE — before the capture fd is opened — keeps the policy
    honest at run boundaries: the new run's capture starts on a fresh
    file the handler shares coherently, and the previous run's history
    survives in the numbered siblings (append-within-a-run,
    rotate-across-runs). Residual, documented: growth WITHIN one run is
    still unbounded by the policy (children hold the fd; you can't
    size-rotate a file descriptor another process is writing) — bounded
    in practice by per-run lifetime now that each restart rolls.

    Mirrors ``RotatingFileHandler.doRollover``'s rename cascade and
    reuses the bundled rotation policy for defaults.
    """
    from alfred.common.logging_handler import resolve_rotation_policy

    resolved_max_bytes, resolved_backup_count = resolve_rotation_policy(
        max_bytes, backup_count
    )
    # RotatingFileHandler semantics: maxBytes=0 or backupCount=0 means
    # "never roll" — mirror that here so a rotation-disabled config
    # disables the spawn-time roll too.
    if resolved_max_bytes <= 0 or resolved_backup_count <= 0:
        return False
    try:
        if not log_path.exists() or log_path.stat().st_size <= resolved_max_bytes:
            return False
        # RotatingFileHandler-style cascade: .{n-1} → .{n}, ... , live → .1
        oldest = log_path.with_name(f"{log_path.name}.{resolved_backup_count}")
        oldest.unlink(missing_ok=True)
        for i in range(resolved_backup_count - 1, 0, -1):
            src = log_path.with_name(f"{log_path.name}.{i}")
            if src.exists():
                src.rename(log_path.with_name(f"{log_path.name}.{i + 1}"))
        log_path.rename(log_path.with_name(f"{log_path.name}.1"))
        return True
    except OSError:
        # Rotation is best-effort — a locked/odd filesystem must never
        # block daemon startup; the append-open below still works.
        return False


def spawn_daemon(
    config_path: str,
    only: str | None,
    log_file: str,
) -> int:
    """Re-exec Alfred as a detached background process.  Returns the child PID."""
    cmd = [
        sys.executable, "-m", "alfred",
        "--config", config_path,
        "up", "--_internal-foreground",
    ]
    if only:
        cmd.extend(["--only", only])

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Roll an oversized capture BEFORE opening the fd (see the helper's
    # docstring — opening first is exactly what produced the 954MB
    # rotated siblings). Append mode below is deliberate and was always
    # the behavior: spawn history accumulates within the rotation policy.
    rotate_capture_log_if_oversized(log_path)

    stdout_f = open(log_path, "a", encoding="utf-8")

    kwargs: dict = dict(
        stdin=subprocess.DEVNULL,
        stdout=stdout_f,
        stderr=stdout_f,
    )

    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        kwargs["creationflags"] = (
            DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **kwargs)
    return proc.pid


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

def stop_daemon(pid_path: Path) -> bool:
    """Send a shutdown signal to the running daemon.  Returns True if stopped."""
    pid = read_pid(pid_path)
    if pid is None:
        return False
    if not is_running(pid):
        remove_pid(pid_path)
        return False

    # Create sentinel file as belt-and-suspenders (for Windows compatibility)
    sentinel = pid_path.parent / "alfred.stop"
    sentinel.write_text("stop", encoding="utf-8")

    if sys.platform == "win32":
        _stop_windows(pid)
    else:
        _stop_unix(pid)

    # Clean up
    remove_pid(pid_path)
    try:
        sentinel.unlink(missing_ok=True)
    except OSError:
        pass

    return True


def _stop_unix(pid: int) -> None:
    """Graceful SIGTERM, then SIGKILL after timeout."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    for _ in range(50):  # 5 seconds
        time.sleep(0.1)
        if not is_running(pid):
            return

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _stop_windows(pid: int) -> None:
    """Send CTRL_BREAK_EVENT (graceful), then TerminateProcess (force)."""
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    # Try graceful shutdown via CTRL_BREAK_EVENT
    try:
        kernel32.GenerateConsoleCtrlEvent(1, pid)  # CTRL_BREAK_EVENT = 1
    except OSError:
        pass

    for _ in range(50):  # 5 seconds
        time.sleep(0.1)
        if not is_running(pid):
            return

    # Force kill
    PROCESS_TERMINATE = 0x0001
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if handle:
        kernel32.TerminateProcess(handle, 1)
        kernel32.CloseHandle(handle)
