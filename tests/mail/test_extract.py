"""Unit tests for alfred.mail.extract (Ship 1 — refactor verification).

These tests pin the public primitives directly. The existing tests at
``tests/test_webhook_image_only.py`` exercise the same logic via the
webhook.py alias path; Ship 1 doesn't change those tests (zero
behavior change). If a test there breaks, it indicates a real
regression in the refactor.

The constant pins (SYNTH_MARKER_IMAGE_ONLY, SYNTH_MARKER_UPSTREAM_TRUNCATED,
MIN_BODY_CHARS) carry the EXACT byte values expected pre-refactor —
operator grep on ``[image-only HTML; body synthesized from headers]``
or ``[upstream-truncated; body lost before Alfred reception]`` is the
canonical signal-finding workflow; any drift breaks it.

The invisible-Unicode fixtures use chr() escapes rather than literal
Unicode in the source so the test file itself is robust against the
Write-tool normalization that motivated
``feedback_write_tool_invisible_unicode_normalization.md``.
"""

from __future__ import annotations

import pytest

from alfred.mail import extract


# ---------------------------------------------------------------------------
# strip_html
# ---------------------------------------------------------------------------

def test_strip_html_removes_style_block() -> None:
    html = "<style>p{color:red}</style><p>hello</p>"
    assert "color" not in extract.strip_html(html)
    assert "hello" in extract.strip_html(html)


def test_strip_html_removes_script_block() -> None:
    html = "<script>alert(1)</script><p>hello</p>"
    assert "alert" not in extract.strip_html(html)
    assert "hello" in extract.strip_html(html)


def test_strip_html_br_becomes_newline() -> None:
    html = "line1<br>line2<br/>line3"
    result = extract.strip_html(html)
    assert "line1" in result and "line2" in result and "line3" in result
    assert "\n" in result


def test_strip_html_strips_inline_tags() -> None:
    html = "<p>hello <b>bold</b> world</p>"
    result = extract.strip_html(html)
    assert "<" not in result and ">" not in result
    assert "hello" in result and "bold" in result and "world" in result


def test_strip_html_decodes_entities() -> None:
    html = "<p>a &amp; b &lt; c &gt; d &quot;e&quot; &nbsp;f</p>"
    result = extract.strip_html(html)
    assert "&amp;" not in result and "&lt;" not in result
    assert "&" in result and "<" in result and ">" in result


def test_strip_html_collapses_blank_lines() -> None:
    html = "<p>a</p><p></p><p></p><p>b</p>"
    result = extract.strip_html(html)
    assert "\n\n\n" not in result


# ---------------------------------------------------------------------------
# visible_text_len
# ---------------------------------------------------------------------------

def test_visible_text_len_none() -> None:
    assert extract.visible_text_len(None) == 0


def test_visible_text_len_empty_string() -> None:
    assert extract.visible_text_len("") == 0


def test_visible_text_len_pure_whitespace() -> None:
    assert extract.visible_text_len("   \t\n  ") == 0


def test_visible_text_len_figure_space_u2007() -> None:
    body = chr(0x2007) * 100
    assert extract.visible_text_len(body) == 0


def test_visible_text_len_combining_grapheme_joiner_u034f() -> None:
    body = chr(0x034F) * 50
    assert extract.visible_text_len(body) == 0


def test_visible_text_len_zero_width_range_u200b_to_u200f() -> None:
    body = "".join(chr(cp) for cp in range(0x200B, 0x2010))
    assert extract.visible_text_len(body) == 0


def test_visible_text_len_invisible_operators_u2060_to_u206f() -> None:
    body = "".join(chr(cp) for cp in range(0x2060, 0x2070))
    assert extract.visible_text_len(body) == 0


def test_visible_text_len_bom_ufeff() -> None:
    assert extract.visible_text_len(chr(0xFEFF) * 10) == 0


def test_visible_text_len_mixed_visible_and_invisible() -> None:
    body = "hello" + chr(0x2007) * 100 + "world"
    assert extract.visible_text_len(body) == 10


