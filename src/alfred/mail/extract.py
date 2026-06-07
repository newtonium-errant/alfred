"""Empty-body detection + synthesis primitives — shared by webhook + fetcher.

History: lifted from ``mail.webhook`` on 2026-06-07 (P11 Ship 1, refactor-
only — zero behavior change) so the IMAP fetcher path (Ship 2) can apply
the same empty-body bifurcation logic that the webhook path already does.

Three prior empty-body fixes (commits 5968127, c4e1247, d4fd77e — 2026-04
through 2026-05) all landed in webhook.py. Salem's deployed config uses
IMAP via fetcher.py — the inactive path for those fixes. Ship 2 will give
fetcher.py parity by importing from this module. Ship 3 already shipped
(curator inbox-stage preference filter).

Public surface (no leading underscore) because:
  1. The fetcher consumer needs to import them
  2. Ship 4 will reference the marker strings in curator + distiller
     SKILLs — operator-visible documentation needs stable names
  3. The inline audit comments below preserve the history of WHY each
     primitive exists; the webhook-pipeline audit-block at the top of
     ``webhook.py`` covers the surrounding pipeline context
"""

from __future__ import annotations

import re


def strip_html(html: str) -> str:
    """Convert HTML email body to readable plain text."""
    # Remove style/script blocks entirely
    text = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Convert <br>, <p>, <div>, <tr>, <li> to newlines for readability
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common HTML entities
    text = text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    # Collapse whitespace within lines, preserve line breaks
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    # Collapse multiple blank lines into one
    result = []
    prev_blank = False
    for line in lines:
        if not line:
            if not prev_blank:
                result.append("")
            prev_blank = True
        else:
            result.append(line)
            prev_blank = False
    return "\n".join(result).strip()


# 2026-05-18 — image-only HTML bifurcation. Per Queue #11, the curator
# was generating "Empty Email" records for senders whose emails are
# pure image templates (marketing newsletters, transactional confirms
# rendered as a single PNG). The HTML-strip pipeline correctly produced
# an empty body — that part is working as intended — but the operator-
# visible record carried no signal about WHY the body was empty, so
# the distiller couldn't distinguish "image-only sender (legitimate
# source-content limitation)" from "pipeline truncation bug." The
# downstream consequence: 30+ per-sender "Empty-Record Sender"
# syntheses generated as a compensation cycle.
#
# Fix: when post-strip body falls below ``MIN_BODY_CHARS``, attempt
# to synthesize a body from the raw HTML headers (``<img alt="…">``
# alt-text + first ``MAX_LINKS_IN_SYNTH`` ``<a href="…">`` anchors).
# The synthesized body carries an explicit ``[image-only HTML; body
# synthesized from headers]`` marker so the curator (and any downstream
# grep) can distinguish image-only from bug-truncated.
MIN_BODY_CHARS = 30
MAX_ALT_TEXTS_IN_SYNTH = 8
MAX_LINKS_IN_SYNTH = 5
SYNTH_MARKER_IMAGE_ONLY = "[image-only HTML; body synthesized from headers]"
# 2026-05-21 — Empty-Email-synth bypass fix. Two new markers for the
# remaining bifurcation tail that the image-only synth (above) missed:
#   * Pattern 1 (71% of post-2026-05-18 Empty Email records): senders
#     pad bodies with hundreds of invisible Unicode chars (U+2007
#     figure space, U+034F combining grapheme joiner, U+200B-U+200F /
#     U+2060-U+206F zero-width range, U+FEFF BOM). `len(body)` returns
#     hundreds so the original `len(body) < MIN_BODY_CHARS` gate at
#     line 320 never fires — the synth path is bypassed and the curator
#     sees garbage. Fix: measure VISIBLE text length (strip invisibles
#     first) before checking the threshold.
#   * Pattern 2 (29%): n8n sends ONLY subject + account label upstream
#     of Alfred — no body, no raw_html, no sender. Image-only synth
#     can't fire (raw_html is absent). Fix: a separate subject-only
#     synth path emits a marker so the curator + distiller can
#     distinguish "n8n sent partial" from "legitimate empty email."
SYNTH_MARKER_UPSTREAM_TRUNCATED = "[upstream-truncated; body lost before Alfred reception]"
# Pattern 1's invisible-Unicode set. Each char class documented
# explicitly so future maintainers don't have to decode the literal
# Unicode bytes:
#   \s          — all standard whitespace (space, tab, newline, etc.)
#          — non-breaking space
#   ͏      — combining grapheme joiner (Patreon, Budo Brothers signature pad)
#   ​-‏ — zero-width space + zero-width non-/joiner + LTR/RTL marks
#          — figure space (common in image-only marketing emails)
#    -  — line/paragraph separators
#   ‪-‮ — bidirectional embedding marks
#   ⁠-⁯ — word joiner + invisible operators + deprecated formatting
#   　      — ideographic space
#   ﻿      — BOM / zero-width no-break space
INVISIBLE_CHARS_RE = re.compile(
    r"[\s ͏​-‏  - ‪-‮⁠-⁯　﻿]+"
)
_IMG_ALT_RE = re.compile(
    r"<img\b[^>]*\balt\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
    flags=re.IGNORECASE,
)
_A_HREF_RE = re.compile(
    r"<a\b[^>]*\bhref\s*=\s*(?:\"([^\"]*)\"|'([^']*)')[^>]*>(.*?)</a>",
    flags=re.IGNORECASE | re.DOTALL,
)

