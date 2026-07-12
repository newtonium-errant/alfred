"""Note-frontmatter completeness marker for the sovereign scribe (#58).

THE single owner of the completeness-proof FIELD NAME + its build/read helpers
(mirrors ``close_manifest.py`` for #57). The proof rides IN the note frontmatter —
the same artifact ``attest`` signs — so:

  * attest reads ONE boolean from the ``vault_read`` it ALREADY performs and NEVER
    touches ``ScribeState`` (preserves the Gap-E prohibition — attest.py imports
    only vault ops + attestation), and the cross-process stale-READY state-file
    race is ELIMINATED (not merely mitigated);
  * the signed record carries its OWN durable completeness proof that survives
    state-file loss / rotation / corruption.

The marker is a self-contained dict ``fm["encounter_completeness"]``:
``{protocol, complete, ready_at, expected_final_seq, folded_through}`` when
complete, or ``{protocol, complete: false, regressed_at, reason}`` when a body
regen de-finalized it. FAIL-CLOSED read: absent / false / malformed / non-dict all
read as INCOMPLETE.

Written ONLY by the daemon (the single writer) at three pipeline sites — stamped
NOTE-FIRST at the READY finalize, cleared atomically with the body at the regen
choke, self-healed for pre-#58 / crash-window markerless READY notes — all via the
``stayc_clinical`` DRAFT-EDIT gate on a LIVE ai_draft (frozen at attest by
construction — deliberately NOT in the ATTEST field set). ``scope.py`` cannot
import this module (the vault layer stays scribe-agnostic), so it cross-references
:data:`MARKER_FIELD` by a documented string LITERAL, guarded by a drift-pin test.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from alfred.vault.ops import VaultError, vault_edit, vault_read

# THE single field name — scope.py pins its literal == this constant (drift guard).
MARKER_FIELD = "encounter_completeness"
MARKER_PROTOCOL = 1

# The DRAFT-EDIT scope the daemon stamps/clears the marker under (a LIVE ai_draft).
_STAMP_SCOPE = "stayc_clinical"


def is_complete(fm: Any) -> bool:
    """FAIL-CLOSED completeness read from a note's frontmatter dict. True ONLY when
    ``fm[MARKER_FIELD]`` is a dict with ``complete is True`` — absent / false /
    malformed / non-dict all read as INCOMPLETE."""
    if not isinstance(fm, dict):
        return False
    m = fm.get(MARKER_FIELD)
    return isinstance(m, dict) and m.get("complete") is True


def _complete_marker(now: datetime, *, expected_final_seq: int | None, folded_through: int) -> dict:
    return {
        "protocol": MARKER_PROTOCOL,
        "complete": True,
        "ready_at": now.isoformat(),
        "expected_final_seq": expected_final_seq,   # None for a synthetic empty close
        "folded_through": folded_through,
    }


def regressed(now: datetime, reason: str) -> dict:
    """The de-finalized marker written atomically with a body regen (the encounter
    is no longer complete until it re-finalizes)."""
    return {
        "protocol": MARKER_PROTOCOL,
        "complete": False,
        "regressed_at": now.isoformat(),
        "reason": reason,
    }


def stamp_complete(
    vault_path: Path, rel_path: str, *, now: datetime,
    expected_final_seq: int | None, folded_through: int,
) -> None:
    """Stamp the note ``complete: true`` via a ``stayc_clinical`` DRAFT-EDIT
    ``vault_edit`` (the note is status==ai_draft at READY, so the DRAFT gate
    permits it). Raises (VaultError / ScopeError) on any write failure — the
    caller (checkpoint) treats a raise as "stay DRAFTED, re-stamp next sweep"."""
    vault_edit(
        vault_path, rel_path,
        set_fields={MARKER_FIELD: _complete_marker(
            now, expected_final_seq=expected_final_seq, folded_through=folded_through,
        )},
        scope=_STAMP_SCOPE,
    )


def maybe_restamp(
    vault_path: Path, rel_path: str, *, now: datetime,
    expected_final_seq: int | None, folded_through: int,
) -> bool:
    """Idempotent SELF-HEAL: stamp a markerless LIVE ai_draft READY note complete —
    closes the pre-#58 migration edge (READY-but-markerless) AND the crash window
    between the stamp ``vault_edit`` and ``state.set(READY)``. NO-OP (returns False)
    when the note isn't a live ai_draft (sealed/missing → never touch) OR already
    carries a complete marker (idempotent). Returns True iff it re-stamped."""
    try:
        rec = vault_read(vault_path, rel_path)
    except VaultError:
        return False                              # note vanished → nothing to heal
    fm = rec.get("frontmatter") or {}
    if fm.get("status") != "ai_draft":
        return False                              # sealed / unknown → do not touch
    if is_complete(fm):
        return False                              # already complete → idempotent no-op
    stamp_complete(
        vault_path, rel_path, now=now,
        expected_final_seq=expected_final_seq, folded_through=folded_through,
    )
    return True
