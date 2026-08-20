"""Capture toggle — the web door onto capture-as-a-mode (R1, 2026-08-20).

Pins for the C1 slice: the ``/chat/capture`` toggle, server-truth span
state, and the turn/stream paths' capture gating.

The load-bearing absence pin — "toggle on → NO model invocation" — is
asserted against the shared ``FakeAnthropicClient``'s call counter with
its positive control IN THE SAME TEST (a normal turn DOES invoke), so the
pin cannot go green against a build where the whole engine is dead.
"""

from __future__ import annotations

import asyncio

import pytest
import structlog

from alfred.telegram import capture_spans
from alfred.telegram.state import StateManager
from alfred.transport.server import build_app
from alfred.transport.state import TransportState
from alfred.web.auth import USER_HEADER
from alfred.web.routes_chat import register_web_routes
from alfred.web.state import WebAuthState

from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse
from tests.test_web_routes_chat import (
    DUMMY_WEB_INGEST_TOKEN,
    _make_talker_config,
    _parse_sse,
    _session_headers,
    _transport_config,
    _web_config,
)


@pytest.fixture
async def capture_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """A web-routes transport app whose fake Anthropic client is EXPOSED.

    Returns ``(client, fake)`` so tests can read ``fake.messages.calls`` —
    the no-model-call instrument every capture pin leans on.
    """
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)

    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    talker_config = _make_talker_config(tmp_path)
    web_auth_state = WebAuthState.create(tmp_path / "web_auth_state.json")
    web_auth_state.load()
    fake = FakeAnthropicClient(
        [
            FakeResponse(content=[FakeBlock(type="text", text="a real reply")])
            for _ in range(6)
        ]
    )
    register_web_routes(
        app,
        web_config=_web_config(),
        web_auth_state=web_auth_state,
        anthropic_client=fake,
        state_mgr=state_mgr,
        talker_config=talker_config,
        system_prompt_provider=lambda: "SYSTEM PROMPT",
        vault_context_str="VAULT CONTEXT",
        allowed_user_ids=[1],
    )
    app["_t_state_mgr"] = state_mgr
    client = await aiohttp_client(app)
    return client, fake


async def _open(client, headers) -> str:
    r = await client.post("/chat/open", json={}, headers=headers)
    assert r.status == 200
    return (await r.json())["session_key"]


async def _toggle(client, headers, key: str, on: bool):
    r = await client.post(
        "/chat/capture", json={"session_key": key, "on": on}, headers=headers
    )
    assert r.status == 200, await r.text()
    return await r.json()


# ---------------------------------------------------------------------------
# The core contract: capture ON → received, persisted, NOT answered
# ---------------------------------------------------------------------------


async def test_capture_on_turn_makes_no_model_call_with_positive_control(
    capture_client,
) -> None:
    client, fake = capture_client
    headers = _session_headers()
    key = await _open(client, headers)

    # POSITIVE CONTROL first: a normal turn DOES invoke the model and gets
    # a real reply — proving the instrument (the call counter) can fire.
    r = await client.post(
        "/chat/turn",
        json={"session_key": key, "message": "hello there"},
        headers=headers,
    )
    body = await r.json()
    assert r.status == 200
    assert body["reply"] == "a real reply"
    assert body["captured"] is False
    assert len(fake.messages.calls) == 1

    # Toggle capture ON.
    state = await _toggle(client, headers, key, True)
    assert state["capture_active"] is True
    assert state["spans"] == [
        {"index": 0, "start": 2, "end": None, "turns": 0, "extracted": False}
    ]
    assert state["closed_span"] is None

    # Captured turn: receipt payload, NO model invocation.
    r = await client.post(
        "/chat/turn",
        json={"session_key": key, "message": "dictated capture material"},
        headers=headers,
    )
    body = await r.json()
    assert r.status == 200
    assert body["captured"] is True
    assert body["reply"] == ""
    assert body["ts"] == ""  # no assistant turn exists
    assert body["user_ts"]  # the persisted user turn's stamp
    assert len(fake.messages.calls) == 1, "capture turn must not call the model"

    # The turn IS persisted — it is span material, visible in history.
    r = await client.get(f"/chat/history/{key}", headers=headers)
    hist = await r.json()
    texts = [t["text"] for t in hist["turns"]]
    assert "dictated capture material" in texts
    # ...and no assistant turn followed it.
    assert hist["turns"][-1]["role"] == "user"


