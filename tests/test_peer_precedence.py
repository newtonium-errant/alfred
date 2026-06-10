"""Unit tests for peer-message precedence (Z/O/P/R) — 2026-06-09.

Pure-function coverage of ``transport.peers`` precedence helpers:
``normalize_precedence`` (default R, unknown→R+flag) and
``render_precedence_prefix`` (the three label styles + the Z Flash marker).
The end-to-end broker / inbox-routing paths live in the handler + daemon
tests.
"""

from __future__ import annotations

import pytest

from alfred.transport.peers import (
    PRECEDENCE_DEFAULT,
    PRECEDENCE_FLASH_MARKER,
    PRECEDENCE_LABEL_STYLE_BOTH,
    PRECEDENCE_LABEL_STYLE_DEFAULT,
    PRECEDENCE_LABEL_STYLE_LETTERS,
    PRECEDENCE_LABEL_STYLE_WORDS,
    normalize_precedence,
    render_precedence_prefix,
)


# ---------------------------------------------------------------------------
# normalize_precedence
# ---------------------------------------------------------------------------


def test_default_is_routine():
    assert PRECEDENCE_DEFAULT == "R"
    assert PRECEDENCE_LABEL_STYLE_DEFAULT == "words"


@pytest.mark.parametrize("value", ["Z", "O", "P", "R"])
def test_valid_precedence_passes_through(value):
    prec, unknown = normalize_precedence(value)
    assert prec == value
    assert unknown is False


def test_lowercase_is_upcased():
    prec, unknown = normalize_precedence("p")
    assert prec == "P"
    assert unknown is False


def test_absent_defaults_to_routine_not_unknown():
    # None = the common "no precedence sent" case → R, NOT flagged unknown.
    prec, unknown = normalize_precedence(None)
    assert prec == "R"
    assert unknown is False


def test_unknown_value_coerces_to_routine_and_flags():
    prec, unknown = normalize_precedence("URGENT")
    assert prec == "R"
    assert unknown is True  # the log-trigger flag


def test_non_string_coerces_to_routine_and_flags():
    prec, unknown = normalize_precedence(5)
    assert prec == "R"
    assert unknown is True


# ---------------------------------------------------------------------------
# render_precedence_prefix — the three label styles
# ---------------------------------------------------------------------------


def test_words_style_default():
    # words is the default — [<peer> · Immediate].
    assert render_precedence_prefix("KAL-LE", "O", PRECEDENCE_LABEL_STYLE_WORDS) == (
        "[KAL-LE · Immediate] "
    )


def test_letters_style():
    assert render_precedence_prefix("KAL-LE", "O", PRECEDENCE_LABEL_STYLE_LETTERS) == (
        "[KAL-LE · O] "
    )


def test_both_style():
    assert render_precedence_prefix("KAL-LE", "O", PRECEDENCE_LABEL_STYLE_BOTH) == (
        "[KAL-LE · O Immediate] "
    )


def test_unknown_style_falls_back_to_words():
    assert render_precedence_prefix("VERA", "P", "bogus") == "[VERA · Priority] "


def test_none_style_falls_back_to_words():
    assert render_precedence_prefix("VERA", "P", None) == "[VERA · Priority] "


def test_flash_marker_on_z_in_words_style():
    out = render_precedence_prefix("VERA", "Z", PRECEDENCE_LABEL_STYLE_WORDS)
    assert PRECEDENCE_FLASH_MARKER in out
    assert "Flash" in out
    assert out == f"[VERA · {PRECEDENCE_FLASH_MARKER} Flash] "


def test_flash_marker_on_z_in_letters_style():
    out = render_precedence_prefix("VERA", "Z", PRECEDENCE_LABEL_STYLE_LETTERS)
    assert PRECEDENCE_FLASH_MARKER in out
    assert out == f"[VERA · {PRECEDENCE_FLASH_MARKER} Z] "


def test_no_flash_marker_on_non_z():
    for prec in ("O", "P", "R"):
        out = render_precedence_prefix("VERA", prec, PRECEDENCE_LABEL_STYLE_WORDS)
        assert PRECEDENCE_FLASH_MARKER not in out


def test_empty_peer_omits_peer_segment():
    # No empty "[ · Immediate]" bracket when from_peer is empty.
    assert render_precedence_prefix("", "O", PRECEDENCE_LABEL_STYLE_WORDS) == (
        "[Immediate] "
    )


def test_unknown_precedence_in_render_defaults_routine():
    # render is defensive — an unexpected precedence renders as R.
    assert render_precedence_prefix("X", "WAT", PRECEDENCE_LABEL_STYLE_WORDS) == (
        "[X · Routine] "
    )


def test_all_styles_for_each_precedence_render():
    # Smoke: every (precedence, style) pair renders a non-empty prefix.
    for prec in ("Z", "O", "P", "R"):
        for style in (
            PRECEDENCE_LABEL_STYLE_LETTERS,
            PRECEDENCE_LABEL_STYLE_WORDS,
            PRECEDENCE_LABEL_STYLE_BOTH,
        ):
            out = render_precedence_prefix("PEER", prec, style)
            assert out.startswith("[PEER · ")
            assert out.endswith("] ")
