"""Learned STT vocabulary — capture, propose, approve (#54 half 2).

WHY THIS EXISTS. The STT mis-hears the operator's domain terms ("chicken
tractor", "front run", "front coop") and he corrects them by hand every time.
The bias mechanism to stop that already existed — Whisper ``prompt=`` and
Deepgram ``keywords``, fed from ``talker.stt.vocab_terms`` — but it only ever
carried a hand-maintained static list, so the corrections taught it nothing.

These pins use the operator's OWN vocabulary as fixtures, because the loop has
to work on the terms that prompted it, not on synthetic ones.

The guardrail: approval is a HUMAN step. Nothing here may write the vocabulary
without an explicit approved list, and the cap must hold even against an
operator who says yes to everything — the prompt window degrades silently, so
the code is the only thing that can notice.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from alfred.telegram.stt_vocab_learning import (
    DECISION_APPROVE,
    DECISION_REJECT,
    MAX_LEARNED_TERMS,
    MAX_RESCUE_SPAN_WORDS,
    MAX_TERM_WORDS,
    MIN_CORRECTION_COUNT,
    CorrectionPair,
    VocabDecision,
    append_correction_pair,
    append_decision,
    apply_approved_terms,
    effective_vocab_terms,
    extract_term_corrections,
    extract_term_corrections_with_stats,
    iter_correction_pairs,
    load_decisions,
    propose_vocab_additions,
)


def _pair(transcript: str, sent: str) -> CorrectionPair:
    return CorrectionPair(transcript=transcript, sent=sent, instance="salem")


# ---------------------------------------------------------------------------
# extract_term_corrections — the (heard, meant) diff
# ---------------------------------------------------------------------------


def test_extracts_the_operators_own_mishearing() -> None:
    got = extract_term_corrections(
        "the chicken tracker needs cleaning", "the chicken tractor needs cleaning",
    )
    assert got == [("tracker", "tractor")]


def test_extracts_a_multiword_term() -> None:
    got = extract_term_corrections("check the front rung", "check the front run")
    assert ("rung", "run") in got


# MEASURED NOTE on the two pins below (2026-08-05), so their mutation behaviour
# is not mistaken for hollowness. Insertions/deletions are excluded TWICE over:
# by the ``tag != "replace"`` filter, and independently by the ``heard and meant``
# truthiness check (an insert opcode yields an empty ``heard``). Removing EITHER
# guard alone leaves these green; removing BOTH reds them. That is redundant
# protection working as intended, not a pin that fails to bind — but a reviewer
# mutating one guard and seeing green should know why before drawing a conclusion.
def test_pure_additions_are_NOT_corrections(  ) -> None:
    """Text the operator ADDED was never mis-heard — it is new thought. Counting
    it would fill the vocabulary with words the STT got right."""
    assert extract_term_corrections("clean the coop", "clean the coop today please") == []


def test_pure_deletions_are_NOT_corrections() -> None:
    """A deletion says nothing about what SHOULD have been recognised."""
    assert extract_term_corrections("clean the coop today", "clean the coop") == []


def test_a_capitalisation_only_edit_is_not_a_mishearing() -> None:
    """Comparison is case-insensitive: fixing 'kal-le' to 'KAL-LE' is not the
    STT hearing the wrong word, and proposing it would waste a slot."""
    assert extract_term_corrections("ask kal-le about it", "ask KAL-LE about it") == []


def test_a_sentence_rewrite_is_not_a_vocabulary_term() -> None:
    """A long replacement is the operator rewriting, not a term. Biasing on it
    would teach the model a whole clause."""
    got = extract_term_corrections(
        "one two three four five six seven",
        "completely different words entirely replacing that whole thing here",
    )
    assert got == []


def test_an_unchanged_message_yields_nothing() -> None:
    assert extract_term_corrections("clean the coop", "clean the coop") == []
    assert extract_term_corrections("", "") == []


# ---------------------------------------------------------------------------
# The swallowed-span defect. `difflib` coalesces a changed region into ONE
# `replace` span and never emits an adjacent insert+delete (measured
# 2026-08-05), so an edit that fixes a term AND touches anything beside it
# arrives as a single span. Taken whole, that span poisons the loop: `meant` is
# both the count key and the proposed vocabulary term, so the real term never
# accumulates a count and a junk phrase is what reaches the operator.
#
# These shapes passed the original 23 pins because the only addition pin covers
# additions with NO correction present (a pure `insert` opcode, which never
# reaches the similarity logic at all).
# ---------------------------------------------------------------------------


def test_a_correction_plus_an_ADDITION_yields_only_the_term() -> None:
    """Fixing a word and adding an afterthought in one pass is ordinary. The
    proposal must be "tractor", never "tractor today please"."""
    assert extract_term_corrections(
        "clean the chicken tracker", "clean the chicken tractor today please",
    ) == [("tracker", "tractor")]


def test_a_correction_plus_a_DELETION_yields_only_the_term() -> None:
    assert extract_term_corrections(
        "clean the chicken tracker today please", "clean the chicken tractor",
    ) == [("tracker", "tractor")]


def test_a_correction_at_the_END_of_the_message_yields_only_the_term() -> None:
    """No trailing matched word to close the span, so the addition rides along
    with the correction — the same defect without a middle."""
    assert extract_term_corrections("front rung", "front run today") == [
        ("rung", "run"),
    ]


def test_a_genuinely_multiword_term_is_NOT_shaved_to_one_word() -> None:
    """The guard against over-correcting the fix: the operator's real terms are
    two words ("chicken tractor", "front coop"), and a mis-hearing that JOINS
    them must still propose the whole term."""
    assert extract_term_corrections(
        "check the frontrun today", "check the front run today",
    ) == [("frontrun", "front run")]


def test_a_short_unrelated_rewrite_is_not_mined_for_a_term() -> None:
    """The cost of recovering terms from loose spans is that a short rewrite
    must not be searched until something vaguely similar turns up. 'two' and
    'words' score exactly as high as the real mis-hearing 'wrong'->'run', so
    only the structural rule (whole span vs rescued fragment) can refuse it."""
    assert extract_term_corrections("one two three", "completely different words") == []
    assert extract_term_corrections("send it now", "cancel that please") == []


def test_a_loose_but_WHOLE_span_is_still_a_correction() -> None:
    """The other side of that rule. "wrong"->"run" scores 0.50 — the same as the
    junk fragment above — and is kept because it IS the span: the operator
    changed exactly that word and nothing else."""
    assert extract_term_corrections("the front wrong", "the front run") == [
        ("wrong", "run"),
    ]


# ---------------------------------------------------------------------------
# The corpus round-trip
# ---------------------------------------------------------------------------


def test_pairs_round_trip_through_the_corpus(tmp_path: Path) -> None:
    p = tmp_path / "stt_corrections.jsonl"
    append_correction_pair(p, _pair("chicken tracker", "chicken tractor"))
    append_correction_pair(p, _pair("front rung", "front run"))

    got = list(iter_correction_pairs(p))
    assert [(g.transcript, g.sent) for g in got] == [
        ("chicken tracker", "chicken tractor"),
        ("front rung", "front run"),
    ]
    assert got[0].instance == "salem" and got[0].at


def test_a_missing_corpus_is_empty_not_an_error(tmp_path: Path) -> None:
    assert list(iter_correction_pairs(tmp_path / "absent.jsonl")) == []


def test_a_corrupt_row_is_skipped_LOUDLY(tmp_path: Path) -> None:
    """Silently dropping a row would make the COUNTS quietly wrong, and the
    counts are what the operator's decision rests on."""
    p = tmp_path / "c.jsonl"
    append_correction_pair(p, _pair("chicken tracker", "chicken tractor"))
    with p.open("a", encoding="utf-8") as f:
        f.write("{not json\n")

    with structlog.testing.capture_logs() as captured:
        got = list(iter_correction_pairs(p))

    assert len(got) == 1
    events = [c for c in captured if c.get("event") == "stt.vocab_corpus.rows_skipped"]
    assert len(events) == 1 and events[0]["skipped"] == 1