async def test_capture_off_resumes_normal_replies(capture_client) -> None:
    client, fake = capture_client
    headers = _session_headers()
    key = await _open(client, headers)

    await _toggle(client, headers, key, True)
    r = await client.post(
        "/chat/turn",
        json={"session_key": key, "message": "captured line"},
        headers=headers,
    )
    assert (await r.json())["captured"] is True
    assert len(fake.messages.calls) == 0

    state = await _toggle(client, headers, key, False)
    assert state["capture_active"] is False
    assert state["closed_span"] == {"index": 0, "turns": 1}

    # Toggle OFF mid-conversation resumes normal responses (the ruling's
    # second half) — the very next turn is answered.
    r = await client.post(
        "/chat/turn",
        json={"session_key": key, "message": "and now respond"},
        headers=headers,
    )
    body = await r.json()
    assert body["captured"] is False
    assert body["reply"] == "a real reply"
    assert len(fake.messages.calls) == 1


async def test_stream_capture_receipt_frame_no_model_call(
    capture_client,
) -> None:
    client, fake = capture_client
    headers = _session_headers()
    key = await _open(client, headers)
    await _toggle(client, headers, key, True)

    r = await client.post(
        "/chat/stream",
        json={"session_key": key, "message": "streamed capture line"},
        headers=headers,
    )
    assert r.status == 200
    frames = _parse_sse(await r.text())
    done = [d for (ev, d) in frames if ev == "done"]
    assert len(done) == 1
    assert done[0]["captured"] is True
    assert done[0]["reply"] == ""
    assert done[0]["user_ts"]
    assert len(fake.messages.calls) == 0

    # Positive control on the SAME surface: off → streamed turn answers.
    await _toggle(client, headers, key, False)
    r = await client.post(
        "/chat/stream",
        json={"session_key": key, "message": "respond now"},
        headers=headers,
    )
    frames = _parse_sse(await r.text())
    done = [d for (ev, d) in frames if ev == "done"]
    assert done[0]["captured"] is False
    assert done[0]["reply"] == "a real reply"
    assert len(fake.messages.calls) == 1


# ---------------------------------------------------------------------------
# Server truth: refresh resumes capture; span boundaries exact
# ---------------------------------------------------------------------------


async def test_refresh_mid_capture_resumes_capturing(capture_client) -> None:
    """The server-truth pin: a client that lost all local state (refresh)
    re-learns capture-ON from history — and its next turn is still
    captured, because the TURN PATH reads the server state too."""
    client, fake = capture_client
    headers = _session_headers()
    key = await _open(client, headers)
    await _toggle(client, headers, key, True)
    await client.post(
        "/chat/turn",
        json={"session_key": key, "message": "pre-refresh dictation"},
        headers=headers,
    )

    # A fresh bootstrap reads history — capture state rides along.
    r = await client.get(f"/chat/history/{key}", headers=headers)
    hist = await r.json()
    assert hist["capture_active"] is True
    assert hist["capture_spans"] == [
        {"index": 0, "start": 0, "end": None, "turns": 1, "extracted": False}
    ]

    # And the next turn (sent by the refreshed client with NO capture
    # knowledge of its own) is still captured — server truth, not client
    # memory.
    r = await client.post(
        "/chat/turn",
        json={"session_key": key, "message": "post-refresh dictation"},
        headers=headers,
    )
    assert (await r.json())["captured"] is True
    assert len(fake.messages.calls) == 0


