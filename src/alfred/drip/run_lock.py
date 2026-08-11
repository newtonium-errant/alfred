"""One drip run at a time, per instance — the cross-process ignition guard.

WHY THIS EXISTS, MEASURED. Until #83 every drip run arrived from exactly one
place: an hourly systemd timer, serialized by having a single caller. #83 adds
a SECOND ignition — the batch submit route kicks a run the moment scans land,
so the operator does not wait up to an hour for the first result. That makes
concurrent runs reachable for the first time, and concurrent runs are not safe.

The claim "the runner claims before it works, so concurrent runs are fine" is
false, and it is false in the expensive direction. ``run_increment`` writes
``state = IN_FLIGHT; claimed_by = run_id`` and persists it BEFORE calling
``work()`` (runner.py), but that is a CRASH-VISIBILITY marker, not a mutual
exclusion primitive: two processes that load the state file before either one
writes both see every item PENDING, both claim, and both work. Measured on this
tree — two concurrent ``run_increment`` calls over one 6-item work-list produced
**12** ``work()`` calls, every item processed exactly twice, and BOTH runs
reported a clean ``done=6``. Nothing anywhere reported a problem.

For ``batch_image`` that is a doubled vision bill per scan. ``process_one``
short-circuits on a ledger row it can already see, but the read happens at the
top and the append happens after the model returns, so two runs interleaved in
that window both pay. The ledger's own ``flock`` does not help: it makes the
two appends serialize, not the two model calls they are recording.

WHERE THE LOCK LIVES, AND WHY HERE. At the shared entry point, so EVERY
ignition serializes on it — the timer's ``alfred drip run``, the batch route's
kick, and an operator running it by hand are the same code path taking the same
lock. A lock in the kick alone would guard nothing, because the race is between
the kick and the TIMER. This is the doctrine ``msgbus.router.run_route_once``
states for the same reason ("THE CONCURRENCY GUARD — the lock lives HERE, not
in the caller") once route-on-send made that router concurrent.

NON-BLOCKING, AND A SKIP IS NOT A FAILURE. ``LOCK_EX | LOCK_NB``: a run that
finds the lock held returns immediately rather than queueing behind a run that
may take an hour. That is correct for both callers — the timer will fire again
next hour, and the kick's work is already being done by whoever holds the lock,
which is precisely the outcome the kick wanted. So the skip exits 0 and says so
in words (intentionally-left-blank): "another run holds the lock" must be
distinguishable from "ran and found nothing", and neither may look like a crash.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path

import structlog

from .state import drip_instance_dir

# fcntl is POSIX-only, mirroring ``alfred.common.file_lock``. The fleet is
# Linux; the guarded import keeps this module importable elsewhere.
try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    _fcntl = None  # type: ignore[assignment]

log = structlog.get_logger(__name__)

#: Sidecar name inside the instance's drip directory. A dotfile so it never
#: reads as a campaign state file to anyone listing that directory.
_RUN_LOCK_NAME = ".run.lock"


def run_lock_path(data_dir: Path | str, instance: str) -> Path:
    """``<data_dir>/drip/<instance-slug>/.run.lock``.

    Derived from :func:`~alfred.drip.state.drip_instance_dir`, the same helper
    ``campaign_state_path`` uses, so the lock is guaranteed to sit beside the
    state it protects. Re-deriving the slug here instead would let the two drift
    into guarding different directories — and a lock on the wrong directory
    fails OPEN, which is the one way a lock can be worse than no lock at all.

    Raises ``DripConfigError`` on a blank instance, for the reason stated there:
    an unscoped path is shared across every instance on the box.
    """
    return drip_instance_dir(data_dir, instance) / _RUN_LOCK_NAME


@contextlib.contextmanager
def run_lock(data_dir: Path | str, instance: str) -> Iterator[bool]:
    """Try to take this instance's drip run lock. Yields whether it was taken.

    Yields ``True`` when this process holds the lock and should run, ``False``
    when another run already holds it and this one should stand down. The
    caller decides what a stand-down means; it is NOT an exception, because a
    skipped run is an ordinary, expected outcome once there are two ignitions.

    Degrades to yielding ``True`` (with a warn) when ``fcntl`` is unavailable —
    the non-POSIX fallback preserves the pre-#83 behaviour of running
    unguarded rather than refusing to run at all. Linux fleet, so defensive.
    """
    if _fcntl is None:  # pragma: no cover - non-POSIX fallback
        log.warning(
            "drip.run_lock.flock_unavailable",
            detail=(
                "fcntl not available (non-POSIX); drip runs are UNGUARDED and "
                "two concurrent runs would double-process every item. The "
                "fleet is Linux; this path is defensive."
            ),
        )
        yield True
        return

    path = run_lock_path(data_dir, instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    # ``a`` so concurrent creators do not truncate each other; the file's
    # CONTENT is never read — the flock on the descriptor is the whole
    # mechanism. Never deleted: unlinking it would reopen a create-race in
    # which two processes lock two different inodes and both proceed.
    with open(path, "a", encoding="utf-8") as fh:
        try:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            log.info(
                "drip.run_lock.busy",
                lock_path=str(path),
                detail="another drip run holds this instance's lock — "
                       "standing down rather than running concurrently, "
                       "which would double-process every claimed item",
            )
            yield False
            return
        # Recorded for a human reading a stuck lock, never read back by code.
        try:
            fh.write(f"{os.getpid()}\n")
            fh.flush()
        except OSError:  # pragma: no cover - a write failure must not lose the lock
            pass
        try:
            yield True
        finally:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)


__all__ = ["run_lock", "run_lock_path"]