# ---------------------------------------------------------------------------
# propose_vocab_additions — the threshold, the cap, the already-have filter
# ---------------------------------------------------------------------------


def test_the_threshold_is_TWO_per_the_operator_ruling() -> None:
    """The ruling of 2026-08-05, pinned as a LITERAL.

    Deliberately not written as ``* MIN_CORRECTION_COUNT`` asserting
    ``count == MIN_CORRECTION_COUNT`` — that shape is self-referential and
    passes at any threshold, so it pins the mechanism while leaving the ruling
    itself unguarded. The literal 2 is the point of this pin.
    """
    assert MIN_CORRECTION_COUNT == 2
    pairs = [_pair("the chicken tracker", "the chicken tractor")] * 2
    props = propose_vocab_additions(pairs, current_vocab=[])
    assert [p.term for p in props] == ["tractor"]
    assert props[0].count == 2
    assert props[0].heard == ["tracker"]


def test_a_term_corrected_ONCE_is_NOT_proposed() -> None:
    """The boundary below. One correction is a typo or a one-off; it is the
    SECOND that turns a slip into the operator repeating himself."""
    pairs = [_pair("the chicken tracker", "the chicken tractor")]
    assert propose_vocab_additions(pairs, current_vocab=[]) == []


def test_a_term_corrected_many_times_is_still_one_proposal() -> None:
    """Above the bar the count keeps climbing; it does not split the term."""
    pairs = [_pair("the chicken tracker", "the chicken tractor")] * 5
    props = propose_vocab_additions(pairs, current_vocab=[])
    assert len(props) == 1 and props[0].count == 5


