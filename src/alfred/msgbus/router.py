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
    MESSAGE_KINDS,
    MessageRecord,
    message_filename,
    parse_message_file,
    validate_record,
    write_message_file,
)
from .registry import ProjectRegistry
from .state import MessageBusEntry, MessageBusState

# Layer-2 contract kinds — imported from the pure contracts.schema (NO
# msgbus↔contracts load cycle: schema imports nothing from msgbus; the
# contracts.router import is lazy, inside run_route_once). The bus accepts
# these kinds (so they aren't malform-quarantined) and dispatches them to
# the contract solver instead of plain inbox-routing.
from alfred.contracts.schema import CONTRACT_KINDS

# Kinds the spool scanner accepts as structurally valid.
_ACCEPTED_KINDS: frozenset[str] = MESSAGE_KINDS | CONTRACT_KINDS

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
    bounced: int = 0        # malformed drops that got a BOUNCE into the sender inbox
    kind_tolerated: int = 0  # unknown-kind messages accepted as fyi + tagged (not binned)

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


def _write_bounce(
    record: MessageRecord, errors: list[str], binned_name: str,
    registry: ProjectRegistry,
) -> bool:
    """Write a BOUNCE (kind=reply) into the SENDER's inbox so a malformed drop is not
    SILENTLY binned (the 2026-07-16 intentionally-left-blank incident: a time-sensitive
    cross-project request was quarantined with no signal, found only because the operator
    knew to look). Returns True iff a bounce was written. Best-effort + NEVER raises — a
    bounce failure must not crash the scan. Only fires when the sender is identifiable AND
    registered; a message with a missing/unknown ``from`` cannot be bounced (the
    receiver-side malformed-bin count is the backstop for those)."""
    sender = record.from_project
    if not sender or registry.get(sender) is None:
        log.warning(
            "msgbus.route.bounce_skipped_no_sender",
            from_project=sender or "(missing)", id=record.id, errors=errors,
            detail="malformed message binned but sender unknown/unregistered — cannot "
                   "bounce; the receiver-side malformed-bin count is the backstop",
        )
        return False
    inbox = registry.inbox_for(sender)
    if inbox is None:
        return False
    created = _now_iso()
    subject = f"BOUNCED malformed: {record.subject or '(no subject)'}"
    body = (
        "Your message was BINNED as malformed by the message bus and NOT delivered.\n\n"
        f"- original id: {record.id or '(unminted)'}\n"
        f"- original kind: {record.original_kind or record.kind or '(missing)'}\n"
        f"- addressed to: {record.to_project or '(missing)'}\n"
        f"- validation errors: {'; '.join(errors)}\n"
        f"- binned file: {_MALFORMED_DIR}/{binned_name}\n\n"
        "Fix the flagged field(s) and re-send. (An unknown `kind` is NOT a bounce cause — "
        "those are accepted as `fyi` with a tag; this bounce means a STRUCTURAL field was "
        "missing.)"
    )
    bounce = MessageRecord(
        from_project=ROUTED_BY, to_project=sender, kind="reply",
        correlation_id=record.correlation_id or record.id or "bounce",
        created=created, subject=subject, body=body,
        reply_to=record.id, precedence="R", routed_at=created, routed_by=ROUTED_BY,
    )
    bounce.id = mint_message_id(
        bounce.from_project, bounce.to_project, created, subject, body)
    try:
        write_message_file(inbox / message_filename(bounce), bounce)
    except Exception as exc:  # noqa: BLE001 — a bounce write must never crash the scan
        log.warning("msgbus.route.bounce_write_failed", id=bounce.id, error=str(exc))
        return False
    log.info(
        "msgbus.route.bounced", to=sender, bounce_id=bounce.id,
        original_id=record.id, errors=errors,
    )
    return True


