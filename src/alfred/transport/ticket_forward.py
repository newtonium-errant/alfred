"""VERA ticket forwarder daemon (pipeline c4).

The sending half of the ratified VERA→KAL-LE→GitHub ticket pipeline
(2026-06-11, R2): a DETERMINISTIC scanner — no LLM, no operator gate —
that walks VERA's ``ticket/`` directory every ``interval_minutes``
(default 15), pushes every eligible open ticket to KAL-LE as
``kind=ticket`` over the peer protocol, and writes the GitHub issue
link-back onto the originating record when the ack carries one.

Eligibility = ``status == "open"`` AND the ticket isn't already linked
to an issue in the forwarder state. A ticket whose ack came back
``recorded_issue_pending`` (KAL-LE recorded it but GitHub was down)
stays eligible — the re-push on the next tick IS the pipeline's single
retry mechanism (no separate retry daemon, no back-channel).

UID contract: ``mint_ticket_uid`` is pure and pinned stable by test —
``vera-<YYYYMMDD>-<sha256(relpath|created)[:8]>``. A record without a
``ticket_uid`` field gets one minted + written via the narrow
``vera_forwarder`` scope BEFORE the first push (the uid travels in the
record from first contact); a record that already carries a uid reuses
it — never re-minted.

Scope discipline: every vault write in this module goes through scope
``vera_forwarder`` — edit-only, fields restricted to
{ticket_uid, github_issue, github_url, forwarded_at} (c2's
``vera_forwarder_link_back_only`` gate). The forwarder can never
create, move, delete, or touch ticket bodies/status.

Kill-switch: ``ticket_forward.enabled`` (default False). The
orchestrator auto-start gate + the daemon runner's exit-78 guard both
honour it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter

from .client import peer_send
from .exceptions import TransportError, TransportRejected
from .utils import get_logger

log = get_logger(__name__)


# Tool-scoped default per the CLAUDE.md state-path rule.
DEFAULT_TICKET_FORWARD_STATE_PATH = "./data/ticket_forward_state.json"

# Ack statuses that carry an issue linkage (the c3 intake's contract).
_LINKED_ACK_STATUSES = frozenset({"created", "exists", "adopted"})


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class TicketForwardConfig:
    """Typed view of the ``ticket_forward:`` config section (VERA-only).

    Modeled on ``load_brief_digest_push_config`` — tolerant defaults,
    ``vault_path`` falling back to the unified ``vault.path``.
    """

    enabled: bool = False
    # Identity this instance presents as body.from + X-Alfred-Client.
    # The receiver's auth.tokens entry must list it in allowed_clients.
    self_name: str = ""
    # Outbound peer key — looks up base_url + token in
    # transport.peers[<this>] on THIS instance's config.
    target_peer: str = "kalle"
    interval_minutes: int = 15
    # Vault root holding the ``ticket/`` directory. Falls back to the
    # unified ``vault.path`` when omitted from the block.
    vault_path: str = ""
    state_path: str = DEFAULT_TICKET_FORWARD_STATE_PATH


def load_ticket_forward_config(raw: dict[str, Any]) -> TicketForwardConfig:
    """Build :class:`TicketForwardConfig` from the unified config dict."""
    section = raw.get("ticket_forward") or {}
    if not isinstance(section, dict):
        return TicketForwardConfig()

    state_raw = section.get("state") or {}
    state_path = ""
    if isinstance(state_raw, dict):
        state_path = str(state_raw.get("path", "") or "")

    vault_path = str(section.get("vault_path", "") or "")
    if not vault_path:
        vault_path = str((raw.get("vault", {}) or {}).get("path", "") or "")

    try:
        interval_minutes = int(section.get("interval_minutes", 15) or 15)
    except (TypeError, ValueError):
        interval_minutes = 15

    return TicketForwardConfig(
        enabled=bool(section.get("enabled", False)),
        self_name=str(section.get("self_name", "") or ""),
        target_peer=str(section.get("target_peer", "kalle") or "kalle"),
        interval_minutes=interval_minutes,
        vault_path=vault_path,
        state_path=state_path or DEFAULT_TICKET_FORWARD_STATE_PATH,
    )


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class TicketForwardEntry:
    """Per-ticket forwarder bookkeeping, keyed by ``ticket_uid``.

    ``issue_number is None`` == not yet linked (still eligible for
    re-push). ``attempts`` counts every push attempt regardless of
    outcome.
    """

    relpath: str = ""
    first_forwarded_at: str = ""
    last_attempt_at: str = ""
    attempts: int = 0
    issue_number: int | None = None
    issue_url: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "TicketForwardEntry":
        """Load-time schema-tolerance contract (per CLAUDE.md)."""
        known = {
            k: v for k, v in data.items() if k in cls.__dataclass_fields__
        }
        return cls(**known)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TicketForwardState:
    """Forwarder state — ``ticket_uid`` → :class:`TicketForwardEntry`.

    Atomic save (``.tmp`` → rename); defensive load (missing file →
    empty; corrupt file → log + empty — KAL-LE's intake dedupes by uid
    so a lost state file degrades to redundant pushes acked "exists",
    never to duplicate issues).
    """

    path: Path
    entries: dict[str, TicketForwardEntry] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "TicketForwardState":
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            log.warning(
                "ticket_forward.state_load_failed",
                path=str(p),
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            return cls(path=p)
        entries_raw = data.get("entries") if isinstance(data, dict) else None
        entries: dict[str, TicketForwardEntry] = {}
        if isinstance(entries_raw, dict):
            for uid, entry_data in entries_raw.items():
                if isinstance(entry_data, dict):
                    entries[str(uid)] = TicketForwardEntry.from_dict(
                        entry_data,
                    )
        return cls(path=p, entries=entries)

    def save(self) -> None:
        """Atomic write — ``.tmp`` then ``os.replace`` rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": {
                uid: entry.to_dict() for uid, entry in self.entries.items()
            },
        }
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp_path, self.path)


