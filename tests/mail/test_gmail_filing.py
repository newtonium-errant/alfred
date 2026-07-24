"""#7 7c-ii — the Gmail-side label-apply loop: the heaviest pins of the arc (live-mailbox mutation).

Covers the five heavy-gate conditions:
  1. INERT-gate bypass hunt — confidence.filing False → NO IMAP connect / SELECT / STORE / archive.
  2. Gate-integrity — the loop reads the EXACT file /calibration_ok filing writes (single-source, no drift).
  3. Fail-safe — label-first/archive-last; a STORE failure leaves the message labeled + in INBOX, never lost.
  4. Idempotent — a second tick over an already-filed (archived) message is a no-op.
  5. Never-mislabel — the label applied == the vault email_category verbatim; no-category → never touched.
Plus the orchestrator two-level gate (static enabled + dynamic per-tick) and loop fault-isolation.
UNCONDITIONAL.
"""

from __future__ import annotations

import imaplib
from pathlib import Path

import frontmatter
import pytest
import structlog

import alfred.orchestrator as orch
from alfred.daily_sync.config import ConfidenceConfig
from alfred.daily_sync.confidence import set_confidence
from alfred.mail.config import MailAccount, MailConfig, load_from_unified
from alfred.mail.gmail_filing import (
    build_category_index,
    file_inbox_messages,
    read_filing_gate,
)


# ===========================================================================
# Fake Gmail IMAP — records STOREs; archive removes the UID from the shared INBOX
# ===========================================================================

class _FakeGmailIMAP:
    def __init__(self, shared_inbox, fail_label, fail_archive, raise_label, raise_archive, fetch_fail):
        self.inbox = shared_inbox        # {uid_bytes: message_id_str} — PERSISTS across connections
        self.fail_label = fail_label       # STORE returns 'NO'
        self.fail_archive = fail_archive
        self.raise_label = raise_label     # STORE raises an exception (transport-fault simulation)
        self.raise_archive = raise_archive
        self.fetch_fail = fetch_fail       # FETCH returns 'NO' (UID-not-found)
        self.stores: list[tuple] = []
        self.select_calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, addr, pw):
        return ("OK", [b"ok"])

    def select(self, mailbox, readonly=False):
        self.select_calls.append((mailbox, readonly))
        return ("OK", [b"1"])

    def uid(self, command, *args):
        cmd = command.upper()
        if cmd == "SEARCH":
            keys = sorted(self.inbox.keys())
            return ("OK", [b" ".join(keys) if keys else b""])
        if cmd == "FETCH":
            uid = args[0]
            if uid in self.fetch_fail:
                return ("NO", [b""])   # UID-not-found (deleted/moved between SEARCH and FETCH)
            mid = self.inbox.get(uid, "")
            blob = b"Message-ID: " + mid.encode() + b"\r\n\r\n"
            return ("OK", [(b"x (UID " + uid + b")", blob), b")"])
        if cmd == "STORE":
            uid, op, val = args[0], args[1], args[2]
            self.stores.append((uid, op, val))
            if op == "+X-GM-LABELS":
                if uid in self.raise_label:
                    raise imaplib.IMAP4.error("simulated label STORE transport fault")
                return ("NO", [b"fail"]) if uid in self.fail_label else ("OK", [b""])
            if op == "-X-GM-LABELS":
                if uid in self.raise_archive:
                    raise imaplib.IMAP4.error("simulated archive STORE transport fault")
                if uid in self.fail_archive:
                    return ("NO", [b"fail"])
                self.inbox.pop(uid, None)  # archived → leaves INBOX (idempotency by construction)
                return ("OK", [b""])
        return ("OK", [b""])

    def logout(self):
        return ("OK", [b""])


def _install_fake(monkeypatch, inbox, *, fail_label=None, fail_archive=None,
                  raise_label=None, raise_archive=None, fetch_fail=None):
    shared = dict(inbox)
    created: list[_FakeGmailIMAP] = []

    def _factory(host, port, ssl_context=None):
        inst = _FakeGmailIMAP(
            shared, fail_label or set(), fail_archive or set(),
            raise_label or set(), raise_archive or set(), fetch_fail or set(),
        )
        created.append(inst)
        return inst

    monkeypatch.setattr("imaplib.IMAP4_SSL", _factory)
    return created, shared


