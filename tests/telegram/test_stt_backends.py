"""Regression pins for the STT fallback chain (M1).

Spec §13 subset — run UNCONDITIONALLY (the backends are mocked; there is
NO importorskip behind a deepgram dep, because the runtime uses httpx
directly, not the Deepgram SDK). Two layers:

  * Router pins (§4 matrix) — fake SttBackend stubs, no HTTP: empty-silence
    → SERVE (no re-spend); empty-with-speech → TRY_NEXT; Groq error → falls
    back to Deepgram; both-fail → NoTranscript; budget/order behaviour.
  * Backend pins — monkeypatch httpx.AsyncClient.post (the established
    telegram-test idiom): Groq verbose_json → has_speech_signal/avg_logprob;
    the M1-prerequisite empty→SttResult-not-raise migration; Deepgram sets
    punctuate+smart_format (output-shape parity §7); error classification.

asyncio_mode=auto (pyproject) → bare ``async def test_`` works.
"""

from __future__ import annotations

import httpx
import structlog

from alfred.telegram.config import STTConfig, SttBackendConfig
from alfred.telegram.stt_backends import (
    STT_ERR_AUTH,
    STT_ERR_NETWORK,
    STT_ERR_RATE_LIMIT,
    DeepgramBackend,
    GroqWhisperBackend,
    NoTranscript,
    SttError,
    SttResult,
    build_chain,
    transcribe_with_fallback,
)


# ---------------------------------------------------------------------------
# Fake backends — controllable SttBackend stubs for the router pins
# ---------------------------------------------------------------------------


class _FakeBackend:
    """A controllable SttBackend: returns a queued SttResult or raises a
    queued SttError. Records whether it was called (to assert no-re-spend)."""

    def __init__(
        self, backend_id, *, result=None, error=None, tier="comparable",
        never_skip=False, timeout_s=10.0,
    ):
        self.backend_id = backend_id
        self.tier = tier
        self.never_skip = never_skip
        self.timeout_s = timeout_s
        self._result = result
        self._error = error
        self.called = False

    async def transcribe(self, audio, mime, vocab):
        self.called = True
        if self._error is not None:
            raise self._error
        return self._result


def _r(text, backend_id="b", *, has_speech_signal=None, tier="comparable"):
    return SttResult(
        text=text, backend_id=backend_id, tier=tier,
        has_speech_signal=has_speech_signal,
    )


# ---------------------------------------------------------------------------
# Router §4 matrix pins (the load-bearing ones)
# ---------------------------------------------------------------------------


async def test_router_serves_primary_success_no_fallback():
    primary = _FakeBackend("groq", result=_r("hello world", "groq"))
    backup = _FakeBackend("deepgram", result=_r("should not run", "deepgram"))
    out = await transcribe_with_fallback(
        b"a", "audio/ogg", [primary, backup], [], 30.0,
    )
    assert isinstance(out, SttResult)
    assert out.text == "hello world"
    assert out.backend_id == "groq"
    assert backup.called is False  # primary served → no re-spend


async def test_router_empty_silence_serves_no_respend():
    """§4: empty-genuine-silence (has_speech_signal=False) from a primary →
    SERVE the empty, do NOT re-spend on the backup."""
    primary = _FakeBackend(
        "groq", result=_r("", "groq", has_speech_signal=False))
    backup = _FakeBackend("deepgram", result=_r("backup", "deepgram"))
    out = await transcribe_with_fallback(
        b"a", "audio/ogg", [primary, backup], [], 30.0,
    )
    assert isinstance(out, SttResult)
    assert out.text == ""
    assert out.backend_id == "groq"
    assert backup.called is False, "silence must not re-spend on the backup"


