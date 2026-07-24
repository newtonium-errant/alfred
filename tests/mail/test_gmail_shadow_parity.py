"""#7 7b — the shadow parity harness: read-only guarantee, fault-isolation, and the normalized compare.

Pins (all UNCONDITIONAL):
  * The READ-ONLY shadow fetch's four non-disruptive belts (EXAMINE + BODY.PEEK + no-STORE + shadow-dir)
    — the load-bearing safety property: a `--shadow` that could touch live Gmail or the production inbox
    is the whole risk. Each belt is asserted independently.
  * SINCE date-window + in-run Message-ID dedup.
  * NOTE-1 fault-isolation: `_fetch_tick` survives a raising `fetch_all` and logs `mail.fetch.loop_error`
    (so the loop + the webhook sharing its process never die on a transient IMAP fault).
  * NOTE-2 References-harmlessness: the extra `**References:**` line does NOT confuse the curator's
    header heuristics (`is_email_inbox` still True, `_extract_sender` still parses From, not References).
  * The display-name-From alias-fix: the fetcher restores the display name n8n's bare-address stripping
    dropped, so `_extract_sender` recovers it (the flip-time classifier behavior change, pinned).
  * The normalized compare on the three ACCEPTED divergences (From/To bare-address, References-only), and
    that a FOURTH divergence FAILS.

Subject-RFC2047-encoding is a candidate 4th divergence surfaced by the adversarial probe; its pin lands
once the accept/eliminate ruling is in.
"""

from __future__ import annotations

import email
import email.policy
from pathlib import Path

import pytest
import structlog

import alfred.orchestrator as orch
from alfred.mail import webhook
from alfred.mail.config import MailAccount, MailConfig
from alfred.mail.fetcher import (
    ParityPair,
    ParityReport,
    _build_markdown,
    _imap_since_date,
    _normalize_addr,
    _normalize_record_for_parity,
    compare_records,
    fetch_account_shadow,
    shadow_fetch_all,
)


# ===========================================================================
# Raw email fixtures
# ===========================================================================

# Bare-address control (mirrors the 7a byte-identity fixture): From/To are bare, ASCII subject.
_COMMON_RAW = (
    b"Subject: Invoice #123\r\nFrom: billing@vendor.com\r\nTo: andrewnewton965@gmail.com\r\n"
    b"Date: Thu, 23 Jul 2026 10:00:00 +0000\r\nMessage-ID: <abc@mail.gmail.com>\r\n"
    b"In-Reply-To: <prev@mail.gmail.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Your July invoice is attached. Total due is 500.00 by month end.\r\n"
)
_COMMON_BODY = "Your July invoice is attached. Total due is 500.00 by month end."

# Adversarial: display-name From, multi-recipient To (with display names), threaded (References).
_DISPLAY_MULTI_THREADED_RAW = (
    b"Subject: Invoice #123\r\n"
    b"From: Jamie Newton <jamie@example.com>\r\n"
    b"To: Andrew <andrewnewton965@gmail.com>, Team <team@example.com>\r\n"
    b"Date: Thu, 23 Jul 2026 10:00:00 +0000\r\nMessage-ID: <abc@mail.gmail.com>\r\n"
    b"In-Reply-To: <prev@mail.gmail.com>\r\n"
    b"References: <root@mail.gmail.com> <prev@mail.gmail.com>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Your July invoice is attached. Total due is 500.00 by month end.\r\n"
)

