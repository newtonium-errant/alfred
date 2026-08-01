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

from .kalle_digest import assemble_digest, assemble_ticket_pipeline_section
from .utils import get_logger
from .vera_ticket_digest import assemble_ticket_digest

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
    # Digest assembler selector (VERA P2, 2026-06-09). Picks which
    # assembler ``fire_once`` invokes:
    #   * ``"git_activity"`` (DEFAULT) — KAL-LE's git-commit + BIT posture
    #     digest via ``kalle_digest.assemble_digest`` (uses repo_paths /
    #     data_dir / bit_state_path below). Default preserves KAL-LE's
    #     behaviour byte-identically — KAL-LE's config omits ``source``.
    #   * ``"tickets"`` — VERA's open-ticket snapshot via
    #     ``vera_ticket_digest.assemble_ticket_digest`` (uses vault_path
    #     below). Re-surfaces all open/in_progress tickets each morning.
    source: str = "git_activity"
    # VERA ticket source: the vault root holding the ``ticket/`` dir.
    # Only consulted when ``source == "tickets"``. Empty for KAL-LE.
    vault_path: str = ""
    # Optional: where to scan for git activity. Defaults populated by
    # load_from_unified to KAL-LE's two repos. (git_activity source only.)
    repo_paths: list[str] = field(default_factory=list)
    # Override the data dir scanned for bash_exec + instructor state.
    # Empty string → use ``logging.dir`` from the unified config.
    # (git_activity source only.)
    data_dir: str = ""
    # Optional BIT state path — when empty AND no bit_state.json found
    # in data_dir, posture defaults to green / no-data per the
    # assembler's docstring. (git_activity source only.)
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

    # Digest source selector (VERA P2). Default "git_activity" keeps
    # KAL-LE's behaviour when the key is absent. "tickets" selects the
    # VERA open-ticket assembler. Any other value falls through to the
    # default at fire time (fire_once logs + uses git_activity).
    source = str(section.get("source", "git_activity") or "git_activity")

    # VERA ticket source vault root. Falls back to the unified config's
    # ``vault.path`` when omitted from the brief_digest_push block, so a
    # VERA config that sets ``source: tickets`` doesn't have to repeat
    # its vault path. Empty for KAL-LE (git_activity ignores it).
    vault_path = str(section.get("vault_path", "") or "")
    if not vault_path:
        vault_path = str((raw.get("vault", {}) or {}).get("path", "") or "")

    return BriefDigestPushConfig(
        enabled=bool(section.get("enabled", False)),
        self_name=str(section.get("self_name", "") or ""),
        target_peer=str(section.get("target_peer", "salem") or "salem"),
        schedule=schedule,
        source=source,
        vault_path=vault_path,
        repo_paths=repo_paths,
        data_dir=data_dir,
        bit_state_path=str(section.get("bit_state_path", "") or ""),
    )


# ---------------------------------------------------------------------------
# BIT posture — state-path resolution
# ---------------------------------------------------------------------------


def _resolve_bit_state_path(
    config: BriefDigestPushConfig,
    raw: dict[str, Any] | None,
) -> Path | None:
    """Resolve the BIT state file the posture line should read, or ``None``.

    Precedence:
      1. ``brief_digest_push.bit_state_path`` — explicit override wins.
      2. The BIT config's own ``state.path`` — SINGLE SOURCE OF TRUTH when
         this instance runs BIT (``bit:`` section present). Sourcing from
         ``alfred.bit.config`` (rather than re-deriving
         ``data_dir/bit_state.json``) means a customized ``bit.state.path``
         is honored instead of silently missed — the failure mode behind
         a "no BIT data" posture on an instance that DOES run BIT. For
         KAL-LE's default config both resolve to the same file.
      3. ``data_dir/bit_state.json`` — legacy fallback for instances with
         no ``bit:`` section (posture correctly reads "no BIT data").

    ILB: when ``bit:`` is configured but its state file is absent, log the
    resolved path so a "no BIT data" posture on a BIT-running instance is
    grep-able (self-diagnosing) instead of indistinguishable from an
    instance that simply runs no BIT.
    """
    if config.bit_state_path:
        return Path(config.bit_state_path)

    if isinstance(raw, dict) and isinstance(raw.get("bit"), dict):
        from alfred.bit.config import load_from_unified as load_bit_config

        configured = Path(load_bit_config(raw).state.path)
        if configured.exists():
            return configured
        log.info(
            "kalle.brief_digest.bit_state_missing",
            configured_path=str(configured),
            self_name=config.self_name,
            detail=(
                "bit: section present but its state file was not found — "
                "posture renders 'no BIT data' this digest"
            ),
        )
        return None

    candidate = Path(config.data_dir) / "bit_state.json"
    if candidate.exists():
        return candidate
    return None


# ---------------------------------------------------------------------------
# Assembler selection (VERA P2)
# ---------------------------------------------------------------------------


