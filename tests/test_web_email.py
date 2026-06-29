"""Tests for ``alfred.web.email`` — the Resend magic-link sender (mocked httpx)."""

from __future__ import annotations

import structlog

from alfred.web import email as email_mod
from alfred.web.config import WebEmailConfig
from alfred.web.email import email_configured, send_magic_link

CONFIGURED = WebEmailConfig(
    provider="resend",
    api_key="DUMMY_RESEND_TEST_KEY",
    from_address="bot@example.com",
)


class _FakeResp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _FakeAsyncClient:
    """Minimal async-context-manager stand-in for httpx.AsyncClient."""

    def __init__(self, resp=None, raise_exc=None, **_kw) -> None:
        self._resp = resp
        self._raise = raise_exc
        self.calls: list[dict] = []

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_a) -> bool:
        return False

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self._raise is not None:
            raise self._raise
        return self._resp


def _patch_httpx(monkeypatch, *, resp=None, raise_exc=None) -> list:
    created: list[_FakeAsyncClient] = []

    def _factory(**kw):
        client = _FakeAsyncClient(resp=resp, raise_exc=raise_exc, **kw)
        created.append(client)
        return client

    monkeypatch.setattr(email_mod.httpx, "AsyncClient", _factory)
    return created


# ---------------------------------------------------------------------------
# email_configured
# ---------------------------------------------------------------------------


def test_email_configured_true() -> None:
    assert email_configured(CONFIGURED) is True


def test_email_configured_false_when_empty() -> None:
    assert email_configured(WebEmailConfig(api_key="", from_address="b@e.com")) is False
    assert email_configured(WebEmailConfig(api_key="k", from_address="")) is False


def test_email_configured_false_when_unresolved_placeholder() -> None:
    cfg = WebEmailConfig(api_key="${RESEND_API_KEY}", from_address="b@e.com")
    assert email_configured(cfg) is False


# ---------------------------------------------------------------------------
# send_magic_link
# ---------------------------------------------------------------------------


async def test_send_not_configured_returns_false_and_logs(monkeypatch) -> None:
    created = _patch_httpx(monkeypatch, resp=_FakeResp(200))
    cfg = WebEmailConfig(api_key="", from_address="")
    with structlog.testing.capture_logs() as captured:
        ok = await send_magic_link(cfg, "a@e.com", "https://x/link")
    assert ok is False
    assert created == []  # never attempted the HTTP call
    assert any(c["event"] == "web.email.not_configured" for c in captured)


async def test_send_success(monkeypatch) -> None:
    created = _patch_httpx(monkeypatch, resp=_FakeResp(200, '{"id":"abc"}'))
    with structlog.testing.capture_logs() as captured:
        ok = await send_magic_link(
            CONFIGURED, "a@e.com", "https://x/link", instance_name="Salem"
        )
    assert ok is True
    assert any(c["event"] == "web.email.sent" for c in captured)
    # Posted to Resend with the bearer + payload.
    call = created[0].calls[0]
    assert call["url"].endswith("/emails")
    assert call["headers"]["Authorization"] == "Bearer DUMMY_RESEND_TEST_KEY"
    assert call["json"]["to"] == ["a@e.com"]
    assert call["json"]["from"] == "bot@example.com"


async def test_send_non_2xx_returns_false_and_logs(monkeypatch) -> None:
    _patch_httpx(monkeypatch, resp=_FakeResp(422, "bad payload"))
    with structlog.testing.capture_logs() as captured:
        ok = await send_magic_link(CONFIGURED, "a@e.com", "https://x/link")
    assert ok is False
    failed = [c for c in captured if c["event"] == "web.email.send_failed"]
    assert len(failed) == 1
    assert failed[0]["status"] == 422


async def test_send_transport_error_returns_false_and_logs(monkeypatch) -> None:
    _patch_httpx(monkeypatch, raise_exc=RuntimeError("connection reset"))
    with structlog.testing.capture_logs() as captured:
        ok = await send_magic_link(CONFIGURED, "a@e.com", "https://x/link")
    assert ok is False
    assert any(c["event"] == "web.email.send_error" for c in captured)


async def test_send_never_logs_the_link(monkeypatch) -> None:
    # The link carries the secret magic token — it must never appear in logs.
    _patch_httpx(monkeypatch, resp=_FakeResp(200))
    secret_link = "https://x/auth/callback?token=SECRET_TOKEN_VALUE"
    with structlog.testing.capture_logs() as captured:
        await send_magic_link(CONFIGURED, "a@e.com", secret_link)
    blob = repr(captured)
    assert "SECRET_TOKEN_VALUE" not in blob
