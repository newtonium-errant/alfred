"""Tests for ``alfred.transport.client``.

Exercises the real client against a mocked httpx transport — no
network, no live aiohttp server. Covers env resolution, retry
policy, exception mapping, and the subprocess-contract log shape.
"""

from __future__ import annotations

import json

import httpx
import pytest

from alfred.transport import client as client_mod
from alfred.transport.exceptions import (
    TransportAuthMissing,
    TransportRejected,
    TransportServerDown,
    TransportUnavailable,
)


DUMMY_TRANSPORT_TEST_TOKEN = "DUMMY_TRANSPORT_CLIENT_TEST_TOKEN_PLACEHOLDER_01234567890"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets a valid token in env unless it unsets it."""
    monkeypatch.setenv("ALFRED_TRANSPORT_TOKEN", DUMMY_TRANSPORT_TEST_TOKEN)
    monkeypatch.delenv("ALFRED_TRANSPORT_HOST", raising=False)
    monkeypatch.delenv("ALFRED_TRANSPORT_PORT", raising=False)


@pytest.fixture
def patch_httpx(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """Patch httpx.AsyncClient so the handler runs against an in-process
    MockTransport. Returns a list to which the test appends handler
    callables; the next request pops the first handler in order.
    """
    handler_queue: list = []
    requests_seen: list[httpx.Request] = []
    real_async_client = httpx.AsyncClient  # capture before we monkey-patch

    def _make_client(*args, **kwargs):  # type: ignore[no-untyped-def]
        def _dispatch(req: httpx.Request) -> httpx.Response:
            requests_seen.append(req)
            if not handler_queue:
                raise AssertionError(
                    f"unexpected request — no handlers queued: {req.url}",
                )
            handler = handler_queue.pop(0)
            return handler(req)

        # Use the real AsyncClient (captured above) so we don't recurse
        # into our own patched wrapper.
        return real_async_client(
            transport=httpx.MockTransport(_dispatch),
            timeout=kwargs.get("timeout"),
        )

    monkeypatch.setattr(client_mod.httpx, "AsyncClient", _make_client)
    # Short-circuit retry sleeps so tests don't actually wait 0.5s+.
    async def _fake_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(client_mod.asyncio, "sleep", _fake_sleep)
    return handler_queue, requests_seen


# ---------------------------------------------------------------------------
# Env resolution
# ---------------------------------------------------------------------------


async def test_missing_token_raises_transport_auth_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALFRED_TRANSPORT_TOKEN", raising=False)
    with pytest.raises(TransportAuthMissing) as exc:
        await client_mod.send_outbound(user_id=1, text="hi")
    # Message should mention the env var so the operator knows what
    # to fix.
    assert "ALFRED_TRANSPORT_TOKEN" in str(exc.value)


async def test_custom_host_and_port_env(
    monkeypatch: pytest.MonkeyPatch, patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setenv("ALFRED_TRANSPORT_HOST", "10.0.0.5")
    monkeypatch.setenv("ALFRED_TRANSPORT_PORT", "9001")
    handlers, seen = patch_httpx

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "status": "sent"})

    handlers.append(_handler)
    await client_mod.send_outbound(user_id=1, text="hi")
    assert seen[0].url.host == "10.0.0.5"
    assert seen[0].url.port == 9001


async def test_auto_detects_client_name_from_argv(
    monkeypatch: pytest.MonkeyPatch, patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setattr(client_mod.sys, "argv", ["alfred-brief", "generate"])
    handlers, seen = patch_httpx

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "status": "sent"})

    handlers.append(_handler)
    await client_mod.send_outbound(user_id=1, text="hi")
    assert seen[0].headers.get("X-Alfred-Client") == "brief"


async def test_explicit_client_name_overrides_autodetect(
    patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    handlers, seen = patch_httpx

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "x", "status": "sent"})

    handlers.append(_handler)
    await client_mod.send_outbound(
        user_id=1, text="hi", client_name="janitor",
    )
    assert seen[0].headers.get("X-Alfred-Client") == "janitor"


# ---------------------------------------------------------------------------
# Happy path — request shape
# ---------------------------------------------------------------------------


async def test_send_outbound_posts_expected_payload(
    patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    handlers, seen = patch_httpx
    captured: list[dict] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content.decode()))
        return httpx.Response(200, json={"id": "abc", "status": "sent"})

    handlers.append(_handler)
    result = await client_mod.send_outbound(
        user_id=42, text="hi", dedupe_key="k",
    )
    assert result["id"] == "abc"
    assert captured == [{"user_id": 42, "text": "hi", "dedupe_key": "k"}]
    assert seen[0].headers["Authorization"] == f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}"


async def test_send_outbound_batch_posts_chunks(
    patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    handlers, _ = patch_httpx
    captured: list[dict] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content.decode()))
        return httpx.Response(200, json={
            "id": "b1", "sent_count": 2, "telegram_message_ids": [10, 11],
        })

    handlers.append(_handler)
    result = await client_mod.send_outbound_batch(
        user_id=42, chunks=["one", "two"], dedupe_key="brief-2026-04-20",
    )
    assert result["sent_count"] == 2
    assert captured[0]["chunks"] == ["one", "two"]
    assert captured[0]["dedupe_key"] == "brief-2026-04-20"


async def test_send_outbound_batch_rejects_empty_chunks() -> None:
    with pytest.raises(TransportRejected):
        await client_mod.send_outbound_batch(user_id=1, chunks=[])


async def test_get_status_round_trip(patch_httpx) -> None:  # type: ignore[no-untyped-def]
    handlers, seen = patch_httpx

    def _handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "q", "status": "scheduled"})

    handlers.append(_handler)
    result = await client_mod.get_status("q")
    assert result["status"] == "scheduled"
    assert seen[0].url.path == "/outbound/status/q"
    assert seen[0].method == "GET"


# ---------------------------------------------------------------------------
# Retry policy — 5xx retries, 4xx never retries
# ---------------------------------------------------------------------------


async def test_retries_once_on_5xx_then_succeeds(
    patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    handlers, _ = patch_httpx
    handlers.append(lambda req: httpx.Response(503, json={"reason": "tmp"}))
    handlers.append(lambda req: httpx.Response(200, json={"id": "ok", "status": "sent"}))

    result = await client_mod.send_outbound(user_id=1, text="hi")
    assert result["id"] == "ok"


async def test_does_not_retry_on_4xx(patch_httpx) -> None:  # type: ignore[no-untyped-def]
    handlers, seen = patch_httpx
    # Only one handler — a second call would raise AssertionError.
    handlers.append(lambda req: httpx.Response(401, json={"error": "invalid_token"}))

    with pytest.raises(TransportRejected) as exc:
        await client_mod.send_outbound(user_id=1, text="hi")
    assert exc.value.status_code == 401
    assert "invalid_token" in exc.value.body
    # Exactly one request made.
    assert len(seen) == 1


async def test_exhausts_retries_on_persistent_5xx(
    patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    handlers, _ = patch_httpx
    # Initial attempt + two retries.
    handlers.append(lambda req: httpx.Response(502, json={"error": "bad"}))
    handlers.append(lambda req: httpx.Response(502, json={"error": "bad"}))
    handlers.append(lambda req: httpx.Response(502, json={"error": "bad"}))

    with pytest.raises(TransportUnavailable):
        await client_mod.send_outbound(user_id=1, text="hi")


async def test_connect_error_raises_server_down(
    monkeypatch: pytest.MonkeyPatch, patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    handlers, _ = patch_httpx

    def _raise(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    # All three slots raise — exhausted retries surface TransportServerDown.
    handlers.append(_raise)
    handlers.append(_raise)
    handlers.append(_raise)

    with pytest.raises(TransportServerDown):
        await client_mod.send_outbound(user_id=1, text="hi")


# ---------------------------------------------------------------------------
# Logging — subprocess-contract shape
# ---------------------------------------------------------------------------


async def test_failure_log_has_subprocess_contract_fields(
    patch_httpx, capsys,  # type: ignore[no-untyped-def]
) -> None:
    """4xx failures emit ``code``, ``body``, and ``response_summary``.

    This is the adapted subprocess-failure contract from builder.md —
    ``response_summary`` is the grep-able one-line summary that lets
    operators find the failure class at a glance. Structlog writes to
    stdout via its ConsoleRenderer; we capture that directly.
    """
    handlers, _ = patch_httpx
    handlers.append(
        lambda req: httpx.Response(
            400,
            json={"error": "user_id_and_text_required"},
        ),
    )
    with pytest.raises(TransportRejected):
        await client_mod.send_outbound(user_id=1, text="hi")

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "transport.client.nonzero_response" in output
    assert "code=400" in output
    assert "Status 400" in output
    assert "response_summary" in output
