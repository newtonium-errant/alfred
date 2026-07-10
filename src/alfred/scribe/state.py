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

# Success/terminal states — never reprocessed.
_DONE_STATES = frozenset({STATE_DRAFTED, STATE_ATTESTED, STATE_REFUSED})

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
