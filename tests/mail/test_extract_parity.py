"""Cross-path parity tests — webhook vs fetcher (P12 / Ship 2 — 2026-06-07).

The durable guard against future drift between the webhook and fetcher
empty-body paths. For equivalent input, the body content below the
``---`` separator in ``webhook._build_markdown(data)`` and
``fetcher._build_markdown(msg, account)`` MUST match byte-for-byte.

Header lines above the ``---`` differ in shape (fetcher always emits
Message-ID / In-Reply-To / References when present; webhook's
payload-dict path is conditional). The parity check focuses on the
body content where synth fires — that's the operator-visible
contract Ship 4's SKILL updates will reference.

Three fixture shapes — the documented failure modes from the
empty-body arc design:
    1. Image-only HTML — Pizza Hut-style marketing template
    2. Invisible-Unicode padded HTML — Patreon-style padded body
    3. Upstream-truncated — empty plain + no html, headers only

Plus three standalone contract pins so Ship 4 (SKILL updates
referencing the markers) can rely on stable byte-strings.
"""

from __future__ import annotations

import email
import email.policy
from email.message import EmailMessage

import pytest

from alfred.mail import extract, webhook
from alfred.mail.fetcher import _build_markdown as fetcher_build


# --- Fixture builders -----------------------------------------------------


def _body_after_separator(md: str) -> str:
    """Extract the body content below the ``---`` header / body delimiter.

    Both paths use ``\\n---\\n`` as the delimiter between headers and
    body so the split logic works for both webhook and fetcher output.
    """
    parts = md.split("\n---\n", 1)
    return parts[1].lstrip("\n") if len(parts) == 2 else ""


def _make_fetcher_email(
    *,
    subject: str,
    from_addr: str,
    plain: str | None,
    html: str | None,
) -> EmailMessage:
    """Build an EmailMessage for fetcher path.

    The headers (To, Date) are uniform across both paths so they don't
    affect the body output the parity check inspects.
    """
    msg = EmailMessage(policy=email.policy.default)
    if subject:
        msg["Subject"] = subject
    if from_addr:
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


def _webhook_data(
    *,
    subject: str,
    from_addr: str,
    body: str,
    account: str = "live",
) -> dict:
    """Build a webhook payload dict equivalent to the fetcher email.

    The webhook path receives a single ``body`` field rather than a
    separate plain / html part; production webhook senders (n8n) post
    the HTML in ``body`` when an HTML message arrives. So for parity:
        * Image-only HTML / invisible-padded HTML cases → ``body=html``
        * Upstream-truncated case → ``body=""``

    The webhook's ``_build_markdown`` does its own HTML detection
    (``"<" in body``); this mirrors the production payload shape.
    """
    return {
        "subject": subject,
        "from": from_addr,
        "to": "andrew@example.com",
        "date": "Mon, 7 Jun 2026 10:00:00 +0000",
        "account": account,
        "body": body,
    }


# --- Fixture 1: Image-only HTML -------------------------------------------


