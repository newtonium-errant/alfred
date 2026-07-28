"""#22b — Daily Sync ticket-notifications section provider tests.

Covers ``src/alfred/daily_sync/ticket_notify_section.py``:

  * OPT-IN: disabled (default) → provider returns None (section omitted).
  * STATE 1: notices present → ``### Ticket notifications (N)`` + one
    bullet per notice carrying text · precedence · source · issue_url.
  * STATE 2: genuinely none → explicit ``(0)`` header + the
    ``*(No ticket notifications since <last sync>)*`` intentionally-left-
    blank line (never a blank/omitted section).
  * STATE 3 (fail loud): a ⚠️ warning on each locally-detectable
    breakage — notifications disabled, web disabled, no operator, store
    read raised, web config unresolved.
  * WINDOW: only notices at/after the last-sync timestamp surface.
  * READ-ONLY: the store file (and its read/ack flags) is byte-unchanged
    after the section renders — the PWA tray owns read/ack.
  * BULLETS not numbered: no ``N.`` numbered lines (would collide with
    the reply parser's "item N" semantics).
  * Registration at priority 8, idempotent.
  * NOT-in-brief: the daily_sync provider is disjoint from the brief
    render.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import structlog

from alfred.daily_sync import assembler
from alfred.daily_sync.config import DailySyncConfig, TicketNotifyConfig
from alfred.daily_sync.ticket_notify_section import (
    SECTION_HEADER_BASE,
    clear_raw_config,
    register,
    set_raw_config,
    ticket_notify_section,
)
from alfred.web.identity import synthetic_chat_id
from alfred.web.notify_state import WebNotifyStore

from datetime import date

TODAY = date(2026, 7, 19)
OPERATOR = "Andrew"


@pytest.fixture(autouse=True)
def _isolate() -> Any:
    """Clear the raw-config holder + assembler registry around each test."""
    clear_raw_config()
    assembler.clear_providers()
    yield
    clear_raw_config()
    assembler.clear_providers()


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _config(tmp_path: Path, *, enabled: bool = True) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True)
    cfg.state.path = str(tmp_path / "daily_sync_state.json")
    cfg.ticket_notify = TicketNotifyConfig(enabled=enabled)
    return cfg


def _web_raw(
    *, enabled: bool = True, notifications: bool = True, users: bool = True,
) -> dict[str, Any]:
    block: dict[str, Any] = {
        "enabled": enabled,
        "notifications": {"enabled": notifications},
    }
    if users:
        block["users"] = [{"name": OPERATOR, "role": "owner", "email": "a@b.c"}]
    else:
        block["users"] = []
    return {"web": block}


def _store_path(cfg: DailySyncConfig) -> Path:
    return Path(cfg.state.path).parent / "web_notify_state.json"


def _enqueue(
    cfg: DailySyncConfig,
    *,
    text: str,
    precedence: str = "R",
    source: str = "kal-le",
    ticket_uid: str = "t1",
    issue_url: str = "",
) -> None:
    store = WebNotifyStore.create(_store_path(cfg))
    store.load()
    store.enqueue(
        synthetic_chat_id(OPERATOR),
        text=text,
        precedence=precedence,
        source=source,
        ticket_uid=ticket_uid,
        issue_url=issue_url,
    )


def _write_state_fired_at(cfg: DailySyncConfig, fired_at: datetime) -> None:
    """Persist a state file with a last-sync timestamp (window anchor)."""
    Path(cfg.state.path).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.state.path).write_text(
        json.dumps({"last_batch": {"fired_at": fired_at.isoformat()}}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Opt-in gate
# ---------------------------------------------------------------------------


def test_disabled_returns_none(tmp_path: Path) -> None:
    """Default-OFF: an instance that didn't opt in omits the section
    entirely (None), so its Daily Sync is byte-unchanged (no false ⚠️)."""
    cfg = _config(tmp_path, enabled=False)
    set_raw_config(_web_raw())
    assert ticket_notify_section(cfg, TODAY) is None


# ---------------------------------------------------------------------------
# STATE 1 — notices present
# ---------------------------------------------------------------------------


def test_state1_renders_notices(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    set_raw_config(_web_raw())
    _enqueue(
        cfg,
        text="New ticket: fix the widget",
        precedence="P",
        source="kal-le",
        issue_url="https://github.com/acme/repo/issues/42",
    )
    rendered = ticket_notify_section(cfg, TODAY)
    assert rendered is not None
    assert "### Ticket notifications (1)" in rendered
    assert "New ticket: fix the widget" in rendered
    assert "precedence P" in rendered
    assert "source kal-le" in rendered
    assert "https://github.com/acme/repo/issues/42" in rendered
    # Loud markers must NOT appear in the healthy state.
    assert "⚠️" not in rendered


def test_state1_is_bulleted_not_numbered(tmp_path: Path) -> None:
    """Observability surface, NOT a reply-routable batch — bullets, never
    a ``N.`` numbered line (which would collide with the reply parser's
    "item N ok" semantics)."""
    cfg = _config(tmp_path)
    set_raw_config(_web_raw())
    _enqueue(cfg, text="alpha")
    _enqueue(cfg, text="bravo")
    rendered = ticket_notify_section(cfg, TODAY)
    assert rendered is not None
    assert "### Ticket notifications (2)" in rendered
    assert "- alpha" in rendered
    assert "- bravo" in rendered
    # No numbered-list lines.
    assert "\n1. " not in rendered
    assert "\n2. " not in rendered


def test_state1_multiple_newest_first(tmp_path: Path) -> None:
    """``list_for`` returns newest-first; the render preserves that order."""
    cfg = _config(tmp_path)
    set_raw_config(_web_raw())
    _enqueue(cfg, text="older", ticket_uid="t1")
    _enqueue(cfg, text="newer", ticket_uid="t2")
    rendered = ticket_notify_section(cfg, TODAY)
    assert rendered is not None
    assert rendered.index("newer") < rendered.index("older")


# ---------------------------------------------------------------------------
# STATE 2 — genuinely none
# ---------------------------------------------------------------------------


def test_state2_explicit_none_missing_store(tmp_path: Path) -> None:
    """No store file yet (healthy, nothing notified) → the explicit ILB
    line, NEVER a blank/omitted section."""
    cfg = _config(tmp_path)
    set_raw_config(_web_raw())
    rendered = ticket_notify_section(cfg, TODAY)
    assert rendered is not None
    assert "### Ticket notifications (0)" in rendered
    assert "No ticket notifications since" in rendered
    assert "⚠️" not in rendered


def test_state2_empty_store(tmp_path: Path) -> None:
    """Store file exists but the operator's tray is empty → still ILB."""
    cfg = _config(tmp_path)
    set_raw_config(_web_raw())
    store = WebNotifyStore.create(_store_path(cfg))
    store.save()  # writes an empty store file
    rendered = ticket_notify_section(cfg, TODAY)
    assert rendered is not None
    assert "### Ticket notifications (0)" in rendered
    assert "No ticket notifications since" in rendered