async def test_router_empty_with_speech_tries_next():
    """§4: empty-WITH-speech (has_speech_signal=True, text empty) from the
    primary → TRY_NEXT (a different decoder may succeed)."""
    primary = _FakeBackend(
        "groq", result=_r("", "groq", has_speech_signal=True))
    backup = _FakeBackend("deepgram", result=_r("recovered text", "deepgram"))
    out = await transcribe_with_fallback(
        b"a", "audio/ogg", [primary, backup], [], 30.0,
    )
    assert isinstance(out, SttResult)
    assert out.text == "recovered text"
    assert out.backend_id == "deepgram"
    assert backup.called is True, "decode-miss must try the next engine"


async def test_router_groq_error_falls_back_to_deepgram():
    """§4: a primary failure (429) → fall back; Deepgram serves."""
    primary = _FakeBackend(
        "groq", error=SttError(STT_ERR_RATE_LIMIT, "429", backend_id="groq"))
    backup = _FakeBackend("deepgram", result=_r("from deepgram", "deepgram"))
    out = await transcribe_with_fallback(
        b"a", "audio/ogg", [primary, backup], [], 30.0,
    )
    assert isinstance(out, SttResult)
    assert out.text == "from deepgram"
    assert out.backend_id == "deepgram"
    assert backup.called is True


async def test_router_both_fail_returns_no_transcript():
    """§4: all backends raise → NoTranscript(all_failed) → bot asks to type."""
    primary = _FakeBackend(
        "groq", error=SttError(STT_ERR_NETWORK, "down", backend_id="groq"))
    backup = _FakeBackend(
        "deepgram", error=SttError(STT_ERR_AUTH, "401", backend_id="deepgram"))
    out = await transcribe_with_fallback(
        b"a", "audio/ogg", [primary, backup], [], 30.0,
    )
    assert isinstance(out, NoTranscript)
    assert out.reason == "all_failed"


async def test_router_empty_at_chain_end_degrades():
    """§4: empty at chain-end (even with speech) → DEGRADE (never serve an
    empty string from the last link) → NoTranscript."""
    primary = _FakeBackend(
        "groq", error=SttError(STT_ERR_NETWORK, "down", backend_id="groq"))
    backup = _FakeBackend(
        "deepgram", result=_r("", "deepgram", has_speech_signal=True))
    out = await transcribe_with_fallback(
        b"a", "audio/ogg", [primary, backup], [], 30.0,
    )
    assert isinstance(out, NoTranscript)


async def test_router_empty_chain_returns_no_transcript():
    out = await transcribe_with_fallback(b"a", "audio/ogg", [], [], 30.0)
    assert isinstance(out, NoTranscript)


async def test_router_zero_budget_still_tries_last_backstop():
    """§3/§13: a blown global budget skips non-last backends (no stacked
    timeouts) but ALWAYS attempts the last link, then degrades. With
    total_budget_s=0 the (single) backend is last → still tried; an empty
    result at chain-end degrades to NoTranscript."""
    only = _FakeBackend(
        "groq", result=_r("", "groq", has_speech_signal=True))
    out = await transcribe_with_fallback(
        b"a", "audio/ogg", [only], [], 0.0,
    )
    # is_last → attempted despite zero budget; empty-at-end → degrade.
    assert only.called is True
    assert isinstance(out, NoTranscript)


async def test_router_zero_budget_skips_nonlast_backend():
    """With a blown budget, the FIRST (non-last) backend is skipped at zero
    call cost; only the last is attempted."""
    first = _FakeBackend("groq", result=_r("should be skipped", "groq"))
    last = _FakeBackend("deepgram", result=_r("served", "deepgram"))
    out = await transcribe_with_fallback(
        b"a", "audio/ogg", [first, last], [], 0.0,
    )
    assert first.called is False, "non-last backend skipped on blown budget"
    assert last.called is True
    assert isinstance(out, SttResult)
    assert out.text == "served"