def test_a_term_ALREADY_in_the_vocabulary_is_never_proposed() -> None:
    """It is already biasing, so a correction on it means the bias is not
    ENOUGH — a different problem, which must not read as a missing term."""
    pairs = [_pair("the chicken tracker", "the chicken tractor")] * 5
    assert propose_vocab_additions(pairs, current_vocab=["Tractor"]) == []


def test_distinct_mishearings_are_carried_as_evidence() -> None:
    pairs = [
        _pair("the front rung", "the front run"),
        _pair("the front wrong", "the front run"),
        _pair("the front rung", "the front run"),
    ]
    props = propose_vocab_additions(pairs, current_vocab=[])
    assert len(props) == 1
    assert props[0].count == 3
    assert sorted(props[0].heard) == ["rung", "wrong"]


def test_proposals_are_ordered_highest_count_first_and_STABLE() -> None:
    pairs = (
        [_pair("front rung", "front run")] * 3
        + [_pair("chicken tracker", "chicken tractor")] * 5
    )
    props = propose_vocab_additions(pairs, current_vocab=[])
    assert [p.term for p in props] == ["tractor", "run"]
    # Stable across runs — an operator re-reading the review sees the same list.
    assert [p.term for p in propose_vocab_additions(pairs, current_vocab=[])] == [
        "tractor", "run",
    ]


def test_the_proposal_stream_is_CAPPED() -> None:
    """A long tail must not flood the morning review or the prompt window."""
    pairs = []
    for i in range(MAX_LEARNED_TERMS + 8):
        pairs += [_pair(f"word heard{i:03d} here", f"word meant{i:03d} here")] * 3

    with structlog.testing.capture_logs() as captured:
        props = propose_vocab_additions(pairs, current_vocab=[])

    assert len(props) == MAX_LEARNED_TERMS
    assert [c for c in captured if c.get("event") == "stt.vocab_proposals.capped"]


def test_an_empty_corpus_says_it_ran(  ) -> None:
    """ILB: 'nothing to propose' is a real answer and must be distinguishable
    from 'the loop never ran'."""
    with structlog.testing.capture_logs() as captured:
        assert propose_vocab_additions([], current_vocab=[]) == []
    events = [c for c in captured if c.get("event") == "stt.vocab_proposals.computed"]
    assert len(events) == 1 and events[0]["proposals"] == 0


# ---------------------------------------------------------------------------
# apply_approved_terms — the human step, and the ceiling
# ---------------------------------------------------------------------------


def test_only_approved_terms_are_added() -> None:
    out = apply_approved_terms(["Salem"], ["chicken tractor"])
    assert out == ["Salem", "chicken tractor"]


def test_nothing_is_added_without_approval() -> None:
    """The whole guardrail: this function IS the human step. No approvals, no
    mutation — never silent auto-mutation."""
    assert apply_approved_terms(["Salem"], []) == ["Salem"]
    assert apply_approved_terms(["Salem"], None) == ["Salem"]


def test_the_existing_vocabulary_is_never_reordered_or_dropped() -> None:
    base = ["Algernon", "Salem", "KAL-LE"]
    out = apply_approved_terms(base, ["front coop"])
    assert out[: len(base)] == base


def test_a_duplicate_approval_is_a_no_op() -> None:
    assert apply_approved_terms(["Salem"], ["salem", "SALEM"]) == ["Salem"]


