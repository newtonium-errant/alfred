"""Gmail-side label-apply reconciliation loop (#7 7c-ii) — the live-mailbox-mutation slice.

Bridges 7c-i's vault classification to Gmail: 7c-i writes ``email_category`` + ``email_message_id`` onto
the note; this loop re-resolves each INBOX message by Message-ID and applies the category as a Gmail label
(``+X-GM-LABELS``) then archives it (``-X-GM-LABELS (\\Inbox)``). The vault note is the SINGLE SOURCE OF
TRUTH — the loop computes no category; a message whose note has no ``email_category`` is never touched.

SAFETY (this mutates the operator's live mailbox):
  * **Fail-CLOSED gate FIRST.** Every tick reads ``confidence.filing`` from the daily_sync state file (the
    ONE authoritative source, written by ``/calibration_ok filing``) BEFORE any IMAP connect. False (or a
    missing/corrupt state) ⇒ the tick returns immediately — no SELECT, no STORE, no archive, no connect.
  * **Label-first / archive-last.** A partial failure leaves the message LABELED and still in INBOX
    (findable, retried next tick, self-healing) — never lost, never half-placed. Archive only after a
    successful label.
  * **Idempotent by construction.** ``+X-GM-LABELS`` on an existing label is a Gmail no-op; archiving
    removes the message from INBOX, so the INBOX-driven scan never re-touches a filed message.
  * **Never touches uncategorized.** Only INBOX messages whose Message-ID resolves to a vault
    ``email_category`` are written; the rest stay in INBOX untouched (already ``\\Seen`` from 7a).
"""

from __future__ import annotations

import email
import email.policy
import imaplib
import json
import re
import ssl
from dataclasses import dataclass
from pathlib import Path

import frontmatter
import structlog

from .config import MailAccount, MailConfig

log = structlog.get_logger(__name__)


@dataclass
class GmailFilingSummary:
    """Per-tick outcome. ``gate_open`` False means the confidence.filing gate was closed → nothing ran."""

    gate_open: bool = False
    inbox_scanned: int = 0
    labeled: int = 0
    archived: int = 0
    skipped_no_category: int = 0
    label_failed: int = 0
    archive_failed: int = 0
    uid_errors: int = 0


# --- The fail-closed operator-approval gate --------------------------------