async def test_router_logs_stt_transcribed_on_serve():
    """§6 ILB: a served result emits stt.transcribed with the per-call
    fields (backend_used, fell_back, tier, latency)."""
    primary = _FakeBackend(
        "groq", error=SttError(STT_ERR_RATE_LIMIT, "429", backend_id="groq"))
    backup = _FakeBackend("deepgram", result=_r("ok", "deepgram"))
    with structlog.testing.capture_logs() as cap:
        await transcribe_with_fallback(
            b"a", "audio/ogg", [primary, backup], [], 30.0)
    served = [c for c in cap if c.get("event") == "stt.transcribed"]
    assert len(served) == 1
    assert served[0]["backend_used"] == "deepgram"
    assert served[0]["fell_back"] is True
    assert served[0]["primary_failure"]["class"] == STT_ERR_RATE_LIMIT
    assert served[0]["tier"] == "comparable"


async def test_router_logs_stt_exhausted_on_all_fail():
    primary = _FakeBackend(
        "groq", error=SttError(STT_ERR_NETWORK, "x", backend_id="groq"))
    backup = _FakeBackend(
        "deepgram", error=SttError(STT_ERR_NETWORK, "y", backend_id="deepgram"))
    with structlog.testing.capture_logs() as cap:
        await transcribe_with_fallback(
            b"a", "audio/ogg", [primary, backup], [], 30.0)
    ex = [c for c in cap if c.get("event") == "stt.exhausted"]
    assert len(ex) == 1
    assert ex[0]["reason"] == "all_failed"


# ---------------------------------------------------------------------------
# GroqWhisperBackend pins (HTTP mocked) — incl. the M1-prerequisite migration
# ---------------------------------------------------------------------------


def _verbose_payload(text, *, no_speech_prob=0.01, avg_logprob=-0.2):
    return {
        "text": text,
        "segments": [
            {"no_speech_prob": no_speech_prob, "avg_logprob": avg_logprob},
        ],
    }


async def test_groq_verbose_json_parses_signals(monkeypatch):
    async def _fake_post(self, url, **kwargs):
        return httpx.Response(200, json=_verbose_payload("hello there"))
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    g = GroqWhisperBackend(api_key="k")
    res = await g.transcribe(b"a", "audio/ogg", [])
    assert res.text == "hello there"
    assert res.has_speech_signal is True          # no_speech_prob 0.01 < 0.5
    assert res.confidence_kind == "logprob"
    assert res.confidence_raw == -0.2


async def test_groq_empty_returns_result_not_raise(monkeypatch):
    """M1 PREREQUISITE pin (§12): an empty transcription RETURNS an empty
    SttResult (with has_speech_signal), it does NOT raise. Else the router's
    `except SttError` would mis-route empty as a failure and re-spend."""
    async def _fake_post(self, url, **kwargs):
        # silence: empty text + high no_speech_prob
        return httpx.Response(200, json=_verbose_payload(
            "", no_speech_prob=0.97, avg_logprob=-1.0))
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    g = GroqWhisperBackend(api_key="k")
    res = await g.transcribe(b"a", "audio/ogg", [])  # must NOT raise
    assert isinstance(res, SttResult)
    assert res.text == ""
    assert res.has_speech_signal is False          # genuine silence


async def test_groq_request_sets_verbose_json_and_vocab_prompt(monkeypatch):
    captured = {}

    async def _fake_post(self, url, **kwargs):
        captured["data"] = kwargs.get("data", {})
        return httpx.Response(200, json=_verbose_payload("x y z"))
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    g = GroqWhisperBackend(api_key="k")
    await g.transcribe(b"a", "audio/ogg", ["RRTS", "Fergus"])
    assert captured["data"]["response_format"] == "verbose_json"
    assert "RRTS" in captured["data"]["prompt"]
    assert "Fergus" in captured["data"]["prompt"]


async def test_groq_429_raises_rate_limit(monkeypatch):
    async def _fake_post(self, url, **kwargs):
        return httpx.Response(429, text="rate limit exceeded")
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    g = GroqWhisperBackend(api_key="k")
    try:
        await g.transcribe(b"a", "audio/ogg", [])
        assert False, "expected SttError"
    except SttError as exc:
        assert exc.error_class == STT_ERR_RATE_LIMIT


