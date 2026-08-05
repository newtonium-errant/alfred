"""Learned STT vocabulary — capture, propose, approve (#54 half 2).

The self-correcting loop for speech recognition, closing onto machinery that
already exists rather than inventing a parallel one.

## The loop

1. **Capture.** A voice-sourced chat message carries both the transcript as
   INSERTED and the text as SENT. If the operator edited it, the difference is a
   correction — free supervision nobody had to be asked for.
2. **Propose.** Recurring TERM-level corrections surface at morning review with
   counts ("'front run' corrected 3x — add to vocabulary?").
3. **Approve.** On the operator's yes, the term is recorded in the decided store
   and from then on it is UNIONED onto the static ``talker.stt.vocab_terms`` by
   :func:`effective_vocab_terms` — the one accessor every consumer of vocabulary
   goes through. It reaches Whisper ``prompt=`` and Deepgram ``keywords`` across
   the whole fallback chain (``stt_backends``), the live stream, and the shadow
   capture, because all of them read the seam rather than the raw config field.

The biasing MECHANISM needed nothing built: it existed, threaded, fed from a
static list that simply never learned. What it needed was a second source and a
single place to merge the two — hence the seam. Approved terms are deliberately
NOT appended into ``vocab_terms`` itself; see the decided-store section for the
two reasons that design cannot hold (the cap stops binding, and a rejection has
nowhere to live).

## Never silent auto-mutation

Approval is a human step by construction: nothing here writes ``vocab_terms``
without an explicit approved-term list handed to :func:`apply_approved_terms`.
That is the platform's self-correcting standard — learn, propose,
operator-approves — and it is also the honest posture for a mechanism that
biases every future transcription.

## Why full pairs are stored but only terms are proposed

The corpus keeps the whole (transcript, sent) pair: it is the audit trail, and a
later pass may want context this extraction throws away. What SURFACES for
approval is the extracted term, because "add 'front run' to your vocabulary" is
a decision the operator can make in one second and "here are two paragraphs that
differ" is not.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .utils import get_logger

log = get_logger(__name__)

#: A term must be corrected this many times before it is proposed.
#:
#: OPERATOR RULING 2026-08-05: **two**. One correction is a typo or a one-off;
#: the second is the operator repeating himself, and that repetition is the
#: signal.
#:
#: The bar is deliberately low because it gates a PROPOSAL, not a mutation.
#: Approval is a human step, so surfacing one term too eagerly costs a single
#: "no" at morning review, while setting it high costs the opposite and worse:
#: the terms he corrects most often never surface at all. (An earlier revision
#: of this module set 3 and described it as operator-ratified. It never was.)
MIN_CORRECTION_COUNT = 2

#: Cap on LEARNED additions, on top of the shipped static list.
#:
#: Operator-ratified default (2026-08-05). The real ceiling is the Whisper
#: prompt window (~224 tokens) — see ``stt_backends._WHISPER_PROMPT_CHAR_BUDGET``,
#: which enforces it at the join for every path. This cap is the earlier, softer
#: guard: it keeps the proposal stream curated so the operator is never the last
#: line of defence against an over-stuffed prompt. Measured 2026-08-05: the 28
#: static terms use ~75 of ~224 tokens, so ~20 learned additions sit
#: comfortably inside the window with room to spare.
MAX_LEARNED_TERMS = 20

#: Longest phrase (in words) worth proposing. A correction spanning more than
#: this is a rewrite of the sentence, not a vocabulary term — biasing on it
#: would teach the model a whole clause.
MAX_TERM_WORDS = 4

#: How alike a WHOLE replace span must be to count as a mis-hearing.
#:
#: A mis-hearing is orthographically close to the truth ("tracker"/"tractor");
#: an unrelated rewrite is not. MEASURED 2026-08-05 over the operator's own
#: vocabulary: real mis-hearings land 0.50-0.94 ("wrong"/"run" 0.50,
#: "cally"/"KAL-LE" 0.55, "tracker"/"tractor" 0.71, "frontrun"/"front run"
#: 0.94), unrelated rewrites land 0.31-0.35. 0.5 sits at the bottom of the real
#: band, which is the right side to err on: this gate only decides whether a
#: correction is CAPTURED, and a term still needs MIN_CORRECTION_COUNT
#: repetitions plus the operator's yes before it biases anything.
MIN_SPAN_SIMILARITY = 0.5

#: How alike a RESCUED fragment must be (see :func:`_rescue_correction`).
#:
#: Strictly higher than :data:`MIN_SPAN_SIMILARITY`, and the gap is the whole
#: point. A single floor provably cannot do this job: "wrong"->"run" (a real
#: mis-hearing) and "two"->"words" (plucked out of an unrelated rewrite) both
#: score exactly 0.50. Nothing separates them numerically — they differ
#: STRUCTURALLY. "wrong"->"run" is the entire span, so the operator changed
#: exactly that word; "two"->"words" is a fragment of a span whose whole scored
#: 0.31. So a whole span is trusted at the lower bar, while a fragment pulled
#: out of a loose span has to be clearly tighter to earn its place.
MIN_RESCUED_SIMILARITY = 0.7

_WORD_RE = re.compile(r"[\w'’\-]+", re.UNICODE)


@dataclass
class CorrectionPair:
    """One voice message the operator edited before sending."""

    transcript: str
    sent: str
    instance: str = ""
    at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class VocabProposal:
    """A recurring correction, ready for the operator's yes/no."""

    term: str          #: what the operator MEANT — the candidate vocabulary term
    heard: list[str]   #: the distinct things the STT produced instead
    count: int         #: how many times it was corrected


