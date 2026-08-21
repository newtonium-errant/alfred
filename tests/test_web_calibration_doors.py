"""R4 — the calibration loop's two WEB doors, driven through the real routes.

Every pin in this file DRIVES ITS ROUTE. That is deliberate and it is the lane's
standing rule: a pin whose name asserts a route behaviour must exercise the
route, because running the predicate inline proves the predicate and says
nothing about whether production reaches it. The capture door and the read door
are both threaded-at-a-call-site features, which is exactly the class where a
per-layer unit test stays green while production never calls the seam.

Fixture shape mirrors ``tests/test_web_routes_chat.py`` (same transport app, same
two auth layers, same fake SDK client) so these doors are tested in the
composition production actually runs.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from alfred.telegram.config import (
    AnthropicConfig,
    CalibrationConfig,
    InstanceConfig,
    LoggingConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
)
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
from alfred.web.auth import SESSION_HEADER, make_session_token
from alfred.web.config import WebAuthConfig, WebConfig, WebUser
from alfred.web.routes_chat import register_web_routes
from alfred.web.state import WebAuthState

from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse

# Obviously-fake test secrets — never a real provider prefix.
DUMMY_WEB_PEER_TOKEN = "DUMMY_WEB_PEER_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_0123456"
DUMMY_WEB_SIGNING_SECRET = "DUMMY_WEB_SIGNING_SECRET_FOR_TESTING_ONLY_0123456789"

_PEER_HEADERS = {
    "Authorization": f"Bearer {DUMMY_WEB_PEER_TOKEN}",
    "X-Alfred-Client": "web",
}

# The person record path the fixture configures as ``primary_users[0]``. Sampled
# from the shape production carries (``person/<Name>``), not minted.
PRIMARY_USER_REL = "person/Andrew Newton"


def _session_headers(name: str = "andrew", role: str = "owner") -> dict[str, str]:
    token = make_session_token(
        name, role, secret=DUMMY_WEB_SIGNING_SECRET, ttl_hours=168
    )
    return {**_PEER_HEADERS, SESSION_HEADER: token}


def _write_person_record(vault_dir: Path, block_body: str = "") -> None:
    """Write the primary user's person record carrying a calibration block."""
    from alfred.telegram.calibration import (
        CALIBRATION_MARKER_END,
        CALIBRATION_MARKER_START,
    )

    (vault_dir / "person").mkdir(parents=True, exist_ok=True)
    (vault_dir / f"{PRIMARY_USER_REL}.md").write_text(
        "---\ntype: person\nname: Andrew Newton\n---\n\n"
        "# Andrew Newton\n\n"
        f"{CALIBRATION_MARKER_START}\n"
        "## Communication Style\n\n"
        f"{block_body}\n"
        f"{CALIBRATION_MARKER_END}\n",
        encoding="utf-8",
    )


def _make_talker_config(tmp_path: Path, calibration: CalibrationConfig) -> TalkerConfig:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    for sub in ("session", "task", "note", "project", "person"):
        (vault_dir / sub).mkdir(exist_ok=True)
    return TalkerConfig(
        bot_token="test-token",
        allowed_users=[1],
        primary_users=[PRIMARY_USER_REL],
        anthropic=AnthropicConfig(api_key="test-key", model="claude-sonnet-4-6"),
        stt=STTConfig(api_key="test-stt", model="whisper-large-v3"),
        session=SessionConfig(
            gap_timeout_seconds=1800,
            state_path=str(tmp_path / "talker_state.json"),
        ),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name="Salem", canonical="S.A.L.E.M."),
        calibration=calibration,
    )


def _transport_config() -> TransportConfig:
    return TransportConfig(
        server=ServerConfig(),
        auth=AuthConfig(
            tokens={
                "web": AuthTokenEntry(
                    token=DUMMY_WEB_PEER_TOKEN, allowed_clients=["web"],
                ),
            }
        ),
        state=StateConfig(),
    )


async def _build_client(aiohttp_client, tmp_path, calibration, responses):
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    talker_config = _make_talker_config(tmp_path, calibration)
    web_auth_state = WebAuthState.create(tmp_path / "web_auth_state.json")
    web_auth_state.load()
    fake = FakeAnthropicClient(responses)
    register_web_routes(
        app,
        web_config=WebConfig(
            enabled=True,
            users=[WebUser(name="andrew", role="owner")],
            auth=WebAuthConfig(session_secret=DUMMY_WEB_SIGNING_SECRET),
        ),
        web_auth_state=web_auth_state,
        anthropic_client=fake,
        state_mgr=state_mgr,
        talker_config=talker_config,
        system_prompt_provider=lambda: "SYSTEM PROMPT",
        vault_context_str="VAULT CONTEXT",
        allowed_user_ids=[1],
    )
    app["_t_talker_config"] = talker_config
    app["_t_state_mgr"] = state_mgr
    return await aiohttp_client(app)


def _text_responses(n: int, text: str = "hello from salem"):
    return [FakeResponse(content=[FakeBlock(type="text", text=text)]) for _ in range(n)]


