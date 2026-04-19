"""Tests for ``alfred.distiller.candidates.compute_score``.

The scoring function turns a ``CandidateSignal`` into a 0.0–1.0 priority
score that gates which records reach the LLM extraction stage. A silent
drift in the weights (e.g., body-length cap, keyword bonuses) would
change which records get distilled without any louder signal. These
tests lock the current contract in place so future tweaks are explicit.
"""

from __future__ import annotations

from alfred.distiller.candidates import CandidateSignal, compute_score


class TestComputeScore:
    """Contract: body-length is capped at 0.3; each keyword family
    contributes +0.15; outcome/context sections each contribute +0.1;
    total is clamped to 1.0."""

    def test_empty_signal_scores_zero(self) -> None:
        # All-zero signal → zero score (no bonuses, no body-length term).
        assert compute_score(CandidateSignal()) == 0.0

    def test_body_length_is_capped_at_0_3(self) -> None:
        # Body-length term is ``min(len/500, 0.3)``; a 10_000-char body
        # must NOT push the score past 0.3 from body alone.
        signal = CandidateSignal(body_length=10_000)
        assert compute_score(signal) == 0.3

    def test_each_keyword_family_adds_0_15(self) -> None:
        # One hit in each family → 4 * 0.15 = 0.6 (no body, no sections).
        signal = CandidateSignal(
            decision_keywords=1,
            assumption_keywords=1,
            constraint_keywords=1,
            contradiction_keywords=1,
        )
        assert compute_score(signal) == 0.6

    def test_outcome_and_context_each_add_0_1(self) -> None:
        # ``## Outcome`` and ``## Context`` are flat +0.1 each regardless
        # of count; total here is 0.2.
        signal = CandidateSignal(has_outcome=True, has_context=True)
        assert compute_score(signal) == 0.2

    def test_score_is_clamped_to_1(self) -> None:
        # Max-out every signal → would sum to 1.3 without the clamp; the
        # final value must be exactly 1.0.
        signal = CandidateSignal(
            body_length=10_000,
            has_outcome=True,
            has_context=True,
            decision_keywords=5,
            assumption_keywords=5,
            constraint_keywords=5,
            contradiction_keywords=5,
        )
        assert compute_score(signal) == 1.0
