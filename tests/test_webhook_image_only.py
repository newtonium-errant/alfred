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
    _UPSTREAM_TRUNCATED_MARKER,
    _build_markdown,
    _extract_alt_texts,
    _extract_links,
    _strip_html,
    _synthesize_body_from_headers,
    _synthesize_minimal_from_subject,
    _visible_text_len,
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


# =============================================================================
# 2026-05-21 Empty-Email-synth bypass — Part A (zero-width-padding gate)
# =============================================================================
#
# Pattern 1 (17/24 = 71% of post-2026-05-18 Empty Email records): senders
# pad bodies with hundreds of invisible Unicode chars (U+2007 figure space,
# U+034F combining grapheme joiner, U+200B-U+200F / U+2060-U+206F zero-width
# range, U+FEFF BOM). Examples: Patreon, Budo Brothers marketing emails.
# `len(body)` returns 200+ so the original `len(body) < 30` gate at
# `_build_markdown` line 320 never fired — image-only synth was bypassed
# and the curator was handed a useless body, producing "Empty Email" records.
#
# Fix replaces the gate with `_visible_text_len(body) < 30`, which strips
# the invisible chars before measuring. Tests below pin:
#   (a) _visible_text_len handles the documented Unicode ranges correctly
#   (b) the gate fires on the zero-width-padding case (was bypassing pre-fix)
#   (c) the gate does NOT over-fire on legitimate plain-text emails


def test_visible_text_len_pure_zero_width_padding():
    """Patreon/Budo Brothers pattern: body composed entirely of invisible
    Unicode padding. `len(body)` returns ~200, but a human reads zero.
    """
    # 100 chars of figure-space + combining grapheme joiner pad
    padding = " ͏" * 100
    assert len(padding) == 200
    assert _visible_text_len(padding) == 0


def test_visible_text_len_mixed_visible_and_invisible():
    """Sanity guard: real visible text + invisible padding returns the
    visible text length, not the bare ``len()`` of the whole body.
    """
    # "hello" + ZWS + " world" — 5 + 1 + 6 = 12 raw chars, 10 visible
    # (the ZWS and the space are both stripped).
    mixed = "hello​ world"
    assert len(mixed) == 12
    assert _visible_text_len(mixed) == 10


def test_visible_text_len_handles_none_and_empty():
    """Defensive — body field may be None or empty string upstream."""
    assert _visible_text_len(None) == 0
    assert _visible_text_len("") == 0


def test_visible_text_len_strips_pure_whitespace():
    """Edge case #8: pure-whitespace body (lots of \\n \\r \\t) — the
    visible-text check must strip standard whitespace too, otherwise
    a whitespace-only HTML body wouldn't trigger the synth path.
    """
    assert _visible_text_len("   \n\n\r\n\t\t   ") == 0


def test_visible_text_len_strips_full_zero_width_range():
    """Spec coverage: every documented invisible range strips to 0.
    Catches regressions if the regex pattern is narrowed by a refactor.
    """
    # U+200B-U+200F (zero-width space + joiner + LTR/RTL marks)
    assert _visible_text_len("".join(chr(c) for c in range(0x200B, 0x2010))) == 0
    # U+2060-U+206F (word joiner + invisible operators + deprecated formatting)
    assert _visible_text_len("".join(chr(c) for c in range(0x2060, 0x2070))) == 0
    # U+FEFF (BOM)
    assert _visible_text_len("﻿﻿﻿") == 0
    # U+034F (combining grapheme joiner — Patreon / Budo signature char)
    assert _visible_text_len("͏" * 50) == 0
    # U+2007 (figure space — common marketing-email pad)
    assert _visible_text_len(" " * 50) == 0