# ---------------------------------------------------------------------------
# The READ door — calibration_str reaching run_turn, per route
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_run_turn(monkeypatch):
    """Intercept ``run_turn`` and record the kwargs each route hands it.

    Both handlers import it lazily from ``alfred.telegram.conversation``, so
    patching the conversation module intercepts the real production call.
    """
    seen: list[dict] = []

    async def _fake_run_turn(**kwargs):
        seen.append(kwargs)
        return "fake reply"

    monkeypatch.setattr("alfred.telegram.conversation.run_turn", _fake_run_turn)
    return seen


async def test_chat_turn_injects_no_calibration_when_inject_disabled(
    aiohttp_client, tmp_path, captured_run_turn
) -> None:
    """DEFAULT POSTURE: ``/chat/turn`` hands ``calibration_str=None``.

    The prompt is byte-identical to pre-R4 for every instance that has not opted
    in — which is the property that makes shipping the read door safe.
    """
    _write_person_record(tmp_path / "vault", "- Prefers terse answers.")
    client = await _build_client(
        aiohttp_client, tmp_path,
        CalibrationConfig(inject_enabled=False),
        _text_responses(4),
    )
    headers = _session_headers()
    key = (await (await client.post("/chat/open", json={}, headers=headers)).json())[
        "session_key"
    ]
    r = await client.post(
        "/chat/turn", json={"session_key": key, "message": "hi"}, headers=headers
    )
    assert r.status == 200

    assert len(captured_run_turn) == 1
    assert captured_run_turn[0]["calibration_str"] is None


async def test_chat_turn_injects_the_approved_calibration_when_enabled(
    aiohttp_client, tmp_path, captured_run_turn
) -> None:
    """THE LOOP CLOSING: with ``inject_enabled``, the approved block reaches the model.

    This is the pin that would have caught the seam being left dead — the
    parameter has existed on ``run_turn`` since the bot era and every live caller
    fed it ``None``.
    """
    _write_person_record(tmp_path / "vault", "- Prefers terse answers.")
    client = await _build_client(
        aiohttp_client, tmp_path,
        CalibrationConfig(inject_enabled=True),
        _text_responses(4),
    )
    headers = _session_headers()
    key = (await (await client.post("/chat/open", json={}, headers=headers)).json())[
        "session_key"
    ]
    r = await client.post(
        "/chat/turn", json={"session_key": key, "message": "hi"}, headers=headers
    )
    assert r.status == 200

    assert len(captured_run_turn) == 1
    injected = captured_run_turn[0]["calibration_str"]
    assert injected is not None
    assert "Prefers terse answers." in injected


async def test_chat_stream_injects_the_same_calibration_as_chat_turn(
    aiohttp_client, tmp_path, captured_run_turn
) -> None:
    """THE SECOND CALL SITE, driven rather than assumed.

    A coverage claim carries its enumeration: ``run_turn`` has exactly two web
    call sites (``/chat/turn`` and ``/chat/stream``), and threading only one is
    the classic half-wired feature. This drives the streaming route and asserts
    it injects the same thing.
    """
    _write_person_record(tmp_path / "vault", "- Prefers terse answers.")
    client = await _build_client(
        aiohttp_client, tmp_path,
        CalibrationConfig(inject_enabled=True),
        _text_responses(4),
    )
    headers = _session_headers()
    key = (await (await client.post("/chat/open", json={}, headers=headers)).json())[
        "session_key"
    ]
    r = await client.post(
        "/chat/stream", json={"session_key": key, "message": "hi"}, headers=headers
    )
    assert r.status == 200
    await r.read()

    assert len(captured_run_turn) == 1
    assert "Prefers terse answers." in captured_run_turn[0]["calibration_str"]


async def test_both_web_run_turn_call_sites_are_threaded() -> None:
    """THE ENUMERATION ITSELF — a property of the SET, invisible per-route.

    The two pins above each drive one route. Neither can see how many routes
    there ARE, so a third ``run_turn`` call site added later would be untested
    and both would stay green. This asserts the population: every ``run_turn``
    call in ``routes_chat`` passes ``calibration_str``.
    """
    import ast

    from alfred.web import routes_chat

    tree = ast.parse(Path(routes_chat.__file__).read_text(encoding="utf-8"))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "run_turn"
    ]
    # Positive control: the census finds the call sites at all.
    assert len(calls) == 2, f"expected 2 run_turn call sites, found {len(calls)}"
    for call in calls:
        kwargs = {k.arg for k in call.keywords}
        assert "calibration_str" in kwargs, (
            f"run_turn call at line {call.lineno} does not thread calibration_str"
        )