def append_correction_pair(corpus_path: str | Path, pair: CorrectionPair) -> None:
    """Append one pair to the append-only JSONL corpus.

    Same shape and same guarantees as ``daily_sync.corpus.append_correction`` —
    one append per operator action, no concurrent writers, parent auto-created.
    """
    path = Path(corpus_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(pair), ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def iter_correction_pairs(corpus_path: str | Path):
    """Yield stored pairs, oldest first. Unparseable rows are skipped, loudly.

    A corrupt row must not blind the whole loop, but it must not vanish either —
    silently dropping corrections would make the counts quietly wrong, and the
    counts are what the operator's decision rests on.
    """
    path = Path(corpus_path)
    if not path.exists():
        return
    skipped = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
            yield CorrectionPair(
                transcript=str(data.get("transcript") or ""),
                sent=str(data.get("sent") or ""),
                instance=str(data.get("instance") or ""),
                at=str(data.get("at") or ""),
            )
        except (ValueError, TypeError):
            skipped += 1
    if skipped:
        log.warning(
            "stt.vocab_corpus.rows_skipped",
            path=str(path), skipped=skipped,
            detail="unparseable corpus rows ignored — correction COUNTS are "
                   "understated by this many",
        )


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text or "")


def _similarity(a: str, b: str) -> float:
    """Character-level closeness of two phrases, case-insensitive."""
    return difflib.SequenceMatcher(None, a.casefold(), b.casefold()).ratio()


def _rescue_correction(
    heard_words: list[str], meant_words: list[str],
) -> tuple[str, str] | None:
    """Pull the real correction out of a span that swallowed other edits.

    THE PROBLEM THIS SOLVES. ``difflib`` coalesces a changed region into ONE
    ``replace`` span — it never emits an adjacent insert+delete pair (verified
    2026-08-05). That is usually what we want, but it means an edit that fixes a
    term AND touches anything beside it arrives as a single span:

        "clean the chicken tracker"
        "clean the chicken tractor today please"
            -> span ("tracker", "tractor today please")

    Taken whole that span is poison, because the ``meant`` side is BOTH the
    count key and the proposed vocabulary term: the real term never accumulates
    a count (every variant is its own key) and what reaches the operator for
    approval is a phrase that then biases every future transcription. Editing a
    word and adding an afterthought in one pass is an ordinary thing to do, so
    this is the common case, not a corner.

    Dropping such spans would be safe but wasteful — it throws away a real
    correction. Instead, search the contiguous sub-spans for the tightest pair
    and accept it only if it clears :data:`MIN_RESCUED_SIMILARITY`, recovering
    ("tracker", "tractor") from the example above.

    Ties break toward the LONGER pair so a genuinely multi-word term is not
    shaved down to one word ("frontrun" -> "front run" must stay whole).
    Returns ``None`` when nothing inside the span is close enough, which is how
    an unrelated short rewrite is refused rather than mined for a spurious term.
    """
    best: tuple[tuple[float, int], tuple[str, str]] | None = None
    for i in range(len(heard_words)):
        for i2 in range(i + 1, min(len(heard_words), i + MAX_TERM_WORDS) + 1):
            for j in range(len(meant_words)):
                for j2 in range(j + 1, min(len(meant_words), j + MAX_TERM_WORDS) + 1):
                    if (i2 - i) == len(heard_words) and (j2 - j) == len(meant_words):
                        continue  # the whole span — already judged by the caller
                    heard = " ".join(heard_words[i:i2])
                    meant = " ".join(meant_words[j:j2])
                    if not heard or not meant:
                        continue
                    if heard.casefold() == meant.casefold():
                        continue
                    score = _similarity(heard, meant)
                    if score < MIN_RESCUED_SIMILARITY:
                        continue
                    key = (round(score, 4), (i2 - i) + (j2 - j))
                    if best is None or key > best[0]:
                        best = (key, (heard, meant))
    return best[1] if best else None


