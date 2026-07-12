"""Alfred Textual TUI — interactive dashboard for ``alfred up --live``."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


def run_textual_dashboard(
    tools: list[str],
    processes: dict[str, Any],
    restart_counts: dict[str, int],
    start_process: Callable[[str], Any],
    sentinel_path: Path | None,
    log_dir: Path,
    state_dir: Path,
    max_restarts: int = 5,
    missing_deps_exit: int = 78,
    sovereign_enabled: bool = False,
) -> bool:
    """Entry point matching the ``run_live_dashboard`` signature from dashboard.py.

    Returns True iff the dashboard tore down on a sovereign runtime breach
    (a slot exited 79 in a sovereign instance) — run_all feeds that into the
    #42 exit-79 propagation. ``sovereign_enabled`` is the SAME bool run_all
    computed at E1 (passed down, never re-derived — #59 predicate consistency).
    """
    from alfred.tui.app import AlfredApp

    # Read version from package metadata
    version = "0.2.0"
    try:
        from importlib.metadata import version as pkg_version
        version = pkg_version("alfred-vault")
    except Exception:
        pass

    app = AlfredApp(
        tools=tools,
        processes=processes,
        restart_counts=restart_counts,
        start_process=start_process,
        sentinel_path=sentinel_path,
        log_dir=log_dir,
        state_dir=state_dir,
        max_restarts=max_restarts,
        missing_deps_exit=missing_deps_exit,
        version=version,
        sovereign_enabled=sovereign_enabled,
    )
    app.run()
    # After App.run() returns (App.exit() was called on breach), surface the
    # breach flag so run_all can propagate exit 79.
    return bool(getattr(app, "_sovereign_breach", False))