def test_approvals_past_the_cap_are_DROPPED_and_logged(  ) -> None:
    """Dropping loudly beats a prompt that quietly stops biasing its own tail.

    Whisper does not error on prompt overflow — it degrades silently — so the
    cap has to hold even against an operator who approves everything.
    """
    approved = [f"term{i:03d}" for i in range(MAX_LEARNED_TERMS + 5)]
    with structlog.testing.capture_logs() as captured:
        out = apply_approved_terms(["Salem"], approved)

    assert len(out) == 1 + MAX_LEARNED_TERMS
    events = [c for c in captured if c.get("event") == "stt.vocab_approval.capped"]
    assert len(events) == 1 and events[0]["dropped"] == 5


def test_the_approved_vocabulary_still_fits_the_whisper_prompt_window() -> None:
    """END-TO-END on the real constraint, through the REAL prompt builder.

    The cap here and the char budget in ``stt_backends`` are two guards on one
    hazard; this pin is what proves they agree. A fully-spent learned budget on
    top of the shipped static list must still emit every term — if it truncates,
    the caps are mis-tuned relative to each other and the tail silently stops
    biasing.
    """
    from alfred.telegram.config import _DEFAULT_STT_VOCAB_TERMS
    from alfred.telegram.stt_backends import (
        _WHISPER_PROMPT_CHAR_BUDGET,
        _vocab_to_whisper_prompt,
    )

    # Realistic learned terms, not one-char stubs — the operator's are 2 words.
    learned = [f"chicken tractor {i:02d}" for i in range(MAX_LEARNED_TERMS)]
    full = apply_approved_terms(list(_DEFAULT_STT_VOCAB_TERMS), learned)
    assert len(full) == len(_DEFAULT_STT_VOCAB_TERMS) + MAX_LEARNED_TERMS

    with structlog.testing.capture_logs() as captured:
        prompt = _vocab_to_whisper_prompt(full)

    assert len(prompt) <= _WHISPER_PROMPT_CHAR_BUDGET
    assert prompt.count(", ") == len(full) - 1, "every term must still be biasing"
    assert [
        c for c in captured if c.get("event") == "stt.vocab_prompt_truncated"
    ] == [], "a fully-spent learned budget must not overflow the window"


# ---------------------------------------------------------------------------
# The decided store + the single accessor seam
# ---------------------------------------------------------------------------


class _Stt:
    """Minimal stand-in for the STT config the seam reads."""

    def __init__(self, vocab_terms, vocab_decided_path=""):
        self.vocab_terms = list(vocab_terms)
        self.vocab_decided_path = str(vocab_decided_path)


def _approve(path, term: str) -> None:
    append_decision(
        path, VocabDecision(type=DECISION_APPROVE, term=term, operator="andrew")
    )


def _reject(path, term: str) -> None:
    append_decision(
        path, VocabDecision(type=DECISION_REJECT, term=term, operator="andrew")
    )


def test_an_approved_term_is_emitted_by_the_seam(tmp_path: Path) -> None:
    """The whole point of the loop: a yes at review reaches transcription."""
    d = tmp_path / "decided.jsonl"
    _approve(d, "chicken tractor")
    assert effective_vocab_terms(_Stt(["Salem"], d)) == ["Salem", "chicken tractor"]


def test_the_raw_config_list_is_no_longer_the_whole_vocabulary(tmp_path: Path) -> None:
    """States the contract the seam exists to enforce: reading `vocab_terms`
    directly now gives an ANSWER THAT IS WRONG, and a consumer that does so
    silently misses everything the operator taught."""
    d = tmp_path / "decided.jsonl"
    _approve(d, "front coop")
    cfg = _Stt(["Salem"], d)
    assert cfg.vocab_terms == ["Salem"]
    assert effective_vocab_terms(cfg) == ["Salem", "front coop"]


def test_a_REJECTED_term_never_reaches_the_vocabulary(tmp_path: Path) -> None:
    d = tmp_path / "decided.jsonl"
    _reject(d, "tractor")
    assert effective_vocab_terms(_Stt(["Salem"], d)) == ["Salem"]


def test_a_rejected_term_is_not_proposed_again(tmp_path: Path) -> None:
    """The groundhog guard, driven through the PRODUCTION filter.

    Without a store that remembers the NO, a declined term returns to the review
    every morning forever — the exact bug
    `routine.match_calibration.filter_pending_for_review` had to fix. The filter
    lives inside `propose_vocab_additions` rather than in its callers, so a
    surface added later cannot forget it and quietly resurrect the groundhog.
    """
    d = tmp_path / "decided.jsonl"
    _reject(d, "tractor")
    pairs = [_pair("the chicken tracker", "the chicken tractor")] * 4

    with structlog.testing.capture_logs() as captured:
        props = propose_vocab_additions(
            pairs, current_vocab=[], decided=load_decisions(d),
        )

    assert props == []
    events = [
        c for c in captured if c.get("event") == "stt.vocab_proposals.already_ruled"
    ]
    assert len(events) == 1 and events[0]["suppressed"] == 1