# ---------------------------------------------------------------------------
# UID mint — pure, pinned stable by test
# ---------------------------------------------------------------------------


def mint_ticket_uid(relpath: str, created: str) -> str:
    """Mint the pipeline-stable ticket UID for one record.

    ``vera-<YYYYMMDD>-<sha256(f"{relpath}|{created}").hexdigest()[:8]>``

    The date part comes from the record's ``created`` value (digits of
    its first 10 chars — handles both ``2026-06-11`` strings and the
    str() of a PyYAML-parsed date), falling back to today when the
    value doesn't yield a full YYYYMMDD. The hash input uses the RAW
    relpath + created strings so the same inputs always mint the same
    uid (the dedupe key the whole pipeline joins on).
    """
    created_str = str(created or "")
    digits = "".join(ch for ch in created_str[:10] if ch.isdigit())
    date_part = digits if len(digits) == 8 else date.today().strftime("%Y%m%d")
    digest = hashlib.sha256(
        f"{relpath}|{created_str}".encode("utf-8"),
    ).hexdigest()[:8]
    return f"vera-{date_part}-{digest}"


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rejected_reason_detail(body: str) -> tuple[str, str]:
    """Parse ``(reason, detail)`` off a peer's 4xx response body.

    The receiver's ``_json_error`` emits ``{"reason": ..., "detail":
    ..., ...}`` JSON; ``TransportRejected.body`` carries the raw text
    (first 500 chars). Empty strings when the body isn't that shape
    (old receivers, proxies, truncated/non-JSON bodies) — callers must
    treat the unparsed case conservatively.
    """
    try:
        data = json.loads(body or "")
    except ValueError:
        return "", ""
    if not isinstance(data, dict):
        return "", ""
    return str(data.get("reason") or ""), str(data.get("detail") or "")


