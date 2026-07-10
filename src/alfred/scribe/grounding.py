"""Deterministic grounding-verify (scribe P2-c) — CODE, not an LLM.

A strong NET for MECHANICAL flips — NOT semantic verification. Over the parsed
:class:`~alfred.scribe.notegen.StructuredNote`, for EVERY claim it
deterministically checks the claim against the transcript segment(s) it cites,
catching the small-model MECHANICAL errors (dose/negation/citation flips) that
an extract-not-infer prompt cannot fully prevent (500mg→5mg, denies→reports,
uncited / fabricated-number assertions).

Checks per claim (each failure ⇒ a flag):
  1. NO source_spans                 → ``ungrounded_assertion``.
  2. a source_span is not a real     → ``ungrounded_span``.
     segment id in the transcript
  3. a NUMBER/DOSE token in the      → ``number_mismatch``   (FORWARD: a
     claim not present verbatim in       note-introduced/changed number is a
     the cited segment(s)                 fabrication; an OMITTED source number
                                          is a legitimate summary, not flagged.
                                          Decimal-boundary-safe: 5mg never
                                          matches 0.5mg / 2.5mg / 12.5mg.)
  4. NEGATION — INVENTED or FLIPPED  → ``negation_mismatch`` (P2-e redesign,
     (B) the claim asserts a negation     REPLACES the P2-c bidirectional
         NOT in the cited segment;        set-EQUALITY. Set-equality was
     (C) the cited segment NEGATES a      empirically CATASTROPHIC — 66% flag
         finding the claim asserts        rate — because it is incompatible with
         POSITIVELY (targeted phrase      the atomic-claim design: an atomic
         check, not set-diff).            claim carries a SUBSET of its
                                          multi-finding segment's negations, so
                                          it could NEVER equal the full set →
                                          near-everything flagged → alarm
                                          fatigue. The SUBSET case is now CLEAN;
                                          (B) invented + (C) targeted-flip keep
                                          the real safety catches.)

⚠️ NOT caught here — relies on the extract-not-infer PROMPT (prompt-tuner) +
the human ATTEST. This gate verifies mechanical grounding, NOT meaning. An
UNFLAGGED claim is NOT proof of grounding — a clinician's trust calibration
depends on knowing these gaps:
  (a) PURE QUALITATIVE fabrication — a claim with no numbers/negations cited to
      a real segment (e.g. "history of MI" invented from a segment that never
      says it) passes CLEAN, by design (no token to check).
  (b) WRONG-SYMPTOM negation — "Denies SOB" cited to "denies chest pain": the
      token ``denies`` matches on both sides, the segment does not negate SOB,
      so the (C) flip does not fire — the SYMPTOM is fabricated but passes.
  (c) DROPPED negation in a BUNDLED (non-atomic) claim — a claim bundling a
      positive + a negative can hide a flipped positive inside a matching
      negation set. Delegated to the atomic-claim contract + human attest (the
      A/B corpus found ZERO flipped/dropped negations across 189 real claims —
      empirically safe; the 66%-FP set-equality "safety" was net-negative).
  (d) COMPOSITE-number coincidence — "BP 120/80" whose digits happen to appear
      as "Room 120, bed 80" in the cited segment passes CLEAN.
  (e) (C)-FLIP mechanism limits — the targeted-flip check extracts the
      finding-phrase that FOLLOWS a negation and requires it to be >=4 chars, so
      two flip shapes slip through (both SAFE-direction under-flags, backstopped
      by the atomic prompt + human attest):
        * SHORT ABBREVIATIONS below the len>=4 filter — "denies SOB" → the
          negated phrase "sob" (3 chars) is dropped → a "Reports SOB" flip is
          MISSED.
        * POST-POSITIVE negation — "bowel sounds absent" (the negation comes
          AFTER the finding) → no finding-phrase is extracted after the
          negation → a "Bowel sounds present" flip is MISSED. (The pre-finding
          form "absent bowel sounds" / "lacks bowel sounds" IS caught.)

FAILURE POLICY = FLAG-IN-NOTE (the draft still proceeds — it is an ai_draft and
the clinician attest is the human gate):
  * every flag is recorded in :attr:`GroundingResult.metadata` — the
    ``grounding_flags`` frontmatter list — so ATTESTING a flagged draft is
    AUDITABLE (the clinician sees exactly which claims + why);
  * ``render_soap`` (notegen) REQUIRES a :class:`GroundingResult` and renders
    each flag UNMISSABLE inline — so a note cannot be rendered without the
    grounding result. (Airtight verify-BEFORE-render — a combined
    generate→verify→render — is enforced structurally in the P2-d pipeline.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from alfred.scribe.notegen import GROUNDING_UNVERIFIED, SOAP_SECTIONS, StructuredNote
from alfred.scribe.transcript import Transcript

log = structlog.get_logger(__name__)


class GroundingIntegrityError(Exception):
    """The transcript itself is structurally corrupt — DUPLICATE segment ids.

    Raised FAIL-CLOSED by :func:`verify` before the ``{id: segment}`` map is
    built. A duplicate id would make the map silently last-wins overwrite, so a
    claim citing ``[S3]`` could be grounded against the WRONG ``S3`` — passing
    grounding CLEAN against text it never cited. That silent mis-grounding is the
    exact medico-legal failure this system exists to prevent, so a corrupt
    transcript is refused outright rather than verified.
    """

# Number+unit (dose / vital / measurement). Word-final unit boundary.
_UNIT = (
    r"(?:mg|mcg|g|kg|ml|l|units?|iu|%|mmhg|bpm|cm|mm|/min|/day|/week|/hr|c|f)"
)
_NUMBER_UNIT_RE = re.compile(rf"\d+(?:\.\d+)?\s*{_UNIT}\b", re.IGNORECASE)
_BARE_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
# Negation tokens (clinical), word-bounded. CURATED set. TWO tokens are
# deliberately EXCLUDED because they false-register (their FP justification):
#   * "non": ``\bnon\b`` matches the "non" INSIDE "non-productive" /
#     "nonspecific" (the "-" is a word boundary), so a POSITIVE claim citing a
#     segment with "non-productive cough" false-flagged (66% FP root-cause #2).
#   * "free": ``\bfree\b`` matches "carbohydrate-free" / "pain-free" as
#     positives.
# "no" is safe — ``\bno\b`` does NOT match inside "none"/"nonspecific" (a word
# char follows "no"). "neither/nor/lacks" ARE included — they are word-bounded-
# SAFE (verified: ``\blacks?\b`` ∌ black/lackadaisical/lacerate; ``\bnor\b`` ∌
# norepinephrine/north/minor/normal; ``\bneither\b`` clean) AND real clinical
# negations. Dropping them (an earlier over-correction) opened confirmed holes:
# (C) "Bowel sounds present" citing "Abdomen lacks bowel sounds" missed the
# flip; (B) "Lacks insight" citing "Patient is oriented" missed the invented
# negation; "Reports fever" citing "Neither fever nor chills" missed the flip.
# "no evidence of" is covered by "no".
_NEGATION_RE = re.compile(
    r"\b(no|not|denies|denied|deny|without|negative|none|never|absent|"
    r"neither|nor|lacks?)\b",
    re.IGNORECASE,
)

# Leading quantifiers/articles stripped off a negated finding-phrase before the
# targeted-flip check, so "denies any chest pain" → phrase "chest pain".
_FLIP_STOPWORDS = frozenset({"any", "a", "an", "the", "his", "her", "their", "of"})


@dataclass
class GroundingFlag:
    """One auditable grounding flag → a ``grounding_flags`` frontmatter entry."""

    section: str
    claim_index: int
    reason: str
    detail: str
    claim: str
    source_spans: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "section": self.section,
            "claim_index": self.claim_index,
            "reason": self.reason,
            "detail": self.detail,
            "claim": self.claim,
            "source_spans": list(self.source_spans),
        }


@dataclass
class GroundingResult:
    flags: list[GroundingFlag] = field(default_factory=list)

    @property
    def metadata(self) -> list[dict[str, Any]]:
        """The ``grounding_flags`` frontmatter value (auditable)."""
        return [f.to_dict() for f in self.flags]

    @property
    def clean(self) -> bool:
        return not self.flags

    def flag_for(self, section: str, index: int) -> str | None:
        """Return the inline flag literal for the claim at
        ``(section, index)``, or ``None`` if it is clean. The single source of
        truth ``render_soap`` reads — no hidden mutation of the claim objects,
        and render CANNOT run without a GroundingResult."""
        for f in self.flags:
            if f.section == section and f.claim_index == index:
                return GROUNDING_UNVERIFIED
        return None


def _normalize(text: str) -> str:
    """Lowercase + glue ``<number> <space> <unit>`` → ``<number><unit>`` so a
    claim ``5 mg`` and a segment ``5mg`` compare equal (and ``500mg`` never
    matches ``5mg``)."""
    t = text.lower()
    return re.sub(rf"(\d+(?:\.\d+)?)\s+({_UNIT})", r"\1\2", t)


def _number_tokens(text: str) -> list[str]:
    """Normalized number+unit tokens AND standalone bare numbers from ``text``.

    Unit tokens are extracted FIRST and stripped before bare-number extraction,
    so ``500`` is never separately extracted from ``500mg`` (which would
    false-positive under word-boundary matching against a cited ``500mg``)."""
    norm = _normalize(text)
    unit_tokens = [re.sub(r"\s+", "", m.group(0)) for m in _NUMBER_UNIT_RE.finditer(norm)]
    residual = _NUMBER_UNIT_RE.sub(" ", norm)
    bare = [m.group(0) for m in _BARE_NUMBER_RE.finditer(residual)]
    return unit_tokens + bare


def _token_in(tok: str, norm_cited: str) -> bool:
    """Decimal-aware numeric-boundary presence.

    NOT ``\\b`` — ``\\b`` treats ``.`` as a word boundary, so ``\\b5mg\\b`` would
    match the ``5mg`` INSIDE ``0.5mg`` (a 10x dose overstate passing CLEAN — the
    exact flip class this gate exists for).

    But a naive digit-OR-DOT boundary ``(?<![\\d.])...(?![\\d.])`` over-rejects a
    SENTENCE-FINAL period: ``120`` vs ``"bp is 120."`` → a false ⚠ flag (the
    trailing ``.`` is a sentence period, not a decimal point). On real dictation
    numbers end sentences constantly → alarm fatigue → the flag that MATTERS
    gets ignored. So distinguish a decimal point (``.`` with a DIGIT on the
    fractional side) from a sentence period:
      * left  ``(?<!\\d)(?<!\\d\\.)`` — reject a token in the fractional part
        (``5`` in ``0.5``, preceded by ``0.``) or glued to a digit (``5`` in
        ``15``);
      * right ``(?!\\d)(?!\\.\\d)`` — reject a token that CONTINUES into a
        decimal (``12`` before ``.5`` in ``12.5``) or another digit, but ACCEPT
        a ``.`` NOT followed by a digit (a sentence end).

    Regression-guarded (must stay REJECTED): 5mg vs 0.5mg / 2.5mg / 12.5mg /
    15mg / 500mg, 5 vs 2.5, 12 vs 12.5. Newly ACCEPTED (FP closed): 120 vs
    "bp is 120.", 98.6 vs "temp is 98.6.". Self-matches unchanged.
    """
    return re.search(
        r"(?<!\d)(?<!\d\.)" + re.escape(tok) + r"(?!\d)(?!\.\d)", norm_cited
    ) is not None


def _negation_set(text: str) -> set[str]:
    return {m.group(1).lower() for m in _NEGATION_RE.finditer(text)}


def _negated_finding_phrases(text: str) -> list[str]:
    """For each negation in ``text``, the finding-phrase it governs — the run
    after the negation up to the next punctuation / conjunction, with leading
    quantifiers stripped. ("denies shortness of breath. reports cough" →
    ["shortness of breath"].) Trivial (<4-char) phrases are dropped."""
    phrases: list[str] = []
    low = text.lower()
    for m in _NEGATION_RE.finditer(low):
        tail = low[m.end():]
        # " nor " is a coordinator boundary too, so "neither fever nor chills"
        # → the "neither" phrase is "fever" (not "fever nor chills"); the "nor"
        # negation separately governs "chills".
        raw = re.split(r"[.,;:]| and | or | nor | but | with ", tail, maxsplit=1)[0]
        words = [w for w in raw.strip().split() if w]
        while words and words[0] in _FLIP_STOPWORDS:
            words = words[1:]
        phrase = " ".join(words)
        if len(phrase) >= 4:
            phrases.append(phrase)
    return phrases


def _flipped_positive_findings(claim_text: str, cited_text: str) -> list[str]:
    """Targeted FLIP check (C): findings the cited segment NEGATES that the
    claim asserts POSITIVELY. Only applies to a positive claim (one that carries
    NO negation of its own) — a negative claim about the same finding is the
    consistent subset case, not a flip. Word-bounded phrase match to avoid
    trivial substring hits."""
    if _negation_set(claim_text):
        return []  # the claim itself negates → not a positive flip
    claim_low = claim_text.lower()
    flipped: list[str] = []
    for phrase in _negated_finding_phrases(cited_text):
        if re.search(r"\b" + re.escape(phrase) + r"\b", claim_low):
            flipped.append(phrase)
    return flipped


def _cited_text(claim_spans, seg_by_id) -> str:
    return " ".join(seg_by_id[s].text for s in claim_spans if s in seg_by_id)


def verify(structured: StructuredNote, transcript: Transcript) -> GroundingResult:
    """Deterministically verify grounding. Returns the auditable
    :class:`GroundingResult` (no mutation of the claim objects — ``render_soap``
    reads flags via ``GroundingResult.flag_for``, so a note can never be
    rendered without the grounding result)."""
    # FAIL-CLOSED integrity gate (scribe P3-b1) — refuse a transcript with
    # DUPLICATE segment ids BEFORE building the {id: segment} map. Last-wins map
    # overwrite would ground a claim against the wrong same-id segment and pass
    # clean. Mutation-bound: remove this and a dup-id transcript verifies clean.
    ids = [s.id for s in transcript.segments]
    if len(set(ids)) != len(ids):
        dupes = sorted({sid for sid in ids if ids.count(sid) > 1})
        raise GroundingIntegrityError(
            f"transcript has duplicate segment ids {dupes} — refusing to verify "
            f"(a same-id collision silently grounds claims against the wrong "
            f"segment). Segment count={len(ids)}, unique={len(set(ids))}."
        )
    seg_by_id = {s.id: s for s in transcript.segments}
    result = GroundingResult()

    for section, idx, claim in structured.all_claims():
        reasons: list[str] = []

        # (1) ungrounded assertion — no citation at all.
        if not claim.source_spans:
            reasons.append("ungrounded_assertion: claim cites no source segment")
        else:
            # (2) every cited span must be a real segment id.
            bad = [s for s in claim.source_spans if s not in seg_by_id]
            if bad:
                reasons.append(
                    f"ungrounded_span: cited segment(s) {bad} not in transcript"
                )

        cited = _cited_text(claim.source_spans, seg_by_id)
        norm_cited = _normalize(cited)

        # (3) FORWARD number/dose check — every claim number must be in the cite.
        missing_nums = [
            tok for tok in _number_tokens(claim.claim)
            if not _token_in(tok, norm_cited)
        ]
        if missing_nums:
            reasons.append(
                f"number_mismatch: {missing_nums} not verbatim in cited segment(s)"
            )

        # (4) NEGATION check — INVENTED + targeted FLIP (P2-e redesign).
        #
        # REPLACES the P2-c bidirectional set-EQUALITY, which was empirically
        # catastrophic: it is FUNDAMENTALLY incompatible with the atomic-claim
        # design we mandated in P2-c — each atomic claim carries a SUBSET of its
        # multi-finding segment's negations, so it could NEVER equal the full
        # set → 66% flag rate (~all negation_mismatch), alarm fatigue, the ⚠
        # that matters ignored. The subset case is now CLEAN.
        #
        # Two genuine-error catches preserve the real safety intent:
        #   (B) INVENTED negation — the claim asserts a negation NOT present in
        #       the cited segment (claim_negs ⊄ cited_negs).
        #   (C) FLIPPED negation — the cited segment NEGATES a finding that the
        #       claim asserts POSITIVELY (targeted phrase check, not set-diff).
        # The dropped-negation-in-a-bundled-claim case is delegated to the
        # atomic-claim prompt + human attest (the A/B corpus found ZERO
        # flipped/dropped negations across 189 real claims — empirically safe).
        claim_negs = _negation_set(claim.claim)
        cited_negs = _negation_set(cited)
        invented = claim_negs - cited_negs
        flipped = _flipped_positive_findings(claim.claim, cited)
        if invented or flipped:
            detail: list[str] = []
            if invented:
                detail.append(
                    f"invented negation(s) {sorted(invented)} not in cited segment(s)"
                )
            if flipped:
                detail.append(
                    f"claim asserts positively a finding the cited segment "
                    f"negates: {flipped}"
                )
            reasons.append("negation_mismatch: " + "; ".join(detail))

        if reasons:
            result.flags.append(
                GroundingFlag(
                    section=section,
                    claim_index=idx,
                    reason=reasons[0].split(":", 1)[0],
                    detail="; ".join(reasons),
                    claim=claim.claim,
                    source_spans=list(claim.source_spans),
                )
            )

    log.info(
        "scribe.grounding.verified",
        source_id=transcript.source_id,
        mode=transcript.mode,
        total_claims=sum(len(structured.section(s)) for s in SOAP_SECTIONS),
        flagged=len(result.flags),
    )
    return result
