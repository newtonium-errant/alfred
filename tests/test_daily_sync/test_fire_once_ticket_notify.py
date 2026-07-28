"""#22b — ``fire_once`` end-to-end wiring for the ticket-notify section.

Pins the daemon path (not just the provider in isolation):

  * the assembled ``body`` carries the ticket-notify section;
  * BEST-EFFORT: a notify-store read that RAISES surfaces as the STATE-3
    loud ⚠️ AND the sync still assembles + fires (mirrors the brief-spool
    swallow discipline — the notify read must never block the fire);
  * READ-ONLY through the full fire: the store file is byte-unchanged
    after ``fire_once`` (the PWA tray still owns read/ack).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from alfred.daily_sync import assembler
from alfred.daily_sync.config import DailySyncConfig, TicketNotifyConfig
from alfred.daily_sync.daemon import fire_once
from alfred.web.identity import synthetic_chat_id
from alfred.web.notify_state import WebNotifyStore

OPERATOR = "Andrew"
TODAY = date(2026, 7, 19)


@pytest.fixture(autouse=True)
def _isolate() -> Any:
    from alfred.daily_sync import ticket_notify_section as tn

    tn.clear_raw_config()
    assembler.clear_providers()
    yield
    tn.clear_raw_config()
    assembler.clear_providers()


def _config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "corpus.jsonl")
    cfg.state.path = str(tmp_path / "daily_sync_state.json")
    cfg.ticket_notify = TicketNotifyConfig(enabled=True)
    return cfg


def _raw() -> dict[str, Any]:
    return {
        "web": {
            "enabled": True,
            "notifications": {"enabled": True},
            "users": [{"name": OPERATOR, "role": "owner", "email": "a@b.c"}],
        }
    }


def _store_path(cfg: DailySyncConfig) -> Path:
    return Path(cfg.state.path).parent / "web_notify_state.json"


def _enqueue(cfg: DailySyncConfig, *, text: str, issue_url: str = "") -> None:
    Path(cfg.state.path).parent.mkdir(parents=True, exist_ok=True)
    store = WebNotifyStore.create(_store_path(cfg))
    store.load()
    store.enqueue(
        synthetic_chat_id(OPERATOR),
        text=text,
        precedence="R",
        source="kal-le",
        ticket_uid="t1",
        issue_url=issue_url,
    )


def _patch_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_send_batch(
        user_id: int, chunks: list[str], *,
        dedupe_key: str | None = None, client_name: str | None = None,
    ) -> dict[str, Any]:
        return {"telegram_message_ids": [9001]}

    import alfred.transport.client as client_mod
    monkeypatch.setattr(client_mod, "send_outbound_batch", _fake_send_batch)


async def test_fire_once_surfaces_ticket_notify_in_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)
    _enqueue(
        cfg,
        text="New ticket: broken deploy",
        issue_url="https://github.com/acme/repo/issues/7",
    )

    result = await fire_once(
        cfg, tmp_path, user_id=42, today=TODAY, raw_config=_raw(),
    )

    assert result["ok"] is True
    body = result["body"]
    assert "### Ticket notifications (1)" in body
    assert "New ticket: broken deploy" in body
    assert "https://github.com/acme/repo/issues/7" in body


async def test_fire_once_store_read_error_still_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A notify-store read that RAISES becomes the loud ⚠️ in the body,
    and the fire still completes (best-effort — never blocks the sync)."""
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)
    _enqueue(cfg, text="unreachable")  # a notice exists, but the read fails

    def _boom(self: Any) -> None:
        raise OSError("disk gremlins")

    monkeypatch.setattr(
        "alfred.web.notify_state.WebNotifyStore.load", _boom,
    )

    result = await fire_once(
        cfg, tmp_path, user_id=42, today=TODAY, raw_config=_raw(),
    )

    # The sync still fired (assembled + pushed).
    assert result["ok"] is True
    assert result["message_ids"] == [9001]
    # And the body carries the loud STATE-3 warning, not a silent gap.
    body = result["body"]
    assert "### Ticket notifications ⚠️" in body
    assert "notify store read failed" in body


async def test_fire_once_leaves_store_read_flags_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """READ-ONLY through the full daemon path: the store file bytes and
    every ``read`` flag are unchanged after ``fire_once``."""
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)
    _enqueue(cfg, text="one", issue_url="")
    _enqueue(cfg, text="two", issue_url="")

    before = _store_path(cfg).read_bytes()
    result = await fire_once(
        cfg, tmp_path, user_id=42, today=TODAY, raw_config=_raw(),
    )
    assert result["ok"] is True
    after = _store_path(cfg).read_bytes()

    assert before == after
    data = json.loads(after.decode("utf-8"))
    entries = data["notifications"][str(synthetic_chat_id(OPERATOR))]
    assert len(entries) == 2
    assert all(e["read"] is False for e in entries)
