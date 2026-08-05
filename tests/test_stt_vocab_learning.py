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
    MAX_LEARNED_TERMS,
    MIN_CORRECTION_COUNT,
    CorrectionPair,
    append_correction_pair,
    apply_approved_terms,
    extract_term_corrections,
    iter_correction_pairs,
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