def test_an_already_approved_term_is_not_proposed_again(tmp_path: Path) -> None:
    """The other half: an approved term is already biasing, so a further
    correction of it means the bias is not ENOUGH — a different problem, and it
    must not read as a missing term."""
    d = tmp_path / "decided.jsonl"
    _approve(d, "tractor")
    pairs = [_pair("the chicken tracker", "the chicken tractor")] * 4
    assert propose_vocab_additions(
        pairs, current_vocab=[], decided=load_decisions(d),
    ) == []


def test_an_undecided_term_is_still_proposed(tmp_path: Path) -> None:
    """The control: the decided-store filter must not swallow everything."""
    d = tmp_path / "decided.jsonl"
    _reject(d, "something else entirely")
    pairs = [_pair("the chicken tracker", "the chicken tractor")] * 2
    props = propose_vocab_additions(
        pairs, current_vocab=[], decided=load_decisions(d),
    )
    assert [p.term for p in props] == ["tractor"]


def test_the_operator_may_change_his_mind_LATER_WINS(tmp_path: Path) -> None:
    d = tmp_path / "decided.jsonl"
    _reject(d, "front run")
    _approve(d, "front run")
    assert effective_vocab_terms(_Stt(["Salem"], d)) == ["Salem", "front run"]

    _reject(d, "front run")
    assert effective_vocab_terms(_Stt(["Salem"], d)) == ["Salem"]


def test_no_decided_store_configured_is_byte_identical(tmp_path: Path) -> None:
    """An instance that has not opted in behaves exactly as before."""
    assert effective_vocab_terms(_Stt(["Salem", "KAL-LE"], "")) == ["Salem", "KAL-LE"]
    assert effective_vocab_terms(
        _Stt(["Salem"], tmp_path / "never_written.jsonl")
    ) == ["Salem"]


def test_the_seam_tolerates_a_missing_stt_config() -> None:
    """`routes_voice` passes `getattr(talker_config, "stt", None)`, which can be
    None on a half-configured instance. Biasing degrades; nothing raises."""
    assert effective_vocab_terms(None) == []


def test_a_corrupt_decision_row_is_skipped_LOUDLY(tmp_path: Path) -> None:
    d = tmp_path / "decided.jsonl"
    _approve(d, "front coop")
    with d.open("a", encoding="utf-8") as f:
        f.write("{not json\n")

    with structlog.testing.capture_logs() as captured:
        learned = load_decisions(d)

    assert learned.approved == ["front coop"]
    events = [c for c in captured if c.get("event") == "stt.vocab_decided.rows_skipped"]
    assert len(events) == 1 and events[0]["skipped"] == 1


def test_the_learned_cap_binds_CUMULATIVELY_across_sessions(tmp_path: Path) -> None:
    """THE MUTATION THAT THE ORIGINAL SINGLE-CALL CAP TEST COULD NOT CATCH.

    Capping per-approval over a flat list never binds in real use: approve a few
    terms each morning and `added` restarts at zero every time, so the learned
    list grows without limit while the Whisper prompt window — which degrades
    SILENTLY — fills up. Reading the WHOLE approved list back out of the store on
    every call is what makes the cap a cumulative bound rather than a per-call
    one.
    """
    d = tmp_path / "decided.jsonl"
    static = ["Salem"]

    # Two separate approval sessions, each individually under the cap.
    for i in range(MAX_LEARNED_TERMS):
        _approve(d, f"termA{i:03d}")
    after_first = effective_vocab_terms(_Stt(static, d))
    assert len(after_first) == len(static) + MAX_LEARNED_TERMS

    for i in range(5):
        _approve(d, f"termB{i:03d}")
    after_second = effective_vocab_terms(_Stt(static, d))

    assert len(after_second) == len(static) + MAX_LEARNED_TERMS, (
        "the cap must bind across sessions, not per approval call"
    )