# Adversarial: RFC2047-encoded subject. policy.default DECODES it (=?UTF-8?B?...?= → 'Café ☕ Receipt');
# a raw header read keeps it encoded — the fourth accepted divergence.
_ENCODED_SUBJECT_RAW = (
    b"Subject: =?UTF-8?B?Q2Fmw6kg4piVIFJlY2VpcHQ=?=\r\n"
    b"From: cafe@example.com\r\nTo: andrewnewton965@gmail.com\r\n"
    b"Date: Thu, 23 Jul 2026 12:00:00 +0000\r\nMessage-ID: <enc@mail.gmail.com>\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
    b"Encoded subject body with plenty of visible text to clear the threshold.\r\n"
)
_ENCODED_SUBJECT_ENCODED = "=?UTF-8?B?Q2Fmw6kg4piVIFJlY2VpcHQ=?="
_ENCODED_SUBJECT_DECODED = "Café ☕ Receipt"
_ENCODED_SUBJECT_BODY = "Encoded subject body with plenty of visible text to clear the threshold."

# Adversarial: multipart/alternative (plain + html) — exercises the fetcher's multipart body extraction.
_MULTIPART_RAW = (
    b"Subject: Multipart Hello\r\nFrom: news@vendor.com\r\nTo: andrewnewton965@gmail.com\r\n"
    b"Date: Thu, 23 Jul 2026 11:00:00 +0000\r\nMessage-ID: <mp@mail.gmail.com>\r\n"
    b'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
    b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
    b"This is the plain part of a multipart message with plenty of visible text.\r\n"
    b"--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
    b"<p>This is the plain part of a multipart message with plenty of visible text.</p>\r\n"
    b"--B--\r\n"
)


def _msg(raw: bytes) -> email.message.EmailMessage:
    return email.message_from_bytes(raw, policy=email.policy.default)


def _webhook_record(*, subject, from_, to, date, message_id, in_reply_to, body, account="gmail") -> str:
    """Render a production (n8n webhook) record. From/To are the BARE addresses n8n's parseAddr produces
    (hand-written per fixture — NOT computed via the production normalizer, to keep the fixture honest)."""
    return webhook._build_markdown({
        "subject": subject, "from": from_, "to": to, "date": date, "account": account,
        "message_id": message_id, "in_reply_to": in_reply_to, "body": body,
    })


# ===========================================================================
# Unit: _normalize_addr mirrors n8n's parseAddr; _imap_since_date
# ===========================================================================

def test_normalize_addr_mirrors_n8n_parseaddr():
    # First <addr> if present; else first comma-part — exactly n8n's parseAddr.
    assert _normalize_addr("Jamie Newton <jamie@example.com>") == "jamie@example.com"
    assert _normalize_addr("jamie@example.com") == "jamie@example.com"
    assert _normalize_addr('"Chudnovsky, Paul" <p@cox.com>') == "p@cox.com"
    assert _normalize_addr("a@x.com, b@y.com") == "a@x.com"
    assert _normalize_addr("Andrew <andrewnewton965@gmail.com>, Team <team@example.com>") == "andrewnewton965@gmail.com"
    assert _normalize_addr("") == ""


def test_decode_rfc2047_decodes_and_is_idempotent():
    from alfred.mail.fetcher import _decode_rfc2047
    assert _decode_rfc2047("=?UTF-8?B?Q2Fmw6kg4piVIFJlY2VpcHQ=?=") == "Café ☕ Receipt"
    assert _decode_rfc2047("Invoice #123") == "Invoice #123"          # plain ASCII no-op
    assert _decode_rfc2047("Café ☕ Receipt") == "Café ☕ Receipt"       # already-decoded no-op
    assert _decode_rfc2047("") == ""
    # Idempotent: decoding a decoded value changes nothing (robust to both Gmail scenarios).
    once = _decode_rfc2047("=?UTF-8?Q?Re=3A_Faktura?= re: x")
    assert _decode_rfc2047(once) == once


def test_imap_since_date_lookback_explicit_and_bad():
    from datetime import date
    assert _imap_since_date(lookback_days=7, today=date(2026, 7, 23)) == "16-Jul-2026"
    assert _imap_since_date(since="2026-07-01") == "01-Jul-2026"
    assert _imap_since_date(today=date(2026, 7, 23)) == "16-Jul-2026"  # 7-day default
    with pytest.raises(ValueError):
        _imap_since_date(since="not-a-date")


