"""IMAP email fetcher — downloads new emails and saves them to the vault inbox."""

from __future__ import annotations

import difflib
import email
import email.policy
import hashlib
import imaplib
import re
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
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


def _safe_get_content(part: email.message.EmailMessage) -> str | None:
    """Return the part's string content, or None if extraction fails.

    :meth:`EmailMessage.get_content` crashes on several structural edge
    cases that arise in real IMAP traffic:

    * **Headers-only messages.** When no body has been set, the
      ``EmailMessage`` defaults to ``text/plain`` at the structural
      level. ``get_body(preferencelist=("plain",))`` returns the
      message itself; ``get_content()`` calls ``get_payload(decode=True)``
      which returns ``None``; the stdlib then tries
      ``None.decode(charset)`` and raises ``AttributeError``. Salem's
      IMAP path sees these whenever n8n upstream drops the body or an
      upstream filter strips content.
    * **Binary sub-types.** Certain content-types (PGP attachments,
      proprietary container formats) raise ``LookupError`` /
      ``KeyError`` from the content manager.
    * **Malformed multipart structures.** Edge cases in the
      content-transfer-encoding step raise other ``AttributeError``
      variants.

    All three are operationally indistinguishable from "no usable
    content" — the caller should fall through to the next path. We
    emit one ``fetcher.get_content_failed`` log per catch (per
    ``feedback_intentionally_left_blank.md``) carrying the exception
    type + the part's content-type so a future edge case is debuggable
    without re-instrumenting the code.

    The defensive shape mirrors the implicit guarantees ``webhook.py``
    enjoys (its ``body`` field is a guaranteed string via JSON parse);
    fetcher receives raw ``EmailMessage`` objects and needs the explicit
    guards.

    Returns:
        The decoded string content on success, ``None`` on any
        extraction failure or when the decoded content is not a
        string (e.g. binary-decoded ``bytes``).
    """
    try:
        content = part.get_content()
    except (AttributeError, KeyError, LookupError) as exc:
        log.info(
            "fetcher.get_content_failed",
            error_type=exc.__class__.__name__,
            content_type=part.get_content_type(),
        )
        return None
    return content if isinstance(content, str) else None


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

    The ``get_content()`` calls on both paths go through
    :func:`_safe_get_content` so a headers-only ``EmailMessage`` (the
    operational shape Salem sees when n8n upstream drops the body)
    falls through cleanly to the empty path rather than crashing with
    ``AttributeError: 'NoneType' object has no attribute 'decode'``.
    Caught at QA time on Ship 2 ship-review (commit ``ea85b6f``) by
    the parity-test surface — fixed in follow-up.
    """
    plain_part = msg.get_body(preferencelist=("plain",))
    if plain_part is not None:
        content = _safe_get_content(plain_part)
        if content is not None:
            stripped = content.strip()
            if extract.visible_text_len(stripped) >= extract.MIN_BODY_CHARS:
                return (stripped, "")

    html_part = msg.get_body(preferencelist=("html",))
    if html_part is not None:
        content = _safe_get_content(html_part)
        if content is not None:
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
    # Header reads — separate the SYNTH-GATE value from the
    # DISPLAY value. Webhook receives data via JSON from n8n where
    # every key is always present (empty string when absent), so
    # ``data.get("subject", "No Subject")`` returns ``""`` for
    # source-side absent subjects. Fetcher receives raw EmailMessage
    # objects where missing headers truly are missing, so
    # ``msg.get("Subject", "No Subject")`` returns the literal "No
    # Subject" fallback — which then leaks into the synth gate's
    # all-falsy check at ``extract.synthesize_minimal_from_subject``
    # and prevents the no-signal branch from firing. The fix
    # threads the falsy-when-absent value into the synth gate; the
    # human-friendly "No Subject" fallback applies only to the
    # markdown heading line.
    #
    # Caught at QA time on Ship 2 ship-review (commits ``ea85b6f``
    # + ``5b68ac0``) — the parity test
    # ``test_empty_message_no_subject_no_from_emits_no_signal_log``
    # was authored against the operationally-correct contract
    # (synth must NOT fire when everything is absent) but the
    # production code's display-fallback was bleeding into the
    # gate. Fix lives here.
    subject = msg.get("Subject") or ""
    from_addr = msg.get("From") or ""
    to_addr = msg.get("To") or ""
    date_str = msg.get("Date") or ""
    message_id = msg.get("Message-ID") or ""
    in_reply_to = msg.get("In-Reply-To") or ""
    references = msg.get("References") or ""

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

    # Display-only fallback for the markdown heading. The synth-gate
    # value (``subject`` above) is empty when the source-side subject
    # was absent; the heading line still gets the human-friendly
    # "No Subject" fallback so the rendered record reads cleanly.
    # See the header-read block above for the gate-vs-display split
    # rationale.
    display_subject = subject or "No Subject"
    lines = [
        f"# {display_subject}",
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


def fetch_all(config: MailConfig, vault_path: Path, *, only_flagged: bool = False) -> int:
    """Fetch new emails and drop them into the vault inbox. Returns total new emails.

    ``only_flagged=False`` (the manual ``alfred mail fetch`` CLI): pull EVERY configured account — the
    deliberate, parity-maintained fallback path, unchanged since 2026-06-07.
    ``only_flagged=True`` (the #7 7a native daemon loop): pull ONLY the ``fetch: true`` accounts (Gmail)
    so the webhook-delivered accounts (live.ca) are never double-fetched.

    The live inbound flow remains the webhook (n8n → tunnel → ``mail.webhook``); with the fetch loop
    turned on it runs ALONGSIDE the webhook, never replacing it. Logs route to ``mail.log`` (CLI) or
    ``mail_webhook.log`` (the daemon thread shares the webhook runner's logging).
    """
    inbox_path = vault_path / config.inbox_dir
    inbox_path.mkdir(parents=True, exist_ok=True)
    accounts = config.fetch_accounts() if only_flagged else config.accounts

    # R1 (2026-06-11): log the CONFIGURED-account truth here, at the one
    # place accounts are actually consumed. The (since renamed)
    # ``mail.state.loaded accounts=N`` line counted seen message ids and
    # misled a diagnosis into "the IMAP account config isn't loading."
    log.info(
        "mail.fetch.starting",
        accounts=len(accounts),
        account_names=[a.name for a in accounts],
        only_flagged=only_flagged,
    )

    state_mgr = StateManager(config.state_path)
    state_mgr.load()

    total = 0
    for account in accounts:
        total += fetch_account(account, inbox_path, state_mgr)

    state_mgr.save()
    log.info("mail.fetch_complete", total=total)
    return total


# ===========================================================================
# #7 7b — shadow parity harness (READ-ONLY) + the normalized parity compare
#
# Purpose: PROVE, on real mail, that the native fetcher produces records
# equivalent to the n8n webhook BEFORE the operator flips off n8n. The live
# run is box-gated (needs the Gmail app password); this module is the tooling,
# fully unit-testable with fixtures.
#
# FOUR ACCEPTED DIVERGENCES (all "fetcher is the richer superset"; ratified
# 2026-07-23) — the compare normalizes exactly these and demands byte-equality
# on everything else, so a FIFTH divergence still fails (fail-loud at the box run):
#   1. From    — fetcher emits the full raw header (display name + address);
#                n8n reduces to the bare address via ``parseAddr``.
#   2. To      — fetcher emits the full raw header (all recipients);
#                n8n reduces to the first bare address.
#   3. References — fetcher emits a ``**References:**`` line for threaded mail;
#                n8n's POST omits it entirely.
#   4. Subject — fetcher parses with ``policy.default`` which DECODES RFC2047
#                encoded-words (``=?UTF-8?...?=`` → Unicode); a raw header read
#                keeps them encoded. The compare RFC2047-decodes BOTH heading
#                lines (idempotent — a no-op on an already-decoded/ASCII subject)
#                so it's robust whether Gmail returns the Subject encoded or
#                pre-decoded, while still demanding subject-equality-modulo-
#                encoding (a genuinely different subject fails).
# ===========================================================================


_ANGLE_ADDR_RE = re.compile(r"<([^>]+)>")
_FROM_LINE_RE = re.compile(r"^\*\*From:\*\*\s*(.*)$")
_TO_LINE_RE = re.compile(r"^\*\*To:\*\*\s*(.*)$")
_REFS_LINE_RE = re.compile(r"^\*\*References:\*\*")
_MID_LINE_RE = re.compile(r"^\*\*Message-ID:\*\*\s*(.*)$")
# The subject appears ONLY as the H1 heading (``# <subject>``); there is no
# ``**Subject:**`` line. The parity compare RFC2047-decodes this line on both sides.
_HEADING_RE = re.compile(r"^#\s(.*)$")


def _decode_rfc2047(value: str) -> str:
    """Decode any RFC2047 encoded-words in a header value to Unicode.

    IDEMPOTENT: an already-decoded (or plain-ASCII) value is returned unchanged,
    so applying it to both sides of the compare is safe whether Gmail returns the
    Subject raw-encoded or pre-decoded. FAIL-SAFE: returns the input verbatim on
    any decode error (a malformed encoded-word must not crash the compare).
    """
    from email.header import decode_header, make_header
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 — malformed encoded-word must not crash the compare
        return value


def _normalize_addr(raw: str) -> str:
    """Reduce a From/To header value to the same bare address the n8n webhook produced.

    Mirrors the n8n 'Build Request Body' node's ``parseAddr`` EXACTLY: the first
    angle-bracketed ``<addr>`` if present, else the first comma-separated part,
    trimmed. Used ONLY by the parity compare to normalize away the ACCEPTED
    From/To divergence (fetcher = full raw header; n8n = bare address) while
    STILL demanding address-EQUALITY — a genuinely wrong From address (different
    address, not just a stripped display name) still fails the compare.
    """
    if not raw:
        return ""
    m = _ANGLE_ADDR_RE.search(raw)
    if m:
        return m.group(1).strip()
    return raw.split(",")[0].strip()


def _imap_since_date(
    *,
    lookback_days: int | None = None,
    since: str | None = None,
    today: date | None = None,
) -> str:
    """Compute the IMAP ``SEARCH SINCE`` date string (``DD-Mon-YYYY``).

    Prefers an explicit ``since`` (``YYYY-MM-DD``); else ``lookback_days`` back
    from ``today``; else a 7-day default. ``today`` is injectable for tests.
    Raises ``ValueError`` on an unparseable ``since`` (fail-loud — a silently
    wrong window would silently narrow the parity proof).
    """
    if today is None:
        today = date.today()
    if since:
        d = date.fromisoformat(since)
    elif lookback_days is not None:
        d = today - timedelta(days=lookback_days)
    else:
        d = today - timedelta(days=7)
    return d.strftime("%d-%b-%Y")


def fetch_account_shadow(
    account: MailAccount,
    shadow_path: Path,
    *,
    since: str,
    folder: str | None = None,
    seen_ids: set[str] | None = None,
) -> int:
    """READ-ONLY shadow fetch of one account for the #7 7b parity proof.

    NON-DISRUPTIVE BY CONSTRUCTION — four independent belts guarantee it cannot
    alter Gmail state or the production inbox:

      1. **EXAMINE** — ``select(folder, readonly=True)``; imaplib maps this to
         the IMAP ``EXAMINE`` command → server-side read-only, no flag write is
         even possible.
      2. **BODY.PEEK[]** — the fetch uses ``BODY.PEEK[]``, NEVER ``RFC822``, so
         ``\\Seen`` is not set even on a writable mailbox.
      3. **No STORE** — the shadow path issues NO ``conn.store(...)`` at all; the
         production path's ``+FLAGS \\Seen`` store is absent here.
      4. **Shadow dir only** — records are written under ``shadow_path`` (never
         the vault inbox), so the curator never ingests them.

    Fetches messages ``SINCE`` the given IMAP date REGARDLESS of ``\\Seen`` (n8n
    has already read/archived them), dedups by Message-ID within the run,
    renders each via the shared :func:`_build_markdown` (byte-identical to the
    production fetcher path), and writes ``email-<account>-<midtag>-<slug>.md``.
    The shadow filename uses a stable Message-ID hash (not a timestamp) so a
    re-run overwrites the same message's file rather than piling up duplicates,
    and a bulk fetch of many same-second messages can't collide. The compare
    joins on the ``**Message-ID:**`` body line, never the filename. Returns the
    count written.
    """
    password = account.resolved_password()
    if not password:
        log.error("mail.shadow.no_password", account=account.name)
        return 0

    if seen_ids is None:
        seen_ids = set()
    target_folder = folder or "[Gmail]/All Mail"
    ctx = ssl.create_default_context()
    count = 0
    shadow_path.mkdir(parents=True, exist_ok=True)

    try:
        with imaplib.IMAP4_SSL(account.imap_host, account.imap_port, ssl_context=ctx) as conn:
            conn.login(account.email, password)
            log.info("mail.shadow.connected", account=account.name, folder=target_folder)

            # Belt 1: EXAMINE (read-only SELECT). imaplib readonly=True → EXAMINE.
            status, _ = conn.select(target_folder, readonly=True)
            if status != "OK":
                log.warning(
                    "mail.shadow.folder_failed",
                    account=account.name,
                    folder=target_folder,
                )
                return 0

            # Date-windowed search — SINCE returns messages regardless of \Seen.
            status, data = conn.search(None, "SINCE", since)
            if status != "OK" or not data or not data[0]:
                log.info(
                    "mail.shadow.no_messages",
                    account=account.name,
                    folder=target_folder,
                    since=since,
                )
                return 0

            msg_nums = data[0].split()
            log.info(
                "mail.shadow.found",
                account=account.name,
                folder=target_folder,
                since=since,
                count=len(msg_nums),
            )

            for num in msg_nums:
                # Belt 2: BODY.PEEK[] — never RFC822 → never sets \Seen.
                status, msg_data = conn.fetch(num, "(BODY.PEEK[])")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                if not raw:
                    continue
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                message_id = msg.get("Message-ID", "")
                if message_id and message_id in seen_ids:
                    continue

                md = _build_markdown(msg, account.name)
                subject = msg.get("Subject", "no-subject")
                slug = _sanitize_filename(subject)
                if message_id:
                    mid_tag = hashlib.sha1(message_id.encode("utf-8")).hexdigest()[:10]
                else:
                    mid_tag = f"nomid-{count}"
                filename = f"email-{account.name}-{mid_tag}-{slug}.md"
                # Belt 4: write ONLY under the shadow dir — never the vault inbox.
                out = shadow_path / filename
                out.write_text(md, encoding="utf-8")
                log.info("mail.shadow.saved", file=filename)
                if message_id:
                    seen_ids.add(message_id)
                count += 1
                # Belt 3: NO conn.store(...) anywhere — \Seen is never touched.

    except imaplib.IMAP4.error as e:
        log.error("mail.shadow.imap_error", account=account.name, error=str(e))
    except Exception as e:  # noqa: BLE001 — a shadow fault must never propagate
        log.error("mail.shadow.error", account=account.name, error=str(e))

    return count


def shadow_fetch_all(
    config: MailConfig,
    *,
    since: str,
    folder: str | None = None,
) -> int:
    """READ-ONLY shadow fetch of the ``fetch: true`` accounts for the 7b parity proof.

    Writes captured records under ``config.fetch.shadow_dir`` (deliberately
    OUTSIDE the vault inbox — the curator never sees them), NEVER the vault
    inbox. Returns the total records written. Per
    ``feedback_intentionally_left_blank.md``, a no-``fetch: true``-accounts
    config logs an explicit ``mail.shadow.no_accounts`` signal. See
    :func:`fetch_account_shadow` for the four non-disruptive belts.
    """
    shadow_dir = Path(config.fetch.shadow_dir)
    shadow_dir.mkdir(parents=True, exist_ok=True)
    accounts = config.fetch_accounts()
    log.info(
        "mail.shadow.starting",
        accounts=len(accounts),
        account_names=[a.name for a in accounts],
        since=since,
        folder=folder or "[Gmail]/All Mail",
        shadow_dir=str(shadow_dir),
    )
    if not accounts:
        log.info(
            "mail.shadow.no_accounts",
            detail="no account has fetch: true — nothing to shadow-fetch.",
        )
        return 0

    seen_ids: set[str] = set()
    total = 0
    for account in accounts:
        total += fetch_account_shadow(
            account, shadow_dir, since=since, folder=folder, seen_ids=seen_ids,
        )
    log.info("mail.shadow.complete", total=total)
    return total


# --- The normalized parity compare -----------------------------------------


@dataclass
class ParityPair:
    """One matched (shadow, production) record pair, joined by Message-ID.

    ``parity`` is True when the two records are byte-identical AFTER normalizing
    the four accepted divergences (From/To to bare address, References dropped,
    Subject RFC2047-decoded). ``diff`` is a unified diff of the NORMALIZED
    records (so it shows only genuine, non-accepted divergences) when ``parity``
    is False.
    """

    message_id: str
    shadow_file: str
    production_file: str
    parity: bool
    diff: str = ""


@dataclass
class ParityReport:
    """Result of :func:`compare_records`.

    ``matched`` — pairs present in BOTH dirs (the real compare). ``shadow_only``
    / ``production_only`` — Message-IDs present in only one side (window/folder
    mismatch, not a parity failure per se, but surfaced for the operator).
    """

    matched: list[ParityPair] = field(default_factory=list)
    shadow_only: list[str] = field(default_factory=list)
    production_only: list[str] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for p in self.matched if p.parity)

    @property
    def failed(self) -> int:
        return sum(1 for p in self.matched if not p.parity)

    @property
    def is_parity(self) -> bool:
        """Parity PROVEN only when there is at least one matched pair AND every
        matched pair passes. Zero matched pairs is INCONCLUSIVE, not proven —
        the caller must distinguish 'nothing to compare' from 'all passed'."""
        return bool(self.matched) and self.failed == 0


def _record_message_id(text: str) -> str:
    """Extract the ``**Message-ID:**`` value from a rendered record, or ""."""
    for line in text.splitlines():
        m = _MID_LINE_RE.match(line)
        if m:
            return m.group(1).strip()
    return ""


def _normalize_record_for_parity(text: str) -> list[str]:
    """Return the record's lines with the four ACCEPTED divergences normalized.

    Drops any ``**References:**`` line entirely; rewrites ``**From:**`` /
    ``**To:**`` to their bare normalized address (see :func:`_normalize_addr`);
    RFC2047-decodes the ``# <subject>`` heading (see :func:`_decode_rfc2047`,
    idempotent). Every other line is kept verbatim, so any FIFTH divergence
    survives into the byte-compare and fails parity — that's the safety property.
    """
    out: list[str] = []
    for line in text.splitlines():
        if _REFS_LINE_RE.match(line):
            continue
        m = _FROM_LINE_RE.match(line)
        if m:
            out.append(f"**From:** {_normalize_addr(m.group(1))}")
            continue
        m = _TO_LINE_RE.match(line)
        if m:
            out.append(f"**To:** {_normalize_addr(m.group(1))}")
            continue
        m = _HEADING_RE.match(line)
        if m:
            out.append(f"# {_decode_rfc2047(m.group(1))}")
            continue
        out.append(line)
    return out


def _index_records_by_mid(directory: Path) -> dict[str, tuple[str, str]]:
    """Map Message-ID → (filename, text) for every ``email-*.md`` in ``directory``.

    Records without a parseable Message-ID can't be joined; they're skipped with
    an ILB ``mail.parity.records_without_message_id`` count so the operator can
    see how many were unjoinable rather than silently dropped.
    """
    result: dict[str, tuple[str, str]] = {}
    if not directory.is_dir():
        log.warning("mail.parity.no_dir", path=str(directory))
        return result
    no_mid = 0
    for f in sorted(directory.glob("email-*.md")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        mid = _record_message_id(text)
        if not mid:
            no_mid += 1
            continue
        result[mid] = (f.name, text)
    if no_mid:
        log.info(
            "mail.parity.records_without_message_id",
            path=str(directory),
            count=no_mid,
        )
    return result


def compare_records(shadow_dir: Path, production_dir: Path) -> ParityReport:
    """Join shadow ↔ production ``email-*.md`` records by Message-ID and prove
    parity modulo the four ACCEPTED divergences.

    A matched pair PASSES when its records are identical after
    :func:`_normalize_record_for_parity` (From/To → bare address, References
    dropped, Subject RFC2047-decoded). Any other difference (Date, Account,
    Message-ID, In-Reply-To, body, structure — a fifth divergence) fails the
    pair and is captured as a unified diff of the normalized records. Returns a
    :class:`ParityReport`; the caller renders it and decides pass/fail.
    """
    shadow_by_mid = _index_records_by_mid(shadow_dir)
    prod_by_mid = _index_records_by_mid(production_dir)

    report = ParityReport()
    for mid in sorted(set(shadow_by_mid) & set(prod_by_mid)):
        sf, stext = shadow_by_mid[mid]
        pf, ptext = prod_by_mid[mid]
        snorm = _normalize_record_for_parity(stext)
        pnorm = _normalize_record_for_parity(ptext)
        if snorm == pnorm:
            report.matched.append(ParityPair(mid, sf, pf, True))
        else:
            diff = "\n".join(
                difflib.unified_diff(
                    pnorm, snorm,
                    fromfile=f"production/{pf}",
                    tofile=f"shadow/{sf}",
                    lineterm="",
                )
            )
            report.matched.append(ParityPair(mid, sf, pf, False, diff))

    report.shadow_only = sorted(set(shadow_by_mid) - set(prod_by_mid))
    report.production_only = sorted(set(prod_by_mid) - set(shadow_by_mid))
    return report
