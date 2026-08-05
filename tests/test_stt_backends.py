

# ---------------------------------------------------------------------------
# #54 — the Whisper prompt window is BOUNDED (team-lead constraint 2)
# ---------------------------------------------------------------------------
#
# Whisper's ~224-token prompt window does not error on overflow; it truncates or
# degrades silently. Nothing bounded this before, which only starts to matter
# once the learned-vocabulary loop appends on operator approval — the operator
# approving one more term cannot see the ceiling.


def test_the_shipped_static_vocab_fits_with_headroom() -> None:
    """The 28 shipped terms must not be near the ceiling, or the learned loop has
    nowhere to grow. Measured 303 chars at the time this landed."""
    from alfred.telegram.config import _DEFAULT_STT_VOCAB_TERMS
    from alfred.telegram.stt_backends import (
        _WHISPER_PROMPT_CHAR_BUDGET,
        _vocab_to_whisper_prompt,
    )

    prompt = _vocab_to_whisper_prompt(list(_DEFAULT_STT_VOCAB_TERMS))
    assert prompt, "the static vocab must actually bias something"
    # Every shipped term survives — the cap must not be clipping today's list.
    assert prompt.count(", ") == len(_DEFAULT_STT_VOCAB_TERMS) - 1
    assert len(prompt) < _WHISPER_PROMPT_CHAR_BUDGET // 2, (
        "the shipped list should sit well under budget so the learned loop has room"
    )


def test_an_overlong_vocab_is_truncated_at_a_TERM_boundary() -> None:
    """Terms are kept whole. Half a term biases toward a string that does not
    exist, which is worse than omitting the term."""
    from alfred.telegram.stt_backends import (
        _WHISPER_PROMPT_CHAR_BUDGET,
        _vocab_to_whisper_prompt,
    )

    vocab = [f"term{i:04d}" for i in range(500)]  # far past the budget
    prompt = _vocab_to_whisper_prompt(vocab)

    assert len(prompt) <= _WHISPER_PROMPT_CHAR_BUDGET
    # No partial term: every emitted piece is one of the inputs, verbatim.
    for piece in prompt.split(", "):
        assert piece in vocab, f"{piece!r} is not a whole input term"


def test_truncation_is_LOGGED_never_silent() -> None:
    """Growth past the window is the failure this budget exists to surface."""
    import structlog

    from alfred.telegram.stt_backends import _vocab_to_whisper_prompt

    vocab = [f"term{i:04d}" for i in range(500)]
    with structlog.testing.capture_logs() as captured:
        _vocab_to_whisper_prompt(vocab)

    events = [c for c in captured if c.get("event") == "stt.vocab_prompt_truncated"]
    assert len(events) == 1
    assert events[0]["dropped"] > 0
    assert events[0]["kept"] < 500


def test_a_within_budget_vocab_logs_NOTHING() -> None:
    """The quiet path stays quiet — otherwise the warning that means 'you have
    outgrown the window' fires on every transcription."""
    import structlog

    from alfred.telegram.stt_backends import _vocab_to_whisper_prompt

    with structlog.testing.capture_logs() as captured:
        out = _vocab_to_whisper_prompt(["chicken tractor", "front run", "front coop"])

    assert out == "chicken tractor, front run, front coop"
    assert [c for c in captured if c.get("event") == "stt.vocab_prompt_truncated"] == []
