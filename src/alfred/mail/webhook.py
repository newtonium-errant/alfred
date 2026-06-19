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


# 2026-06-07 (P11 Ship 1): empty-body detection + synthesis primitives
# moved to ``alfred.mail.extract`` so the IMAP fetcher path (Ship 2)
# can apply the same bifurcation logic. The audit-comment history
# at the TOP of this file is preserved for webhook-pipeline context;
# the canonical implementation now lives in ``extract.py``.
#
# The inline audit comments that documented WHY each primitive
# exists (2026-05-18 image-only fix at original lines 148-164,
# 2026-05-21 invisible-Unicode + n8n-upstream-truncation fix at
# original lines 169-197) moved WITH the constants to extract.py.
#
# The underscored aliases below preserve the public surface that
# ``tests/test_webhook_image_only.py`` imports from this module.
# New code (curator / distiller SKILLs in Ship 4, fetcher in Ship 2)
# should import from ``alfred.mail.extract`` directly with the
# public names.
from . import extract as _extract

_MIN_BODY_CHARS = _extract.MIN_BODY_CHARS
_MAX_ALT_TEXTS_IN_SYNTH = _extract.MAX_ALT_TEXTS_IN_SYNTH
_MAX_LINKS_IN_SYNTH = _extract.MAX_LINKS_IN_SYNTH
_SYNTH_MARKER = _extract.SYNTH_MARKER_IMAGE_ONLY
_UPSTREAM_TRUNCATED_MARKER = _extract.SYNTH_MARKER_UPSTREAM_TRUNCATED
_INVISIBLE_CHARS_RE = _extract.INVISIBLE_CHARS_RE
_IMG_ALT_RE = _extract._IMG_ALT_RE
_A_HREF_RE = _extract._A_HREF_RE

_strip_html = _extract.strip_html
_visible_text_len = _extract.visible_text_len
_extract_alt_texts = _extract.extract_alt_texts
_extract_links = _extract.extract_links
_synthesize_body_from_headers = _extract.synthesize_body_from_headers
_synthesize_minimal_from_subject = _extract.synthesize_minimal_from_subject




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
    host: str = "127.0.0.1",
    port: int = 5005,
    token: str = "",
    idle_tick_enabled: bool = True,
    idle_tick_interval_seconds: int = 60,
) -> None:
    """Start the webhook HTTPServer and (optionally) the idle-tick heartbeat.

    ``host`` defaults to loopback (``127.0.0.1``), NOT all-interfaces. The
    public ingress is the Cloudflare tunnel (cloudflared), the single proxy
    in front of this receiver — its ``config.yml`` forwards
    ``service: http://localhost:5005`` — so the only traffic that reaches this
    server is the tunnel hitting localhost. Binding loopback keeps the
    receiver off every other interface; the bearer-token check
    (``MAIL_WEBHOOK_TOKEN``) remains the auth layer and is unaffected by this.
    Override ``host`` only if you deliberately front the webhook with a
    different reverse proxy bound to another interface.

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