# ===========================================================================
# Fake IMAP — records what the shadow path does to the connection
# ===========================================================================

class _FakeIMAP:
    def __init__(self, host, port, ssl_context=None, messages=None):
        self.host = host
        self.port = port
        self.messages = messages or []
        self.select_calls: list[tuple] = []
        self.search_calls: list[tuple] = []
        self.fetch_calls: list[tuple] = []
        self.store_calls: list[tuple] = []   # MUST stay empty in the shadow path
        self.logged_in = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, addr, pw):
        self.logged_in = True
        return ("OK", [b"ok"])

    def select(self, folder, readonly=False):
        self.select_calls.append((folder, readonly))
        return ("OK", [b"1"])

    def search(self, charset, *criteria):
        self.search_calls.append((charset, criteria))
        nums = b" ".join(str(i + 1).encode() for i in range(len(self.messages)))
        return ("OK", [nums])

    def fetch(self, num, spec):
        self.fetch_calls.append((num, spec))
        idx = int(num.decode() if isinstance(num, bytes) else num) - 1
        raw = self.messages[idx]
        header = b"%d (BODY[] {%d}" % (idx + 1, len(raw))
        return ("OK", [(header, raw), b")"])

    def store(self, num, flag, value):
        self.store_calls.append((num, flag, value))
        return ("OK", [b""])

    def logout(self):
        return ("OK", [b""])


def _install_fake_imap(monkeypatch, messages):
    created: list[_FakeIMAP] = []

    def _factory(host, port, ssl_context=None):
        inst = _FakeIMAP(host, port, ssl_context=ssl_context, messages=messages)
        created.append(inst)
        return inst

    monkeypatch.setattr("imaplib.IMAP4_SSL", _factory)
    return created


def _gmail_account():
    return MailAccount(name="gmail", email="g@gmail.com", imap_host="imap.gmail.com",
                       password="test-shadow-pw", fetch=True)


# ===========================================================================
# The four non-disruptive belts (load-bearing safety) — each asserted independently
# ===========================================================================

def test_shadow_fetch_readonly_four_belts(tmp_path, monkeypatch):
    created = _install_fake_imap(monkeypatch, [_COMMON_RAW, _DISPLAY_MULTI_THREADED_RAW])
    # NOTE: both fixtures share Message-ID <abc@mail.gmail.com>, so dedup → 1 written; the belt
    # assertions below are about HOW the connection was used, independent of the write count.
    shadow = tmp_path / "shadow"
    fetch_account_shadow(_gmail_account(), shadow, since="16-Jul-2026", folder="[Gmail]/All Mail")
    conn = created[0]

    # Belt 1: EXAMINE — the ONLY select is read-only.
    assert conn.select_calls == [("[Gmail]/All Mail", True)]
    # Belt 2: BODY.PEEK[] — every fetch peeks; RFC822 never appears.
    assert conn.fetch_calls, "expected at least one fetch"
    assert all(spec == "(BODY.PEEK[])" for _num, spec in conn.fetch_calls)
    assert all("RFC822" not in spec for _num, spec in conn.fetch_calls)
    # Belt 3: no STORE — the \Seen flag is never touched.
    assert conn.store_calls == []
    # Belt 4: writes land ONLY under the shadow dir.
    assert sorted(p.parent for p in shadow.glob("email-*.md")) == [shadow]
    # SINCE date-window search (regardless of \Seen).
    assert conn.search_calls == [(None, ("SINCE", "16-Jul-2026"))]


def test_shadow_default_folder_is_all_mail(tmp_path, monkeypatch):
    created = _install_fake_imap(monkeypatch, [_COMMON_RAW])
    fetch_account_shadow(_gmail_account(), tmp_path / "shadow", since="16-Jul-2026")  # no folder → default
    assert created[0].select_calls == [("[Gmail]/All Mail", True)]


