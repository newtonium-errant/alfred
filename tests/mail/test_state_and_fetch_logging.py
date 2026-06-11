"""R1 pins (2026-06-11): the seen_ids/accounts misnomer fix.

``mail.state.loaded`` logged ``accounts=len(seen_ids)`` for months —
the field counted account BUCKETS in the seen-ids dedup map (accounts
that have fetched at least once), not configured IMAP accounts, and
``accounts=0`` sent a diagnosis down a "the account config isn't
loading" rabbit hole. These pins lock the rename AND the new
configured-account-truth log at the one place accounts are consumed
(``fetch_all``), per the log-emission test discipline (capture_logs
drives the production code path).
"""

from __future__ import annotations

import json

import structlog

from alfred.mail.config import MailAccount, MailConfig
from alfred.mail.state import StateManager


def test_state_loaded_logs_seen_ids_not_accounts(tmp_path) -> None:
    state_path = tmp_path / "mail_state.json"
    state_path.write_text(
        json.dumps({"seen_ids": {"live": ["id-1", "id-2", "id-3"]}}),
        encoding="utf-8",
    )
    mgr = StateManager(state_path)
    with structlog.testing.capture_logs() as captured:
        mgr.load()
    loaded = [c for c in captured if c.get("event") == "mail.state.loaded"]
    assert len(loaded) == 1
    # The renamed field carries the dedup-state count...
    assert loaded[0]["seen_ids"] == 1  # one account bucket in seen_ids
    # ...and the misleading name is GONE from this event.
    assert "accounts" not in loaded[0]


def test_fetch_all_logs_configured_account_truth(tmp_path, monkeypatch) -> None:
    """``mail.fetch.starting`` carries the real configured-account count
    and names — the truth the misnomer used to obscure."""
    from alfred.mail import fetcher as fetcher_mod

    config = MailConfig(
        accounts=[
            MailAccount(name="live", email="a@live.ca", imap_host="imap.live.com"),
        ],
        state_path=str(tmp_path / "mail_state.json"),
    )

    # No network: stub the per-account fetch.
    monkeypatch.setattr(
        fetcher_mod, "fetch_account", lambda account, inbox, mgr: 0,
    )

    with structlog.testing.capture_logs() as captured:
        total = fetcher_mod.fetch_all(config, tmp_path / "vault")

    assert total == 0
    starts = [c for c in captured if c.get("event") == "mail.fetch.starting"]
    assert len(starts) == 1
    assert starts[0]["accounts"] == 1
    assert starts[0]["account_names"] == ["live"]
