"""State persistence — pattern miner proposal log.

KAL-LE distiller-radar Phase 4. Tracks every proposal candidate the
miner has ever surfaced via a fingerprint-keyed dict so a second mine
pass against an unchanged cluster doesn't re-propose the same theme.

Fingerprint shape (see :func:`fingerprint_cluster` in
``pattern_miner.py``): SHA-256 of
``"\\n".join(sorted(member_files)) + "\\n--\\n" + ",".join(sorted(labels))``.
A fingerprint stays stable across mine runs as long as both the cluster
membership AND the label tuple are unchanged. Either changing yields
a new fingerprint and a new proposal opportunity.

## Status lifecycle

A proposal cycles through four states based on the operator's response:

- ``pending`` — file exists at ``proposed_path``. Don't re-propose for
  the same fingerprint.
- ``promoted`` — operator moved the proposal file out of
  ``inbox/proposed-canonical/`` (typically into ``architecture/`` or
  ``principles/``). Reconcile sweep marks the entry ``promoted`` on
  the next mine run; don't re-propose for the same fingerprint.
- ``discarded`` — operator deleted the proposal file with no move.
  Reconcile sweep marks the entry ``discarded``; don't re-propose for
  the same fingerprint.
- ``superseded`` — fingerprint changed (cluster membership or labels
  shifted) before the previous proposal was acted on. New proposal
  supersedes; old marked ``superseded``.

The reconcile sweep distinguishes ``promoted`` from ``discarded`` by
walking the configured ``canonical_match_dirs`` for any file whose
slug matches ``proposed_slug``; if found, status flips to
``promoted``. If absent and ``proposed_path`` is also absent, status
flips to ``discarded`` (operator's decision recorded as "no").

## Schema-tolerance contract

``ProposalEntry.from_dict`` filters incoming dict keys against
``__dataclass_fields__`` per the project-wide load() schema-tolerance
contract (see CLAUDE.md "State persistence — load() schema-tolerance
contract"). A future-version state file with extra fields silently
ignores them on rollback; an older file missing fields gets defaults.
This is the same shape used in distiller's ``state.py`` /
``backfill.py`` and surveyor's ``state.py``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# State file schema version. Bump when the on-disk shape changes in a
# way that the schema-tolerance filter alone can't smooth over (e.g. a
# field rename with semantic drift). The loader ignores the version
# today — it's a forward-compat marker for future migration paths.
_STATE_VERSION: int = 1


# Status sentinels. Plain strings (not StrEnum) for trivial JSON round-
# trip without a custom encoder. The four valid states are documented
# in the module docstring.
STATUS_PENDING: str = "pending"
STATUS_PROMOTED: str = "promoted"
STATUS_DISCARDED: str = "discarded"
STATUS_SUPERSEDED: str = "superseded"

_VALID_STATUSES: frozenset[str] = frozenset({
    STATUS_PENDING, STATUS_PROMOTED, STATUS_DISCARDED, STATUS_SUPERSEDED,
})


@dataclass
class ProposalEntry:
    """One Phase 4 proposal candidate the miner has surfaced.

    Fields mirror the proposal markdown's frontmatter shape so the
    state file and the proposal file stay in lockstep on key fields
    (slug, cluster id, member count, proposed type). The status field
    is state-only — the proposal markdown carries ``status: proposed``
    at write time and never changes; the state file owns the lifecycle.
    """

    fingerprint: str = ""
    cluster_id: str = ""
    labels: list[str] = field(default_factory=list)
    member_count: int = 0
    proposed_at: str = ""  # ISO timestamp
    proposed_path: str = ""  # vault-relative path to the proposal file
    proposed_slug: str = ""
    proposed_canonical_type: str = ""  # "architecture" | "principles"
    status: str = STATUS_PENDING

    @classmethod
    def from_dict(cls, data: dict) -> "ProposalEntry":
        """Schema-tolerant load: filter incoming keys against fields.

        Per the project-wide load() schema-tolerance contract; see
        CLAUDE.md. Unknown legacy fields silently dropped, missing
        fields fall back to dataclass defaults. Adding/removing fields
        across versions never crashes the loader.
        """
        known = set(cls.__dataclass_fields__.keys())
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)

    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "cluster_id": self.cluster_id,
            "labels": list(self.labels),
            "member_count": self.member_count,
            "proposed_at": self.proposed_at,
            "proposed_path": self.proposed_path,
            "proposed_slug": self.proposed_slug,
            "proposed_canonical_type": self.proposed_canonical_type,
            "status": (
                self.status if self.status in _VALID_STATUSES else STATUS_PENDING
            ),
        }


class PatternMinerState:
    """In-memory + on-disk state for the Phase 4 pattern miner.

    Keyed by fingerprint (the cluster identity hash). One entry per
    proposal-ever-surfaced; lifecycle managed by the reconcile sweep.

    Atomic save: write to .tmp, then ``os.replace`` (matches the
    pattern in distiller/state.py + surveyor/state.py). Crash between
    write and rename leaves the previous state file intact — no
    half-written corruption.
    """

    def __init__(self, state_path: str | Path) -> None:
        self.state_path = Path(state_path)
        self.version: int = _STATE_VERSION
        self.last_run: str = ""
        self.proposals: dict[str, ProposalEntry] = {}

    def load(self) -> None:
        """Load state from disk if it exists.

        Missing state file is the empty-state case (first run): no
        proposals on record, all clusters fair game. Logs an explicit
        ``pattern_miner_state.no_existing_state`` event so observers
        can distinguish first-run from broken-load.
        """
        if not self.state_path.exists():
            log.info(
                "pattern_miner_state.no_existing_state",
                path=str(self.state_path),
            )
            return
        with open(self.state_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        self.version = raw.get("version", _STATE_VERSION)
        self.last_run = raw.get("last_run", "")
        for fp, pdata in raw.get("proposals", {}).items():
            if not isinstance(pdata, dict):
                continue
            entry = ProposalEntry.from_dict(pdata)
            # Defensive: the dict-key fingerprint is authoritative;
            # if the entry's fingerprint field disagrees (older write,
            # corruption), trust the key and rewrite the field on save.
            if not entry.fingerprint:
                entry.fingerprint = fp
            self.proposals[fp] = entry
        log.info(
            "pattern_miner_state.loaded",
            path=str(self.state_path),
            proposals=len(self.proposals),
        )

    def save(self) -> None:
        """Atomic save: write to .tmp then os.replace."""
        self.last_run = datetime.now(timezone.utc).isoformat()
        data = {
            "version": _STATE_VERSION,
            "last_run": self.last_run,
            "proposals": {
                fp: entry.to_dict() for fp, entry in self.proposals.items()
            },
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp_path, self.state_path)

    # ------------------------------------------------------------------
    # Lookup helpers — keep the call sites in pattern_miner.py terse.
    # ------------------------------------------------------------------

    def has_entry_for_fingerprint(self, fingerprint: str) -> bool:
        """True iff any proposal entry exists for this fingerprint.

        Operator-decided fingerprints stay decided — the whole point
        of the state file is to remember the decision. Re-proposing a
        discarded theme on every run would be noise. Hence: ANY
        existing entry blocks re-proposal for the same fingerprint,
        regardless of status. A material cluster change produces a
        new fingerprint → new proposal (the supersede path).
        """
        return fingerprint in self.proposals

    def record_proposal(self, entry: ProposalEntry) -> None:
        """Insert a new proposal entry. Caller is responsible for
        ensuring the fingerprint isn't already present (gate logic)."""
        self.proposals[entry.fingerprint] = entry

    def mark_status(self, fingerprint: str, status: str) -> None:
        """Update the status of an existing entry. No-op if missing."""
        if status not in _VALID_STATUSES:
            log.info(
                "pattern_miner_state.invalid_status",
                fingerprint=fingerprint, status=status,
            )
            return
        entry = self.proposals.get(fingerprint)
        if entry is None:
            return
        entry.status = status

    def supersede(self, old_fingerprint: str) -> None:
        """Mark an old fingerprint as superseded. Used when a cluster's
        identity has shifted (new fingerprint) before the operator
        acted on the old proposal."""
        self.mark_status(old_fingerprint, STATUS_SUPERSEDED)


__all__ = [
    "ProposalEntry",
    "PatternMinerState",
    "STATUS_PENDING",
    "STATUS_PROMOTED",
    "STATUS_DISCARDED",
    "STATUS_SUPERSEDED",
]
