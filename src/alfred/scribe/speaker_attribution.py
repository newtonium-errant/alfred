"""P4-2 — deterministic speaker-aware grounding + the mis-attribution safety net.

The additive sibling of :mod:`alfred.scribe.inferred_dx` (its TWIN): a
deterministic post-grounding pass that ONLY EXTENDS ``GroundingResult.flags`` —
``grounding.verify()`` stays byte-identical, and (like the #48 twin) the flags
ride the SAME render (``flags_for`` → inline ⚠) + ``grounding_flags`` frontmatter
path. NO LLM; pure deterministic string/graph ops.

EXTRACT-NOT-INFER: attribution is DERIVED from the ``[S#]`` citation graph ×
``Segment.speaker`` (the roles the diarizer already resolved). There is NO
model-emitted attribution field — nothing new for the small model to fabricate.
Token-subset grounding (:mod:`alfred.scribe.grounding`) proves a claim cites the
right WORDS; THIS pass proves it cites the right SPEAKER.

═══════════════════════════════════════════════════════════════════════════════
THE RULES (asymmetric, per SOAP section, over the CITED segments' resolved roles)
═══════════════════════════════════════════════════════════════════════════════
The SOAP sections carry an implicit speaker contract: Objective / Assessment /
Plan are the CLINICIAN's content (measured facts, impression, next steps);
Subjective is the PATIENT's report. The rules flag a claim whose citations
violate that contract.

  * O/A/P — any cited segment with KNOWN role ``patient`` or ``other`` ⇒
    ``speaker_mismatch``, EVEN when a clinician segment is co-cited. (This closes
    the co-citation-laundering hole: "any clinician clears it" would let a
    patient turn be laundered into clinician-authored content by co-citing one
    clinician turn.)
  * ALL sections — any cited segment resolving ``unknown`` (incl. ``None`` and
    sub-purity demotions) ⇒ ``speaker_unverified``.
  * Subjective — any cited segment with KNOWN role ``other`` ⇒
    ``collateral_attribution``. NOT gated on patient-presence: a Subjective claim
    citing ONLY clinician segments gets NO flag (clinician-relayed HPI is legit;
    a spurious flag there is alarm fatigue that erodes the flag that matters).

One flag MAXIMUM per claim per reason; a claim MAY carry multiple DISTINCT
reasons (e.g. an Objective claim citing one ``patient`` + one ``unknown`` turn
carries both ``speaker_mismatch`` and ``speaker_unverified``).

  * NOTE-LEVEL — when ``diarized`` is True AND NO ``clinician`` role appears
    anywhere in the transcript's segments ⇒ a single ``attribution_unverified``
    banner. This is the COMPOSED FAIL-OPEN close: enrollment missing/failed ⇒
    every turn resolves unknown/patient ⇒ the per-claim flags alone could still
    compose into a quiet-looking note, so the banner says attribution AS A WHOLE
    is unverified. Carried as a section-less flag (``section="note"``,
    ``claim_index=-1``) so it rides ``grounding_flags`` frontmatter like any
    other flag AND renders (via ``flags_for("note", -1)``) as a visible banner
    line at the top of the note body.

═══════════════════════════════════════════════════════════════════════════════
ROLE RESOLUTION — the cr-p41 carry-forward (MANDATORY)
═══════════════════════════════════════════════════════════════════════════════
``Transcript.diarized`` LATCHES True once ANY chunk diarizes. A MIXED
accumulation (chunk1 diarized, chunk2 fail-opened) therefore leaves
``diarized=True`` WITH ``speaker=None`` segments. So ``diarized=True`` does NOT
imply every segment carries a canonical role. EVERY cited segment's ``speaker``
is passed through :func:`~alfred.scribe.transcript.normalize_role` (None / '' /
a raw pyannote cluster all fold to ``unknown``, fail-closed).

Additionally (defense-in-depth) a segment whose ``speaker_conf`` is NOT None AND
``< purity_threshold`` is demoted to ``unknown`` for this pass — a sub-purity
turn is not trustworthy. The threshold is ``config.diarize.purity_threshold``
(NOT hardcoded). The ``speaker_conf``-is-None-but-role-known case: the role
STANDS — the engine already fail-closes weak matches to ``unknown`` at resolution
time, so a resolved role with no recorded conf is a confident resolution, not a
missing one; only an EXPLICIT sub-purity conf demotes.

═══════════════════════════════════════════════════════════════════════════════
CLOSE-vs-DOCUMENT LEDGER (P4-2 residual-risk discoverability)
═══════════════════════════════════════════════════════════════════════════════
P4-2 CLOSES:
  (a) co-cited-clinician laundering in O/A/P (a patient/other turn co-cited with
      a clinician turn in a clinician section still flags);
  (b) the composed fail-open (the note-level ``attribution_unverified`` banner);
  (c) unknown / None / sub-purity citations (``speaker_unverified`` in every
      section, plus the cr-p41 mixed-accumulation None-speaker case).

P4-2 DOCUMENTS-BUT-CANNOT-CLOSE — delegated to the P4-3 prompt rules + the human
ATTEST + the P4-6 out-of-band audit (listed verbatim so the residual risk is
discoverable):
  * CLINICIAN RELAY / PARAPHRASE — the clinician restates a patient's answer,
    the claim cites the CLINICIAN turn ⇒ it clears Subjective. This is the most
    common REAL mis-attribution and is invisible here (the cited speaker really
    is the clinician; only the CONTENT originated with the patient).
  * HOME-VITAL READBACK LAUNDERING — the patient's home BP is read back BY the
    clinician; the claim cites the clinician turn ⇒ passes as an Objective vital
    (and passes number grounding, the digits being verbatim).
  * CAREGIVER-AS-PATIENT — a caregiver turn mislabeled ``patient`` by the
    diarizer clears Subjective as if the patient spoke.
  * CROSS-SPAN ELICITATION FUSION — a finding elicited across a clinician
    question + a patient answer, fused into one claim citing both spans.
  * REVERSE CLINICIAN-SPECULATION-AS-PATIENT-CONCERN — the clinician's spoken
    hypothesis reattributed as the patient's stated concern.

STATE PLAINLY: a PARTIAL cluster mislabel (the diarizer swaps two turns' roles
with high purity) is SILENT to this layer — it has no signal to key on. The note
is NEVER presented as "attribution verified"; the human ATTEST is the primary
control, and this pass is the mechanical net beneath it.
"""

