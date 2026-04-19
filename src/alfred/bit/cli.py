"""``alfred bit`` subcommand handlers."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from alfred.health.renderer import render_human
from alfred.health.types import Status

from .config import BITConfig
from .daemon import run_bit_once
from .state import StateManager


def cmd_run_now(config: BITConfig, raw: dict[str, Any], *, wants_json: bool = False) -> int:
    """Execute one BIT run now. Exit code 0 on OK/WARN/SKIP, 1 on FAIL."""
    state_mgr = StateManager(config.state.path)
    state_mgr.load()

    async def _run() -> tuple[str, Status]:
        return await run_bit_once(config, raw, state_mgr)

    path, status = asyncio.run(_run())
    if wants_json:
        print(json.dumps({"record_path": path, "overall_status": status.value}, indent=2))
    else:
        print(f"BIT run written: {path}")
        print(f"Overall status: {status.value}")
    return 1 if status == Status.FAIL else 0


def cmd_status(config: BITConfig, *, wants_json: bool = False) -> int:
    """Show the BIT daemon's last run + next scheduled time."""
    state_mgr = StateManager(config.state.path)
    state_mgr.load()
    latest = state_mgr.state.latest()

    payload = {
        "schedule": {
            "time": config.schedule.time,
            "timezone": config.schedule.timezone,
            "mode": config.schedule.mode,
            "lead_minutes": config.schedule.lead_minutes,
        },
        "latest": latest.to_dict() if latest else None,
        "run_count": len(state_mgr.state.runs),
    }

    if wants_json:
        print(json.dumps(payload, indent=2))
        return 0

    print("=" * 60)
    print("ALFRED BIT STATUS")
    print("=" * 60)
    print(f"Schedule: {config.schedule.time} {config.schedule.timezone} "
          f"(mode={config.schedule.mode}, lead={config.schedule.lead_minutes}m)")
    if latest:
        print(f"Last run: {latest.generated_at}")
        print(f"  date:        {latest.date}")
        print(f"  status:      {latest.overall_status}")
        print(f"  mode:        {latest.mode}")
        print(f"  record:      {latest.vault_path}")
        print(f"  tool_counts: {latest.tool_counts}")
    else:
        print("Last run: never")
    print(f"Runs recorded: {len(state_mgr.state.runs)}")
    return 0


def cmd_history(config: BITConfig, *, limit: int = 10, wants_json: bool = False) -> int:
    """Print recent BIT runs."""
    state_mgr = StateManager(config.state.path)
    state_mgr.load()
    runs = list(state_mgr.state.runs[-limit:])

    if wants_json:
        print(json.dumps([r.to_dict() for r in runs], indent=2))
        return 0

    if not runs:
        print("No BIT runs recorded yet.")
        return 0
    print(f"Last {len(runs)} BIT run(s):")
    for run in runs:
        print(
            f"  {run.generated_at}  status={run.overall_status}  "
            f"mode={run.mode}  {run.vault_path}"
        )
    return 0
