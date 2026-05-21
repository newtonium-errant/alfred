"""Simple webhook receiver — accepts POSTed email data and writes to vault inbox.

Pipeline truncation audit (2026-05-18, Queue #11 Part B)
========================================================

The brief flagged "suspected character-limit truncation somewhere in
the pipeline" as a separate failure mode adjacent to the image-only
HTML bifurcation. A full per-layer walk found **no truncation point**
in the Alfred-controlled pipeline. Each layer's audit conclusion:

1. **n8n Outlook Trigger** — Microsoft Graph ``body.content`` field is
   returned in full (the truncation-prone field is ``bodyPreview``,
   capped at 255 chars; the trigger uses ``body.content``). No size
   limit configured on the trigger node.

2. **n8n Code node** ("Build Request Body") — executes
   ``email.body?.content || ''`` then ``JSON.stringify(body)``. The
   Code node has a per-execution memory cap (~512MB default) but no
   silent string-length truncation. Verified against
   ``workflows/email-to-alfred-ingest.json``.

3. **n8n HTTP Request node** ("POST to Alfred Ingest") — uses axios
   defaults internally; no body-size cap. Timeout 30s, retry-on-fail
   enabled. No silent truncation.

4. **Cloudflare Tunnel** (``webhook.ruralroutetransportation.ca``) —
   Cloudflare's per-request body limit is 100MB on the free plan.
   Far above any realistic email body. Out-of-band but checked.

5. **Python webhook receiver** (``mail/webhook.py``, this module) —
   reads ``self.rfile.read(content_length)`` — exact Content-Length
   bytes from the socket. ``http.server.BaseHTTPRequestHandler`` has
   NO built-in body-size limit. No ``MAX_CONTENT_LENGTH`` constant
   in the codebase (verified by exhaustive grep). Full payload lands
   in ``raw``, parsed by ``json.loads(raw)``.

6. **HTML strip** (``_strip_html``) — operates on the full body
   string. Stages are: style/script removal, block-tag → newline
   conversion, tag stripping, entity decode, whitespace collapse.
   No length-bounded operation. Verified by reading the function
   end-to-end.

7. **Markdown build** (``_build_markdown``) — interpolates the
   (post-strip OR synthesized) body into the markdown template
   unbounded. No truncation.

8. **Inbox file write** (``out.write_text(md, encoding="utf-8")``)
   — ``pathlib.Path.write_text`` writes the full string atomically.
   No truncation.

9. **Curator pipeline** (``curator/pipeline.py``) — ``inbox_content``
   read via ``inbox_file.read_text(encoding="utf-8")`` (no limit),
   interpolated into the LLM prompt via ``f"{inbox_content}"`` (no
   limit), prompt written to a temp file via
   ``prompt_file.write(prompt)`` (no limit), piped to the LLM
   backend's stdin (no limit). The temp-file detour is explicitly
   to avoid ARG_MAX, NOT to cap input size.

10. **LLM backend** — Claude Code (``-p`` via stdin) has Anthropic
    SDK's ~200k-token context limit (~600KB-1MB chars). OpenClaw
    HTTP backend has provider-dependent limits but realistic
    email bodies are far below any provider's cap.

**Conclusion**: the "30+ Empty-Record Sender" syntheses are not
caused by a Python pipeline truncation bug. The empty-body symptom
is fully explained by:

  (a) Image-only HTML emails — addressed by Part A's header-synth
      fallback in this same file.
  (b) HTML with content but no extractable text per the strip
      regex's tag-handling rules (e.g., content nested inside
      ``<table>`` cells with unusual attribute encodings, or
      content where every text node is whitespace-only after
      entity decode). These are also addressed by Part A's synth
      path when the alt-text / link surface is non-empty.
  (c) Genuinely empty HTML (rare — likely scam senders or broken
      sender tools). These now log
      ``webhook.body_synthesis_no_signal`` so the operator can
      grep for and triage the bifurcation tail.

**Open follow-up**: the IMAP fetcher path (``mail/fetcher.py``) uses
a different ``_build_markdown`` with a rougher HTML strip
(``re.sub(r"<[^>]+>", " ", content)``). It has no synthesis
fallback. If/when an instance's primary ingest is IMAP rather
than webhook, the same Part A treatment should be applied there.
Tracked as a defer-with-reason rather than a blocker — operators
report Gmail goes through webhook (per the empty inbox file pattern
matching the webhook format, plus ``data/mail_state.json`` showing
no IMAP-fetched seen IDs in the live instance).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from functools import partial
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import structlog

from alfred.common.heartbeat import Heartbeat

log = structlog.get_logger(__name__)

# Module-level idle-tick heartbeat — see ``alfred.common.heartbeat`` for
# the rationale ("intentionally left blank" pattern). Counter is bumped
# in :meth:`WebhookHandler.do_POST` after each successful save, and from
# the IMAP fetcher path (``mail/fetcher.py``). The heartbeat thread is
# spawned in :func:`run_webhook` only when ``enabled`` is True.
heartbeat: Heartbeat = Heartbeat(daemon_name="mail", log=log)


def _sanitize_filename(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:max_len].rstrip("-") or "no-subject"


def _strip_html(html: str) -> str:
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
# Fix: when post-strip body falls below ``_MIN_BODY_CHARS``, attempt
# to synthesize a body from the raw HTML headers (``<img alt="…">``
# alt-text + first ``_MAX_LINKS_IN_SYNTH`` ``<a href="…">`` anchors).
# The synthesized body carries an explicit ``[image-only HTML; body
# synthesized from headers]`` marker so the curator (and any downstream
# grep) can distinguish image-only from bug-truncated.
_MIN_BODY_CHARS = 30
_MAX_ALT_TEXTS_IN_SYNTH = 8
_MAX_LINKS_IN_SYNTH = 5
_SYNTH_MARKER = "[image-only HTML; body synthesized from headers]"
# 2026-05-21 — Empty-Email-synth bypass fix. Two new markers for the
# remaining bifurcation tail that the image-only synth (above) missed:
#   * Pattern 1 (71% of post-2026-05-18 Empty Email records): senders
#     pad bodies with hundreds of invisible Unicode chars (U+2007
#     figure space, U+034F combining grapheme joiner, U+200B-U+200F /
#     U+2060-U+206F zero-width range, U+FEFF BOM). `len(body)` returns
#     hundreds so the original `len(body) < _MIN_BODY_CHARS` gate at
#     line 320 never fires — the synth path is bypassed and the curator
#     sees garbage. Fix: measure VISIBLE text length (strip invisibles
#     first) before checking the threshold.
#   * Pattern 2 (29%): n8n sends ONLY subject + account label upstream
#     of Alfred — no body, no raw_html, no sender. Image-only synth
#     can't fire (raw_html is absent). Fix: a separate subject-only
#     synth path emits a marker so the curator + distiller can
#     distinguish "n8n sent partial" from "legitimate empty email."
_UPSTREAM_TRUNCATED_MARKER = "[upstream-truncated; body lost before Alfred reception]"
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
_INVISIBLE_CHARS_RE = re.compile(
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


def _visible_text_len(body: str) -> int:
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
    return len(_INVISIBLE_CHARS_RE.sub("", body))


def _synthesize_minimal_from_subject(
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
        _UPSTREAM_TRUNCATED_MARKER,
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


def _extract_alt_texts(html: str) -> list[str]:
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


def _extract_links(html: str) -> list[tuple[str, str]]:
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


def _synthesize_body_from_headers(
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
    alt_texts = _extract_alt_texts(html)[:_MAX_ALT_TEXTS_IN_SYNTH]
    links = _extract_links(html)[:_MAX_LINKS_IN_SYNTH]
    if not alt_texts and not links:
        return None

    lines: list[str] = [_SYNTH_MARKER, ""]
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


def _build_markdown(data: dict) -> str:
    subject = data.get("subject", "No Subject")
    from_addr = data.get("from", "")
    to_addr = data.get("to", "")
    date = data.get("date", "")
    body = data.get("body", "")
    account = data.get("account", "")
    message_id = data.get("message_id", "")
    in_reply_to = data.get("in_reply_to", "")

    lines = [f"# {subject}", ""]
    if from_addr:
        lines.append(f"**From:** {from_addr}")
    if to_addr:
        lines.append(f"**To:** {to_addr}")
    if date:
        lines.append(f"**Date:** {date}")
    if account:
        lines.append(f"**Account:** {account}")
    if message_id:
        lines.append(f"**Message-ID:** {message_id}")
    if in_reply_to:
        lines.append(f"**In-Reply-To:** {in_reply_to}")
    # Strip HTML if body looks like it contains HTML tags. Preserve the
    # raw HTML for the image-only fallback in case the strip leaves us
    # below the minimum-content threshold (2026-05-18, Queue #11).
    raw_html = body if body and "<" in body else ""
    if raw_html:
        body = _strip_html(body)
    # Image-only fallback: when the post-strip body has too little
    # VISIBLE content to carry signal (default 30 chars after stripping
    # invisible Unicode padding — see ``_visible_text_len``), and the
    # raw HTML has alt-text / link anchors to fall back to, synthesize
    # a body from those. The synth marker is grep-able so post-hoc
    # analysis can count the bifurcation rate across the inbox stream.
    #
    # The ``_visible_text_len`` gate replaced the original
    # ``len(body)`` gate on 2026-05-21 (Empty-Email-synth bypass fix).
    # The bare ``len(body)`` measurement let Patreon-style emails
    # padded with hundreds of U+2007 / U+034F invisible chars bypass
    # the synth — body looked 200 chars to ``len()`` but was visually
    # empty. ``_visible_text_len`` strips those before measuring.
    if raw_html and _visible_text_len(body) < _MIN_BODY_CHARS:
        synth = _synthesize_body_from_headers(
            raw_html, subject=subject, from_addr=from_addr,
        )
        if synth is not None:
            log.info(
                "webhook.body_synthesized_from_headers",
                from_addr=from_addr or "",
                subject=subject or "",
                stripped_len=len(body),
                visible_len=_visible_text_len(body),
                synth_len=len(synth),
            )
            body = synth
        else:
            # Image-only fallback couldn't recover anything useful
            # (no alt-text, no usable links). Emit an explicit
            # "ran, nothing to do" signal so the empty body
            # produces a grep-able record of the bifurcation path
            # — per ``feedback_intentionally_left_blank.md``. The
            # body remains empty; the curator sees the headers
            # only and the operator can grep this event to count
            # truly-empty sources.
            log.info(
                "webhook.body_synthesis_no_signal",
                from_addr=from_addr or "",
                subject=subject or "",
                stripped_len=len(body),
                visible_len=_visible_text_len(body),
                raw_html_len=len(raw_html),
            )
    elif not raw_html and _visible_text_len(body) == 0:
        # Pattern 2 from the 2026-05-21 Empty-Email-synth bypass fix.
        # n8n upstream truncation: ``body`` is empty or whitespace /
        # invisible-only AND there's no raw_html to extract from
        # (image-only synth can't fire). 7 of 24 post-2026-05-18 Empty
        # Email records hit this path. Emit a minimal subject-only
        # synth carrying the ``[upstream-truncated; body lost before
        # Alfred reception]`` marker so the operator can grep it
        # distinct from the image-only marker and the curator +
        # distiller can distinguish "n8n sent partial" from
        # "legitimate empty email."
        #
        # Gated on ``_visible_text_len(body) == 0`` (NOT
        # ``< _MIN_BODY_CHARS``) to avoid over-firing on legitimate
        # short plain-text bodies like "thanks" or "ok" — those have
        # real content under 30 chars but are not upstream-truncated.
        # Pattern 2 specifically is "n8n sent no body at all"; the
        # visible-len-zero check matches the operator-observed
        # symptom without inventing a synth marker for content-bearing
        # short emails.
        synth = _synthesize_minimal_from_subject(
            subject=subject, from_addr=from_addr, account=account,
        )
        if synth is not None:
            log.info(
                "webhook.body_synthesized_upstream_truncated",
                from_addr=from_addr or "",
                subject=subject or "",
                account=account or "",
                body_len=len(body or ""),
                visible_len=_visible_text_len(body),
                synth_len=len(synth),
            )
            body = synth
        else:
            # No subject, no from, no account — nothing at all
            # survived upstream. Emit a no-signal event mirroring
            # the image-only no-signal log so the operator can grep
            # this terminal-truncation case too.
            log.info(
                "webhook.body_synthesis_upstream_no_signal",
                from_addr=from_addr or "",
                subject=subject or "",
                account=account or "",
                body_len=len(body or ""),
                visible_len=_visible_text_len(body),
            )
    lines.extend(["", "---", "", body])
    return "\n".join(lines)


class WebhookHandler(BaseHTTPRequestHandler):
    def __init__(self, inbox_path: Path, token: str, *args, **kwargs):
        self.inbox_path = inbox_path
        self.token = token
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        # Suppress default stderr logging, use structlog instead
        pass

    def _check_auth(self) -> bool:
        if not self.token:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {self.token}"

    def do_POST(self):
        if self.path != "/ingest":
            self.send_error(404)
            return

        if not self._check_auth():
            self.send_error(401, "Unauthorized")
            log.warning("webhook.auth_failed")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        md = _build_markdown(data)
        subject = data.get("subject", "no-subject")
        slug = _sanitize_filename(subject)
        account = data.get("account", "mail")
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"email-{account}-{ts}-{slug}.md"

        self.inbox_path.mkdir(parents=True, exist_ok=True)
        out = self.inbox_path / filename
        out.write_text(md, encoding="utf-8")

        log.info("webhook.saved", file=filename)
        # Idle-tick counter — one webhook received and saved = one event.
        heartbeat.record_event()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok", "file": filename}).encode())

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_error(404)


def run_webhook(
    inbox_path: Path,
    host: str = "0.0.0.0",
    port: int = 5005,
    token: str = "",
    idle_tick_enabled: bool = True,
    idle_tick_interval_seconds: int = 60,
) -> None:
    """Start the webhook HTTPServer and (optionally) the idle-tick heartbeat.

    The heartbeat runs in a daemon thread because ``HTTPServer.serve_forever``
    is sync — there's no asyncio loop to host an async tick task. Same
    counter semantics as the asyncio daemons; see
    ``alfred.common.heartbeat`` for rationale.
    """
    import threading

    handler = partial(WebhookHandler, inbox_path, token)
    server = HTTPServer((host, port), handler)
    log.info("webhook.started", host=host, port=port)
    print(f"Webhook listening on http://{host}:{port}/ingest")

    # Idle-tick heartbeat — emits ``mail.idle_tick`` every
    # ``idle_tick_interval_seconds``. Default 60s, on by default. Spawned
    # only when enabled; the disabled path skips the thread entirely.
    heartbeat_shutdown = threading.Event()
    heartbeat_thread = None
    if idle_tick_enabled:
        from alfred.common.heartbeat import run_in_thread
        heartbeat_thread = run_in_thread(
            heartbeat,
            interval_seconds=idle_tick_interval_seconds,
            shutdown_event=heartbeat_shutdown,
        )
        log.info(
            "webhook.heartbeat_started",
            interval_seconds=idle_tick_interval_seconds,
        )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        # Signal the heartbeat thread to exit; thread is daemon=True so
        # it won't block process exit even if it lingers.
        heartbeat_shutdown.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=2)
        server.server_close()
