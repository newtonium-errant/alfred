"""Integration: full V1 assistant loop over a real WebRTC session.

GATED on aiortc/av (skipif). Drives the WHOLE plane in-process: a client
RTCPeerConnection with a ``voice`` datachannel + a tone track → the real
server media path (resample → chunk) → the FakeStreamProvider (fires an
utterance on feed-count) → the turn driver → ``run_turn_streaming`` over a
scripted streaming Anthropic fake → a ``turn_final`` reply back over the
datachannel. Proves the seam integration the unit tests pin piecewise.
"""

from __future__ import annotations

import asyncio
import fractions
import importlib.util
from pathlib import Path

import pytest

from alfred.telegram.config import (
    AnthropicConfig,
    InstanceConfig,
    LoggingConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
)
from alfred.telegram.session import open_session as open_chat_session
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
from alfred.web.config import (
    WebAuthConfig,
    WebConfig,
    WebUser,
    WebVoiceConfig,
    WebVoiceSttConfig,
)
from alfred.web.identity import synthetic_chat_id
from alfred.web.keys import KEY_WEB_STATE_MGR
from alfred.web.routes_chat import register_web_routes
from alfred.web.state import WebAuthState

from tests.telegram.test_run_turn_streaming import (
    _FinalMsg,
    _TextBlk,
    _streaming_client,
    _text_delta,
)

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("aiortc") is None,
    reason="aiortc not installed (webrtc extra) — optional-feature integration test",
)

DUMMY_WEB_PEER_TOKEN = "DUMMY_WEB_PEER_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_0123456"
DUMMY_WEB_SIGNING_SECRET = "DUMMY_WEB_SIGNING_SECRET_FOR_TESTING_ONLY_0123456789"
_REPLY = "hello from salem"

_HEADERS = {
    "Authorization": f"Bearer {DUMMY_WEB_PEER_TOKEN}",
    "X-Alfred-Client": "web",
    "Content-Type": "application/json",
}


def _headers() -> dict[str, str]:
    token = make_session_token(
        "andrew", "owner", secret=DUMMY_WEB_SIGNING_SECRET, ttl_hours=168,
    )
    return {**_HEADERS, SESSION_HEADER: token}


def _talker_config(tmp_path: Path) -> TalkerConfig:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    for sub in ("session", "note"):
        (vault_dir / sub).mkdir()
    return TalkerConfig(
        bot_token="test-token", allowed_users=[1],
        primary_users=["person/Andrew Newton"],
        anthropic=AnthropicConfig(api_key="test-key", model="claude-sonnet-4-6"),
        stt=STTConfig(api_key="test-stt", model="whisper-large-v3"),
        session=SessionConfig(gap_timeout_seconds=1800,
                              state_path=str(tmp_path / "talker_state.json")),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name="Salem", canonical="S.A.L.E.M."),
    )


def _transport_config() -> TransportConfig:
    return TransportConfig(
        server=ServerConfig(),
        auth=AuthConfig(tokens={
            "web": AuthTokenEntry(token=DUMMY_WEB_PEER_TOKEN, allowed_clients=["web"]),
        }),
        state=StateConfig(),
    )


def _web_config() -> WebConfig:
    return WebConfig(
        enabled=True,
        users=[WebUser(name="andrew", role="owner")],
        auth=WebAuthConfig(session_secret=DUMMY_WEB_SIGNING_SECRET),
        voice=WebVoiceConfig(
            enabled=True, max_sessions=2, pipeline="assistant",
            stt=WebVoiceSttConfig(provider="fake"),
        ),
    )


@pytest.fixture
async def assistant_client(aiohttp_client, tmp_path):
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    web_auth_state = WebAuthState.create(tmp_path / "web_auth_state.json")
    web_auth_state.load()
    # A scripted streaming client: any utterance → the same reply.
    client = _streaming_client([
        ([_text_delta(_REPLY)], _FinalMsg([_TextBlk(_REPLY)], "end_turn"))
        for _ in range(4)
    ])
    register_web_routes(
        app, web_config=_web_config(), web_auth_state=web_auth_state,
        anthropic_client=client, state_mgr=state_mgr,
        talker_config=_talker_config(tmp_path),
        system_prompt_provider=lambda: "SYS", vault_context_str="CTX",
        allowed_user_ids=[1],
    )
    return await aiohttp_client(app)


def _tone_track():
    import av
    import numpy as np
    from aiortc.mediastreams import MediaStreamTrack

    class ToneTrack(MediaStreamTrack):
        kind = "audio"

        def __init__(self) -> None:
            super().__init__()
            self.sample_rate = 48000
            self.samples = 960
            self._pts = 0
            self._start = None

        async def recv(self):
            loop = asyncio.get_event_loop()
            if self._start is None:
                self._start = loop.time()
            self._pts += self.samples
            target = self._start + self._pts / self.sample_rate
            delay = target - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            t = (np.arange(self.samples) + self._pts) / self.sample_rate
            wave = (0.4 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
            frame = av.AudioFrame.from_ndarray(
                wave.reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate = self.sample_rate
            frame.pts = self._pts
            frame.time_base = fractions.Fraction(1, self.sample_rate)
            return frame

    return ToneTrack()


async def test_assistant_full_loop_to_turn_final(assistant_client) -> None:
    from aiortc import RTCPeerConnection, RTCSessionDescription

    client = assistant_client
    # Pre-open a chat session so the voice turn lands in the same history.
    state_mgr = client.app[KEY_WEB_STATE_MGR]
    chat = open_chat_session(
        state_mgr, synthetic_chat_id("andrew"), model="claude-sonnet-4-6")

    pc = RTCPeerConnection()
    events: list[dict] = []
    got_final = asyncio.Event()

    dc = pc.createDataChannel("voice", ordered=True)

    @dc.on("open")
    def _on_open() -> None:
        import json
        dc.send(json.dumps({"v": 1, "type": "hello"}))

    @dc.on("message")
    def _on_message(raw) -> None:
        import json
        ev = json.loads(raw)
        events.append(ev)
        if ev.get("type") == "turn_final":
            got_final.set()

    pc.addTrack(_tone_track())
    try:
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)
        resp = await client.post(
            "/voice/offer", headers=_headers(),
            json={"sdp": pc.localDescription.sdp, "type": "offer",
                  "session_key": chat.session_id},
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["chat_session_key"] == chat.session_id
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=body["sdp"], type="answer"))

        # The fake provider fires an utterance after ~20 fed chunks (~2 s of
        # tone); allow generous headroom for ICE + media flow.
        await asyncio.wait_for(got_final.wait(), timeout=25)

        types = [e["type"] for e in events]
        assert "state" in types  # ready ack
        assert "stt_final" in types
        final = next(e for e in events if e["type"] == "turn_final")
        assert final["reply"] == _REPLY
        assert final["truncated"] is False
        # The voice turn landed in the SAME chat history.
        active = state_mgr.get_active(synthetic_chat_id("andrew"))
        assert active["session_id"] == chat.session_id
        assert any(_REPLY in str(t.get("content", "")) for t in active["transcript"])
    finally:
        await pc.close()
