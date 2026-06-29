"""Tests for ``alfred.web.routes_stt`` — web STT over HTTP (2026-06-29).

``POST /stt/transcribe`` reuses the live STT fallback chain
(``stt_backends.build_chain`` + ``transcribe_with_fallback``) and maps the
SttResult / NoTranscript outcomes to the CONTRACT §4 response. Auth is
two-layer (peer token + ``X-Alfred-Session``), identical to /chat/*.

The transcribe call is monkeypatched throughout so NO network / engine
calls happen — these tests exercise the route's gating, streaming size
cap, mime allowlist, and the outcome→response mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import structlog

from alfred.telegram import stt_backends
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
from alfred.web import routes_stt
from alfred.web.auth import SESSION_HEADER, make_session_token
from alfred.web.config import WebAuthConfig, WebConfig, WebUser
from alfred.web.routes_chat import register_web_routes
from alfred.web.state import WebAuthState

from tests.telegram.conftest import FakeAnthropicClient

# Obviously-fake test secrets — never a real provider prefix.
DUMMY_WEB_PEER_TOKEN = "DUMMY_WEB_PEER_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_0123456"
DUMMY_WEB_SIGNING_SECRET = "DUMMY_WEB_SIGNING_SECRET_FOR_TESTING_ONLY_0123456789"

_PEER_HEADERS = {
    "Authorization": f"Bearer {DUMMY_WEB_PEER_TOKEN}",
    "X-Alfred-Client": "web",
}


def _audio_headers(mime: str = "audio/webm") -> dict[str, str]:
    token = make_session_token(
        "andrew", "owner", secret=DUMMY_WEB_SIGNING_SECRET, ttl_hours=168
    )
    return {**_PEER_HEADERS, SESSION_HEADER: token, "Content-Type": mime}


@dataclass
class _FakeBackend:
    backend_id: str


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
    )


@pytest.fixture
async def stt_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
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


def _patch_chain(monkeypatch, *, served=None, first_backend="groq-whisper", raises=None):
    """Patch build_chain + transcribe_with_fallback to a deterministic outcome."""
    monkeypatch.setattr(
        stt_backends, "build_chain",
        lambda cfg: [_FakeBackend(first_backend), _FakeBackend("deepgram")],
    )

    async def _fake_transcribe(audio, mime, chain, vocab, budget):
        if raises is not None:
            raise raises
        return served

    monkeypatch.setattr(stt_backends, "transcribe_with_fallback", _fake_transcribe)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_stt_route_mounted_when_web_enabled(stt_client) -> None:
    paths = [
        r.resource.canonical
        for r in stt_client.app.router.routes()
        if r.resource is not None
    ]
    assert "/stt/transcribe" in paths


# ---------------------------------------------------------------------------
# Auth gates
# ---------------------------------------------------------------------------


async def test_stt_requires_peer_token(stt_client) -> None:
    resp = await stt_client.post(
        "/stt/transcribe", data=b"abc", headers={"Content-Type": "audio/webm"}
    )
    assert resp.status == 401


async def test_stt_requires_session(stt_client) -> None:
    resp = await stt_client.post(
        "/stt/transcribe",
        data=b"abc",
        headers={**_PEER_HEADERS, "Content-Type": "audio/webm"},
    )
    assert resp.status == 401
    assert (await resp.json())["error"] == "invalid_session"


# ---------------------------------------------------------------------------
# Mime + size + empty edge guards
# ---------------------------------------------------------------------------


async def test_stt_unsupported_media_type(stt_client) -> None:
    resp = await stt_client.post(
        "/stt/transcribe", data=b"abc", headers=_audio_headers("text/plain")
    )
    assert resp.status == 415
    assert (await resp.json())["error"] == "unsupported_media_type"


async def test_stt_mime_with_codecs_param_accepted(stt_client, monkeypatch) -> None:
    _patch_chain(
        monkeypatch,
        served=stt_backends.SttResult(
            text="hello world", backend_id="groq-whisper", tier="comparable",
        ),
    )
    resp = await stt_client.post(
        "/stt/transcribe",
        data=b"audio-bytes",
        headers=_audio_headers("audio/webm;codecs=opus"),
    )
    assert resp.status == 200
    assert (await resp.json())["transcript"] == "hello world"


async def test_stt_no_audio(stt_client) -> None:
    resp = await stt_client.post(
        "/stt/transcribe", data=b"", headers=_audio_headers("audio/webm")
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "no_audio"


async def test_stt_audio_too_large(stt_client, monkeypatch) -> None:
    monkeypatch.setattr(routes_stt, "MAX_AUDIO_BYTES", 50)
    resp = await stt_client.post(
        "/stt/transcribe", data=b"x" * 200, headers=_audio_headers("audio/webm")
    )
    assert resp.status == 413
    assert (await resp.json())["error"] == "audio_too_large"


async def test_stt_streams_past_1mb_client_max_size(stt_client, monkeypatch) -> None:
    """REGRESSION PIN — the handler must STREAM the body via
    request.content.iter_chunked, NOT request.read()/post()/multipart(),
    which enforce the shared transport app's default 1 MB client_max_size.

    This sends a >1 MB audio body (1 MB + 4 KB) with MAX_AUDIO_BYTES at its
    real 25 MB default (NOT patched down) and asserts the handler ACCEPTS
    it (200, not 413). A regression swapping the streaming read for
    request.read() would 413 here (and 413 every real voice note >1 MB)
    while passing every other test in this file — exactly the spec's #1
    backend gotcha. test_stt_audio_too_large above only sends 200 bytes
    under a monkeypatched 50-byte cap, so it never crosses the 1 MB
    client_max_size and does NOT cover this surface."""
    _patch_chain(
        monkeypatch,
        served=stt_backends.SttResult(
            text="transcript from a large note",
            backend_id="groq-whisper",
            tier="comparable",
        ),
    )
    big_audio = b"\x00" * (1024 * 1024 + 4096)  # >1 MB, under the 25 MB cap
    resp = await stt_client.post(
        "/stt/transcribe", data=big_audio, headers=_audio_headers("audio/webm")
    )
    assert resp.status == 200, (
        "handler 413'd a >1MB body — the streaming iter_chunked read was "
        "likely swapped for request.read()/post(), which enforces the "
        "app's 1MB client_max_size"
    )
    assert (await resp.json())["transcript"] == "transcript from a large note"


# ---------------------------------------------------------------------------
# Outcome mapping (CONTRACT §4)
# ---------------------------------------------------------------------------


async def test_stt_served_non_empty_first_backend(stt_client, monkeypatch) -> None:
    _patch_chain(
        monkeypatch,
        served=stt_backends.SttResult(
            text="a clean transcript", backend_id="groq-whisper", tier="comparable",
        ),
    )
    with structlog.testing.capture_logs() as captured:
        resp = await stt_client.post(
            "/stt/transcribe", data=b"audio", headers=_audio_headers()
        )
    assert resp.status == 200
    body = await resp.json()
    assert body["transcript"] == "a clean transcript"
    assert body["backend_used"] == "groq-whisper"
    assert body["fell_back"] is False
    assert body["tier"] == "comparable"
    assert body["low_confidence"] is False
    served = [c for c in captured if c.get("event") == "web.stt.transcribed"]
    assert len(served) == 1
    assert served[0]["backend_used"] == "groq-whisper"
    assert served[0]["fell_back"] is False
    assert served[0]["low_confidence"] is False


async def test_stt_served_fell_back_marks_low_confidence(stt_client, monkeypatch) -> None:
    # Served by deepgram while chain[0] is groq-whisper → fell_back True.
    _patch_chain(
        monkeypatch,
        served=stt_backends.SttResult(
            text="fallback transcript", backend_id="deepgram", tier="comparable",
        ),
    )
    resp = await stt_client.post(
        "/stt/transcribe", data=b"audio", headers=_audio_headers()
    )
    body = await resp.json()
    assert body["fell_back"] is True
    assert body["low_confidence"] is True


async def test_stt_served_empty_returns_signal(stt_client, monkeypatch) -> None:
    _patch_chain(
        monkeypatch,
        served=stt_backends.SttResult(
            text="   ", backend_id="groq-whisper", tier="comparable",
        ),
    )
    with structlog.testing.capture_logs() as captured:
        resp = await stt_client.post(
            "/stt/transcribe", data=b"audio", headers=_audio_headers()
        )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"transcript": "", "empty": True, "low_confidence": True}
    empties = [c for c in captured if c.get("event") == "web.stt.empty"]
    assert len(empties) == 1
    assert empties[0]["reason"] == "served_empty"


async def test_stt_degraded_returns_signal(stt_client, monkeypatch) -> None:
    _patch_chain(monkeypatch, served=stt_backends.NoTranscript(reason="degraded"))
    with structlog.testing.capture_logs() as captured:
        resp = await stt_client.post(
            "/stt/transcribe", data=b"audio", headers=_audio_headers()
        )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"transcript": "", "degraded": True, "low_confidence": True}
    empties = [c for c in captured if c.get("event") == "web.stt.empty"]
    assert len(empties) == 1
    assert empties[0]["reason"] == "degraded"


async def test_stt_all_failed_returns_502(stt_client, monkeypatch) -> None:
    _patch_chain(monkeypatch, served=stt_backends.NoTranscript(reason="all_failed"))
    with structlog.testing.capture_logs() as captured:
        resp = await stt_client.post(
            "/stt/transcribe", data=b"audio", headers=_audio_headers()
        )
    assert resp.status == 502
    assert (await resp.json())["error"] == "stt_failed"
    failed = [c for c in captured if c.get("event") == "web.stt.failed"]
    assert len(failed) == 1
    assert failed[0]["reason"] == "all_failed"


async def test_stt_engine_exception_returns_502(stt_client, monkeypatch) -> None:
    _patch_chain(monkeypatch, raises=RuntimeError("boom"))
    resp = await stt_client.post(
        "/stt/transcribe", data=b"audio", headers=_audio_headers()
    )
    assert resp.status == 502
    assert (await resp.json())["error"] == "stt_failed"
