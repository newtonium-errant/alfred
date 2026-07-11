"""Structural attestation orchestrator for clinical_note (scribe P2-a, #41).

The ONLY sanctioned path to set a clinical_note's attest triad (``status`` /
``attested_by`` / ``attested_at``). Makes the P1-c ``authorize_attestation``
primitives STRUCTURAL, not advisory:

  * the ``stayc_clinical`` (pipeline/agent) scope CANNOT raw-flip the triad
    (its edit gate ``stayc_clinical_no_attest`` hard-denies) nor create a
    born-attested note (the create-bypass guard in
    ``stayc_clinical_types_only``);
  * the ONLY writer of the triad is this orchestrator, which
    (1) reads the note + asserts ``type == clinical_note``,
    (2) runs :func:`scribe.authorize_attestation` (forward-only lifecycle +
        distinct-human-clinician attester + non-empty creator),
    (3) writes the triad via ``vault_edit`` under the PRIVILEGED
        ``stayc_clinical_attest`` scope (a scope the pipeline can never
        select), and
    (4) appends a durable, PHI-FREE medico-legal attest audit
        (``clinical_attest_audit.jsonl``) + emits ``scribe.attest.recorded``.

Deliberately a scribe-layer orchestrator, NOT a ``vault_attest`` op in
``ops.py`` — keeps the vault layer scribe-agnostic (no vault→scribe import).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import structlog

from alfred.scribe.attestation import (
    SCRIBE_DRAFTER_IDENTITY,
    AttestationError,
    authorize_attestation,
)
from alfred.scribe.identity import compute_encounter_id
from alfred.vault.ops import vault_edit, vault_read

log = structlog.get_logger(__name__)

# The privileged scope the orchestrator writes under (SCOPE_RULES). The
# pipeline/agent scope can never select it; its edit gate allows exactly the
# attest triad on clinical_note.
ATTEST_SCOPE = "stayc_clinical_attest"

# The attest triad — the ONLY fields this orchestrator writes.
_TRIAD_STATUS = "status"
_TRIAD_ATTESTED_BY = "attested_by"
_TRIAD_ATTESTED_AT = "attested_at"


def source_id_for(raw_label: str | None, *, salt: str) -> str:
    """Return the SALTED, opaque ``source_id`` for logs / the attest audit.

    Delegates to :func:`alfred.scribe.identity.compute_encounter_id` — a salted
    HMAC of the raw label, non-reversible without the per-instance secret
    (``scribe.encounter_salt``). This REPLACED the P2 implementation, which (a)
    returned the operator label VERBATIM in synthetic mode — a PHI leak the salt
    now closes — and (b) prefixed clinical ids with ``"sha256:"``, a colon that
    corrupted note filenames. There is no ``mode`` branch: the salt makes
    label-hashing safe in EVERY mode, and the id is stable across an encounter's
    chunks (the label is the identity, not the per-chunk bytes)."""
    return compute_encounter_id(raw_label or "synthetic", salt=salt)


def _append_attest_audit(audit_path: str | Path, entry: dict) -> None:
    """Append one JSONL line to the durable medico-legal attest audit.

    PHI-FREE by construction: the caller passes only clinician id, from/to
    status, timestamp, note path, and the (already-opaque-in-clinical-mode)
    source_id. Never write transcript / note body / patient identifiers here.
    """
    p = Path(audit_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def attest(
    vault_path: Path,
    rel_path: str,
    *,
    new_status: str,
    attester: str,
    clinician_ids,
    audit_path: str | Path,
    creator: str | None = None,
    now: datetime | None = None,
) -> dict:
    """Attest a clinical_note — the ONLY sanctioned triad writer. Fail-closed.

    Args:
        vault_path / rel_path: the clinical_note to attest.
        new_status: target status (``attested`` / ``amended``). The forward-
            only lifecycle is enforced by ``authorize_attestation``.
        attester: the human clinician performing the attestation.
        clinician_ids: the designated-clinician allowlist (``scribe.clinicians``);
            an empty set refuses everyone (fail-closed).
        audit_path: the durable attest-audit JSONL path.
        creator: the draft creator. Defaults to the note's ``drafted_by``
            frontmatter (P2-d sets it), falling back to
            :data:`SCRIBE_DRAFTER_IDENTITY` — so the distinct-attester check
            refuses the scribe attesting its own draft even on P1-b notes that
            predate ``drafted_by``.
        now: attestation timestamp (defaults to UTC now); injectable for tests.

    Returns:
        the ``vault_edit`` result dict.

    Raises:
        AttestationError: non-clinical_note target, or any lifecycle / attester
            / creator violation from ``authorize_attestation``.
        VaultError / ScopeError: from the underlying vault ops.
    """
    now = now or datetime.now(timezone.utc)

    rec = vault_read(vault_path, rel_path)
    fm = rec["frontmatter"] or {}
    rtype = fm.get("type")
    if rtype != "clinical_note":
        raise AttestationError(
            "not_clinical_note",
            f"scribe.attest refuses to attest a non-clinical_note record "
            f"(type={rtype!r}, path={rel_path!r}). Attestation applies only to "
            f"clinical notes.",
        )

    current_status = fm.get("status", "ai_draft")
    effective_creator = creator or fm.get("drafted_by") or SCRIBE_DRAFTER_IDENTITY

    # THE gate — forward-only lifecycle + distinct-human-clinician + non-empty
    # creator. Raises AttestationError on any violation (self-attest included).
    authorize_attestation(
        current_status=current_status,
        new_status=new_status,
        attester=attester,
        creator=effective_creator,
        clinician_ids=clinician_ids,
    )

    # Write the triad under the PRIVILEGED scope. vault_edit parses the record
    # type from the note; the ``stayc_clinical_attest`` scope's edit gate
    # allows exactly {status, attested_by, attested_at} + refuses body writes.
    result = vault_edit(
        vault_path,
        rel_path,
        set_fields={
            _TRIAD_STATUS: new_status,
            _TRIAD_ATTESTED_BY: attester,
            _TRIAD_ATTESTED_AT: now.isoformat(),
        },
        scope=ATTEST_SCOPE,
    )

    # Durable, PHI-FREE medico-legal audit. The note is identified by
    # ``source_id`` ONLY — an opaque hash in clinical mode (the pipeline hashed
    # it). The record PATH / title is DELIBERATELY excluded: a clinical_note
    # title can carry PHI, so it never lands in this durable trail (or in the
    # scribe.log line below). The file path IS captured in the standard vault
    # audit (data/vault_audit.log) when enabled — a separate, operational trail.
    source_id = fm.get("source_id", "")
    _append_attest_audit(
        audit_path,
        {
            "ts": now.isoformat(),
            "op": "attest",
            "source_id": source_id,
            "from_status": current_status,
            "to_status": new_status,
            "attester": attester,
            "creator": effective_creator,
        },
    )
    log.info(
        "scribe.attest.recorded",
        source_id=source_id,
        from_status=current_status,
        to_status=new_status,
        attester=attester,
    )

    # #48 self-correcting Part-1 CAPTURE — a READ-ONLY signal (per inferred_diagnosis
    # flag, kept-vs-removed in the attested body). SIDE-EFFECT-FREE: it runs AFTER
    # the triad write + audit + recorded-log already succeeded, and is wrapped so
    # any error here can NEVER alter/fail the attestation (medico-legal path). The
    # attested body == rec["body"] (attest writes only the triad, never the body).
    try:
        from alfred.scribe.inferred_dx import record_inferred_dx_attest_outcome
        record_inferred_dx_attest_outcome(
            grounding_flags=fm.get("grounding_flags"),
            draft_original=str(fm.get("draft_original") or ""),
            attested_body=str(rec.get("body") or ""),
            source_id=source_id,
        )
    except Exception:  # noqa: BLE001 — belt: capture must never affect a valid attest
        log.warning("scribe.inferred_dx.attest_capture_error", source_id=source_id)

    return result
