"""KAL-LE ticket-intake config + state (pipeline c3).

The receiving half of the ratified VERA→KAL-LE→GitHub ticket pipeline
(2026-06-11, R1/R3): VERA pushes ``kind=ticket`` over the peer
protocol → KAL-LE records the ticket in its vault (backlog keeper)
and posts the GitHub issue with the auto-fix label — ALL deterministic
(no LLM anywhere in this path). The sync ack carries the outcome;
GitHub-down means record-then-pending-ack, and VERA's re-push is the
single retry mechanism (no separate retry daemon, no back-channel).

This module holds the intake's CONFIG + STATE only. The HTTP handler
lives in :mod:`alfred.transport.peer_handlers` (the ``kind=ticket``
in-transport branch), wired at daemon startup via
``register_ticket_intake``.

Config home decision (c3): ``labels`` + ``label_map`` live on the
``github:`` section (where c1 put them, on
:class:`alfred.integrations.github_ops.GitHubOpsConfig`) — ONE home,
no duplication. The ``ticket_intake:`` section carries only
``enabled`` + ``state.path``.

State semantics: one entry per ``ticket_uid``. The state file is
deletable bookkeeping (per CLAUDE.md) — the intake handler's
marker-search guard (``issue_search_marker``) recovers issue linkage
after a state deletion so duplicates are never minted. The
effectiveness-loop fields (``pr_number`` / ``pr_state`` /
``disposition`` / ``ticket_to_pr_latency_days`` /
``outcome_checked_at``) are FILLED by the c5 digest loop
(``kalle_digest.assemble_ticket_pipeline_section``) — c3 writes them
as defaults and never touches them.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# Terminal ticket statuses for which a still-pending intake entry
# (no GitHub issue created yet) should be reconciled to "abandoned" —
# the ticket reached an end state without ever needing an issue, so its
# stale retry counter must stop counting as "pending retry" in the
# digest (KAL-LE flag FIX 2). Mirrors the ticket lifecycle's terminal
# states (vera SKILL: open → in_progress → resolved|closed|wont_fix).
# ``wont_fix`` is the case-of-record; ``resolved`` / ``closed`` are
# included because a ticket that reached either WITHOUT a GitHub issue
# (issue_number is None) is equally "no issue needed, stop retrying."
TICKET_TERMINAL_NO_ISSUE_STATUSES: frozenset[str] = frozenset({
    "wont_fix",
    "closed",
    "resolved",
})


# Tool-scoped default per the CLAUDE.md state-path rule — sharing a
# generic ``state.json`` default across tools lets one tool silently
# load another's state file.
DEFAULT_TICKET_INTAKE_STATE_PATH = "./data/ticket_intake_state.json"

# Wire-format pin for ``payload.ticket_uid``. The uid flows verbatim
# into three injection-sensitive sinks: the GitHub search phrase
# (``issue_search_marker`` quotes it into ``q=``), the HTML dedupe
# marker comment (``issue_marker``), and the uid-suffixed vault record
# filename (the title-collision path in ``_record_kalle_ticket``).
# Constraining to a filename- and search-safe charset at the schema
# gate keeps all three sinks clean. The forwarder's minted shape
# (``vera-YYYYMMDD-<8 hex>``) conforms — pinned in
# ``tests/test_ticket_forward.py``'s mint-stability test.
TICKET_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class TicketIntakeConfig:
    """Typed view of the ``ticket_intake:`` config section (KAL-LE-only).

    Deliberately tiny — the GitHub side (repo / pat / labels /
    label_map / audit log) all lives on the ``github:`` section per the
    one-home decision in the module docstring.
    """

    enabled: bool = False
    state_path: str = DEFAULT_TICKET_INTAKE_STATE_PATH
    # Option B: authenticated relay PEER (transport auth.tokens key /
    # X-Alfred-Client) → project slug. DETERMINISTIC INFRA PROVENANCE (the
    # peer is authenticated, so the slug is vouched, not asserted) — NOT
    # LLM-classified, NOT an ``if peer == X`` literal. The slug is stamped
    # into the tracker issue body via the algernon-project marker; the
    # on-box drafter routes the fix PR to the mapped app repo. Empty map →
    # no marker → single-repo drafter behavior (Phase 0).
    project_by_client: dict = field(default_factory=dict)


def load_ticket_intake_config(raw: dict[str, Any]) -> TicketIntakeConfig:
    """Build :class:`TicketIntakeConfig` from the unified config dict.

    Tolerant defaults: an absent / malformed section returns the
    disabled default config (the daemon then skips registration and
    ``kind=ticket`` answers 501).
    """
    section = raw.get("ticket_intake") or {}
    if not isinstance(section, dict):
        return TicketIntakeConfig()

    state_raw = section.get("state") or {}
    state_path = ""
    if isinstance(state_raw, dict):
        state_path = str(state_raw.get("path", "") or "")

    pbc_raw = section.get("project_by_client") or {}
    project_by_client: dict[str, str] = {}
    if isinstance(pbc_raw, dict):
        for peer, slug in pbc_raw.items():
            peer_s, slug_s = str(peer or ""), str(slug or "")
            if peer_s and slug_s:
                project_by_client[peer_s] = slug_s

    return TicketIntakeConfig(
        enabled=bool(section.get("enabled", False)),
        state_path=state_path or DEFAULT_TICKET_INTAKE_STATE_PATH,
        project_by_client=project_by_client,
    )


# ---------------------------------------------------------------------------
# Outcome write-back config (c7) — KAL-LE→VERA push settings
# ---------------------------------------------------------------------------


@dataclass
class TicketOutcomeConfig:
    """Typed view of the ``ticket_outcome:`` config section (pipeline c7).

    The KAL-LE→VERA outcome write-back. ONE section, TWO roles — each
    instance sets only the flag for its side:

      * KAL-LE (the PUSHER): ``enabled: true`` + ``self_name`` +
        ``target_peer`` → KAL-LE's nightly effectiveness loop pushes a
        terminal-disposition outcome to ``target_peer`` over the peer
        protocol.
      * VERA (the RECEIVER): ``receiver_enabled: true`` → the talker
        daemon wires the ``ticket_outcome`` resolver so
        ``POST /peer/ticket_outcome`` applies the write-back (and the
        handshake advertises the ``ticket_outcome`` capability).

    The two flags are independent: an instance that only pushes leaves
    ``receiver_enabled`` False; an instance that only receives leaves
    ``enabled`` False. Both default False — an instance with no
    ``ticket_outcome:`` block neither pushes nor receives (the
    effectiveness loop runs exactly as before; the resolver 501s).

    Deliberately tiny — the GitHub side (audit log) lives on ``github:``
    and the issue state lives on the shared ``ticket_intake`` state
    file. ``target_peer`` looks up base_url + token in THIS instance's
    ``transport.peers[<target_peer>]``; ``self_name`` is the identity
    KAL-LE presents as ``body.from`` + ``X-Alfred-Client`` (VERA's
    ``auth.tokens`` entry must list it in ``allowed_clients``). No
    hardcoded-default antipattern: ``self_name`` empty by default,
    fail-loud at the push call site if a push is attempted without one
    (per feedback_hardcoding_and_alfred_naming.md).
    """

    enabled: bool = False
    receiver_enabled: bool = False
    self_name: str = ""
    target_peer: str = "vera"


def load_ticket_outcome_config(raw: dict[str, Any]) -> TicketOutcomeConfig:
    """Build :class:`TicketOutcomeConfig` from the unified config dict.

    Tolerant defaults: an absent / malformed section returns the
    all-disabled default config (the pusher's effectiveness loop then
    skips the push and logs ``kalle.digest.ticket_outcome_push_disabled``;
    the receiver leaves the resolver unwired so the route 501s).
    """
    section = raw.get("ticket_outcome") or {}
    if not isinstance(section, dict):
        return TicketOutcomeConfig()

    return TicketOutcomeConfig(
        enabled=bool(section.get("enabled", False)),
        receiver_enabled=bool(section.get("receiver_enabled", False)),
        self_name=str(section.get("self_name", "") or ""),
        target_peer=str(section.get("target_peer", "vera") or "vera"),
    )


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class TicketIntakeEntry:
    """Per-ticket intake bookkeeping, keyed by ``ticket_uid``.

    Lifecycle written by c3:
      * ``recorded_at`` / ``kalle_relpath`` — set the moment the vault
        record lands (BEFORE the GitHub post, so a crash between
        record and post resumes in the pending-retry path rather than
        duplicate-recording).
      * ``issue_number`` / ``issue_url`` / ``issue_created_at`` — set
        when the GitHub issue is created (or adopted via the marker
        search). ``issue_number is None`` == issue still pending.
      * ``retry_count`` — incremented on every GitHub failure that
        acked ``recorded_issue_pending``.
      * ``ticket_type`` — the wire frontmatter's ``ticket_type``
        (schema-gated non-empty by the handler), captured at record
        time as the c5 scoreboard's split key. Additive +
        backward-safe via the schema-tolerance loader: entries
        recorded before this field existed carry ``""`` and bucket
        under "unspecified" in the digest scoreboard.

    Filled by c5 (the digest effectiveness loop,
    ``kalle_digest.assemble_ticket_pipeline_section``):
      * ``pr_number`` / ``pr_state`` — the linked PR, once one exists.
      * ``disposition`` — ``""`` until set; vocabulary
        ``merged_clean | merged_after_rework | closed_unmerged |
        stalled`` (terminal = the merged_*/closed_unmerged three;
        ``stalled`` keeps re-checking).
      * ``ticket_to_pr_latency_days`` — float days from issue creation
        to PR merge (merged dispositions only).
      * ``outcome_checked_at`` — last time c5 evaluated this entry.

    Filled by c7 (the KAL-LE→VERA outcome write-back, fired from the
    same c5 effectiveness loop):
      * ``outcome_pushed_at`` — ISO timestamp of the first successful
        write-back to VERA's ticket copy; ``""`` until then. The
        idempotency guard (write once on the open→terminal transition).
    """

    recorded_at: str = ""
    kalle_relpath: str = ""
    issue_number: int | None = None
    issue_url: str = ""
    issue_created_at: str = ""
    retry_count: int = 0
    ticket_type: str = ""
    # --- c5 effectiveness-loop capture fields (reserved; c3 never writes) ---
    pr_number: int | None = None
    pr_state: str = ""
    disposition: str = ""
    ticket_to_pr_latency_days: float | None = None
    outcome_checked_at: str = ""
    # --- self-describing cross-repo PR origin (Option B, C4b) ---
    # When the fix PR lives on a DIFFERENT app repo than the central
    # tracker, ``pr_number`` alone is ambiguous — it's the APP repo's PR
    # number, meaningless against the central client. These record WHICH
    # repo/forge that number belongs to, stamped by
    # ``kalle_digest._check_one_ticket_outcome`` whenever a cross-repo PR is
    # resolved. They make the intake entry self-sufficient: later passes
    # route the poll to the app repo from the ENTRY itself, INDEPENDENT of
    # the drafter's (separately wipeable) ``load_pr_links`` state file — so
    # a wiped drafter state can never leave ``pr_number`` (an app number)
    # to be wrongly polled against the central tracker. Empty == same-repo
    # (or legacy) → the timeline path. Additive + backward-safe via the
    # schema-tolerance loader (existing entries default "").
    pr_app_repo: str = ""
    pr_app_forge_type: str = ""
    # --- c7 outcome write-back idempotency flag ---
    # Set to the ISO timestamp of the FIRST successful KAL-LE→VERA
    # outcome write-back (``brief.kalle_digest.check_ticket_outcomes``).
    # Empty == not yet propagated. The write-back fires once on the
    # open→terminal transition; the terminal-latch's skip is gated on
    # this being non-empty (a terminal entry whose push FAILED stays
    # re-checkable so the next nightly pass retries). Additive +
    # backward-safe via the schema-tolerance loader: entries written
    # before this field existed carry "" and propagate on the next pass.
    outcome_pushed_at: str = ""
    # --- intake reconciliation flag (KAL-LE flag FIX 2) ---
    # Set True when a still-pending entry (``issue_number is None``,
    # ``retry_count > 0``) is reconciled against its ticket record and
    # the ticket is in a terminal "no issue needed" status
    # (``wont_fix`` / ``closed`` / ``resolved``). Such an entry should no
    # longer be counted as "pending retry" in the digest — the ticket was
    # abandoned/resolved without ever needing a GitHub issue, so the
    # retry counter is stale, not an active failure. (It's NOT actively
    # retrying GitHub — it's just a counter that never got cleared when
    # the ticket flipped terminal.) See
    # :func:`reconcile_intake_against_tickets`. Additive + backward-safe
    # via the schema-tolerance loader (existing entries default False).
    intake_abandoned: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "TicketIntakeEntry":
        """Load-time schema-tolerance contract (per CLAUDE.md)."""
        known = {
            k: v for k, v in data.items() if k in cls.__dataclass_fields__
        }
        return cls(**known)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TicketIntakeState:
    """Intake state — ``ticket_uid`` → :class:`TicketIntakeEntry`.

    JSON file shape::

        {"entries": {"<ticket_uid>": {...entry fields...}, ...}}

    Atomic save (``.tmp`` → rename). Load is defensive: a missing
    file is an empty state; a corrupt file logs + starts empty (the
    marker-search guard in the handler recovers issue linkage, so a
    lost state file degrades to extra GitHub searches, never to
    duplicate issues).
    """

    path: Path
    entries: dict[str, TicketIntakeEntry] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "TicketIntakeState":
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            log.warning(
                "ticket_intake.state_load_failed",
                path=str(p),
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            return cls(path=p)
        entries_raw = data.get("entries") if isinstance(data, dict) else None
        entries: dict[str, TicketIntakeEntry] = {}
        if isinstance(entries_raw, dict):
            for uid, entry_data in entries_raw.items():
                if isinstance(entry_data, dict):
                    entries[str(uid)] = TicketIntakeEntry.from_dict(entry_data)
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
# Outcome write-back resolver (c7) — VERA-side receiver logic
# ---------------------------------------------------------------------------


def find_ticket_by_uid(vault_path: Path, ticket_uid: str) -> str | None:
    """Locate a ticket record by ``ticket_uid``; return its relpath or None.

    Globs ``<vault>/ticket/*.md`` and returns the first record whose
    ``ticket_uid`` frontmatter matches. Defensive parse (a malformed
    record logs + is skipped, never raises). Deterministic order via
    ``sorted`` so a (pathological) duplicate-uid vault resolves stably.
    The VERA-side resolver uses this to find the originating ticket the
    KAL-LE outcome write-back targets.
    """
    import frontmatter

    ticket_dir = vault_path / "ticket"
    if not ticket_dir.exists():
        return None
    for md_file in sorted(ticket_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(md_file))
        except Exception as exc:  # noqa: BLE001 — one bad record never fails the lookup
            log.warning(
                "ticket_outcome.lookup_parse_failed",
                path=str(md_file),
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            continue
        fm = dict(post.metadata or {})
        if fm.get("type") != "ticket":
            continue
        if str(fm.get("ticket_uid") or "") == ticket_uid:
            return f"ticket/{md_file.name}"
    return None


def _read_ticket_status(vault_path: Path, relpath: str) -> str | None:
    """Read a ticket record's ``status`` from a vault-relative path.

    Returns the lowercased status string, or ``None`` if the record is
    missing / unreadable / not a ticket. Defensive: never raises (a
    reconcile pass over many entries must not fail on one bad record).
    """
    import frontmatter

    if not relpath:
        return None
    full = vault_path / relpath
    if not full.exists():
        return None
    try:
        post = frontmatter.load(str(full))
    except Exception as exc:  # noqa: BLE001 — one bad record never fails the sweep
        log.warning(
            "ticket_intake.reconcile_parse_failed",
            relpath=relpath,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return None
    fm = dict(post.metadata or {})
    if fm.get("type") != "ticket":
        return None
    return str(fm.get("status") or "").strip().lower()


def reconcile_intake_against_tickets(
    state: "TicketIntakeState", vault_path: Path,
) -> int:
    """Sweep still-pending intake entries against their ticket records'
    status, marking abandoned the ones whose ticket reached a terminal
    "no issue needed" state (KAL-LE flag FIX 2). Returns the count newly
    marked.

    The bug it fixes: a ticket flipped to ``wont_fix`` (or otherwise
    resolved) AFTER its intake entry was already in the pending-retry
    shape (``issue_number is None`` + ``retry_count > 0``) — the
    terminal flip never propagated to the intake state, so the entry was
    counted as "pending retry" in the KAL-LE digest forever (a stale
    counter, NOT an active GitHub retry). This reconciles each such
    entry against its ticket record and latches ``intake_abandoned`` so
    the digest stops counting it.

    Scope (only touches the genuinely-stale shape):
      * ``issue_number is not None`` → an issue exists; outcome tracking
        is the c5/c7 loop's job, not this sweep. Skip.
      * ``retry_count == 0`` → never failed a post; not "pending retry."
        Skip (a fresh entry mid-first-attempt isn't stale).
      * already ``intake_abandoned`` → idempotent skip.
      * ticket status NOT terminal-no-issue → leave pending (a genuinely
        open ticket whose issue post keeps failing is a REAL pending
        retry the operator should still see).

    Reads the ticket status from ``entry.kalle_relpath`` first (KAL-LE's
    own recorded copy — the common case), falling back to a uid-glob via
    :func:`find_ticket_by_uid` if the relpath record is gone. Pure local
    vault reads; no GitHub call — so it runs on every digest pass
    regardless of credential state.

    Caller persists (``state.save()``) when the return is > 0.
    """
    marked = 0
    for uid in sorted(state.entries):
        entry = state.entries[uid]
        if entry.issue_number is not None:
            continue
        if entry.retry_count <= 0:
            continue
        if entry.intake_abandoned:
            continue
        status = _read_ticket_status(vault_path, entry.kalle_relpath)
        if status is None:
            # kalle_relpath record gone / unreadable — fall back to a
            # uid lookup before giving up (state-deletion recovery shape).
            fallback = find_ticket_by_uid(vault_path, uid)
            if fallback is not None:
                status = _read_ticket_status(vault_path, fallback)
        if status in TICKET_TERMINAL_NO_ISSUE_STATUSES:
            entry.intake_abandoned = True
            marked += 1
            log.info(
                "ticket_intake.reconciled_abandoned",
                ticket_uid=uid,
                ticket_status=status,
                retry_count=entry.retry_count,
                kalle_relpath=entry.kalle_relpath,
                detail=(
                    "intake entry was pending-retry (no issue, "
                    f"retry_count={entry.retry_count}) but its ticket is "
                    f"terminal ({status}) — marked intake_abandoned so the "
                    "digest stops counting it as pending retry. The "
                    "terminal flip never propagated to intake state; this "
                    "sweep reconciles it. Not an active GitHub retry."
                ),
            )
    if marked == 0:
        # ILB: a swept-nothing pass is distinguishable from a broken one.
        log.info(
            "ticket_intake.reconcile_no_abandoned",
            scanned=len(state.entries),
            detail=(
                "reconcile sweep ran; no pending-retry entry had a "
                "terminal-no-issue ticket status (nothing to mark)."
            ),
        )
    return marked


def resolve_ticket_outcome(
    vault_path: Path,
    *,
    ticket_uid: str,
    status: str,
    disposition: str,
    pr_number: int | None = None,
    resolved_at: str | None = None,
) -> dict[str, Any]:
    """Apply a KAL-LE outcome write-back to the VERA ticket copy.

    The VERA-side resolver core (pipeline c7), called by the talker
    daemon's registered ``ticket_outcome`` resolver closure. Locates the
    ticket by ``ticket_uid`` and flips it out of the open worklist via
    a single ``vera_ticket_outcome``-scoped edit of exactly the four
    allowlisted fields (status / ticket_disposition / resolved_at /
    github_pr).

    Idempotent: re-applying an already-resolved status is a harmless
    re-write (``applied=True``); the scope gate + vault_edit handle the
    no-change case without error.

    Returns the resolver-contract dict consumed by
    ``peer_handlers._handle_peer_ticket_outcome``:
      * ticket not found → ``{"found": False}`` (handler → 404).
      * applied → ``{"found": True, "applied": True, "relpath": ...}``.
      * write denied/failed → raised (handler → 502); the resolver does
        NOT swallow scope/vault errors — a denied write is a real wiring
        bug the operator must see, not a silent no-op.
    """
    from alfred.vault.ops import vault_edit

    relpath = find_ticket_by_uid(vault_path, ticket_uid)
    if relpath is None:
        log.info(
            "ticket_outcome.ticket_not_found",
            ticket_uid=ticket_uid,
            vault_path=str(vault_path),
        )
        return {"found": False}

    set_fields: dict[str, Any] = {
        "status": status,
        "ticket_disposition": disposition,
    }
    if resolved_at:
        set_fields["resolved_at"] = resolved_at
    if pr_number is not None:
        set_fields["github_pr"] = pr_number

    result = vault_edit(
        vault_path,
        relpath,
        set_fields=set_fields,
        scope="vera_ticket_outcome",
    )
    log.info(
        "ticket_outcome.applied",
        ticket_uid=ticket_uid,
        relpath=relpath,
        status=status,
        disposition=disposition,
        fields_changed=result.get("fields_changed", []),
    )
    return {
        "found": True,
        "applied": True,
        "relpath": relpath,
    }


__all__ = [
    "DEFAULT_TICKET_INTAKE_STATE_PATH",
    "TICKET_TERMINAL_NO_ISSUE_STATUSES",
    "TICKET_UID_RE",
    "TicketIntakeConfig",
    "TicketIntakeEntry",
    "TicketIntakeState",
    "TicketOutcomeConfig",
    "find_ticket_by_uid",
    "load_ticket_intake_config",
    "load_ticket_outcome_config",
    "reconcile_intake_against_tickets",
    "resolve_ticket_outcome",
]
