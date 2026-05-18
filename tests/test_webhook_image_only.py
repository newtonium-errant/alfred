"""Tests for the webhook image-only HTML bifurcation (Queue #11, 2026-05-18).

Image-only marketing / transactional emails (Pizza Hut order confirms,
Patreon newsletters, etc.) carry their content as PNG/JPG images with
``<img alt="…">`` tags and no extractable text blocks. The HTML strip
correctly produces an empty body for these — that part is working as
intended — but downstream consumers (curator + distiller) couldn't
distinguish "image-only sender (legitimate source-content limitation)"
from "pipeline truncation bug." This led to 30+ per-sender Empty-Record
Sender syntheses generated as a compensation cycle.

The fix synthesizes a body from headers (alt-text + first 5 link
anchors) when the post-strip body is below 30 chars. The synth body
always carries the ``[image-only HTML; body synthesized from headers]``
marker so post-hoc grep can count the bifurcation rate.

These tests pin:
  * the synth marker presence on image-only HTML
  * alt-text capture from typical Microsoft-Graph-shaped HTML
  * link extraction with `javascript:` / `mailto:` filtering
  * the threshold behavior — body just above 30 chars stays as-is,
    body just under triggers the synth path
  * the no-signal path (HTML with no alt-text, no links) → empty body
    with the ``webhook.body_synthesis_no_signal`` log emission per
    ``feedback_intentionally_left_blank.md``
  * structured log emission via ``structlog.testing.capture_logs``
"""

from __future__ import annotations

import structlog

from alfred.mail.webhook import (
    _SYNTH_MARKER,
    _build_markdown,
    _extract_alt_texts,
    _extract_links,
    _strip_html,
    _synthesize_body_from_headers,
)


# -----------------------------------------------------------------------------
# Unit-level extractors
# -----------------------------------------------------------------------------


def test_extract_alt_texts_basic():
    html = (
        '<div><img src="hero.png" alt="Pizza Hut delivery hero image">'
        '<img alt="" src="spacer.gif">'
        '<img alt=\'Order #12345 on the way\' src="status.png"></div>'
    )
    alts = _extract_alt_texts(html)
    assert alts == [
        "Pizza Hut delivery hero image",
        "Order #12345 on the way",
    ]


def test_extract_alt_texts_dedupes_preserving_order():
    html = (
        '<img alt="Pizza Hut">'
        '<img alt="Order #12345">'
        '<img alt="Pizza Hut">'  # duplicate — second occurrence dropped
    )
    alts = _extract_alt_texts(html)
    assert alts == ["Pizza Hut", "Order #12345"]


def test_extract_alt_texts_decodes_entities():
    html = '<img alt="Order &amp; delivery for &quot;Andrew&quot;">'
    alts = _extract_alt_texts(html)
    assert alts == ['Order & delivery for "Andrew"']


def test_extract_links_skips_javascript_and_mailto():
    html = (
        '<a href="https://pizzahut.ca/track">Track your order</a>'
        '<a href="javascript:void(0)">×</a>'
        '<a href="mailto:hello@example.com">Email us</a>'
        '<a href="https://pizzahut.ca/unsubscribe">unsubscribe</a>'
    )
    links = _extract_links(html)
    assert links == [
        ("https://pizzahut.ca/track", "Track your order"),
        ("https://pizzahut.ca/unsubscribe", "unsubscribe"),
    ]


def test_extract_links_strips_inner_tags_in_anchor():
    # Anchor wraps an <img> + some inline formatting — anchor "text"
    # is empty / image-only. Extractor returns the href + the stripped
    # tag-text (possibly empty string).
    html = (
        '<a href="https://example.com/x">'
        '<img src="btn.png" alt="Click here">'
        '</a>'
        '<a href="https://example.com/y">'
        '<strong>Get</strong> <em>started</em>'
        '</a>'
    )
    links = _extract_links(html)
    assert links == [
        ("https://example.com/x", ""),
        ("https://example.com/y", "Get started"),
    ]


def test_extract_links_dedupes_on_url():
    html = (
        '<a href="https://example.com/x">A</a>'
        '<a href="https://example.com/x">B</a>'
        '<a href="https://example.com/y">C</a>'
    )
    links = _extract_links(html)
    assert links == [
        ("https://example.com/x", "A"),
        ("https://example.com/y", "C"),
    ]


# -----------------------------------------------------------------------------
# Synthesis logic
# -----------------------------------------------------------------------------


def test_synthesize_from_alt_texts_only():
    html = (
        '<img alt="Pizza Hut delivery hero">'
        '<img alt="Order #PP12345">'
    )
    synth = _synthesize_body_from_headers(
        html, subject="Your Pizza Hut Order Is Confirmed", from_addr="pizza@hut.com",
    )
    assert synth is not None
    assert synth.startswith(_SYNTH_MARKER)
    assert "Subject: Your Pizza Hut Order Is Confirmed" in synth
    assert "From: pizza@hut.com" in synth
    assert "Pizza Hut delivery hero" in synth
    assert "Order #PP12345" in synth