def test_the_effective_vocabulary_still_fits_the_prompt_window(tmp_path: Path) -> None:
    """The cap agreement, now driven through the SEAM rather than a direct call —
    which is the path production actually takes."""
    from alfred.telegram.config import _DEFAULT_STT_VOCAB_TERMS
    from alfred.telegram.stt_backends import (
        _WHISPER_PROMPT_CHAR_BUDGET,
        _vocab_to_whisper_prompt,
    )

    d = tmp_path / "decided.jsonl"
    for i in range(MAX_LEARNED_TERMS):
        _approve(d, f"chicken tractor {i:02d}")

    full = effective_vocab_terms(_Stt(list(_DEFAULT_STT_VOCAB_TERMS), d))
    assert len(full) == len(_DEFAULT_STT_VOCAB_TERMS) + MAX_LEARNED_TERMS

    with structlog.testing.capture_logs() as captured:
        prompt = _vocab_to_whisper_prompt(full)

    assert len(prompt) <= _WHISPER_PROMPT_CHAR_BUDGET
    assert prompt.count(", ") == len(full) - 1, "every term must still be biasing"
    assert [
        c for c in captured if c.get("event") == "stt.vocab_prompt_truncated"
    ] == []


# ---------------------------------------------------------------------------
# The gate is a JUDGMENT, so it has to be observable and it must not be
# destructive. Two properties, pinned separately.
# ---------------------------------------------------------------------------


def test_a_rejected_span_is_logged_so_over_dropping_is_visible() -> None:
    """ILB on the drop. "the loop stopped proposing" and "the gate got too
    tight" must not look identical from the outside."""
    with structlog.testing.capture_logs() as captured:
        assert extract_term_corrections("one two three", "completely different words") == []

    events = [c for c in captured if c.get("event") == "stt.vocab_span_rejected"]
    assert len(events) == 1
    ev = events[0]
    assert ev["score"] < ev["span_floor"]
    assert ev["heard_words"] == 3 and ev["meant_words"] == 3


def test_the_rejection_log_carries_no_transcript_TEXT() -> None:
    """The spans are the operator's own words. Lengths and a score are enough to
    tune a threshold; the content is not the log's business."""
    with structlog.testing.capture_logs() as captured:
        extract_term_corrections("send it now", "cancel that please")

    events = [c for c in captured if c.get("event") == "stt.vocab_span_rejected"]
    assert len(events) == 1
    rendered = " ".join(str(v) for v in events[0].values())
    for secret_ish in ("send", "cancel", "please", "now"):
        assert secret_ish not in rendered


def test_a_span_the_gate_REFUSES_is_still_retained_in_the_corpus(
    tmp_path: Path,
) -> None:
    """The gate filters EXTRACTION, never the corpus. This is what makes the
    thresholds safe to hold loosely: a better extractor run later recovers
    whatever a heuristic dropped today, so no operator correction is ever
    permanently lost to a judgment call."""
    corpus = tmp_path / "c.jsonl"
    pair = _pair("one two three", "completely different words")
    append_correction_pair(corpus, pair)

    # Extraction refuses it...
    assert extract_term_corrections(pair.transcript, pair.sent) == []
    # ...and it is on disk regardless, in full.
    rows = list(iter_correction_pairs(corpus))
    assert len(rows) == 1
    assert rows[0].transcript == "one two three"
    assert rows[0].sent == "completely different words"


def test_a_REORDER_recovers_the_term_rather_than_mining_the_move() -> None:
    """Measured behaviour on the reorder case: the operator did mean "tractor",
    and the tightest fragment in the span says so. The move itself is not a
    vocabulary term and is not proposed."""
    assert extract_term_corrections(
        "the chicken tracker needs cleaning", "needs cleaning the chicken tractor",
    ) == [("tracker", "tractor")]


# ---------------------------------------------------------------------------
# The gate's BATCH-boundary aggregate — one headline number per pass
# ---------------------------------------------------------------------------


def test_the_stats_variant_reports_the_refused_span_count() -> None:
    """The per-span DEBUG line answers "why was THIS one refused"; the count
    answers "how often is the gate firing", which is not a per-pair question."""
    corrections, rejected = extract_term_corrections_with_stats(
        "one two three", "completely different words",
    )
    assert corrections == []
    assert rejected == 1


def test_the_stats_variant_counts_zero_when_the_gate_accepts() -> None:
    corrections, rejected = extract_term_corrections_with_stats(
        "clean the chicken tracker", "clean the chicken tractor",
    )
    assert corrections == [("tracker", "tractor")]
    assert rejected == 0