async def test_groq_401_raises_auth(monkeypatch):
    async def _fake_post(self, url, **kwargs):
        return httpx.Response(401, text="invalid api key")
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    g = GroqWhisperBackend(api_key="k")
    try:
        await g.transcribe(b"a", "audio/ogg", [])
        assert False, "expected SttError"
    except SttError as exc:
        assert exc.error_class == STT_ERR_AUTH


# ---------------------------------------------------------------------------
# DeepgramBackend pins (HTTP mocked) — output-shape parity §7
# ---------------------------------------------------------------------------


def _dg_payload(transcript, *, confidence=0.95):
    return {
        "results": {
            "channels": [
                {"alternatives": [
                    {"transcript": transcript, "confidence": confidence},
                ]},
            ],
        },
    }


async def test_deepgram_sets_punctuate_and_smart_format(monkeypatch):
    """§7 output-shape parity pin: the Deepgram request MUST carry
    punctuate=true + smart_format=true (an unconfigured Deepgram returns
    lowercase/unpunctuated text that breaks downstream matchers)."""
    captured = {}

    async def _fake_post(self, url, **kwargs):
        captured["params"] = kwargs.get("params", [])
        return httpx.Response(200, json=_dg_payload("Hello there."))
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    d = DeepgramBackend(api_key="k")
    res = await d.transcribe(b"a", "audio/ogg", ["RRTS"])
    params = dict(captured["params"])  # list of (k, v) tuples
    assert params["punctuate"] == "true"
    assert params["smart_format"] == "true"
    # vocab → keywords param
    kw = [v for (k, v) in captured["params"] if k == "keywords"]
    assert "RRTS" in kw
    assert res.text == "Hello there."
    assert res.confidence_kind == "probability"
    assert res.confidence_raw == 0.95


async def test_deepgram_empty_returns_result_not_raise(monkeypatch):
    async def _fake_post(self, url, **kwargs):
        return httpx.Response(200, json=_dg_payload(""))
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    d = DeepgramBackend(api_key="k")
    res = await d.transcribe(b"a", "audio/ogg", [])
    assert isinstance(res, SttResult)
    assert res.text == ""
    assert res.has_speech_signal is False


async def test_deepgram_429_raises_rate_limit(monkeypatch):
    async def _fake_post(self, url, **kwargs):
        return httpx.Response(429, text="too many requests")
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    d = DeepgramBackend(api_key="k")
    try:
        await d.transcribe(b"a", "audio/ogg", [])
        assert False, "expected SttError"
    except SttError as exc:
        assert exc.error_class == STT_ERR_RATE_LIMIT


# ---------------------------------------------------------------------------
# build_chain pins — config → backend instances
# ---------------------------------------------------------------------------


def test_build_chain_legacy_single_groq():
    """A legacy STTConfig (no chain) → a single Groq backend (back-compat)."""
    cfg = STTConfig(provider="groq", api_key="gk", model="whisper-large-v3")
    chain = build_chain(cfg)
    assert len(chain) == 1
    assert isinstance(chain[0], GroqWhisperBackend)
    assert chain[0].api_key == "gk"


def test_build_chain_two_backend():
    cfg = STTConfig(chain=[
        SttBackendConfig(backend="groq-whisper", api_key="gk"),
        SttBackendConfig(backend="deepgram", api_key="dk"),
    ])
    chain = build_chain(cfg)
    assert len(chain) == 2
    assert isinstance(chain[0], GroqWhisperBackend)
    assert isinstance(chain[1], DeepgramBackend)
    assert chain[1].punctuate is True and chain[1].smart_format is True


def test_build_chain_unknown_backend_skipped():
    cfg = STTConfig(chain=[
        SttBackendConfig(backend="groq-whisper", api_key="gk"),
        SttBackendConfig(backend="local-whisper", api_key=""),  # M4, not M1
    ])
    chain = build_chain(cfg)
    # local-whisper unknown in M1 → skipped (groq remains).
    assert len(chain) == 1
    assert isinstance(chain[0], GroqWhisperBackend)