def test_shadow_dedups_by_message_id(tmp_path, monkeypatch):
    # Two messages, same Message-ID → only one record written.
    _install_fake_imap(monkeypatch, [_COMMON_RAW, _COMMON_RAW])
    shadow = tmp_path / "shadow"
    n = fetch_account_shadow(_gmail_account(), shadow, since="16-Jul-2026")
    assert n == 1
    assert len(list(shadow.glob("email-*.md"))) == 1


def test_shadow_fetch_all_no_accounts_is_ilb(tmp_path, monkeypatch):
    # No fetch:true accounts → explicit ILB signal + zero writes (no thread, no connect).
    cfg = MailConfig(accounts=[MailAccount(name="live", email="a@live.ca", imap_host="h")])  # fetch=False
    cfg.fetch.shadow_dir = str(tmp_path / "shadow")
    with structlog.testing.capture_logs() as cap:
        total = shadow_fetch_all(cfg, since="16-Jul-2026")
    assert total == 0
    assert [e for e in cap if e.get("event") == "mail.shadow.no_accounts"]


def test_shadow_fetch_all_writes_under_configured_shadow_dir(tmp_path, monkeypatch):
    _install_fake_imap(monkeypatch, [_COMMON_RAW])
    cfg = MailConfig(accounts=[_gmail_account()])
    cfg.fetch.shadow_dir = str(tmp_path / "custom_shadow")
    total = shadow_fetch_all(cfg, since="16-Jul-2026")
    assert total == 1
    assert len(list((tmp_path / "custom_shadow").glob("email-*.md"))) == 1


# ===========================================================================
# NOTE-1 — fault isolation: _fetch_tick survives a raising fetch_all + logs loop_error
# ===========================================================================