async def test_span_boundaries_exact(capture_client) -> None:
    """The turn sent one tick before toggle-on is NOT captured; the
    toggle-on turn onward is. Boundaries are transcript indices stamped at
    toggle time — asserted EXACTLY."""
    client, fake = capture_client
    headers = _session_headers()
    key = await _open(client, headers)

    # Pre-toggle turn → transcript [user@0, assistant@1].
    await client.post(
        "/chat/turn",
        json={"session_key": key, "message": "before capture"},
        headers=headers,
    )
    state = await _toggle(client, headers, key, True)
    assert state["spans"][0]["start"] == 2  # NOT 0 — the prior turn is outside

    # Two captured turns → transcript indices 2 and 3 (no assistant turns).
    await client.post(
        "/chat/turn",
        json={"session_key": key, "message": "captured one"},
        headers=headers,
    )
    await client.post(
        "/chat/turn",
        json={"session_key": key, "message": "captured two"},
        headers=headers,
    )
    state = await _toggle(client, headers, key, False)
    assert state["closed_span"] == {"index": 0, "turns": 2}
    assert state["spans"] == [
        {"index": 0, "start": 2, "end": 4, "turns": 2, "extracted": False}
    ]


async def test_empty_span_discarded_with_positive_control(
    capture_client,
) -> None:
    client, fake = capture_client
    headers = _session_headers()
    key = await _open(client, headers)

    # on → off with nothing said: span dropped, logged, no offer.
    await _toggle(client, headers, key, True)
    with structlog.testing.capture_logs() as captured:
        state = await _toggle(client, headers, key, False)
    assert state["closed_span"] is None
    assert state["spans"] == []
    discards = [
        c for c in captured
        if c.get("event") == "talker.capture_span.empty_span_discarded"
    ]
    assert len(discards) == 1

    # POSITIVE CONTROL: the same toggles around one captured turn KEEP the
    # span — proving the discard branch above was a decision, not a dead
    # span-recording path.
    await _toggle(client, headers, key, True)
    await client.post(
        "/chat/turn",
        json={"session_key": key, "message": "not empty this time"},
        headers=headers,
    )
    state = await _toggle(client, headers, key, False)
    assert state["closed_span"] == {"index": 0, "turns": 1}
    assert len(state["spans"]) == 1


# ---------------------------------------------------------------------------
# Toggle guards: in-flight turn, idempotency, validation, auth
# ---------------------------------------------------------------------------


async def test_toggle_rejected_while_turn_in_flight(
    capture_client, monkeypatch
) -> None:
    """A span boundary stamped mid-append would be ambiguous — the toggle
    is refused 409 while a turn runs."""
    client, fake = capture_client
    headers = _session_headers()
    key = await _open(client, headers)

    async def _slow_run_turn(**kwargs):
        await asyncio.sleep(0.15)
        from alfred.telegram.session import append_turn

        append_turn(kwargs["state"], kwargs["session"], "user",
                    kwargs["user_message"])
        append_turn(kwargs["state"], kwargs["session"], "assistant", "slow")
        return "slow"

    monkeypatch.setattr(
        "alfred.telegram.conversation.run_turn", _slow_run_turn
    )
    turn_task = asyncio.ensure_future(client.post(
        "/chat/turn",
        json={"session_key": key, "message": "long turn"},
        headers=headers,
    ))
    await asyncio.sleep(0.05)  # let the turn reserve the in-flight slot
    r = await client.post(
        "/chat/capture", json={"session_key": key, "on": True},
        headers=headers,
    )
    assert r.status == 409
    assert (await r.json())["error"] == "turn_in_flight"
    tr = await turn_task
    assert tr.status == 200
    # After the turn completes the toggle succeeds (the guard released).
    state = await _toggle(client, headers, key, True)
    assert state["capture_active"] is True


async def test_toggle_idempotent_both_directions(capture_client) -> None:
    client, fake = capture_client
    headers = _session_headers()
    key = await _open(client, headers)

    s1 = await _toggle(client, headers, key, True)
    s2 = await _toggle(client, headers, key, True)  # double-tap
    assert s2["capture_active"] is True
    assert len(s2["spans"]) == len(s1["spans"]) == 1  # no forked span

    off1 = await _toggle(client, headers, key, False)
    off2 = await _toggle(client, headers, key, False)
    assert off2["capture_active"] is False
    assert off2["closed_span"] is None  # nothing left to close