from __future__ import annotations

import structlog

from alfred.scribe.config import ScribeConfig
from alfred.scribe.grounding import GroundingFlag
from alfred.scribe.notegen import StructuredNote
from alfred.scribe.transcript import (
    ROLE_CLINICIAN,
    ROLE_OTHER,
    ROLE_PATIENT,
    ROLE_UNKNOWN,
    Segment,
    Transcript,
    normalize_role,
)

log = structlog.get_logger(__name__)

# The reason literals these flags carry (dispatched to their inline ⚠ by
# GroundingResult.flags_for via grounding._REASON_INLINE_LITERAL). The STRINGS are
# duplicated as keys there — grounding cannot import them (this module imports
# grounding, so importing back would cycle), so a test pins the two sides. Same
# shape as inferred_dx.INFERRED_DIAGNOSIS_REASON.
SPEAKER_MISMATCH_REASON = "speaker_mismatch"
SPEAKER_UNVERIFIED_REASON = "speaker_unverified"
COLLATERAL_ATTRIBUTION_REASON = "collateral_attribution"
ATTRIBUTION_UNVERIFIED_REASON = "attribution_unverified"

# The clinician-authored SOAP sections (Objective / Assessment / Plan). A cited
# patient/other turn here is a mismatch; Subjective (the patient's report) has its
# own asymmetric rule (collateral) below.
_OAP_SECTIONS: frozenset[str] = frozenset({"objective", "assessment", "plan"})

# Section-less identity of the NOTE-LEVEL banner flag (rides grounding_flags
# frontmatter + renders via flags_for("note", -1)).
_NOTE_SECTION = "note"
_NOTE_CLAIM_INDEX = -1


def _resolve_role(seg: Segment, purity_threshold: float) -> str:
    """The canonical role of a cited segment under the P4-2 attribution rules.

    normalize_role folds None / '' / a raw pyannote cluster → ``unknown``
    (fail-closed; the cr-p41 mixed-accumulation None-speaker case lands here). An
    EXPLICIT sub-purity conf (``speaker_conf`` is not None AND < ``purity_threshold``)
    additionally demotes a known role to ``unknown`` (defense-in-depth). The
    conf-is-None-but-role-known case: the role STANDS — the engine already
    fail-closes weak matches to ``unknown`` at resolution time, so a resolved role
    with no recorded conf is confident, not missing."""
    role = normalize_role(seg.speaker)
    conf = seg.speaker_conf
    if conf is not None and conf < purity_threshold:
        return ROLE_UNKNOWN
    return role