def _gmail_config():
    return MailConfig(accounts=[MailAccount(
        name="gmail", email="g@gmail.com", imap_host="imap.gmail.com", password="pw", fetch=True,
    )])


def _vault_with_note(tmp_path, *, message_id, category):
    vault = tmp_path / "vault"
    (vault / "note").mkdir(parents=True)
    if message_id is not None:
        fm = {"type": "note", "email_category": category, "email_message_id": message_id}
        (vault / "note" / "n.md").write_text(frontmatter.dumps(frontmatter.Post("b", **fm)) + "\n", encoding="utf-8")
    return vault


def _gate_open(tmp_path) -> Path:
    """Write confidence.filing=True via the daily_sync writer; return that path (the single source)."""
    state = tmp_path / "daily_sync_state.json"
    set_confidence(state, "filing", True, seed=ConfidenceConfig())
    return state


def _gate_closed(tmp_path) -> Path:
    state = tmp_path / "daily_sync_state.json"
    set_confidence(state, "filing", False, seed=ConfidenceConfig())
    return state


# ===========================================================================
# 1. INERT-gate bypass hunt — gate closed → NO connect / SELECT / STORE
# ===========================================================================

def test_gate_closed_never_connects(tmp_path, monkeypatch):
    created, _ = _install_fake(monkeypatch, {b"1": "<a@x>"})
    vault = _vault_with_note(tmp_path, message_id="<a@x>", category="Business/Receipts")
    summary = file_inbox_messages(_gmail_config(), vault, _gate_closed(tmp_path))
    assert summary.gate_open is False
    assert created == []          # IMAP4_SSL was NEVER constructed — no connect, no SELECT, no STORE
    assert summary.labeled == 0 and summary.archived == 0


def test_gate_missing_state_is_fail_closed(tmp_path, monkeypatch):
    created, _ = _install_fake(monkeypatch, {b"1": "<a@x>"})
    vault = _vault_with_note(tmp_path, message_id="<a@x>", category="Business/Receipts")
    # No state file at all → fail-closed → no connect.
    summary = file_inbox_messages(_gmail_config(), vault, tmp_path / "does_not_exist.json")
    assert summary.gate_open is False and created == []


def test_read_filing_gate_fail_closed_variants(tmp_path):
    assert read_filing_gate(tmp_path / "nope.json") is False
    (tmp_path / "corrupt.json").write_text("{bad", encoding="utf-8")
    assert read_filing_gate(tmp_path / "corrupt.json") is False
    (tmp_path / "nokey.json").write_text("{}", encoding="utf-8")
    assert read_filing_gate(tmp_path / "nokey.json") is False


# ===========================================================================
# 2. Gate-integrity — the loop reads the SAME file /calibration_ok filing writes
# ===========================================================================

def test_gate_reads_the_daily_sync_written_file(tmp_path, monkeypatch):
    created, _ = _install_fake(monkeypatch, {b"1": "<a@x>"})
    vault = _vault_with_note(tmp_path, message_id="<a@x>", category="Business/Receipts")
    # Flip the gate via the daily_sync writer (what /calibration_ok filing calls), then point the loop at
    # that EXACT path — the gate must open (no drift, single source).
    state = _gate_open(tmp_path)
    summary = file_inbox_messages(_gmail_config(), vault, state)
    assert summary.gate_open is True
    assert len(created) == 1      # gate open → it DID connect


def test_gate_on_a_different_path_stays_closed(tmp_path, monkeypatch):
    # Flipping filing True on path A must NOT open a loop pointed at path B (proves no hidden second path).
    created, _ = _install_fake(monkeypatch, {b"1": "<a@x>"})
    vault = _vault_with_note(tmp_path, message_id="<a@x>", category="Business/Receipts")
    _gate_open(tmp_path)  # writes tmp_path/daily_sync_state.json
    other = tmp_path / "some_other_state.json"
    summary = file_inbox_messages(_gmail_config(), vault, other)
    assert summary.gate_open is False and created == []


# ===========================================================================
# 3. Fail-safe — label-first / archive-last
# ===========================================================================