def read_filing_gate(confidence_state_path: str | Path) -> bool:
    """Read ``confidence.filing`` from the daily_sync state file. FAIL-CLOSED.

    Returns False on ANY uncertainty — missing file, corrupt JSON, invalid UTF-8, missing ``confidence``
    key, wrong types. Live-mailbox mutation must never fire on a stat/parse error. The gate IS the
    operator-approval barrier, so its fail-closed guarantee must be SELF-COMPLETE — it must not depend on
    the outer tick's except-belt. ``ValueError`` covers both ``json.JSONDecodeError`` (a ValueError
    subclass) and ``UnicodeDecodeError`` (from an invalid-UTF-8 state file). This reads the EXACT file
    ``/calibration_ok filing`` writes (the path is single-sourced from the daily_sync config), so the gate
    can never drift from the operator's actual approval."""
    try:
        state = json.loads(Path(confidence_state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(state, dict):
        return False
    confidence = state.get("confidence")
    if not isinstance(confidence, dict):
        return False
    return bool(confidence.get("filing"))


# --- The single source of truth: vault email_category ----------------------


def build_category_index(vault_path: Path) -> dict[str, str]:
    """Map ``email_message_id`` → ``email_category`` from vault ``note/*.md`` records.

    Only notes carrying BOTH a non-empty ``email_message_id`` (the join key, written by 7c-i) and a
    non-empty ``email_category`` (the label to apply) are indexed. This is the ONLY source the loop reads
    the category from — no recomputation, so a label can never disagree with the vault."""
    index: dict[str, str] = {}
    note_dir = vault_path / "note"
    if not note_dir.is_dir():
        return index
    for f in sorted(note_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(f))
        except Exception:  # noqa: BLE001 — a malformed note must not break the index build
            continue
        fm = post.metadata or {}
        mid = fm.get("email_message_id")
        cat = fm.get("email_category")
        if isinstance(mid, str) and mid and isinstance(cat, str) and cat:
            index[mid] = cat
    return index


# --- Message-ID parsing from a fetched header ------------------------------

_MID_HEADER_RE = re.compile(rb"(?im)^Message-ID:\s*(.+?)\s*$")


def _parse_message_id(header_bytes: bytes) -> str:
    """Extract the Message-ID value from a fetched ``HEADER.FIELDS (MESSAGE-ID)`` blob, or ""."""
    if not header_bytes:
        return ""
    m = _MID_HEADER_RE.search(header_bytes)
    return m.group(1).decode("utf-8", errors="replace").strip() if m else ""


# --- The write: label-first, archive-last ----------------------------------


def _gmail_label_value(category: str) -> str:
    """Render a category as an ``X-GM-LABELS`` argument: a quoted list, e.g. ``("Business/Receipts")``.

    Gmail renders the ``/`` as a nested label; quoting is defensive against spaces/special chars. The exact
    on-wire syntax is confirmed by the on-box archive-semantics verification (runbook) before go-live."""
    return f'("{category}")'


def _apply_label_and_archive(conn, uid: bytes, category: str, summary: GmailFilingSummary) -> None:
    """Apply ``category`` as a Gmail label, then archive (remove ``\\Inbox``). Label FIRST, archive LAST.

    A label-STORE failure aborts BEFORE archiving (the message stays in INBOX, unlabeled-or-labeled, safe
    to retry). An archive-STORE failure leaves the message LABELED and in INBOX (findable, retried next
    tick — re-adding the label next time is a Gmail no-op). Neither path loses or mis-places mail."""
    # Belt: label first.
    try:
        status, _ = conn.uid("STORE", uid, "+X-GM-LABELS", _gmail_label_value(category))
    except Exception as exc:  # noqa: BLE001 — a STORE fault must not crash the loop
        log.warning("gmail_filing.label_store_error", uid=uid, category=category, error=str(exc))
        summary.label_failed += 1
        return
    if status != "OK":
        log.warning("gmail_filing.label_store_failed", uid=uid, category=category, status=status)
        summary.label_failed += 1
        return
    summary.labeled += 1

    # Archive last (remove the Inbox label). Only reached after a successful label.
    try:
        status, _ = conn.uid("STORE", uid, "-X-GM-LABELS", "(\\Inbox)")
    except Exception as exc:  # noqa: BLE001
        log.warning("gmail_filing.archive_store_error", uid=uid, error=str(exc))
        summary.archive_failed += 1
        return
    if status != "OK":
        log.warning("gmail_filing.archive_store_failed", uid=uid, status=status)
        summary.archive_failed += 1
        return
    summary.archived += 1
    log.info("gmail_filing.filed", uid=uid, category=category)


def _file_account_inbox(account: MailAccount, index: dict[str, str], summary: GmailFilingSummary) -> None:
    """Process one account's INBOX: for each message with a categorized Message-ID, label + archive.

    UID-based throughout (UIDs are stable across the session even as messages leave INBOX; sequence
    numbers renumber). SELECT is read-WRITE (we mutate) — only reached when the gate is already open."""
    password = account.resolved_password()
    if not password:
        log.error("gmail_filing.no_password", account=account.name)
        return
    ctx = ssl.create_default_context()
    try:
        with imaplib.IMAP4_SSL(account.imap_host, account.imap_port, ssl_context=ctx) as conn:
            conn.login(account.email, password)
            status, _ = conn.select("INBOX", readonly=False)
            if status != "OK":
                log.warning("gmail_filing.select_failed", account=account.name)
                return
            status, data = conn.uid("SEARCH", None, "ALL")
            if status != "OK" or not data or not data[0]:
                log.info("gmail_filing.inbox_empty", account=account.name)
                return
            uids = data[0].split()
            for uid in uids:
                summary.inbox_scanned += 1
                status, msg_data = conn.uid(
                    "FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
                )
                if status != "OK" or not msg_data or not msg_data[0]:
                    summary.uid_errors += 1
                    continue
                header_bytes = msg_data[0][1] if isinstance(msg_data[0], tuple) else b""
                message_id = _parse_message_id(header_bytes or b"")
                category = index.get(message_id)
                if category is None:
                    # Never touch a message we didn't classify (n8n skip branch; already \Seen from 7a).
                    summary.skipped_no_category += 1
                    continue
                _apply_label_and_archive(conn, uid, category, summary)
    except imaplib.IMAP4.error as e:
        log.error("gmail_filing.imap_error", account=account.name, error=str(e))
    except Exception as e:  # noqa: BLE001 — a filing fault must never propagate
        log.error("gmail_filing.error", account=account.name, error=str(e))


def file_inbox_messages(
    config: MailConfig,
    vault_path: Path,
    confidence_state_path: str | Path,
) -> GmailFilingSummary:
    """One reconciliation tick. GATE FIRST — fail-closed, before any IMAP connect.

    Reads ``confidence.filing`` from ``confidence_state_path`` (the daily_sync state — single-sourced by
    the caller). Closed ⇒ returns immediately (``gate_open=False``), no connect. Open ⇒ builds the
    category index from the vault and labels+archives each categorized INBOX message. Returns a
    :class:`GmailFilingSummary`. Never raises."""
    summary = GmailFilingSummary()

    # THE GATE — the very first action, before any IMAP object is constructed. Fail-closed.
    if not read_filing_gate(confidence_state_path):
        log.debug("gmail_filing.gate_closed")
        return summary
    summary.gate_open = True

    accounts = config.fetch_accounts()
    if not accounts:
        log.info("gmail_filing.no_accounts")
        return summary

    index = build_category_index(vault_path)
    if not index:
        # ILB: gate is open but there's nothing classified to file yet — distinguishable from broken.
        log.info("gmail_filing.no_categorized_notes")
        return summary

    for account in accounts:
        _file_account_inbox(account, index, summary)

    log.info(
        "gmail_filing.tick_complete",
        inbox_scanned=summary.inbox_scanned,
        labeled=summary.labeled,
        archived=summary.archived,
        skipped_no_category=summary.skipped_no_category,
        label_failed=summary.label_failed,
        archive_failed=summary.archive_failed,
        uid_errors=summary.uid_errors,
    )
    return summary
