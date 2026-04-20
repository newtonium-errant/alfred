"""Subcommand handlers for ``alfred instructor``.

Three subcommands, mirroring the shape of other tool CLIs:

- ``scan``   — run one detection pass, print a summary of pending
               directives without executing them (dry-observation).
- ``run``    — run the daemon loop in foreground. Intended for
               debugging; production use goes through ``alfred up``.
- ``status`` — print state summary (tracked files, pending retries,
               last run timestamp).

Commit 3 scope: ``scan`` and ``status`` are fully wired because they
only use the detector + state layer shipped here. ``run`` starts the
detector-only poll loop from ``daemon.py``; the real executor
dispatches in commit 4 and will be invoked from the same entry point.
"""

from __future__ import annotations

import asyncio

from .config import InstructorConfig
from .daemon import detect_pending, run as run_daemon
from .state import InstructorState
from .utils import get_logger

log = get_logger(__name__)


def cmd_scan(config: InstructorConfig) -> None:
    """Run one detection pass and print a summary.

    Does NOT mutate state (we pass a throwaway InstructorState so the
    operator can observe the live queue without touching persisted
    hashes). Useful for "what's pending right now?" checks.
    """
    # Throwaway state — no load(), no save(). hash_unchanged() then
    # returns False for every file, which is exactly what we want on a
    # scan: re-inspect everything, report what's populated, then exit.
    probe_state = InstructorState("/dev/null")

    pending = detect_pending(
        config.vault.vault_path,
        probe_state,
        ignore_dirs=config.vault.ignore_dirs,
    )

    if not pending:
        print("No pending alfred_instructions directives.")
        return

    print(f"Pending directives: {len(pending)}")
    by_path: dict[str, list[str]] = {}
    for p in pending:
        by_path.setdefault(p.rel_path, []).append(p.directive)
    for rel_path, directives in sorted(by_path.items()):
        print(f"\n  {rel_path}")
        for d in directives:
            # Truncate very long directives — full text lives in the
            # vault, this is just a quick overview.
            preview = d if len(d) <= 120 else d[:117] + "..."
            print(f"    - {preview}")


def cmd_run(config: InstructorConfig) -> None:
    """Run the poll loop in foreground until Ctrl-C."""
    state = InstructorState(config.state.path)
    state.load()
    try:
        asyncio.run(run_daemon(config, state))
    except KeyboardInterrupt:
        print("\nStopped.")


def cmd_status(config: InstructorConfig) -> None:
    """Print state summary — tracked files, pending retries, last run."""
    state = InstructorState(config.state.path)
    state.load()

    print("=" * 60)
    print("INSTRUCTOR STATUS")
    print("=" * 60)
    print(f"  State file:      {config.state.path}")
    print(f"  Tracked records: {len(state.file_hashes)}")
    print(f"  Last run:        {state.last_run_ts or 'never'}")

    pending_retries = {k: v for k, v in state.retry_counts.items() if v > 0}
    if pending_retries:
        print(f"  Records with retries pending ({len(pending_retries)}):")
        for rel_path, count in sorted(pending_retries.items()):
            print(f"    {rel_path}  (retries={count}/{config.max_retries})")
    else:
        print("  Pending retries: none")

    print(f"  Poll interval:   {config.poll_interval_seconds}s")
    print(f"  Model:           {config.anthropic.model}")
    print()
