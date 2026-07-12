"""Shared close-manifest for the sovereign scribe close lifecycle (#57).

THE single owner of the ``_CLOSED`` sentinel NAME + its content contract, imported
by BOTH the ingest server (``ingest_web.py`` — the WRITER) and the pipeline
(``pipeline.py`` — the READER). This kills the prior two-private-``_CLOSED_SENTINEL``
literal drift (each module held its own copy with no shared key/shape).

The sentinel's CONTENT carries the client's PROMISED final seq as a versioned JSON
manifest ``{"protocol": 2, "final_seq": N}`` — the structural "ready ⇒ complete"
assertion (#57): the READY gate finalizes only once seqs ``1..final_seq`` are ALL
folded, so a client that writes ``_CLOSED`` BEFORE the final chunk lands can never
reach a premature READY (structural, not client-discipline-dependent). The manifest
rides the EXISTING atomic sentinel (temp→``os.replace``), so the accumulator never
sees a partial manifest and no new half-closed two-file race is introduced.
PHI-FREE by construction (a version int + a seq int).

READ CONTRACT — ``read_close_manifest(path, *, require) -> (expected_final_seq: int|None, ambiguous: bool)``:
  * EMPTY content (legacy ``""`` close)               → ``(None, ambiguous=require)``
    — empty is ambiguous ONLY under strict (clinical / require) mode; legacy-tolerant
    otherwise (the shipped synthetic PWA's empty close still finalizes to READY).
  * VALID ``{"protocol": 2, "final_seq": N}`` (N int ≥ 1) → ``(N, False)``.
  * MALFORMED JSON / missing/non-int/<1 final_seq / UNKNOWN protocol
                                                        → ``(None, ambiguous=True)``
    — FAIL-CLOSED ALWAYS (regardless of ``require``): a corrupt promise can never
    finalize READY.

STRICT ENFORCEMENT — ``resolve_require_close_manifest(config)`` = clinical mode OR
the explicit ``scribe.require_close_manifest`` opt-in. In strict mode a missing /
empty / ambiguous manifest is fail-closed at BOTH the ``/close`` route (400,
nothing written) AND the checkpoint gate (``close_ambiguous`` → never READY), so
the invariant is structural exactly at the medico-legal boundary #57 gates, while
the shipped synthetic PWA stays legacy-tolerant.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from alfred.scribe.config import SCRIBE_MODE_CLINICAL

# THE single sentinel name (both the writer and the reader import this — no drift).
CLOSE_SENTINEL_NAME = "_CLOSED"
_MANIFEST_PROTOCOL = 2


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic text write (temp → ``os.replace``) — the SAME discipline the ingest
    server uses for the sentinel, so accumulate never observes a partial manifest."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_close_manifest(enc_dir: Path, final_seq: int) -> None:
    """Write the versioned close manifest into the encounter's ``_CLOSED`` sentinel
    (atomic). ``final_seq`` is the client's asserted final seq (the completeness bar)."""
    _atomic_write_text(
        Path(enc_dir) / CLOSE_SENTINEL_NAME,
        json.dumps({"protocol": _MANIFEST_PROTOCOL, "final_seq": int(final_seq)}),
    )


def read_close_manifest(path: Path, *, require: bool) -> tuple[int | None, bool]:
    """Parse the ``_CLOSED`` sentinel content → ``(expected_final_seq, ambiguous)``.

    See the module docstring for the full contract. Empty → legacy-tolerant unless
    ``require``; any malformed / unknown-protocol content is FAIL-CLOSED
    (``ambiguous=True``) regardless of ``require``."""
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError:
        # sentinel vanished between the exists() check and the read — treat like
        # empty (ambiguous under strict, legacy-tolerant otherwise).
        return (None, require)
    stripped = content.strip()
    if not stripped:
        return (None, require)                 # legacy empty close
    try:
        data: Any = json.loads(stripped)
    except json.JSONDecodeError:
        return (None, True)                    # malformed JSON → fail-closed ALWAYS
    if not isinstance(data, dict) or data.get("protocol") != _MANIFEST_PROTOCOL:
        return (None, True)                    # unknown / missing protocol → fail-closed
    fs = data.get("final_seq")
    # bool is an int subclass — exclude it explicitly (a "True" final_seq is corrupt).
    if not isinstance(fs, int) or isinstance(fs, bool) or fs < 1:
        return (None, True)                    # missing / non-int / <1 → fail-closed
    return (fs, False)


def resolve_require_close_manifest(config: Any) -> bool:
    """True iff the close manifest is REQUIRED (strict enforcement): clinical mode
    OR the explicit ``scribe.require_close_manifest`` opt-in."""
    return getattr(config, "mode", "") == SCRIBE_MODE_CLINICAL or bool(
        getattr(config, "require_close_manifest", False)
    )