def scan_tickets(
    vault_path: Path,
    state: TicketForwardState,
) -> tuple[int, list[dict[str, Any]]]:
    """Walk ``<vault>/ticket/*.md`` and select forward-eligible records.

    Returns ``(scanned, eligible)`` where ``scanned`` counts every
    ticket-typed record seen and ``eligible`` is a list of
    ``{relpath, frontmatter, body, uid}`` dicts (``uid`` empty when the
    record has no ``ticket_uid`` field yet — the caller mints one).

    Eligible = ``status == "open"`` AND (uid not in state OR the state
    entry lacks ``issue_number``). Defensive frontmatter parsing
    (mirrors ``vera_ticket_digest._scan_open_tickets``): a malformed
    file logs + skips, never kills the tick.
    """
    scanned = 0
    eligible: list[dict[str, Any]] = []
    ticket_dir = vault_path / "ticket"
    if not ticket_dir.exists():
        log.info(
            "ticket_forward.no_ticket_dir", vault_path=str(vault_path),
        )
        return 0, []

    for md_file in sorted(ticket_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(md_file))
        except Exception as exc:  # noqa: BLE001 — one bad record never kills the tick
            log.warning(
                "ticket_forward.scan_parse_failed",
                path=str(md_file),
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            continue
        fm = dict(post.metadata or {})
        if fm.get("type") != "ticket":
            continue
        scanned += 1
        if str(fm.get("status") or "") != "open":
            continue
        relpath = f"ticket/{md_file.name}"
        uid_raw = fm.get("ticket_uid")
        uid = uid_raw if isinstance(uid_raw, str) and uid_raw else ""
        if uid:
            entry = state.entries.get(uid)
            if entry is not None and entry.issue_number is not None:
                continue  # already linked — ineligible
        eligible.append({
            "relpath": relpath,
            "frontmatter": fm,
            "body": post.content,
            "uid": uid,
        })
    return scanned, eligible


# ---------------------------------------------------------------------------
# One tick — the testable unit + the CLI probe surface
# ---------------------------------------------------------------------------


async def run_forward_once(
    config: TicketForwardConfig,
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Run one forward tick. Returns the summary dict for CLI/tests.

    Per-ticket outcomes (also in the returned ``results`` list):
      * ``linked`` — ack in {created, exists, adopted} with an issue
        number; link-back written to the record + state.
      * ``issue_pending`` — KAL-LE recorded the ticket but GitHub was
        down; stays eligible, re-pushed next tick (this IS the retry).
      * ``peer_not_upgraded`` — the peer 400'd ``kind=ticket`` with
        ``unknown_kind`` (an upgraded receiver's enum gate) or a bare
        ``schema_error`` without a ``payload.*`` detail (a PRE-upgrade
        receiver's enum gate — it can't emit unknown_kind yet); both
        mean version skew → abort remaining pushes this tick, leave
        ALL tickets queued.
      * ``push_rejected`` — the peer 400'd ``schema_error`` whose
        detail names a ``payload.*`` field: THIS ticket is malformed.
        Isolated per-ticket (the loop continues — aborting here would
        starve every later ticket at the sorted-glob position); stays
        issue-less so the c5 digest's ``forward FAILED ×N`` tail
        surfaces it after the second attempt.
      * ``push_failed`` — transport down / timeout / non-400 error;
        isolated per-ticket, the loop continues.

    ILB: ``ticket_forward.tick`` is logged EVERY tick — zero-work
    ticks included — so idle is distinguishable from broken.
    """
    from .config import load_from_unified as load_transport_config
    from .peer_search import json_sanitize
    from alfred.vault.ops import vault_edit

    transport_config = load_transport_config(raw)
    vault_path = Path(config.vault_path)
    state = TicketForwardState.load(config.state_path)

    scanned, eligible = scan_tickets(vault_path, state)

    forwarded = 0
    failed = 0
    pending = 0
    aborted = False
    results: list[dict[str, Any]] = []

    for item in eligible:
        relpath = item["relpath"]
        fm = item["frontmatter"]
        body = item["body"]
        uid = item["uid"]

        # First contact: mint + write the uid into the record via the
        # narrow scope BEFORE pushing (the uid travels in the record
        # from first contact). Existing uids are reused — never
        # re-minted.
        if not uid:
            uid = mint_ticket_uid(relpath, str(fm.get("created") or ""))
            try:
                vault_edit(
                    vault_path,
                    relpath,
                    set_fields={"ticket_uid": uid},
                    scope="vera_forwarder",
                )
            except Exception as exc:  # noqa: BLE001 — isolate per ticket
                log.warning(
                    "ticket_forward.uid_write_failed",
                    relpath=relpath,
                    error=str(exc),
                    error_type=exc.__class__.__name__,
                )
                failed += 1
                results.append({
                    "uid": uid,
                    "relpath": relpath,
                    "outcome": "uid_write_failed",
                })
                continue
            fm = dict(fm)
            fm["ticket_uid"] = uid

        entry = state.entries.get(uid)
        if entry is None:
            entry = TicketForwardEntry(relpath=relpath)
            state.entries[uid] = entry
        now = _now_iso()
        if not entry.first_forwarded_at:
            entry.first_forwarded_at = now
        entry.last_attempt_at = now
        entry.attempts += 1

        payload = {
            "precedence": "R",
            "ticket_uid": uid,
            "relpath": relpath,
            # Full frontmatter dict (including ticket_uid), sanitized
            # for the JSON wire (PyYAML parses dates into date objects
            # that json.dumps rejects).
            "frontmatter": json_sanitize(fm),
            "body": body,
        }
        try:
            ack = await peer_send(
                config.target_peer,
                "ticket",
                payload,
                config=transport_config,
                self_name=config.self_name,
            )
        except TransportRejected as exc:
            # 4xx — never retried by the client. The 400 family needs
            # CLASSIFICATION (2026-06-12 review WARN-2): the receiver
            # uses 400 both for "kind unknown" (version skew) and for
            # a per-payload schema failure (one malformed ticket).
            # Conflating them let one bad ticket abort every tick at
            # its sorted-glob position and starve all later tickets
            # under a misleading peer_not_upgraded log.
            reason, reject_detail = _rejected_reason_detail(exc.body)
            if (
                exc.status_code == 400
                and reason == "schema_error"
                and "payload." in reject_detail
            ):
                # Per-payload gate on an upgraded receiver → THIS
                # ticket is malformed; skip it, keep pushing the rest.
                # attempts was already bumped above, the entry stays
                # issue-less → the c5 digest's FAILED ×N tail surfaces
                # it from the second attempt onward.
                log.warning(
                    "ticket_forward.push_rejected",
                    uid=uid,
                    relpath=relpath,
                    detail=reject_detail[:200],
                    http_status=exc.status_code,
                )
                failed += 1
                results.append({
                    "uid": uid,
                    "relpath": relpath,
                    "outcome": "push_rejected",
                })
                continue
            if exc.status_code == 400:
                # ``unknown_kind`` is an upgraded receiver's enum gate;
                # a bare ``schema_error`` with no ``payload.*`` detail
                # is a PRE-upgrade receiver's enum gate (it can't emit
                # unknown_kind yet). Anything else unparsed is treated
                # the same, conservatively. All mean: re-pushing the
                # REST of the queue this tick is pointless.
                log.warning(
                    "ticket_forward.peer_not_upgraded",
                    target_peer=config.target_peer,
                    http_status=exc.status_code,
                    reason=reason or "(unparsed)",
                    body=str(exc.body)[:200],
                    detail=(
                        "peer rejected kind=ticket with 400 — likely not "
                        "yet upgraded; leaving the queue intact and "
                        "aborting the remaining pushes this tick"
                    ),
                )
                failed += 1
                aborted = True
                results.append({
                    "uid": uid,
                    "relpath": relpath,
                    "outcome": "peer_not_upgraded",
                })
                break
            log.warning(
                "ticket_forward.push_failed",
                uid=uid,
                relpath=relpath,
                error=str(exc),
                error_type=exc.__class__.__name__,
                http_status=exc.status_code,
            )
            failed += 1
            results.append({
                "uid": uid,
                "relpath": relpath,
                "outcome": "push_failed",
            })
            continue
        except TransportError as exc:
            # Transport down / timeout / 5xx-exhausted — isolated
            # per-ticket so one bad push never starves the rest.
            log.warning(
                "ticket_forward.push_failed",
                uid=uid,
                relpath=relpath,
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            failed += 1
            results.append({
                "uid": uid,
                "relpath": relpath,
                "outcome": "push_failed",
            })
            continue

        status = ack.get("status") if isinstance(ack, dict) else None
        if status in _LINKED_ACK_STATUSES and ack.get("issue_number") is not None:
            issue_number = int(ack["issue_number"])
            issue_url = str(ack.get("issue_url") or "")
            try:
                vault_edit(
                    vault_path,
                    relpath,
                    set_fields={
                        "github_issue": issue_number,
                        "github_url": issue_url,
                        "forwarded_at": now,
                    },
                    scope="vera_forwarder",
                )
            except Exception as exc:  # noqa: BLE001 — state still records the link
                # The record does NOT self-heal: the state entry below
                # gets issue_number, the ticket becomes ineligible, and
                # this path never re-runs. The operator-visible
                # mitigation is the c5 digest's state-fallback tail
                # (``vera_ticket_digest._forward_status_tail`` precedence
                # 2 renders ``→ GH#<n>`` from state when the record
                # lacks ``github_issue``), keyed off this WARNING.
                log.warning(
                    "ticket_forward.link_back_write_failed",
                    uid=uid,
                    relpath=relpath,
                    error=str(exc),
                    error_type=exc.__class__.__name__,
                )
            entry.issue_number = issue_number
            entry.issue_url = issue_url
            forwarded += 1
            log.info(
                "ticket_forward.linked",
                uid=uid,
                issue_number=issue_number,
                ack_status=status,
                relpath=relpath,
            )
            results.append({
                "uid": uid,
                "relpath": relpath,
                "outcome": "linked",
                "issue_number": issue_number,
                "ack_status": status,
            })
        elif status == "recorded_issue_pending":
            # KAL-LE recorded the ticket; GitHub was down. The entry
            # stays issue-less → eligible again next tick. This IS the
            # retry mechanism.
            pending += 1
            log.info(
                "ticket_forward.issue_pending",
                uid=uid,
                kalle_relpath=str(ack.get("kalle_relpath") or ""),
            )
            results.append({
                "uid": uid,
                "relpath": relpath,
                "outcome": "issue_pending",
            })
        else:
            failed += 1
            log.warning(
                "ticket_forward.unexpected_ack",
                uid=uid,
                ack_status=str(status),
                ack_keys=sorted(ack.keys()) if isinstance(ack, dict) else [],
            )
            results.append({
                "uid": uid,
                "relpath": relpath,
                "outcome": "unexpected_ack",
                "ack_status": str(status),
            })

    state.save()

    summary = {
        "scanned": scanned,
        "eligible": len(eligible),
        "forwarded": forwarded,
        "pending": pending,
        "failed": failed,
        "aborted": aborted,
    }
    # ILB: every tick — zero-work ticks included — emits the summary so
    # an idle forwarder is distinguishable from a broken one.
    log.info("ticket_forward.tick", **summary)
    return {**summary, "results": results}


# ---------------------------------------------------------------------------
# Daemon loop
# ---------------------------------------------------------------------------


async def run_daemon(
    config: TicketForwardConfig,
    raw: dict[str, Any],
) -> None:
    """Interval loop: tick, sleep ``interval_minutes``, repeat.

    Not clock-aligned (a plain sleep between ticks is fine — the 15-min
    cadence is a freshness bound, not a schedule). Per-tick exception
    containment: a bad tick logs + continues; the daemon never dies to
    a single failure.
    """
    log.info(
        "ticket_forward.daemon.starting",
        interval_minutes=config.interval_minutes,
        target_peer=config.target_peer,
        self_name=config.self_name,
        vault_path=config.vault_path,
        state_path=config.state_path,
    )
    while True:
        try:
            await run_forward_once(config, raw)
        except Exception:  # noqa: BLE001 — daemon-level safety net
            log.exception("ticket_forward.daemon.tick_error")
        await asyncio.sleep(config.interval_minutes * 60)


__all__ = [
    "DEFAULT_TICKET_FORWARD_STATE_PATH",
    "TicketForwardConfig",
    "TicketForwardEntry",
    "TicketForwardState",
    "load_ticket_forward_config",
    "mint_ticket_uid",
    "run_daemon",
    "run_forward_once",
    "scan_tickets",
]