def extract_term_corrections(transcript: str, sent: str) -> list[tuple[str, str]]:
    """``(heard, meant)`` phrase pairs where the operator changed the words.

    Thin wrapper over :func:`extract_term_corrections_with_stats` — see there for
    the full contract. This is the shape almost every caller wants; the stats
    variant exists only for the batch pass, which reports how often the gate
    fired across a whole corpus.
    """
    corrections, _rejected = extract_term_corrections_with_stats(transcript, sent)
    return corrections


def extract_term_corrections_with_stats(
    transcript: str, sent: str,
) -> tuple[list[tuple[str, str]], int]:
    """``(corrections, gate_rejected_span_count)``.

    Identical extraction to :func:`extract_term_corrections`, additionally
    reporting how many ``replace`` spans the similarity gate turned down. The
    per-span DEBUG line answers "why was THIS one refused"; the count answers
    "how often is the gate firing at all", which is a batch-level question and
    so cannot be answered from inside the per-pair loop.

    A separate function rather than a widened return, so the existing callers
    (:func:`propose_vocab_additions` aside, the web capture site) keep their
    signature — a count nobody asked for should not become everybody's problem.

    Word-level diff via :mod:`difflib`, collecting only ``replace`` spans —
    the shape of "the STT heard X, I meant Y". Pure insertions and deletions are
    ignored on purpose: text the operator ADDED was never mis-heard (it is new
    thought, not a correction), and text they DELETED tells us nothing about what
    should have been recognised instead.

    Spans longer than :data:`MAX_TERM_WORDS` are dropped — a five-word
    replacement is the operator rewriting a sentence, and biasing the model
    toward a whole clause is not vocabulary.

    Case is preserved in the output (the vocabulary wants "KAL-LE", not
    "kal-le"); comparison is case-insensitive so a capitalisation-only edit is
    not mistaken for a mis-hearing.

    A span is only a mis-hearing if the two sides actually LOOK alike
    (:data:`MIN_SPAN_SIMILARITY`). A span that does not clear that bar gets one
    second chance through :func:`_rescue_correction`, because ``difflib``
    coalesces a correction with any edit beside it — see that function for why
    the whole-span and rescued-fragment bars deliberately differ.

    **THE GATE FILTERS EXTRACTION, NEVER THE CORPUS.** The full (transcript,
    sent) pair is stored unconditionally at capture time; only the mining of
    TERMS from it is gated here. That separation is deliberate and it is what
    makes these thresholds safe to hold loosely: every span this function
    refuses is still on disk, so a better extractor run later recovers what a
    heuristic dropped today. Nothing is ever permanently lost to a judgment call
    — which is also the precondition for tuning the gate itself, per the
    self-correcting standard.

    A refused span therefore logs at DEBUG rather than louder: it is a
    diagnostic for "is the gate too tight?", not a data-loss warning, because
    the data is not lost. Lengths and the score are recorded, never the text —
    these are the operator's own words.
    """
    heard_words = _words(transcript)
    sent_words = _words(sent)
    if not heard_words or not sent_words:
        return [], 0

    matcher = difflib.SequenceMatcher(
        a=[w.casefold() for w in heard_words],
        b=[w.casefold() for w in sent_words],
    )
    out: list[tuple[str, str]] = []
    gate_rejected = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "replace":
            continue
        if (i2 - i1) > MAX_TERM_WORDS or (j2 - j1) > MAX_TERM_WORDS:
            continue
        span_heard = heard_words[i1:i2]
        span_meant = sent_words[j1:j2]
        heard = " ".join(span_heard)
        meant = " ".join(span_meant)
        if not heard or not meant or heard.casefold() == meant.casefold():
            continue
        score = _similarity(heard, meant)
        if score >= MIN_SPAN_SIMILARITY:
            out.append((heard, meant))
            continue
        rescued = _rescue_correction(span_heard, span_meant)
        if rescued is not None:
            out.append(rescued)
            continue
        # ILB on the drop. The gate is a judgment, so how often it fires has to
        # be answerable — "the loop stopped proposing" and "the gate got too
        # tight" must not look the same. DEBUG because the pair itself is
        # retained in the corpus (see the docstring), so this is a tuning
        # signal, not a loss warning. Lengths and the score only: the spans are
        # the operator's own words and do not belong in a log line.
        #
        # Counted as well as logged: per-span at DEBUG answers "why was THIS one
        # refused", but an operator asking "is the gate eating my corrections?"
        # needs ONE number per pass, and reading it off DEBUG lines means
        # turning on the noisiest level to count something. The aggregate rides
        # the batch summary at INFO (``propose_vocab_additions``).
        gate_rejected += 1
        log.debug(
            "stt.vocab_span_rejected",
            heard_words=len(span_heard),
            meant_words=len(span_meant),
            heard_len=len(heard),
            meant_len=len(meant),
            score=round(score, 3),
            span_floor=MIN_SPAN_SIMILARITY,
            rescue_floor=MIN_RESCUED_SIMILARITY,
            detail="replace span too dissimilar to be a mis-hearing, and no "
                   "tighter fragment inside it — the pair is still in the "
                   "corpus and re-minable",
        )
    return out, gate_rejected


