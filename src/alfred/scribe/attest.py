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
    (4) DUAL-WRITES the durable, PHI-FREE medico-legal trail — the legacy
        ``clinical_attest_audit.jsonl`` line FIRST (an independent trail that
        survives any event-store bug), THEN the durable ``attest.recorded``
        event into the hash-chained store (event-store design §5.2/§8 row 2) —
        and emits the ``scribe.attest.recorded`` structlog line.

Deliberately a scribe-layer orchestrator, NOT a ``vault_attest`` op in
``ops.py`` — keeps the vault layer scribe-agnostic (no vault→scribe import).
The attestation-as-event integration adds a scribe→``scribe.events`` dependency
(the ``events`` facade is threaded in as a keyword arg by ``cmd_scribe``, never
imported at the vault layer) — a scribe→scribe edge that does NOT touch the
vault's scribe-agnosticism invariant (design §2.3).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:  # scribe→scribe.events dependency (design §2.3); type-only import
    from alfred.scribe.events import ScribeEvents

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
    enrollment_dir: str | Path | None = None,
    events: "ScribeEvents | None" = None,
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
        events: the STAY-C event-store facade (design §5.2). When present:
            (1) ``preflight()`` runs BEFORE the first vault_read — a store that
            cannot open fail-closes the attest (``event_store_unavailable``), so
            an attested note without a durable trail is near-impossible; (2) a
            refusal emits best-effort ``attest.refused`` (never masking the
            refusal); (3) success dual-writes ``attest.recorded`` [D] AFTER the
            legacy audit line, in the SAME post-triad slot (the CAS window is
            NOT widened). ``None`` (tests / non-clinical callers) → the attest
            path is byte-identical to pre-#11.

    Returns:
        the ``vault_edit`` result dict.

    Raises:
        AttestationError: non-clinical_note target, or any lifecycle / attester
            / creator violation from ``authorize_attestation``.
        VaultError / ScopeError: from the underlying vault ops.
    """
    now = now or datetime.now(timezone.utc)

    # Store PREFLIGHT (design §5.2) — open + flock acquire/release + tip
    # resolution, NO append, BEFORE the first vault_read. A store that cannot
    # open fail-closes the attest here (never mid-operation), making an
    # attested-note-without-trail near-impossible. This sits ENTIRELY before the
    # CAS bracket, so it cannot affect the CAS window. Best-effort by nature: the
    # facade is None for tests / non-clinical callers → skipped.
    if events is not None:
        try:
            events.preflight()
        except Exception as exc:  # noqa: BLE001 — any open failure fail-closes the attest
            raise AttestationError(
                "event_store_unavailable",
                "the medico-legal event store failed to open (preflight) — "
                "refusing to attest without a durable audit trail. Likely a "
                "permissions/disk problem on the events dir; re-run once fixed.",
            ) from exc

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

    # PHI-FREE event provenance — HOISTED so BOTH the refusal events
    # (attest.refused) and the success event (attest.recorded) carry the same
    # facts, computed from the FIRST read (fm). ``completeness`` is a fixed enum
    # (complete | incomplete | absent — never free text); ``grounding_reasons``
    # is the ``reason`` enum of each flag ONLY (the free-text ``claim`` is PHI and
    # never leaves frontmatter, design §5.2/§11).
    source_id = fm.get("source_id", "")
    completeness = (
        "complete" if complete
        else ("incomplete" if isinstance(fm.get(completeness_marker.MARKER_FIELD), dict) else "absent")
    )
    _gflags = fm.get("grounding_flags")
    grounding_flag_count = len(_gflags) if isinstance(_gflags, list) else 0
    grounding_reasons = (
        [str(f.get("reason")) for f in _gflags if isinstance(f, dict) and f.get("reason")]
        if isinstance(_gflags, list) else []
    )

    # THE gate — completeness (FIRST, #58) + forward-only lifecycle + distinct-
    # human-clinician + non-empty creator. Raises AttestationError on the first
    # violation. An override (allow_incomplete + reason) bypasses ONLY completeness.
    # A refusal emits best-effort attest.refused (never masking the refusal), then
    # re-raises — attestation.py stays pure (no store import; the store call is here).
    try:
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
    except AttestationError as exc:
        if events is not None:
            events.attest_refused(
                subject_id=source_id, attester=attester, reason=exc.reason,
                from_status=current_status, to_status=new_status,
                completeness=completeness, forced=bool(allow_incomplete),
                now=now.isoformat(),
            )
        raise

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
    # The attested-version pin (design §5.2): the sha of the body as it stands at
    # the CAS re-read — the exact bytes the triad is about to sign. Persisted into
    # ``attest.recorded`` + the attested-digest index (the post-attest-edit sweep's
    # source of truth). One field from computed-here to persisted.
    attested_body_sha = _body_sha(rec2.get("body", ""))
    body_stable = _body_sha(rec.get("body", "")) == attested_body_sha
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
    # (``source_id`` / ``completeness`` / grounding provenance were computed once
    # at the FIRST read above — HOISTED so the refusal AND success events carry
    # identical PHI-free facts; not recomputed here.)
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

    # DUAL-WRITE, durable event SECOND (design §8 row 2 / §5.2). The legacy JSONL
    # line above is the independent trail that survives any event-store bug; THIS is
    # the hash-chained medico-legal record (the ``attest.recorded`` [D] event). It is
    # DURABLE — it raises on failure, the SAME fail-loud posture as the legacy append
    # above (both post-triad + unwrapped, so the CAS window is NOT widened; the slot
    # is unchanged). ``attest_recorded`` also pins the attested-version digest into
    # the attested-digest index UNDER the clinical lock (§7.4) — the post-attest-edit
    # sweep's source of truth. ``events`` is None for tests / non-clinical callers
    # (cmd_scribe passes None on a degraded store) → skipped, path byte-identical
    # to pre-#11.
    if events is not None:
        events.attest_recorded(
            subject_id=source_id,
            attester=attester,
            from_status=current_status,
            to_status=new_status,
            creator=effective_creator,
            forced=bool(allow_incomplete),
            completeness=completeness,
            body_sha=attested_body_sha,
            grounding_flag_count=grounding_flag_count,
            grounding_reasons=grounding_reasons,
            rel_path=rel_path,
            now=now.isoformat(),
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

    # P4-5 self-correcting Part-1 CAPTURE for SPEAKER attribution — the TWIN of the
    # inferred-dx capture above. READ-ONLY, try-wrapped, fail-silent (a capture bug must
    # NEVER alter/fail a medico-legal attestation). Records each speaker-attribution flag's
    # reason + whether the flagged claim SURVIVED into the attested body (``kept`` = a
    # normalized-substring heuristic → the correction vehicle: kept ⇒ likely a
    # false-positive flag; removed ⇒ the check was right). No-op unless ``enrollment_dir``
    # is set (the capture sink lives under it).
    try:
        if enrollment_dir:
            _capture_speaker_attest_outcome(
                enrollment_dir=enrollment_dir,
                grounding_flags=fm.get("grounding_flags"),
                diarize_provenance=fm.get("diarize_provenance"),
                attested_body=str(rec.get("body") or ""),
                source_id=source_id,
            )
    except Exception:  # noqa: BLE001 — belt: capture must never affect a valid attest
        log.warning("scribe.enroll_learning.attest_capture_error", source_id=source_id)

    return result


def _capture_speaker_attest_outcome(
    *, enrollment_dir, grounding_flags, diarize_provenance, attested_body, source_id,
) -> None:
    """Append an ``attest_outcome`` capture row per SPEAKER-attribution flag (P4-5).

    Mirrors the inferred-dx twin: for each speaker-attribution flag in the note's
    ``grounding_flags``, record the reason + ``kept`` (did the flagged claim's content
    survive into the attested body — a normalized-substring heuristic, the P4-5
    correction vehicle). The note-level ``attribution_unverified`` BANNER carries no
    claim → recorded ``is_banner=True`` (a banner-carrying note was signed). Preset
    provenance rides the frontmatter ``diarize_provenance`` (ids only — PHI-free)."""
    from alfred.scribe import enroll_learning
    from alfred.scribe.diagnosis_lexicon import normalize_text
    from alfred.scribe.speaker_attribution import (
        ATTRIBUTION_UNVERIFIED_REASON,
        COLLATERAL_ATTRIBUTION_REASON,
        SPEAKER_MISMATCH_REASON,
        SPEAKER_UNVERIFIED_REASON,
    )
    speaker_reasons = {
        SPEAKER_MISMATCH_REASON, SPEAKER_UNVERIFIED_REASON,
        COLLATERAL_ATTRIBUTION_REASON, ATTRIBUTION_UNVERIFIED_REASON,
    }
    if not isinstance(grounding_flags, list):
        return
    prov = diarize_provenance if isinstance(diarize_provenance, dict) else {}
    user, preset_id = prov.get("user"), prov.get("preset_id")
    centroid_version = prov.get("centroid_version")
    norm_body = normalize_text(attested_body)
    for flag in grounding_flags:
        if not isinstance(flag, dict) or flag.get("reason") not in speaker_reasons:
            continue
        is_banner = flag.get("reason") == ATTRIBUTION_UNVERIFIED_REASON
        if is_banner:
            kept = True                       # the note was signed banner-carrying (note-level)
        else:
            norm_claim = normalize_text(str(flag.get("claim") or ""))
            kept = bool(norm_claim) and norm_claim in norm_body
        enroll_learning.record_attest_outcome(
            enrollment_dir,
            source_id=source_id, user=user, preset_id=preset_id,
            centroid_version=centroid_version, reason=flag.get("reason"),
            kept=kept, is_banner=is_banner,
        )
