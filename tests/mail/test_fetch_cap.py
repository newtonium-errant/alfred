"""#7 bounded-batch cap pins — ``MailFetchConfig.max_per_run`` on the native fetch loop.

The Gmail intake was dead ~2 months → ~1100 unread. When native fetch flips on, an uncapped run
would pull ALL of them in one tick and flood the curator. The cap bounds messages PULLED per run;
the account's ``\\Seen`` mark IS the cursor (each run searches ``UNSEEN`` + marks processed messages
seen), so capped ticks drain the backlog N-at-a-time with NO extra state (requires ``mark_read: true``).

These pins prove: the ``\\Seen``-cursor drain across ticks (the whole basis for the no-extra-state
design), ``None`` = unlimited = pre-cap behavior, the cap does NOT touch the read-only shadow path,
and the ``capped`` drain-progress observability.
"""
from __future__ import annotations

import structlog

from alfred.mail.config import MailAccount, MailConfig
from alfred.mail.fetcher import fetch_account, shadow_fetch_all
from alfred.mail.state import StateManager


def _raw(i: int) -> bytes:
    """A minimal RFC822 message with a unique Message-ID + Subject."""
    return (
        f"From: sender{i}@example.com\r\n"
        f"To: me@example.com\r\n"
        f"Subject: msg {i}\r\n"
        f"Message-ID: <msg-{i}@example.com>\r\n"
        f"\r\n"
        f"body {i}\r\n"
    ).encode()


class _SeenAwareIMAP:
    """Fake IMAP that models the SERVER-SIDE ``\\Seen`` cursor.

    ``search(UNSEEN)`` returns only messages not yet flagged ``\\Seen``; ``store(+FLAGS \\Seen)`` marks
    them. The ``\\Seen`` set is SHARED across every connection the factory makes (the real Gmail server
    persists ``\\Seen`` across connections — ``fetch_account`` opens a fresh connection per run), which is
    exactly what makes the bounded drain work with no extra state. The existing shadow ``_FakeIMAP``
    returns every message on every search, so it can't prove this; hence a purpose-built fake here.
    """

    def __init__(self, host, port, ssl_context=None, messages=None, seen=None):
        self.host = host
        self.port = port
        self._messages = messages or []          # list[bytes], 1-indexed by IMAP num
        self._seen = seen if seen is not None else set()  # SHARED server-side \Seen (1-indexed nums)
        self.select_calls: list[tuple] = []
        self.store_calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, addr, pw):
        return ("OK", [b"ok"])

    def select(self, folder, readonly=False):
        self.select_calls.append((folder, readonly))
        return ("OK", [b"1"])

    def search(self, charset, *criteria):
        # UNSEEN semantics: only nums not yet flagged \Seen (the cursor).
        nums = [
            str(i + 1).encode()
            for i in range(len(self._messages))
            if (i + 1) not in self._seen
        ]
        return ("OK", [b" ".join(nums)])

    def fetch(self, num, spec):
        idx = int(num.decode() if isinstance(num, bytes) else num) - 1
        raw = self._messages[idx]
        header = b"%d (RFC822 {%d}" % (idx + 1, len(raw))
        return ("OK", [(header, raw), b")"])

    def store(self, num, flag, value):
        n = int(num.decode() if isinstance(num, bytes) else num)
        self.store_calls.append((num, flag, value))
        if flag == "+FLAGS" and "\\Seen" in value:
            self._seen.add(n)          # advance the shared server-side cursor
        return ("OK", [b""])

    def logout(self):
        return ("OK", [b""])


def _install(monkeypatch, messages):
    """Patch ``imaplib.IMAP4_SSL`` with a factory whose connections SHARE one ``\\Seen`` set
    (server-side persistence across the fresh-connection-per-run pattern)."""
    created: list[_SeenAwareIMAP] = []
    seen: set[int] = set()

    def _factory(host, port, ssl_context=None):
        inst = _SeenAwareIMAP(host, port, ssl_context=ssl_context, messages=messages, seen=seen)
        created.append(inst)
        return inst

    monkeypatch.setattr("imaplib.IMAP4_SSL", _factory)
    return created


def _gmail_account():
    return MailAccount(
        name="gmail", email="g@gmail.com", imap_host="imap.gmail.com",
        password="test-fetch-pw", fetch=True, mark_read=True, folders=["INBOX"],
    )


