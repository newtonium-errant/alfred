"""Brief CLI subcommand handlers."""

from __future__ import annotations

import asyncio

from .config import BriefConfig
from .daemon import generate_brief, run_daemon, update_weather
from .state import StateManager
from .utils import get_logger

log = get_logger(__name__)


def cmd_generate(config: BriefConfig, refresh: bool = False) -> None:
    """Generate a morning brief right now."""
    state_mgr = StateManager(config.state.path)
    state_mgr.load()
    path = asyncio.run(generate_brief(config, state_mgr, refresh=refresh))
    if path:
        print(f"Brief generated: {path}")
    else:
        print("Brief already exists for today. Use --refresh to overwrite, or 'alfred brief weather' to update weather only.")


def cmd_weather(config: BriefConfig) -> None:
    """Update the weather section in today's brief with fresh data."""
    path = asyncio.run(update_weather(config))
    print(f"Weather updated: {path}")


def cmd_status(config: BriefConfig) -> None:
    """Show brief generation status."""
    state_mgr = StateManager(config.state.path)
    state_mgr.load()
    print("=== Brief Status ===")
    print(f"Last run: {state_mgr.state.last_run or 'never'}")
    print(f"Total briefs: {len(state_mgr.state.runs)}")
    if state_mgr.state.runs:
        last = state_mgr.state.runs[-1]
        print(f"Latest: {last.vault_path} ({last.date})")


def cmd_history(config: BriefConfig, limit: int = 10) -> None:
    """Show recent brief history."""
    state_mgr = StateManager(config.state.path)
    state_mgr.load()
    runs = state_mgr.state.runs[-limit:]
    if not runs:
        print("No brief history.")
        return
    print(f"=== Brief History (last {len(runs)}) ===\n")
    print(f"{'Date':<12} {'Generated':<28} {'Path'}")
    print("-" * 70)
    for r in reversed(runs):
        print(f"{r.date:<12} {r.generated_at:<28} {r.vault_path}")


def cmd_watch(config: BriefConfig) -> None:
    """Daemon mode — generate on schedule."""
    try:
        asyncio.run(run_daemon(config))
    except KeyboardInterrupt:
        log.info("brief.daemon.interrupted")
        print("\nStopped.")