def visible_text_len(body: str | None) -> int:
    """Length of ``body`` after stripping whitespace + zero-width chars.

    Used by the synth gate to detect the empty-marketing-email pattern
    where senders pad bodies with hundreds of invisible Unicode chars
    (U+2007 figure space, U+034F combining grapheme joiner, U+200B-200F
    zero-width range, etc.) that defeat HTML-to-text extraction but
    bypass the bare ``len(body) < threshold`` gate.

    Empty Email records from Patreon, Budo Brothers, etc. all hit this
    pattern — 17 of 24 post-2026-05-18 records (71%) have bodies
    composed entirely of invisible Unicode padding that ``len()`` sees
    as 200+ chars but a human reads as completely empty.

    Returns 0 for a body that is None / empty / pure whitespace / pure
    invisible padding; returns the count of visible code-points
    otherwise.
    """
    if not body:
        return 0
    return len(INVISIBLE_CHARS_RE.sub("", body))

def synthesize_minimal_from_subject(
    *,
    subject: str,
    from_addr: str,
    account: str,
) -> str | None:
    """Build a minimal synth body for the upstream-truncated case.

    Pattern 2 from the Empty-Email-synth fix: n8n sends subject + account
    label but no body, no raw_html, no sender headers (or only some
    subset). The image-only-HTML synth above can't fire because there's
    no HTML to extract alt-text / links from. This fallback emits a
    grep-able marker (``[upstream-truncated; body lost before Alfred
    reception]``) plus whatever headers DO survive, so the operator can
    grep ``upstream-truncated`` to count the n8n-dropped-body tail and
    the curator can distinguish "n8n sent partial" from "legitimate
    empty email."

    Returns ``None`` when subject AND from_addr AND account are all
    absent — there is no signal to synthesize from. Caller falls back
    to empty body.
    """
    if not subject and not from_addr and not account:
        return None
    lines: list[str] = [
        SYNTH_MARKER_UPSTREAM_TRUNCATED,
        "",
    ]
    if subject:
        lines.append(f"Subject: {subject}")
    if from_addr:
        lines.append(f"From: {from_addr}")
    else:
        lines.append("From: unknown — n8n dropped sender")
    if account:
        lines.append(f"Account: {account}")
    return "\n".join(lines).rstrip()

