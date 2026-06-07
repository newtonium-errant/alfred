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

from . import extract
from .config import MailAccount, MailConfig
from .state import StateManager

log = structlog.get_logger(__name__)


def _sanitize_filename(s: str, max_len: int = 80) -> str:
    """Turn a string into a safe filename slug."""
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s).strip("-")
    return s[:max_len].rstrip("-") or "no-subject"


def _extract_text(msg: email.message.EmailMessage) -> tuple[str, str]:
    """Extract ``(body_text, raw_html)`` from an email message.

    Returns:
        ``(body, raw_html)``:
            * ``body`` — the text the caller should treat as the message
              body. Empty string when no usable content was found.
            * ``raw_html`` — the original HTML source when an HTML part
              exists, ``""`` otherwise. The caller uses this for the
              image-only synth fallback in :func:`_build_markdown`.

    Routing:
        1. **Plain-text fast path.** If a ``text/plain`` part exists AND
           its :func:`extract.visible_text_len` is at or above
           :data:`extract.MIN_BODY_CHARS`, return ``(plain.strip(), "")``.
           Synth never fires because the plain part carries real content
           and there's no HTML to fall back to.

        2. **HTML path.** Otherwise, look up a ``text/html`` part. If
           present, return ``(extract.strip_html(html), html)`` so the
           caller has the raw HTML available to fall back to.

        3. **Empty fallthrough.** Neither path produced content: return
           ``("", "")``. The caller routes through the upstream-truncated
           synth path on the empty body.

    Note: the pre-P12 implementation preferred plain text unconditionally,
    which let marketing emails whose plain-text part was a preheader
    teaser ("View in browser…") bypass the HTML synth gate. The new
    flow applies the same visibility threshold to both paths, fixing
    the Salem IMAP empty-body parity gap. See
    ``project_empty_body_email_arc.md`` for the design.
    """
    plain_part = msg.get_body(preferencelist=("plain",))
    if plain_part is not None:
        content = plain_part.get_content()
        if isinstance(content, str):
            stripped = content.strip()
            if extract.visible_text_len(stripped) >= extract.MIN_BODY_CHARS:
                return (stripped, "")

    html_part = msg.get_body(preferencelist=("html",))
    if html_part is not None:
        content = html_part.get_content()
        if isinstance(content, str):
            return (extract.strip_html(content), content)

    return ("", "")


