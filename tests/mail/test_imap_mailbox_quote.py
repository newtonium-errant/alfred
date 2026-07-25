"""#7 IMAP mailbox-quoting pins — ``_imap_mailbox`` + its two SELECT sites.

imaplib does not quote mailbox names, so a spaced name (``[Gmail]/All Mail``) is sent as two tokens
and the server answers ``BAD Could not parse command``; only a double-quoted IMAP string works.
``_imap_mailbox`` wraps a spaced name; plain atoms (``INBOX``) and already-quoted names pass through.
These pin the rule and that ``fetch_account`` routes the folder through it. (The shadow path's quoting
is pinned in ``test_gmail_shadow_parity`` — the EXAMINE select there now receives the quoted form.)
"""
from __future__ import annotations

from alfred.mail.config import MailAccount
from alfred.mail.fetcher import _imap_mailbox, fetch_account
from alfred.mail.state import StateManager


# --- unit: the quoting rule -------------------------------------------------

def test_spaced_name_is_quoted():
    assert _imap_mailbox("[Gmail]/All Mail") == '"[Gmail]/All Mail"'
    assert _imap_mailbox("[Gmail]/Sent Mail") == '"[Gmail]/Sent Mail"'


def test_plain_atom_unchanged():
    assert _imap_mailbox("INBOX") == "INBOX"
    assert _imap_mailbox("[Gmail]/All") == "[Gmail]/All"   # no space → valid IMAP atom


def test_already_quoted_not_double_wrapped():
    assert _imap_mailbox('"[Gmail]/All Mail"') == '"[Gmail]/All Mail"'


def test_lone_quote_char_not_treated_as_already_quoted():
    # A single '"' must not satisfy the already-quoted guard (len >= 2 required).
    assert _imap_mailbox('"') == '"'


# --- integration: fetch_account routes the folder through the helper --------

class _RecordingIMAP:
    """Minimal fake: records select, returns an empty UNSEEN search so
    ``fetch_account`` just selects and finds nothing to pull."""

    def __init__(self, host, port, ssl_context=None):
        self.select_calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, addr, pw):
        return ("OK", [b"ok"])

    def select(self, folder, readonly=False):
        self.select_calls.append((folder, readonly))
        return ("OK", [b"0"])

    def search(self, charset, *criteria):
        return ("OK", [b""])          # no unseen → no fetch / store

    def logout(self):
        return ("OK", [b""])


def _install(monkeypatch):
    created: list[_RecordingIMAP] = []

    def _factory(host, port, ssl_context=None):
        inst = _RecordingIMAP(host, port, ssl_context=ssl_context)
        created.append(inst)
        return inst

    monkeypatch.setattr("imaplib.IMAP4_SSL", _factory)
    return created


def _acct(folders):
    return MailAccount(
        name="gmail", email="g@gmail.com", imap_host="imap.gmail.com",
        password="test-fetch-pw", fetch=True, mark_read=True, folders=folders,
    )


def test_fetch_account_quotes_spaced_folder(tmp_path, monkeypatch):
    created = _install(monkeypatch)
    state = StateManager(tmp_path / "state.json")
    state.load()
    fetch_account(_acct(["[Gmail]/All Mail"]), tmp_path, state)
    # mark_read=True → readonly=False on the production select; the spaced
    # mailbox is quoted.
    assert created[0].select_calls == [('"[Gmail]/All Mail"', False)]


def test_fetch_account_leaves_inbox_unquoted(tmp_path, monkeypatch):
    created = _install(monkeypatch)
    state = StateManager(tmp_path / "state.json")
    state.load()
    fetch_account(_acct(["INBOX"]), tmp_path, state)
    assert created[0].select_calls == [("INBOX", False)]