def check_speaker_attribution(
    structured: StructuredNote, transcript: Transcript, config: ScribeConfig,
) -> list[GroundingFlag]:
    """Return the P4-2 speaker-attribution :class:`GroundingFlag`s for ``structured``.

    A deterministic post-grounding pass that only PRODUCES flags (the pipeline
    EXTENDS ``grounding.flags`` with them, exactly like the #48 twin). Returns
    ``[]`` unchanged when the transcript is un-diarized — the whole pass no-ops so
    an un-diarized encounter's flags / rendered note / frontmatter are
    BYTE-IDENTICAL to pre-P4-2.

    See the module docstring for the asymmetric per-section rules, the cr-p41
    role-resolution contract, and the close-vs-document residual-risk ledger."""
    # GATE — un-diarized transcripts carry no trustworthy roles; the pass no-ops
    # so the note is byte-identical to pre-P4-2 (frozen-contract requirement).
    if not transcript.diarized:
        return []

    purity = config.diarize.purity_threshold
    seg_by_id = {s.id: s for s in transcript.segments}
    flags: list[GroundingFlag] = []

    for section, idx, claim in structured.all_claims():
        # Resolve the role of every REAL cited segment. A cited span that is NOT a
        # real segment id contributes NO role here — grounding already flags it
        # (ungrounded_span); this layer keys only on the citation graph's actual
        # speakers (documented judgment: don't double-flag a missing span).
        cited_roles = [
            _resolve_role(seg_by_id[s], purity)
            for s in claim.source_spans
            if s in seg_by_id
        ]

        reasons: list[tuple[str, str]] = []  # (reason, detail) — one per distinct reason

        # ALL sections: any cited turn whose speaker is unverified.
        if ROLE_UNKNOWN in cited_roles:
            reasons.append((
                SPEAKER_UNVERIFIED_REASON,
                "speaker_unverified: a cited turn's speaker could not be "
                "confidently identified (unknown / un-diarized / sub-purity) — "
                "the claim's attribution is unverified; clinician to confirm",
            ))

        if section in _OAP_SECTIONS:
            # O/A/P is clinician-authored: a cited patient/other turn is a
            # mismatch EVEN if a clinician turn is co-cited (co-citation-laundering
            # close — do NOT let a co-cited clinician clear it).
            if ROLE_PATIENT in cited_roles or ROLE_OTHER in cited_roles:
                reasons.append((
                    SPEAKER_MISMATCH_REASON,
                    f"speaker_mismatch: this {section} claim (clinician-authored "
                    "content) cites a patient/other turn — the content may be "
                    "mis-attributed; clinician to confirm the speaker",
                ))
        elif section == "subjective":
            # Subjective is the patient's report. A cited OTHER (caregiver/family)
            # turn is collateral history, not the patient's own words. NOT gated
            # on patient-presence — a clinician-ONLY Subjective claim (relayed HPI)
            # is legit and gets NO flag (alarm-fatigue guard).
            if ROLE_OTHER in cited_roles:
                reasons.append((
                    COLLATERAL_ATTRIBUTION_REASON,
                    "collateral_attribution: this subjective claim cites a "
                    "caregiver/other turn — it is collateral history, not the "
                    "patient's own report; clinician to confirm",
                ))

        for reason, detail in reasons:
            flags.append(GroundingFlag(
                section=section,
                claim_index=idx,
                reason=reason,
                detail=detail,
                claim=claim.claim,
                source_spans=list(claim.source_spans),
            ))

    # NOTE-LEVEL banner — diarized but NO clinician voice anywhere (the composed
    # fail-open close). Uses the SAME role resolution (a sub-purity clinician does
    # NOT clear the banner) so the whole-note caveat fires whenever no trustworthy
    # clinician turn exists.
    all_roles = [_resolve_role(s, purity) for s in transcript.segments]
    if ROLE_CLINICIAN not in all_roles:
        flags.append(GroundingFlag(
            section=_NOTE_SECTION,
            claim_index=_NOTE_CLAIM_INDEX,
            reason=ATTRIBUTION_UNVERIFIED_REASON,
            detail=(
                "attribution_unverified: this encounter is diarized but NO "
                "clinician voice was identified anywhere — enrollment may be "
                "missing/failed, so speaker attribution is unreliable throughout "
                "(the per-claim flags alone could compose into a quiet-looking "
                "note); clinician to treat all attribution as unverified"
            ),
            claim="",
            source_spans=[],
        ))

    return flags