def propose_vocab_additions(
    pairs,
    *,
    current_vocab,
    decided: "LearnedVocab | None" = None,
    min_count: int = MIN_CORRECTION_COUNT,
    max_learned: int = MAX_LEARNED_TERMS,
) -> list[VocabProposal]:
    """Recurring corrections worth the operator's attention, highest count first.

    Filters, in order, and each one earns its place:

    * a term ALREADY in ``current_vocab`` is never proposed — it is already
      biasing, so a correction on it means the bias is not enough, which is a
      different problem than a missing term and must not read as this one;
    * a term the operator has already RULED ON (``decided``) is never proposed
      again — approved terms are already biasing, and a rejected one must stay
      rejected. Re-asking a question he has answered is the groundhog bug
      ``routine.match_calibration`` had to fix; the filter lives HERE rather
      than in each caller so a future surface cannot forget it;
    * a term corrected fewer than ``min_count`` times is a one-off;
    * at most ``max_learned`` proposals surface, so a long tail cannot flood the
      morning review or the prompt window.

    Counting is keyed on the MEANT term, case-folded, because that is the thing
    being added; the distinct mis-hearings are carried along as evidence for the
    operator ("heard as 'front-run', 'frunt run'").
    """
    have = {str(t).casefold().strip() for t in (current_vocab or []) if str(t).strip()}

    counts: dict[str, int] = {}
    heard_by_term: dict[str, list[str]] = {}
    display: dict[str, str] = {}

    gate_rejected = 0
    for pair in pairs:
        corrections, rejected = extract_term_corrections_with_stats(
            pair.transcript, pair.sent,
        )
        gate_rejected += rejected
        for heard, meant in corrections:
            key = meant.casefold()
            if key in have:
                continue
            counts[key] = counts.get(key, 0) + 1
            display.setdefault(key, meant)
            if heard not in heard_by_term.setdefault(key, []):
                heard_by_term[key].append(heard)

    ruled_on = 0
    proposals: list[VocabProposal] = []
    for k, n in counts.items():
        if n < min_count:
            continue
        term = display[k]
        if decided is not None and decided.is_decided(term):
            ruled_on += 1
            continue
        proposals.append(
            VocabProposal(term=term, heard=heard_by_term.get(k, []), count=n)
        )
    if ruled_on:
        # ILB: a shrinking proposal list must be explicable. "you already told me"
        # has to be distinguishable from "the extraction stopped working".
        log.info(
            "stt.vocab_proposals.already_ruled",
            suppressed=ruled_on,
            detail="recurring corrections the operator has already approved or "
                   "rejected — not re-proposed",
        )
    # Highest count first, then alphabetical so the order is stable across runs
    # (an operator re-reading yesterday's review should see the same list).
    proposals.sort(key=lambda p: (-p.count, p.term.casefold()))

    if len(proposals) > max_learned:
        log.info(
            "stt.vocab_proposals.capped",
            surfaced=max_learned, suppressed=len(proposals) - max_learned,
            detail="more recurring corrections than the learned-term cap — the "
                   "highest-count terms surface first; the rest wait",
        )
        proposals = proposals[:max_learned]

    log.info(
        "stt.vocab_proposals.computed",
        proposals=len(proposals),
        distinct_corrected_terms=len(counts),
        min_count=min_count,
        # The batch-boundary aggregate of the F4 gate. The per-span refusals log
        # at DEBUG (deliberately — the pair is retained, so each one is a tuning
        # datum, not a loss), but "is the gate eating my corrections?" is a
        # question about the PASS, and answering it should not require enabling
        # the noisiest level and counting lines. Emitted ALWAYS, including as 0:
        # "the gate refused nothing this pass" is the positive evidence that
        # makes a shrinking proposal list attributable to something else.
        gate_rejected_spans=gate_rejected,
        detail="ran, nothing to propose" if not proposals else "recurring "
               "corrections ready for morning review",
    )
    return proposals


