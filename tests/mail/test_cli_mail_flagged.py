"""#7 `alfred mail fetch --flagged` pins — the scoped manual/overnight drain.

The manual CLI calls `fetch_all(only_flagged=False)` → ALL accounts (live/Outlook + gmail). The
overnight Gmail drain must be gmail-only, else it also IMAP-fetches the dead live.ca/Outlook account
every tick. `--flagged` threads `only_flagged=True` (fetch:true accounts only — the daemon loop's
scope, reusing `fetch_accounts()`); absent → current all-accounts behavior, byte-unchanged.

Pins: the flag threads through to `fetch_all`; end-to-end it fetches ONLY gmail (no flag → both);
and it composes with `--once` + the `max_per_run` cap (one capped gmail-only run).
"""
from __future__ import annotations

import argparse

from alfred import cli


def _raw(vault_path, *, max_per_run=None):
    mail = {
        "accounts": [
            {"name": "live", "email": "a@live.ca", "imap_host": "imap-mail.outlook.com"},  # fetch absent → False
            {"name": "gmail", "email": "g@gmail.com", "imap_host": "imap.gmail.com", "fetch": True},
        ],
        "state": {"path": str(vault_path / "mail_state.json")},
    }
    if max_per_run is not None:
        mail["fetch"] = {"max_per_run": max_per_run}
    return {"vault": {"path": str(vault_path)}, "mail": mail}


def _args(*, flagged, once=True):
    return argparse.Namespace(
        config="unused", mail_cmd="fetch", shadow=False, once=once, flagged=flagged,
    )


def _patch_env(monkeypatch, raw):
    monkeypatch.setattr(cli, "_load_unified_config", lambda path: raw)
    monkeypatch.setattr(cli, "_setup_logging_from_config", lambda *a, **k: None)


def test_cli_fetch_flagged_threads_only_flagged_true(tmp_path, monkeypatch):
    """--flagged → fetch_all(only_flagged=True), called exactly once with --once."""
    calls = []

    def _spy(config, vault_path, *, only_flagged=False):
        calls.append(only_flagged)
        return 0

    _patch_env(monkeypatch, _raw(tmp_path))
    monkeypatch.setattr("alfred.mail.fetcher.fetch_all", _spy)
    cli.cmd_mail(_args(flagged=True))
    assert calls == [True]                    # threaded True, one run (--once)


def test_cli_fetch_no_flag_is_all_accounts(tmp_path, monkeypatch):
    """No flag → fetch_all(only_flagged=False) — current behavior, byte-unchanged."""
    calls = []

    def _spy(config, vault_path, *, only_flagged=False):
        calls.append(only_flagged)
        return 0

    _patch_env(monkeypatch, _raw(tmp_path))
    monkeypatch.setattr("alfred.mail.fetcher.fetch_all", _spy)
    cli.cmd_mail(_args(flagged=False))
    assert calls == [False]


def test_cli_flagged_fetches_only_gmail_and_composes_with_cap(tmp_path, monkeypatch):
    """End-to-end through the REAL fetch_all: --flagged --once with max_per_run=2 →
    only gmail's fetch_account is invoked (live.ca skipped) AND it receives the cap.
    Proves the three compose: scoped (gmail-only) + one-shot + capped."""
    fetched = []

    def _spy_account(account, inbox_path, state_mgr, *, max_per_run=None):
        fetched.append((account.name, max_per_run))
        return 0

    _patch_env(monkeypatch, _raw(tmp_path, max_per_run=2))
    monkeypatch.setattr("alfred.mail.fetcher.fetch_account", _spy_account)
    cli.cmd_mail(_args(flagged=True))
    # live.ca (fetch:false) skipped; gmail pulled with the cap threaded through.
    assert fetched == [("gmail", 2)]


def test_cli_no_flag_fetches_both_accounts_end_to_end(tmp_path, monkeypatch):
    """No flag through the REAL fetch_all → both accounts fetched (order preserved)."""
    fetched = []

    def _spy_account(account, inbox_path, state_mgr, *, max_per_run=None):
        fetched.append(account.name)
        return 0

    _patch_env(monkeypatch, _raw(tmp_path))
    monkeypatch.setattr("alfred.mail.fetcher.fetch_account", _spy_account)
    cli.cmd_mail(_args(flagged=False))
    assert fetched == ["live", "gmail"]
