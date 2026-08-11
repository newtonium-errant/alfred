"""Start a drip run NOW, detached — the on-submit ignition (#83 item 5).

The hourly timer is the steady-state ignition. It is also up to an hour of
nothing after an operator uploads thirty scans and reasonably expects to see
something happen. This kicks one run immediately so the first results land in
minutes, and then gets out of the way.

WHY A DETACHED SUBPROCESS, NOT AN IN-PROCESS CALL. Three reasons, in order of
how badly each would bite:

1. **It would block the event loop.** ``run_increment`` is synchronous and
   ``BatchImageCampaign.work`` makes a blocking Anthropic vision call per scan.
   Calling it from an aiohttp handler would stall the SHARED transport app —
   chat, STT, peer routes, health — for the length of the whole increment.
   Minutes, not milliseconds.
2. **A thread only relocates the problem.** Offloading to an executor frees the
   loop but plants a long-running, uninterruptible thread inside the talker
   daemon that outlives the request and cannot be stopped at shutdown. That is
   the unstoppable-thread shape already boarded twice on this project (#77).
3. **It would be a SECOND code path.** The timer runs ``alfred drip run``. An
   in-process runner call would be a different path to the same work, and the
   two would drift — different config resolution, different error handling,
   different budgets. One ignition mechanism, invoked two ways, is the design.

So: spawn exactly what the timer spawns, with ``start_new_session=True`` (the
``daemon.spawn_daemon`` / ``cloudflared.daemon`` precedent), and return the pid.
The request does not wait for it, the child survives a daemon restart, and its
output lands in the drip log where the operator already looks.

RACING THE TIMER IS HANDLED, ELSEWHERE AND ON PURPOSE. A kick can fire while
the hourly run is mid-flight, and concurrent runs double-process every claimed
item (measured — see :mod:`alfred.drip.run_lock`). The guard is the run lock
inside ``cmd_run``, NOT a check here: the child takes the lock and stands down
on its own if the timer holds it. Putting the check here instead would be a
TOCTOU race — the timer can start in the window between a check and the spawn —
and it would leave the timer-vs-timer case unguarded.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

#: Log file the kicked run's stdout/stderr is appended to, relative to the
#: instance's data dir. Matches the per-tool ``data/<tool>.log`` convention, so
#: a kicked run's output lands exactly where an operator already looks for drip
#: output rather than in a second place invented for this path.
DRIP_LOG_NAME = "drip.log"


def kick_drip_run(
    *,
    config_path: str,
    campaign: str,
    data_dir: str | Path = "",
) -> int | None:
    """Spawn a detached ``alfred drip run --campaign <campaign>``.

    Returns the child's pid, or ``None`` when the spawn could not be attempted
    or failed. NEVER raises: a submission that has already been saved to disk
    must not be reported as failed because the optional head start could not be
    given. The scans are safe either way; the timer will drain them.

    ``config_path`` must be THIS instance's config file. It is required rather
    than defaulted because ``alfred`` with no ``--config`` reads ``config.yaml``
    — so a Hypatia daemon that omitted it would kick a run against SALEM's
    campaigns, spending another instance's budget on another instance's vault.
    """
    if not config_path:
        # Not an error the operator caused, and not silent: without it the kick
        # would run against the wrong instance, so declining is correct.
        log.warning(
            "drip.kick.no_config_path",
            campaign=campaign,
            detail="declining to kick — without an explicit --config the run "
                   "would read config.yaml and drain a DIFFERENT instance's "
                   "campaigns. The hourly timer still covers this batch.",
        )
        return None

    cmd = [
        sys.executable, "-m", "alfred",
        "--config", str(config_path),
        "drip", "run",
        "--campaign", campaign,
    ]

    # ``run`` applies by default (``apply=not args.dry_run``), so there is no
    # ``--apply`` to pass — adding one would be an argparse error at spawn time.

    log_fh = None
    if data_dir:
        try:
            log_path = Path(data_dir) / DRIP_LOG_NAME
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(log_path, "a", encoding="utf-8")
        except OSError as exc:
            # Degrade to DEVNULL rather than refusing to kick. Losing the
            # child's stdout is worse than nothing but far better than not
            # draining the batch at all.
            log.warning(
                "drip.kick.log_unavailable",
                campaign=campaign,
                error_type=type(exc).__name__,
                detail=str(exc)[:200],
            )
            log_fh = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_fh if log_fh is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        log.warning(
            "drip.kick.spawn_failed",
            campaign=campaign,
            error_type=type(exc).__name__,
            detail=str(exc)[:200],
            note="the batch is saved and the hourly timer will still drain "
                 "it — only the head start was lost",
        )
        return None
    finally:
        # The child holds its own dup of the descriptor; the parent must not
        # keep this one open or the daemon leaks one fd per submission.
        if log_fh is not None:
            log_fh.close()

    log.info(
        "drip.kick.spawned",
        campaign=campaign,
        pid=proc.pid,
        config_path=str(config_path),
        detail="detached run started; it stands down by itself if the hourly "
               "run already holds the lock",
    )
    return proc.pid


__all__ = ["DRIP_LOG_NAME", "kick_drip_run"]