def test_cap_drains_backlog_n_at_a_time_via_seen_cursor(tmp_path, monkeypatch):
    """Backlog of 5, cap of 2 → three ticks write 2, 2, 1 as the SHARED ``\\Seen`` cursor advances;
    a fourth tick finds nothing. This DIRECTLY proves the ``\\Seen`` mark is the cursor — the basis for
    bounded drain with no extra state."""
    created = _install(monkeypatch, [_raw(i) for i in range(1, 6)])  # 5 messages
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    state = StateManager(tmp_path / "state.json")
    state.load()

    def _tick() -> int:
        return fetch_account(_gmail_account(), inbox, state, max_per_run=2)

    assert _tick() == 2
    assert _tick() == 2
    assert _tick() == 1
    assert _tick() == 0                       # fully drained — UNSEEN now empty

    # All 5 landed in the inbox, exactly once each.
    assert len(list(inbox.glob("email-gmail-*.md"))) == 5

    # One fresh connection per tick; \Seen store advanced the cursor 2 / 2 / 1 / 0.
    assert len(created) == 4
    assert [len(conn.store_calls) for conn in created] == [2, 2, 1, 0]


def test_none_max_per_run_is_unlimited_pre_cap_behavior(tmp_path, monkeypatch):
    """``None`` (and the omitted default) pull the whole backlog in one run — byte-unchanged from
    before the cap existed."""
    _install(monkeypatch, [_raw(i) for i in range(1, 6)])  # 5 messages
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    state = StateManager(tmp_path / "state.json")
    state.load()

    # Default (kwarg omitted) == None == unlimited.
    n = fetch_account(_gmail_account(), inbox, state)
    assert n == 5
    assert len(list(inbox.glob("email-gmail-*.md"))) == 5


def test_cap_does_not_apply_to_shadow_path(tmp_path, monkeypatch):
    """The cap lives on the native fetch path only. The read-only shadow harness is a separate
    function that never reads ``max_per_run`` — a cap there would be meaningless (BODY.PEEK, no
    mark-seen). Set the cap to 1 and confirm all 3 messages are still shadow-captured."""
    _install(monkeypatch, [_raw(i) for i in range(1, 4)])  # 3 messages
    cfg = MailConfig(accounts=[_gmail_account()])
    cfg.fetch.shadow_dir = str(tmp_path / "shadow")
    cfg.fetch.max_per_run = 1                 # would cap the NATIVE path to 1

    total = shadow_fetch_all(cfg, since="16-Jul-2026")
    assert total == 3                         # cap ignored on the shadow path
    assert len(list((tmp_path / "shadow").glob("email-*.md"))) == 3


def test_cap_emits_capped_drain_progress_observability(tmp_path, monkeypatch):
    """``mail.found`` carries ``found`` / ``count`` / ``capped`` so a drain's progress is visible in
    mail.log (intentionally-left-blank: a trimmed batch must be observable, not silent)."""
    _install(monkeypatch, [_raw(i) for i in range(1, 6)])  # 5 messages
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    state = StateManager(tmp_path / "state.json")
    state.load()

    # Capped run: found 5, pulled 2, capped True.
    with structlog.testing.capture_logs() as cap:
        fetch_account(_gmail_account(), inbox, state, max_per_run=2)
    found = [e for e in cap if e.get("event") == "mail.found"]
    assert len(found) == 1
    assert found[0]["found"] == 5
    assert found[0]["count"] == 2
    assert found[0]["capped"] is True
    assert found[0]["max_per_run"] == 2

    # Uncapped run over the remaining 3: capped False, max_per_run None.
    with structlog.testing.capture_logs() as cap2:
        fetch_account(_gmail_account(), inbox, state, max_per_run=None)
    found2 = [e for e in cap2 if e.get("event") == "mail.found"]
    assert len(found2) == 1
    assert found2[0]["found"] == 3
    assert found2[0]["count"] == 3
    assert found2[0]["capped"] is False
    assert found2[0]["max_per_run"] is None


# ===========================================================================
# NIT-2 — the mark_read=false silent-stall footgun: cap + mark_read:false
# stalls the drain (\Seen never set → UNSEEN never shrinks), so warn loud.
# ===========================================================================

def _acct(*, mark_read: bool, folders=None):
    return MailAccount(
        name="gmail", email="g@gmail.com", imap_host="imap.gmail.com",
        password="test-fetch-pw", fetch=True, mark_read=mark_read,
        folders=folders or ["INBOX"],
    )


def test_cap_with_mark_read_false_warns_of_stall(tmp_path, monkeypatch):
    """max_per_run set + mark_read=false → a loud warning (the drain would
    silently stall; \\Seen is the cursor)."""
    _install(monkeypatch, [_raw(i) for i in range(1, 4)])
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    state = StateManager(tmp_path / "state.json")
    state.load()
    with structlog.testing.capture_logs() as cap:
        fetch_account(_acct(mark_read=False), inbox, state, max_per_run=2)
    warns = [e for e in cap if e.get("event") == "mail.cap_without_mark_read"]
    assert len(warns) == 1
    assert warns[0]["max_per_run"] == 2
    assert warns[0]["log_level"] == "warning"


def test_cap_with_mark_read_true_does_not_warn(tmp_path, monkeypatch):
    """The safe combo (mark_read=true, the default) is silent — no footgun warn."""
    _install(monkeypatch, [_raw(i) for i in range(1, 4)])
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    state = StateManager(tmp_path / "state.json")
    state.load()
    with structlog.testing.capture_logs() as cap:
        fetch_account(_gmail_account(), inbox, state, max_per_run=2)  # mark_read=True
    assert [e for e in cap if e.get("event") == "mail.cap_without_mark_read"] == []


