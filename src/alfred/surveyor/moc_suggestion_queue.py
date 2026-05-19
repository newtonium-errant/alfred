"""Out-of-vault JSONL queue for cluster→MOC suggestions
(Phase 5 Sub-arc D1, 2026-05-19).

Per ratified Q1: suggestions persist as JSONL outside the vault
(default ``<state.path>.parent / moc_suggestions.jsonl``). Out-of-
vault means surveyor doesn't need new scope rules + Obsidian
doesn't index queue mutations + atomic rewrites are safe.

I/O semantics (ratified Q5):

  * **Proposals are upsert-by-ID.** Same (sorted_members, target)
    hash yields the same id across sweeps; the queue dedupes via
    that id. Re-proposing an id whose status is non-pending is a
    no-op (negative-learning surface). Re-proposing a pending id
    refreshes ``cluster_id_at_proposal`` + ``cluster_tags`` +
    ``reasoning`` (forensic refresh) but keeps ``created`` and
    ``id`` stable.

  * **Status flips are full-rewrites under flock.** Accept,
    reject, applied, last_apply_error all reuse the same
    rewrite-under-lock helper. JSONL-append for new proposals is
    fast-path; status flips re-serialize the whole queue. Queue
    is bounded by ``max_pending_per_target`` × number of MOCs,
    so even a Hypatia-sized vault keeps the queue under a few
    hundred lines; full-rewrite is cheap.

  * **Atomic .tmp → rename on full rewrites.** Standard surveyor
    state-persistence idiom (see ``state.py:save``).

  * **fcntl.flock on RMW.** Concurrent surveyor + bot writes are
    safe. Lock acquired on file-open, released on close.

Schema-tolerance contract (CLAUDE.md "load() schema-tolerance
contract"): the loader filters incoming JSON dicts against
``MocSuggestion.__dataclass_fields__`` so a future field addition
doesn't crash older loaders, and a rollback doesn't crash on
newer-fielded entries.

Failure-isolated: corrupt lines log + skip. Queue I/O failures
return False / empty list rather than raising — the daemon
proceeds with the surveyor sweep regardless of queue health.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import structlog

from .moc_suggester import MocSuggestion

log = structlog.get_logger(__name__)


# Canonical schema versioning lives on the queue file's first line
# as ``{"_schema_version": 1}`` IF Andrew ever needs version-gated
# migrations. We don't write it in D1 — every line is a suggestion;
# the schema-tolerance filter on load handles forward/backward
# compat for the v1→v2 case. Reserved as a future-proofing note.

_KNOWN_FIELDS: frozenset[str] = frozenset(MocSuggestion.__dataclass_fields__)


def derive_default_queue_path(state_path: str | Path) -> Path:
    """Mirror the audit-log path derivation in ``daemon.py``: the
    queue file lives next to the surveyor state file, named
    ``moc_suggestions.jsonl``.

    Caller (config + daemon) uses this when
    ``MocSuggestionConfig.queue_path`` is None.
    """
    parent = Path(state_path).parent
    return parent / "moc_suggestions.jsonl"


def load_queue(queue_path: str | Path) -> list[MocSuggestion]:
    """Load + parse the full JSONL queue. Returns [] if file is
    missing (no proposals yet).

    Schema-tolerance: filters each line against
    ``MocSuggestion.__dataclass_fields__`` before instantiation. A
    future-version queue file with extra fields loads cleanly on
    an older binary; an older file missing newly-added optional
    fields loads cleanly on a newer binary (dataclass defaults
    backfill).

    Per-line failure isolation: a single corrupt line logs +
    skips; the rest of the queue still loads. Same defensive
    shape as :func:`alfred.telegram.inventory_views.collect_records`.
    """
    qp = Path(queue_path)
    if not qp.exists():
        return []
    out: list[MocSuggestion] = []
    try:
        with open(qp, "r", encoding="utf-8") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                for lineno, raw_line in enumerate(f, start=1):
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError as exc:
                        log.warning(
                            "surveyor.moc_suggestion.queue_load_skip_corrupt",
                            queue_path=str(qp),
                            lineno=lineno,
                            error=str(exc)[:200],
                        )
                        continue
                    if not isinstance(data, dict):
                        continue
                    filtered = {k: v for k, v in data.items() if k in _KNOWN_FIELDS}
                    try:
                        out.append(MocSuggestion(**filtered))
                    except TypeError as exc:
                        # Missing required field — likely from a
                        # very old queue file pre-dating an
                        # additive non-default. Skip but log so
                        # operator can decide.
                        log.warning(
                            "surveyor.moc_suggestion.queue_load_skip_missing_fields",
                            queue_path=str(qp),
                            lineno=lineno,
                            error=str(exc)[:200],
                        )
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        log.warning(
            "surveyor.moc_suggestion.queue_load_failed",
            queue_path=str(qp),
            error=str(exc)[:200],
        )
        return []
    return out


def upsert_proposals(
    queue_path: str | Path,
    proposals: list[MocSuggestion],
    *,
    max_pending_per_target: int,
    max_proposals_per_sweep: int,
) -> tuple[int, int]:
    """Add new proposals + refresh existing pending ones.

    Returns ``(n_added, n_refreshed)``. Both caps apply:

      * ``max_proposals_per_sweep`` caps the count of NEW entries
        introduced in this call (refreshes don't count). Excess
        proposals are dropped silently — operator-visible via the
        daemon's ``surveyor.moc_suggestion.sweep_cap_hit`` log.
      * ``max_pending_per_target`` caps the count of pending
        entries against any single target MOC across the union
        of pre-existing-pending + new-this-sweep. Excess proposals
        for a given target are dropped silently — operator-visible
        via ``surveyor.moc_suggestion.target_cap_hit``.

    Idempotency:
      * Existing non-pending entry (rejected / applied / archived)
        with matching id → no-op. Negative-learning preserved.
      * Existing pending entry with matching id → refresh
        ``cluster_id_at_proposal`` + ``cluster_tags`` +
        ``reasoning`` (the forensic fields). ``created`` + ``id``
        + ``cluster_member_paths`` + ``target_moc_rel_path`` stay
        stable.
      * No existing entry → add (subject to caps).

    File-write strategy:
      * If no changes (all proposals were no-op idempotent hits),
        the file is untouched.
      * Otherwise full-rewrite under exclusive flock. Append-only
        was tempting but the refresh case requires in-place
        modification; mixing append + rewrite would race.

    Returns counts so the daemon can emit the right summary log.
    """
    if not proposals:
        return (0, 0)

    qp = Path(queue_path)
    qp.parent.mkdir(parents=True, exist_ok=True)

    # Touch the file so flock has something to attach to even on
    # fresh init. Open in r+ for the RMW; if the file is new, fall
    # back to "w+".
    if not qp.exists():
        qp.touch()

    n_added = 0
    n_refreshed = 0
    with open(qp, "r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            existing = _load_locked(f, str(qp))

            # Index existing by id for O(1) lookup.
            by_id: dict[str, MocSuggestion] = {s.id: s for s in existing}

            # Per-target pending counter for cap enforcement.
            pending_per_target: dict[str | None, int] = {}
            for s in existing:
                if s.status == "pending":
                    pending_per_target[s.target_moc_rel_path] = (
                        pending_per_target.get(s.target_moc_rel_path, 0) + 1
                    )

            for proposal in proposals:
                if n_added >= max_proposals_per_sweep:
                    log.info(
                        "surveyor.moc_suggestion.sweep_cap_hit",
                        cap=max_proposals_per_sweep,
                        suggestion_id=proposal.id,
                    )
                    continue

                existing_entry = by_id.get(proposal.id)
                if existing_entry is None:
                    # NEW proposal — check per-target cap.
                    target = proposal.target_moc_rel_path
                    if pending_per_target.get(target, 0) >= max_pending_per_target:
                        log.info(
                            "surveyor.moc_suggestion.target_cap_hit",
                            target_moc=target,
                            cap=max_pending_per_target,
                            suggestion_id=proposal.id,
                        )
                        continue
                    by_id[proposal.id] = proposal
                    pending_per_target[target] = pending_per_target.get(target, 0) + 1
                    n_added += 1
                elif existing_entry.status != "pending":
                    # Negative-learning preserved: rejected /
                    # applied / archived stay as-is.
                    continue
                else:
                    # REFRESH pending entry's forensic fields.
                    existing_entry.cluster_id_at_proposal = proposal.cluster_id_at_proposal
                    existing_entry.cluster_tags = list(proposal.cluster_tags)
                    existing_entry.reasoning = proposal.reasoning
                    existing_entry.mapping_signal = proposal.mapping_signal
                    existing_entry.mapping_score = proposal.mapping_score
                    # candidates_to_add can shift across sweeps as
                    # members get the MOC added to their mocs:
                    # frontmatter (between sweeps the operator may
                    # have manually accepted some). Refresh it.
                    existing_entry.candidate_members_to_add = list(
                        proposal.candidate_members_to_add,
                    )
                    n_refreshed += 1

            if n_added == 0 and n_refreshed == 0:
                # Nothing changed — don't rewrite the file.
                return (0, 0)

            # Atomic rewrite via .tmp + rename. Hold the lock on
            # the original file; write to .tmp; rename under lock.
            _rewrite_locked(qp, list(by_id.values()))
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    log.info(
        "surveyor.moc_suggestion.upsert_complete",
        queue_path=str(qp),
        added=n_added,
        refreshed=n_refreshed,
    )
    return (n_added, n_refreshed)


def update_status(
    queue_path: str | Path,
    suggestion_id: str,
    new_status: str,
    *,
    last_apply_error: str | None = None,
) -> bool:
    """Flip a single suggestion's status under exclusive lock.

    D2 + bot accept/reject handlers consume this. Status transitions
    permitted:

      pending → accepted
      accepted → applied         (vault_edit succeeded)
      accepted → pending         (apply failed; ``last_apply_error`` set)
      pending → rejected
      applied → archived         (compaction cron; future ship)

    Returns True on success; False if suggestion_id absent or
    transition denied. Full-rewrite under flock.
    """
    qp = Path(queue_path)
    if not qp.exists():
        log.warning(
            "surveyor.moc_suggestion.status_update_queue_missing",
            queue_path=str(qp),
            suggestion_id=suggestion_id,
        )
        return False

    with open(qp, "r+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            existing = _load_locked(f, str(qp))
            for s in existing:
                if s.id != suggestion_id:
                    continue
                # Validate transition. The set of forward
                # transitions is small enough to enumerate.
                allowed_forward = {
                    "pending": {"accepted", "rejected"},
                    "accepted": {"applied", "pending"},  # pending re-flip on apply failure
                    "applied": {"archived"},
                    "rejected": set(),  # terminal
                    "archived": set(),  # terminal
                }
                if new_status not in allowed_forward.get(s.status, set()):
                    log.warning(
                        "surveyor.moc_suggestion.status_transition_denied",
                        suggestion_id=suggestion_id,
                        from_status=s.status,
                        to_status=new_status,
                    )
                    return False
                s.status = new_status
                now_iso = datetime.now(timezone.utc).isoformat()
                if new_status in ("accepted", "rejected"):
                    s.decided_at = now_iso
                if new_status == "applied":
                    s.applied_at = now_iso
                    s.last_apply_error = None
                if new_status == "pending" and last_apply_error is not None:
                    s.last_apply_error = last_apply_error
                _rewrite_locked(qp, existing)
                log.info(
                    "surveyor.moc_suggestion.status_updated",
                    suggestion_id=suggestion_id,
                    new_status=new_status,
                )
                return True
            log.warning(
                "surveyor.moc_suggestion.status_update_id_missing",
                queue_path=str(qp),
                suggestion_id=suggestion_id,
            )
            return False
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Internal helpers — assume lock already held.
# ---------------------------------------------------------------------------


def _load_locked(f, queue_path: str) -> list[MocSuggestion]:
    """Read + parse the queue file under an already-held flock.

    Caller MUST hold a shared or exclusive flock on ``f``. Schema-
    tolerance + per-line failure isolation matches :func:`load_queue`.
    """
    f.seek(0)
    out: list[MocSuggestion] = []
    for lineno, raw_line in enumerate(f, start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            log.warning(
                "surveyor.moc_suggestion.queue_load_skip_corrupt",
                queue_path=queue_path,
                lineno=lineno,
                error=str(exc)[:200],
            )
            continue
        if not isinstance(data, dict):
            continue
        filtered = {k: v for k, v in data.items() if k in _KNOWN_FIELDS}
        try:
            out.append(MocSuggestion(**filtered))
        except TypeError as exc:
            log.warning(
                "surveyor.moc_suggestion.queue_load_skip_missing_fields",
                queue_path=queue_path,
                lineno=lineno,
                error=str(exc)[:200],
            )
    return out


def _rewrite_locked(queue_path: Path, entries: list[MocSuggestion]) -> None:
    """Atomic rewrite via .tmp + os.replace. Caller MUST hold an
    exclusive flock on the parent file.

    Standard surveyor state-persistence idiom (see ``state.py:save``).
    """
    tmp_path = queue_path.with_suffix(queue_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry.to_dict(), separators=(",", ":")) + "\n")
    os.replace(tmp_path, queue_path)


__all__ = [
    "derive_default_queue_path",
    "load_queue",
    "upsert_proposals",
    "update_status",
]
