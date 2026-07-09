"""Clinic-capture Piece 2 — STT noise hardening.

  * 2b core: ``common.stt_noise.filter_stt_noise`` — line-level, exact-match,
    union-with-default. The MANDATORY near-miss: a real "thank you" inside a
    clinical sentence SURVIVES (no substring-nuke).
  * 2a: ``build_deepgram_url`` maps vocab to keyterm (nova-3) / keywords (nova-2).
  * 2b backstop: ``append_turn`` filters a VOICE user caption, never a typed one.

Each assert is mutation-verified (the note says which revert flips it).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from alfred.common.stt_noise import (
    _DEFAULT_STT_HALLUCINATION_DENYLIST,
    filter_stt_noise,
    normalized_denylist,
)


# ---------------------------------------------------------------------------
# 2b core — filter_stt_noise
# ---------------------------------------------------------------------------


def test_standalone_caption_is_dropped() -> None:
    kept, dropped = filter_stt_noise("Thank you for watching!")
    assert kept == "" and dropped == ["Thank you for watching!"]


def test_denylist_phrase_embedded_in_clinical_sentence_survives() -> None:
    """MANDATORY near-miss (clinical-safety): a denylist phrase that appears as a
    SUBSTRING of a real clinical sentence must NOT nuke the sentence — matching
    is exact-line, never substring. Here "please subscribe" IS a substring of the
    clinical line, and "thanks for listening" is embedded in the other. Mutation:
    switch the matcher to substring-``in`` → both are dropped → fails."""
    for text in (
        "please subscribe the patient to the portal and send the forms",
        "thanks for listening to the referral, book the follow-up appointment",
    ):
        kept, dropped = filter_stt_noise(text)
        assert dropped == [], f"substring-nuked: {text!r}"
        assert kept == text


def test_multiline_drops_only_the_caption_line() -> None:
    kept, dropped = filter_stt_noise(
        "Book the Friday appointment.\nPlease subscribe")
    assert kept == "Book the Friday appointment."
    assert dropped == ["Please subscribe"]


def test_per_instance_extra_unions_with_default() -> None:
    """A per-instance extra term is dropped AND the universal default stays
    active (union, not replace). Mutation: make config REPLACE the default →
    'thanks for watching' survives → the second assert fails."""
    kept, dropped = filter_stt_noise("Hedgesha", ["Hedgesha"])
    assert kept == "" and dropped == ["Hedgesha"]
    # default still active even when an extra list is supplied
    kept2, dropped2 = filter_stt_noise("thanks for watching", ["Hedgesha"])
    assert kept2 == "" and dropped2 == ["thanks for watching"]


def test_clean_text_is_identity() -> None:
    text = "send the prescription refill to the pharmacy"
    assert filter_stt_noise(text) == (text, [])


def test_empty_text_no_drop() -> None:
    assert filter_stt_noise("") == ("", [])


def test_normalization_handles_punct_and_case() -> None:
    # Trailing punctuation + casing + extra whitespace all normalize to a match.
    kept, dropped = filter_stt_noise("  Please   Subscribe.  ")
    assert kept == "" and len(dropped) == 1


def test_bare_thank_you_survives() -> None:
    """A bare closing 'thank you' is DELIBERATELY not in the default set — only
    the caption '...for watching' forms are. A clinician saying 'thank you'
    must survive."""
    assert filter_stt_noise("thank you") == ("thank you", [])


def test_default_denylist_is_nonempty_and_normalized() -> None:
    assert len(_DEFAULT_STT_HALLUCINATION_DENYLIST) >= 5
    assert "thank you for watching" in normalized_denylist()


# ---------------------------------------------------------------------------
# 2a — build_deepgram_url vocab → keyterm/keywords (model-aware)
# ---------------------------------------------------------------------------


def test_deepgram_url_nova3_uses_keyterm() -> None:
    from alfred.web.config import WebVoiceSttConfig
    from alfred.web.stt_deepgram import build_deepgram_url

    url = build_deepgram_url(WebVoiceSttConfig(
        provider="deepgram", model="nova-3",
        vocab_terms=["Hypatia", "disability tax credit"]))
    # nova-3 → keyterm (Keyterm Prompting); keywords NOT emitted.
    assert "keyterm=Hypatia" in url
    assert "keyterm=disability+tax+credit" in url
    assert "keywords=" not in url


def test_deepgram_url_nova2_uses_keywords() -> None:
    from alfred.web.config import WebVoiceSttConfig
    from alfred.web.stt_deepgram import build_deepgram_url

    url = build_deepgram_url(WebVoiceSttConfig(
        provider="deepgram", model="nova-2", vocab_terms=["Salem"]))
    assert "keywords=Salem" in url and "keyterm=" not in url


def test_deepgram_url_empty_vocab_byte_identical() -> None:
    from alfred.web.config import WebVoiceSttConfig
    from alfred.web.stt_deepgram import build_deepgram_url

    url = build_deepgram_url(WebVoiceSttConfig(provider="deepgram", model="nova-3"))
    assert "keyterm" not in url and "keywords" not in url


# ---------------------------------------------------------------------------
# 2b backstop — append_turn filters a VOICE caption, never a typed turn
# ---------------------------------------------------------------------------


def _sess():
    from alfred.telegram.session import Session
    now = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)
    return Session(
        session_id="s1", chat_id=1, started_at=now, last_message_at=now,
        model="claude-sonnet-4-6", transcript=[], vault_ops=[])


def test_append_turn_backstop_filters_voice_caption(tmp_path: Path) -> None:
    """A VOICE user turn carrying a caption artifact is filtered at append_turn.
    Mutation: drop the backstop → the caption persists in the transcript →
    fails."""
    from alfred.telegram.session import append_turn
    from alfred.telegram.state import StateManager

    state = StateManager(tmp_path / "s.json")
    state.load()
    sess = _sess()
    append_turn(state, sess, "user", "Thanks for watching", kind="voice")
    assert sess.transcript[-1]["content"] == ""     # caption stripped


def test_append_turn_backstop_leaves_typed_text(tmp_path: Path) -> None:
    """A TYPED user turn is NEVER filtered — a user could legitimately type a
    denylist phrase. Only voice STT is subject to the hallucination backstop."""
    from alfred.telegram.session import append_turn
    from alfred.telegram.state import StateManager

    state = StateManager(tmp_path / "s.json")
    state.load()
    sess = _sess()
    append_turn(state, sess, "user", "Thanks for watching", kind="text")
    assert sess.transcript[-1]["content"] == "Thanks for watching"


def test_append_turn_backstop_keeps_real_voice_content(tmp_path: Path) -> None:
    from alfred.telegram.session import append_turn
    from alfred.telegram.state import StateManager

    state = StateManager(tmp_path / "s.json")
    state.load()
    sess = _sess()
    append_turn(state, sess, "user", "send the prescription refill", kind="voice")
    assert sess.transcript[-1]["content"] == "send the prescription refill"
