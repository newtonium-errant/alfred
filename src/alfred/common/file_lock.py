"""Generic cross-process read-modify-write lock (``fcntl.flock`` sidecar).

An exclusive lock around any file's read-modify-write so concurrent RMW writers
in SEPARATE processes serialize instead of clobbering each other's updates.

Promoted here from ``tier.daily_curation`` (where it was ``daily_file_lock``)
once a second subsystem — the routine CLI writers — needed the SAME lock on the
SAME ``routine/<Name>.md`` records that ``tier.promote`` already locks. A neutral
home (``alfred.common``) avoids a ``routine`` → ``tier`` import coupling.

Mechanism: an exclusive ``fcntl.flock(LOCK_EX)`` on a ``<path>.lock`` sidecar
next to the file the caller is about to write. Deriving the lock from the actual
write path means every writer of the SAME file locks the SAME sidecar.

IMPORTANT — the lock SERIALIZES; it does NOT make the write atomic. Pair it with
an atomic ``.tmp → os.replace`` write INSIDE the lock to also close torn reads:
the flock fixes LOST UPDATES (writer A reads → B reads the same state → A writes
→ B writes A's now-stale view → A's keys clobbered), the atomic write fixes TORN
READS (a concurrent reader seeing a half-written file). Both are needed — an
atomic write alone does NOT fix lost updates across two RMW writers.

Degrades to a no-op (with a warn) if ``fcntl`` is unavailable (non-POSIX); the
fleet is Linux, so that path is defensive-only.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import structlog

# fcntl is POSIX-only. The fleet runs on Linux (prod box + WSL dev), so it's
# always available there; the guarded import keeps the module importable on a
# hypothetical non-POSIX box (the lock then degrades to a no-op).
try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    _fcntl = None  # type: ignore[assignment]

log = structlog.get_logger(__name__)


@contextlib.contextmanager
def file_rmw_lock(file_path: Path) -> Iterator[None]:
    """Exclusive cross-process lock around a file read-modify-write.

    Wraps a writer's WHOLE RMW (read existing → merge → atomic write) in an
    ``fcntl.flock(LOCK_EX)`` on a ``<file_path>.lock`` sidecar, so two RMWs of
    the same file serialize: the second writer blocks until the first's write
    lands, then reads the fresh state.

    ``file_path`` is the RESOLVED path the caller is about to write; the lock is
    that path with a ``.lock`` suffix. Deriving the lock from the actual file
    path (not a hardcoded dir) means every writer of the SAME file locks the
    SAME sidecar — structurally consistent whatever path each writer resolved.
    The lock file is created on demand, never deleted (a stable 0-byte sidecar;
    deleting it would reopen a create-race on the lock).

    Degrades to a no-op (with a warn) if ``fcntl`` is unavailable — preserves
    the atomic-only behaviour on non-POSIX rather than crashing. Linux fleet, so
    defensive-only.
    """
    if _fcntl is None:  # pragma: no cover - non-POSIX fallback
        log.warning(
            "common.file_lock.flock_unavailable",
            detail=(
                "fcntl not available (non-POSIX); RMW writes fall back to "
                "atomic-only (torn-reads closed, lost-update window OPEN). "
                "The fleet is Linux; this path is defensive."
            ),
        )
        yield
        return

    lock_path = file_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Open (create if absent) the sidecar lock file. ``a`` so concurrent
    # creators don't truncate each other; we never write to it — the flock on
    # the fd is the whole mechanism.
    with open(lock_path, "a", encoding="utf-8") as lock_fd:
        _fcntl.flock(lock_fd.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            _fcntl.flock(lock_fd.fileno(), _fcntl.LOCK_UN)