def _build_markdown(msg: email.message.EmailMessage, account: str) -> str:
    """Build a markdown file from an email message for the vault inbox.

    Mirrors :func:`alfred.mail.webhook._build_markdown` — the dispatch
    block below is the empty-body synth logic from the webhook path,
    lifted here so the IMAP fetcher produces byte-equivalent records
    for image-only / invisible-padded / upstream-truncated inputs.
    The two paths emit DIFFERENT log event prefixes (``fetcher.*`` vs
    ``webhook.*``) so per-path counters stay distinguishable in log
    aggregation; the resulting markdown bodies are byte-identical for
    equivalent input.

    Args:
        msg: Parsed :class:`email.message.EmailMessage` from the IMAP
            fetch.
        account: Account name (e.g. ``"live"``, ``"alfred"``) — feeds
            into the upstream-truncated synth path AND the
            ``**Account:**`` header line. Renamed from ``account_name``
            for parity with the webhook path's ``data["account"]`` field
            and to thread cleanly into
            :func:`extract.synthesize_minimal_from_subject`.

    The byte-equivalence guarantee (webhook vs fetcher producing
    identical body content for equivalent input) is pinned by
    ``tests/mail/test_extract_parity.py``. Per-path log-event coverage
    is pinned by ``tests/mail/test_fetcher_synth.py``.
    """
    subject = msg.get("Subject", "No Subject")
    from_addr = msg.get("From", "")
    to_addr = msg.get("To", "")
    date_str = msg.get("Date", "")
    message_id = msg.get("Message-ID", "")
    in_reply_to = msg.get("In-Reply-To", "")
    references = msg.get("References", "")

    body, raw_html = _extract_text(msg)

    # Empty-body bifurcation — image-only HTML synth (Pattern 1). When
    # the post-strip body has too little VISIBLE content (default 30
    # chars after stripping invisible Unicode padding — see
    # ``extract.visible_text_len``) AND the raw HTML has alt-text /
    # link anchors to fall back to, synthesize a body from those. The
    # synth marker (``[image-only HTML; body synthesized from
    # headers]``) is grep-able so post-hoc analysis can count the
    # bifurcation rate across the inbox stream.
    #
    # See ``webhook._build_markdown`` for the canonical implementation
    # this mirrors; ``extract.synthesize_body_from_headers`` is the
    # shared primitive used by both paths.
    if raw_html and extract.visible_text_len(body) < extract.MIN_BODY_CHARS:
        synth = extract.synthesize_body_from_headers(
            raw_html, subject=subject, from_addr=from_addr,
        )
        if synth is not None:
            log.info(
                "fetcher.body_synthesized_from_headers",
                from_addr=from_addr or "",
                subject=subject or "",
                stripped_len=len(body),
                visible_len=extract.visible_text_len(body),
                synth_len=len(synth),
            )
            body = synth
        else:
            # Image-only fallback couldn't recover anything useful
            # (no alt-text, no usable links). Per
            # ``feedback_intentionally_left_blank.md`` — emit an
            # explicit "ran, nothing to do" signal so the empty body
            # produces a grep-able record of the bifurcation path. The
            # body remains empty; the curator sees the headers only
            # and the operator can grep this event to count truly-
            # empty sources.
            log.info(
                "fetcher.body_synthesis_no_signal",
                from_addr=from_addr or "",
                subject=subject or "",
                stripped_len=len(body),
                visible_len=extract.visible_text_len(body),
                raw_html_len=len(raw_html),
            )
    # Empty-body bifurcation — upstream-truncated synth (Pattern 2).
    # IMAP returned an empty multipart structure (no plain part above
    # threshold, no HTML part at all) — body is empty / whitespace /
    # invisible-only AND there's no raw_html for the image-only synth
    # to fall back to. Emit a minimal subject-only synth carrying the
    # ``[upstream-truncated; body lost before Alfred reception]``
    # marker so the operator can grep it distinct from the image-only
    # marker and the curator + distiller can distinguish "IMAP
    # produced no body" from "legitimate empty email."
    #
    # Gated on ``visible_text_len(body) == 0`` (NOT
    # ``< MIN_BODY_CHARS``) to avoid over-firing on legitimate short
    # plain-text bodies like "thanks" or "ok" — those have real
    # content under 30 chars but are not upstream-truncated. The
    # plain-text fast path in ``_extract_text`` returns short
    # plain-text content as-is with ``raw_html=""``; the gate here
    # would then fire on visible_text_len > 0 and skip the synth.
    # Pattern 2 specifically is "fetcher produced no body at all"; the
    # visible-len-zero check matches the operator-observed symptom
    # without inventing a synth marker for content-bearing short
    # emails.
    elif not raw_html and extract.visible_text_len(body) == 0:
        synth = extract.synthesize_minimal_from_subject(
            subject=subject, from_addr=from_addr, account=account,
        )
        if synth is not None:
            log.info(
                "fetcher.body_synthesized_upstream_truncated",
                from_addr=from_addr or "",
                subject=subject or "",
                account=account or "",
                body_len=len(body or ""),
                visible_len=extract.visible_text_len(body),
                synth_len=len(synth),
            )
            body = synth
        else:
            # No subject, no from, no account — nothing at all
            # survived the IMAP fetch. Emit a no-signal event
            # mirroring the image-only no-signal log so the operator
            # can grep this terminal-truncation case too.
            log.info(
                "fetcher.body_synthesis_upstream_no_signal",
                from_addr=from_addr or "",
                subject=subject or "",
                account=account or "",
                body_len=len(body or ""),
                visible_len=extract.visible_text_len(body),
            )

    lines = [
        f"# {subject}",
        "",
        f"**From:** {from_addr}",
        f"**To:** {to_addr}",
        f"**Date:** {date_str}",
        f"**Account:** {account}",
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
