"""#20 P5 B1 — ad-hoc-T3 recurrence detection + promotion-proposal store (PROPOSE-ONLY).

The self-correcting "recurrence→promote" loop (``feedback_self_correcting_design_standard``): an
ad-hoc T3 chore the operator keeps checking off (``T3Entry.done_at`` stamped across days by
``tier.daily_curation.mark_t3_done`` — the LIVE producer, #20 P1-P4) is DETECTED as a recurring
pattern and PROPOSED for promotion to a ``routine/`` record. The proposal is surfaced at the Daily
Sync morning review; the operator approves/rejects.

DETECTION (B1) is PROPOSE-ONLY: it writes ONLY the pending proposal queue — it NEVER creates or edits
a ``routine/`` record. The APPROVAL half (B2 — :func:`approve_proposal` / :func:`reject_proposal`) adds
EXACTLY ONE gated write path: on an operator approve (the CLI ``alfred tier-recurrence approve`` or the
Daily Sync reply), the recurring chore is promoted to a soft-cadence ``routine/`` item and the decision
is recorded; reject records the decision only. learn (done_at history) → propose (B1) → operator-approves
(B2). Safe by: a THRESHOLD, PROPOSE-ONLY detection, exclusions (already-in-a-routine / pending / decided),
and — the load-bearing invariant — the routine record is written ONLY inside :func:`approve_proposal`,
NEVER by detection/render.

Mirrors the ``routine.match_calibration`` pending/glossary pattern (schema-tolerant JSONL twins,
degrade-not-crash) and the ``scribe negation-glossary`` approve/reject order (routine-write THEN
decided-row → a crash between self-heals: the append is idempotent-by-text and the id is not-yet-decided,
so a re-run completes). The routine-record write uses the generic ``daily_file_lock`` (an
``fcntl``-flock sidecar on any path) for a ``mark_t3_done``-parity atomic locked-RMW.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import frontmatter
import structlog
import yaml

from alfred.routine.match_calibration import query_key
from alfred.tier.daily_curation import daily_file_lock, load_daily_curation

log = structlog.get_logger(__name__)

# Ad-hoc T3 sources eligible for recurrence promotion — a free-text chore the operator typed or aspired
# to, NOT a routine-origin surfaced item (which already lives in a routine record). See daily_curation
# SOURCES. ``aspirational`` is intentionally eligible: "you aspired to it + keep doing it → promote to a
# committed cadence" is the prime use-case (#20 P5 B2 NOTE-2 — the approve only ADDS the committed item;
# it never mutates the source aspirational entry). ``rollover`` is EXCLUDED: it is a T1/T2-only source
# (daily_curation), never a valid T3 source, so it would be a dead set member (B2 NOTE-1).
_ADHOC_T3_SOURCES: frozenset[str] = frozenset({"operator-adhoc", "aspirational", "operator"})

# Terminal disposition marker written into a pending row.
DISPOSITION_PENDING = "pending"

# Decision kinds (B1 defines + reads for exclusion; B2 writes).
DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"


def proposal_id(key: str) -> str:
    """Deterministic id for a recurrence cluster, keyed on the normalised identity ``key``
    (:func:`match_calibration.query_key`). Stable across the operator's phrasings of the same chore
    and across re-scans → idempotent regeneration + the pending/decided exclusion. Mirrors negation's
    ``npc-…`` derivation."""
    return "trp-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


# ── The pending-proposal + decided stores (schema-tolerant JSONL, degrade-not-crash) ─────────────

@dataclass
class RecurrenceProposal:
    """One PENDING promotion proposal — an ad-hoc chore detected as recurring, awaiting operator
    review. Written ONLY by detection (:func:`materialize_proposals`); disposition is resolved by B2."""

    proposal_id: str
    query_key: str            # the normalised identity key the cluster is grouped by
    sample_text: str          # a representative phrasing (most-recent) — DISPLAY only; identity is the key
    done_days: int            # distinct done-DAYS in the window at detection time
    window_days: int
    first_seen: str = ""      # earliest done_at in the cluster (ISO date)
    last_seen: str = ""       # latest done_at (ISO date)
    detected_at: str = ""     # ISO date the proposal was first materialised
    disposition: str = DISPOSITION_PENDING

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecurrenceProposal":
        """Schema-tolerant construct — filter to known fields (load contract). A row from a different
        tool version with extra/missing fields loads without crashing (extras dropped, absents default);
        ``proposal_id``/``query_key``/``sample_text``/``done_days``/``window_days`` are required."""
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class RecurrenceDecision:
    """One operator decision (approve/reject) on a proposal — B1 DEFINES + READS this (the re-proposal
    exclusion); B2 WRITES it (approve → routine-record promotion + this row; reject → this row only)."""

    proposal_id: str
    decision: str             # DECISION_APPROVE | DECISION_REJECT
    operator: str = ""
    decided_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecurrenceDecision":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _append_jsonl(path: str | Path, row: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _load_jsonl(path: str | Path, build, *, kind: str) -> list:
    """Load rows via ``build(dict)``; a bad-JSON / non-object / malformed row is SKIPPED with a warning
    (the morning surface must degrade on a partially-corrupt store, never crash)."""
    p = Path(path)
    if not p.exists():
        return []
    out: list = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError("row is not a JSON object")
            out.append(build(data))
        except (ValueError, TypeError) as exc:
            log.warning("tier.recurrence.skip_bad_row", path=str(p), kind=kind, error=str(exc))
    return out


def append_pending(path: str | Path, proposal: RecurrenceProposal) -> None:
    _append_jsonl(path, asdict(proposal))


def load_pending(path: str | Path) -> list[RecurrenceProposal]:
    return _load_jsonl(path, RecurrenceProposal.from_dict, kind="pending")


def append_decision(path: str | Path, decision: RecurrenceDecision) -> None:
    """Append one operator decision. Called ONLY by B2 (approval CLI / reply) — NEVER by detection."""
    _append_jsonl(path, asdict(decision))


def load_decisions(path: str | Path) -> list[RecurrenceDecision]:
    return _load_jsonl(path, RecurrenceDecision.from_dict, kind="decided")


def decided_ids(path: str | Path) -> set[str]:
    """The set of proposal_ids with a durable approve/reject — the re-proposal exclusion set."""
    return {d.proposal_id for d in load_decisions(path) if d.proposal_id}


# ── Detection — a PURE projection over the done_at history (writes nothing) ───────────────────────

def _daily_dates_in_window(vault_path: Path, today: _date, window_days: int) -> list[_date]:
    """The ISO-named ``daily/*.md`` files within ``[today - window_days, today]``, as dates. Defensive:
    a non-date stem is skipped; a missing daily dir yields ``[]`` (the scan is a no-op, not a crash)."""
    start = today - timedelta(days=max(window_days, 0))
    daily_dir = vault_path / "daily"
    out: list[_date] = []
    try:
        entries = list(daily_dir.glob("*.md"))
    except OSError:
        return []
    for f in entries:
        try:
            d = _date.fromisoformat(f.stem)
        except ValueError:
            continue
        if start <= d <= today:
            out.append(d)
    return sorted(out)


def compute_recurrence_proposals(
    vault_path: Path,
    *,
    today: _date,
    threshold_done_days: int,
    window_days: int,
    existing_ids: Iterable[str] = (),
    routine_item_texts: list[str] | None = None,
) -> list[RecurrenceProposal]:
    """Scan the ``daily/*.md`` done_at history in the window and PROPOSE promotion for each ad-hoc T3
    chore checked off on ≥ ``threshold_done_days`` DISTINCT days. PURE — writes nothing.

    Clusters done ad-hoc items (``done_at`` set, ``source`` ∈ :data:`_ADHOC_T3_SOURCES`) by
    :func:`match_calibration.query_key` (the same normalisation the routine matcher uses). Counts
    DISTINCT done-days (a chore done twice on one day counts once). Excludes a cluster whose
    ``proposal_id`` is already in ``existing_ids`` (pending ∪ decided → idempotent, no re-propose) and
    — when ``routine_item_texts`` is supplied (the belt) — one whose representative text already matches
    an active routine item (never propose what a routine already covers). Emits a ``tier.recurrence.scan``
    ILB summary so an empty scan is distinguishable from a broken one."""
    existing = set(existing_ids)
    dates = _daily_dates_in_window(vault_path, today, window_days)

    # cluster key → {done_dates: set[str], samples: list[(date_iso, text)]}
    clusters: dict[str, dict[str, Any]] = {}
    days_parsed = 0
    for d in dates:
        curation = load_daily_curation(vault_path, d)   # defensive: None on missing/malformed
        if curation is None:
            continue
        days_parsed += 1
        for e in getattr(curation, "t3", []) or []:
            done_at = getattr(e, "done_at", None)
            item = getattr(e, "item", "") or ""
            source = getattr(e, "source", "") or ""
            if not done_at or not item or source not in _ADHOC_T3_SOURCES:
                continue
            key = query_key(item)
            if not key:
                continue
            c = clusters.setdefault(key, {"done_dates": set(), "samples": []})
            c["done_dates"].add(str(done_at))
            c["samples"].append((str(done_at), item))

    proposals: list[RecurrenceProposal] = []
    below_threshold = excluded_existing = excluded_routine = 0
    for key, c in clusters.items():
        done_days = len(c["done_dates"])
        if done_days < threshold_done_days:
            below_threshold += 1
            continue
        pid = proposal_id(key)
        if pid in existing:
            excluded_existing += 1
            continue
        samples = sorted(c["samples"])            # by (done_at, text)
        sample_text = samples[-1][1]              # most-recent phrasing (display-only)
        if routine_item_texts and _matches_any_routine(sample_text, routine_item_texts):
            excluded_routine += 1
            continue
        done_dates_sorted = sorted(c["done_dates"])
        proposals.append(RecurrenceProposal(
            proposal_id=pid, query_key=key, sample_text=sample_text,
            done_days=done_days, window_days=window_days,
            first_seen=done_dates_sorted[0], last_seen=done_dates_sorted[-1],
            detected_at=today.isoformat()))

    log.info(
        "tier.recurrence.scan",
        days_parsed=days_parsed, clusters=len(clusters), proposed=len(proposals),
        below_threshold=below_threshold, excluded_existing=excluded_existing,
        excluded_routine=excluded_routine, threshold_done_days=threshold_done_days,
        window_days=window_days,
        detail=("no recurring ad-hoc T3 chores over threshold this scan" if not proposals
                else "recurrence proposals detected"),
    )
    return proposals


def _matches_any_routine(sample_text: str, routine_item_texts: list[str]) -> bool:
    """True iff ``sample_text`` already matches an active routine item (the belt exclusion). Uses the
    routine fuzzy matcher with NO glossary (per the ruled B1 scope — the learned glossary is boarded)."""
    from alfred.routine.cli import _matches_item
    for text in routine_item_texts:
        try:
            if _matches_item(sample_text, text):
                return True
        except Exception:  # noqa: BLE001 — the belt is best-effort; a matcher error never blocks a proposal
            continue
    return False


def _active_routine_item_texts(vault_path: Path) -> list[str]:
    """Active routine item texts for the belt. Defensive: any read error → ``[]`` (belt off, detection
    still runs — the operator review is the backstop)."""
    try:
        from alfred.routine.cli import _iter_active_routine_items
        return [getattr(c, "item_text", "") for c in _iter_active_routine_items(vault_path)
                if getattr(c, "item_text", "")]
    except Exception:  # noqa: BLE001
        log.warning("tier.recurrence.routine_scan_failed",
                    detail="could not read routine items for the belt exclusion — belt skipped this scan")
        return []


def materialize_proposals(vault_path: Path, config: Any, today: _date) -> list[RecurrenceProposal]:
    """Detect + idempotently APPEND any NEW proposals to the pending queue, then return the current
    pending set (minus decided) for the surface. The ONLY write here is the pending-queue append — no
    ``routine/`` record is ever touched (that is B2). Re-running is idempotent: a cluster already in
    pending or decided is excluded by :func:`compute_recurrence_proposals`, so no duplicate row is
    appended."""
    decided = decided_ids(config.decided_path)
    pending = load_pending(config.pending_path)
    existing = decided | {p.proposal_id for p in pending}
    routine_texts = _active_routine_item_texts(vault_path)
    new_proposals = compute_recurrence_proposals(
        vault_path, today=today, threshold_done_days=config.threshold_done_days,
        window_days=config.window_days, existing_ids=existing, routine_item_texts=routine_texts)
    for p in new_proposals:
        append_pending(config.pending_path, p)
    # Surface = all pending (prior + newly-appended) minus any decided (append-only + decided-filter).
    return [p for p in (pending + new_proposals) if p.proposal_id not in decided]


# ── B2 — the APPROVAL-gated promotion write (the ONLY routine-record writer) ─────────────────────

# Common cadences a soft-recurring chore snaps to — the operator confirms/overrides via --cadence, so
# exactness doesn't matter; this is a clean confirmable suggestion (daily / 2-3d / weekly / biweekly / monthly).
_COMMON_CADENCES: tuple[int, ...] = (1, 2, 3, 7, 14, 30)


def infer_cadence_days(proposal: RecurrenceProposal) -> int:
    """SUGGEST a soft cadence from the observed done-day spread — the AVERAGE gap between done-days
    (``(last_seen - first_seen) / (done_days - 1)``), snapped to the nearest :data:`_COMMON_CADENCES`.
    B1 stores only first/last/count (not every date), so this is the average, not the median — adequate
    for a soft cadence the operator confirms. Falls back to weekly (7) on an unparseable span."""
    try:
        first = _date.fromisoformat(proposal.first_seen)
        last = _date.fromisoformat(proposal.last_seen)
    except (ValueError, TypeError):
        return 7
    span = (last - first).days
    n = max(proposal.done_days - 1, 1)
    avg = max(round(span / n), 1)
    return min(_COMMON_CADENCES, key=lambda c: abs(c - avg))


def _atomic_write_routine(path: Path, fm: dict[str, Any], body: str) -> None:
    """Atomic (tmp → ``os.replace``) frontmatter round-trip preserving key-order (``sort_keys=False``,
    matching ``routine.cli.cmd_done``'s round-trip) + the record body. Caller holds the file lock."""
    fm_yaml = yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False)
    out = f"---\n{fm_yaml}---\n\n{body.strip()}\n" if body.strip() else f"---\n{fm_yaml}---\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".promote.tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, path)


def append_promoted_item(
    vault_path: Path, routine_record: str, *, text: str, cadence_days: int,
    priority: str = "aspirational",
) -> str:
    """Append a soft-cadence ``Item`` (``text`` + ``priority`` + ``target_cadence_days`` — NO deadline,
    never escalates) to ``routine/<routine_record>.md``'s ``items:`` list. Creates the record (``type:
    routine`` + ``name``) if absent. ATOMIC locked-RMW under :func:`daily_file_lock` (``mark_t3_done``
    parity). IDEMPOTENT: returns ``"duplicate"`` (no write) when an item whose ``query_key(text)``
    already matches is present — a re-approve never adds a second copy. Returns ``"appended"`` on write."""
    path = Path(vault_path) / "routine" / f"{routine_record}.md"
    target_key = query_key(text)
    with daily_file_lock(path):
        if path.exists():
            post = frontmatter.load(str(path))
            fm = dict(post.metadata or {})
            body = post.content or ""
        else:
            fm = {"type": "routine", "name": routine_record}
            body = ""
        items = fm.get("items")
        if not isinstance(items, list):
            items = []
        for it in items:
            if isinstance(it, dict) and query_key(str(it.get("text") or "")) == target_key:
                return "duplicate"
        items.append({"text": text, "priority": priority, "target_cadence_days": int(cadence_days)})
        fm["items"] = items
        _atomic_write_routine(path, fm, body)
    return "appended"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def approve_proposal(
    vault_path: Path, config: Any, proposal_id_: str, *, routine_record: str, operator: str,
    cadence_days: int | None = None,
) -> dict[str, Any]:
    """Operator APPROVE — the ONLY routine-record write path. Promotes the proposal's chore to a
    soft-cadence ``routine_record`` item + records the decision. Refuses an already-decided or
    non-pending id. Cadence defaults to :func:`infer_cadence_days`. ORDER = routine-write THEN
    decided-row (crash-between self-heals: append is idempotent-by-text, id not-yet-decided → re-run
    completes). A ``"duplicate"`` write still records the decision (the item is already there → the
    proposal is resolved). Returns a machine-readable result dict.

    FAIL-CLOSED no-junk-default guard AT THE CORE (not just the CLI boundary): a blank/empty
    ``routine_record`` refuses BEFORE any write, so EVERY caller — the CLI today, B2b's reply path
    tomorrow — inherits the no-junk invariant for free (never a blind write into an auto-created
    ``routine/.md``). Placement is always operator-chosen; there is no default record. B2b's reply
    approve MUST rely on THIS refusal, not re-implement it."""
    routine = (routine_record or "").strip()
    if not routine:
        return {"error": "no routine target — approve requires an explicit routine_record (placement is "
                         "always operator-chosen; there is no default record)", "proposal_id": proposal_id_}
    decided = decided_ids(config.decided_path)
    if proposal_id_ in decided:
        return {"error": f"proposal {proposal_id_} already decided", "proposal_id": proposal_id_}
    pending = {p.proposal_id: p for p in load_pending(config.pending_path)}
    p = pending.get(proposal_id_)
    if p is None:
        return {"error": f"proposal {proposal_id_} is not pending (unknown / already resolved)",
                "proposal_id": proposal_id_}
    cadence = int(cadence_days) if cadence_days is not None else infer_cadence_days(p)
    write = append_promoted_item(vault_path, routine, text=p.sample_text, cadence_days=cadence)
    append_decision(config.decided_path, RecurrenceDecision(
        proposal_id=proposal_id_, decision=DECISION_APPROVE, operator=operator, decided_at=_now_iso()))
    log.info("tier.recurrence.approved", proposal_id=proposal_id_, routine=routine,
             cadence_days=cadence, write=write, operator=operator)
    return {"approved": proposal_id_, "routine": routine, "text": p.sample_text,
            "cadence_days": cadence, "write": write, "operator": operator}


def reject_proposal(config: Any, proposal_id_: str, *, operator: str) -> dict[str, Any]:
    """Operator REJECT — records the decision ONLY (no routine touch). A rejected proposal never
    re-surfaces (the decided-set excludes it from detection). Refuses an already-decided or non-pending id."""
    decided = decided_ids(config.decided_path)
    if proposal_id_ in decided:
        return {"error": f"proposal {proposal_id_} already decided", "proposal_id": proposal_id_}
    pending = {p.proposal_id for p in load_pending(config.pending_path)}
    if proposal_id_ not in pending:
        return {"error": f"proposal {proposal_id_} is not pending (unknown / already resolved)",
                "proposal_id": proposal_id_}
    append_decision(config.decided_path, RecurrenceDecision(
        proposal_id=proposal_id_, decision=DECISION_REJECT, operator=operator, decided_at=_now_iso()))
    log.info("tier.recurrence.rejected", proposal_id=proposal_id_, operator=operator)
    return {"rejected": proposal_id_, "operator": operator}


__all__ = [
    "RecurrenceProposal",
    "RecurrenceDecision",
    "proposal_id",
    "append_pending",
    "load_pending",
    "append_decision",
    "load_decisions",
    "decided_ids",
    "compute_recurrence_proposals",
    "materialize_proposals",
    "infer_cadence_days",
    "append_promoted_item",
    "approve_proposal",
    "reject_proposal",
    "DECISION_APPROVE",
    "DECISION_REJECT",
]