def test_build_markdown_zero_width_padded_body_fires_synth():
    """The Pattern 1 friction case: HTML body padded with invisible
    Unicode chars but containing real signal (alt-text / links).

    Before the fix: `len(body)` after `_strip_html` was ~200 (the strip
    doesn't remove invisible Unicode, only HTML tags), the gate
    `len(body) < 30` failed, synth path was bypassed, the curator got
    a useless body and emitted an Empty Email record.

    After the fix: `_visible_text_len(body)` returns 0, the gate fires,
    the synth path extracts the alt-text + links and the marker.
    """
    # HTML body where the text content is composed entirely of invisible
    # Unicode padding (a Patreon-style email shape). The <img> alt-text
    # and <a href> are the only real signal — image-only synth recovers
    # them via the raw_html fallback.
    padded_invisible = (
        "<p>" + (" ͏" * 100) + "</p>"
        + "<img src='hero.png' alt='Patreon update from Creator X'>"
        + "<a href='https://patreon.com/post/12345'>Read the full post</a>"
    )
    data = {
        "subject": "New post from Creator X",
        "from": "noreply@patreon.com",
        "body": padded_invisible,
    }
    md = _build_markdown(data)
    # The image-only synth marker fires — this was the bug: pre-fix, it
    # did NOT, because `len(stripped_body)` was 200 (invisible padding
    # preserved by `_strip_html`), bypassing the `< 30` gate.
    assert _SYNTH_MARKER in md
    assert "Patreon update from Creator X" in md
    assert "https://patreon.com/post/12345" in md


def test_build_markdown_short_real_text_does_not_over_fire_synth():
    """Regression guard for Part A: a body with 50 chars of real,
    visible text (above the 30-char threshold) must NOT trigger the
    synth path. The visible-text gate must measure correctly, not just
    return 0 for everything.
    """
    body_50_chars = (
        "<p>This is a real short message of fifty chars ok.</p>"
    )
    # ``_visible_text_len`` on the stripped form is well above 30
    data = {
        "subject": "Quick note",
        "from": "alice@example.com",
        "body": body_50_chars,
    }
    md = _build_markdown(data)
    assert _SYNTH_MARKER not in md
    assert _UPSTREAM_TRUNCATED_MARKER not in md
    assert "real short message of fifty chars" in md


def test_build_markdown_zero_width_padded_logs_synthesis_event():
    """Log-emission discipline (per feedback_log_emission_test_pattern.md):
    when the zero-width-padding case triggers the synth, the
    ``webhook.body_synthesized_from_headers`` log event MUST fire with
    the new ``visible_len`` field set to 0 (operator-grep-able signal
    that distinguishes Pattern 1 from the original image-only case).
    """
    padded_invisible = (
        "<p>" + (" ͏" * 100) + "</p>"
        + "<img alt='Test alt'>"
        + "<a href='https://example.com'>Link</a>"
    )
    data = {
        "subject": "Test",
        "from": "x@y.z",
        "body": padded_invisible,
    }
    with structlog.testing.capture_logs() as captured:
        _build_markdown(data)
    matches = [
        c for c in captured
        if c.get("event") == "webhook.body_synthesized_from_headers"
    ]
    assert len(matches) == 1
    entry = matches[0]
    # The new visible_len field — Pattern 1 signature is "stripped_len
    # large, visible_len << stripped_len" so the operator can grep this
    # specific shape (visible text well under threshold despite raw
    # length being above). For this fixture: the strip extracts only
    # the anchor text "Link" (4 chars), leaving ~200 invisible chars
    # surrounding it; visible_len strips those down to 4.
    assert "visible_len" in entry
    assert entry["visible_len"] < 30
    # stripped_len is the raw post-strip length (still 200+ of invisible
    # chars + the 4-char anchor text) — confirms the strip itself didn't
    # touch the invisible padding, only the new visible-text gate did.
    assert entry["stripped_len"] > 30
    # The gap between stripped_len and visible_len is the Pattern 1
    # signature: the invisible-padding mass.
    assert entry["stripped_len"] - entry["visible_len"] > 100


# =============================================================================
# 2026-05-21 Empty-Email-synth bypass — Part B (subject-only synth)
# =============================================================================
#
# Pattern 2 (7/24 = 29%): n8n sends ONLY subject + account label upstream
# of Alfred. No body, no raw_html, no sender headers. Confirmed via inbox
# file inspection — total file size was 151 bytes (headers + empty body).
#
# Fix: when ``_visible_text_len(body) == 0`` AND ``raw_html`` is empty,
# emit a minimal synth from whatever survived (subject, from_addr, account)
# carrying the ``[upstream-truncated; body lost before Alfred reception]``
# marker. Distinct from Part A's image-only marker so the operator can
# grep each pattern's tail separately.
#
# Judgment call (flagged in the builder report): the dispatch prompt
# suggested the gate be ``_visible_text_len(body) < _MIN_BODY_CHARS`` to
# mirror Part A. The implementation tightens it to ``== 0`` to avoid
# over-firing on legitimate short plain-text bodies ("thanks", "ok",
# "approved"). Pattern 2 is specifically "n8n sent NO body"; a content-
# bearing short body is not upstream-truncated and shouldn't carry the
# truncated marker.