def test_happy_path_labels_then_archives(tmp_path, monkeypatch):
    created, shared = _install_fake(monkeypatch, {b"1": "<a@x>"})
    vault = _vault_with_note(tmp_path, message_id="<a@x>", category="Business/Receipts")
    summary = file_inbox_messages(_gmail_config(), vault, _gate_open(tmp_path))
    conn = created[0]
    # Ordering: label FIRST, archive LAST.
    assert conn.stores == [
        (b"1", "+X-GM-LABELS", '("Business/Receipts")'),
        (b"1", "-X-GM-LABELS", "(\\Inbox)"),
    ]
    assert summary.labeled == 1 and summary.archived == 1
    assert b"1" not in shared  # archived → left INBOX


def test_label_failure_does_not_archive(tmp_path, monkeypatch):
    # A label STORE failure must abort BEFORE archiving — the message stays in INBOX, unarchived.
    created, shared = _install_fake(monkeypatch, {b"1": "<a@x>"}, fail_label={b"1"})
    vault = _vault_with_note(tmp_path, message_id="<a@x>", category="Business/Receipts")
    summary = file_inbox_messages(_gmail_config(), vault, _gate_open(tmp_path))
    conn = created[0]
    # Only the (failed) label STORE was attempted; NO archive STORE.
    assert conn.stores == [(b"1", "+X-GM-LABELS", '("Business/Receipts")')]
    assert summary.label_failed == 1 and summary.archived == 0
    assert b"1" in shared  # never left INBOX — safe, retriable


def test_archive_failure_leaves_message_labeled_in_inbox(tmp_path, monkeypatch):
    # Label succeeds, archive fails → message is labeled AND still in INBOX (findable, retried next tick).
    created, shared = _install_fake(monkeypatch, {b"1": "<a@x>"}, fail_archive={b"1"})
    vault = _vault_with_note(tmp_path, message_id="<a@x>", category="Business/Receipts")
    summary = file_inbox_messages(_gmail_config(), vault, _gate_open(tmp_path))
    assert summary.labeled == 1 and summary.archive_failed == 1 and summary.archived == 0
    assert b"1" in shared  # still in INBOX — not lost, not half-placed


# ===========================================================================
# 4. Idempotent — a second tick over an already-filed message is a no-op
# ===========================================================================

def test_second_tick_is_noop_after_filing(tmp_path, monkeypatch):
    created, shared = _install_fake(monkeypatch, {b"1": "<a@x>"})
    vault = _vault_with_note(tmp_path, message_id="<a@x>", category="Business/Receipts")
    gate = _gate_open(tmp_path)
    # Tick 1 files it (archives → leaves the shared INBOX).
    file_inbox_messages(_gmail_config(), vault, gate)
    assert b"1" not in shared
    # Tick 2: the message is gone from INBOX → SEARCH returns nothing → no STORE at all.
    file_inbox_messages(_gmail_config(), vault, gate)
    assert created[1].stores == []  # second connection issued zero STOREs — idempotent


# ===========================================================================
# 5. Never-mislabel — label == vault email_category; no-category → never touched
# ===========================================================================

def test_label_matches_vault_category_verbatim(tmp_path, monkeypatch):
    created, _ = _install_fake(monkeypatch, {b"1": "<a@x>"})
    vault = _vault_with_note(tmp_path, message_id="<a@x>", category="Finance/Tax")
    file_inbox_messages(_gmail_config(), vault, _gate_open(tmp_path))
    label_stores = [s for s in created[0].stores if s[1] == "+X-GM-LABELS"]
    assert label_stores == [(b"1", "+X-GM-LABELS", '("Finance/Tax")')]  # verbatim, no recomputation


def test_message_without_vault_category_is_never_touched(tmp_path, monkeypatch):
    # An INBOX message whose Message-ID has no categorized note is SKIPPED — no STORE, stays in INBOX.
    created, shared = _install_fake(monkeypatch, {b"1": "<uncategorized@x>"})
    vault = _vault_with_note(tmp_path, message_id="<a@x>", category="Business/Receipts")  # different mid
    summary = file_inbox_messages(_gmail_config(), vault, _gate_open(tmp_path))
    assert created[0].stores == []            # nothing written
    assert summary.skipped_no_category == 1
    assert b"1" in shared                     # untouched, still in INBOX


