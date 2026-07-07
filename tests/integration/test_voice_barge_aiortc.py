"""Integration: a V3 barge over a real WebRTC session (the §1.6 seam pin).

GATED on aiortc/av (skipif). Drives the WHOLE plane in-process — a client
RTCPeerConnection with a ``voice`` datachannel + a tone track → the real server
media path → an INJECTED 2-utterance FakeStreamProvider script → the turn driver
(barge ENABLED) → ``run_turn_streaming`` over a scripted streaming Anthropic fake
→ the FakeTTSProvider tone → the REAL ``TTSPlayoutSource`` → the outbound track.

Scenario (contract §1.6 — the confirmed-barge case, exercised end-to-end):

1. utterance-1 fires → turn T1 → a LONG reply, so its TTS tone plays out over
   several wall-clock seconds on the outbound track (``speaking_started`` fires).
2. WHILE T1 is still SPEAKING, utterance-2 fires. Its partial already qualifies
   (Stage-A silent audio flush, no wire event, §1.6); its final re-confirms
   (Stage-B) → ``stt_final`` → ``speaking_done{barged_in}`` → the driver runs T2.

The barge's PRINCIPAL job is to interrupt the AUDIO — so this test keys on the
outbound track: T1's tone is heard, the flush stops it, and NO stale T1 tone
survives past the flush (the §1.6 generation-gate at integration level).

Two deliberate design choices make the pin ROBUST against the fake's fixed 440 Hz
tone (T1 and T2 would be acoustically indistinguishable):

* **T2's reply is EMPTY** — so after the flush the outbound track goes and STAYS
  silent while T2 still completes (``turn_final``). Any T1 frame leaking past the
  flush would show up as non-silence in a multi-second post-barge window where
  T1's flushed tone WOULD otherwise have played. This is the cleanest observable
  proof of "audio frames stop" + "zero stale T1 audio" with one tone frequency.
* The fake STT **script is injected** (the mount hard-wires the default 3-utter
  script) via a monkeypatch of the ``_make_stt_provider`` fake seam — the only
  test seam touched; the pc/driver/worker/playout are all the real thing.

The assertions hold whether, by the time the barge fires, T1's *text* turn has
already completed (the instant fake — the common case: barge interrupts live
playout) OR is still in flight (would add a ``turn_cancelled(T1)`` between the
``speaking_done`` and ``turn_started(T2)`` — still one ``speaking_done``, still
table order), so the pin is not brittle to turn/playout scheduling.
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
    BargeInConfig,
    WebAuthConfig,
    WebConfig,
    WebUser,
    WebVoiceConfig,
    WebVoiceSttConfig,
    WebVoiceTtsConfig,
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

# A long, single-sentence reply → the fake TTS tone runs for several seconds
# (fake: ms = chars * 50, capped at 5000), giving a wide mid-playout window for
# the barge to land in. Deliberately DISJOINT from the barge utterance below so
# the echo gate can't suppress it.
_T1_REPLY = (
    "the harbor lay quiet under a pale grey dawn as gulls wheeled above the "
    "water and a small fishing boat drifted slowly past the old wooden pier "
    "toward the open bay"
)
# A non-empty T2 reply for the audio-after-barge variant (disjoint from the
# barge utterance so nothing re-triggers). Long enough for a multi-second tone.
_T2_REPLY = (
    "the second reply flows on for a while so its spoken tone runs several "
    "seconds and can be measured cleanly on the outbound track after the flush"
)
# The barge utterance — a partial that already qualifies for the Stage-A audio
# flush (>= min_words / min_chars, not a backchannel/interrupt-phrase, disjoint
# from _T1_REPLY) and a final that re-confirms at Stage B.
_BARGE_PARTIAL = "switch topics"
_BARGE_FINAL = "switch topics please now"

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


def _barge_script():
    """A precise 2-utterance script (the mount hard-wires the default 3-utter
    one, so it is injected via ``_make_stt_provider``). utterance-2 fires ~20
    feed-chunks after utterance-1 (~2 s at the media path's ~10 feeds/s), i.e.
    while T1's multi-second tone is still playing."""
    from alfred.web.stt_stream import FakeUtterance

    return [
        FakeUtterance(chunks=20, partials=["begin"], final="begin the tour"),
        FakeUtterance(chunks=20, partials=[_BARGE_PARTIAL], final=_BARGE_FINAL),
    ]


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
            tts=WebVoiceTtsConfig(
                enabled=True, provider="fake",
                barge_in=BargeInConfig(enabled=True),
            ),
        ),
    )


