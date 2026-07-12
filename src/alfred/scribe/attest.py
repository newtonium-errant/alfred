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

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import structlog

from alfred.scribe import completeness_marker
from alfred.scribe.attestation import (
    SCRIBE_DRAFTER_IDENTITY,
    AttestationError,
    authorize_attestation,
)
from alfred.scribe.identity import compute_encounter_id
from alfred.vault.mutation_log import append_to_audit_log, build_audit_mutations
from alfred.vault.ops import vault_edit, vault_read

log = structlog.get_logger(__name__)


def _body_sha(body: str) -> str:
    """sha256 of a note body — the CAS-bracket stability fingerprint. PHI-FREE
    (irreversible)."""
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


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
    allow_incomplete: bool = False,
    override_reason: str | None = None,
    vault_audit_path: str | Path | None = None,
) -> dict:
    """Attest a clinical_note — the ONLY sanctioned triad writer. Fail-closed.

    #58: refuses unless the encounter is provably complete (its note-frontmatter
    ``encounter_completeness`` marker reads ``complete: true``), UNLESS
    ``allow_incomplete=True`` with a non-empty ``override_reason`` (an audited
    clinician override that bypasses ONLY the completeness precondition — the
    lifecycle + attester controls stay absolute).

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
        allow_incomplete: the audited --force-incomplete override — attest an
            incomplete encounter (bypasses ONLY the completeness precondition;
            lifecycle + attester stay absolute). Requires a non-empty
            ``override_reason`` (else ``force_without_reason``).
        override_reason: the free-text justification for a forced override. #58-D2:
            routed to the VAULT AUDIT (``vault_audit_path``), NEVER the PHI-free
            ``audit_path`` (clinical_attest_audit.jsonl).
        vault_audit_path: the general vault mutation-provenance trail
            (``<logging.dir>/vault_audit.log``). Where a forced override's
            free-text ``override_reason`` is recorded (keeps the attest audit
            PHI-free by construction). No-op if unset.

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

    # #58 — the encounter-completeness precondition, read from the SAME frontmatter
    # attest already vault_read (NEVER ScribeState — preserves Gap-E). Fail-closed:
    # absent/false/malformed marker → incomplete.
    complete = completeness_marker.is_complete(fm)

    # THE gate — completeness (FIRST, #58) + forward-only lifecycle + distinct-
    # human-clinician + non-empty creator. Raises AttestationError on the first
    # violation. An override (allow_incomplete + reason) bypasses ONLY completeness.
    authorize_attestation(
        current_status=current_status,
        new_status=new_status,
        attester=attester,
        creator=effective_creator,
        clinician_ids=clinician_ids,
        encounter_complete=complete,
        forced=allow_incomplete,
        force_reason=override_reason,
    )

    # #58 CAS bracket — a double-read immediately before the triad write closes the
    # attest-read↔triad-write TOCTOU to microseconds. Re-read the note; REFUSE
    # note_changed_under_attest unless status is unchanged AND the body-sha is
    # unchanged from the first read AND (the marker is STILL complete OR this is a
    # forced override). When forced, skip the marker re-check but STILL assert
    # status + body stable — even a forced legacy attest can't race a concurrent
    # regen. (Best-effort, not a hard mutex — a regen between this read and the
    # triad write is backstopped by the pipeline's post-attest seal detection.)
    rec2 = vault_read(vault_path, rel_path)
    fm2 = rec2["frontmatter"] or {}
    status_stable = fm2.get("status", "ai_draft") == current_status
    body_stable = _body_sha(rec.get("body", "")) == _body_sha(rec2.get("body", ""))
    marker_ok = allow_incomplete or completeness_marker.is_complete(fm2)
    if not (status_stable and body_stable and marker_ok):
        log.warning(
            "scribe.attest.note_changed_under_attest",
            source_id=fm.get("source_id", ""),
            status_stable=status_stable, body_stable=body_stable, marker_ok=marker_ok,
        )
        raise AttestationError(
            "note_changed_under_attest",
            "the clinical_note changed between attest's read and the triad write "
            "(status / body / completeness moved under attest) — refusing to sign "
            "a moving target. Re-run attest.",
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
    # #58 — PHI-FREE completeness provenance in the durable trail. ``completeness``
    # is a fixed enum ("complete" / "incomplete" / "absent") — never free text.
    completeness = (
        "complete" if complete
        else ("incomplete" if isinstance(fm.get(completeness_marker.MARKER_FIELD), dict) else "absent")
    )
    forced_engaged = allow_incomplete and not complete
    if forced_engaged:
        # Loud, audited override — a clinician took explicit responsibility to sign
        # an incomplete encounter (the bypass ENGAGED because the note was not
        # complete). Default (no flag) is strict-refuse, handled by authorize above.
        log.warning(
            "scribe.attest.incomplete_override",
            source_id=source_id,
            completeness=completeness,
            attester=attester,
            detail="ATTESTED an INCOMPLETE encounter under --force-incomplete "
                   "(audited clinician override).",
        )
        # #58-D2 (operator decision) — route the FREE-TEXT override_reason to the
        # VAULT AUDIT (``<logging.dir>/vault_audit.log`` — the general
        # mutation-provenance trail that already carries arbitrary ``detail`` text),
        # NOT the clinical_attest_audit.jsonl. This keeps the attest audit
        # PHI-FREE by construction (it records only ``forced`` + the completeness
        # enum below — never the reason). override_reason is guaranteed non-empty
        # here (authorize raised force_without_reason otherwise). No-op if the
        # caller didn't supply a vault-audit path.
        if vault_audit_path is not None:
            append_to_audit_log(
                vault_audit_path,
                "scribe",
                build_audit_mutations("edit", rel_path),
                detail=(
                    f"force-incomplete attest override (completeness={completeness}, "
                    f"attester={attester}): {override_reason}"
                ),
            )
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
            # #58 — PHI-FREE override provenance. ``forced`` = the flag was set;
            # ``completeness`` = the enum. The free-text override_reason is NEVER
            # written to this by-construction PHI-FREE trail — it lands ONLY in the
            # vault audit above (#58-D2 operator decision).
            "forced": bool(allow_incomplete),
            "completeness": completeness,
        },
    )
    log.info(
        "scribe.attest.recorded",
        source_id=source_id,
        from_status=current_status,
        to_status=new_status,
        attester=attester,
        completeness=completeness,
        forced=bool(allow_incomplete),
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