# ===========================================================================
# NIT-1 — multi-folder per-RUN budget carry + mail.cap_reached completeness.
# Spaceless folder names (INBOX / Archive) so _imap_mailbox (added in the quote
# commit) is a no-op and these pins hold both before AND after that commit.
# ===========================================================================

class _MultiFolderIMAP:
    """Fake whose ``search`` is folder-aware: ``select`` sets the active folder,
    ``search(UNSEEN)`` returns that folder's not-yet-``\\Seen`` nums."""

    def __init__(self, host, port, ssl_context=None, folders=None):
        self.host = host
        self.port = port
        self._folders = folders or {}          # {folder_name: [raw, ...]}
        self._seen: dict[str, set] = {}         # {folder: {seen nums}}
        self._active: str | None = None
        self.select_calls: list[tuple] = []
        self.store_calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, addr, pw):
        return ("OK", [b"ok"])

    def select(self, folder, readonly=False):
        self.select_calls.append((folder, readonly))
        self._active = folder
        self._seen.setdefault(folder, set())
        return ("OK", [b"1"])

    def search(self, charset, *criteria):
        msgs = self._folders.get(self._active, [])
        seen = self._seen.get(self._active, set())
        nums = [
            str(i + 1).encode() for i in range(len(msgs)) if (i + 1) not in seen
        ]
        return ("OK", [b" ".join(nums)])

    def fetch(self, num, spec):
        msgs = self._folders.get(self._active, [])
        idx = int(num.decode() if isinstance(num, bytes) else num) - 1
        raw = msgs[idx]
        header = b"%d (RFC822 {%d}" % (idx + 1, len(raw))
        return ("OK", [(header, raw), b")"])

    def store(self, num, flag, value):
        n = int(num.decode() if isinstance(num, bytes) else num)
        self.store_calls.append((self._active, num, flag, value))
        if flag == "+FLAGS" and "\\Seen" in value:
            self._seen.setdefault(self._active, set()).add(n)
        return ("OK", [b""])

    def logout(self):
        return ("OK", [b""])


def _install_multi(monkeypatch, folders):
    created: list[_MultiFolderIMAP] = []

    def _factory(host, port, ssl_context=None):
        inst = _MultiFolderIMAP(host, port, ssl_context=ssl_context, folders=folders)
        created.append(inst)
        return inst

    monkeypatch.setattr("imaplib.IMAP4_SSL", _factory)
    return created


def test_cap_budget_carries_across_folders(tmp_path, monkeypatch):
    """Cap 4 over INBOX=3 + Archive=3 → pulls exactly 4 (3 + 1): the budget is
    carried ACROSS folders, not applied per-folder (which would be 6, and
    unbounded-per-folder would be 8)."""
    folders = {
        "INBOX": [_raw(i) for i in range(1, 4)],
        "Archive": [_raw(i) for i in range(10, 13)],
    }
    created = _install_multi(monkeypatch, folders)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    state = StateManager(tmp_path / "state.json")
    state.load()

    n = fetch_account(_acct(mark_read=True, folders=["INBOX", "Archive"]), inbox, state, max_per_run=4)
    assert n == 4                                          # 3 + 1, not 6, not 8
    assert len(list(inbox.glob("email-gmail-*.md"))) == 4
    conn = created[0]
    # Both folders selected; \Seen stored 3 on INBOX + 1 on Archive.
    assert [f for f, _ro in conn.select_calls] == ["INBOX", "Archive"]
    per_folder = {}
    for folder, _num, _flag, _val in conn.store_calls:
        per_folder[folder] = per_folder.get(folder, 0) + 1
    assert per_folder == {"INBOX": 3, "Archive": 1}


def test_cap_exhausted_skips_later_folder_and_logs_cap_reached(tmp_path, monkeypatch):
    """Cap 3 with INBOX=3 → the budget is spent on INBOX, so Archive is skipped
    (never selected) and mail.cap_reached fires exactly once."""
    folders = {
        "INBOX": [_raw(i) for i in range(1, 4)],
        "Archive": [_raw(i) for i in range(10, 13)],
    }
    created = _install_multi(monkeypatch, folders)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    state = StateManager(tmp_path / "state.json")
    state.load()

    with structlog.testing.capture_logs() as cap:
        n = fetch_account(_acct(mark_read=True, folders=["INBOX", "Archive"]), inbox, state, max_per_run=3)
    assert n == 3
    # Archive never selected — the budget was exhausted first.
    assert [f for f, _ro in created[0].select_calls] == ["INBOX"]
    reached = [e for e in cap if e.get("event") == "mail.cap_reached"]
    assert len(reached) == 1
    assert reached[0]["max_per_run"] == 3
