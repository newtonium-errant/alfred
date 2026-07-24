"""#7 7a — Gmail rehome INERT infra: config gate, fetch filtering, and the _build_markdown parity pin.

Pins: the INERT fetch gate (default off → no fetch thread, no IMAP connect); per-account fetch
selection (webhook-delivered accounts never double-fetched); the load-bearing `_build_markdown`
byte-equivalence between the webhook and fetcher paths (so the two manually-synced impls can't drift),
including the documented References divergence. UNCONDITIONAL.
"""

from __future__ import annotations

import email
import email.policy
import threading
from pathlib import Path

import pytest

import alfred.orchestrator as orch
from alfred.mail import webhook
from alfred.mail.config import MailFetchConfig, load_from_unified
from alfred.mail.fetcher import _build_markdown as fetcher_build


# ===========================================================================
# Config — the INERT fetch gate + per-account selection
# ===========================================================================

def _cfg(**mail):
    return load_from_unified({"mail": mail})


def test_fetch_gate_defaults_inert():
    # No fetch block → enabled=False (INERT): the loop does not run, matching today's behavior.
    assert _cfg(accounts=[{"name": "gmail", "email": "g", "imap_host": "h", "fetch": True}]).fetch.enabled is False


def test_fetch_accounts_selects_only_flagged():
    c = _cfg(accounts=[
        {"name": "live", "email": "a@live.ca", "imap_host": "imap-mail.outlook.com"},        # fetch absent → False
        {"name": "gmail", "email": "g@gmail.com", "imap_host": "imap.gmail.com", "fetch": True},
    ])
    assert [a.name for a in c.fetch_accounts()] == ["gmail"]      # live.ca (webhook) never native-fetched
    assert c.accounts[0].fetch is False and c.accounts[1].fetch is True


def test_fetch_poll_interval_fallback_and_override():
    assert _cfg(poll_interval=300, accounts=[]).fetch_poll_interval() == 300           # falls back
    assert _cfg(poll_interval=300, fetch={"poll_interval": 120}, accounts=[]).fetch_poll_interval() == 120


# ===========================================================================
# INERT gate — no thread / no connect when disabled (the load-bearing safety)
# ===========================================================================

