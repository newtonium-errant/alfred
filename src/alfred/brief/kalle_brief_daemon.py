"""KAL-LE morning digest pusher daemon.

Runs on KAL-LE (or any specialist instance using the same shape).
Wakes at the configured time (typically 05:30 ADT — 30 minutes before
Salem's 06:00 brief), assembles a one-slide digest via
:mod:`alfred.brief.kalle_digest`, and pushes it to the principal's
``/peer/brief_digest`` endpoint via the outbound transport client.

Lifecycle pattern mirrors ``alfred.brief.daemon.run_daemon`` and
``alfred.daily_sync.daemon.run_daemon``: compute next fire,
``sleep_until`` with drift-bounded chunks, fire, log, loop. Failures
are log-and-continue — the principal's brief renderer tolerates a
missing digest via the intentionally-left-blank line, so a single
push failure never cascades into a missing brief.

Scope discipline: this daemon does NOT run on Salem. Salem hosts the
RECEIVER (transport ``/peer/brief_digest`` endpoint + brief section);
KAL-LE / STAY-C / future specialists run this SENDER. Per-instance
config gating is enforced in the orchestrator via the ``brief_digest_push``
key — when the block is absent or ``enabled: false``, the daemon
short-circuits with exit code 78 (orchestrator's "not configured"
convention so auto-restart skips it).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from alfred.common.schedule import ScheduleConfig, compute_next_fire, sleep_until
from alfred.transport.client import peer_send_brief_digest
from alfred.transport.config import TransportConfig
from alfred.transport.exceptions import TransportError

from .kalle_digest import assemble_digest
from .utils import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class BriefDigestPushConfig:
    """Per-sender config for the digest pusher.

    Lives at top level of ``config.kalle.yaml`` under
    ``brief_digest_push:``. STAY-C will adopt the same block once it
    spins up.
    """

    enabled: bool = False
    # Identity this instance presents in body.peer + as
    # X-Alfred-Client. The principal's auth.tokens entry must list
    # this name in allowed_clients.
    self_name: str = ""
    # Outbound peer key — looks up base_url + token in
    # transport.peers[<this>].
    target_peer: str = "salem"
    schedule: ScheduleConfig = field(
        default_factory=lambda: ScheduleConfig(
            time="05:30", timezone="America/Halifax",
        ),
    )
    # Optional: where to scan for git activity. Defaults populated by
    # load_from_unified to KAL-LE's two repos.
    repo_paths: list[str] = field(default_factory=list)
    # Override the data dir scanned for bash_exec + instructor state.
    # Empty string → use ``logging.dir`` from the unified config.
    data_dir: str = ""
    # Optional BIT state path — when empty AND no bit_state.json found
    # in data_dir, posture defaults to green / no-data per the
    # assembler's docstring.
    bit_state_path: str = ""


def load_brief_digest_push_config(raw: dict[str, Any]) -> BriefDigestPushConfig:
    """Build a :class:`BriefDigestPushConfig` from the unified config.

    Defaults are tuned for KAL-LE; STAY-C overrides ``self_name`` +
    ``target_peer`` + ``repo_paths`` in its own config.
    """
    section = raw.get("brief_digest_push") or {}
    if not isinstance(section, dict):
        return BriefDigestPushConfig(enabled=False)

    schedule_raw = section.get("schedule", {}) or {}
    schedule = ScheduleConfig(
        time=str(schedule_raw.get("time", "05:30")),
        timezone=str(schedule_raw.get("timezone", "America/Halifax")),
    )

    repo_paths_raw = section.get("repo_paths") or []
    repo_paths = [str(p) for p in repo_paths_raw if isinstance(p, str)]

    data_dir = str(section.get("data_dir", "") or "")
    if not data_dir:
        data_dir = str(raw.get("logging", {}).get("dir", "./data"))

    return BriefDigestPushConfig(
        enabled=bool(section.get("enabled", False)),
        self_name=str(section.get("self_name", "") or ""),
        target_peer=str(section.get("target_peer", "salem") or "salem"),
        schedule=schedule,
        repo_paths=repo_paths,
        data_dir=data_dir,
        bit_state_path=str(section.get("bit_state_path", "") or ""),
    )


# ---------------------------------------------------------------------------
# Single fire — assemble + push
# ---------------------------------------------------------------------------


async def fire_once(
    config: BriefDigestPushConfig,
    transport_config: TransportConfig,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    """Build today's digest and push it to the target peer.

    Returns a result dict with ``ok``, ``date``, ``digest_length``,
    and ``response`` (the server's reply when push succeeded). On
    failure: ``ok: False`` + ``error`` + ``error_type``.

    Failure is non-fatal at the daemon level — the loop logs and
    continues so the next day's fire still runs.
    """
    today = today or _local_today(config.schedule.timezone)
    today_iso = today.isoformat()

    bit_path: Path | None = None
    if config.bit_state_path:
        bit_path = Path(config.bit_state_path)
    else:
        candidate = Path(config.data_dir) / "bit_state.json"
        if candidate.exists():
            bit_path = candidate

    digest_md = assemble_digest(
        today=today,
        data_dir=Path(config.data_dir),
        repo_paths=[Path(p) for p in config.repo_paths],
        bit_state_path=bit_path,
    )

    log.info(
        "kalle.brief_digest.assembled",
        date=today_iso,
        digest_length=len(digest_md),
        target_peer=config.target_peer,
        self_name=config.self_name,
    )

    try:
        response = await peer_send_brief_digest(
            config.target_peer,
            digest_markdown=digest_md,
            digest_date=today_iso,
            self_name=config.self_name,
            config=transport_config,
        )
    except TransportError as exc:
        log.warning(
            "kalle.brief_digest.push_failed",
            date=today_iso,
            target_peer=config.target_peer,
            error=str(exc),
            error_type=exc.__class__.__name__,
            response_summary=f"{exc.__class__.__name__}: {exc}",
        )
        return {
            "ok": False,
            "date": today_iso,
            "digest_length": len(digest_md),
            "error": str(exc),
            "error_type": exc.__class__.__name__,
        }
    except Exception as exc:  # noqa: BLE001 — transport may raise unexpected types
        log.warning(
            "kalle.brief_digest.push_failed",
            date=today_iso,
            target_peer=config.target_peer,
            error=str(exc),
            error_type=exc.__class__.__name__,
            response_summary=f"{exc.__class__.__name__}: {exc}",
        )
        return {
            "ok": False,
            "date": today_iso,
            "digest_length": len(digest_md),
            "error": str(exc),
            "error_type": exc.__class__.__name__,
        }

    log.info(
        "kalle.brief_digest.pushed",
        date=today_iso,
        target_peer=config.target_peer,
        digest_length=len(digest_md),
        response_path=str(response.get("path", "")) if isinstance(response, dict) else "",
    )
    return {
        "ok": True,
        "date": today_iso,
        "digest_length": len(digest_md),
        "response": response,
    }


def _local_today(tz_name: str) -> date:
    """Today as anchored in the configured wall-clock timezone."""
    return datetime.now(ZoneInfo(tz_name)).date()


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------


async def run_daemon(
    config: BriefDigestPushConfig,
    transport_config: TransportConfig,
) -> None:
    """Daily loop: sleep until ``schedule.time`` ADT, fire, repeat."""
    log.info(
        "kalle.brief_digest.daemon.starting",
        schedule_time=config.schedule.time,
        tz=config.schedule.timezone,
        target_peer=config.target_peer,
        self_name=config.self_name,
    )

    while True:
        tz = ZoneInfo(config.schedule.timezone)
        now = datetime.now(tz)
        target = compute_next_fire(config.schedule, now)
        sleep_seconds = (target - now).total_seconds()

        if sleep_seconds > 0:
            log.info(
                "kalle.brief_digest.daemon.sleeping",
                next_run=target.isoformat(),
                sleep_seconds=round(sleep_seconds, 1),
                sleep_hours=round(sleep_seconds / 3600, 1),
            )
            actual_seconds = await sleep_until(target)
            log.info(
                "kalle.brief_digest.daemon.woke",
                intended_seconds=round(sleep_seconds, 1),
                actual_seconds=round(actual_seconds, 1),
                drift_seconds=round(actual_seconds - sleep_seconds, 1),
            )

        try:
            await fire_once(config, transport_config)
        except Exception:  # noqa: BLE001 — daemon-level safety net
            log.exception("kalle.brief_digest.daemon.fire_error")

        # 60s buffer — same as brief.daemon — to avoid double-firing
        # if the wall clock drifts back across the target moment.
        await asyncio.sleep(60)
