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
from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState
from alfred.web.auth import USER_HEADER
from alfred.web.routes_chat import register_web_routes
from alfred.web.state import WebAuthState

from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse
from tests.test_web_routes_chat import (
    DUMMY_WEB_INGEST_TOKEN,
    DUMMY_WEB_PEER_TOKEN,
    _make_talker_config,
    _parse_sse,
    _relay_web_config,
    _session_headers,
    _transport_config,
    _web_config,
)

# Obviously-fake fixture credential (no realistic provider prefix — the
# GitGuardian rule); mirrors ``tests/test_web_notify.py``'s constant.
DUMMY_RRTS_RELAY_TOKEN = "DUMMY_RRTS_RELAY_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_01234"


def _build_capture_app(tmp_path, responses):  # type: ignore[no-untyped-def]
    """A web-routes transport app whose fake Anthropic client is EXPOSED."""
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)

    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    talker_config = _make_talker_config(tmp_path)
    web_auth_state = WebAuthState.create(tmp_path / "web_auth_state.json")
    web_auth_state.load()
    fake = FakeAnthropicClient(responses)
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
    app["_t_talker_config"] = talker_config
    return app, fake


def _relay_transport_config() -> TransportConfig:
    """Transport config with the chat ``web`` peer AND the vouched
    ``rrts_relay`` peer — BOTH carrying ``allowed_clients: [web]``.

    That sharing is the whole point (same shape as the ``web_ingest``
    fixture in ``tests/test_web_routes_chat.py``): an ``rrts_relay``
    request clears Layer 1 cleanly, so a 401 on a capture route can ONLY
    have come from the Layer-2 peer-pin under test — never from
    ``auth_middleware`` refusing the client name.
    """
    return TransportConfig(
        server=ServerConfig(),
        auth=AuthConfig(
            tokens={
                "web": AuthTokenEntry(
                    token=DUMMY_WEB_PEER_TOKEN, allowed_clients=["web"],
                ),
                "rrts_relay": AuthTokenEntry(
                    token=DUMMY_RRTS_RELAY_TOKEN, allowed_clients=["web"],
                ),
            }
        ),
        state=StateConfig(),
    )


def _build_relay_capture_app(tmp_path, responses):  # type: ignore[no-untyped-def]
    """RELAY-mode twin of :func:`_build_capture_app`.

    Relay mode is where the escalation is reachable: the ``rrts_relay``
    identity only RESOLVES on the asserted-header path, so a session-mode
    app would refuse it at Layer 2 for the wrong reason and the pin would
    be vacuous.
    """
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_relay_transport_config(), tstate)

    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    talker_config = _make_talker_config(tmp_path)
    web_auth_state = WebAuthState.create(tmp_path / "web_auth_state.json")
    web_auth_state.load()
    fake = FakeAnthropicClient(responses)
    register_web_routes(
        app,
        web_config=_relay_web_config(),
        web_auth_state=web_auth_state,
        anthropic_client=fake,
        state_mgr=state_mgr,
        talker_config=talker_config,
        system_prompt_provider=lambda: "SYSTEM PROMPT",
        vault_context_str="VAULT CONTEXT",
        allowed_user_ids=[1],
    )
    app["_t_state_mgr"] = state_mgr
    app["_t_talker_config"] = talker_config
    return app, fake


@pytest.fixture
async def capture_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """``(client, fake)`` — fake pre-loaded with plain text replies.

    ``fake.messages.calls`` is the no-model-call instrument every capture
    pin leans on.
    """
    app, fake = _build_capture_app(
        tmp_path,
        [
            FakeResponse(content=[FakeBlock(type="text", text="a real reply")])
            for _ in range(6)
        ],
    )
    client = await aiohttp_client(app)
    return client, fake


