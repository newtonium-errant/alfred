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
  4. NEGATION — INVENTED or FLIPPED  → ``negation_mismatch`` (P2-f: (B) is
     (B) the claim negates a CONCEPT      negated-CONCEPT grounding — a claim
         the cited segment does NOT       negation is grounded iff the CITE
         also negate (in ANY surface      negates the SAME concept in ANY surface
         form / marker);                  form (contraction-aware); a concept
     (C) the cited segment NEGATES a      negated NOWHERE in the cite flags.
         finding the claim asserts        REPLACES the P2-e marker SET-DIFFERENCE,
         POSITIVELY (targeted phrase      which false-flagged faithful paraphrases
         check, not set-diff).            that realize the same pertinent-negative
                                          with a DIFFERENT negation surface ("no
                                          X" vs "haven't noticed any X"). The
                                          lexically-DISJOINT-paraphrase residual
                                          ("not adequately controlled" vs "haven't
                                          come down as hoped") stays FLAGGED by
                                          design → the #26 learned-suppression
                                          loop, not a looser threshold. (C)
                                          targeted-flip unchanged. Atomic-claim
                                          SUBSET case stays CLEAN.)

⚠️ NOT caught here — relies on the extract-not-infer PROMPT (prompt-tuner) +
the human ATTEST. This gate verifies mechanical grounding, NOT meaning. An
UNFLAGGED claim is NOT proof of grounding — a clinician's trust calibration
depends on knowing these gaps:
  (a) PURE QUALITATIVE fabrication — a claim with no numbers/negations cited to
      a real segment (e.g. "history of MI" invented from a segment that never
      says it) passes CLEAN, by design (no token to check).
  (b) DROPPED negation in a BUNDLED (non-atomic) claim — a claim bundling a
      positive + a negative can hide a flipped positive inside a matching
      negation set. Delegated to the atomic-claim contract + human attest (the
      A/B corpus found ZERO flipped/dropped negations across 189 real claims —
      empirically safe; the 66%-FP set-equality "safety" was net-negative).
  (c) COMPOSITE-number coincidence — "BP 120/80" whose digits happen to appear
      as "Room 120, bed 80" in the cited segment passes CLEAN.
  (d) (C)-FLIP mechanism limits — the targeted-flip check extracts the
      finding-phrase that FOLLOWS a negation and requires it to be >=4 chars, so
      two flip shapes slip through (both SAFE-direction under-flags, backstopped
      by the atomic prompt + human attest):
        * SHORT ABBREVIATIONS below the len>=4 filter — a POSITIVE "Reports SOB"
          citing "denies SOB": the (C) negated-phrase "sob" (3 chars) is dropped
          → the flip is MISSED. (NOTE: the NEGATION direction — "Denies SOB"
          citing "denies chest pain", a fabricated pertinent-negative — IS now
          caught by the (B) negated-CONCEPT check, which sees "sob" negated
          NOWHERE in the cite. Only the positive-claim (C) flip retains this
          short-abbrev gap.)
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
from typing import Any, Protocol

import structlog

from alfred.scribe.notegen import (
    ATTRIBUTION_UNVERIFIED,
    COLLATERAL_ATTRIBUTION,
    GROUNDING_UNVERIFIED,
    INFERRED_DIAGNOSIS,
    QUALITY_ASSESSMENT_NO_PLAN,
    QUALITY_REQUIRED_SECTION_EMPTY,
    QUALITY_VERBOSE,
    SOAP_SECTIONS,
    SPEAKER_MISMATCH,
    SPEAKER_UNVERIFIED,
    StructuredNote,
)
from alfred.scribe.transcript import Transcript

log = structlog.get_logger(__name__)


# ── Mechanical grounding-reason codes (the 4 verify()-minted reasons) ────────────
# The reason CODE minted for each mechanical failure — the token BEFORE the ":" in the
# flag string (``reasons[0].split(":", 1)[0]``). Extracted to named constants so
# :data:`MECHANICAL_GROUNDING_REASONS` is a LIVE registry the namespace-disjointness pin
# derives from (test_scribe_notegen_quality) instead of a hand-maintained copy. The VALUES
# are byte-identical to the former inline literals — grounding DETECTION is unchanged
# (pinned by ``test_grounding_detector_unchanged_on_a_clean_note``; mutate a value so it
# differs from its mint site and that pin reds).
UNGROUNDED_ASSERTION_REASON = "ungrounded_assertion"
UNGROUNDED_SPAN_REASON = "ungrounded_span"
NUMBER_MISMATCH_REASON = "number_mismatch"
NEGATION_MISMATCH_REASON = "negation_mismatch"

# The LIVE registry of grounding's OWN (mechanical) reason codes — the disjointness pin
# unions this with ``inferred_dx.GROUNDING_REASONS`` + ``speaker_attribution.GROUNDING_REASONS``
# (which grounding cannot import — those modules import grounding, so a reverse import cycles).
# A NEW mechanical reason MUST be registered here alongside its mint site (both co-located in
# this file) so it auto-enters the guard. Practical ceiling: a brand-new INLINE literal that is
# NOT added here is not auto-detected — the guard covers REGISTERED reasons (see the pin docstring).
MECHANICAL_GROUNDING_REASONS: frozenset[str] = frozenset({
    UNGROUNDED_ASSERTION_REASON,
    UNGROUNDED_SPAN_REASON,
    NUMBER_MISMATCH_REASON,
    NEGATION_MISMATCH_REASON,
})


class NegationSuppressionStore(Protocol):
    """Duck-typed #26 Phase-2 approved-suppression store, passed into :func:`verify`
    by the PIPELINE (the concrete impl is ``negation_suppression.NegationSuppression``).

    ``verify`` consults it as an ADDITIONAL suppression source on the (B) negated-CONCEPT
    path ONLY — never (C) (a positive/negative flip is a real contradiction, never
    suppressible). grounding NEVER imports the concrete class: that module imports
    grounding's own (B)-path helpers (``_negated_concepts`` / ``_CITE_NEGATION_RE``), so
    the reverse import would cycle — so grounding stays PURE (no I/O, no config import)
    and just calls ``.suppresses()`` duck-typed. ``None`` (the default, every non-daemon
    caller) ⇒ byte-identical to pre-#26 output."""

    def suppresses(
        self, claim_concept: set[str], cite_neg_concepts: list[set[str]],
    ) -> bool: ...


# H2 — reason → inline literal dispatch for GroundingResult.flags_for. A flag's
# ``reason`` selects the inline ⚠. Grounding's mechanical reasons all map to
# GROUNDING_UNVERIFIED (the default); the #48 ``inferred_diagnosis`` reason maps
# to the distinct INFERRED_DIAGNOSIS literal; the P4-2 speaker-attribution reasons
# each map to their own distinct literal (the per-claim three + the note-level
# banner). A reason absent from this map falls back to GROUNDING_UNVERIFIED (safe
# default — a flag never renders unflagged).
#
# The reason STRINGS are duplicated as constants in the modules that MINT the
# flags (inferred_dx.INFERRED_DIAGNOSIS_REASON, speaker_attribution.*_REASON) —
# grounding cannot import them (those modules import grounding, so importing back
# would cycle), so the strings live here as literals and a test pins the two
# sides in lockstep. ``attribution_unverified`` is the NOTE-LEVEL banner reason:
# it is mapped here too so ``flags_for("note", -1)`` renders it via the SAME
# single dispatch as every per-claim flag.
#
# #14c — the ``quality_*`` NOTE-LEVEL advisory reasons are registered here too (PURELY ADDITIVE to the
# render-dispatch dict — grounding's DETECTION logic (verify / the #24/#26 negation checks) is
# byte-UNCHANGED; this dict only maps a reason → its inline ⚠ for render). They render at the top
# banner via the SAME ``flags_for("note", -1)`` path.
_REASON_INLINE_LITERAL: dict[str, str] = {
    "inferred_diagnosis": INFERRED_DIAGNOSIS,
    "speaker_mismatch": SPEAKER_MISMATCH,
    "speaker_unverified": SPEAKER_UNVERIFIED,
    "collateral_attribution": COLLATERAL_ATTRIBUTION,
    "attribution_unverified": ATTRIBUTION_UNVERIFIED,
    "quality_required_section_empty": QUALITY_REQUIRED_SECTION_EMPTY,
    "quality_verbose": QUALITY_VERBOSE,
    "quality_assessment_no_plan": QUALITY_ASSESSMENT_NO_PLAN,
}


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

# EXTENDED negation lexicon — the base clinical set PLUS English CONTRACTED
# negations ("haven't", "wouldn't", …). Used ONLY by the cite-side negated-CONCEPT
# grounding in verify() (the (B) precision path), NEVER by _negation_set / the
# (C) flip / inferred_dx. DELIBERATELY separate from the shared _NEGATION_RE:
# inferred_dx.py reuses _NEGATION_RE for hedge detection and the _negation_set
# membership pins assert its exact set — folding contractions into the shared
# regex would couple this precision fix to those and risk churn. Isolation keeps
# the blast radius to grounding. The contraction set is kept reasonably COMPLETE
# so a stray contraction can't reopen the lexicon-gap FP class this redesign
# closes. The ``['’]t`` is REQUIRED (never optional) — a bare "can"/"won"/"don"
# is the modal/verb/name, NOT a negation; only the contracted form negates. The
# apostrophe class ['’] matches straight AND curly quotes (transcripts carry
# either).
_CITE_NEGATION_RE = re.compile(
    r"\b(?:"
    r"no|not|denies|denied|deny|without|negative|none|never|absent|"
    r"neither|nor|lacks?|cannot"
    r"|(?:haven|hasn|hadn|wouldn|couldn|shouldn|didn|doesn|don|isn|aren"
    r"|wasn|weren|won|can)['’]t"
    r")\b",
    re.IGNORECASE,
)

# Leading quantifiers/articles stripped off a negated finding-phrase before the
# targeted-flip check, so "denies any chest pain" → phrase "chest pain".
_FLIP_STOPWORDS = frozenset({"any", "a", "an", "the", "his", "her", "their", "of"})

# Function-word drop-set for negated-CONCEPT extraction (_negated_concepts). The
# concept filter drops these by MEMBERSHIP, NOT by length — a length<3 drop
# silently swallowed 2-char clinical abbreviations (MI/PE/CP/GI/GU/RR/BP/AS…),
# so "No MI" extracted to an EMPTY concept and NEVER flagged (a fabricated
# pertinent-negative slipping the safety net — grounding-precision review BLOCK-1).
# Membership-drop keeps "mi"/"pe" as real concepts while still killing the
# preposition/article FPs (a length>=2 floor alone would let "pain IN the back"
# carry "in" and false-flag a faithful "no back pain" paraphrase). ONLY
# unambiguous function words go here — DELIBERATELY EXCLUDES tokens that collide
# with clinical abbreviations ("as" = aortic stenosis, "am"/"pm") so a real negated
# finding is never dropped. NB "or" IS kept in the coordinators set below (dropped):
# "OR" = operating-room as a negated finding is vanishingly rare AND " or " is already
# a finding-phrase boundary, so dropping the coordinator can't lose a real concept.
# Includes the negation markers themselves
# so a run-on "no fever no cough" doesn't carry "no" into the concept.
_CONCEPT_STOPWORDS = frozenset({
    # articles / determiners / quantifiers
    "a", "an", "the", "any", "some", "this", "that", "these", "those",
    "all", "each", "every", "both", "no",
    # prepositions / particles
    "of", "on", "in", "by", "to", "at", "up", "off", "out", "for", "per",
    "from", "into", "onto", "over", "under", "with", "within", "about",
    # coordinators
    "and", "or", "nor", "but",
    # negation markers (keep concepts free of the marker words)
    "not", "never", "none", "neither", "denies", "denied", "deny",
    "negative", "absent", "lacks", "lack", "without", "cannot",
})


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

    def flags_for(self, section: str, index: int) -> list[str]:
        """Return ALL distinct inline flag literals for the claim at
        ``(section, index)`` — deduped, insertion-ordered — or ``[]`` if it is
        clean. DISPATCHES each matched flag's ``reason`` (H2): grounding's
        mechanical reasons → GROUNDING_UNVERIFIED; ``inferred_diagnosis`` →
        INFERRED_DIAGNOSIS; the P4-2 speaker reasons → their own literals. The
        single source of truth ``render_soap`` reads — no hidden mutation of the
        claim objects, and render CANNOT run without a GroundingResult.

        P4-2 rename of the former single-literal first-match-wins ``flag_for``:
        a claim can now carry flags from THREE independent passes (grounding +
        #48 inferred-dx + P4-2 speaker), and ALL of them must render inline —
        first-match-wins would silently hide the speaker ⚠ behind a co-located
        grounding ⚠. Dedup is by LITERAL (two grounding reasons that both map to
        GROUNDING_UNVERIFIED render one ⚠); the note-level banner is fetched with
        ``flags_for("note", -1)`` (its section-less identity, P4-2 convention)."""
        literals: list[str] = []
        for f in self.flags:
            if f.section == section and f.claim_index == index:
                lit = _REASON_INLINE_LITERAL.get(f.reason, GROUNDING_UNVERIFIED)
                if lit not in literals:
                    literals.append(lit)
        return literals


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


def _negated_concepts(text: str, neg_re: re.Pattern) -> list[set[str]]:
    """For each negation marker in ``text`` (matched by ``neg_re``), the SET of
    SALIENT content words in the phrase it governs — the run after the marker up
    to the next punctuation / coordinator, with FUNCTION words dropped by
    :data:`_CONCEPT_STOPWORDS` membership (NOT by length — a length<3 drop swallowed
    2-char clinical abbreviations like "MI"/"PE", the BLOCK-1 false-negative).

    Powers the negated-CONCEPT grounding in :func:`verify` (the (B) precision
    path): a claim negation is grounded iff its concept word-set is a SUBSET of
    some cite negated concept — matching the negated MEANING regardless of the
    negation's surface form ("no neck swelling" ⊆ "haven't noticed any neck
    swelling"). Requiring the FULL word-set (not a single incidental word) keeps
    it conservative — a shared drug name alone does NOT ground a differently
    negated concept. Returns only NON-EMPTY concepts: a contentless negation (all
    function words) has no groundable finding, so it is not checked.

    Callers pass ONE segment's text at a time (never the space-joined multi-span
    cite) so a negation in one span cannot absorb the next span's words across the
    join — see :func:`verify` (BLOCK-2)."""
    concepts: list[set[str]] = []
    low = text.lower()
    for m in neg_re.finditer(low):
        tail = low[m.end():]
        raw = re.split(r"[.,;:]| and | or | nor | but | with ", tail, maxsplit=1)[0]
        words = {
            tok
            for w in raw.split()
            if (tok := w.strip(".,;:!?()[]'\"’")) and len(tok) >= 2
            and tok not in _CONCEPT_STOPWORDS
        }
        if words:
            concepts.append(words)
    return concepts


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


def verify(
    structured: StructuredNote, transcript: Transcript, *,
    suppression: "NegationSuppressionStore | None" = None,
) -> GroundingResult:
    """Deterministically verify grounding. Returns the auditable
    :class:`GroundingResult` (no mutation of the claim objects — ``render_soap``
    reads flags via ``GroundingResult.flags_for``, so a note can never be
    rendered without the grounding result).

    ``suppression`` (#26 Phase-2 FEED-BACK, keyword-only, default ``None``) is the
    operator-approved learned-suppression store, loaded + threaded by the PIPELINE.
    ``None`` or an empty store ⇒ output byte-identical to pre-#26: no negation flag is
    ever ADDED or removed. A populated store can ONLY turn a residual (B) invented-
    negation flag into CLEAN (never touches (C) flips, never adds a flag) — the
    conservative, medico-legal-safe direction. grounding stays PURE — it consults the
    store via the duck-typed :class:`NegationSuppressionStore` protocol, doing no I/O."""
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
    suppressed_count = 0   # #26 — residual (B) negations turned CLEAN by an approved pair

    for section, idx, claim in structured.all_claims():
        reasons: list[str] = []

        # (1) ungrounded assertion — no citation at all.
        if not claim.source_spans:
            reasons.append(f"{UNGROUNDED_ASSERTION_REASON}: claim cites no source segment")
        else:
            # (2) every cited span must be a real segment id.
            bad = [s for s in claim.source_spans if s not in seg_by_id]
            if bad:
                reasons.append(
                    f"{UNGROUNDED_SPAN_REASON}: cited segment(s) {bad} not in transcript"
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
                f"{NUMBER_MISMATCH_REASON}: {missing_nums} not verbatim in cited segment(s)"
            )

        # (4) NEGATION check — negated-CONCEPT grounding (B) + targeted FLIP (C).
        #
        # (B) is the P2-f PRECISION redesign, replacing the P2-e marker
        # SET-DIFFERENCE (claim_negs - cited_negs). Set-difference compared
        # negation MARKER TOKENS by lexical identity, so a faithful paraphrase
        # realizing the SAME pertinent-negative with a DIFFERENT negation surface
        # false-flagged as "invented": "no neck swelling" vs "haven't noticed any
        # neck swelling", "denies a plan" vs "wouldn't do anything" ("haven't"/
        # "wouldn't" aren't even in the base lexicon → cited_negs empty). That FP
        # class is the medico-legal detector "crying wolf."
        #
        # Now: for each CONCEPT the CLAIM negates (its salient word-set), the
        # negation is GROUNDED iff the CITE negates the SAME concept — the claim
        # word-set is a SUBSET of some cite negated concept — in ANY surface form
        # (different marker OK; contraction-aware via _CITE_NEGATION_RE). A claim
        # negation whose concept is negated NOWHERE in the cite still FLAGS
        # (invented, or the cite asserts it positively — e.g. "Denies chest pain"
        # vs "reports chest pain": no cite negation → flagged). CONSERVATIVE by
        # design: requiring the FULL word-set (not a single incidental overlap —
        # a shared drug name) keeps a lexically-DISJOINT paraphrase FLAGGED ("not
        # adequately controlled" vs "haven't come down as hoped" — only "metformin"
        # overlaps); that residual class's home is the #26 learned-suppression
        # loop, NOT a looser threshold (loosening would drop genuine invented
        # negations — the false-NEGATIVE that matters most here).
        #
        # (C) FLIPPED — the cited segment NEGATES a finding the claim asserts
        # POSITIVELY (targeted phrase check) — is UNCHANGED. The dropped-negation-
        # in-a-bundled-claim case stays delegated to the atomic-claim prompt +
        # human attest (the A/B corpus found ZERO across 189 real claims).
        # BLOCK-2: extract cite negated concepts PER-SPAN (not from the
        # space-joined `cited`). _cited_text joins spans with a bare " " and
        # negation governance only breaks on punctuation/coordinators, so a span
        # lacking trailing punctuation would RUN ON — a negation in span N would
        # absorb span N+1's words ("No fever" + "reports chest pain" → one concept
        # {fever, reports, chest, pain} ⊇ {chest, pain}) and SUPPRESS a genuine
        # flip the cite states positively. Per-span keeps each negation's
        # governance inside its own segment.
        claim_neg_concepts = _negated_concepts(claim.claim, _CITE_NEGATION_RE)
        cite_neg_concepts: list[set[str]] = []
        for span_id in claim.source_spans:
            seg = seg_by_id.get(span_id)
            if seg is not None:
                cite_neg_concepts.extend(
                    _negated_concepts(seg.text, _CITE_NEGATION_RE)
                )
        ungrounded_negs = [
            c for c in claim_neg_concepts
            if not any(c <= span for span in cite_neg_concepts)
        ]
        # #26 Phase-2 FEED-BACK — consult the operator-approved suppression store as an
        # ADDITIONAL suppression source on the (B) residual ONLY (NEVER the (C) flip below —
        # a positive/negative flip is a real contradiction, never suppressible). A residual
        # ungrounded negation is dropped IFF an approved pair EXACT-set-matches BOTH its claim
        # concept AND a present cite negated concept. suppression is None (default — every
        # non-daemon caller) OR an empty store ⇒ this block is a no-op, ungrounded_negs is
        # unchanged, suppressed_count stays 0 (byte-identical). PURE: only a duck-typed call,
        # no I/O. Placed AFTER the (B) subset check so an already-grounded negation never
        # reaches the store (a suppression can only clear a would-be FLAG, never add one).
        if suppression is not None and ungrounded_negs:
            kept = [
                c for c in ungrounded_negs
                if not suppression.suppresses(c, cite_neg_concepts)
            ]
            suppressed_count += len(ungrounded_negs) - len(kept)
            ungrounded_negs = kept
        flipped = _flipped_positive_findings(claim.claim, cited)
        if ungrounded_negs or flipped:
            detail: list[str] = []
            if ungrounded_negs:
                pretty = [" ".join(sorted(c)) for c in ungrounded_negs]
                detail.append(
                    f"invented negation(s) {pretty} not negated in cited segment(s)"
                )
            if flipped:
                detail.append(
                    f"claim asserts positively a finding the cited segment "
                    f"negates: {flipped}"
                )
            reasons.append(f"{NEGATION_MISMATCH_REASON}: " + "; ".join(detail))

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
        # #26 — count of residual (B) negations an approved pair turned CLEAN this note.
        # 0 = inert (None/empty store, the Phase-2 default) — every note logs it so an
        # operator can grep whether the learned feed-back fired (intentionally-left-blank).
        suppressed=suppressed_count,
    )
    return result