def test_index_excludes_notes_missing_either_field(tmp_path):
    vault = tmp_path / "v"
    (vault / "note").mkdir(parents=True)
    both = frontmatter.Post("b", type="note", email_category="Finance/Tax", email_message_id="<m@x>")
    cat_only = frontmatter.Post("b", type="note", email_category="Finance/Tax")
    mid_only = frontmatter.Post("b", type="note", email_message_id="<m2@x>")
    (vault / "note" / "a.md").write_text(frontmatter.dumps(both) + "\n", encoding="utf-8")
    (vault / "note" / "b.md").write_text(frontmatter.dumps(cat_only) + "\n", encoding="utf-8")
    (vault / "note" / "c.md").write_text(frontmatter.dumps(mid_only) + "\n", encoding="utf-8")
    assert build_category_index(vault) == {"<m@x>": "Finance/Tax"}


# ===========================================================================
# ILB + no-accounts
# ===========================================================================

def test_gate_open_no_categorized_notes_is_ilb(tmp_path, monkeypatch):
    created, _ = _install_fake(monkeypatch, {b"1": "<a@x>"})
    vault = tmp_path / "v"
    (vault / "note").mkdir(parents=True)  # no categorized notes
    with structlog.testing.capture_logs() as cap:
        summary = file_inbox_messages(_gmail_config(), vault, _gate_open(tmp_path))
    assert summary.gate_open is True and summary.labeled == 0
    assert [e for e in cap if e.get("event") == "gmail_filing.no_categorized_notes"]
    assert created == []  # no index → returned before connecting


# ===========================================================================
# Orchestrator: two-level gate (static enabled + dynamic per-tick) + fault-isolation
# ===========================================================================

