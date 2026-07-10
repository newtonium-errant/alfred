"""The accumulated-transcript ledger — the single source of truth for an
encounter's growing transcript (scribe P3-b1).

Each encounter has ONE ledger file, ``<encounter_id>.transcript.json``,
CO-LOCATED with the encounter's chunk data (inside the per-encounter input
subdir) — deliberately NOT in the PHI-free ``state.json``. The ledger holds the
accumulated :class:`~alfred.scribe.transcript.Transcript`, which carries PHI
(the transcript text), so it lives in the same PHI trust-zone as the audio, and
its FILENAME is the opaque ``encounter_id`` (no label leak).

It is the SINGLE SOURCE OF TRUTH for segment-id continuity: the next segment id
derives ONLY from the persisted transcript's segment count (see
``Transcript.append_chunk``), so a crash/restart resumes id numbering exactly.

Contract:
  * ``save_ledger`` writes ATOMICALLY (``.tmp`` → ``os.replace``) and is called
    BEFORE any downstream draft update (the ledger is authoritative; the draft
    is derived).
  * ``load_ledger`` is schema-tolerant (``Transcript.from_dict`` drops unknown
    keys / defaults missing ones), so a ledger written by a newer/older scribe
    version round-trips.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import structlog

from alfred.scribe.transcript import Transcript

log = structlog.get_logger(__name__)

_LEDGER_SUFFIX = ".transcript.json"


def ledger_path(encounter_dir: Path, encounter_id: str) -> Path:
    """The ledger path for an encounter — co-located with its chunk data.

    Named by the OPAQUE ``encounter_id`` (not the possibly-PHI directory name),
    so the filename itself never leaks the operator label.
    """
    return Path(encounter_dir) / f"{encounter_id}{_LEDGER_SUFFIX}"


def load_ledger(path: Path) -> Transcript | None:
    """Load the accumulated transcript from ``path``, or ``None`` if absent.

    Schema-tolerant + defensive: a missing file returns ``None`` (fresh
    encounter); a malformed/corrupt file logs and returns ``None`` rather than
    crash the sweep (the fold then starts fresh — the chunks on disk are the
    ultimate source, and idempotent re-folding is safe).
    """
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning(
            "scribe.ledger.unreadable",
            # OPAQUE BASENAME ONLY (``enc-xxx.transcript.json``). ``str(p)`` would
            # leak the PARENT dir, which IS the encounter's raw label (MAY be
            # PHI — see identity.py); logging it verbatim violates NOTE-4. Only
            # the salted opaque filename is safe. (Comment-lies trap fixed in
            # review: the prior "path is opaque" claim was FALSE for str(p).)
            path=Path(p).name,
            error_class=type(e).__name__,   # class only — never the message
        )
        return None
    if not isinstance(data, dict):
        return None
    return Transcript.from_dict(data)


def save_ledger(path: Path, transcript: Transcript) -> None:
    """Persist ``transcript`` to ``path`` ATOMICALLY (``.tmp`` → ``os.replace``).

    Called after each fold, BEFORE any downstream draft update — the ledger is
    the authoritative accumulated transcript.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(transcript.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, p)