@pytest.fixture
async def barge_client(aiohttp_client, tmp_path, monkeypatch):
    # Inject the 2-utterance barge script into the fake STT provider (the only
    # test seam — everything downstream is the real plane).
    from alfred.web import routes_voice

    def _fake_stt(stt_norm, vid):
        from alfred.web.stt_stream import FakeStreamProvider
        return FakeStreamProvider(script=_barge_script(), voice_session_id=vid)

    monkeypatch.setattr(routes_voice, "_make_stt_provider", _fake_stt)

    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    web_auth_state = WebAuthState.create(tmp_path / "web_auth_state.json")
    web_auth_state.load()
    # T1 → a long spoken reply; T2 (the barge) → an EMPTY reply (so the post-
    # flush track is cleanly silent). Trailing guard entries in case the engine
    # opens an unexpected extra stream iteration.
    client = _streaming_client([
        ([_text_delta(_T1_REPLY)], _FinalMsg([_TextBlk(_T1_REPLY)], "end_turn")),
        ([], _FinalMsg([_TextBlk("")], "end_turn")),
        ([], _FinalMsg([_TextBlk("")], "end_turn")),
    ])
    register_web_routes(
        app, web_config=_web_config(), web_auth_state=web_auth_state,
        anthropic_client=client, state_mgr=state_mgr,
        talker_config=_talker_config(tmp_path),
        system_prompt_provider=lambda: "SYS", vault_context_str="CTX",
        allowed_user_ids=[1],
    )
    return await aiohttp_client(app)


