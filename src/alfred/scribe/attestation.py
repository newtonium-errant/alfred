"""Attestation-integrity controls for clinical_note (scribe P1-c).

NOTE-2 from the P1-b review: the medico-legal attestation controls belong at
THIS pipeline/identity layer, NOT in the vault schema. Two controls, both
fail-closed, enforced at the code path that changes a clinical_note's status /
sets ``attested_by`` (the "attest transition"). For P1-c the mechanism is
present + pinned on synthetic records; real clinician-auth wiring is deferred.

(a) FORWARD-ONLY status lifecycle: ``ai_draft → attested → amended`` ONLY. No
    reverting ``attested → ai_draft`` (no un-attesting), no skipping, no
    same→same, no backward. Enforced at the transition; fail-closed on any
    illegal transition.

(b) DISTINCT HUMAN-CLINICIAN attester: the AI/scribe CANNOT attest its own
    draft. ``attested_by`` must be a designated human clinician, distinct from
    both the scribe drafting agent (:data:`SCRIBE_DRAFTER_IDENTITY`) AND the
    record's creator (no self-attestation). Auto-attestation by the pipeline is
    structurally impossible — the drafter identity is refused as an attester.

⚠️ NOT-YET-WIRED — false-coverage warning (P1-c review). The P1-b vault scope
gate ``stayc_clinical_attest_only`` is a field-NAME allowlist ONLY. It does NOT
enforce the forward-only status lifecycle or the distinct-attester identity
check — those are HERE, in :func:`authorize_attestation`, which is NOT yet
wired. The pipeline (P2) MUST route every clinical_note status/attested_by
write (create AND edit) through :func:`authorize_attestation`; the scope
field-allowlist is necessary but NOT sufficient. Do not mistake that gate for
attestation enforcement.

Observability (intentionally-left-blank): every authorization decision emits a
``scribe.attestation`` event (``authorized`` + ``reason`` + from/to status) so
a refused attestation is distinguishable from an idle pipeline.
"""

from __future__ import annotations

from collections.abc import Iterable

import structlog

log = structlog.get_logger(__name__)


class AttestationError(Exception):
    """Raised when an attest transition violates lifecycle or attester
    integrity. ``reason`` is a greppable id for triage."""

    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"attestation refused [{reason}]: {detail}")


# The scribe drafting agent/process identity — the AI that creates every
# ai_draft. NEVER a valid attester (structurally blocks auto-attestation). A
# clinical_note's ``attested_by`` must never equal this.
SCRIBE_DRAFTER_IDENTITY = "stayc_scribe"

# clinical_note statuses (mirrors the schema TypeDefinition; kept here as the
# lifecycle's source of truth for the transition gate).
STATUS_AI_DRAFT = "ai_draft"
STATUS_ATTESTED = "attested"
STATUS_AMENDED = "amended"

# The ONLY legal forward transitions. Explicit set (not a rank comparison) so
# skips (ai_draft→amended), reverts (attested→ai_draft — un-attesting),
# same→same, and unknown statuses are ALL refused by exclusion. Fail-closed.
_LEGAL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    (STATUS_AI_DRAFT, STATUS_ATTESTED),
    (STATUS_ATTESTED, STATUS_AMENDED),
})


def validate_status_transition(current_status: str, new_status: str) -> None:
    """Enforce the forward-only lifecycle. Raises :class:`AttestationError`
    unless ``(current, new)`` is one of the two legal forward transitions.

    Refuses (fail-closed): the un-attest revert ``attested → ai_draft``, the
    skip ``ai_draft → amended``, any backward move, same→same, and any
    transition involving an unknown status.
    """
    pair = (current_status, new_status)
    if pair not in _LEGAL_TRANSITIONS:
        raise AttestationError(
            "illegal_status_transition",
            f"transition {current_status!r} → {new_status!r} is not permitted. "
            f"The clinical_note lifecycle is forward-only: "
            f"{STATUS_AI_DRAFT} → {STATUS_ATTESTED} → {STATUS_AMENDED}. "
            f"Reverting an attested note to {STATUS_AI_DRAFT} (un-attesting), "
            f"skipping, or moving backward is refused — a correction is a NEW "
            f"clinical_note with status {STATUS_AMENDED!r} that supersedes.",
        )