def extract_alt_texts(html: str) -> list[str]:
    """Return non-empty alt-text strings from ``<img alt="…">`` tags.

    Trims whitespace, de-duplicates while preserving order, drops empty
    strings (`alt=""` is common for spacer images and carries no signal).
    """
    seen: set[str] = set()
    out: list[str] = []
    for m in _IMG_ALT_RE.finditer(html):
        alt = (m.group(1) or m.group(2) or "").strip()
        if not alt:
            continue
        # Decode the same minimal entity set the strip path handles
        # so synthesized text reads the same way the body would have.
        alt = (
            alt.replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
        )
        if alt in seen:
            continue
        seen.add(alt)
        out.append(alt)
    return out

def extract_links(html: str) -> list[tuple[str, str]]:
    """Return ``(href, anchor_text)`` pairs from ``<a href="…">…</a>`` tags.

    Anchor text is HTML-stripped (image-only anchors are common — the
    anchor wraps an ``<img>`` and has no direct text). De-duped on URL.
    Empty hrefs and ``javascript:`` / ``mailto:`` are skipped — they
    don't carry useful destination signal for the curator.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for m in _A_HREF_RE.finditer(html):
        href = (m.group(1) or m.group(2) or "").strip()
        if not href:
            continue
        lower = href.lower()
        if lower.startswith("javascript:") or lower.startswith("mailto:"):
            continue
        if href in seen:
            continue
        seen.add(href)
        # Strip tags + collapse whitespace in the anchor body — the
        # raw inner-HTML can contain images, spans, formatting tags
        # whose text content is the only thing the operator cares about.
        anchor_html = m.group(3) or ""
        anchor_text = re.sub(r"<[^>]+>", " ", anchor_html)
        anchor_text = re.sub(r"\s+", " ", anchor_text).strip()
        out.append((href, anchor_text))
    return out

def synthesize_body_from_headers(
    html: str,
    *,
    subject: str,
    from_addr: str,
) -> str | None:
    """Build a body from HTML headers when the strip produced no text.

    Returns ``None`` when the HTML carries neither alt-text nor a usable
    link — in that case there's nothing to synthesize from and the
    caller should fall back to the empty body (operator + curator still
    see the headers above the ``---`` separator).

    The returned string is the synthesized body content (without the
    headers above ``---`` — those are added by ``_build_markdown``).
    Always starts with the synth marker so a grep for ``image-only HTML``
    or ``body synthesized from headers`` surfaces every record that
    went through this path.
    """
    alt_texts = extract_alt_texts(html)[:MAX_ALT_TEXTS_IN_SYNTH]
    links = extract_links(html)[:MAX_LINKS_IN_SYNTH]
    if not alt_texts and not links:
        return None

    lines: list[str] = [SYNTH_MARKER_IMAGE_ONLY, ""]
    # Echo subject + from at the top of the synth body so the curator
    # has a complete short-form description even if it skipped the
    # markdown header lines (unlikely but defensive).
    if subject:
        lines.append(f"Subject: {subject}")
    if from_addr:
        lines.append(f"From: {from_addr}")
    if subject or from_addr:
        lines.append("")
    if alt_texts:
        lines.append("Image alt-text:")
        for alt in alt_texts:
            lines.append(f"- {alt}")
        lines.append("")
    if links:
        lines.append("Links:")
        for href, anchor in links:
            if anchor:
                lines.append(f"- {anchor}: {href}")
            else:
                lines.append(f"- {href}")
    return "\n".join(lines).rstrip()


__all__ = [
    "INVISIBLE_CHARS_RE",
    "MAX_ALT_TEXTS_IN_SYNTH",
    "MAX_LINKS_IN_SYNTH",
    "MIN_BODY_CHARS",
    "SYNTH_MARKER_IMAGE_ONLY",
    "SYNTH_MARKER_UPSTREAM_TRUNCATED",
    "extract_alt_texts",
    "extract_links",
    "strip_html",
    "synthesize_body_from_headers",
    "synthesize_minimal_from_subject",
    "visible_text_len",
]