def apply_approved_terms(
    current_vocab, approved, *, max_learned: int = MAX_LEARNED_TERMS,
) -> list[str]:
    """The vocabulary after the operator's approvals. Pure — returns a new list.

    ONLY terms explicitly handed in are added: this function is the human step,
    and nothing upstream may call it with proposals the operator has not seen.

    THE PROMPT WINDOW IS THE REAL CEILING, and this is the place that has to know
    it. Whisper's prompt is ~224 tokens and it does NOT error on overflow — it
    truncates or degrades silently, costing general transcription accuracy with no
    signal. ``stt_backends._vocab_to_whisper_prompt`` enforces that bound at the
    join for every path (static and learned), and logs when it bites. The
    ``max_learned`` cap here is the earlier, softer guard: it stops the list
    reaching the hard bound in the first place, so the operator approving one more
    term is never unknowingly the thing that pushes an existing term out of the
    window. Measured 2026-08-05: 28 static terms ≈ 75 of ~224 tokens.

    Refuses silently-lossy growth: once the learned budget is spent, further
    approvals are DROPPED and logged rather than appended past the cap. Dropping
    loudly beats a prompt that quietly stops biasing its own tail.
    """
    base = [str(t) for t in (current_vocab or []) if str(t).strip()]
    have = {t.casefold().strip() for t in base}

    added: list[str] = []
    rejected: list[str] = []
    for term in approved or []:
        clean = str(term).strip()
        if not clean or clean.casefold() in have:
            continue
        if len(added) >= max_learned:
            rejected.append(clean)
            continue
        added.append(clean)
        have.add(clean.casefold())

    if rejected:
        log.warning(
            "stt.vocab_approval.capped",
            added=len(added), dropped=len(rejected), max_learned=max_learned,
            detail="learned-vocabulary cap reached — approved terms beyond it "
                   "were NOT added; prune the list before approving more",
        )
    if added:
        log.info(
            "stt.vocab_approval.applied", added=len(added), total=len(base) + len(added),
        )
    return base + added


# ---------------------------------------------------------------------------
# The decided store — the operator's verdicts, and the ONLY source of learned
# vocabulary.
# ---------------------------------------------------------------------------
#
# WHY A SEPARATE STORE rather than appending into ``stt.vocab_terms``.
#
# The original design said approved terms append to the per-instance
# ``vocab_terms`` list. That cannot work, for two reasons found by audit:
#
# 1. THE CAP CANNOT BIND. A flat merged list cannot tell a static term from a
#    learned one, so "at most 20 LEARNED terms" is unanswerable from it. Capping
#    per-approval instead lets the ceiling drift upward forever — approve three
#    terms a morning and a cap of 20 never once binds, while the Whisper prompt
#    window (which degrades SILENTLY) fills up. Keeping learned terms separate
#    makes the cumulative count a fact the code can read.
# 2. REJECTIONS HAVE NOWHERE TO LIVE. A store of approvals alone cannot remember
#    a "no", so every declined term returns to the review the next morning,
#    forever. That is the groundhog bug ``routine.match_calibration`` already
#    had to fix (see ``filter_pending_for_review``); this store records the
#    refusals so it never has to be fixed twice.
#
# Append-only JSONL, replayed LATER-WINS per term, mirroring the match
# calibration corpus: the operator may approve a term he once rejected, or
# retire one he approved, and the last thing he said is what holds.

DECISION_APPROVE = "vocab_approve"
DECISION_REJECT = "vocab_reject"