def validate_attester(
    *,
    attester: str,
    creator: str,
    clinician_ids: Iterable[str],
) -> None:
    """Enforce the distinct-human-clinician attester rule. Raises
    :class:`AttestationError` unless ``attester`` is a designated clinician,
    non-empty, distinct from the scribe drafter, and distinct from the record
    creator (no self-attestation).

    Args:
        attester: the identity performing the attestation.
        creator: the identity that created the ai_draft (the scribe drafter in
            the normal flow). REQUIRED non-empty (fail-closed — see the
            ``creator_missing`` check); ``attester`` must differ from it.
        clinician_ids: the designated-clinician allowlist. ``attester`` must be
            in it. Fail-closed: an empty allowlist refuses everyone (no real
            clinician-auth is wired in P1-c — the mechanism is present + pinned).
    """
    a = (attester or "").strip()
    if not a:
        raise AttestationError(
            "attester_missing",
            "an attester identity is required — a clinical_note cannot be "
            "attested anonymously.",
        )
    if a == SCRIBE_DRAFTER_IDENTITY:
        raise AttestationError(
            "scribe_self_attest",
            f"the scribe drafting agent ({SCRIBE_DRAFTER_IDENTITY!r}) may not "
            f"attest — attestation requires a distinct human clinician. "
            f"Auto-attestation by the pipeline is structurally forbidden.",
        )
    # NOTE-2 fail-closed hardening (P1-c review): REQUIRE a non-empty creator.
    # A medico-legal self-attest guard must not be disable-able by omitting the
    # creator — the old ``if creator and a == creator`` short-circuited the
    # equality check when creator was empty/None/blank, silently skipping the
    # self-attest refusal. Fail closed: no creator => no attestation.
    c = (creator or "").strip()
    if not c:
        raise AttestationError(
            "creator_missing",
            "a non-empty draft-creator identity is required — the "
            "self-attestation guard must not be disable-able by omitting the "
            "creator. A clinical_note records who drafted it; attestation "
            "cannot proceed without it.",
        )
    if a == c:
        raise AttestationError(
            "self_attest",
            "the attester must differ from the draft creator — the identity "
            "that created a note may not attest it.",
        )
    if a not in set(clinician_ids):
        raise AttestationError(
            "attester_not_clinician",
            "the attester is not a designated clinician. attested_by must be a "
            "human clinician identity on the designated-clinician allowlist.",
        )


def validate_encounter_complete(
    *, encounter_complete: bool, forced: bool, force_reason: str | None,
) -> None:
    """#58 fail-closed completeness precondition. The encounter MUST be provably
    complete (its note-frontmatter marker read ``complete: true``) unless a human
    clinician EXPLICITLY forces the attestation with a non-empty reason.

    Raises :class:`AttestationError`:
      * ``encounter_incomplete`` — the marker is absent/false/malformed AND not
        forced (the medico-legal core: attest never silently signs an incomplete
        note);
      * ``force_without_reason`` — forced but the reason is empty/whitespace (an
        override must carry an auditable justification)."""
    if not encounter_complete and not forced:
        raise AttestationError(
            "encounter_incomplete",
            "the encounter is NOT provably complete (its note-frontmatter "
            "completeness marker is absent/false) — refusing to attest an "
            "incomplete clinical note. The daemon stamps the marker at the READY "
            "finalize; a legacy/markerless note requires an explicit, audited "
            "--force-incomplete with a reason.",
        )
    if forced and not (force_reason or "").strip():
        raise AttestationError(
            "force_without_reason",
            "an incomplete-attest override (--force-incomplete) requires a "
            "non-empty --reason — refusing a reasonless force.",
        )


def authorize_attestation(
    *,
    current_status: str,
    new_status: str,
    attester: str,
    creator: str,
    clinician_ids: Iterable[str],
    encounter_complete: bool,
    forced: bool = False,
    force_reason: str | None = None,
) -> None:
    """Combined fail-closed gate for a clinical_note attest transition.

    Runs THREE controls — the #58 encounter-completeness precondition (FIRST),
    the forward-only lifecycle, AND the distinct-human-clinician attester — and
    emits a ``scribe.attestation`` observability event. Raises
    :class:`AttestationError` on the first violation (after logging the refusal
    with its reason). ``encounter_complete`` is a REQUIRED keyword (no default) —
    the fail-closed choice so this primitive can never be called without the
    completeness verdict. An override (``forced=True`` + a non-empty
    ``force_reason``) bypasses ONLY the completeness precondition; the lifecycle +
    attester controls remain ABSOLUTE (a human clinician still takes explicit
    medico-legal responsibility).
    """
    try:
        # #58 — completeness is the FIRST refusal (before lifecycle/attester).
        validate_encounter_complete(
            encounter_complete=encounter_complete, forced=forced, force_reason=force_reason,
        )
        validate_status_transition(current_status, new_status)
        validate_attester(
            attester=attester, creator=creator, clinician_ids=clinician_ids,
        )
    except AttestationError as e:
        log.warning(
            "scribe.attestation",
            authorized=False,
            reason=e.reason,
            from_status=current_status,
            to_status=new_status,
            encounter_complete=encounter_complete,
            forced=forced,
        )
        raise
    log.info(
        "scribe.attestation",
        authorized=True,
        reason="authorized",
        from_status=current_status,
        to_status=new_status,
        encounter_complete=encounter_complete,
        forced=forced,
    )