def malformed_counts_by_project(spool_path: str | Path) -> dict[str, int]:
    """Count malformed-bin files grouped by the project they were ADDRESSED TO (``to``) —
    the RECEIVER-side signal for the intentionally-left-blank fix: a routine inbox drain
    would otherwise never learn a message meant for it was quarantined. Files that don't
    parse / carry no ``to`` count under the ``"?"`` bucket (unattributable to a receiver).
    Never raises (a bad file is skipped). Cheap enough for the status/inbox surfaces."""
    counts: dict[str, int] = {}
    mdir = Path(spool_path) / _MALFORMED_DIR
    if not mdir.exists():
        return counts
    for md_file in sorted(mdir.glob("*.md")):
        if not md_file.is_file():
            continue
        try:
            key = parse_message_file(md_file).to_project or "?"
        except Exception:  # noqa: BLE001 — an unparseable malformed file is unattributed
            key = "?"
        counts[key] = counts.get(key, 0) + 1
    return counts


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

        # Structural validation. The id is mint-if-absent below (a missing id is NOT
        # malformed). A present-but-UNKNOWN ``kind`` is EXCLUDED here — it is tolerable enum
        # drift, handled AFTER this gate (below): a message that is ALSO structurally broken
        # must be BINNED + BOUNCED reporting the REAL kind, and must NOT be double-counted as
        # tolerated. Accept BOTH plain message kinds AND contract kinds. (An EMPTY "missing
        # kind" STAYS structural — a message that named no kind at all IS broken.)
        structural = [
            e for e in validate_record(record, valid_kinds=_ACCEPTED_KINDS)
            if e != "missing id" and not e.startswith("invalid kind:")
        ]
        if structural:
            log.warning(
                "msgbus.route.malformed",
                path=str(md_file),
                errors=structural,
            )
            result.malformed += 1
            binned_name = md_file.name  # _quarantine preserves the name on the happy path
            _quarantine(md_file, spool_root, _MALFORMED_DIR, "; ".join(structural))
            # BOUNCE to the sender so the bin is not silent (intentionally-left-blank). The
            # record's kind is NOT rewritten (tolerance is deferred below), so the bounce
            # reports the REAL kind the sender used.
            if _write_bounce(record, structural, binned_name, registry):
                result.bounced += 1
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

        # TOLERANT+TAG (applied ONLY to a message that is otherwise ELIGIBLE — structurally
        # sound + deliverable + not a dup): a present-but-UNKNOWN kind is enum DRIFT between
        # projects, not a broken message — accept it as ``fyi`` and RECORD the original so the
        # receiver sees the drift (the 2026-07-16 incident: rrts sent an unknown kind). Doing
        # this AFTER the gates means a binned/undeliverable message is never tolerated-counted
        # and its bounce reports the real kind. Known message + contract kinds pass untouched.
        if record.kind and record.kind not in _ACCEPTED_KINDS:
            log.warning(
                "msgbus.route.unknown_kind_tolerated",
                path=str(md_file), id=record.id,
                original_kind=record.kind, to=record.to_project,
                detail="unknown kind accepted as fyi + tagged (schema-tolerance); NOT binned",
            )
            record.original_kind = record.kind
            record.kind = "fyi"
            result.kind_tolerated += 1

        result.eligible.append(record)
        result.eligible_paths.append(md_file)

    return result


