"""Config default-OFF + the ``_build_stt_shadow`` fail-closed gate.

The gate degrades to no-shadow (never unmounts STT, never mounts a shadow that
would 100%-error) and always logs a ``shadow_disabled``/``shadow_enabled`` with
the reason (log-emission pinned per feedback_log_emission_test_pattern).
"""

from __future__ import annotations

from types import SimpleNamespace

import structlog

from alfred.telegram.config import STTConfig
from alfred.web.config import WebSttShadowCaptureConfig, _build_voice_stt
from alfred.web.routes_voice import _build_stt_shadow
from alfred.web.voice_stt_shadow import VoiceSttShadow

_STT_NORM = SimpleNamespace(sample_rate=16000)


def _voice(enabled: bool, dir_="./data/stt_corpus"):
    stt = _build_voice_stt(
        {"provider": "deepgram",
         "shadow_capture": {"enabled": enabled, "dir": dir_}})
    return SimpleNamespace(stt=stt)


def _talker(api_key="DUMMY_GROQ_TEST_KEY", vocab=("RRTS", "Fergus")):
    return SimpleNamespace(
        stt=STTConfig(api_key=api_key, vocab_terms=list(vocab)),
        instance=SimpleNamespace(name="Salem"))


# --- config default-OFF ----------------------------------------------------


def test_shadow_config_defaults_off() -> None:
    stt = _build_voice_stt({"provider": "deepgram"})
    assert isinstance(stt.shadow_capture, WebSttShadowCaptureConfig)
    assert stt.shadow_capture.enabled is False
    assert stt.shadow_capture.dir == "./data/stt_corpus"


def test_shadow_config_enabled_and_dir_parsed() -> None:
    stt = _build_voice_stt(
        {"provider": "deepgram",
         "shadow_capture": {"enabled": True, "dir": "/data/corpus"}})
    assert stt.shadow_capture.enabled is True
    assert stt.shadow_capture.dir == "/data/corpus"


def test_shadow_config_schema_tolerant() -> None:
    # An unknown sub-key is ignored (forward-compat), not a crash.
    stt = _build_voice_stt(
        {"provider": "deepgram",
         "shadow_capture": {"enabled": True, "future_field": 7}})
    assert stt.shadow_capture.enabled is True


# --- gate: disabled ---------------------------------------------------------


def test_gate_disabled_returns_none_and_logs() -> None:
    with structlog.testing.capture_logs() as cap:
        factory = _build_stt_shadow(_voice(False), _talker(), _STT_NORM)
    assert factory is None
    ev = [c for c in cap if c.get("event") == "web.voice.stt.shadow_disabled"]
    assert len(ev) == 1 and ev[0]["reason"] == "not_enabled"


# --- gate: enabled + resolvable key → factory ------------------------------


def test_gate_enabled_returns_session_factory() -> None:
    with structlog.testing.capture_logs() as cap:
        factory = _build_stt_shadow(_voice(True), _talker(), _STT_NORM)
    assert callable(factory)
    shadow = factory("vid-42")
    assert isinstance(shadow, VoiceSttShadow)
    assert shadow._vocab == ["RRTS", "Fergus"]      # vocab reused from talker stt
    assert shadow._instance == "Salem"
    assert shadow._vid == "vid-42"
    en = [c for c in cap if c.get("event") == "web.voice.stt.shadow_enabled"]
    assert len(en) == 1 and en[0]["vocab_terms"] == 2 and en[0]["instance"] == "Salem"


# --- gate: fail-closed matrix ----------------------------------------------


def test_gate_no_talker_stt_fails_closed() -> None:
    tc = SimpleNamespace(stt=None, instance=SimpleNamespace(name="Salem"))
    with structlog.testing.capture_logs() as cap:
        factory = _build_stt_shadow(_voice(True), tc, _STT_NORM)
    assert factory is None
    ev = [c for c in cap if c.get("event") == "web.voice.stt.shadow_disabled"]
    assert ev and ev[0]["reason"] == "no_talker_stt"


def test_gate_empty_groq_key_fails_closed() -> None:
    with structlog.testing.capture_logs() as cap:
        factory = _build_stt_shadow(_voice(True), _talker(api_key=""), _STT_NORM)
    assert factory is None
    ev = [c for c in cap if c.get("event") == "web.voice.stt.shadow_disabled"]
    assert ev and ev[0]["reason"] == "groq_key_missing"


def test_gate_unresolved_groq_key_fails_closed() -> None:
    with structlog.testing.capture_logs() as cap:
        factory = _build_stt_shadow(
            _voice(True), _talker(api_key="${GROQ_API_KEY}"), _STT_NORM)
    assert factory is None
    ev = [c for c in cap if c.get("event") == "web.voice.stt.shadow_disabled"]
    assert ev and ev[0]["reason"] == "groq_key_missing"
