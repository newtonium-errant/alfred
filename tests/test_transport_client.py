"""Tests for ``alfred.transport.client``.

Exercises the real client against a mocked httpx transport — no
network, no live aiohttp server. Covers env resolution, retry
policy, exception mapping, and the subprocess-contract log shape.
"""

from __future__ import annotations

import json

import httpx
import pytest
import structlog

from alfred.transport import client as client_mod
from alfred.transport.exceptions import (
    TelegramUnavailable,
    TransportAuthMissing,
    TransportError,
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
    patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    """4xx failures emit ``code``, ``body``, and ``response_summary``.

    This is the adapted subprocess-failure contract from builder.md —
    ``response_summary`` is the grep-able one-line summary that lets
    operators find the failure class at a glance.

    The emit fires from inside ``await client_mod.send_outbound`` (an
    async coroutine), so we capture at structlog's processor chain via
    ``structlog.testing.capture_logs`` rather than scraping stdout with
    ``capsys``. The stdout-scrape variant passed in isolation but failed
    in a full-suite run: a sibling test that installs a global structlog
    config (renderer routed somewhere other than the capsys-captured
    stdout, e.g. a file sink or the JSON renderer) leaves that config
    live, so the rendered line never reaches capsys. ``capture_logs``
    intercepts above the final renderer and is config-independent —
    passes in both isolated and full-suite runs. Per
    ``feedback_structlog_assertion_patterns.md`` (the 5th of the
    ordering-pollution set; same trap as test_vision /
    test_session_substance_slug).
    """
    handlers, _ = patch_httpx
    handlers.append(
        lambda req: httpx.Response(
            400,
            json={"error": "user_id_and_text_required"},
        ),
    )
    with structlog.testing.capture_logs() as captured:
        with pytest.raises(TransportRejected):
            await client_mod.send_outbound(user_id=1, text="hi")

    matches = [
        c for c in captured
        if c.get("event") == "transport.client.nonzero_response"
    ]
    assert len(matches) == 1, (
        f"expected exactly one nonzero_response log, got {len(matches)}: "
        f"{[c.get('event') for c in captured]}"
    )
    entry = matches[0]
    # Subprocess-contract fields the assertion pins (structured kwargs,
    # not a rendered string): ``code``, ``body``, ``response_summary``.
    assert entry["code"] == 400
    assert "user_id_and_text_required" in entry["body"]
    assert entry["response_summary"].startswith("Status 400")


# ---------------------------------------------------------------------------
# 409 collapse — peer_propose_event mirrors peer_propose_canonical_record
# ---------------------------------------------------------------------------


async def test_peer_propose_event_409_collapses_to_structured_return(
    monkeypatch: pytest.MonkeyPatch, patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    """A 409 from /canonical/event/propose-create must NOT raise — the
    handler returns ``{"status": "exists", "path": ..., "correlation_id":
    ...}`` so the dispatcher can surface ``path`` to Andrew. Mirrors the
    sister handling in ``peer_propose_canonical_record``.
    """
    from alfred.transport.config import PeerEntry, TransportConfig

    cfg = TransportConfig()
    cfg.peers["salem"] = PeerEntry(
        base_url="http://127.0.0.1:8891",
        token=DUMMY_TRANSPORT_TEST_TOKEN,
    )

    handlers, _ = patch_httpx
    handlers.append(
        lambda req: httpx.Response(
            409,
            json={
                "status": "exists",
                "path": "event/Recurring Standup 2026-07-01.md",
                "correlation_id": "test-cid-event-409",
            },
        ),
    )

    result = await client_mod.peer_propose_event(
        peer_name="salem",
        title="Recurring Standup",
        start="2026-07-01T13:00:00-03:00",
        end="2026-07-01T14:00:00-03:00",
        config=cfg,
        self_name="hypatia",
        correlation_id="test-cid-event-409",
    )

    # Did NOT raise — instead, returned the parsed body verbatim.
    assert result["status"] == "exists"
    assert result["path"] == "event/Recurring Standup 2026-07-01.md"
    assert result["correlation_id"] == "test-cid-event-409"


async def test_peer_propose_event_409_with_unparseable_body_still_returns_dict(
    patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    """Defensive: if the 409 body is not valid JSON, still return a dict
    with ``status=exists`` + the correlation id rather than raising.
    """
    from alfred.transport.config import PeerEntry, TransportConfig

    cfg = TransportConfig()
    cfg.peers["salem"] = PeerEntry(
        base_url="http://127.0.0.1:8891",
        token=DUMMY_TRANSPORT_TEST_TOKEN,
    )

    handlers, _ = patch_httpx
    handlers.append(
        lambda req: httpx.Response(409, content=b"not json"),
    )

    result = await client_mod.peer_propose_event(
        peer_name="salem",
        title="Some title",
        start="2026-07-01T13:00:00-03:00",
        end="2026-07-01T14:00:00-03:00",
        config=cfg,
        self_name="hypatia",
        correlation_id="test-cid-event-409-bad",
    )

    assert result["status"] == "exists"
    assert result["correlation_id"] == "test-cid-event-409-bad"


async def test_peer_propose_event_4xx_other_than_409_still_raises(
    patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    """Only 409 is collapsed — other 4xx (e.g. 400 schema_error, 403
    spoofed origin) still raise TransportRejected so the caller sees the
    failure surface.
    """
    from alfred.transport.config import PeerEntry, TransportConfig

    cfg = TransportConfig()
    cfg.peers["salem"] = PeerEntry(
        base_url="http://127.0.0.1:8891",
        token=DUMMY_TRANSPORT_TEST_TOKEN,
    )

    handlers, _ = patch_httpx
    handlers.append(
        lambda req: httpx.Response(400, json={"reason": "schema_error"}),
    )

    with pytest.raises(TransportRejected) as exc:
        await client_mod.peer_propose_event(
            peer_name="salem",
            title="Some title",
            start="2026-07-01T13:00:00-03:00",
            end="2026-07-01T14:00:00-03:00",
            config=cfg,
            self_name="hypatia",
        )
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# PY-A — the client's half of the skip contract
#
# The server answers 503 ``{"status": "skipped", "reason":
# "telegram_unavailable"}`` for a send that was never delivered. Client-side
# that must become a NARROW exception, and it must not spend the retry
# budget: the server ANSWERED, so it is up, and the fact it reported cannot
# change in 2.5s. On a web-only instance this is every outbound send.
# ---------------------------------------------------------------------------


async def test_skip_maps_to_telegram_unavailable_without_retrying(
    patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    handlers, seen = patch_httpx
    # ONE handler queued on purpose: a retry would pop an empty queue and
    # raise AssertionError, so this pins the no-retry behaviour rather than
    # merely describing it.
    handlers.append(lambda req: httpx.Response(503, json={
        "status": "skipped", "delivered": False,
        "reason": "telegram_unavailable",
    }))

    with pytest.raises(TelegramUnavailable):
        await client_mod.send_outbound(user_id=1, text="hi")
    assert len(seen) == 1


async def test_the_skip_is_catchable_as_a_plain_transport_error(
    patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    """THE FAIL-CLOSED PROPERTY. Consumers written before this lane catch
    ``TransportError`` and record a non-delivery — correct, on day one,
    without knowing the narrow class exists."""
    handlers, _ = patch_httpx
    handlers.append(lambda req: httpx.Response(503, json={
        "reason": "telegram_unavailable",
    }))

    with pytest.raises(TransportError):
        await client_mod.send_outbound(user_id=1, text="hi")


async def test_telegram_not_configured_is_also_terminal(
    patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    """The sibling reason. Same fact ("there is no Telegram there"), reached
    by a different route (no callable registered) — so it gets the same
    no-retry treatment, but NOT the narrow type: nothing is dark, the wiring
    is missing."""
    handlers, seen = patch_httpx
    handlers.append(lambda req: httpx.Response(503, json={
        "reason": "telegram_not_configured",
    }))

    with pytest.raises(TransportUnavailable) as exc:
        await client_mod.send_outbound(user_id=1, text="hi")
    assert not isinstance(exc.value, TelegramUnavailable)
    assert len(seen) == 1


async def test_an_ordinary_5xx_still_retries(
    patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    """POSITIVE CONTROL for the two pins above: the retry budget they assert
    is skipped must demonstrably still exist for the transient case."""
    handlers, seen = patch_httpx
    handlers.append(lambda req: httpx.Response(503, json={"reason": "tmp"}))
    handlers.append(lambda req: httpx.Response(200, json={"id": "ok", "status": "sent"}))

    result = await client_mod.send_outbound(user_id=1, text="hi")
    assert result["id"] == "ok"
    assert len(seen) == 2


async def test_a_5xx_with_no_reason_field_still_retries(
    patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    """The terminal set is an ALLOWLIST of known-terminal reasons, so an
    unrecognised or absent reason falls through to the retry — unknown fails
    toward trying again, not toward giving up."""
    handlers, seen = patch_httpx
    handlers.append(lambda req: httpx.Response(500, text="upstream exploded"))
    handlers.append(lambda req: httpx.Response(200, json={"id": "ok", "status": "sent"}))

    result = await client_mod.send_outbound(user_id=1, text="hi")
    assert result["id"] == "ok"
    assert len(seen) == 2


async def test_the_batch_path_maps_the_skip_too(
    patch_httpx,  # type: ignore[no-untyped-def]
) -> None:
    """Brief and pending-items both go through the batch call — the two
    consumers whose failure modes cost the most."""
    handlers, seen = patch_httpx
    handlers.append(lambda req: httpx.Response(503, json={
        "status": "skipped", "reason": "telegram_unavailable",
    }))

    with pytest.raises(TelegramUnavailable):
        await client_mod.send_outbound_batch(user_id=1, chunks=["a"])
    assert len(seen) == 1