@pytest.fixture
async def barge_speaking_client(aiohttp_client, tmp_path, monkeypatch):
    # As barge_client, but T2 (the barge's turn) has a NON-EMPTY reply so it
    # SPEAKS after the flush — the regression pin for the av-resampler EOF that
    # killed the TTS pump on the first post-barge audio frame (all further
    # session audio lost). The empty-T2 test masks that bug by construction.
    from alfred.web import routes_voice

    def _fake_stt(stt_norm, vid):
        from alfred.web.stt_stream import FakeStreamProvider
        return FakeStreamProvider(script=_barge_script(), voice_session_id=vid)

    monkeypatch.setattr(routes_voice, "_make_stt_provider", _fake_stt)

    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    web_auth_state = WebAuthState.create(tmp_path / "web_auth_state.json")
    web_auth_state.load()
    client = _streaming_client([
        ([_text_delta(_T1_REPLY)], _FinalMsg([_TextBlk(_T1_REPLY)], "end_turn")),
        ([_text_delta(_T2_REPLY)], _FinalMsg([_TextBlk(_T2_REPLY)], "end_turn")),
        ([], _FinalMsg([_TextBlk("")], "end_turn")),
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


async def test_barge_full_loop_flushes_audio_and_runs_t2(barge_client) -> None:
    import json

    import numpy as np
    from aiortc import RTCPeerConnection, RTCSessionDescription

    client = barge_client
    state_mgr = client.app[KEY_WEB_STATE_MGR]
    chat = open_chat_session(
        state_mgr, synthetic_chat_id("andrew"), model="claude-sonnet-4-6")

    pc = RTCPeerConnection()
    events: list[dict] = []              # every DC frame, in arrival order
    frame_peaks: list[tuple[float, int]] = []   # (recv_time, |peak|) per audio frame
    barge_at: list[float] = []           # recv time of speaking_done{barged_in}
    got_barge = asyncio.Event()
    got_t2_final = asyncio.Event()

    dc = pc.createDataChannel("voice", ordered=True)

    @dc.on("open")
    def _on_open() -> None:
        dc.send(json.dumps({"v": 1, "type": "hello"}))

    @dc.on("message")
    def _on_message(raw) -> None:
        ev = json.loads(raw)
        events.append(ev)
        etype = ev.get("type")
        if etype == "speaking_done" and ev.get("reason") == "barged_in":
            barge_at.append(asyncio.get_event_loop().time())
            got_barge.set()
        # T1's turn_final lands ~2 s BEFORE the barge, so the first turn_final
        # seen AFTER the barge is T2's — its arrival means T2 completed.
        if etype == "turn_final" and got_barge.is_set():
            got_t2_final.set()

    @pc.on("track")
    def _on_track(track) -> None:
        async def consume():
            try:
                while True:
                    frame = await track.recv()
                    arr = frame.to_ndarray()
                    frame_peaks.append(
                        (asyncio.get_event_loop().time(), int(np.abs(arr).max())))
            except Exception:  # noqa: BLE001 — track ends on teardown
                pass

        asyncio.ensure_future(consume())

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
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=body["sdp"], type="answer"))

        # utterance-1 (~2 s of tone) → T1; utterance-2 (~2 s later, mid-playout)
        # → the barge. Generous headroom for ICE + media flow.
        await asyncio.wait_for(got_barge.wait(), timeout=30)
        barge_time = barge_at[0]
        # T2 (empty reply) completes fast after the barge.
        await asyncio.wait_for(got_t2_final.wait(), timeout=10)
        # Let the post-barge window fill: this is where T1's FLUSHED tone would
        # have kept playing if the gen-gate leaked. Also drains the jitter buffer.
        await asyncio.sleep(2.5)

        # --- DC wire: the §1.6 table order --------------------------------
        # Exactly one speaking_done, and it is the barge (reason barged_in).
        speaking_dones = [e for e in events if e["type"] == "speaking_done"]
        assert len(speaking_dones) == 1, [e["type"] for e in events]
        assert speaking_dones[0]["reason"] == "barged_in"

        # T1 spoke exactly once; T2 (empty) never speaks.
        speaking_starts = [e for e in events if e["type"] == "speaking_started"]
        assert len(speaking_starts) == 1
        t1_id = speaking_starts[0]["turn_id"]
        assert speaking_dones[0]["turn_id"] == t1_id

        # Two turn_started (T1 then T2), distinct ids; the barge's turn is T1.
        turn_starts = [e for e in events if e["type"] == "turn_started"]
        assert len(turn_starts) == 2
        assert turn_starts[0]["turn_id"] == t1_id
        t2_id = turn_starts[1]["turn_id"]
        assert t2_id != t1_id

        # Table order (reg-W3): stt_final(barge) → speaking_done{barged_in} →
        # turn_started(T2).
        i_stt_final = next(
            i for i, e in enumerate(events)
            if e["type"] == "stt_final" and e.get("text") == _BARGE_FINAL)
        i_speaking_done = next(
            i for i, e in enumerate(events) if e["type"] == "speaking_done")
        i_turn_started_t2 = next(
            i for i, e in enumerate(events)
            if e["type"] == "turn_started" and e["turn_id"] == t2_id)
        assert i_stt_final < i_speaking_done < i_turn_started_t2

        # T2 ran to completion (born of the barge) — its turn_final arrived.
        t2_finals = [e for e in events
                     if e["type"] == "turn_final" and e["turn_id"] == t2_id]
        assert len(t2_finals) == 1

        # --- outbound audio: T1 heard, flush stops it, NO stale T1 tone ----
        pre = [p for (t, p) in frame_peaks if t < barge_time]
        post = [p for (t, p) in frame_peaks if t > barge_time + 1.0]
        assert pre and max(pre) > 500, "T1 never spoke on the outbound track"
        assert post, "no audio frames captured after the barge"
        # The flush dropped T1's queued tone; T2 is silent — so a full second+
        # after the barge the track must be silent. A leaked T1 frame (gen-gate
        # miss) would ring at the ~440 Hz tone's amplitude here.
        assert max(post) < 500, f"stale T1 audio after the flush: peak={max(post)}"
    finally:
        await pc.close()