def test_fetch_tick_survives_raising_fetch_all_and_logs(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("transient IMAP fault")
    monkeypatch.setattr("alfred.mail.fetcher.fetch_all", _boom)
    with structlog.testing.capture_logs() as cap:
        # MUST NOT raise — the loop (and the webhook sharing its process) survives the fault.
        orch._fetch_tick(MailConfig(), Path("/tmp"))
    errs = [e for e in cap if e.get("event") == "mail.fetch.loop_error"]
    assert len(errs) == 1
    assert errs[0].get("log_level") == "error"


def test_fetch_tick_happy_path_calls_fetch_all_only_flagged(monkeypatch):
    seen = {}
    def _spy(config, vault_path, *, only_flagged=False):
        seen["only_flagged"] = only_flagged
        return 0
    monkeypatch.setattr("alfred.mail.fetcher.fetch_all", _spy)
    orch._fetch_tick(MailConfig(), Path("/tmp"))
    assert seen["only_flagged"] is True


# ===========================================================================
# NOTE-2 — References is harmless to the curator's header heuristics;
#          display-name From restores the alias-match signal (the flip behavior change)
# ===========================================================================

def test_references_line_harmless_to_curator_header_heuristics():
    from alfred.email_classifier.classifier import _extract_sender, is_email_inbox
    raw_with_refs = _COMMON_RAW.replace(
        b"In-Reply-To: <prev@mail.gmail.com>\r\n",
        b"In-Reply-To: <prev@mail.gmail.com>\r\nReferences: <root@m> <prev@m>\r\n",
    )
    rec = _build_markdown(_msg(raw_with_refs), "gmail")
    assert "**References:**" in rec                      # the divergent line is present...
    assert is_email_inbox(rec) is True                    # ...but is_email_inbox is not confused
    addr, display = _extract_sender(rec)                  # ...and _extract_sender keys on From, not References
    assert addr == "billing@vendor.com"


def test_display_name_from_restores_sender_display_for_alias_match():
    # The flip-time behavior change (pinned): n8n stripped From to a bare address, suppressing the
    # classifier's display-name alias-match; the fetcher preserves it, so _extract_sender recovers it.
    from alfred.email_classifier.classifier import _extract_sender
    rec = _build_markdown(_msg(_DISPLAY_MULTI_THREADED_RAW), "gmail")
    addr, display = _extract_sender(rec)
    assert addr == "jamie@example.com"
    assert display == "Jamie Newton"


# ===========================================================================
# The normalized compare — three ACCEPTED divergences pass; a FOURTH fails
# ===========================================================================

def test_compare_passes_on_the_three_accepted_divergences(tmp_path):
    shadow = tmp_path / "shadow"; prod = tmp_path / "prod"
    shadow.mkdir(); prod.mkdir()
    # Fetcher record: full display-name From, multi-recipient To, a References line.
    fetch_rec = _build_markdown(_msg(_DISPLAY_MULTI_THREADED_RAW), "gmail")
    # Production record: n8n's bare From/To (hand-written), no References.
    prod_rec = _webhook_record(
        subject="Invoice #123", from_="jamie@example.com", to="andrewnewton965@gmail.com",
        date="Thu, 23 Jul 2026 10:00:00 +0000", message_id="<abc@mail.gmail.com>",
        in_reply_to="<prev@mail.gmail.com>", body=_COMMON_BODY,
    )
    (shadow / "email-gmail-aaa-invoice.md").write_text(fetch_rec, encoding="utf-8")
    (prod / "email-gmail-bbb-invoice.md").write_text(prod_rec, encoding="utf-8")

    report = compare_records(shadow, prod)
    assert report.is_parity
    assert report.passed == 1 and report.failed == 0
    assert report.shadow_only == [] and report.production_only == []
    # Pre-normalization the records genuinely differ (the divergences are REAL, not vacuous):
    assert fetch_rec != prod_rec
    assert "**References:**" in fetch_rec and "**References:**" not in prod_rec


def test_compare_from_to_references_normalize_equal():
    # Three of the four accepted divergences on an ASCII-subject fixture: after normalizing From/To→bare
    # and dropping References, the fetcher and production records are byte-identical.
    fetch_rec = _build_markdown(_msg(_DISPLAY_MULTI_THREADED_RAW), "gmail")
    prod_rec = _webhook_record(
        subject="Invoice #123", from_="jamie@example.com", to="andrewnewton965@gmail.com",
        date="Thu, 23 Jul 2026 10:00:00 +0000", message_id="<abc@mail.gmail.com>",
        in_reply_to="<prev@mail.gmail.com>", body=_COMMON_BODY,
    )
    assert _normalize_record_for_parity(fetch_rec) == _normalize_record_for_parity(prod_rec)


# ---- The FOURTH accepted divergence: Subject RFC2047 decode (scenario-agnostic) ----

def test_compare_passes_on_encoded_subject_scenario_a(tmp_path):
    # Scenario A: n8n passes the Subject RAW-encoded (fetcher decoded it). The decode-normalize makes
    # them equal; pre-normalization the headings genuinely differ (decoded vs encoded).
    shadow = tmp_path / "shadow"; prod = tmp_path / "prod"
    shadow.mkdir(); prod.mkdir()
    fetch_rec = _build_markdown(_msg(_ENCODED_SUBJECT_RAW), "gmail")
    prod_rec = _webhook_record(
        subject=_ENCODED_SUBJECT_ENCODED, from_="cafe@example.com", to="andrewnewton965@gmail.com",
        date="Thu, 23 Jul 2026 12:00:00 +0000", message_id="<enc@mail.gmail.com>",
        in_reply_to="", body=_ENCODED_SUBJECT_BODY,
    )
    assert f"# {_ENCODED_SUBJECT_DECODED}" in fetch_rec       # fetcher decoded
    assert _ENCODED_SUBJECT_ENCODED in prod_rec               # n8n raw-encoded
    (shadow / "email-gmail-enc.md").write_text(fetch_rec, encoding="utf-8")
    (prod / "email-gmail-enc2.md").write_text(prod_rec, encoding="utf-8")
    report = compare_records(shadow, prod)
    assert report.is_parity


def test_compare_passes_on_encoded_subject_scenario_b(tmp_path):
    # Scenario B: n8n/Gmail delivered the Subject ALREADY decoded. The idempotent decode is a no-op on
    # both sides → parity still holds. Proves the normalize is robust to Gmail's unknown behavior.
    shadow = tmp_path / "shadow"; prod = tmp_path / "prod"
    shadow.mkdir(); prod.mkdir()
    fetch_rec = _build_markdown(_msg(_ENCODED_SUBJECT_RAW), "gmail")
    prod_rec = _webhook_record(
        subject=_ENCODED_SUBJECT_DECODED, from_="cafe@example.com", to="andrewnewton965@gmail.com",
        date="Thu, 23 Jul 2026 12:00:00 +0000", message_id="<enc@mail.gmail.com>",
        in_reply_to="", body=_ENCODED_SUBJECT_BODY,
    )
    (shadow / "email-gmail-enc.md").write_text(fetch_rec, encoding="utf-8")
    (prod / "email-gmail-enc2.md").write_text(prod_rec, encoding="utf-8")
    report = compare_records(shadow, prod)
    assert report.is_parity


def test_compare_fails_on_genuinely_different_subject(tmp_path):
    # Honesty: decode-normalization tolerates the ENCODING delta only — a genuinely different subject
    # (both decoded) still FAILS. We normalize the encoding, not the content.
    shadow = tmp_path / "shadow"; prod = tmp_path / "prod"
    shadow.mkdir(); prod.mkdir()
    fetch_rec = _build_markdown(_msg(_ENCODED_SUBJECT_RAW), "gmail")   # # Café ☕ Receipt
    prod_rec = _webhook_record(
        subject="A Completely Different Subject", from_="cafe@example.com", to="andrewnewton965@gmail.com",
        date="Thu, 23 Jul 2026 12:00:00 +0000", message_id="<enc@mail.gmail.com>",
        in_reply_to="", body=_ENCODED_SUBJECT_BODY,
    )
    (shadow / "email-gmail-enc.md").write_text(fetch_rec, encoding="utf-8")
    (prod / "email-gmail-enc2.md").write_text(prod_rec, encoding="utf-8")
    report = compare_records(shadow, prod)
    assert not report.is_parity and report.failed == 1


def test_compare_fails_on_a_fourth_divergence(tmp_path):
    shadow = tmp_path / "shadow"; prod = tmp_path / "prod"
    shadow.mkdir(); prod.mkdir()
    fetch_rec = _build_markdown(_msg(_DISPLAY_MULTI_THREADED_RAW), "gmail")
    # A genuine content divergence (tampered body) beyond the accepted three MUST fail parity.
    prod_rec = _webhook_record(
        subject="Invoice #123", from_="jamie@example.com", to="andrewnewton965@gmail.com",
        date="Thu, 23 Jul 2026 10:00:00 +0000", message_id="<abc@mail.gmail.com>",
        in_reply_to="<prev@mail.gmail.com>", body="Total due is 999.99 — TAMPERED",
    )
    (shadow / "email-gmail-aaa.md").write_text(fetch_rec, encoding="utf-8")
    (prod / "email-gmail-bbb.md").write_text(prod_rec, encoding="utf-8")

    report = compare_records(shadow, prod)
    assert not report.is_parity
    assert report.failed == 1 and report.passed == 0
    assert report.matched[0].diff  # a unified diff was captured for the operator


def test_compare_fails_on_wrong_from_address(tmp_path):
    # Address-normalization is honest: a genuinely WRONG From address (not just a stripped display
    # name) still fails — we normalize the display-name delta, not the address itself.
    shadow = tmp_path / "shadow"; prod = tmp_path / "prod"
    shadow.mkdir(); prod.mkdir()
    fetch_rec = _build_markdown(_msg(_DISPLAY_MULTI_THREADED_RAW), "gmail")  # jamie@example.com
    prod_rec = _webhook_record(
        subject="Invoice #123", from_="IMPOSTER@evil.com", to="andrewnewton965@gmail.com",
        date="Thu, 23 Jul 2026 10:00:00 +0000", message_id="<abc@mail.gmail.com>",
        in_reply_to="<prev@mail.gmail.com>", body=_COMMON_BODY,
    )
    (shadow / "email-gmail-a.md").write_text(fetch_rec, encoding="utf-8")
    (prod / "email-gmail-b.md").write_text(prod_rec, encoding="utf-8")
    report = compare_records(shadow, prod)
    assert not report.is_parity and report.failed == 1


def test_compare_multipart_body_introduces_no_extra_divergence(tmp_path):
    # Adversarial: a multipart/alternative message. The fetcher extracts the plain part; feeding that
    # same body to the webhook side proves multipart adds no divergence beyond the accepted set.
    from alfred.mail.fetcher import _extract_text
    shadow = tmp_path / "shadow"; prod = tmp_path / "prod"
    shadow.mkdir(); prod.mkdir()
    fetch_rec = _build_markdown(_msg(_MULTIPART_RAW), "gmail")
    body, _ = _extract_text(_msg(_MULTIPART_RAW))
    prod_rec = _webhook_record(
        subject="Multipart Hello", from_="news@vendor.com", to="andrewnewton965@gmail.com",
        date="Thu, 23 Jul 2026 11:00:00 +0000", message_id="<mp@mail.gmail.com>",
        in_reply_to="", body=body,
    )
    (shadow / "email-gmail-mp.md").write_text(fetch_rec, encoding="utf-8")
    (prod / "email-gmail-mp2.md").write_text(prod_rec, encoding="utf-8")
    report = compare_records(shadow, prod)
    assert report.is_parity


def test_compare_accounts_unmatched_message_ids(tmp_path):
    shadow = tmp_path / "shadow"; prod = tmp_path / "prod"
    shadow.mkdir(); prod.mkdir()
    def _rec(mid):
        return _webhook_record(subject="S", from_="a@x.com", to="b@y.com", date="D",
                               message_id=mid, in_reply_to="", body="B")
    (shadow / "email-gmail-a.md").write_text(_rec("<A@m>"), encoding="utf-8")
    (shadow / "email-gmail-b.md").write_text(_rec("<B@m>"), encoding="utf-8")
    (prod / "email-gmail-a.md").write_text(_rec("<A@m>"), encoding="utf-8")
    (prod / "email-gmail-c.md").write_text(_rec("<C@m>"), encoding="utf-8")
    report = compare_records(shadow, prod)
    assert [p.message_id for p in report.matched] == ["<A@m>"]
    assert report.shadow_only == ["<B@m>"]
    assert report.production_only == ["<C@m>"]


def test_compare_zero_matched_is_not_parity(tmp_path):
    # ILB: zero matched pairs is INCONCLUSIVE, never "proven".
    shadow = tmp_path / "shadow"; prod = tmp_path / "prod"
    shadow.mkdir(); prod.mkdir()
    report = compare_records(shadow, prod)
    assert report.matched == []
    assert report.is_parity is False


def test_compare_records_without_message_id_are_skipped_with_ilb(tmp_path):
    shadow = tmp_path / "shadow"; prod = tmp_path / "prod"
    shadow.mkdir(); prod.mkdir()
    # A record with no Message-ID line can't be joined — skipped with an ILB count.
    (shadow / "email-gmail-nomid.md").write_text("# No id\n\n**From:** a@x.com\n\n---\n\nbody", encoding="utf-8")
    with structlog.testing.capture_logs() as cap:
        report = compare_records(shadow, prod)
    assert report.matched == []
    assert [e for e in cap if e.get("event") == "mail.parity.records_without_message_id"]
