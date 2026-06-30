"""Routing daemon — scan spool → validate → dedup → place → notify.

The structural mirror of ``transport.ticket_forward``, generalized from
"one GitHub destination over HTTP" to "N project inbox dirs over the
shared filesystem". Deterministic, LLM-free, per-item isolated (one bad
record never kills the tick), ILB-every-tick, ``enabled`` kill-switch.

Idempotency: keyed by message ``id`` in :class:`MessageBusState`. A
re-dropped file with a known id is archived to ``routed/`` without
re-placing; a torn placement (placed but crashed before ``state.save``)
re-places to the SAME id-keyed destination filename via atomic write =
at-least-once placement + idempotent target = effectively once.
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from .config import MessageBusConfig
from .record import (
    MessageRecord,
    message_filename,
    parse_message_file,
    validate_record,
    write_message_file,
)
from .registry import ProjectRegistry
from .state import MessageBusEntry, MessageBusState

log = structlog.get_logger(__name__)


ROUTED_BY = "kalle"

# Spool sub-dirs (siblings of the pending drop point).
_ROUTED_DIR = "routed"
_MALFORMED_DIR = "malformed"
_UNDELIVERABLE_DIR = "undeliverable"
# Suffix for a file we FAILED to move out of the spool — renaming it out of
# the ``*.md`` glob stops it being re-parsed + re-counted every tick.
_QFAIL_SUFFIX = ".qfail"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mint_message_id(
    from_project: str,
    to_project: str,
    created: str,
    subject: str,
    body: str = "",
) -> str:
    """Mint the stable message id — ``msg-<YYYYMMDD>-<sha8>``.

    Pure + pinned-stable by test (mirrors ``mint_ticket_uid``). The date
    part comes from ``created`` (its first 10 chars' digits), falling back
    to today; the hash keys on ``from|to|created|subject|body`` so the same
    inputs always mint the same id (a true re-drop dedups) while two
    DISTINCT messages sharing from/to/created/subject but differing in body
    get DISTINCT ids — without ``body`` in the hash the second would be
    silently dropped as a fake re-drop (a real no-loss bug)."""
    created_str = str(created or "")
    digits = "".join(ch for ch in created_str[:10] if ch.isdigit())
    date_part = digits if len(digits) == 8 else date.today().strftime("%Y%m%d")
    digest = hashlib.sha256(
        f"{from_project}|{to_project}|{created_str}|{subject}|{body}".encode("utf-8"),
    ).hexdigest()[:8]
    return f"msg-{date_part}-{digest}"


@dataclass
class ScanResult:
    """Outcome of one spool scan. ``eligible`` is the list of records to
    route; the counts feed the ILB tick. Malformed / undeliverable /
    already-routed files have ALREADY been moved out of the spool root."""

    scanned: int = 0
    eligible: list[MessageRecord] = None  # type: ignore[assignment]
    eligible_paths: list[Path] = None  # type: ignore[assignment]
    malformed: int = 0
    undeliverable: int = 0
    skipped_dup: int = 0

    def __post_init__(self) -> None:
        if self.eligible is None:
            self.eligible = []
        if self.eligible_paths is None:
            self.eligible_paths = []


def _quarantine(path: Path, spool_root: Path, subdir: str, reason: str) -> None:
    """Move a spool file into a quarantine/archive sub-dir. Never raises.

    On a move failure, RENAME the file in place to ``<name>.qfail`` so it
    drops out of the ``*.md`` glob — otherwise an un-movable file would be
    re-parsed + re-counted every tick (metric inflation + log spam). If
    even the rename fails, log once and leave it (bounded by the rename
    fallback in practice)."""
    dest_dir = spool_root / subdir
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest_dir / path.name))
        return
    except OSError as exc:
        log.warning(
            "msgbus.route.quarantine_move_failed",
            path=str(path),
            subdir=subdir,
            reason=reason,
            error_type=exc.__class__.__name__,
        )
    try:
        path.rename(path.with_name(path.name + _QFAIL_SUFFIX))
    except OSError as exc:
        log.warning(
            "msgbus.route.quarantine_rename_failed",
            path=str(path),
            error_type=exc.__class__.__name__,
        )


def _id_in_dir(directory: Path, message_id: str) -> bool:
    """True if a placed message file for ``message_id`` exists in
    ``directory`` (the id is the trailing filename segment
    ``…-<id>.md``). Filename scan — no parse. Used for the consumer-side
    read/ dedup (already-delivered-and-read)."""
    if not message_id or not directory.exists():
        return False
    return any(message_id in p.name for p in directory.glob("*.md"))


def scan_spool(
    spool_path: str | Path,
    registry: ProjectRegistry,
    state: MessageBusState,
) -> ScanResult:
    """Walk ``<spool>/*.md`` (sorted, NON-recursive — the quarantine
    sub-dirs are skipped) and classify each file.

      * bad parse / missing required field / bad kind → ``malformed/``
      * ``to`` not in the registry → ``undeliverable/`` + an
        ``msgbus.route.unknown_destination`` log
      * ``id`` already in state → already routed → archive to ``routed/``
      * otherwise → eligible (returned for placement)

    Quarantine + already-routed moves happen here (the spool janitor);
    placement of eligible records is :func:`run_route_once`'s job."""
    spool_root = Path(spool_path)
    result = ScanResult()
    if not spool_root.exists():
        log.info("msgbus.route.no_spool_dir", spool_path=str(spool_root))
        return result

    for md_file in sorted(spool_root.glob("*.md")):
        if not md_file.is_file():
            continue
        result.scanned += 1
        try:
            record = parse_message_file(md_file)
        except Exception as exc:  # noqa: BLE001 — one bad file never kills the scan
            log.warning(
                "msgbus.route.parse_failed",
                path=str(md_file),
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            result.malformed += 1
            _quarantine(md_file, spool_root, _MALFORMED_DIR, "parse_failed")
            continue

        # Structural validation (the id is mint-if-absent below, so a
        # missing id is NOT malformed — every OTHER missing field / bad
        # kind is).
        structural = [
            e for e in validate_record(record) if e != "missing id"
        ]
        if structural:
            log.warning(
                "msgbus.route.malformed",
                path=str(md_file),
                errors=structural,
            )
            result.malformed += 1
            _quarantine(md_file, spool_root, _MALFORMED_DIR, "; ".join(structural))
            continue

        if registry.get(record.to_project) is None:
            log.warning(
                "msgbus.route.unknown_destination",
                path=str(md_file),
                to=record.to_project,
                known=registry.names(),
            )
            result.undeliverable += 1
            _quarantine(
                md_file, spool_root, _UNDELIVERABLE_DIR, "unknown_destination",
            )
            continue

        # Dedup: a known id was already routed on a prior tick → archive
        # the re-drop without re-placing.
        if record.id and record.id in state.entries:
            result.skipped_dup += 1
            _quarantine(md_file, spool_root, _ROUTED_DIR, "already_routed")
            continue

        result.eligible.append(record)
        result.eligible_paths.append(md_file)

    return result


async def run_route_once(
    config: MessageBusConfig,
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Run one routing tick. Returns the summary dict for CLI/tests.

    Per eligible message: mint id if absent, stamp ``routed_at``/
    ``routed_by``, ATOMICALLY place into ``<dest_inbox>/<id-keyed-name>``,
    record the state entry, ``state.save()``, archive the spool file to
    ``routed/``. Per-item isolated. Emits ``msgbus.route.tick`` (ILB) EVERY
    call — zero-work ticks included — so an idle bus is distinguishable
    from a broken one. Fires the optional operator ping after any tick that
    routed ≥1 message."""
    registry = config.registry()
    state = MessageBusState.load(config.state_path)
    spool_root = Path(config.spool_path)

    # ILB on scan-FAILURE: a scan-level error (unreadable spool, NotADir)
    # must still emit a tick so the every-cycle guarantee holds.
    try:
        scan = scan_spool(config.spool_path, registry, state)
    except Exception as exc:  # noqa: BLE001 — scan-level failure
        log.warning(
            "msgbus.route.scan_failed",
            spool_path=str(spool_root),
            error_type=exc.__class__.__name__,
        )
        empty = {
            "scanned": 0, "routed": 0, "skipped_dup": 0,
            "malformed": 0, "undeliverable": 0, "failed": 0,
        }
        log.info("msgbus.route.tick", scan_error=True, **empty)
        return {**empty, "scan_error": True, "results": [], "by_destination": {}}

    failed = 0
    post_mint_skipped = 0
    results: list[dict[str, Any]] = []
    by_destination: dict[str, int] = {}
    # Count DISTINCT placed ids so `routed` can never exceed files actually
    # placed (loss-safe counter, independent of the dedup gates).
    placed_ids: set[str] = set()

    for record, src_path in zip(scan.eligible, scan.eligible_paths):
        try:
            # Mint the id if absent — AFTER scan_spool's pre-mint dedup gate.
            if not record.id:
                record.id = mint_message_id(
                    record.from_project, record.to_project,
                    record.created, record.subject, record.body,
                )

            # POST-MINT dedup (gate A): scan_spool dedups on the PRE-mint id,
            # so an id-less file's minted id was never checked. State is
            # saved in-loop per message, so this single check catches BOTH
            # the 2nd-identical-in-tick AND the cross-tick re-drop-after-drain
            # — without it, id-less messages were lost (intra-tick overwrite)
            # and duplicated (re-delivered after drain).
            if record.id in state.entries or record.id in placed_ids:
                post_mint_skipped += 1
                _quarantine(src_path, spool_root, _ROUTED_DIR, "already_routed")
                results.append({"id": record.id, "outcome": "skipped_dup"})
                continue

            inbox = registry.inbox_for(record.to_project)
            if inbox is None:
                # Defensive: scan_spool already filtered unknown dests.
                failed += 1
                results.append({"id": record.id, "outcome": "no_inbox"})
                continue

            # CONSUMER-SIDE dedup (gate C): a place→state.save crash followed
            # by a destination drain would re-deliver an already-read message
            # (read-state is pure directory position). If a file for this id
            # already sits in the destination read/ dir, it was delivered +
            # read — skip the re-delivery.
            read_dir = registry.read_dir_for(record.to_project)
            if read_dir is not None and _id_in_dir(read_dir, record.id):
                post_mint_skipped += 1
                _quarantine(src_path, spool_root, _ROUTED_DIR, "already_read")
                results.append({"id": record.id, "outcome": "skipped_read"})
                continue

            record.routed_at = _now_iso()
            record.routed_by = ROUTED_BY
            dest_path = inbox / message_filename(record)

            # ATOMIC placement (tmp+os.replace via write_message_file);
            # id-keyed filename → a torn re-place overwrites the same target.
            write_message_file(dest_path, record)

            state.entries[record.id] = MessageBusEntry(
                id=record.id,
                from_project=record.from_project,
                to_project=record.to_project,
                kind=record.kind,
                correlation_id=record.correlation_id,
                routed_at=record.routed_at,
                dest_path=str(dest_path),
                attempts=1,
            )
            # Save state BEFORE archiving the spool file so a crash leaves
            # the record routed (id in state) — the re-drop on next tick is
            # then a no-op skip, never a duplicate placement.
            state.save()
            _quarantine(src_path, spool_root, _ROUTED_DIR, "routed")

            if record.id not in placed_ids:
                placed_ids.add(record.id)
                by_destination[record.to_project] = (
                    by_destination.get(record.to_project, 0) + 1
                )
            else:
                # Should be unreachable after the post-mint gate; keep the
                # counter honest + greppable if it ever fires.
                log.warning("msgbus.route.intra_tick_id_collision", id=record.id)
            log.info(
                "msgbus.route.placed",
                id=record.id,
                to=record.to_project,
                kind=record.kind,
                dest=str(dest_path),
            )
            results.append({
                "id": record.id,
                "to": record.to_project,
                "kind": record.kind,
                "outcome": "routed",
            })
        except Exception as exc:  # noqa: BLE001 — isolate per message
            log.warning(
                "msgbus.route.place_failed",
                id=record.id,
                to=record.to_project,
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            failed += 1
            results.append({
                "id": record.id, "to": record.to_project, "outcome": "failed",
            })

    summary = {
        "scanned": scan.scanned,
        # Loss-safe: distinct placed ids, never more than files placed.
        "routed": len(placed_ids),
        "skipped_dup": scan.skipped_dup + post_mint_skipped,
        "malformed": scan.malformed,
        "undeliverable": scan.undeliverable,
        "failed": failed,
    }
    # ILB: every tick emits the summary so idle is distinguishable from broken.
    log.info("msgbus.route.tick", **summary)

    if placed_ids and config.notify_telegram:
        _notify_operator(by_destination, raw)

    return {**summary, "results": results, "by_destination": by_destination}


async def run_daemon(
    config: MessageBusConfig,
    raw: dict[str, Any],
) -> None:
    """Interval loop: tick, sleep ``interval_minutes``, repeat. Per-tick
    exception containment — a bad tick logs + continues; the daemon never
    dies to a single failure."""
    log.info(
        "msgbus.daemon.starting",
        interval_minutes=config.interval_minutes,
        spool_path=config.spool_path,
        state_path=config.state_path,
        projects=registry_names(config),
    )
    while True:
        try:
            await run_route_once(config, raw)
        except Exception:  # noqa: BLE001 — daemon-level safety net
            log.exception("msgbus.daemon.tick_error")
        await asyncio.sleep(config.interval_minutes * 60)


def registry_names(config: MessageBusConfig) -> list[str]:
    return [p.name for p in config.projects]


# ---------------------------------------------------------------------------
# Operator notification (optional, best-effort) — Telegram push
# ---------------------------------------------------------------------------


def _telegram_send(token: str, chat_id: str, text: str) -> None:
    """Best-effort Telegram sendMessage (module-level so tests mock it).

    Synchronous httpx POST with a short timeout; the caller swallows
    failures (a notify failure never kills the routing tick)."""
    import httpx

    httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10.0,
    )


def _notify_operator(by_destination: dict[str, int], raw: dict[str, Any]) -> None:
    """Push a "project X has N inbound message(s)" ping to the operator.

    Gated behind ``message_bus.notify.telegram`` by the caller. Best-effort:
    a missing token / chat id or any send error logs + is swallowed."""
    tel = raw.get("telegram") or {}
    token = str(tel.get("bot_token", "") or "")
    allowed = tel.get("allowed_users") or []
    chat_id = str(allowed[0]) if isinstance(allowed, list) and allowed else ""
    if not token or "${" in token or not chat_id:
        log.info(
            "msgbus.route.notify_skipped",
            reason="no_bot_token_or_chat_id",
            by_destination=by_destination,
        )
        return
    lines = [
        f"{proj}: {n} inbound message(s)"
        for proj, n in sorted(by_destination.items())
    ]
    text = "📬 Inter-project bus routed:\n" + "\n".join(lines)
    try:
        _telegram_send(token, chat_id, text)
        log.info("msgbus.route.notified", by_destination=by_destination)
    except Exception as exc:  # noqa: BLE001 — never kills the tick
        # Log error_type ONLY — str(exc) on an httpx error can embed the
        # request URL, which carries the bot token (/bot<token>/sendMessage).
        log.warning(
            "msgbus.route.notify_failed",
            error_type=exc.__class__.__name__,
        )


__all__ = [
    "ROUTED_BY",
    "ScanResult",
    "mint_message_id",
    "run_daemon",
    "run_route_once",
    "scan_spool",
]
