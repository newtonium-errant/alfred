"""Brief daemon — scheduled daily brief generation."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from alfred.common.schedule import compute_next_fire

from .config import BriefConfig
from .health_section import render_health_section
from .renderer import render_brief, serialize_record
from .state import BriefRun, StateManager
from .utils import get_logger
from .operations import format_operations_section
from .weather import fetch_and_format

log = get_logger(__name__)


async def _push_brief_to_telegram(
    content: str,
    today: str,
    user_id: int,
) -> None:
    """Dispatch the rendered brief as Telegram chunks via the transport.

    Best-effort. Transport failures are logged and swallowed — the
    brief is already written to the vault, so the user's primary
    artifact is safe even if the push fails. Catches the
    ``transport-down`` case (common during talker daemon restarts) so
    brief generation doesn't become coupled to the talker's liveness.

    Single-user v1: ``user_id`` is the first entry in
    ``telegram.allowed_users``. Multi-user support arrives with the
    peer protocol in Stage 3.5.
    """
    # Local imports so the brief module doesn't drag transport into
    # tools that don't need it (e.g. when telegram isn't configured).
    from alfred.transport.client import send_outbound_batch
    from alfred.transport.exceptions import TransportError
    from alfred.transport.utils import chunk_for_telegram

    try:
        chunks = chunk_for_telegram(content)
        if not chunks or not chunks[0]:
            log.info("brief.push_skipped_empty", date=today)
            return
        await send_outbound_batch(
            user_id=user_id,
            chunks=chunks,
            dedupe_key=f"brief-{today}",
            client_name="brief",
        )
        log.info(
            "brief.pushed",
            date=today,
            chunks=len(chunks),
            user_id=user_id,
        )
    except TransportError as exc:
        log.warning(
            "brief.push_failed",
            date=today,
            error_type=exc.__class__.__name__,
            error=str(exc),
            response_summary=f"{exc.__class__.__name__}: {exc}",
        )


async def generate_brief(config: BriefConfig, state_mgr: StateManager, refresh: bool = False) -> str | None:
    """Generate a morning brief. Returns the vault-relative path, or None if skipped."""
    today = date.today().isoformat()

    if not refresh and state_mgr.state.has_brief_for_date(today):
        log.info("brief.already_exists", date=today)
        return None

    log.info("brief.generating", date=today, refresh=refresh)

    # Fetch weather section
    weather_md = await fetch_and_format(config.weather)

    # Operations snapshot
    data_dir = str(Path(config.state.path).parent)
    ops_md = format_operations_section(data_dir, config.vault_path, since=today)

    # Health section — reads the latest BIT record from vault/process/,
    # falling back to the BIT state file if no record is available.
    bit_state_path = Path(data_dir) / "bit_state.json"
    health_md = render_health_section(
        config.vault_path,
        state_path=bit_state_path,
        today=today,
    )

    # Section order is load-bearing: Health first (readers scan top-down;
    # critical status gets the highest priority real estate), Weather
    # second (time-sensitive but non-operational), Operations third
    # (retrospective summary — less time-sensitive than the others).
    sections = [
        ("Health", health_md),
        ("Weather", weather_md),
        ("Operations", ops_md),
    ]

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

    # Post-write push to Telegram — best-effort, swallows transport
    # errors so the brief stays in the vault even if the talker daemon
    # is restarting. ``primary_telegram_user_id`` is None when no
    # telegram section is configured — skip the push silently.
    if config.primary_telegram_user_id is not None:
        await _push_brief_to_telegram(
            content, today, config.primary_telegram_user_id,
        )

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


async def run_daemon(config: BriefConfig) -> None:
    """Daily brief generator daemon. Runs at configured time."""
    log.info("brief.daemon.starting",
             schedule_time=config.schedule.time,
             tz=config.schedule.timezone)

    state_mgr = StateManager(config.state.path)
    state_mgr.load()

    while True:
        tz = ZoneInfo(config.schedule.timezone)
        now = datetime.now(tz)
        # Clock-aligned next-fire via shared helper (see
        # ``alfred.common.schedule``). Daily-only for brief; the weekly
        # gate there is used by distiller consolidation, not brief.
        target = compute_next_fire(config.schedule, now)
        sleep_seconds = (target - now).total_seconds()

        if sleep_seconds > 0:
            log.info("brief.daemon.sleeping",
                     next_run=target.isoformat(),
                     sleep_hours=round(sleep_seconds / 3600, 1))
            await asyncio.sleep(sleep_seconds)

        # Best-effort vault snapshot BEFORE brief — captures previous day's work
        try:
            from alfred.vault.snapshot import (
                build_snapshot_summary,
                get_status,
                take_snapshot,
            )

            vault_path = Path(config.vault_path)
            audit_log = Path(config.state.path).parent / "vault_audit.log"
            status = get_status(vault_path)
            since = status.get("last_commit_date")
            summary = build_snapshot_summary(audit_log, since=since)
            commit = take_snapshot(vault_path, message=summary)
            if commit:
                log.info("brief.daemon.snapshot", commit=commit)
            else:
                log.info("brief.daemon.snapshot_noop")
        except Exception:
            log.warning("brief.daemon.snapshot_failed", exc_info=True)

        # Generate the morning brief
        try:
            path = await generate_brief(config, state_mgr)
            if path:
                log.info("brief.daemon.generated", path=path)
        except Exception:
            log.exception("brief.daemon.error")

        # Sleep 60s to avoid double-firing
        await asyncio.sleep(60)