@dataclass
class VocabDecision:
    """One operator verdict on a proposed term."""

    type: str          #: DECISION_APPROVE | DECISION_REJECT
    term: str
    operator: str = ""
    at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class LearnedVocab:
    """The replayed verdicts: what is approved, and what must not be re-asked."""

    approved: list[str] = field(default_factory=list)  #: in approval order
    rejected: set[str] = field(default_factory=set)    #: casefolded terms

    def is_decided(self, term: str) -> bool:
        """True when the operator has already ruled on this term either way.

        The proposal pass consults this so a term he approved is not offered
        again (it is already biasing) and — the part that matters — neither is
        one he declined.
        """
        key = (term or "").casefold().strip()
        return key in self.rejected or any(
            t.casefold().strip() == key for t in self.approved
        )


def append_decision(decided_path: str | Path, decision: VocabDecision) -> None:
    """Record one operator verdict (append-only, parent auto-created)."""
    path = Path(decided_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(decision), ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_decisions(decided_path: str | Path) -> LearnedVocab:
    """Replay the decided store, LATER-WINS per term.

    An approve clears a prior reject for that term and vice-versa, so the
    operator can change his mind without editing a file. Malformed rows are
    skipped loudly — a half-corrupt store must degrade to "fewer learned terms",
    never to a crash on the transcription path.
    """
    approved: list[str] = []
    rejected: set[str] = set()
    path = Path(decided_path)
    if not path.exists():
        return LearnedVocab(approved=approved, rejected=rejected)

    skipped = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("row is not a JSON object")
            row_type = str(data.get("type") or "")
            term = str(data.get("term") or "").strip()
        except (ValueError, TypeError):
            skipped += 1
            continue
        if not term:
            skipped += 1
            continue
        key = term.casefold()
        if row_type == DECISION_APPROVE:
            rejected.discard(key)
            if not any(t.casefold() == key for t in approved):
                approved.append(term)
        elif row_type == DECISION_REJECT:
            approved[:] = [t for t in approved if t.casefold() != key]
            rejected.add(key)
        # An unknown row type is ignored rather than counted as corrupt: a
        # store written by a newer build must not make an older one shout.

    if skipped:
        log.warning(
            "stt.vocab_decided.rows_skipped",
            path=str(path), skipped=skipped,
            detail="unparseable decision rows ignored — learned vocabulary may "
                   "be missing terms the operator approved",
        )
    return LearnedVocab(approved=approved, rejected=rejected)


def effective_vocab_terms(stt_config) -> list[str]:
    """The vocabulary that ACTUALLY biases transcription. **The single seam.**

    Every consumer of vocabulary — Whisper ``prompt=``, Deepgram ``keywords``,
    the live stream, the shadow capture — must call THIS, never
    ``stt_config.vocab_terms`` directly. That is the whole point of it existing:
    the union, the cumulative cap, and anything added later (provenance, expiry,
    per-backend filtering) live in one place, so a consumer written next month
    cannot silently miss the learned terms by reading the raw config field. The
    raw field is now only HALF the answer.

    Degrades to the static list when no decided store is configured, so an
    instance that has not opted in behaves exactly as before.

    The cumulative cap binds HERE, and only because the whole approved list is
    passed in at once: ``apply_approved_terms`` counts what it adds over the
    static base, so N successive approvals over N mornings still total at most
    ``MAX_LEARNED_TERMS``. Capping at each approval instead would never bind.
    """
    static = [str(t) for t in (getattr(stt_config, "vocab_terms", None) or [])]
    decided_path = str(getattr(stt_config, "vocab_decided_path", "") or "")
    if not decided_path:
        return static
    learned = load_decisions(decided_path)
    if not learned.approved:
        return static
    return apply_approved_terms(static, learned.approved)


__all__ = [
    "DECISION_APPROVE",
    "DECISION_REJECT",
    "LearnedVocab",
    "VocabDecision",
    "append_decision",
    "effective_vocab_terms",
    "load_decisions",
    "MAX_LEARNED_TERMS",
    "MAX_TERM_WORDS",
    "MIN_CORRECTION_COUNT",
    "MIN_RESCUED_SIMILARITY",
    "MIN_SPAN_SIMILARITY",
    "CorrectionPair",
    "VocabProposal",
    "append_correction_pair",
    "apply_approved_terms",
    "extract_term_corrections",
    "extract_term_corrections_with_stats",
    "iter_correction_pairs",
    "propose_vocab_additions",
]
