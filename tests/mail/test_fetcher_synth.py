"""Synth-path tests for alfred.mail.fetcher (P12 / Ship 2 — 2026-06-07).

Drives the fetcher path through synthetic ``EmailMessage`` objects.
Each test asserts:
    (a) Body content after the ``---`` separator matches the expected
        marker / no-marker contract.
    (b) The expected ``fetcher.*`` log event fires with the right
        structured fields (via ``structlog.testing.capture_logs``).

Mirrors the webhook synth coverage at
``tests/test_webhook_image_only.py``; the byte-equivalence cross-check
between the two paths lives in ``tests/mail/test_extract_parity.py``.

The ``EmailMessage`` fixture builder mirrors the wire shape that
``email.message_from_bytes(..., policy=email.policy.default)`` produces
in the live IMAP fetch path — that's the actual production input
shape, so the fixture helpers exercise the same routing.
"""

from __future__ import annotations

import email
import email.policy
from email.message import EmailMessage

import structlog

from alfred.mail import extract
from alfred.mail.fetcher import _build_markdown, _extract_text


# --- Fixture builders -----------------------------------------------------


def _make_email(
    *,
    subject: str = "Test Subject",
    from_addr: str = "sender@example.com",
    plain: str | None = None,
    html: str | None = None,
) -> EmailMessage:
    """Build an ``EmailMessage`` with the requested parts.

    Routing rules:
        * Both ``plain`` and ``html`` provided → multipart/alternative
        * Only ``plain`` → text/plain singlepart
        * Only ``html`` → text/html singlepart
        * Neither → headers-only (no body parts at all)

    All emails get standard headers (To, Date) so the markdown output
    has uniform shape.
    """
    msg = EmailMessage(policy=email.policy.default)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = "andrew@example.com"
    msg["Date"] = "Mon, 7 Jun 2026 10:00:00 +0000"
    if plain is not None and html is not None:
        msg.set_content(plain)
        msg.add_alternative(html, subtype="html")
    elif plain is not None:
        msg.set_content(plain)
    elif html is not None:
        msg.set_content(html, subtype="html")
    return msg


def _body_after_separator(md: str) -> str:
    """Return the body content below the ``---`` header / body delimiter."""
    parts = md.split("\n---\n", 1)
    return parts[1].lstrip("\n") if len(parts) == 2 else ""


# --- _extract_text returns a tuple ----------------------------------------


def test_extract_text_returns_tuple_for_plain_substantive() -> None:
    """Plain-text body above threshold → ``(body, "")`` — fast path."""
    msg = _make_email(
        plain="This is a substantive plain-text body that goes well above the "
              "MIN_BODY_CHARS threshold of 30 chars.",
    )
    body, raw_html = _extract_text(msg)
    assert raw_html == ""
    assert body.startswith("This is a substantive")
    assert extract.visible_text_len(body) >= extract.MIN_BODY_CHARS


def test_extract_text_returns_tuple_for_html() -> None:
    """HTML-only path → ``(stripped, raw_html)``."""
    html = "<html><body><p>HTML body text content here lasting many chars.</p></body></html>"
    msg = _make_email(plain=None, html=html)
    body, raw_html = _extract_text(msg)
    assert raw_html != ""
    # raw_html preserves the original HTML for the synth fallback.
    assert "<p>" in raw_html
    # body is the stripped form.
    assert "<p>" not in body


def test_extract_text_returns_empty_tuple_for_empty_message() -> None:
    """No plain part AND no html part → ``("", "")``."""
    msg = EmailMessage(policy=email.policy.default)
    msg["Subject"] = "Empty"
    msg["From"] = "sender@example.com"
    msg["To"] = "andrew@example.com"
    msg["Date"] = "Mon, 7 Jun 2026 10:00:00 +0000"
    body, raw_html = _extract_text(msg)
    assert body == ""
    assert raw_html == ""


def test_extract_text_plain_preheader_only_falls_through_to_html() -> None:
    """Plain-text below threshold + HTML present → HTML wins.

    This is the core fix — pre-P12, a preheader teaser in the plain
    part ("View in browser") bypassed the HTML synth gate because the
    plain text was returned unconditionally. The new flow applies the
    visible-len gate to the plain path and falls through to HTML on
    short content.
    """
    msg = _make_email(
        plain="View in browser",  # 15 chars; under 30
        html="<html><body><p>Substantive HTML body content here</p></body></html>",
    )
    body, raw_html = _extract_text(msg)
    # The HTML path wins because the plain part was too short.
    assert raw_html != ""
    assert "Substantive HTML body content here" in body


# --- _build_markdown — substantive paths (no synth) -----------------------


def test_plain_text_substantive_no_synth() -> None:
    """Plain body with >30 visible chars → no synth, no log fires."""
    msg = _make_email(
        plain="This is a substantive plain-text body well above MIN_BODY_CHARS.",
    )
    with structlog.testing.capture_logs() as captured:
        md = _build_markdown(msg, account="live")
    body = _body_after_separator(md)
    # No synth markers in the output.
    assert extract.SYNTH_MARKER_IMAGE_ONLY not in body
    assert extract.SYNTH_MARKER_UPSTREAM_TRUNCATED not in body
    # No fetcher.body_synth* events fired.
    synth_events = [c for c in captured if c.get("event", "").startswith("fetcher.body_synth")]
    assert synth_events == []