def test_the_plain_extractor_still_returns_a_bare_list() -> None:
    """The wrapper's whole job. A widened return would have made a count nobody
    asked for into every caller's problem — including the web capture site."""
    out = extract_term_corrections("clean the chicken tracker", "clean the chicken tractor")
    assert out == [("tracker", "tractor")]


def test_the_batch_summary_carries_the_spans_refused_aggregate() -> None:
    """The operator's grep gets ONE number per pass at INFO, without having to
    turn on DEBUG and count per-span lines."""
    pairs = [
        _pair("one two three", "completely different words"),   # refused
        _pair("send it now", "cancel that please"),             # refused
        _pair("clean the chicken tracker", "clean the chicken tractor"),  # accepted
    ]
    with structlog.testing.capture_logs() as captured:
        propose_vocab_additions(pairs, current_vocab=[])

    events = [c for c in captured if c.get("event") == "stt.vocab_proposals.computed"]
    assert len(events) == 1
    assert events[0]["spans_refused"] == 2


def test_the_aggregate_is_emitted_even_when_it_is_zero() -> None:
    """ILB: "the gate refused nothing this pass" is the positive evidence that
    makes a shrinking proposal list attributable to something else. A field that
    vanishes at zero cannot carry that."""
    with structlog.testing.capture_logs() as captured:
        propose_vocab_additions([], current_vocab=[])

    events = [c for c in captured if c.get("event") == "stt.vocab_proposals.computed"]
    assert len(events) == 1
    assert events[0]["spans_refused"] == 0
    assert events[0]["detail"] == "ran, nothing to propose"


def test_the_aggregate_stays_a_COUNT_and_never_carries_span_text() -> None:
    """Same privacy rule as the per-span event, re-asserted at the aggregate:
    this is the batch summary of the messages the extractor understood LEAST,
    which is exactly the wrong material to widen a log surface with."""
    with structlog.testing.capture_logs() as captured:
        propose_vocab_additions(
            [_pair("send it now", "cancel that please")], current_vocab=[],
        )

    events = [c for c in captured if c.get("event") == "stt.vocab_proposals.computed"]
    rendered = " ".join(str(v) for v in events[0].values())
    for word in ("send", "cancel", "please", "now"):
        assert word not in rendered


# ---------------------------------------------------------------------------
# WARN-2 — an over-long span must not eat the correction hiding inside it
# ---------------------------------------------------------------------------


def test_an_over_long_span_still_yields_its_rescued_term() -> None:
    """The bug this closes, in one fixture: editing a word AND adding an
    afterthought in the same pass is ordinary, and difflib coalesces the two into
    one long span. Dropping that span whole lost the correction ENTIRELY — and
    invisibly, because the drop happened before the counter as well as before the
    rescue, so the aggregate read zero while a real correction was discarded."""
    out, refused = extract_term_corrections_with_stats(
        "clean the chicken tracker",
        "clean the chicken tractor today if you get a chance",
    )
    assert out == [("tracker", "tractor")]
    # Recovered, so nothing was refused — the count must not charge for a span it
    # successfully mined.
    assert refused == 0


def test_the_rescued_term_from_a_long_span_is_still_bounded() -> None:
    """The never-propose-a-clause guarantee survives the change. The rescue only
    considers sub-spans of at most MAX_TERM_WORDS and refuses the whole span by
    construction, so no clause can come back through this door."""
    out, _refused = extract_term_corrections_with_stats(
        "clean the chicken tracker",
        "clean the chicken tractor today if you get a chance",
    )
    for heard, meant in out:
        assert len(heard.split()) <= MAX_TERM_WORDS
        assert len(meant.split()) <= MAX_TERM_WORDS


def test_a_searched_span_with_nothing_rescuable_is_counted_and_reasoned() -> None:
    """Tier 2 refusal: wide enough to search, nothing inside cleared the rescue
    floor. Still refused — but COUNTED, and labelled so the aggregate can be
    broken down by cause."""
    with structlog.testing.capture_logs() as captured:
        out, refused = extract_term_corrections_with_stats(
            "one two three", "completely different words entirely plus more",
        )
    assert out == []
    assert refused == 1
    events = [c for c in captured if c.get("event") == "stt.vocab_span_rejected"]
    assert len(events) == 1
    assert events[0]["reason"] == "no_rescuable_fragment"


