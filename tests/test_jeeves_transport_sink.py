"""The device-side peer link, and the whole chain through it (task #81).

The last file in the stack, and the one that proves the two halves meet: a
JeevesService with a real sink, pointed at a real transport app running the
real route, writing a real vault record. Everything between the microphone
and the vault is exercised — only the mic adapter and the acoustic model are
substituted.
"""

from __future__ import annotations

import frontmatter
import pytest
import structlog

from alfred.jeeves import cues, marklog, service, telemetry
from alfred.jeeves.audio import AudioFormat, MemoryAudioSource, silence, tone
from alfred.jeeves.config import (
    JEEVES_MODE_LIVE,
    JeevesConfig,
    JeevesCueConfig,
    JeevesRingConfig,
    JeevesRouteSinkConfig,
    JeevesSttConfig,
    JeevesWindowConfig,
    load_from_unified,
)
from alfred.jeeves.transport_sink import (
    CAPTURE_PATH,
    JeevesTransportSink,
    build_route_sink,
)
from alfred.jeeves.wake import ScriptedWakeDetector
from alfred.telegram.stt_backends import SttResult
from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.peer_handlers import register_vault_path
from alfred.transport.routes_jeeves import JEEVES_PEER_NAME, register_jeeves_routes
from alfred.transport.server import build_app
from alfred.transport.state import TransportState

DUMMY_JEEVES_PEER_TOKEN = (
    "DUMMY_JEEVES_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_01234567890"
)
FMT = AudioFormat(sample_rate=1000, sample_width=2, channels=1)


# ---------------------------------------------------------------------------
# The path constant
# ---------------------------------------------------------------------------


def test_the_sink_and_the_route_agree_on_the_path():
    """DRIFT PIN. The device posts to a URL the server mounts; a rename on
    one side would leave the device posting into a 404, which in the garage
    is indistinguishable from success."""
    import inspect

    from alfred.transport import routes_jeeves

    source = inspect.getsource(routes_jeeves.register_jeeves_routes)
    assert f'add_post("{CAPTURE_PATH}"' in source


# ---------------------------------------------------------------------------
# Construction — inert unless fully configured
# ---------------------------------------------------------------------------


def test_a_sink_needs_BOTH_a_url_and_a_token():
    """Either alone cannot send anything, and a half-configured sink would
    fail every send where the local fallback is the better shape."""
    for route in (
        JeevesRouteSinkConfig(),
        JeevesRouteSinkConfig(base_url="https://x"),
        JeevesRouteSinkConfig(token="t"),
    ):
        config = JeevesConfig(route=route)
        assert build_route_sink(config) is None


def test_an_inert_sink_says_so():
    """Intentionally-left-blank: a device quietly keeping everything local
    must be distinguishable from one that is sending, because in the garage
    the two look identical."""
    with structlog.testing.capture_logs() as captured:
        build_route_sink(JeevesConfig())
    events = [c for c in captured if c.get("event") == "jeeves.sink.inert"]
    assert len(events) == 1
    assert "LOCAL mark log" in events[0]["detail"]


def test_a_configured_sink_is_armed_and_logged():
    config = JeevesConfig(route=JeevesRouteSinkConfig(
        base_url="https://peerbox:8891", token="DUMMY_JEEVES_TEST_TOKEN"))
    with structlog.testing.capture_logs() as captured:
        sink = build_route_sink(config)
    assert sink is not None
    assert sink.url == f"https://peerbox:8891{CAPTURE_PATH}"
    events = [c for c in captured if c.get("event") == "jeeves.sink.armed"]
    assert len(events) == 1


def test_a_synthetic_device_TAGS_what_it_sends():
    """So a synthetic-mode receiver accepts it — which is what makes an
    end-to-end dev path possible without either side flipped to live."""
    config = JeevesConfig(route=JeevesRouteSinkConfig(
        base_url="https://x", token="DUMMY_JEEVES_TEST_TOKEN"))
    sink = build_route_sink(config)
    assert sink.provenance == {"synthetic": True}