async def _run_route_once_locked(
    config: MessageBusConfig,
    raw: dict[str, Any],
) -> dict[str, Any]:
    """The routing sweep body — runs ONLY while holding the spool lock (its
    sole caller is :func:`run_route_once`, which acquires the lock first).

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
            "scanned": 0, "routed": 0, "contracts_applied": 0,
            "skipped_dup": 0, "malformed": 0, "bounced": 0, "kind_tolerated": 0,
            "undeliverable": 0, "failed": 0,
        }
        log.info("msgbus.route.tick", scan_error=True, **empty)
        return {**empty, "scan_error": True, "results": [], "by_destination": {}}

    failed = 0
    post_mint_skipped = 0
    contracts_applied = 0
    runtime_undeliverable = 0
    # Independent off-switch: contract messages are only dispatched to the
    # solver when ``contracts.enabled``. A bus-on/contracts-off box cleanly
    # quarantines them (cheap dict read — no contracts import for the gate,
    # keeping the bus decoupled).
    contracts_enabled = bool((raw.get("contracts") or {}).get("enabled"))
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
            # and duplicated (re-delivered after drain). For a CONTRACT
            # message this also prevents a double-APPLY (a re-applied counter
            # would double-bump the version).
            if record.id in state.entries or record.id in placed_ids:
                post_mint_skipped += 1
                _quarantine(src_path, spool_root, _ROUTED_DIR, "already_routed")
                results.append({"id": record.id, "outcome": "skipped_dup"})
                continue

            # LAYER-2 DISPATCH: a contract-kind message is handed to the
            # contract solver instead of being placed as a plain inbox
            # message. Lazy import keeps the bus decoupled + dormant (only
            # imported when such a message actually appears). The bus id is
            # recorded in state (dedup) + the spool file archived, exactly
            # like a routed message, so a re-drop is a no-op skip.
            if record.kind in CONTRACT_KINDS:
                if not contracts_enabled:
                    # Off-switch: don't process contract messages on a box
                    # with the bus on but contracts off — quarantine instead.
                    log.warning(
                        "msgbus.route.contracts_disabled",
                        id=record.id, kind=record.kind,
                    )
                    _quarantine(
                        src_path, spool_root, _UNDELIVERABLE_DIR,
                        "contracts_disabled",
                    )
                    runtime_undeliverable += 1
                    results.append({
                        "id": record.id, "outcome": "contracts_disabled",
                    })
                    continue
                from alfred.contracts.router import handle_bus_contract_message
                cres = handle_bus_contract_message(
                    src_path, registry=registry, raw=raw,
                )
                state.entries[record.id] = MessageBusEntry(
                    id=record.id, from_project=record.from_project,
                    to_project=record.to_project, kind=record.kind,
                    correlation_id=record.correlation_id,
                    routed_at=_now_iso(), dest_path="(contract)", attempts=1,
                )
                state.save()
                _quarantine(src_path, spool_root, _ROUTED_DIR, "contract")
                contracts_applied += 1
                results.append({
                    "id": record.id,
                    "kind": record.kind,
                    "outcome": "contract_applied" if cres.get("ok") else "contract_rejected",
                    "contract_id": cres.get("contract_id", ""),
                })
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
        "contracts_applied": contracts_applied,
        "skipped_dup": scan.skipped_dup + post_mint_skipped,
        "malformed": scan.malformed,
        "bounced": scan.bounced,
        "kind_tolerated": scan.kind_tolerated,
        "undeliverable": scan.undeliverable + runtime_undeliverable,
        "failed": failed,
    }
    # ILB: every tick emits the summary so idle is distinguishable from broken.
    log.info("msgbus.route.tick", **summary)

    if placed_ids and config.notify_telegram:
        _notify_operator(by_destination, raw)

    return {**summary, "results": results, "by_destination": by_destination}


# Interactive route-on-send waits up to this long for an in-flight sweep to
# release before routing its own file — so a --route message that races a cron
# tick which already snapshotted the spool BEFORE the mint is delivered PROMPTLY
# (a couple seconds), not stranded to the next 5-min cron tick. Background ticks
# (cron/daemon) use lock_wait=0 (non-blocking skip) — they re-tick anyway.
ROUTE_NOW_LOCK_WAIT_SECONDS = 3.0


def _skipped_locked_result() -> dict[str, Any]:
    return {
        "scanned": 0, "routed": 0, "contracts_applied": 0,
        "skipped_dup": 0, "malformed": 0, "bounced": 0, "kind_tolerated": 0,
        "undeliverable": 0,
        "failed": 0, "skipped_locked": True, "results": [],
        "by_destination": {},
    }


async def _acquire_route_lock(lock_file: Any, lock_wait: float) -> bool:
    """Try to take the exclusive spool lock. ``lock_wait <= 0`` → a single
    non-blocking attempt (returns False immediately if held). ``lock_wait >
    0`` → poll up to that many seconds (the interactive route-on-send path).
    Returns True iff acquired."""
    import fcntl
    import time

    deadline = time.monotonic() + max(0.0, lock_wait)
    while True:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)


async def run_route_once(
    config: MessageBusConfig,
    raw: dict[str, Any],
    *,
    lock_wait: float = 0.0,
) -> dict[str, Any]:
    """Run one routing tick, SERIALIZED on ``<spool>/.route.lock`` (Path B).

    THE CONCURRENCY GUARD — the lock lives HERE, not in the caller, so EVERY
    routing entry point (``route_now`` CLI, the cron ``msg route-once``, AND
    the daemon loop) serializes on it. Route-on-send makes the router
    concurrent for the first time: a ``--route`` sweep at the same instant as
    a 5-min cron/daemon tick. The per-item dedup gate (``if record.id in
    state.entries``) only protects SEQUENTIAL re-drops — state is saved
    BETWEEN ticks. Two CONCURRENT sweeps both ``MessageBusState.load()``
    before either ``.save()``, both pass the gate, and both dispatch a
    CONTRACT message → the counter's version bump is applied TWICE
    (version-integrity corruption). Plain messages tolerate the race
    (idempotent id-keyed re-place); contracts do NOT.

    ``lock_wait`` (seconds): 0 (default — cron/daemon) → non-blocking, a
    lock-loser returns a ``skipped_locked`` summary + re-ticks later; > 0
    (route_now) → wait for the in-flight sweep to release then route promptly.
    """
    spool_root = Path(config.spool_path)
    spool_root.mkdir(parents=True, exist_ok=True)
    lock_path = spool_root / ".route.lock"
    with open(lock_path, "w") as lock_file:
        if not await _acquire_route_lock(lock_file, lock_wait):
            # Another sweep holds the lock and will route the just-minted
            # file — no double-sweep, no double-apply, no message loss.
            log.info("msgbus.route.skipped_locked", spool_path=str(spool_root))
            return _skipped_locked_result()
        return await _run_route_once_locked(config, raw)


def route_now(
    config: MessageBusConfig,
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Route-on-send entry (Path B) — a thin sync wrapper around the
    self-locking :func:`run_route_once`, on the INTERACTIVE lock-wait path so
    a ``--route`` message racing an in-flight sweep is delivered PROMPTLY
    (waits ~seconds) instead of stranded to the next cron tick. Returns the
    sweep summary (or ``skipped_locked`` only if the wait times out)."""
    return asyncio.run(
        run_route_once(config, raw, lock_wait=ROUTE_NOW_LOCK_WAIT_SECONDS),
    )


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
    "route_now",
    "run_daemon",
    "run_route_once",
    "scan_spool",
]