def _pizza_hut_image_only_html() -> str:
    """Image-only marketing email HTML (Pizza Hut shape).

    Identical fixture used in ``tests/test_webhook_image_only.py`` for
    the webhook side and ``tests/mail/test_fetcher_synth.py`` for the
    fetcher side. Centralising it here would create a circular import
    between the test files; the duplication is intentional + small.
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


def test_parity_image_only_html() -> None:
    """Image-only HTML produces byte-equivalent bodies on both paths."""
    html = _pizza_hut_image_only_html()
    subject = "Your Pizza Hut order"
    from_addr = "orders@pizzahut.com"
    account = "live"

    webhook_md = webhook._build_markdown(_webhook_data(
        subject=subject, from_addr=from_addr, body=html, account=account,
    ))
    fetcher_md = fetcher_build(
        _make_fetcher_email(
            subject=subject, from_addr=from_addr, plain=None, html=html,
        ),
        account,
    )

    webhook_body = _body_after_separator(webhook_md)
    fetcher_body = _body_after_separator(fetcher_md)
    assert webhook_body == fetcher_body, (
        f"Image-only HTML body parity broken:\n"
        f"WEBHOOK BODY:\n{webhook_body!r}\n\n"
        f"FETCHER BODY:\n{fetcher_body!r}"
    )
    # Both should carry the image-only synth marker.
    assert extract.SYNTH_MARKER_IMAGE_ONLY in webhook_body
    assert extract.SYNTH_MARKER_IMAGE_ONLY in fetcher_body


# --- Fixture 2: Invisible-Unicode padded HTML -----------------------------


def _patreon_invisible_padded_html() -> str:
    """Patreon-style HTML padded with U+2007 figure spaces.

    The padding makes ``len(body)`` look like 200+ chars but
    ``visible_text_len`` strips to 0. The alt-text + link signal
    survives the strip-and-synth path.
    """
    padding = " " * 200  # U+2007 figure space
    return (
        f'<html><body><p>{padding}</p>'
        '<img src="hero.png" alt="Patreon supporter exclusive content">'
        '<a href="https://patreon.com/post/123"><img alt="Read on Patreon"></a>'
        '</body></html>'
    )


def test_parity_invisible_padded_html() -> None:
    """Invisible-Unicode padded HTML produces byte-equivalent bodies."""
    html = _patreon_invisible_padded_html()
    subject = "Supporter post"
    from_addr = "noreply@patreon.com"
    account = "live"

    webhook_md = webhook._build_markdown(_webhook_data(
        subject=subject, from_addr=from_addr, body=html, account=account,
    ))
    fetcher_md = fetcher_build(
        _make_fetcher_email(
            subject=subject, from_addr=from_addr, plain=None, html=html,
        ),
        account,
    )

    webhook_body = _body_after_separator(webhook_md)
    fetcher_body = _body_after_separator(fetcher_md)
    assert webhook_body == fetcher_body, (
        f"Invisible-padded HTML body parity broken:\n"
        f"WEBHOOK BODY:\n{webhook_body!r}\n\n"
        f"FETCHER BODY:\n{fetcher_body!r}"
    )
    assert extract.SYNTH_MARKER_IMAGE_ONLY in webhook_body
    assert extract.SYNTH_MARKER_IMAGE_ONLY in fetcher_body


# --- Fixture 3: Upstream-truncated (empty body, headers only) -------------


def test_parity_upstream_truncated() -> None:
    """Empty body + populated headers produces byte-equivalent synth."""
    subject = "Newsletter blast"
    from_addr = "news@example.com"
    account = "live"

    webhook_md = webhook._build_markdown(_webhook_data(
        subject=subject, from_addr=from_addr, body="", account=account,
    ))
    msg = EmailMessage(policy=email.policy.default)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = "andrew@example.com"
    msg["Date"] = "Mon, 7 Jun 2026 10:00:00 +0000"
    fetcher_md = fetcher_build(msg, account)

    webhook_body = _body_after_separator(webhook_md)
    fetcher_body = _body_after_separator(fetcher_md)
    assert webhook_body == fetcher_body, (
        f"Upstream-truncated body parity broken:\n"
        f"WEBHOOK BODY:\n{webhook_body!r}\n\n"
        f"FETCHER BODY:\n{fetcher_body!r}"
    )
    assert extract.SYNTH_MARKER_UPSTREAM_TRUNCATED in webhook_body
    assert extract.SYNTH_MARKER_UPSTREAM_TRUNCATED in fetcher_body


# --- Contract pins for Ship 4 SKILL references ----------------------------


def test_synth_markers_match_byte_for_byte_across_paths() -> None:
    """Ship 4 will quote these markers in SKILLs; byte stability matters.

    Pin the exact byte values so a future refactor that "improves the
    wording" of either marker breaks this test before the SKILL
    documentation drifts out of sync.
    """
    assert extract.SYNTH_MARKER_IMAGE_ONLY == (
        "[image-only HTML; body synthesized from headers]"
    )
    assert extract.SYNTH_MARKER_UPSTREAM_TRUNCATED == (
        "[upstream-truncated; body lost before Alfred reception]"
    )


def test_no_synth_paths_match() -> None:
    """Substantive HTML body produces byte-equivalent body across paths.

    Negative parity: when no synth fires, the strip path on both sides
    should produce identical output. This pins the strip equivalence
    independently of the synth equivalence.
    """
    html = (
        "<html><body>"
        "<p>This is a long substantive HTML body that has well over thirty "
        "characters of visible text content after stripping all the tags.</p>"
        "</body></html>"
    )
    subject = "A real message"
    from_addr = "real@example.com"
    account = "live"

    webhook_md = webhook._build_markdown(_webhook_data(
        subject=subject, from_addr=from_addr, body=html, account=account,
    ))
    fetcher_md = fetcher_build(
        _make_fetcher_email(
            subject=subject, from_addr=from_addr, plain=None, html=html,
        ),
        account,
    )
    webhook_body = _body_after_separator(webhook_md)
    fetcher_body = _body_after_separator(fetcher_md)
    assert webhook_body == fetcher_body
    # No synth markers in either output.
    assert extract.SYNTH_MARKER_IMAGE_ONLY not in webhook_body
    assert extract.SYNTH_MARKER_IMAGE_ONLY not in fetcher_body


def test_min_body_chars_threshold_matches() -> None:
    """Both paths use the same MIN_BODY_CHARS threshold (regression pin).

    If a future drift moves either path off the shared
    ``extract.MIN_BODY_CHARS`` constant, the parity guarantee for
    boundary cases (visible_len == 29 vs visible_len == 30) would
    break silently. The constant is the single source of truth for
    both paths; pin that both paths reference the same value.
    """
    # webhook.py aliases extract.MIN_BODY_CHARS as _MIN_BODY_CHARS
    # for back-compat with existing tests; the aliased value must
    # be the same object.
    assert webhook._MIN_BODY_CHARS == extract.MIN_BODY_CHARS
    # fetcher.py reads extract.MIN_BODY_CHARS directly — no alias to
    # verify, but pin the value so a future MIN_BODY_CHARS change is
    # caught.
    assert extract.MIN_BODY_CHARS == 30