@pytest.fixture
async def capture_client_factory(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Factory variant: tests choose the fake's response script (the
    extraction e2e needs tool_use responses in a specific order)."""

    async def _make(responses):
        app, fake = _build_capture_app(tmp_path, responses)
        client = await aiohttp_client(app)
        return client, fake

    return _make


def _batch_summary_response(**overrides):
    """A canned ``emit_structured_summary`` tool_use (structuring pass)."""
    tool_input = {
        "topics": ["stoicism reading"],
        "decisions": [],
        "open_questions": [],
        "action_items": [],
        "key_insights": ["dichotomy of control is foundational"],
        "raw_contradictions": [],
    }
    tool_input.update(overrides)
    return FakeResponse(
        content=[
            FakeBlock(
                type="tool_use",
                id="toolu_batch",
                name="emit_structured_summary",
                input=tool_input,
            )
        ],
        stop_reason="tool_use",
    )


def _extract_note_response(*notes):
    """A canned ``create_note`` tool_use response (extraction pass)."""
    blocks = [
        FakeBlock(
            type="tool_use",
            id=f"toolu_note_{i}",
            name="create_note",
            input=note,
        )
        for i, note in enumerate(notes)
    ]
    return FakeResponse(content=blocks, stop_reason="tool_use")


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

    # Valid Layer-1 web_ingest token + asserted user — refused by the
    # WEB_CHAT_PEER pin. The REASON is asserted, not just the status: a
    # 401 with error=invalid_session would mean the identity layer
    # happened to refuse it and the peer-pin could be absent entirely.
    with structlog.testing.capture_logs() as captured:
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
    assert (await r.json())["error"] == "wrong_peer"
    events = [
        c for c in captured
        if c.get("event") == "web.chat.capture_wrong_peer"
    ]
    assert len(events) == 1
    assert events[0]["reason"] == "wrong_peer"
    assert events[0]["peer"] == "web_ingest"
    # POSITIVE CONTROL: the real headers still work after the refusals.
    state = await _toggle(client, headers, key, True)
    assert state["capture_active"] is True


async def test_capture_routes_peer_pinned_against_rrts_relay(
    aiohttp_client, tmp_path
) -> None:
    """LOAD-BEARING peer-pin (CLAUDE.md "Relay / asserted-identity routes").

    The vouched ``rrts_relay`` peer is a FIRST-CLASS caller of
    ``/chat/turn`` — ``_resolve_relay_identity`` admits it and
    ``_user_name_for`` threads its reporter name into ``run_turn``. That
    is safe there ONLY because ``resolve_scope`` maps it to the fixed
    ``rrts_intake`` scope. ``/chat/capture/extract`` runs ``vault_create``
    + structuring + note extraction off ``talker_config`` directly,
    outside that machinery, so a leaked reporter token would otherwise
    turn reporter-controlled content into the operator's notes and tasks.

    Driven in RELAY mode (where the reporter identity actually resolves),
    both routes, both directions:

    * the reporter is refused 401 ``wrong_peer`` on BOTH routes, with the
      logged reason — not merely "some 401";
    * NOTHING is touched by the refusal (no model call, no vault record,
      the operator's own span state byte-unchanged);
    * POSITIVE CONTROL on the LIVE path: the ``web`` peer, over the same
      wire, in the same relay mode, on the SAME span, extracts to a real
      record — so the pin discriminates the PEER, not relay mode, and not
      "the route refuses everyone".
    """
    import frontmatter as fmlib

    app, fake = _build_relay_capture_app(tmp_path, [
        _batch_summary_response(),
        _extract_note_response({
            "name": "Reporter Boundary Holds",
            "body": "The capture door admits one peer.",
            "confidence_tier": "high",
            "source_quote": "the pin is what keeps the bound true",
        }),
    ])
    client = await aiohttp_client(app)

    owner = {
        "Authorization": f"Bearer {DUMMY_WEB_PEER_TOKEN}",
        "X-Alfred-Client": "web",
        USER_HEADER: "andrew",
    }
    reporter = {
        "Authorization": f"Bearer {DUMMY_RRTS_RELAY_TOKEN}",
        "X-Alfred-Client": "web",
        USER_HEADER: "Dana Dispatcher",
    }

    # Operator opens a session and closes one real span (relay mode).
    key = await _open(client, owner)
    await _toggle(client, owner, key, True)
    for msg in (
        "the pin is what keeps the bound true for the relay peer",
        "span extraction runs outside resolve_scope entirely",
    ):
        r = await client.post(
            "/chat/turn", json={"session_key": key, "message": msg},
            headers=owner,
        )
        assert (await r.json())["captured"] is True
    state = await _toggle(client, owner, key, False)
    assert state["closed_span"] == {"index": 0, "turns": 2}
    assert len(fake.messages.calls) == 0  # capture made no model call

    vault = tmp_path / "vault"
    sessions_before = sorted(p.name for p in (vault / "session").iterdir())
    r = await client.get(f"/chat/history/{key}", headers=owner)
    spans_before = (await r.json())["capture_spans"]

    # ---- the escalation drive: BOTH routes, the reporter peer ----
    with structlog.testing.capture_logs() as captured:
        r = await client.post(
            "/chat/capture", json={"session_key": key, "on": True},
            headers=reporter,
        )
        assert r.status == 401
        assert (await r.json())["error"] == "wrong_peer"

        r = await client.post(
            "/chat/capture/extract",
            json={"session_key": key, "span_index": 0},
            headers=reporter,
        )
        assert r.status == 401
        assert (await r.json())["error"] == "wrong_peer"

    events = [
        c for c in captured
        if c.get("event") == "web.chat.capture_wrong_peer"
    ]
    assert len(events) == 2  # one per route — the WHY, not just the 401
    assert {e["reason"] for e in events} == {"wrong_peer"}
    assert {e["peer"] for e in events} == {"rrts_relay"}
    assert {e["expected"] for e in events} == {"web"}
    # The reporter's asserted name never became an identity: the pin
    # fires BEFORE resolve_web_identity, so the vouched-identity log the
    # relay path would otherwise emit is absent.
    assert not [
        c for c in captured
        if c.get("event") == "web.auth.relay_vouched_identity"
    ]

    # ---- nothing out there was touched by the refusals ----
    assert len(fake.messages.calls) == 0
    assert sorted(p.name for p in (vault / "session").iterdir()) == (
        sessions_before
    )
    r = await client.get(f"/chat/history/{key}", headers=owner)
    assert (await r.json())["capture_spans"] == spans_before

    # ---- POSITIVE CONTROL: the web peer extracts the SAME span ----
    r = await client.post(
        "/chat/capture/extract",
        json={"session_key": key, "span_index": 0},
        headers=owner,
    )
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["ok"] is True
    assert body["record"].startswith("session/capture-")
    assert len(body["notes"]) == 1
    span_post = fmlib.load(vault / body["record"])
    assert span_post["session_type"] == "capture"
    assert "resolve_scope" in span_post.content


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


# ---------------------------------------------------------------------------
# C2 — span extraction: the preserved machinery, end to end
# ---------------------------------------------------------------------------


async def test_extract_span_end_to_end_via_preserved_machinery(
    capture_client_factory, tmp_path
) -> None:
    """The dispatch's e2e pin: a REAL span through /chat/capture/extract —
    span session record + structuring + note with the editor-tone/anchor
    properties the existing capture_extract tests define (blockquote,
    ``_Source:`` attribution, inline ``(p.23)`` annotation, queryable
    ``source_anchor`` + ``created_by_capture`` frontmatter)."""
    import frontmatter as fmlib

    client, fake = await capture_client_factory([
        FakeResponse(
            content=[FakeBlock(type="text", text="a normal pre-span reply")]
        ),
        _batch_summary_response(),
        _extract_note_response({
            "name": "Dichotomy Of Control As Foundation",
            "body": "Marcus grounds the whole practice in the dichotomy "
                    "of control.",
            "confidence_tier": "high",
            "source_quote": "the dichotomy of control is foundational",
            "source_anchor": "p.23",
        }),
    ])
    headers = _session_headers()
    key = await _open(client, headers)
    # A NORMAL pre-span exchange first (transcript turns 0+1) — extraction
    # must consume THE SPAN, so this text must never reach the span record.
    r = await client.post(
        "/chat/turn",
        json={"session_key": key,
              "message": "ordinary pre-span conversation about groceries"},
        headers=headers,
    )
    assert (await r.json())["captured"] is False
    await _toggle(client, headers, key, True)
    for msg in (
        "Marcus on page 23 talks about the dichotomy of control",
        "the dichotomy of control is foundational to everything after",
    ):
        r = await client.post(
            "/chat/turn", json={"session_key": key, "message": msg},
            headers=headers,
        )
        assert (await r.json())["captured"] is True
    assert len(fake.messages.calls) == 1  # only the pre-span turn called
    state = await _toggle(client, headers, key, False)
    assert state["closed_span"] == {"index": 0, "turns": 2}

    r = await client.post(
        "/chat/capture/extract",
        json={"session_key": key, "span_index": 0},
        headers=headers,
    )
    assert r.status == 200, await r.text()
    body = await r.json()
    assert body["ok"] is True
    assert body["skipped_reason"] == ""
    assert len(body["notes"]) == 1
    # Exactly the two preserved-machinery calls on top of the pre-span
    # turn: structuring + extraction.
    assert len(fake.messages.calls) == 3

    vault = tmp_path / "vault"
    # The span became its own session record, capture-shaped, covering
    # EXACTLY the span (turns 2-4) — never the pre-span conversation.
    span_rel = body["record"]
    assert span_rel.startswith("session/capture-")
    assert "-s2" in span_rel
    span_post = fmlib.load(vault / span_rel)
    assert span_post["session_type"] == "capture"
    assert span_post["capture_span_range"] == "2-4"
    assert span_post["capture_span_parent"]  # the parent session id
    assert span_post["capture_structured"] == "true"
    assert "# Transcript" in span_post.content
    assert "dichotomy of control" in span_post.content
    assert "groceries" not in span_post.content  # span, not conversation
    assert "## Structured Summary" in span_post.content
    assert span_post["derived_notes"] == [f"[[{body['notes'][0]}]]"]

    # The note: preserved editor-tone + anchor properties.
    note_post = fmlib.load(vault / body["notes"][0])
    assert note_post["created_by_capture"] is True
    assert note_post["confidence_tier"] == "high"
    assert note_post["source_anchor"] == "p.23"
    assert note_post["source_session"] == f"[[{span_rel}]]"
    assert "> the dichotomy of control is foundational" in note_post.content
    assert f"_Source: [[{span_rel}]]_" in note_post.content
    assert note_post.content.startswith("(p.23) ")

    # State marked extracted — visible on the wire.
    r = await client.get(f"/chat/history/{key}", headers=headers)
    hist = await r.json()
    assert hist["capture_spans"][0]["extracted"] is True


async def test_extract_refusals_are_named_with_positive_control(
    capture_client_factory,
) -> None:
    """Deny-pins with their admissible neighbour: span_open refused while
    capture is ON; the SAME span extracts fine once closed; a second
    extract refuses already_extracted CARRYING the first run's outputs;
    span_not_found / span_index validation named."""
    client, fake = await capture_client_factory([
        _batch_summary_response(),
        _extract_note_response({
            "name": "One Good Note",
            "body": "A single idea worth keeping.",
            "confidence_tier": "medium",
            "source_quote": "worth keeping",
        }),
    ])
    headers = _session_headers()
    key = await _open(client, headers)
    await _toggle(client, headers, key, True)
    await client.post(
        "/chat/turn",
        json={"session_key": key, "message": "an idea worth keeping"},
        headers=headers,
    )

    # OPEN span → named refusal, no LLM call, nothing written.
    r = await client.post(
        "/chat/capture/extract",
        json={"session_key": key, "span_index": 0},
        headers=headers,
    )
    assert r.status == 409
    assert (await r.json())["error"] == "span_open"
    assert len(fake.messages.calls) == 0

    # Bad index / non-int index → named refusals.
    r = await client.post(
        "/chat/capture/extract",
        json={"session_key": key, "span_index": 7},
        headers=headers,
    )
    assert r.status == 404
    assert (await r.json())["error"] == "span_not_found"
    r = await client.post(
        "/chat/capture/extract",
        json={"session_key": key, "span_index": "0"},
        headers=headers,
    )
    assert r.status == 400
    assert (await r.json())["error"] == "span_index_required"

    # POSITIVE CONTROL: close the span — the same index now extracts.
    await _toggle(client, headers, key, False)
    r = await client.post(
        "/chat/capture/extract",
        json={"session_key": key, "span_index": 0},
        headers=headers,
    )
    assert r.status == 200
    first = await r.json()
    assert first["ok"] is True and len(first["notes"]) == 1

    # Second extract on the same span: named refusal CARRYING the record.
    r = await client.post(
        "/chat/capture/extract",
        json={"session_key": key, "span_index": 0},
        headers=headers,
    )
    assert r.status == 409
    dup = await r.json()
    assert dup["error"] == "already_extracted"
    assert dup["record"] == first["record"]
    assert dup["notes"] == first["notes"]
    # No third LLM call happened for the refusal.
    assert len(fake.messages.calls) == 2


async def test_reopen_close_backstop_extracts_unextracted_spans(
    capture_client_factory, tmp_path
) -> None:
    """The close-time backstop: a span the operator never extracted is
    finalized when the session closes (web reopen path) — parent record
    stamps capture_spans, the heuristic is SUPPRESSED (spans own the
    material), and the post-close finalizer patches the parent rows."""
    import frontmatter as fmlib

    client, fake = await capture_client_factory([
        _batch_summary_response(),
        _extract_note_response({
            "name": "Backstop Note",
            "body": "Material rescued at close.",
            "confidence_tier": "medium",
            "source_quote": "rescued at close",
        }),
    ])
    headers = _session_headers()
    key = await _open(client, headers)
    await _toggle(client, headers, key, True)
    # Three substantial turns — WOULD make the whole-session heuristic
    # fire (is_substantive: >=3 turns, >=150 chars) if spans didn't
    # suppress it. That is what makes the suppression assertion below
    # non-vacuous.
    for i in range(3):
        await client.post(
            "/chat/turn",
            json={"session_key": key,
                  "message": f"substantial dictated capture line {i} — "
                             f"enough characters to cross the substance bar"},
            headers=headers,
        )
    # Capture still ON at close — the open span must be closed at the
    # transcript end by the close path, not lost.

    with structlog.testing.capture_logs() as captured:
        r = await client.post("/chat/open", json={}, headers=headers)
    body = await r.json()
    prior = body.get("prior_capture")
    assert prior is not None
    assert prior["status"] == "spans_extracting"
    assert prior["spans"] == 1
    assert prior["unextracted"] == 1
    assert prior["turns"] == 3
    suppressed = [
        c for c in captured
        if c.get("event") == "talker.capture.heuristic_suppressed_spans_present"
    ]
    assert len(suppressed) == 1

    # Drain the detached finalization task.
    for _ in range(100):
        if not capture_spans._FINALIZE_TASKS:
            break
        await asyncio.sleep(0.01)
    assert not capture_spans._FINALIZE_TASKS

    vault = tmp_path / "vault"
    parent_rel = prior["record"]
    parent_post = fmlib.load(vault / parent_rel)
    # Heuristic suppressed: no capture_structured marker on the parent.
    assert parent_post.get("capture_structured") is None
    rows = parent_post["capture_spans"]
    assert len(rows) == 1
    assert rows[0]["span"] == "0-3"
    assert rows[0]["turns"] == 3
    # The finalizer patched the parent row with the outcome.
    assert rows[0]["extracted"] is True
    assert rows[0]["record"].startswith("[[session/capture-")
    # And the span record + note really exist.
    span_rel = rows[0]["record"].strip("[]") + ".md"
    span_post = fmlib.load(vault / span_rel)
    assert span_post["capture_structured"] == "true"
    assert span_post["capture_span_range"] == "0-3"
    note_paths = span_post["derived_notes"]
    assert len(note_paths) == 1
    assert len(fake.messages.calls) == 2


async def test_reopen_without_spans_keeps_heuristic_path(
    capture_client,
) -> None:
    """POSITIVE CONTROL for the suppression: the same reopen with NO spans
    still walks the existing whole-session capture-candidate path (the
    clinic-capture contract is untouched)."""
    client, fake = capture_client
    headers = _session_headers()
    key = await _open(client, headers)
    for i in range(3):
        await client.post(
            "/chat/turn",
            json={"session_key": key,
                  "message": f"dictated action item number {i} with enough "
                             f"substance to cross the candidate bar easily"},
            headers=headers,
        )
    with structlog.testing.capture_logs() as captured:
        r = await client.post("/chat/open", json={}, headers=headers)
    body = await r.json()
    prior = body.get("prior_capture")
    assert prior is not None
    assert prior["status"] == "held_unstructured"
    held = [
        c for c in captured
        if c.get("event") == "web.chat.prior_capture_held"
    ]
    assert len(held) == 1


async def test_extract_requires_auth(capture_client) -> None:
    """All three gates, each named by its own reason — a bare ``401``
    cannot tell them apart, and a peer-pin asserted only by status is
    green against a build that has no peer-pin at all:

    * no bearer at all  → Layer 1 ``missing_bearer``;
    * chat ``web`` peer, no session token → identity ``invalid_session``;
    * valid ``web_ingest`` peer token → the peer-pin ``wrong_peer``.
    """
    client, fake = capture_client
    headers = _session_headers()
    key = await _open(client, headers)
    r = await client.post(
        "/chat/capture/extract",
        json={"session_key": key, "span_index": 0},
    )
    assert r.status == 401
    assert (await r.json())["error"] == "missing_bearer"
    r = await client.post(
        "/chat/capture/extract",
        json={"session_key": key, "span_index": 0},
        headers={
            "Authorization": f"Bearer {DUMMY_WEB_PEER_TOKEN}",
            "X-Alfred-Client": "web",
        },
    )
    assert r.status == 401
    assert (await r.json())["error"] == "invalid_session"
    with structlog.testing.capture_logs() as captured:
        r = await client.post(
            "/chat/capture/extract",
            json={"session_key": key, "span_index": 0},
            headers={
                "Authorization": f"Bearer {DUMMY_WEB_INGEST_TOKEN}",
                "X-Alfred-Client": "web",
                USER_HEADER: "andrew",
            },
        )
    assert r.status == 401
    assert (await r.json())["error"] == "wrong_peer"
    events = [
        c for c in captured
        if c.get("event") == "web.chat.capture_wrong_peer"
    ]
    assert len(events) == 1
    assert events[0]["peer"] == "web_ingest"