async def test_inject_enabled_with_no_primary_user_injects_nothing(
    aiohttp_client, tmp_path, captured_run_turn
) -> None:
    """Fail-quiet, and SAID: no target record → nothing injected, logged once."""
    import structlog

    _write_person_record(tmp_path / "vault", "- Prefers terse answers.")
    client = await _build_client(
        aiohttp_client, tmp_path,
        CalibrationConfig(inject_enabled=True),
        _text_responses(4),
    )
    client.app["_t_talker_config"].primary_users = []
    headers = _session_headers()
    key = (await (await client.post("/chat/open", json={}, headers=headers)).json())[
        "session_key"
    ]
    with structlog.testing.capture_logs() as captured:
        r = await client.post(
            "/chat/turn", json={"session_key": key, "message": "hi"}, headers=headers
        )
    assert r.status == 200
    assert captured_run_turn[0]["calibration_str"] is None
    assert [
        c for c in captured if c.get("event") == "web.chat.calibration_not_injected"
    ]


# ---------------------------------------------------------------------------
# The CAPTURE door — session close drafting proposals into the pending store
# ---------------------------------------------------------------------------


_ANALYZER_REPLY = json.dumps([
    {
        "subsection": "Communication Style",
        "bullet": "Prefers bottom-line-up-front answers.",
        "confidence": 0.85,
    }
])


async def _drain_capture_tasks() -> None:
    """Await whatever the route scheduled (the door is a detached task)."""
    from alfred.telegram import calibration_capture

    for _ in range(20):
        tasks = set(calibration_capture._CAPTURE_TASKS)
        if not tasks:
            await asyncio.sleep(0)
            continue
        await asyncio.gather(*tasks, return_exceptions=True)
        return
    await asyncio.sleep(0)


async def test_reopen_captures_calibration_proposals_when_enabled(
    aiohttp_client, tmp_path
) -> None:
    """THE CAPTURE DOOR, driven through ``/chat/open``.

    Reopening archives the prior session; the calibration door then reads that
    record and drafts proposals into the PENDING store — and writes no vault.
    """
    from alfred.telegram import calibration_store

    _write_person_record(tmp_path / "vault", "- existing line.")
    cal = CalibrationConfig(
        capture_enabled=True,
        pending_path=str(tmp_path / "pending.jsonl"),
        decided_path=str(tmp_path / "decided.jsonl"),
    )
    client = await _build_client(
        aiohttp_client, tmp_path, cal,
        # EVERY response is the analyzer's JSON, deliberately. Queueing chat
        # replies first and the analyzer's last couples the test to the exact
        # position the door fires at — which is precisely the thing under test
        # and therefore the thing the fixture must not assume. The chat turns
        # simply reply with JSON text, which nothing here asserts on.
        [FakeResponse(content=[FakeBlock(type="text", text=_ANALYZER_REPLY)])
         for _ in range(6)],
    )
    headers = _session_headers()
    key = (await (await client.post("/chat/open", json={}, headers=headers)).json())[
        "session_key"
    ]
    await client.post(
        "/chat/turn", json={"session_key": key, "message": "hi there"}, headers=headers
    )
    # Reopen → archives the prior session → the capture door fires.
    await client.post("/chat/open", json={}, headers=headers)
    await _drain_capture_tasks()

    pending = calibration_store.open_proposals(cal.pending_path, cal.decided_path)
    assert len(pending) == 1
    assert pending[0].bullet == "Prefers bottom-line-up-front answers."
    assert pending[0].source_session_rel

    # AND THE GUARDRAIL AT THE DOOR: capture proposed, it did not apply. The
    # person record still carries only what it started with.
    record = (tmp_path / "vault" / f"{PRIMARY_USER_REL}.md").read_text(encoding="utf-8")
    assert "Prefers bottom-line-up-front answers." not in record
    assert "existing line." in record


async def test_reopen_captures_nothing_when_capture_disabled(
    aiohttp_client, tmp_path
) -> None:
    """The POSITIVE CONTROL's partner: default-off really is off.

    Paired with the test above so "no proposals" is proven to mean the switch,
    not a capture path that never worked.
    """
    from alfred.telegram import calibration_store

    _write_person_record(tmp_path / "vault", "- existing line.")
    cal = CalibrationConfig(
        capture_enabled=False,
        pending_path=str(tmp_path / "pending.jsonl"),
        decided_path=str(tmp_path / "decided.jsonl"),
    )
    client = await _build_client(
        aiohttp_client, tmp_path, cal,
        # EVERY response is the analyzer's JSON, deliberately. Queueing chat
        # replies first and the analyzer's last couples the test to the exact
        # position the door fires at — which is precisely the thing under test
        # and therefore the thing the fixture must not assume. The chat turns
        # simply reply with JSON text, which nothing here asserts on.
        [FakeResponse(content=[FakeBlock(type="text", text=_ANALYZER_REPLY)])
         for _ in range(6)],
    )
    headers = _session_headers()
    key = (await (await client.post("/chat/open", json={}, headers=headers)).json())[
        "session_key"
    ]
    await client.post(
        "/chat/turn", json={"session_key": key, "message": "hi there"}, headers=headers
    )
    await client.post("/chat/open", json={}, headers=headers)
    await _drain_capture_tasks()

    assert calibration_store.open_proposals(cal.pending_path, cal.decided_path) == []
    assert not Path(cal.pending_path).exists()
