"""Integration: real aiortc in-process loopback echo (V0 voice).

GATED — ``pytest.mark.skipif(find_spec("aiortc") is None)``. This is an
OPTIONAL-feature test (visible in ``-ra`` output), NOT a regression pin; the
unconditional pins live in ``tests/test_web_routes_voice.py`` +
``tests/test_web_voice_session.py``.

Proves the whole media path end-to-end WITHOUT the WSL2 NAT-mode UDP boundary
(client PC + server PC share the process + network namespace): a client
RTCPeerConnection with a 440 Hz sine-tone track offers through the REAL app
fixture (real auth headers), the server echoes it back, and the client
receives non-silent frames. Then close → 200 ``closed:true`` and config
``yours`` empties.
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
from alfred.web.config import WebAuthConfig, WebConfig, WebUser, WebVoiceConfig
from alfred.web.routes_chat import register_web_routes
from alfred.web.state import WebAuthState

from tests.telegram.conftest import FakeAnthropicClient

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("aiortc") is None,
    reason="aiortc not installed (webrtc extra) — optional-feature integration test",
)

DUMMY_WEB_PEER_TOKEN = "DUMMY_WEB_PEER_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_0123456"
DUMMY_WEB_SIGNING_SECRET = "DUMMY_WEB_SIGNING_SECRET_FOR_TESTING_ONLY_0123456789"

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


def _make_talker_config(tmp_path: Path) -> TalkerConfig:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    for sub in ("session", "note"):
        (vault_dir / sub).mkdir()
    return TalkerConfig(
        bot_token="test-token",
        allowed_users=[1],
        primary_users=["person/Andrew Newton"],
        anthropic=AnthropicConfig(api_key="test-key", model="claude-sonnet-4-6"),
        stt=STTConfig(api_key="test-stt", model="whisper-large-v3"),
        session=SessionConfig(
            gap_timeout_seconds=1800,
            state_path=str(tmp_path / "talker_state.json"),
        ),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name="Salem", canonical="S.A.L.E.M."),
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


def _web_config() -> WebConfig:
    return WebConfig(
        enabled=True,
        users=[WebUser(name="andrew", role="owner")],
        auth=WebAuthConfig(session_secret=DUMMY_WEB_SIGNING_SECRET),
        voice=WebVoiceConfig(enabled=True, max_sessions=2, pipeline="echo"),
    )


@pytest.fixture
async def voice_app_client(aiohttp_client, tmp_path):
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    web_auth_state = WebAuthState.create(tmp_path / "web_auth_state.json")
    web_auth_state.load()
    register_web_routes(
        app,
        web_config=_web_config(),
        web_auth_state=web_auth_state,
        anthropic_client=FakeAnthropicClient([]),
        state_mgr=state_mgr,
        talker_config=_make_talker_config(tmp_path),
        system_prompt_provider=lambda: "SYS",
        vault_context_str="CTX",
        allowed_user_ids=[1],
    )
    return await aiohttp_client(app)


def _sine_track():
    """A 440 Hz sine-tone AudioStreamTrack (paced at 20 ms/48 k frames)."""
    import av
    import numpy as np
    from aiortc.mediastreams import MediaStreamTrack

    class SineAudioTrack(MediaStreamTrack):
        kind = "audio"

        def __init__(self) -> None:
            super().__init__()
            self.sample_rate = 48000
            self.samples = 960  # 20 ms
            self._pts = 0
            self._start: float | None = None

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
            wave = (0.5 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
            frame = av.AudioFrame.from_ndarray(
                wave.reshape(1, -1), format="s16", layout="mono",
            )
            frame.sample_rate = self.sample_rate
            frame.pts = self._pts
            frame.time_base = fractions.Fraction(1, self.sample_rate)
            return frame

    return SineAudioTrack()


async def _negotiate(client, pc) -> str:
    """Create + POST an offer through the real route; apply the answer.
    Returns the ``voice_session_id``."""
    from aiortc import RTCSessionDescription

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)  # vanilla ICE — waits for gathering
    resp = await client.post(
        "/voice/offer", headers=_headers(),
        json={"sdp": pc.localDescription.sdp, "type": "offer"},
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["type"] == "answer"
    await pc.setRemoteDescription(
        RTCSessionDescription(sdp=body["sdp"], type="answer")
    )
    return body["voice_session_id"]


async def test_echo_frames_flow_back(voice_app_client) -> None:
    import numpy as np
    from aiortc import RTCPeerConnection

    client = voice_app_client
    pc = RTCPeerConnection()
    got_track: asyncio.Future = asyncio.get_event_loop().create_future()

    @pc.on("track")
    def on_track(track):
        if not got_track.done():
            got_track.set_result(track)

    pc.addTrack(_sine_track())
    try:
        vid = await asyncio.wait_for(_negotiate(client, pc), timeout=20)
        assert len(vid) == 32

        track = await asyncio.wait_for(got_track, timeout=10)
        # Pull frames until we see real (non-silent) audio echoed back.
        max_amp = 0
        for _ in range(100):
            frame = await asyncio.wait_for(track.recv(), timeout=10)
            arr = frame.to_ndarray()
            max_amp = max(max_amp, int(np.abs(arr).max()))
            if max_amp > 1000:
                break
        assert max_amp > 1000, f"echo silent (peak amplitude {max_amp})"
    finally:
        await pc.close()


async def test_connect_close_and_config_lifecycle(voice_app_client) -> None:
    from aiortc import RTCPeerConnection

    client = voice_app_client
    pc = RTCPeerConnection()
    connected = asyncio.Event()

    @pc.on("connectionstatechange")
    def on_state():
        if pc.connectionState == "connected":
            connected.set()

    @pc.on("track")
    def on_track(track):  # keep the transceiver alive; not inspected here
        pass

    pc.addTrack(_sine_track())
    try:
        vid = await asyncio.wait_for(_negotiate(client, pc), timeout=20)
        await asyncio.wait_for(connected.wait(), timeout=15)

        # config lists the session as ours while it is live.
        resp = await client.get("/voice/config", headers=_headers())
        body = await resp.json()
        assert body["available"] is True
        assert any(y["voice_session_id"] == vid for y in body["yours"])

        # close → 200 closed:true (own live session).
        resp = await client.post(
            "/voice/close", headers=_headers(),
            json={"voice_session_id": vid},
        )
        assert resp.status == 200
        assert await resp.json() == {"closed": True}

        # config now shows no sessions for the caller.
        resp = await client.get("/voice/config", headers=_headers())
        assert (await resp.json())["yours"] == []
    finally:
        await pc.close()
