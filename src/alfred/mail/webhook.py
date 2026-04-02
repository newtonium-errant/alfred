"""Simple webhook receiver — accepts POSTed email data and writes to vault inbox."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from functools import partial
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


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
    # Strip HTML if body looks like it contains HTML tags
    if body and "<" in body:
        body = _strip_html(body)
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


def run_webhook(inbox_path: Path, host: str = "0.0.0.0", port: int = 5005, token: str = "") -> None:
    handler = partial(WebhookHandler, inbox_path, token)
    server = HTTPServer((host, port), handler)
    log.info("webhook.started", host=host, port=port)
    print(f"Webhook listening on http://{host}:{port}/ingest")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
