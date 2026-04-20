"""Tests for the brief daemon's post-write Telegram dispatch.

Covers:
- ``config.primary_telegram_user_id`` is resolved from the unified
  config's ``telegram.allowed_users[0]``.
- The brief is dispatched as Telegram chunks after write.
- Transport failures are log-and-continue — the brief stays in the
  vault even when the push fails.
- Missing telegram config skips the push silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from alfred.brief.config import BriefConfig, load_from_unified
from alfred.brief.daemon import _push_brief_to_telegram


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------


def test_load_from_unified_resolves_primary_user(tmp_path: Path) -> None:
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "brief": {"schedule": {"time": "06:00", "timezone": "America/Halifax"}},
        "telegram": {"allowed_users": [8661018406, 5555555]},
    }
    cfg = load_from_unified(raw)
    assert cfg.primary_telegram_user_id == 8661018406


def test_load_from_unified_no_telegram_section(tmp_path: Path) -> None:
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "brief": {"schedule": {"time": "06:00", "timezone": "America/Halifax"}},
    }
    cfg = load_from_unified(raw)
    assert cfg.primary_telegram_user_id is None


def test_load_from_unified_empty_allowed_users(tmp_path: Path) -> None:
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "brief": {"schedule": {"time": "06:00", "timezone": "America/Halifax"}},
        "telegram": {"allowed_users": []},
    }
    cfg = load_from_unified(raw)
    assert cfg.primary_telegram_user_id is None


# ---------------------------------------------------------------------------
# _push_brief_to_telegram — success path
# ---------------------------------------------------------------------------


async def test_push_brief_invokes_transport_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_send_batch(
        user_id: int,
        chunks: list[str],
        *,
        dedupe_key: str | None = None,
        client_name: str | None = None,
    ) -> dict[str, Any]:
        captured["user_id"] = user_id
        captured["chunks"] = chunks
        captured["dedupe_key"] = dedupe_key
        captured["client_name"] = client_name
        return {"id": "xyz", "sent_count": len(chunks)}

    # Patch the client-side function the daemon imports inline.
    import alfred.transport.client as client_mod
    monkeypatch.setattr(
        client_mod, "send_outbound_batch", _fake_send_batch,
    )

    await _push_brief_to_telegram(
        content="# Brief\n\nBody text that fits in one chunk.",
        today="2026-04-20",
        user_id=8661018406,
    )

    assert captured["user_id"] == 8661018406
    # Short content — single chunk.
    assert len(captured["chunks"]) == 1
    assert captured["dedupe_key"] == "brief-2026-04-20"
    assert captured["client_name"] == "brief"


async def test_push_brief_splits_into_multiple_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_chunks: list[list[str]] = []

    async def _fake_send_batch(user_id, chunks, **kw):  # type: ignore[no-untyped-def]
        captured_chunks.append(list(chunks))
        return {"sent_count": len(chunks)}

    import alfred.transport.client as client_mod
    monkeypatch.setattr(
        client_mod, "send_outbound_batch", _fake_send_batch,
    )

    # Build a brief that forces multi-chunk dispatch.
    p1 = "A" * 2000
    p2 = "B" * 2000
    p3 = "C" * 2000
    big_brief = f"{p1}\n\n{p2}\n\n{p3}"
    await _push_brief_to_telegram(big_brief, "2026-04-20", user_id=42)

    assert len(captured_chunks) == 1  # one batch call
    sent = captured_chunks[0]
    # Multi-chunk batch.
    assert len(sent) >= 2


async def test_push_brief_skips_empty_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty brief doesn't invoke the transport — no spurious 0-length batch."""
    calls: list = []

    async def _fake_send_batch(user_id, chunks, **kw):  # type: ignore[no-untyped-def]
        calls.append((user_id, chunks))
        return {}

    import alfred.transport.client as client_mod
    monkeypatch.setattr(
        client_mod, "send_outbound_batch", _fake_send_batch,
    )

    await _push_brief_to_telegram("", "2026-04-20", user_id=42)
    assert calls == []


# ---------------------------------------------------------------------------
# _push_brief_to_telegram — failure path
# ---------------------------------------------------------------------------


async def test_push_brief_transport_down_logged_and_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TransportServerDown does NOT propagate — brief stays in vault."""
    from alfred.transport.exceptions import TransportServerDown

    async def _boom(user_id, chunks, **kw):  # type: ignore[no-untyped-def]
        raise TransportServerDown("talker daemon is restarting")

    import alfred.transport.client as client_mod
    monkeypatch.setattr(
        client_mod, "send_outbound_batch", _boom,
    )

    # Spy on the daemon's structlog logger directly so this test
    # isn't sensitive to log-handler reconfiguration done by other
    # tests' ``setup_logging`` calls earlier in the suite.
    import alfred.brief.daemon as brief_daemon
    captured: list[dict] = []

    def _capture_warning(event: str, **kw):  # type: ignore[no-untyped-def]
        captured.append({"event": event, **kw})

    monkeypatch.setattr(brief_daemon.log, "warning", _capture_warning)

    # Must not raise.
    await _push_brief_to_telegram(
        "brief content", "2026-04-20", user_id=42,
    )

    # Exactly one warning emitted with the expected contract fields.
    assert len(captured) == 1
    entry = captured[0]
    assert entry["event"] == "brief.push_failed"
    assert entry["error_type"] == "TransportServerDown"
    assert "response_summary" in entry
    assert "TransportServerDown" in entry["response_summary"]


async def test_push_brief_rejected_logged_and_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config-level transport rejection is logged, not propagated."""
    from alfred.transport.exceptions import TransportRejected

    async def _boom(user_id, chunks, **kw):  # type: ignore[no-untyped-def]
        raise TransportRejected(
            "HTTP 401 from /outbound/send_batch", status_code=401,
        )

    import alfred.transport.client as client_mod
    monkeypatch.setattr(
        client_mod, "send_outbound_batch", _boom,
    )

    import alfred.brief.daemon as brief_daemon
    captured: list[dict] = []

    def _capture_warning(event: str, **kw):  # type: ignore[no-untyped-def]
        captured.append({"event": event, **kw})

    monkeypatch.setattr(brief_daemon.log, "warning", _capture_warning)

    await _push_brief_to_telegram(
        "brief content", "2026-04-20", user_id=42,
    )
    assert len(captured) == 1
    assert captured[0]["event"] == "brief.push_failed"
    assert captured[0]["error_type"] == "TransportRejected"