def test_html_substantive_body_no_synth() -> None:
    """HTML body with >30 visible chars after strip → no synth."""
    html = (
        "<html><body>"
        "<p>This is a long substantive HTML body that has well over thirty "
        "characters of visible text content after stripping all the tags.</p>"
        "</body></html>"
    )
    msg = _make_email(plain=None, html=html)
    with structlog.testing.capture_logs() as captured:
        md = _build_markdown(msg, account="live")
    body = _body_after_separator(md)
    assert extract.SYNTH_MARKER_IMAGE_ONLY not in body
    assert extract.SYNTH_MARKER_UPSTREAM_TRUNCATED not in body
    synth_events = [c for c in captured if c.get("event", "").startswith("fetcher.body_synth")]
    assert synth_events == []


# --- _build_markdown — image-only synth path (Pattern 1) -----------------


def _pizza_hut_image_only_html() -> str:
    """Image-only marketing email HTML (Pizza Hut shape).

    Tags + alt-text + tracking-link anchors. After strip the visible
    text content is below threshold, but the alt-text + anchors carry
    enough signal for the synth path to recover something useful.
    """
    return (
        '<div>'
        '<a href="https://pizzahut.com/promo?utm=foo">'
        '<img src="hero.png" alt="Pizza Hut delivery hero image">'
        '</a>'
        '<a href="https://pizzahut.com/track?order=12345">'
        '<img alt="Order #12345 on the way" src="status.png">'
        '</a>'
        '</div>'
    )


def test_html_only_image_only_routes_to_image_synth() -> None:
    """Image-only HTML → ``synthesize_body_from_headers`` fires."""
    msg = _make_email(plain=None, html=_pizza_hut_image_only_html())
    with structlog.testing.capture_logs() as captured:
        md = _build_markdown(msg, account="live")
    body = _body_after_separator(md)
    # Image-only synth marker present.
    assert extract.SYNTH_MARKER_IMAGE_ONLY in body
    # Upstream-truncated marker NOT present (different bifurcation).
    assert extract.SYNTH_MARKER_UPSTREAM_TRUNCATED not in body
    # The synth event fired.
    matches = [
        c for c in captured
        if c.get("event") == "fetcher.body_synthesized_from_headers"
    ]
    assert len(matches) == 1


def test_html_invisible_unicode_padding_routes_to_image_synth() -> None:
    """Invisible-Unicode-padded HTML → visible_text_len gate catches it.

    Patreon / Budo Brothers-style emails pad bodies with hundreds of
    U+2007 / U+034F / U+200B chars that ``len(body)`` sees as 200+
    chars but visible_text_len strips to 0. The synth still fires.
    """
    # 200 chars of U+2007 (figure space) + alt-text.
    padding = " " * 200
    html = (
        f'<html><body><p>{padding}</p>'
        '<img src="hero.png" alt="Patreon supporter exclusive content">'
        '<a href="https://patreon.com/post/123"><img alt="Read on Patreon"></a>'
        '</body></html>'
    )
    msg = _make_email(plain=None, html=html)
    with structlog.testing.capture_logs() as captured:
        md = _build_markdown(msg, account="live")
    body = _body_after_separator(md)
    assert extract.SYNTH_MARKER_IMAGE_ONLY in body
    matches = [
        c for c in captured
        if c.get("event") == "fetcher.body_synthesized_from_headers"
    ]
    assert len(matches) == 1


def test_html_no_alt_no_links_emits_no_signal_log() -> None:
    """HTML with no alt-text and no anchors → ``no_signal`` event.

    ``synthesize_body_from_headers`` returns None because there's
    nothing to synthesize from. Per
    ``feedback_intentionally_left_blank.md``, the empty body must
    still produce a grep-able log so the operator can distinguish
    "synth fired, no signal recovered" from "synth never ran."
    """
    # An HTML that strips to nearly nothing AND has no alt/link signal.
    html = "<html><body><div></div></body></html>"
    msg = _make_email(plain=None, html=html)
    with structlog.testing.capture_logs() as captured:
        md = _build_markdown(msg, account="live")
    body = _body_after_separator(md)
    # No synth markers present because synth returned None.
    assert extract.SYNTH_MARKER_IMAGE_ONLY not in body
    matches = [
        c for c in captured
        if c.get("event") == "fetcher.body_synthesis_no_signal"
    ]
    assert len(matches) == 1


# --- _build_markdown — upstream-truncated synth path (Pattern 2) ----------


