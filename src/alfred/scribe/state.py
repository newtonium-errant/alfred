"""Per-source pipeline state for the sovereign scribe (scribe P2-d).

Keyed by ``source_id`` (opaque hash in clinical mode; PHI-FREE — see NOTE-4).
Persisted so a crash/restart RESUMES: a source that already reached ``drafted``
is never reprocessed; an intermediate/failed source is re-run from the top
(idempotent). Load applies the schema-tolerance filter (the load() contract).

State machine: ``recorded → transcribing → structuring → drafted → attested``.
Operational additions: ``refused`` (non-synthetic input rejected by the mode
gate — terminal) and ``failed`` (an exception mid-pipeline — RETRIABLE until
``attempts`` hits the cap, then terminal). ``attested`` is set ONLY by
``scribe/attest.py`` (never the pipeline).

PHI: every field here is PHI-FREE — ``source_id`` (opaque in clinical),
``note_path`` (derived from source_id), ``last_error_class`` (an exception
class name, never a message/transcript). No title/transcript text is ever
written to this file.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# --- the state machine ------------------------------------------------------
STATE_RECORDED = "recorded"
STATE_TRANSCRIBING = "transcribing"
STATE_STRUCTURING = "structuring"
STATE_DRAFTED = "drafted"
STATE_ATTESTED = "attested"
STATE_REFUSED = "refused"   # non-synthetic input rejected by the mode gate
STATE_FAILED = "failed"     # mid-pipeline exception (retriable until the cap)
# --- P3-b2 checkpoint co-pilot states ---------------------------------------
STATE_BUDGET_CAPPED = "budget_capped"   # regen over context budget; last-good draft kept, still folding
STATE_HUMAN_EDITED = "human_edited"     # a human edited the draft; auto-evolution FROZEN (opt-in to resume)
STATE_READY = "ready"                   # _CLOSED: draft complete, ready for attestation (attest stays orchestrator-only)
# --- P3-b3 attest-semantics state -------------------------------------------
STATE_POST_ATTEST_AUDIO = "post_attest_audio"  # new audio arrived AFTER the draft was attested — REFUSED + surfaced (clinician may need to amend); the signed note is untouched

# Success/terminal states — never reprocessed by the flat one-shot path.
# NOTE: the checkpoint (subdir) path does NOT gate on this — it makes its own
# state decisions (a ``budget_capped`` encounter keeps FOLDING; a ``ready`` one
# is finalized). ``budget_capped`` / ``human_edited`` are deliberately NOT
# "done": the encounter is still live (folding / awaiting operator opt-in), just
# not auto-drafting this checkpoint.
_DONE_STATES = frozenset({STATE_DRAFTED, STATE_ATTESTED, STATE_REFUSED, STATE_READY})

# A failed source is retried until it has been attempted this many times, then
# it becomes terminal (skipped, logged) to bound the retry loop.
MAX_ATTEMPTS = 3


@dataclass
class SourceState:
    source_id: str
    state: str = STATE_RECORDED
    note_path: str = ""          # vault rel_path once drafted (for attest)
    attempts: int = 0
    last_error_class: str = ""   # exception CLASS name only — NEVER PHI
    updated_at: str = ""
    # P3-b2 clobber-detect: sha256 of the note BODY as the pipeline last WROTE
    # it (re-read from disk after the write). A HASH — PHI-FREE (irreversible),
    # NOT the body text. The checkpoint compares the on-disk body's sha against
    # this before body_replace: a mismatch means a HUMAN edited the draft →
    # freeze auto-evolution rather than clobber a clinician correction.
    pipeline_body_sha: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceState":
        # Load-time schema-tolerance contract — filter unknown fields so a
        # state file from a newer/older version never crashes the loader.
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ScribeState:
    """Source-id → :class:`SourceState`, JSON-persisted with atomic writes."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.sources: dict[str, SourceState] = {}

    def load(self) -> None:
        if not self.path.exists():
            log.info("scribe.state.no_existing_state", path=str(self.path))
            return
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for sid, sdata in (raw.get("sources") or {}).items():
            if isinstance(sdata, dict):
                self.sources[sid] = SourceState.from_dict(sdata)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "sources": {k: v.to_dict() for k, v in self.sources.items()},
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def get(self, source_id: str) -> SourceState | None:
        return self.sources.get(source_id)

    def is_done(self, source_id: str) -> bool:
        """True iff the source is at a success/terminal state (never reprocess),
        OR a ``failed`` source that has exhausted its retry budget."""
        st = self.sources.get(source_id)
        if st is None:
            return False
        if st.state in _DONE_STATES:
            return True
        return st.state == STATE_FAILED and st.attempts >= MAX_ATTEMPTS

    def set(self, source_id: str, **fields: Any) -> SourceState:
        """Update (or create) a source's state and persist. Every transition
        saves — so a crash after any transition resumes from the last one."""
        st = self.sources.get(source_id) or SourceState(source_id=source_id)
        for k, v in fields.items():
            setattr(st, k, v)
        st.updated_at = datetime.now(timezone.utc).isoformat()
        self.sources[source_id] = st
        self.save()
        return st