def test_synthesize_from_links_only():
    # No alt-text — just hyperlinks. Still synthesizes.
    html = (
        '<a href="https://example.com/track">Track</a>'
        '<a href="https://example.com/help">Help</a>'
    )
    synth = _synthesize_body_from_headers(
        html, subject="Update", from_addr="x@y.z",
    )
    assert synth is not None
    assert _SYNTH_MARKER in synth
    assert "https://example.com/track" in synth
    assert "Track" in synth


def test_synthesize_returns_none_when_no_signal():
    # HTML with no alt-text and no links — nothing to synthesize from.
    # The caller falls back to empty body + emits the no-signal log.
    html = "<div><img src='x.png'></div><table><tr><td></td></tr></table>"
    synth = _synthesize_body_from_headers(
        html, subject="x", from_addr="y@z.w",
    )
    assert synth is None


def test_synthesize_caps_alt_texts_and_links():
    # 10 alt-texts + 10 links — synth must cap each list.
    imgs = "".join(f'<img alt="alt-{i}">' for i in range(10))
    anchors = "".join(
        f'<a href="https://example.com/{i}">link-{i}</a>' for i in range(10)
    )
    synth = _synthesize_body_from_headers(
        imgs + anchors, subject="x", from_addr="y@z.w",
    )
    assert synth is not None
    # Default caps: 8 alts, 5 links. Match the bullet-prefixed form
    # (``- alt-N`` / ``- link-N``) so the literal ``Image alt-text:``
    # section header (which contains the substring ``alt-``) doesn't
    # spuriously satisfy the count — per code-reviewer findings on
    # the original c1 ship of these tests.
    assert synth.count("- alt-") == 8
    assert synth.count("- link-") == 5


# -----------------------------------------------------------------------------
# Integration via _build_markdown (the operator-visible record body)
# -----------------------------------------------------------------------------


def _pizza_hut_image_only_html() -> str:
    """A realistic-shape image-only Pizza Hut order confirmation HTML."""
    return (
        '<html><body>'
        '<table><tr><td>'
        '<img src="https://cdn.pizzahut.ca/hero.png" '
        'alt="Your Pizza Hut order is on the way">'
        '</td></tr><tr><td>'
        '<a href="https://pizzahut.ca/track?o=12345">'
        '<img src="track-btn.png" alt="Track your order">'
        '</a>'
        '</td></tr><tr><td>'
        '<a href="https://pizzahut.ca/order/12345">View order details</a>'
        '</td></tr></table>'
        '</body></html>'
    )


def test_build_markdown_image_only_pizza_hut_produces_synthesized_body():
    """The Queue #11 friction case: image-only Pizza Hut order confirm.

    Before this fix, the operator-visible record body was empty (just
    the headers above ``---`` followed by nothing). The curator could
    only describe the email by subject + sender, which the distiller
    then aggregated into "Empty-Record Sender" syntheses.

    After this fix: body carries the synth marker + alt-text + link
    anchors. The operator can read the record body and see what the
    email was about, the distiller has enough signal to decide
    "image-only sender" vs "pipeline bug," and post-hoc grep on the
    marker surfaces the full bifurcation set.
    """
    data = {
        "subject": "Your Pizza Hut Order Is Confirmed",
        "from": "noreply@pizzahut.ca",
        "to": "andrew@example.com",
        "date": "2026-05-18",
        "body": _pizza_hut_image_only_html(),
        "account": "live",
    }
    md = _build_markdown(data)
    # Headers are present.
    assert "# Your Pizza Hut Order Is Confirmed" in md
    assert "**From:** noreply@pizzahut.ca" in md
    # Synth marker is present below the `---` separator.
    assert _SYNTH_MARKER in md
    assert "Your Pizza Hut order is on the way" in md
    assert "Track your order" in md
    assert "View order details" in md
    assert "https://pizzahut.ca/track?o=12345" in md


def test_build_markdown_body_with_real_text_skips_synthesis():
    """When the HTML strip produces real body text, the synth path
    must NOT activate — we don't want to clutter normal emails with
    the synth marker.
    """
    data = {
        "subject": "Re: Project update",
        "from": "alice@example.com",
        "body": (
            "<html><body>"
            "<p>Hi Andrew, the budget for Q3 is approved. "
            "Let's sync next Tuesday to walk through the line items.</p>"
            "<p>Best, Alice</p>"
            "</body></html>"
        ),
    }
    md = _build_markdown(data)
    assert _SYNTH_MARKER not in md
    assert "budget for Q3 is approved" in md
    assert "Let's sync next Tuesday" in md