def test_a_LIVE_device_asserts_nothing_about_itself():
    """A live device sends no synthetic tag: if the receiver is still
    synthetic it refuses, loudly, which is correct. An un-flipped receiver
    must not silently accept real garage audio because the DEVICE asserted
    something."""
    config = JeevesConfig(
        mode=JEEVES_MODE_LIVE,
        route=JeevesRouteSinkConfig(
            base_url="https://x", token="DUMMY_JEEVES_TEST_TOKEN"),
    )
    sink = build_route_sink(config)
    assert sink.provenance is None


def test_an_unresolved_token_placeholder_leaves_the_sink_inert():
    """Arming a peer link with a truthy, publicly-known placeholder is
    strictly worse than keeping captures local."""
    with structlog.testing.capture_logs() as captured:
        loaded = load_from_unified({"jeeves": {"route": {
            "base_url": "https://x",
            "token": "${JEEVES_TOKEN_THAT_IS_NOT_SET}",
        }}})
    assert loaded.route.token == ""
    assert build_route_sink(loaded) is None
    assert [c for c in captured
            if c.get("event") == "jeeves.config.unresolved_route_token_placeholder"]


def test_a_trailing_slash_on_the_base_url_does_not_double_up():
    sink = JeevesTransportSink(base_url="https://x/", token="t")
    assert sink.url == f"https://x{CAPTURE_PATH}"


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------


def _capture(transcript="the bearing is a 6203") -> service.RoutedCapture:
    return service.RoutedCapture(
        transcript=transcript,
        verb=cues.CUE_ROUTE,
        captured_at="2026-08-11T14:03:02+00:00",
        target="peerbox",
        capture_facts={"lookback_used_seconds": 45.0, "stt_calls": 1},
    )


async def test_the_payload_is_built_from_NAMED_FIELDS_only(monkeypatch):
    """There is no path by which audio could ride along, even if something
    upstream started carrying it — the sink never forwards a caller-supplied
    dict wholesale."""
    sent: dict = {}

    class FakeResponse:
        status_code = 200
        text = ""

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            sent["url"] = url
            sent["json"] = json
            sent["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: FakeClient())

    sink = JeevesTransportSink(
        base_url="https://x", token="DUMMY_JEEVES_TEST_TOKEN",
        provenance={"synthetic": True},
    )
    assert await sink.send(_capture()) is True

    assert set(sent["json"]) == {
        "transcript", "verb", "captured_at", "capture_facts", "provenance",
    }
    assert sent["headers"]["Authorization"] == "Bearer DUMMY_JEEVES_TEST_TOKEN"
    assert sent["headers"]["X-Alfred-Client"] == "jeeves"


# ---------------------------------------------------------------------------
# Failure handling — never raise, always return
# ---------------------------------------------------------------------------


async def test_a_network_error_returns_false_rather_than_raising(monkeypatch):
    """A capture device must keep listening through a peer that is down.
    Returning False means the service keeps the words locally; raising would
    lose them, and the audio behind them is already gone."""
    import httpx

    class ExplodingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise httpx.ConnectError("no route to host")

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: ExplodingClient())
    sink = JeevesTransportSink(base_url="https://x", token="t")

    with structlog.testing.capture_logs() as captured:
        assert await sink.send(_capture()) is False
    events = [c for c in captured if c.get("event") == "jeeves.sink.send_failed"]
    assert len(events) == 1
    assert events[0]["reason"] == "network"
    # The stdout_tail sentinel — "no diagnostic output at all" stays greppable.
    assert events[0]["stdout_tail"] == ""


@pytest.mark.parametrize("status", [400, 401, 403, 413, 500, 503])
async def test_an_http_error_returns_false_with_the_body(monkeypatch, status):
    class FakeResponse:
        status_code = status
        text = '{"error":"wrong_peer"}'

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: FakeClient())
    sink = JeevesTransportSink(base_url="https://x", token="t")

    with structlog.testing.capture_logs() as captured:
        assert await sink.send(_capture()) is False
    events = [c for c in captured if c.get("event") == "jeeves.sink.send_failed"]
    # The refusal codes are the actionable part for a device author.
    assert "wrong_peer" in events[0]["detail"]