def test_synthesize_minimal_from_subject_full_headers():
    """Standard Pattern 2 shape: subject + from + account all present."""
    synth = _synthesize_minimal_from_subject(
        subject="Boost Your Mobile Studio",
        from_addr="news@example.com",
        account="live",
    )
    assert synth is not None
    assert synth.startswith(_UPSTREAM_TRUNCATED_MARKER)
    assert "Subject: Boost Your Mobile Studio" in synth
    assert "From: news@example.com" in synth
    assert "Account: live" in synth


def test_synthesize_minimal_from_subject_missing_sender():
    """Edge case from the brief: n8n dropped the sender too. Synth still
    fires (subject + account are enough), but the From line carries a
    grep-able ``unknown — n8n dropped sender`` signal so the operator
    can spot the worst-case truncation tail.
    """
    synth = _synthesize_minimal_from_subject(
        subject="Daily Digest",
        from_addr="",
        account="live",
    )
    assert synth is not None
    assert _UPSTREAM_TRUNCATED_MARKER in synth
    assert "Subject: Daily Digest" in synth
    assert "From: unknown — n8n dropped sender" in synth
    assert "Account: live" in synth


def test_synthesize_minimal_returns_none_when_nothing_survived():
    """Defensive: subject + from + account all empty → nothing to
    synthesize from. Caller falls back to empty body and emits the
    ``webhook.body_synthesis_upstream_no_signal`` log so the operator
    can grep this terminal-truncation shape.
    """
    synth = _synthesize_minimal_from_subject(
        subject="", from_addr="", account="",
    )
    assert synth is None


def test_build_markdown_empty_body_empty_raw_html_fires_subject_only_synth():
    """The Pattern 2 friction case: n8n sent subject + account but no body.

    Before the fix: body was empty, raw_html was empty (no `<` in body),
    the image-only synth gate (`raw_html AND len(body) < 30`) didn't
    fire because raw_html was absent. Result: an Empty Email record
    with just headers and a blank body.

    After the fix: a second synth path fires when body is truly empty
    AND raw_html is absent. The subject-only synth emits the
    ``[upstream-truncated; body lost before Alfred reception]`` marker
    so the curator + distiller can distinguish this from a legitimate
    empty email.
    """
    data = {
        "subject": "Boost Your Mobile Studio",
        "from": "news@boostmobilestudio.com",
        "body": "",  # n8n dropped it
        "account": "live",
    }
    md = _build_markdown(data)
    assert _UPSTREAM_TRUNCATED_MARKER in md
    assert _SYNTH_MARKER not in md  # not the image-only marker
    assert "Subject: Boost Your Mobile Studio" in md
    assert "From: news@boostmobilestudio.com" in md
    assert "Account: live" in md


def test_build_markdown_empty_body_with_dropped_sender_fires_subject_only_synth():
    """Worst-case Pattern 2: n8n dropped body AND sender. Subject-only
    synth still fires with the unknown-sender marker.
    """
    data = {
        "subject": "Some Subject",
        "from": "",
        "body": "",
        "account": "live",
    }
    md = _build_markdown(data)
    assert _UPSTREAM_TRUNCATED_MARKER in md
    assert "Subject: Some Subject" in md
    assert "From: unknown — n8n dropped sender" in md
    assert "Account: live" in md