def test_build_markdown_plain_text_body_unchanged():
    """Plain-text bodies (no HTML, IMAP-fetched) bypass the synth
    path entirely — no `<` in body means no strip, no synth.
    """
    data = {
        "subject": "Hi",
        "from": "alice@example.com",
        "body": "Just a plain text email, no HTML at all.",
    }
    md = _build_markdown(data)
    assert _SYNTH_MARKER not in md
    assert "Just a plain text email" in md


def test_build_markdown_short_html_body_above_threshold_unchanged():
    """Body of ~50 chars (above the 30-char threshold) stays as-is.

    Regression guard: the threshold logic must not over-trigger.
    """
    data = {
        "subject": "Short note",
        "from": "alice@example.com",
        "body": (
            "<p>This message is just over thirty chars long.</p>"
        ),
    }
    md = _build_markdown(data)
    assert _SYNTH_MARKER not in md
    assert "This message is just over thirty chars long." in md


def test_build_markdown_empty_html_with_no_synth_signal_logs_no_signal_path():
    """HTML with no alt-text and no links → synth returns None, body
    stays empty, ``webhook.body_synthesis_no_signal`` event fires.

    The log event is the only operator-visible signal that an
    "image-only HTML" path was attempted and produced nothing —
    per ``feedback_intentionally_left_blank.md``.
    """
    data = {
        "subject": "x",
        "from": "y@z.w",
        # HTML tags but no alt-text, no links, no extractable text.
        "body": "<div><span></span><table><tr><td></td></tr></table></div>",
    }
    with structlog.testing.capture_logs() as captured:
        md = _build_markdown(data)
    # Body still empty (no signal to synthesize from).
    assert _SYNTH_MARKER not in md
    # No-signal event emitted.
    no_signal = [
        c for c in captured
        if c.get("event") == "webhook.body_synthesis_no_signal"
    ]
    assert len(no_signal) == 1
    entry = no_signal[0]
    assert entry["from_addr"] == "y@z.w"
    assert entry["subject"] == "x"
    assert entry["stripped_len"] == 0
    assert entry["raw_html_len"] > 0


def test_build_markdown_image_only_logs_synthesis_event():
    """The synth path emits ``webhook.body_synthesized_from_headers``
    with operator-grep-able fields (from_addr, subject, length deltas).

    Per the log-emission-test-pattern discipline — when we add a log
    line in production code, the test must drive the path and pin
    the event + key fields.
    """
    data = {
        "subject": "Your Pizza Hut Order Is Confirmed",
        "from": "noreply@pizzahut.ca",
        "body": _pizza_hut_image_only_html(),
    }
    with structlog.testing.capture_logs() as captured:
        _build_markdown(data)
    matches = [
        c for c in captured
        if c.get("event") == "webhook.body_synthesized_from_headers"
    ]
    assert len(matches) == 1
    entry = matches[0]
    assert entry["from_addr"] == "noreply@pizzahut.ca"
    assert entry["subject"] == "Your Pizza Hut Order Is Confirmed"
    # Stripped body was empty / very short; synth length is substantial.
    assert entry["stripped_len"] < 30
    assert entry["synth_len"] > 30


# -----------------------------------------------------------------------------
# Strip behavior unchanged — regression guard
# -----------------------------------------------------------------------------


def test_strip_html_image_only_still_returns_below_threshold():
    """The strip function itself is UNCHANGED. Image-only HTML still
    produces a string short enough to trigger the synth bifurcation —
    the bifurcation happens at the ``_build_markdown`` layer above.
    This regression guard catches any future "fix" that tries to merge
    strip + synth, OR a change that makes the strip extract significantly
    more text from image-only HTML (which would defeat the threshold gate).

    Note: the realistic Pizza Hut fixture has an ``<a>View order details</a>``
    anchor whose inner text IS extractable by ``_strip_html`` (the strip
    regex removes the ``<a>`` tag, leaving "View order details"). So the
    post-strip output is not literally empty — it's short. The intent
    we pin is: short enough that ``_build_markdown`` will route to the
    synth path (< ``_MIN_BODY_CHARS`` == 30). Per code-reviewer findings
    + ``feedback_worked_example_accuracy.md`` — pin the actual production
    semantic ("below the synth threshold"), not the coincidental
    "literally empty" assertion that breaks on realistic fixtures.
    """
    out = _strip_html(_pizza_hut_image_only_html())
    # Strip extracts only the anchor's inner text ("View order details");
    # no <p>/<div>/<table> text content. Length is well under 30 chars
    # → _build_markdown will trigger the synth fallback for this fixture.
    assert len(out) < 30
