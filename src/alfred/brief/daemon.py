"""Brief daemon — scheduled daily brief generation."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import BriefConfig
from .renderer import render_brief, serialize_record
from .state import BriefRun, StateManager
from .utils import get_logger
from .weather import fetch_and_format

log = get_logger(__name__)


async def generate_brief(config: BriefConfig, state_mgr: StateManager, refresh: bool = False) -> str | None:
    """Generate a morning brief. Returns the vault-relative path, or None if skipped."""
    today = date.today().isoformat()

    if not refresh and state_mgr.state.has_brief_for_date(today):
        log.info("brief.already_exists", date=today)
        return None

    log.info("brief.generating", date=today, refresh=refresh)

    # Fetch weather section
    weather_md = await fetch_and_format(config.weather)
    sections = [("Weather", weather_md)]

    # Render
    frontmatter, body = render_brief(today, sections, config)
    content = serialize_record(frontmatter, body)

    # Write to vault
    vault_path = Path(config.vault_path)
    name = config.output.name_template.replace("{date}", today)
    rel_path = f"{config.output.directory}/{name}.md"
    file_path = vault_path / rel_path

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")

    log.info("brief.written", path=rel_path)

    # Update state
    state_mgr.state.add_run(BriefRun(
        date=today,
        generated_at=datetime.now(timezone.utc).isoformat(),
        vault_path=rel_path,
        sections=["weather"],
        success=True,
    ))
    state_mgr.save()

    return rel_path


async def update_weather(config: BriefConfig) -> str:
    """Update the weather section in today's brief. Creates the brief if it doesn't exist."""
    today = date.today().isoformat()
    vault_path = Path(config.vault_path)
    name = config.output.name_template.replace("{date}", today)
    rel_path = f"{config.output.directory}/{name}.md"
    file_path = vault_path / rel_path

    log.info("brief.updating_weather", date=today)

    # Fetch fresh weather
    weather_md = await fetch_and_format(config.weather)

    if not file_path.exists():
        # No brief yet — generate a full one
        sections = [("Weather", weather_md)]
        frontmatter, body = render_brief(today, sections, config)
        content = serialize_record(frontmatter, body)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        log.info("brief.weather_created", path=rel_path)
        return rel_path

    # Brief exists — replace the weather section in-place
    existing = file_path.read_text(encoding="utf-8")

    # Find and replace the ## Weather section
    import re
    # Match from "## Weather\n" to the next "## " or "---" divider or end of file
    pattern = r"(## Weather\n)(.*?)(\n## |\n---|\Z)"
    replacement_body = f"## Weather\n\n{weather_md}\n"

    match = re.search(pattern, existing, re.DOTALL)
    if match:
        # Preserve what comes after
        suffix = match.group(3)
        updated = existing[:match.start()] + replacement_body + suffix + existing[match.end():]
        file_path.write_text(updated, encoding="utf-8")
        log.info("brief.weather_updated", path=rel_path)
    else:
        log.warning("brief.weather_section_not_found", path=rel_path)

    return rel_path


def _next_run_time(schedule_time: str, tz_name: str) -> datetime:
    """Calculate the next scheduled run time."""
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    hour, minute = map(int, schedule_time.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


async def run_daemon(config: BriefConfig) -> None:
    """Daily brief generator daemon. Runs at configured time."""
    log.info("brief.daemon.starting",
             schedule_time=config.schedule.time,
             tz=config.schedule.timezone)

    state_mgr = StateManager(config.state.path)
    state_mgr.load()

    while True:
        tz = ZoneInfo(config.schedule.timezone)
        target = _next_run_time(config.schedule.time, config.schedule.timezone)
        now = datetime.now(tz)
        sleep_seconds = (target - now).total_seconds()

        if sleep_seconds > 0:
            log.info("brief.daemon.sleeping",
                     next_run=target.isoformat(),
                     sleep_hours=round(sleep_seconds / 3600, 1))
            await asyncio.sleep(sleep_seconds)

        try:
            path = await generate_brief(config, state_mgr)
            if path:
                log.info("brief.daemon.generated", path=path)
        except Exception:
            log.exception("brief.daemon.error")

        # Sleep 60s to avoid double-firing
        await asyncio.sleep(60)
