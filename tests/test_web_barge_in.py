"""Unit tests for ``alfred.web.barge_in`` — the pure suppression pipeline.

UNCONDITIONAL (no deps). The mandated garbled-echo cases (§1.5), the evaluation
order (§1.4), and the mount-time clamps + list caps (§1.3 / sec-W1).
"""

from __future__ import annotations

import pytest

from alfred.web.barge_in import (
    BargeSettings,
    echo_score,
    evaluate_barge,
    normalize_barge_settings,
    normalize_text,
)
from alfred.web.config import BargeInConfig


def _settings(**over) -> BargeSettings:
    base = dict(enabled=True, too_early_ms=700, min_words=2, min_chars=6,
                echo_threshold=0.8, echo_grace_s=2.0,
                interrupt_phrases=frozenset({"stop", "wait", "salem"}),
                backchannel_phrases=frozenset({"yeah", "ok", "uh huh"}))
    base.update(over)
    return BargeSettings(**base)


# ---------------------------------------------------------------------------
# echo_score — the mandated garbled cases (§1.5) must all clear 0.8
# ---------------------------------------------------------------------------

_SPOKEN = "the quarterly report shows revenue grew twelve percent"


def test_echo_exact_is_one() -> None:
    assert echo_score(_SPOKEN, _SPOKEN) == 1.0


def test_echo_one_token_substitution() -> None:
    garbled = "the quarterly report shows revenue GREW twelve percent".replace("GREW", "flew")
    assert echo_score(garbled, _SPOKEN) >= 0.8


def test_echo_dropped_word() -> None:
    garbled = "the quarterly report shows revenue twelve percent"   # dropped "grew"
    assert echo_score(garbled, _SPOKEN) >= 0.8


def test_echo_split_word() -> None:
    garbled = "the quarterly re port shows revenue grew twelve percent"  # report→re port
    assert echo_score(garbled, _SPOKEN) >= 0.8


def test_echo_scattered_common_words_is_low() -> None:
    # Shares only a couple of common words, not a run.
    assert echo_score("the meeting is on percent street", _SPOKEN) < 0.8


def test_echo_empty_is_zero() -> None:
    assert echo_score("", _SPOKEN) == 0.0
    assert echo_score("hello", "") == 0.0


def test_echo_single_shared_word_below_min2() -> None:
    assert echo_score("percent", _SPOKEN) < 0.8   # 1-token overlap gated out


# ---------------------------------------------------------------------------
# evaluate_barge — order (§1.4)
# ---------------------------------------------------------------------------


def test_too_early_beats_interrupt_phrase() -> None:
    d = evaluate_barge("stop", elapsed_ms=300, spoken="", settings=_settings())
    assert not d.barge and d.reason == "too_early"


def test_interrupt_phrase_barges() -> None:
    d = evaluate_barge("stop", elapsed_ms=1000, spoken="", settings=_settings())
    assert d.barge


def test_instance_name_is_interrupt() -> None:
    d = evaluate_barge("Salem!", elapsed_ms=1000, spoken="", settings=_settings())
    assert d.barge


def test_backchannel_suppressed() -> None:
    d = evaluate_barge("yeah", elapsed_ms=1000, spoken="", settings=_settings())
    assert not d.barge and d.reason == "backchannel"


def test_too_short_suppressed() -> None:
    d = evaluate_barge("no", elapsed_ms=1000, spoken="", settings=_settings())
    assert not d.barge and d.reason == "too_short"   # 1 word < min_words=2


def test_echo_suppressed_with_score() -> None:
    d = evaluate_barge(_SPOKEN, elapsed_ms=1000, spoken=_SPOKEN, settings=_settings())
    assert not d.barge and d.reason == "echo" and d.score >= 0.8


def test_genuine_utterance_barges() -> None:
    d = evaluate_barge("what about the budget for next quarter",
                       elapsed_ms=1000, spoken=_SPOKEN, settings=_settings())
    assert d.barge


def test_empty_text_is_too_short() -> None:
    d = evaluate_barge("   ", elapsed_ms=1000, spoken="", settings=_settings())
    assert not d.barge and d.reason == "too_short"


# ---------------------------------------------------------------------------
# normalize_barge_settings — clamps + list caps (§1.3 / sec-W1)
# ---------------------------------------------------------------------------


def test_normalize_folds_instance_name() -> None:
    s, _ = normalize_barge_settings(BargeInConfig(enabled=True), instance_name="KAL-LE")
    assert "kal le" in s.interrupt_phrases    # normalized


def test_normalize_clamps_threshold() -> None:
    s, w = normalize_barge_settings(BargeInConfig(echo_threshold=5.0))
    assert s.echo_threshold == 1.0
    assert any("echo_threshold" in x for x in w)


def test_normalize_clamps_too_early() -> None:
    s, w = normalize_barge_settings(BargeInConfig(too_early_ms=99999))
    assert s.too_early_ms == 5000


def test_normalize_caps_list_entries() -> None:
    cfg = BargeInConfig(enabled=True, interrupt_extra=[f"phrase{i}" for i in range(100)])
    s, w = normalize_barge_settings(cfg)
    assert any("capped" in x for x in w)


def test_normalize_drops_overlong_entries() -> None:
    cfg = BargeInConfig(enabled=True, backchannel_extra=["x" * 60, "ok yeah"])
    s, w = normalize_barge_settings(cfg)
    assert any("over 48 chars" in x for x in w)
    assert "ok yeah" in s.backchannel_phrases
    assert "x" * 60 not in s.backchannel_phrases


def test_normalize_text() -> None:
    assert normalize_text("Hello, THERE!!  friend") == "hello there friend"
