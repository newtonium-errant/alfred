"""Deterministic grounding-verify (scribe P2-c) — CODE, not an LLM.

The anti-hallucination gate. Over the parsed :class:`~alfred.scribe.notegen.
StructuredNote`, for EVERY claim it deterministically checks that the claim is
grounded in the transcript segment(s) it cites — catching the small-model
errors an extract-not-infer prompt cannot fully prevent (500mg→5mg dose flip,
denies→reports negation flip, fabricated / uncited assertions).

Checks per claim (each failure ⇒ a flag):
  1. NO source_spans                 → ``ungrounded_assertion``.
  2. a source_span is not a real     → ``ungrounded_span``.
     segment id in the transcript
  3. a NUMBER/DOSE token in the      → ``number_mismatch``   (FORWARD: a
     claim not present verbatim in       note-introduced/changed number is a
     the cited segment(s)                 fabrication; an OMITTED source number
                                          is a legitimate summary, not flagged)
  4. the set of NEGATION tokens in   → ``negation_mismatch`` (BIDIRECTIONAL:
     the claim ≠ the set in the           BOTH a fabricated negation AND a
     cited segment(s)                      DROPPED one — the denies→reports flip
                                           — are dangerous; over-flagging is
                                           safe [flag-in-note, clinician
                                           confirms], under-flagging a dropped
                                           negation is a patient-safety error)

FAILURE POLICY = FLAG-IN-NOTE (the draft still proceeds — it is an ai_draft and
the clinician attest is the human gate):
  * each flagged claim's ``grounding_flag`` is set to the frozen literal
    ``notegen.GROUNDING_UNVERIFIED`` (rendered UNMISSABLE inline in the body);
  * every flag is recorded in :attr:`GroundingResult.metadata` — the
    ``grounding_flags`` frontmatter list — so ATTESTING a flagged draft is
    AUDITABLE (the clinician sees exactly which claims + why).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from alfred.scribe.notegen import GROUNDING_UNVERIFIED, SOAP_SECTIONS, StructuredNote
from alfred.scribe.transcript import Transcript

log = structlog.get_logger(__name__)

# Number+unit (dose / vital / measurement). Word-final unit boundary.
_UNIT = (
    r"(?:mg|mcg|g|kg|ml|l|units?|iu|%|mmhg|bpm|cm|mm|/min|/day|/week|/hr|c|f)"
)
_NUMBER_UNIT_RE = re.compile(rf"\d+(?:\.\d+)?\s*{_UNIT}\b", re.IGNORECASE)
_BARE_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
# Negation tokens (clinical). Word-bounded.
_NEGATION_RE = re.compile(
    r"\b(no|not|non|denies|denied|deny|without|negative|absent|none|never|"
    r"neither|nor|lacks?|free)\b",
    re.IGNORECASE,
)


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
    """Word-bounded presence — ``5mg`` must not match ``500mg``; ``12`` must not
    match ``120``."""
    return re.search(r"\b" + re.escape(tok) + r"\b", norm_cited) is not None


def _negation_set(text: str) -> set[str]:
    return {m.group(1).lower() for m in _NEGATION_RE.finditer(text)}


def _cited_text(claim_spans, seg_by_id) -> str:
    return " ".join(seg_by_id[s].text for s in claim_spans if s in seg_by_id)


def verify(structured: StructuredNote, transcript: Transcript) -> GroundingResult:
    """Deterministically verify grounding. MUTATES each flagged claim's
    ``grounding_flag`` (for inline render) and returns the auditable result."""
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

        # (4) BIDIRECTIONAL negation check — the negation set must match.
        claim_negs = _negation_set(claim.claim)
        cited_negs = _negation_set(cited)
        if claim_negs != cited_negs:
            reasons.append(
                f"negation_mismatch: claim negations {sorted(claim_negs)} != "
                f"cited {sorted(cited_negs)}"
            )

        if reasons:
            claim.grounding_flag = GROUNDING_UNVERIFIED
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