def test_empty_message_routes_to_upstream_truncated_synth() -> None:
    """No plain, no HTML, but headers present → upstream-truncated synth."""
    msg = EmailMessage(policy=email.policy.default)
    msg["Subject"] = "Important: empty body"
    msg["From"] = "newsletter@example.com"
    msg["To"] = "andrew@example.com"
    msg["Date"] = "Mon, 7 Jun 2026 10:00:00 +0000"
    with structlog.testing.capture_logs() as captured:
        md = _build_markdown(msg, account="live")
    body = _body_after_separator(md)
    assert extract.SYNTH_MARKER_UPSTREAM_TRUNCATED in body
    assert extract.SYNTH_MARKER_IMAGE_ONLY not in body
    matches = [
        c for c in captured
        if c.get("event") == "fetcher.body_synthesized_upstream_truncated"
    ]
    assert len(matches) == 1


def test_plain_text_preheader_only_with_no_html_routes_to_upstream_truncated() -> None:
    """Short plain ``raw_html=""`` AND visible_text_len > 0 → no synth fires.

    Important distinction from image-only synth: the plain-text fast
    path returns short content as-is (with raw_html=""), and the
    upstream-truncated gate requires ``visible_text_len == 0``. A
    short content-bearing plain text like "thanks" lands as the body
    with no synth — that's the correct behavior per the
    ``feedback_intentionally_left_blank.md`` rationale baked into the
    Pattern 2 gate.
    """
    msg = _make_email(plain="thanks")
    with structlog.testing.capture_logs() as captured:
        md = _build_markdown(msg, account="live")
    body = _body_after_separator(md)
    # Plain text returned as-is — but wait, "thanks" is 6 chars, under
    # MIN_BODY_CHARS=30. With no HTML part to fall through to, _extract_text
    # falls through to the empty path. visible_text_len("") == 0 so the
    # upstream-truncated path fires.
    assert extract.SYNTH_MARKER_UPSTREAM_TRUNCATED in body
    matches = [
        c for c in captured
        if c.get("event") == "fetcher.body_synthesized_upstream_truncated"
    ]
    assert len(matches) == 1


def test_empty_message_no_subject_no_from_emits_no_signal_log() -> None:
    """Empty body + empty subject + empty from + empty account → no signal."""
    msg = EmailMessage(policy=email.policy.default)
    msg["To"] = "andrew@example.com"
    msg["Date"] = "Mon, 7 Jun 2026 10:00:00 +0000"
    with structlog.testing.capture_logs() as captured:
        md = _build_markdown(msg, account="")
    body = _body_after_separator(md)
    # No synth markers — synth returned None.
    assert extract.SYNTH_MARKER_UPSTREAM_TRUNCATED not in body
    matches = [
        c for c in captured
        if c.get("event") == "fetcher.body_synthesis_upstream_no_signal"
    ]
    assert len(matches) == 1


def test_account_threads_to_upstream_truncated_synth() -> None:
    """The ``account`` arg appears in the upstream-truncated synth body."""
    msg = EmailMessage(policy=email.policy.default)
    msg["Subject"] = "Test"
    msg["From"] = "sender@example.com"
    msg["To"] = "andrew@example.com"
    msg["Date"] = "Mon, 7 Jun 2026 10:00:00 +0000"
    md = _build_markdown(msg, account="live")
    body = _body_after_separator(md)
    assert "Account: live" in body


# --- Log event field pins -------------------------------------------------


def test_log_event_fields_for_image_synth() -> None:
    """Pin all fields on ``fetcher.body_synthesized_from_headers``.

    Per ``feedback_log_emission_test_pattern.md`` — assert key fields
    so a future refactor that renames or drops a field is caught at
    test time.
    """
    msg = _make_email(
        subject="Pizza Hut order update",
        from_addr="orders@pizzahut.com",
        plain=None,
        html=_pizza_hut_image_only_html(),
    )
    with structlog.testing.capture_logs() as captured:
        _build_markdown(msg, account="live")
    matches = [
        c for c in captured
        if c.get("event") == "fetcher.body_synthesized_from_headers"
    ]
    assert len(matches) == 1
    event = matches[0]
    assert event["from_addr"] == "orders@pizzahut.com"
    assert event["subject"] == "Pizza Hut order update"
    assert "stripped_len" in event
    assert "visible_len" in event
    assert "synth_len" in event
    assert isinstance(event["stripped_len"], int)
    assert isinstance(event["visible_len"], int)
    assert isinstance(event["synth_len"], int)


def test_log_event_fields_for_upstream_truncated() -> None:
    """Pin all fields on ``fetcher.body_synthesized_upstream_truncated``."""
    msg = EmailMessage(policy=email.policy.default)
    msg["Subject"] = "Newsletter blast"
    msg["From"] = "news@example.com"
    msg["To"] = "andrew@example.com"
    msg["Date"] = "Mon, 7 Jun 2026 10:00:00 +0000"
    with structlog.testing.capture_logs() as captured:
        _build_markdown(msg, account="live")
    matches = [
        c for c in captured
        if c.get("event") == "fetcher.body_synthesized_upstream_truncated"
    ]
    assert len(matches) == 1
    event = matches[0]
    assert event["from_addr"] == "news@example.com"
    assert event["subject"] == "Newsletter blast"
    assert event["account"] == "live"
    assert "body_len" in event
    assert "visible_len" in event
    assert "synth_len" in event