def test_a_span_wider_than_the_search_bound_is_refused_UNEXAMINED() -> None:
    """Tier 3, and the reason it exists is COST, not quality.

    `_rescue_correction` is quadratic in span length, and MAX_TERM_WORDS used to
    be the only thing shielding it. Measured before this bound existed: 16 words
    0.068s, 64 1.51s, 128 6.56s — extrapolating to ~480s for one full-length
    rewritten dictation, on a function called once per corpus pair on every card
    render. So a span wider than MAX_RESCUE_SPAN_WORDS is refused without being
    searched, and says so, rather than pretending a verdict was reached.
    """
    heard = " ".join(f"alpha{i}" for i in range(MAX_RESCUE_SPAN_WORDS + 8))
    meant = " ".join(f"beta{i}" for i in range(MAX_RESCUE_SPAN_WORDS + 8))
    with structlog.testing.capture_logs() as captured:
        out, refused = extract_term_corrections_with_stats(heard, meant)
    assert out == []
    assert refused == 1
    events = [c for c in captured if c.get("event") == "stt.vocab_span_rejected"]
    assert len(events) == 1
    assert events[0]["reason"] == "span_too_long"
    # The bound is reported, so a reader need not re-derive why it refused.
    assert events[0]["max_rescue_span_words"] == MAX_RESCUE_SPAN_WORDS


def test_the_search_bound_keeps_a_pathological_pair_in_MILLISECONDS() -> None:
    """The shield, measured rather than asserted by comment.

    A wholly-rewritten full-length dictation (MAX_MESSAGE_CHARS 8000 is ~1100
    words) is ONE giant replace span — the exact input that cost ~8 minutes per
    pair while the rescue was unbounded. A generous ceiling: the point is
    milliseconds-not-minutes, and a bound this loose still fails by ~1000x if the
    shield is ever removed.
    """
    import time

    heard = " ".join(f"alpha{i}" for i in range(1100))
    meant = " ".join(f"beta{i}" for i in range(1100))
    t0 = time.perf_counter()
    out, refused = extract_term_corrections_with_stats(heard, meant)
    elapsed = time.perf_counter() - t0
    assert out == []
    assert refused == 1
    assert elapsed < 1.0, f"pathological pair took {elapsed:.2f}s — the search bound is not holding"


def test_EVERY_refusal_reason_is_reachable_by_some_input() -> None:
    """No dead branches. A `reason` value no input can produce is a lie in the
    vocabulary of the log — the SEM005/006 precedent, where unreachable codes sat
    in a published set for months. Each tier gets a concrete driving input here,
    so the taxonomy cannot outgrow the code.
    """
    drivers = {
        "similarity": ("one two three", "completely different words"),
        "no_rescuable_fragment": (
            "one two three", "completely different words entirely plus more",
        ),
        "span_too_long": (
            " ".join(f"a{i}" for i in range(MAX_RESCUE_SPAN_WORDS + 8)),
            " ".join(f"b{i}" for i in range(MAX_RESCUE_SPAN_WORDS + 8)),
        ),
    }
    for expected, (heard, meant) in drivers.items():
        with structlog.testing.capture_logs() as captured:
            extract_term_corrections_with_stats(heard, meant)
        reasons = [
            c.get("reason") for c in captured
            if c.get("event") == "stt.vocab_span_rejected"
        ]
        assert expected in reasons, f"no input reaches reason={expected!r}"


def test_a_similarity_refusal_is_labelled_as_such() -> None:
    """The other reason, distinguishable. Both refusals leave an identical
    absence in the output, so without ``reason`` "the gate is too tight" could
    not be told apart from "he writes long afterthoughts"."""
    with structlog.testing.capture_logs() as captured:
        _out, refused = extract_term_corrections_with_stats(
            "one two three", "completely different words",
        )
    assert refused == 1
    events = [c for c in captured if c.get("event") == "stt.vocab_span_rejected"]
    assert events[0]["reason"] == "similarity"


def test_the_aggregate_covers_BOTH_refusal_reasons() -> None:
    """The name change earns itself here: one refusal of each kind, counted
    together, because a number covering only the similarity gate would answer a
    narrower question than the operator asked."""
    pairs = [
        _pair("one two three", "completely different words"),                        # similarity
        _pair("one two three", "completely different words entirely plus more"),  # no_rescuable_fragment
        _pair("clean the chicken tracker", "clean the chicken tractor"),             # accepted
    ]
    with structlog.testing.capture_logs() as captured:
        propose_vocab_additions(pairs, current_vocab=[])
    events = [c for c in captured if c.get("event") == "stt.vocab_proposals.computed"]
    assert len(events) == 1
    assert events[0]["spans_refused"] == 2