def _assemble_for_source(
    config: BriefDigestPushConfig,
    today: date,
    raw: dict[str, Any] | None = None,
) -> str:
    """Dispatch to the right digest assembler per ``config.source``.

    * ``"tickets"`` → VERA open-ticket snapshot
      (``vera_ticket_digest.assemble_ticket_digest``), scanning
      ``config.vault_path / ticket/``. When the unified config dict is
      available (the daemon path), the forwarder state path from its
      ``ticket_forward`` section is threaded through so each line can
      carry its pipeline forward-status tail (c5).
    * anything else (default ``"git_activity"``) → KAL-LE's
      git-commit + BIT posture digest (``kalle_digest.assemble_digest``).
      Byte-identical to the pre-VERA-P2 code path — an unknown source
      value falls through here too (logged) rather than raising, so a
      config typo degrades to the KAL-LE digest instead of crashing the
      daemon loop. (The c5 ticket-pipeline section is appended by
      ``fire_once``, not here — this function stays sync + mockable.)

    Kept as a pure function (no I/O beyond the assemblers' own reads, no
    push) so tests can exercise the branch selection directly.
    """
    if config.source == "tickets":
        forward_state_path: str | None = None
        if isinstance(raw, dict):
            from alfred.transport.ticket_forward import (
                load_ticket_forward_config,
            )
            forward_state_path = load_ticket_forward_config(raw).state_path
        return assemble_ticket_digest(
            today=today,
            vault_path=Path(config.vault_path),
            forward_state_path=forward_state_path,
        )

    if config.source != "git_activity":
        # Unknown source — fall through to the KAL-LE default but log so
        # a config typo is grep-able rather than silently mis-routing.
        log.warning(
            "kalle.brief_digest.unknown_source",
            source=config.source,
            detail="falling back to git_activity assembler",
        )

    bit_path = _resolve_bit_state_path(config, raw)

    return assemble_digest(
        today=today,
        data_dir=Path(config.data_dir),
        repo_paths=[Path(p) for p in config.repo_paths],
        bit_state_path=bit_path,
    )


# ---------------------------------------------------------------------------
# Ticket-pipeline section gating
# ---------------------------------------------------------------------------


def _ticket_pipeline_configured(raw: dict[str, Any] | None) -> bool:
    """True when this instance runs the VERA→KAL-LE→GitHub ticket pipeline.

    The signal is a ``ticket_intake:`` section in the unified config: the
    digest's Ticket pipeline section reads EXCLUSIVELY from ticket-intake
    state (plus ``github:`` for the PR outcome check), so an instance
    without ``ticket_intake`` has no pipeline to report on. Config-driven
    gate — NOT a hardcoded instance-name check: KAL-LE has
    ``ticket_intake:``, Hypatia does not. Matches
    ``load_ticket_intake_config``'s own "absent / malformed → disabled"
    tolerance (a present-but-non-dict section counts as absent).
    """
    return isinstance(raw, dict) and isinstance(raw.get("ticket_intake"), dict)


# ---------------------------------------------------------------------------
# Single fire — assemble + push
# ---------------------------------------------------------------------------


async def fire_once(
    config: BriefDigestPushConfig,
    transport_config: TransportConfig,
    *,
    today: date | None = None,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build today's digest and push it to the target peer.

    Returns a result dict with ``ok``, ``date``, ``digest_length``,
    and ``response`` (the server's reply when push succeeded). On
    failure: ``ok: False`` + ``error`` + ``error_type``.

    ``raw`` is the unified config dict (threaded from the orchestrator
    runner) — the c5 ticket-pipeline section and the VERA forward-status
    tails read their state paths + github config from it. The c5 section
    is only appended when ``raw`` carries a ``ticket_intake:`` section
    (this instance runs the ticket pipeline); ``None`` or a config with
    no ``ticket_intake`` (old callers/tests, Hypatia) omits the section
    and logs the skip per ILB.

    Failure is non-fatal at the daemon level — the loop logs and
    continues so the next day's fire still runs.
    """
    today = today or _local_today(config.schedule.timezone)
    today_iso = today.isoformat()

    digest_md = _assemble_for_source(config, today, raw)

    if config.source != "tickets" and _ticket_pipeline_configured(raw):
        # Ticket-pipeline section (pipeline c5) — KAL-LE-digest-family AND
        # ticket-pipeline-configured only. The "tickets" source is VERA's
        # snapshot (per-line forward-status tails instead); an instance
        # with no ``ticket_intake:`` (e.g. Hypatia) has no pipeline to
        # report, so the section is omitted entirely rather than rendering
        # a misleading "no tickets received yet" line for a pipeline it
        # doesn't run. Awaited HERE because the github_ops client is async
        # and fire_once is the assembler call's nearest async context.
        # Internally §-contained: a section failure degrades to its
        # "section unavailable" line, never a missing digest. Rendered on
        # EVERY digest of a ticket-pipeline instance (ILB).
        section = await assemble_ticket_pipeline_section(raw)
        digest_md = f"{digest_md}\n\n{section}"
    elif config.source != "tickets":
        # ILB: this instance runs no ticket pipeline (no ``ticket_intake:``
        # config) — omit the section, but log so "omitted because
        # unconfigured" is distinguishable from a broken/dropped section.
        log.info(
            "kalle.brief_digest.ticket_pipeline_section_skipped",
            reason="no_ticket_intake_config",
            self_name=config.self_name,
        )

    log.info(
        "kalle.brief_digest.assembled",
        date=today_iso,
        digest_length=len(digest_md),
        target_peer=config.target_peer,
        self_name=config.self_name,
        source=config.source,
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
    raw: dict[str, Any] | None = None,
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
            await fire_once(config, transport_config, raw=raw)
        except Exception:  # noqa: BLE001 — daemon-level safety net
            log.exception("kalle.brief_digest.daemon.fire_error")

        # 60s buffer — same as brief.daemon — to avoid double-firing
        # if the wall clock drifts back across the target moment.
        await asyncio.sleep(60)