async def test_a_409_counts_as_delivered(monkeypatch):
    """The transcript IS in the vault, which is what the caller asked about.
    Treating a collision as failure would write a duplicate to the local log
    on every retry."""
    class FakeResponse:
        status_code = 409
        text = '{"error":"title_collision"}'

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: FakeClient())
    sink = JeevesTransportSink(base_url="https://x", token="t")

    with structlog.testing.capture_logs() as captured:
        assert await sink.send(_capture()) is True
    assert [c for c in captured if c.get("event") == "jeeves.sink.already_present"]


async def test_the_sent_log_carries_a_length_never_the_words(monkeypatch):
    class FakeResponse:
        status_code = 200
        text = ""

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return FakeResponse()

    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: FakeClient())
    sink = JeevesTransportSink(base_url="https://x", token="t")
    secret = "the alarm code is nine four two"

    with structlog.testing.capture_logs() as captured:
        await sink.send(_capture(secret))
    for entry in captured:
        assert "alarm code" not in " ".join(str(v) for v in entry.values())
    events = [c for c in captured if c.get("event") == "jeeves.sink.sent"]
    assert events[0]["transcript_chars"] == len(secret)


# ---------------------------------------------------------------------------
# THE WHOLE CHAIN — microphone-shaped audio in, vault record out
# ---------------------------------------------------------------------------


class StubStt:
    def __init__(self, text: str):
        self.text = text

    async def transcribe(self, audio, mime, vocab):
        return SttResult(
            text=self.text, backend_id="stub", tier="comparable",
            has_speech_signal=False, confidence_raw=-0.28,
        )


def _transport_config() -> TransportConfig:
    return TransportConfig(
        server=ServerConfig(),
        auth=AuthConfig(tokens={
            JEEVES_PEER_NAME: AuthTokenEntry(
                token=DUMMY_JEEVES_PEER_TOKEN, allowed_clients=["jeeves"]),
        }),
        state=StateConfig(),
    )