def test_loop_not_started_when_disabled(tmp_path, monkeypatch):
    import threading
    called = {"n": 0}
    monkeypatch.setattr("alfred.mail.gmail_filing.file_inbox_messages", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    before = threading.active_count()
    cfg = load_from_unified({"mail": {"gmail_filing": {"enabled": False}, "accounts": [{"name": "gmail", "email": "g", "imap_host": "h", "fetch": True}]}})
    orch._maybe_start_gmail_filing_loop(cfg, tmp_path, str(tmp_path / "s.json"))
    assert threading.active_count() == before and called["n"] == 0


def test_loop_enabled_no_accounts_is_ilb(tmp_path, monkeypatch):
    import threading
    before = threading.active_count()
    cfg = load_from_unified({"mail": {"gmail_filing": {"enabled": True}, "accounts": [{"name": "live", "email": "a", "imap_host": "h"}]}})
    with structlog.testing.capture_logs() as cap:
        orch._maybe_start_gmail_filing_loop(cfg, tmp_path, str(tmp_path / "s.json"))
    assert threading.active_count() == before
    assert [e for e in cap if e.get("event") == "gmail_filing.loop_no_accounts"]


def test_tick_fault_isolated_logs_loop_error(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("transient")
    monkeypatch.setattr("alfred.mail.gmail_filing.file_inbox_messages", _boom)
    with structlog.testing.capture_logs() as cap:
        orch._gmail_filing_tick(_gmail_config(), tmp_path, str(tmp_path / "s.json"))  # must NOT raise
    assert [e for e in cap if e.get("event") == "gmail_filing.loop_error"]


# ===========================================================================
# Gate hardening (NIT-1 / NIT-2 / NIT-3) — the fail-closed contract must be self-complete,
# and every live-mailbox branch must be pinned.
# ===========================================================================

def test_gate_invalid_utf8_is_fail_closed(tmp_path, monkeypatch):
    # NIT-1: an invalid-UTF-8 confidence file raises UnicodeDecodeError (a ValueError). The gate's
    # fail-closed guarantee is SELF-COMPLETE — it returns False here, not relying on the outer belt.
    created, _ = _install_fake(monkeypatch, {b"1": "<a@x>"})
    vault = _vault_with_note(tmp_path, message_id="<a@x>", category="Business/Receipts")
    bad = tmp_path / "invalid_utf8.json"
    bad.write_bytes(b"\xff\xfe not valid utf-8 \x80")
    assert read_filing_gate(bad) is False
    summary = file_inbox_messages(_gmail_config(), vault, bad)
    assert summary.gate_open is False and created == []   # fail-closed → no connect


def test_gate_revocation_stops_writes_next_tick(tmp_path, monkeypatch):
    # NIT-2a: the per-tick re-read must REVOKE approval, not just grant it. A message kept in INBOX
    # (archive fails) would be re-processed next tick IF the gate stayed open — but after the operator
    # revokes, the next tick makes NO connection and touches nothing.
    created, shared = _install_fake(monkeypatch, {b"1": "<a@x>"}, fail_archive={b"1"})
    vault = _vault_with_note(tmp_path, message_id="<a@x>", category="Business/Receipts")
    gate = _gate_open(tmp_path)
    file_inbox_messages(_gmail_config(), vault, gate)     # tick 1 (open): labels, archive fails → stays
    assert b"1" in shared and len(created) == 1
    set_confidence(gate, "filing", False, seed=ConfidenceConfig())   # operator revokes
    file_inbox_messages(_gmail_config(), vault, gate)     # tick 2 (revoked): must stop
    assert len(created) == 1        # NO second connection — revocation took effect next tick
    assert b"1" in shared           # untouched


def test_uid_not_found_is_skipped_no_crash(tmp_path, monkeypatch):
    # NIT-2b: a UID whose FETCH fails (deleted/moved between SEARCH and FETCH) → counted + skipped, no crash.
    created, shared = _install_fake(monkeypatch, {b"1": "<a@x>"}, fetch_fail={b"1"})
    vault = _vault_with_note(tmp_path, message_id="<a@x>", category="Business/Receipts")
    summary = file_inbox_messages(_gmail_config(), vault, _gate_open(tmp_path))
    assert summary.uid_errors == 1 and summary.labeled == 0
    assert created[0].stores == []   # nothing written
    assert b"1" in shared


def test_label_store_exception_is_caught(tmp_path, monkeypatch):
    # NIT-2c: a raising label STORE (transport fault) is caught → label_failed, no archive, no crash.
    created, shared = _install_fake(monkeypatch, {b"1": "<a@x>"}, raise_label={b"1"})
    vault = _vault_with_note(tmp_path, message_id="<a@x>", category="Business/Receipts")
    summary = file_inbox_messages(_gmail_config(), vault, _gate_open(tmp_path))  # must NOT raise
    assert summary.label_failed == 1 and summary.archived == 0
    assert b"1" in shared   # never archived — safe


def test_archive_store_exception_is_caught(tmp_path, monkeypatch):
    # NIT-2c: a raising archive STORE is caught → labeled but archive_failed; message stays in INBOX.
    created, shared = _install_fake(monkeypatch, {b"1": "<a@x>"}, raise_archive={b"1"})
    vault = _vault_with_note(tmp_path, message_id="<a@x>", category="Business/Receipts")
    summary = file_inbox_messages(_gmail_config(), vault, _gate_open(tmp_path))  # must NOT raise
    assert summary.labeled == 1 and summary.archive_failed == 1
    assert b"1" in shared   # labeled + still in INBOX — retriable


def test_self_healing_retry_after_transient_archive_failure(tmp_path, monkeypatch):
    # NIT-2d: archive fails tick 1 (message labeled + in INBOX), the fault clears, tick 2 archives → filed.
    fail_archive = {b"1"}
    created, shared = _install_fake(monkeypatch, {b"1": "<a@x>"}, fail_archive=fail_archive)
    vault = _vault_with_note(tmp_path, message_id="<a@x>", category="Business/Receipts")
    gate = _gate_open(tmp_path)
    file_inbox_messages(_gmail_config(), vault, gate)   # tick 1: archive fails
    assert b"1" in shared
    fail_archive.discard(b"1")                          # transient fault clears
    file_inbox_messages(_gmail_config(), vault, gate)   # tick 2: re-label (Gmail no-op) + archive
    assert b"1" not in shared                           # self-healed — now filed


def test_seed_labels_are_x_gm_labels_safe():
    # NIT-3: a future label-set change can't introduce a malformed X-GM-LABELS arg — the seed labels
    # carry no quote/backslash that would break _gmail_label_value's quoting.
    from alfred.email_filing.rules import SEED_CATEGORY_LABELS
    for label in SEED_CATEGORY_LABELS:
        assert '"' not in label and "\\" not in label, label
