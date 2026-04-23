"""IMAP email fetcher — downloads new emails and saves them to the vault inbox."""

from __future__ import annotations

import email
import email.policy
import imaplib
import re
import ssl
from datetime import datetime, timezone
from pathlib import Path

import structlog

from .config import MailAccount, MailConfig
from .state import StateManager

log = structlog.get_logger(__name__)


def _sanitize_filename(s: str, max_len: int = 80) -> str:
    """Turn a string into a safe filename slug."""
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:max_len].rstrip("-") or "no-subject"


def _extract_text(msg: email.message.EmailMessage) -> str:
    """Extract plain text body from an email message."""
    body = msg.get_body(preferencelist=("plain",))
    if body:
        content = body.get_content()
        if isinstance(content, str):
            return content.strip()
    # Fallback: try html and strip tags minimally
    body = msg.get_body(preferencelist=("html",))
    if body:
        content = body.get_content()
        if isinstance(content, str):
            # Rough html strip — curator will handle the rest
            text = re.sub(r"<[^>]+>", " ", content)
            return re.sub(r"\s+", " ", text).strip()
    return ""


def _build_markdown(msg: email.message.EmailMessage, account_name: str) -> str:
    """Build a markdown file from an email message for the vault inbox."""
    subject = msg.get("Subject", "No Subject")
    from_addr = msg.get("From", "")
    to_addr = msg.get("To", "")
    date_str = msg.get("Date", "")
    message_id = msg.get("Message-ID", "")
    in_reply_to = msg.get("In-Reply-To", "")
    references = msg.get("References", "")

    body = _extract_text(msg)

    lines = [
        f"# {subject}",
        "",
        f"**From:** {from_addr}",
        f"**To:** {to_addr}",
        f"**Date:** {date_str}",
        f"**Account:** {account_name}",
    ]
    if message_id:
        lines.append(f"**Message-ID:** {message_id}")
    if in_reply_to:
        lines.append(f"**In-Reply-To:** {in_reply_to}")
    if references:
        lines.append(f"**References:** {references}")
    lines.extend(["", "---", "", body])
    return "\n".join(lines)


def fetch_account(
    account: MailAccount,
    inbox_path: Path,
    state_mgr: StateManager,
) -> int:
    """Fetch new emails from one account. Returns count of new emails saved."""
    password = account.resolved_password()
    if not password:
        log.error("mail.no_password", account=account.name)
        return 0

    ctx = ssl.create_default_context()
    count = 0

    try:
        with imaplib.IMAP4_SSL(account.imap_host, account.imap_port, ssl_context=ctx) as conn:
            conn.login(account.email, password)
            log.info("mail.connected", account=account.name)

            for folder in account.folders:
                status, _ = conn.select(folder, readonly=not account.mark_read)
                if status != "OK":
                    log.warning("mail.folder_failed", account=account.name, folder=folder)
                    continue

                # Search for unseen messages
                status, data = conn.search(None, "UNSEEN")
                if status != "OK" or not data[0]:
                    log.info("mail.no_new", account=account.name, folder=folder)
                    continue

                msg_nums = data[0].split()
                log.info("mail.found", account=account.name, folder=folder, count=len(msg_nums))

                for num in msg_nums:
                    status, msg_data = conn.fetch(num, "(RFC822)")
                    if status != "OK":
                        continue

                    raw = msg_data[0][1]
                    msg = email.message_from_bytes(raw, policy=email.policy.default)
                    message_id = msg.get("Message-ID", "")

                    if state_mgr.state.is_seen(account.name, message_id):
                        continue

                    # Build and save markdown file
                    md = _build_markdown(msg, account.name)
                    subject = msg.get("Subject", "no-subject")
                    slug = _sanitize_filename(subject)
                    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                    filename = f"email-{account.name}-{ts}-{slug}.md"

                    out = inbox_path / filename
                    out.write_text(md, encoding="utf-8")
                    log.info("mail.saved", file=filename)
                    # Idle-tick counter — one email fetched and saved =
                    # one event. Imported lazily so importing the fetcher
                    # doesn't drag the heartbeat module in unless someone
                    # actually runs it.
                    from .webhook import heartbeat as _heartbeat
                    _heartbeat.record_event()

                    if account.mark_read:
                        conn.store(num, "+FLAGS", "\\Seen")

                    state_mgr.state.mark_seen(account.name, message_id)
                    count += 1

    except imaplib.IMAP4.error as e:
        log.error("mail.imap_error", account=account.name, error=str(e))
    except Exception as e:
        log.error("mail.error", account=account.name, error=str(e))

    return count


def fetch_all(config: MailConfig, vault_path: Path) -> int:
    """Fetch from all configured accounts. Returns total new emails."""
    inbox_path = vault_path / config.inbox_dir
    inbox_path.mkdir(parents=True, exist_ok=True)

    state_mgr = StateManager(config.state_path)
    state_mgr.load()

    total = 0
    for account in config.accounts:
        total += fetch_account(account, inbox_path, state_mgr)

    state_mgr.save()
    log.info("mail.fetch_complete", total=total)
    return total