# ---------------------------------------------------------------------------
# extract_alt_texts
# ---------------------------------------------------------------------------

def test_extract_alt_texts_happy_path() -> None:
    html = '<img alt="logo"><img alt="banner"><img alt="footer">'
    assert extract.extract_alt_texts(html) == ["logo", "banner", "footer"]


def test_extract_alt_texts_dedups_preserving_order() -> None:
    html = '<img alt="logo"><img alt="banner"><img alt="logo">'
    assert extract.extract_alt_texts(html) == ["logo", "banner"]


def test_extract_alt_texts_skips_empty_and_decodes_entities() -> None:
    html = '<img alt=""><img alt="a &amp; b"><img alt="">'
    assert extract.extract_alt_texts(html) == ["a & b"]


# ---------------------------------------------------------------------------
# extract_links
# ---------------------------------------------------------------------------

def test_extract_links_happy_path() -> None:
    html = '<a href="https://example.com">Example</a> <a href="https://x.io">X</a>'
    result = extract.extract_links(html)
    assert result == [("https://example.com", "Example"), ("https://x.io", "X")]


def test_extract_links_dedups_on_url() -> None:
    html = '<a href="https://a.com">A</a> <a href="https://a.com">A again</a>'
    assert len(extract.extract_links(html)) == 1


def test_extract_links_skips_javascript_and_mailto() -> None:
    html = (
        '<a href="javascript:alert(1)">js</a>'
        '<a href="mailto:x@y.com">email</a>'
        '<a href="https://ok.com">ok</a>'
    )
    result = extract.extract_links(html)
    assert result == [("https://ok.com", "ok")]


# ---------------------------------------------------------------------------
# synthesize_body_from_headers
# ---------------------------------------------------------------------------

def test_synthesize_body_from_headers_happy_path() -> None:
    html = '<img alt="logo"><a href="https://example.com">Read</a>'
    result = extract.synthesize_body_from_headers(
        html, subject="Hello", from_addr="x@y.com",
    )
    assert result is not None
    assert extract.SYNTH_MARKER_IMAGE_ONLY in result
    assert "logo" in result
    assert "https://example.com" in result


def test_synthesize_body_from_headers_none_when_no_signal() -> None:
    html = "<p>nothing useful here</p>"
    result = extract.synthesize_body_from_headers(
        html, subject="Hi", from_addr="x@y.com",
    )
    assert result is None


def test_synthesize_body_from_headers_includes_subject_and_from() -> None:
    html = '<img alt="alt-text">'
    result = extract.synthesize_body_from_headers(
        html, subject="My Subject", from_addr="sender@example.com",
    )
    assert "My Subject" in result
    assert "sender@example.com" in result


# ---------------------------------------------------------------------------
# synthesize_minimal_from_subject
# ---------------------------------------------------------------------------

def test_synthesize_minimal_from_subject_happy_path() -> None:
    result = extract.synthesize_minimal_from_subject(
        subject="Hello", from_addr="x@y.com", account="live",
    )
    assert result is not None
    assert extract.SYNTH_MARKER_UPSTREAM_TRUNCATED in result
    assert "Hello" in result and "x@y.com" in result and "live" in result


def test_synthesize_minimal_from_subject_none_when_all_blank() -> None:
    result = extract.synthesize_minimal_from_subject(
        subject="", from_addr="", account="",
    )
    assert result is None


def test_synthesize_minimal_from_subject_marks_dropped_sender() -> None:
    result = extract.synthesize_minimal_from_subject(
        subject="S", from_addr="", account="live",
    )
    assert "n8n dropped sender" in result


# ---------------------------------------------------------------------------
# Contract pins
# ---------------------------------------------------------------------------

def test_min_body_chars_pinned_at_30() -> None:
    assert extract.MIN_BODY_CHARS == 30


def test_max_alt_texts_in_synth_pinned_at_8() -> None:
    assert extract.MAX_ALT_TEXTS_IN_SYNTH == 8


def test_max_links_in_synth_pinned_at_5() -> None:
    assert extract.MAX_LINKS_IN_SYNTH == 5