async def test_barge_then_t2_speaks_over_the_track(barge_speaking_client) -> None:
    # Regression for the av-resampler EOF (the smoke's "silent after the first
    # barge"): T2 has a NON-EMPTY reply, so after the flush the real playout
    # must resample + carry T2's tone. Before the fix, flush()'s resampler drain
    # left av's resampler EOF, the first post-flush frame raised EOFError inside
    # the worker pump and killed it → T2 (and every later turn) produced ZERO
    # audio while still emitting speaking_started. Assert SUSTAINED T2 tone.
    import json

    import numpy as np
    from aiortc import RTCPeerConnection, RTCSessionDescription

    client = barge_speaking_client
    state_mgr = client.app[KEY_WEB_STATE_MGR]
    chat = open_chat_session(
        state_mgr, synthetic_chat_id("andrew"), model="claude-sonnet-4-6")

    pc = RTCPeerConnection()
    events: list[dict] = []
    frame_peaks: list[tuple[float, int]] = []
    barge_at: list[float] = []
    got_barge = asyncio.Event()
    got_t2_final = asyncio.Event()

    dc = pc.createDataChannel("voice", ordered=True)

    @dc.on("open")
    def _on_open() -> None:
        dc.send(json.dumps({"v": 1, "type": "hello"}))

    @dc.on("message")
    def _on_message(raw) -> None:
        ev = json.loads(raw)
        events.append(ev)
        etype = ev.get("type")
        if etype == "speaking_done" and ev.get("reason") == "barged_in":
            barge_at.append(asyncio.get_event_loop().time())
            got_barge.set()
        if etype == "turn_final" and got_barge.is_set():
            got_t2_final.set()

    @pc.on("track")
    def _on_track(track) -> None:
        async def consume():
            try:
                while True:
                    frame = await track.recv()
                    arr = frame.to_ndarray()
                    frame_peaks.append(
                        (asyncio.get_event_loop().time(), int(np.abs(arr).max())))
            except Exception:  # noqa: BLE001 — track ends on teardown
                pass

        asyncio.ensure_future(consume())

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
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=body["sdp"], type="answer"))

        await asyncio.wait_for(got_barge.wait(), timeout=30)
        barge_time = barge_at[0]
        await asyncio.wait_for(got_t2_final.wait(), timeout=15)
        # T2's tone plays out over several seconds AFTER the barge — capture it.
        await asyncio.sleep(3.0)

        # T2 attempted to speak (its own speaking_started) AND completed.
        speaking_starts = [e for e in events if e["type"] == "speaking_started"]
        assert len(speaking_starts) == 2                      # T1 then T2
        t2_id = [e for e in events if e["type"] == "turn_started"][1]["turn_id"]
        assert any(e["type"] == "turn_final" and e["turn_id"] == t2_id
                   for e in events)

        # THE PIN: sustained non-silent audio AFTER the barge = T2's tone made it
        # through the real resampler + playout. A pump killed by the resampler
        # EOF would leave this window silent (speaking_started fired, then dead).
        post = [p for (t, p) in frame_peaks if t > barge_time + 1.0]
        loud = [p for p in post if p > 500]
        assert len(loud) >= 20, (
            "T2 produced no sustained audio after the barge (av-resampler EOF "
            f"would kill the pump): {len(loud)} loud of {len(post)} frames")
    finally:
        await pc.close()