async def test_captured_turn_idempotent_retry_dedups_to_receipt(
    capture_client,
) -> None:
    client, fake = capture_client
    headers = _session_headers()
    key = await _open(client, headers)
    await _toggle(client, headers, key, True)

    body = {
        "session_key": key,
        "message": "captured once",
        "idempotency_key": "cap-key-1",
    }
    r1 = await client.post("/chat/turn", json=body, headers=headers)
    p1 = await r1.json()
    assert p1["captured"] is True and p1["deduped"] is False

    r2 = await client.post("/chat/turn", json=body, headers=headers)
    p2 = await r2.json()
    assert p2["captured"] is True, "retry must dedup to a CAPTURED receipt"
    assert p2["deduped"] is True

    # The retry appended nothing — exactly one captured turn persisted.
    r = await client.get(f"/chat/history/{key}", headers=headers)
    hist = await r.json()
    assert [t["text"] for t in hist["turns"]] == ["captured once"]
    assert hist["capture_spans"][0]["turns"] == 1
    assert len(fake.messages.calls) == 0


async def test_toggle_validation_and_unknown_session(capture_client) -> None:
    client, fake = capture_client
    headers = _session_headers()
    key = await _open(client, headers)

    # `on` must be a bool — named refusal, not a silent default.
    r = await client.post(
        "/chat/capture", json={"session_key": key}, headers=headers
    )
    assert r.status == 400
    assert (await r.json())["error"] == "on_required"

    r = await client.post(
        "/chat/capture", json={"session_key": "nope", "on": True},
        headers=headers,
    )
    assert r.status == 404
    assert (await r.json())["error"] == "no_such_session"

    # POSITIVE CONTROL: the well-formed toggle on the live key succeeds —
    # the refusals above were the gates firing, not a dead route.
    state = await _toggle(client, headers, key, True)
    assert state["capture_active"] is True


async def test_capture_route_auth_gates(capture_client) -> None:
    """The new asserted-identity surface rides the same peer-pinned spine:
    no session token → 401; the deterministic-create-only ``web_ingest``
    peer token cannot drive the toggle (the escalation-pin mirror)."""
    client, fake = capture_client
    headers = _session_headers()
    key = await _open(client, headers)

    # No Layer-2 session token.
    r = await client.post(
        "/chat/capture", json={"session_key": key, "on": True},
    )
    assert r.status == 401

    # Valid Layer-1 web_ingest token + asserted user — must NOT clear the
    # web-chat identity spine (WEB_CHAT_PEER pin).
    r = await client.post(
        "/chat/capture",
        json={"session_key": key, "on": True},
        headers={
            "Authorization": f"Bearer {DUMMY_WEB_INGEST_TOKEN}",
            "X-Alfred-Client": "web",
            USER_HEADER: "andrew",
        },
    )
    assert r.status == 401
    # POSITIVE CONTROL: the real headers still work after the refusals.
    state = await _toggle(client, headers, key, True)
    assert state["capture_active"] is True


# ---------------------------------------------------------------------------
# Composer parity: voice + images are span material too
# ---------------------------------------------------------------------------


async def test_capture_accepts_voice_kind_and_counts_it(capture_client) -> None:
    """All composer inputs are valid during capture — a voice-kind turn is
    received as span material exactly like text (no model call, kind
    stamped on the persisted turn)."""
    client, fake = capture_client
    headers = _session_headers()
    key = await _open(client, headers)
    await _toggle(client, headers, key, True)

    r = await client.post(
        "/chat/turn",
        json={"session_key": key, "message": "spoken capture", "kind": "voice"},
        headers=headers,
    )
    assert (await r.json())["captured"] is True
    assert len(fake.messages.calls) == 0

    state_mgr = client.server.app["_t_state_mgr"]
    active = next(iter(state_mgr.state["active_sessions"].values()))
    turn = active["transcript"][0]
    assert turn["_kind"] == "voice"
    assert capture_spans.spans_summary(active)[0]["turns"] == 1