def test_build_markdown_empty_body_logs_upstream_truncated_event():
    """Log-emission discipline: the Part B path emits
    ``webhook.body_synthesized_upstream_truncated`` with operator-grep-
    able fields. Distinct event name from the image-only path so the
    operator can count each pattern separately.
    """
    data = {
        "subject": "Boost Your Mobile Studio",
        "from": "news@boostmobilestudio.com",
        "body": "",
        "account": "live",
    }
    with structlog.testing.capture_logs() as captured:
        _build_markdown(data)
    matches = [
        c for c in captured
        if c.get("event") == "webhook.body_synthesized_upstream_truncated"
    ]
    assert len(matches) == 1
    entry = matches[0]
    assert entry["from_addr"] == "news@boostmobilestudio.com"
    assert entry["subject"] == "Boost Your Mobile Studio"
    assert entry["account"] == "live"
    assert entry["body_len"] == 0
    assert entry["visible_len"] == 0
    assert entry["synth_len"] > 0


def test_build_markdown_short_plain_text_body_does_not_fire_upstream_synth():
    """Regression guard for Part B: a legitimate short plain-text body
    like "thanks" or "ok" (6 chars, no HTML, well under 30) must NOT
    trigger the upstream-truncated synth path.

    Judgment call (flagged): the dispatch prompt suggested the gate be
    ``_visible_text_len(body) < _MIN_BODY_CHARS`` (i.e., < 30). That
    would have over-fired on this case — "thanks" would be marked
    "upstream-truncated" even though it's a real email. The
    implementation uses ``== 0`` instead to gate strictly on
    "absolutely no visible content survived upstream."
    """
    data = {
        "subject": "Re: lunch?",
        "from": "alice@example.com",
        "body": "thanks!",
        "account": "live",
    }
    md = _build_markdown(data)
    assert _UPSTREAM_TRUNCATED_MARKER not in md
    assert _SYNTH_MARKER not in md
    assert "thanks!" in md


def test_build_markdown_invisible_only_body_no_html_fires_upstream_synth():
    """Pattern 1 + Pattern 2 corner case: body is pure invisible-Unicode
    padding (no real text, no HTML markers). raw_html is empty (no `<`),
    `_visible_text_len(body) == 0`. Should route to Part B subject-only
    synth, not Part A image-only synth (no raw_html to extract from).
    """
    data = {
        "subject": "Test Subject",
        "from": "x@y.z",
        "body": " ͏​" * 50,  # 150 invisible chars
        "account": "live",
    }
    md = _build_markdown(data)
    assert _UPSTREAM_TRUNCATED_MARKER in md
    assert _SYNTH_MARKER not in md  # no raw_html → image-only synth can't fire


def test_build_markdown_empty_body_no_signal_at_all_logs_no_signal():
    """Terminal Pattern 2: body empty, raw_html empty, AND subject /
    from / account all empty. Nothing survived upstream at all.
    ``_synthesize_minimal_from_subject`` returns None, body stays
    empty, ``webhook.body_synthesis_upstream_no_signal`` log fires.

    Per ``feedback_intentionally_left_blank.md`` — silence on this
    path would be indistinguishable from "no traffic"; the explicit
    no-signal log makes the terminal-truncation case grep-able.
    """
    data = {
        "subject": "",
        "from": "",
        "body": "",
        "account": "",
    }
    with structlog.testing.capture_logs() as captured:
        md = _build_markdown(data)
    # No synth marker (nothing to synthesize from).
    assert _UPSTREAM_TRUNCATED_MARKER not in md
    assert _SYNTH_MARKER not in md
    # The no-signal event fires so the operator can grep this case.
    no_signal = [
        c for c in captured
        if c.get("event") == "webhook.body_synthesis_upstream_no_signal"
    ]
    assert len(no_signal) == 1


def test_build_markdown_does_not_double_synth():
    """Regression guard: a body that already triggers Part A (raw_html
    present + visible text below threshold) must NOT also trigger
    Part B. The ``if/elif`` branch structure makes this true; the test
    pins it explicitly so a future refactor that splits the branches
    can't accidentally cause both markers to appear.
    """
    data = {
        "subject": "x",
        "from": "y@z.w",
        "body": "<img alt='hi'><a href='https://example.com'>link</a>",
    }
    md = _build_markdown(data)
    # Part A fires (image-only synth)
    assert _SYNTH_MARKER in md
    # Part B does NOT fire (Part A already handled it)
    assert _UPSTREAM_TRUNCATED_MARKER not in md