def test_synth_marker_image_only_byte_for_byte() -> None:
    assert extract.SYNTH_MARKER_IMAGE_ONLY == "[image-only HTML; body synthesized from headers]"


def test_synth_marker_upstream_truncated_byte_for_byte() -> None:
    assert extract.SYNTH_MARKER_UPSTREAM_TRUNCATED == "[upstream-truncated; body lost before Alfred reception]"


# ---------------------------------------------------------------------------
# Byte-level Unicode pin
# ---------------------------------------------------------------------------

def test_invisible_chars_re_pattern_covers_all_documented_codepoints() -> None:
    """Pin the code-points the INVISIBLE_CHARS_RE pattern was designed to catch."""
    codepoints = [
        0x00A0, 0x034F,
        0x200B, 0x200C, 0x200D, 0x200E, 0x200F,
        0x2007, 0x2028, 0x2029,
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
        0x2060, 0x206F,
        0x3000, 0xFEFF,
    ]
    body = "".join(chr(cp) for cp in codepoints)
    stripped = extract.INVISIBLE_CHARS_RE.sub("", body)
    assert stripped == "", (
        f"INVISIBLE_CHARS_RE missed codepoints; remaining: "
        f"{[hex(ord(c)) for c in stripped]}"
    )


def test_invisible_chars_re_includes_standard_whitespace() -> None:
    body = " \t\n\r"
    assert extract.INVISIBLE_CHARS_RE.sub("", body) == ""


# ---------------------------------------------------------------------------
# KNOWN/DEFERRED: cause-class 4 (HTML-drops-URLs)
# ---------------------------------------------------------------------------
# strip_html keeps anchor TEXT but discards the href URL for any email
# whose visible text exceeds MIN_BODY_CHARS (the synth path never fires,
# so extract_links — which DOES preserve URLs — is never consulted).
# This is the 4th documented cause-class in the empty-body arc memo
# (project_email_empty_body_pipeline.md) — content-bearing-but-lossy, a
# distinct surface from empty-body. Fixing it changes every email's body
# output and needs a parity re-pin, so it is DEFERRED to its own arc.
# These fixtures pin the CURRENT (lossy) behavior so the deferral is
# visible and a future fix-arc has a regression baseline to flip. When
# that arc lands and URL-preservation ships, these two assertions flip
# from "not in" to "in" — that's the signal the deferral closed.


def test_strip_html_drops_href_url_keeps_anchor_text_KNOWN_DEFERRED() -> None:
    """KNOWN/DEFERRED cause-class 4: strip_html drops the href, keeps text."""
    html = (
        "<p>Read our full report on the quarterly numbers and the outlook "
        "for next year over at "
        '<a href="https://example.com/q3-report">our blog</a> today.</p>'
    )
    result = extract.strip_html(html)
    # Anchor text survives the strip.
    assert "our blog" in result
    # URL is dropped — DEFERRED behavior, not a bug to fix in this arc.
    assert "https://example.com/q3-report" not in result


def test_strip_html_url_drop_not_recovered_when_body_above_threshold_KNOWN_DEFERRED() -> None:
    """Synth path (which preserves URLs) never fires above MIN_BODY_CHARS.

    Pins WHY cause-class 4 is uncovered by the Ship 1-5 work: the synth
    fallback (extract_links preserves URLs) only runs when the stripped
    body is below MIN_BODY_CHARS. A substantive body keeps its prose but
    silently loses its URLs, and no synth marker fires to flag it.
    """
    html = (
        "<p>This is a substantive newsletter body with well over thirty "
        "visible characters of real prose content, plus a "
        '<a href="https://example.com/cta">call to action</a> link.</p>'
    )
    stripped = extract.strip_html(html)
    # Body is above the synth threshold, so the synth path never fires.
    assert extract.visible_text_len(stripped) >= extract.MIN_BODY_CHARS
    # URL dropped and NOT recovered — the deferred cause-class 4 surface.
    assert "https://example.com/cta" not in stripped