def test_disabled_starts_no_thread_no_connect(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr("alfred.mail.fetcher.fetch_all", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    before = threading.active_count()
    orch._maybe_start_mail_fetch_loop(
        _cfg(fetch={"enabled": False}, accounts=[{"name": "gmail", "email": "g", "imap_host": "h", "fetch": True}]),
        Path("/tmp"))
    assert threading.active_count() == before      # NO thread started
    assert called["n"] == 0                         # fetch_all NEVER called → no IMAP connection


def test_enabled_but_no_fetch_accounts_is_ilb_no_thread(monkeypatch):
    import structlog
    called = {"n": 0}
    monkeypatch.setattr("alfred.mail.fetcher.fetch_all", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    before = threading.active_count()
    with structlog.testing.capture_logs() as cap:
        orch._maybe_start_mail_fetch_loop(
            _cfg(fetch={"enabled": True}, accounts=[{"name": "live", "email": "a", "imap_host": "h"}]),
            Path("/tmp"))
    assert threading.active_count() == before and called["n"] == 0   # gate on, nothing to pull → no thread
    # ILB (discipline #9): 'gate on, nothing to pull' is an explicit signal, distinguishable from off.
    assert [e for e in cap if e.get("event") == "mail.fetch.loop_no_accounts"]


def test_enabled_with_accounts_runs_fetch_only_flagged(monkeypatch):
    done = threading.Event()
    seen = {}

    def _spy(config, vault_path, *, only_flagged=False):
        seen["only_flagged"] = only_flagged
        seen["names"] = [a.name for a in config.fetch_accounts()]
        done.set()
        return 0
    monkeypatch.setattr("alfred.mail.fetcher.fetch_all", _spy)
    orch._maybe_start_mail_fetch_loop(
        _cfg(fetch={"enabled": True, "poll_interval": 1},
             accounts=[{"name": "gmail", "email": "g", "imap_host": "h", "fetch": True}]),
        Path("/tmp"))
    assert done.wait(timeout=5)                     # the daemon thread ran the fetch once
    assert seen["only_flagged"] is True and seen["names"] == ["gmail"]


# ===========================================================================
# The LOAD-BEARING _build_markdown byte-equivalence pin (webhook vs fetcher)
# ===========================================================================

def _fetcher_email(raw: bytes):
    """Build the fetcher's EmailMessage from RAW BYTES — the real production path (imaplib →
    message_from_bytes), so headers (esp. Date) are read verbatim, not reformatted by a programmatic set."""
    return email.message_from_bytes(raw, policy=email.policy.default)


_COMMON_RAW = (
    b"Subject: Invoice #123\r\nFrom: billing@vendor.com\r\nTo: andrewnewton965@gmail.com\r\n"
    b"Date: Thu, 23 Jul 2026 10:00:00 +0000\r\nMessage-ID: <abc@mail.gmail.com>\r\n"
    b"In-Reply-To: <prev@mail.gmail.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Your July invoice is attached. Total due is 500.00 by month end.\r\n"
)


def test_build_markdown_byte_identical_common_gmail():
    # The rehome is a drop-in ONLY if the native fetcher produces byte-identical records to the n8n
    # webhook for the same email. For the common Gmail case (all headers, no References, plain body) the
    # two manually-synced _build_markdown impls MUST be byte-identical — the drift guard.
    fm = fetcher_build(_fetcher_email(_COMMON_RAW), "gmail")
    wm = webhook._build_markdown({
        "subject": "Invoice #123", "from": "billing@vendor.com", "to": "andrewnewton965@gmail.com",
        "date": "Thu, 23 Jul 2026 10:00:00 +0000", "account": "gmail",
        "message_id": "<abc@mail.gmail.com>", "in_reply_to": "<prev@mail.gmail.com>",
        "body": "Your July invoice is attached. Total due is 500.00 by month end."})
    assert fm == wm


def test_build_markdown_references_is_the_only_divergence():
    # DOCUMENTED divergence (pinned so it can't silently grow): the fetcher emits a **References:** line
    # (it reads the raw email) that the webhook lacks (n8n's POST omits references). For a threaded email
    # the fetcher record = the webhook record + exactly that one line. Everything else stays identical.
    raw_with_refs = _COMMON_RAW.replace(
        b"In-Reply-To: <prev@mail.gmail.com>\r\n",
        b"In-Reply-To: <prev@mail.gmail.com>\r\nReferences: <root@mail.gmail.com> <prev@mail.gmail.com>\r\n")
    fm = fetcher_build(_fetcher_email(raw_with_refs), "gmail")
    wm = webhook._build_markdown({
        "subject": "Invoice #123", "from": "billing@vendor.com", "to": "andrewnewton965@gmail.com",
        "date": "Thu, 23 Jul 2026 10:00:00 +0000", "account": "gmail",
        "message_id": "<abc@mail.gmail.com>", "in_reply_to": "<prev@mail.gmail.com>",
        "body": "Your July invoice is attached. Total due is 500.00 by month end."})
    extra = [ln for ln in fm.splitlines() if ln not in wm.splitlines()]
    assert extra == ["**References:** <root@mail.gmail.com> <prev@mail.gmail.com>"]   # exactly one extra line


def test_account_name_gmail_keeps_filename_shape():
    # name: gmail (not the email) → the fetcher's **Account:** line + the email-gmail-*.md filename stay
    # byte-identical to the current n8n webhook output (which hardcodes account: "gmail").
    fm = fetcher_build(_fetcher_email(_COMMON_RAW), "gmail")
    assert "**Account:** gmail" in fm