# ---------------------------------------------------------------------------
# STATE 3 — fail loud
# ---------------------------------------------------------------------------


def test_state3_notifications_disabled(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    set_raw_config(_web_raw(notifications=False))
    rendered = ticket_notify_section(cfg, TODAY)
    assert rendered is not None
    assert rendered.startswith(f"{SECTION_HEADER_BASE} ⚠️")
    assert "⚠️" in rendered
    assert "web.notifications.enabled is false" in rendered


def test_state3_web_disabled(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    set_raw_config(_web_raw(enabled=False))
    rendered = ticket_notify_section(cfg, TODAY)
    assert rendered is not None
    assert "⚠️" in rendered
    assert "web.enabled is false" in rendered


def test_state3_no_operator(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    set_raw_config(_web_raw(users=False))
    rendered = ticket_notify_section(cfg, TODAY)
    assert rendered is not None
    assert "⚠️" in rendered
    assert "no web.users configured" in rendered


def test_state3_store_read_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store read that RAISES becomes the loud ⚠️ — and the provider
    itself never propagates the exception (best-effort discipline)."""
    cfg = _config(tmp_path)
    set_raw_config(_web_raw())

    def _boom(self: Any) -> None:
        raise OSError("disk gremlins")

    monkeypatch.setattr(
        "alfred.web.notify_state.WebNotifyStore.load", _boom,
    )
    rendered = ticket_notify_section(cfg, TODAY)  # MUST NOT raise
    assert rendered is not None
    assert "⚠️" in rendered
    assert "notify store read failed" in rendered
    assert "OSError" in rendered


def test_state3_web_config_unresolved(tmp_path: Path) -> None:
    """No raw config stashed AND no config_path → the pipeline can't be
    verified → loud ⚠️ (fail-closed), not a silent empty section."""
    cfg = _config(tmp_path)
    cfg.config_path = None  # nothing to load web config from
    # NOTE: _isolate cleared the raw-config holder, so this is genuinely
    # unresolvable.
    rendered = ticket_notify_section(cfg, TODAY)
    assert rendered is not None
    assert "⚠️" in rendered
    assert "could not resolve web config" in rendered


# ---------------------------------------------------------------------------
# WINDOW — since last sync
# ---------------------------------------------------------------------------


def test_window_filters_notices_before_last_sync(tmp_path: Path) -> None:
    """Only notices at/after the last-sync timestamp surface. A notice
    with a ts BEFORE the recorded ``last_batch.fired_at`` is windowed
    out; a fresh one surfaces."""
    cfg = _config(tmp_path)
    set_raw_config(_web_raw())
    now = datetime.now(timezone.utc)
    last_sync = now - timedelta(hours=2)
    _write_state_fired_at(cfg, last_sync)

    # Write the store file directly so we control each entry's ts.
    key = str(synthetic_chat_id(OPERATOR))
    old = {
        "id": "aaaa", "text": "stale ticket", "precedence": "R",
        "source": "kal-le", "ticket_uid": "old",
        "issue_url": "", "ts": (now - timedelta(hours=6)).isoformat(),
        "read": False,
    }
    fresh = {
        "id": "bbbb", "text": "fresh ticket", "precedence": "R",
        "source": "kal-le", "ticket_uid": "new",
        "issue_url": "", "ts": (now - timedelta(minutes=30)).isoformat(),
        "read": False,
    }
    _store_path(cfg).write_text(
        json.dumps({"version": 1, "notifications": {key: [old, fresh]}}),
        encoding="utf-8",
    )

    rendered = ticket_notify_section(cfg, TODAY)
    assert rendered is not None
    assert "### Ticket notifications (1)" in rendered
    assert "fresh ticket" in rendered
    assert "stale ticket" not in rendered
    # The window label reflects the last-sync anchor, not the 24h fallback.
    assert "since the last sync" in rendered


def test_window_falls_back_to_24h_without_state(tmp_path: Path) -> None:
    """No recoverable last-sync timestamp → the 24h fallback window; a
    notice from 30 min ago surfaces and the label says so."""
    cfg = _config(tmp_path)
    set_raw_config(_web_raw())
    _enqueue(cfg, text="recent ticket")
    rendered = ticket_notify_section(cfg, TODAY)
    assert rendered is not None
    assert "recent ticket" in rendered
    assert "the last 24h" in rendered


# ---------------------------------------------------------------------------
# READ-ONLY — the PWA tray owns read/ack
# ---------------------------------------------------------------------------


def test_read_only_store_bytes_unchanged(tmp_path: Path) -> None:
    """Rendering the section must NEVER mutate the store — the file bytes
    and every entry's ``read`` flag are unchanged after the render."""
    cfg = _config(tmp_path)
    set_raw_config(_web_raw())
    _enqueue(cfg, text="ticket one", ticket_uid="t1")
    _enqueue(cfg, text="ticket two", ticket_uid="t2")

    before = _store_path(cfg).read_bytes()
    rendered = ticket_notify_section(cfg, TODAY)
    assert rendered is not None
    assert "### Ticket notifications (2)" in rendered
    after = _store_path(cfg).read_bytes()

    assert before == after  # byte-identical — no save() happened
    # And no read flag flipped.
    data = json.loads(after.decode("utf-8"))
    entries = data["notifications"][str(synthetic_chat_id(OPERATOR))]
    assert all(e["read"] is False for e in entries)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_adds_provider_at_priority_8() -> None:
    register()
    assert "ticket_notify" in assembler.registered_providers()
    matches = [e for e in assembler._REGISTRY if e.name == "ticket_notify"]
    assert len(matches) == 1
    assert matches[0].priority == 8
    # No item_count_after — this section does not consume reply-routable
    # item numbers.
    assert matches[0].item_count_after is None


def test_register_is_idempotent() -> None:
    register()
    register()  # MUST NOT raise
    assert assembler.registered_providers().count("ticket_notify") == 1


# ---------------------------------------------------------------------------
# NOT in the morning brief
# ---------------------------------------------------------------------------


def test_provider_absent_from_brief_render() -> None:
    """The ticket-notify surface is DAILY-SYNC ONLY. Registering it on the
    daily_sync assembler must not leak into a rendered brief (the brief
    renders only the sections its own daemon hands it)."""
    from alfred.brief.config import BriefConfig
    from alfred.brief.renderer import render_brief

    register()  # onto the daily_sync assembler registry
    cfg = BriefConfig(vault_path="/tmp/unused-ticket-notify")
    _fm, body = render_brief("2026-07-19", [("Weather", "sunny")], cfg)
    assert SECTION_HEADER_BASE not in body
    assert "Ticket notifications" not in body


def test_brief_daemon_source_does_not_reference_ticket_notify() -> None:
    """Regression pin: the brief daemon must not import or wire the
    ticket-notify section (this feature is daily_sync ONLY, per the
    operator directive — 'not in the brief')."""
    import alfred.brief.daemon as brief_daemon

    src = Path(brief_daemon.__file__).read_text(encoding="utf-8")
    assert "ticket_notify" not in src
    assert "Ticket notifications" not in src