@pytest.fixture
async def receiver(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """A real transport app with the real capture route, in LIVE mode."""
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    vault = tmp_path / "vault"
    for sub in ("note", "source"):
        (vault / sub).mkdir(parents=True)
    register_vault_path(app, vault)
    register_jeeves_routes(
        app, enabled=True, instance_name="TestBox",
        jeeves_raw_config={"jeeves": {"mode": "live"}},
    )
    # Stashed BEFORE the app starts — aiohttp deprecates mutating a started
    # application, and the tests only need to read the path back.
    app["_vault"] = vault
    return await aiohttp_client(app)


def _device_config(tmp_path, base_url: str) -> JeevesConfig:
    return JeevesConfig(
        mode=JEEVES_MODE_LIVE,
        ring=JeevesRingConfig(
            seconds=60, sample_rate=1000, sample_width=2, channels=1),
        window=JeevesWindowConfig(
            lookback_seconds=5.0, silence_seconds=2.0,
            max_lookahead_seconds=5.0, silence_rms_threshold=0.01),
        stt=JeevesSttConfig(api_key="DUMMY_GROQ_TEST_KEY"),
        cues=JeevesCueConfig(route_target="peerbox"),
        route=JeevesRouteSinkConfig(
            base_url=base_url, token=DUMMY_JEEVES_PEER_TOKEN),
        telemetry_path=str(tmp_path / "telemetry.jsonl"),
        mark_log_path=str(tmp_path / "marks.jsonl"),
    )


def _stream():
    blobs = [tone(1.0, FMT) for _ in range(12)] + \
            [silence(1.0, FMT) for _ in range(4)]
    return MemoryAudioSource(blobs, audio_format=FMT)


async def test_a_route_cue_travels_from_the_ring_into_a_vault_record(
    receiver, tmp_path,
):
    """THE END-TO-END PIN. Ring -> scripted cue -> window -> stub STT ->
    classification -> sink -> HTTP -> peer pin -> mode gate -> vault
    scope -> a file on disk. Every layer is the production one."""
    base_url = str(receiver.make_url("")).rstrip("/")
    config = _device_config(tmp_path, base_url)
    sink = build_route_sink(config)
    assert sink is not None

    svc = service.JeevesService(
        config, audio_format=FMT,
        detector=ScriptedWakeDetector([10.0], audio_format=FMT),
        stt_backend=StubStt("Jeeves, tell peerbox the compressor is leaking"),
        route_sink=sink,
        provenance={"synthetic": True},
    )
    outcomes = await svc.run(_stream())

    assert len(outcomes) == 1
    assert outcomes[0].verb == cues.CUE_ROUTE
    assert outcomes[0].disposition == service.DISPOSITION_ROUTED

    notes = list((receiver.app["_vault"] / "note").glob("*.md"))
    assert len(notes) == 1
    post = frontmatter.load(notes[0])
    assert "the compressor is leaking" in post.content
    assert post["captured_via"] == "jeeves"
    assert post["capture_verb"] == cues.CUE_ROUTE
    assert post["lookback_used_seconds"] == pytest.approx(5.0)
    assert post["capture_multi_speaker"] is True

    # ...and it did NOT also land in the local log — a routed capture goes
    # to exactly one place.
    assert marklog.read_entries(str(tmp_path / "marks.jsonl")) == []
    rows = telemetry.read_rows(str(tmp_path / "telemetry.jsonl"))
    assert [r["event"] for r in rows] == [telemetry.EVENT_CAPTURE_ROUTED]


async def test_a_mark_cue_never_reaches_the_peer_even_with_a_sink_armed(
    receiver, tmp_path,
):
    """The MARK/ROUTE asymmetry, end to end. MARK-DOWN's whole cost model
    rests on it staying local."""
    base_url = str(receiver.make_url("")).rstrip("/")
    config = _device_config(tmp_path, base_url)
    svc = service.JeevesService(
        config, audio_format=FMT,
        detector=ScriptedWakeDetector([10.0], audio_format=FMT),
        stt_backend=StubStt("Jeeves, note that"),
        route_sink=build_route_sink(config),
        provenance={"synthetic": True},
    )
    outcomes = await svc.run(_stream())

    assert outcomes[0].disposition == service.DISPOSITION_MARKED
    assert list((receiver.app["_vault"] / "note").glob("*.md")) == []
    assert len(marklog.read_entries(str(tmp_path / "marks.jsonl"))) == 1


async def test_a_receiver_in_synthetic_mode_refuses_and_the_device_keeps_it_local(
    aiohttp_client, tmp_path,
):
    """The realistic misconfiguration: the device is deployed and live, the
    receiver was never flipped. Nothing is lost — the transcript is in the
    local log and the operator can move it by hand."""
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    vault = tmp_path / "vault"
    (vault / "note").mkdir(parents=True)
    register_vault_path(app, vault)
    register_jeeves_routes(
        app, enabled=True, instance_name="TestBox", jeeves_raw_config={},
    )
    receiver = await aiohttp_client(app)

    base_url = str(receiver.make_url("")).rstrip("/")
    config = _device_config(tmp_path, base_url)
    svc = service.JeevesService(
        config, audio_format=FMT,
        detector=ScriptedWakeDetector([10.0], audio_format=FMT),
        stt_backend=StubStt("Jeeves, tell peerbox about the compressor"),
        route_sink=build_route_sink(config),
        provenance={"synthetic": True},
    )
    outcomes = await svc.run(_stream())

    assert outcomes[0].verb == cues.CUE_ROUTE
    assert outcomes[0].disposition == service.DISPOSITION_MARKED
    assert outcomes[0].reason == "route_send_failed"
    assert list((vault / "note").glob("*.md")) == []

    entries = marklog.read_entries(str(tmp_path / "marks.jsonl"))
    assert len(entries) == 1
    assert entries[0]["provenance"]["route_failed"] is True
