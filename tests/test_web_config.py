"""Tests for ``alfred.web.config`` — the ``web:`` section loader.

Covers: disabled-by-default, ``${VAR}`` substitution, the schema-tolerance
filter (unknown keys dropped), nameless/malformed user dropping, and the
hand-rolled nested ``auth`` / ``email`` construction that deliberately
sidesteps the shared ``_build`` collision footgun.
"""

from __future__ import annotations

import pytest

from alfred.web.config import (
    VoiceIceConfig,
    WebVoiceSttConfig,
    WebVoiceTtsConfig,
    WebAuthConfig,
    WebConfig,
    WebEmailConfig,
    WebUser,
    WebVoiceConfig,
    _is_unresolved,
    load_from_unified,
    resolve_signing_secret,
)


def test_absent_web_block_is_disabled_default() -> None:
    cfg = load_from_unified({})
    assert isinstance(cfg, WebConfig)
    assert cfg.enabled is False
    assert cfg.users == []
    # Nested blocks default to their own dataclass defaults.
    assert isinstance(cfg.auth, WebAuthConfig)
    assert isinstance(cfg.email, WebEmailConfig)
    assert cfg.auth.session_ttl_hours == 168
    assert cfg.email.provider == "resend"
    # Tool-scoped default nonce-store path.
    assert cfg.state_path == "./data/web_auth_state.json"


def test_state_path_override() -> None:
    cfg = load_from_unified(
        {"web": {"enabled": True, "state_path": "./data/custom_web_nonces.json"}}
    )
    assert cfg.state_path == "./data/custom_web_nonces.json"


def test_non_dict_web_section_is_tolerated() -> None:
    # A scalar / list in the ``web`` slot must not crash the loader.
    cfg = load_from_unified({"web": "nonsense"})
    assert cfg.enabled is False
    assert cfg.users == []


def test_basic_users_and_roles() -> None:
    cfg = load_from_unified(
        {
            "web": {
                "enabled": True,
                "users": [
                    {"name": "andrew", "role": "owner", "email": "a@example.com"},
                    {"name": "ben", "role": "ops", "email": "b@example.com"},
                ],
            }
        }
    )
    assert cfg.enabled is True
    assert cfg.users == [
        WebUser(name="andrew", role="owner", email="a@example.com"),
        WebUser(name="ben", role="ops", email="b@example.com"),
    ]


def test_role_defaults_to_owner_when_omitted() -> None:
    cfg = load_from_unified(
        {"web": {"enabled": True, "users": [{"name": "andrew"}]}}
    )
    assert cfg.users[0].role == "owner"
    assert cfg.users[0].email == ""


def test_nameless_and_malformed_users_dropped() -> None:
    cfg = load_from_unified(
        {
            "web": {
                "enabled": True,
                "users": [
                    {"role": "owner"},          # no name → dropped
                    {"name": "   "},            # blank name → dropped
                    "not-a-dict",               # non-dict → dropped
                    {"name": "real", "role": "ops"},
                ],
            }
        }
    )
    assert [u.name for u in cfg.users] == ["real"]


def test_users_not_a_list_yields_empty() -> None:
    cfg = load_from_unified(
        {"web": {"enabled": True, "users": {"name": "andrew"}}}
    )
    assert cfg.users == []


def test_env_substitution(monkeypatch) -> None:
    monkeypatch.setenv("TEST_WEB_SECRET", "s3cr3t-from-env")
    monkeypatch.setenv("TEST_WEB_BASE", "https://salem.example.com")
    monkeypatch.setenv("TEST_RESEND_KEY", "DUMMY_RESEND_TEST_KEY")
    cfg = load_from_unified(
        {
            "web": {
                "enabled": True,
                "users": [{"name": "andrew"}],
                "auth": {
                    "session_secret": "${TEST_WEB_SECRET}",
                    "base_url": "${TEST_WEB_BASE}",
                },
                "email": {"api_key": "${TEST_RESEND_KEY}"},
            }
        }
    )
    assert cfg.auth.session_secret == "s3cr3t-from-env"
    assert cfg.auth.base_url == "https://salem.example.com"
    assert cfg.email.api_key == "DUMMY_RESEND_TEST_KEY"


def test_unset_env_var_left_literal() -> None:
    # An unset ${VAR} stays as its literal text (visible-missing, not blank).
    cfg = load_from_unified(
        {"web": {"enabled": True, "auth": {"session_secret": "${DEFINITELY_UNSET_WEB_VAR}"}}}
    )
    assert cfg.auth.session_secret == "${DEFINITELY_UNSET_WEB_VAR}"


def test_emptied_env_var_resolves_to_literal(monkeypatch) -> None:
    """An env var set to "" coalesces to the literal ${VAR} (canonical _env).

    This is the reconciled semantic the WARN fix brought in: an operator
    who EMPTIES the secret to break auth gets the same fail-loud-able
    literal placeholder as one who never set it (NOT a silent empty string).
    """
    monkeypatch.setenv("TEST_WEB_EMPTY_SECRET", "")
    cfg = load_from_unified(
        {"web": {"enabled": True, "auth": {"session_secret": "${TEST_WEB_EMPTY_SECRET}"}}}
    )
    assert cfg.auth.session_secret == "${TEST_WEB_EMPTY_SECRET}"


def test_is_unresolved_predicate() -> None:
    assert _is_unresolved("") is True
    assert _is_unresolved(None) is True
    assert _is_unresolved("${ALFRED_WEB_SESSION_SECRET}") is True
    assert _is_unresolved("a-real-secret") is False


def test_resolve_signing_secret_returns_valid() -> None:
    auth = WebAuthConfig(session_secret="a-strong-random-secret")
    assert resolve_signing_secret(auth) == "a-strong-random-secret"


def test_resolve_signing_secret_fails_loud_on_empty() -> None:
    with pytest.raises(ValueError, match="session_secret"):
        resolve_signing_secret(WebAuthConfig(session_secret=""))


def test_resolve_signing_secret_fails_loud_on_unresolved_placeholder() -> None:
    # An emptied/absent ALFRED_WEB_SESSION_SECRET arrives here as a literal
    # ${...} placeholder — MUST trip the guard, never HMAC-sign with it.
    with pytest.raises(ValueError, match="unresolved"):
        resolve_signing_secret(
            WebAuthConfig(session_secret="${ALFRED_WEB_SESSION_SECRET}")
        )


def test_auth_email_schema_tolerance_unknown_keys_dropped() -> None:
    # Hand-rolled construction must drop unknown nested keys, not crash.
    cfg = load_from_unified(
        {
            "web": {
                "enabled": True,
                "auth": {
                    "session_secret": "x",
                    "session_ttl_hours": 24,
                    "future_unknown_field": "ignored",
                },
                "email": {
                    "provider": "resend",
                    "api_key": "k",
                    "from_address": "f@e.com",
                    "another_future_field": 123,
                },
            }
        }
    )
    assert cfg.auth.session_secret == "x"
    assert cfg.auth.session_ttl_hours == 24
    assert cfg.email.api_key == "k"
    assert cfg.email.from_address == "f@e.com"
    assert not hasattr(cfg.auth, "future_unknown_field")


def test_state_key_in_web_block_does_not_misdispatch() -> None:
    """The ``_build`` collision footgun is sidestepped by hand-rolling.

    ``state`` is a key mapped to other dataclasses in sibling config
    modules' ``_DATACLASS_MAP``. A stray ``state`` key under ``web`` must
    be harmlessly ignored — never built into a foreign dataclass.
    """
    cfg = load_from_unified(
        {
            "web": {
                "enabled": True,
                "users": [{"name": "andrew"}],
                "state": {"path": "./data/should_be_ignored.json"},
                "auth": {"session_secret": "x"},
            }
        }
    )
    assert cfg.enabled is True
    assert cfg.auth.session_secret == "x"
    assert not hasattr(cfg, "state")


def test_int_coercion_for_ttl_fields() -> None:
    cfg = load_from_unified(
        {
            "web": {
                "enabled": True,
                "auth": {
                    "session_ttl_hours": "72",        # str → int
                    "magic_link_ttl_minutes": "bad",  # invalid → default
                },
            }
        }
    )
    assert cfg.auth.session_ttl_hours == 72
    assert cfg.auth.magic_link_ttl_minutes == 15  # default fallback


# ---------------------------------------------------------------------------
# auth.mode (cross-instance chat: session vs relay)
# ---------------------------------------------------------------------------


def test_auth_mode_defaults_to_session() -> None:
    cfg = load_from_unified({"web": {"enabled": True}})
    assert cfg.auth.mode == "session"


def test_auth_mode_relay_loads() -> None:
    cfg = load_from_unified(
        {"web": {"enabled": True, "auth": {"mode": "relay"}}}
    )
    assert cfg.auth.mode == "relay"


def test_auth_mode_is_case_insensitive() -> None:
    cfg = load_from_unified(
        {"web": {"enabled": True, "auth": {"mode": "RELAY"}}}
    )
    assert cfg.auth.mode == "relay"


def test_auth_mode_unknown_coalesces_to_session() -> None:
    # A typo must fail closed to the secret-requiring default, never serve
    # an unverified surface under an unknown mode.
    cfg = load_from_unified(
        {"web": {"enabled": True, "auth": {"mode": "relayed"}}}
    )
    assert cfg.auth.mode == "session"


def test_auth_mode_relay_does_not_require_secret() -> None:
    # Relay mode carries no session_secret — that is valid (no token
    # minting). The loader must not invent one or fail.
    cfg = load_from_unified(
        {
            "web": {
                "enabled": True,
                "auth": {"mode": "relay"},
                "users": [{"name": "andrew", "role": "owner"}],
            }
        }
    )
    assert cfg.auth.mode == "relay"
    assert cfg.auth.session_secret == ""
    assert cfg.enabled is True


# ---------------------------------------------------------------------------
# web.voice block (V0 WebRTC voice)
# ---------------------------------------------------------------------------


def test_voice_absent_is_disabled_default() -> None:
    # An absent voice block → all-default (disabled) WebVoiceConfig, so the
    # /voice/* routes stay unmounted (byte-identical route table).
    cfg = load_from_unified({"web": {"enabled": True}})
    assert isinstance(cfg.voice, WebVoiceConfig)
    assert cfg.voice.enabled is False
    assert cfg.voice.max_sessions == 2
    assert cfg.voice.pipeline == "echo"
    assert cfg.voice.offer_timeout_seconds == 10
    assert cfg.voice.reaper_interval_seconds == 15
    assert isinstance(cfg.voice.ice, VoiceIceConfig)
    assert cfg.voice.ice.stun_servers == []


def test_voice_full_block_loads() -> None:
    cfg = load_from_unified(
        {
            "web": {
                "enabled": True,
                "voice": {
                    "enabled": True,
                    "max_sessions": 4,
                    "pipeline": "echo",
                    "offer_timeout_seconds": 12,
                    "connect_deadline_seconds": 25,
                    "idle_timeout_seconds": 90,
                    "max_session_seconds": 900,
                    "reaper_interval_seconds": 20,
                    "ice": {
                        "advertised_ip": "203.0.113.9",
                        "stun_servers": ["stun:stun.l.google.com:19302"],
                        "udp_port_range": "40000-40100",
                    },
                },
            }
        }
    )
    v = cfg.voice
    assert v.enabled is True
    assert v.max_sessions == 4
    assert v.offer_timeout_seconds == 12
    assert v.connect_deadline_seconds == 25
    assert v.idle_timeout_seconds == 90
    assert v.max_session_seconds == 900
    assert v.reaper_interval_seconds == 20
    assert v.ice.advertised_ip == "203.0.113.9"
    assert v.ice.stun_servers == ["stun:stun.l.google.com:19302"]
    assert v.ice.udp_port_range == "40000-40100"


def test_voice_int_coercion_and_schema_tolerance() -> None:
    # String ints coerce; a bad int falls back to the default; an unknown key
    # is dropped (schema-tolerance).
    cfg = load_from_unified(
        {
            "web": {
                "enabled": True,
                "voice": {
                    "enabled": True,
                    "max_sessions": "3",          # str → 3
                    "offer_timeout_seconds": "oops",  # bad → default 10
                    "future_knob": "ignored",     # unknown → dropped
                },
            }
        }
    )
    assert cfg.voice.max_sessions == 3
    assert cfg.voice.offer_timeout_seconds == 10


def test_voice_stun_servers_drops_non_str_entries() -> None:
    cfg = load_from_unified(
        {
            "web": {
                "enabled": True,
                "voice": {"ice": {"stun_servers": ["stun:a:1", 42, "", "  stun:b:2 "]}},
            }
        }
    )
    # Non-str / blank dropped; surviving entries stripped.
    assert cfg.voice.ice.stun_servers == ["stun:a:1", "stun:b:2"]


def test_voice_non_dict_block_tolerated() -> None:
    cfg = load_from_unified({"web": {"enabled": True, "voice": "nonsense"}})
    assert cfg.voice.enabled is False  # defaults, no crash


# ---------------------------------------------------------------------------
# web.voice.stt block (V1 assistant pipeline)
# ---------------------------------------------------------------------------


def test_voice_stt_absent_is_defaults() -> None:
    cfg = load_from_unified({"web": {"enabled": True, "voice": {"enabled": True}}})
    assert isinstance(cfg.voice.stt, WebVoiceSttConfig)
    assert cfg.voice.stt.provider == ""       # unconfigured (mount gate rejects)
    assert cfg.voice.stt.model == "nova-3"
    assert cfg.voice.stt.smart_format is True
    assert cfg.voice.no_speech_close_s == 600


def test_voice_stt_full_block_and_provider_lowercased() -> None:
    cfg = load_from_unified({
        "web": {"enabled": True, "voice": {
            "enabled": True, "pipeline": "assistant", "no_speech_close_s": 300,
            "stt": {
                "provider": "Deepgram", "api_key": "k", "model": "nova-3",
                "language": "en", "sample_rate": 16000, "endpointing_ms": 250,
                "utterance_end_ms": 1200, "min_utterance_chars": 4,
                "smart_format": False,
            },
        }},
    })
    stt = cfg.voice.stt
    assert stt.provider == "deepgram"        # stripped + lowercased
    assert stt.api_key == "k"
    assert stt.endpointing_ms == 250
    assert stt.utterance_end_ms == 1200
    assert stt.min_utterance_chars == 4
    assert stt.smart_format is False
    assert cfg.voice.no_speech_close_s == 300


def test_voice_stt_schema_tolerance_and_int_coercion() -> None:
    cfg = load_from_unified({
        "web": {"enabled": True, "voice": {"stt": {
            "provider": "deepgram", "endpointing_ms": "250", "future_knob": "ignored",
        }}},
    })
    assert cfg.voice.stt.endpointing_ms == 250  # str → int
    assert cfg.voice.stt.provider == "deepgram"


def test_voice_stt_api_key_env_substitution(monkeypatch) -> None:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-secret-xyz")
    cfg = load_from_unified({
        "web": {"enabled": True, "voice": {"stt": {
            "provider": "deepgram", "api_key": "${DEEPGRAM_API_KEY}",
        }}},
    })
    assert cfg.voice.stt.api_key == "dg-secret-xyz"


def test_voice_stt_unresolved_key_stays_placeholder() -> None:
    # No env → the placeholder survives so _is_unresolved trips the mount gate.
    cfg = load_from_unified({
        "web": {"enabled": True, "voice": {"stt": {
            "provider": "deepgram", "api_key": "${DEEPGRAM_API_KEY}",
        }}},
    })
    assert _is_unresolved(cfg.voice.stt.api_key)


# ---------------------------------------------------------------------------
# web.voice.tts block (V2 talk-back)
# ---------------------------------------------------------------------------


def test_voice_tts_absent_is_defaults() -> None:
    cfg = load_from_unified({"web": {"enabled": True, "voice": {"enabled": True}}})
    assert isinstance(cfg.voice.tts, WebVoiceTtsConfig)
    assert cfg.voice.tts.enabled is False        # default-off → V1 byte-identical
    assert cfg.voice.tts.provider == ""
    assert cfg.voice.tts.model == "eleven_flash_v2_5"
    assert cfg.voice.tts.output_format == "pcm_24000"
    assert cfg.voice.tts.max_tts_chars_per_turn == 4096


def test_voice_tts_full_block_provider_and_format_lowercased() -> None:
    cfg = load_from_unified({
        "web": {"enabled": True, "voice": {"tts": {
            "enabled": True, "provider": "ElevenLabs", "api_key": "k",
            "voice": "Rachel", "output_format": "PCM_24000", "auto_mode": False,
            "max_tts_chars_per_turn": 2000, "max_buffer_seconds": 20,
            "inactivity_timeout_s": 60, "zero_retention": True,
        }}},
    })
    tts = cfg.voice.tts
    assert tts.enabled is True
    assert tts.provider == "elevenlabs"          # stripped + lowercased
    assert tts.output_format == "pcm_24000"      # lowercased
    assert tts.auto_mode is False
    assert tts.max_tts_chars_per_turn == 2000
    assert tts.zero_retention is True


def test_voice_tts_schema_tolerance_and_int_coercion() -> None:
    cfg = load_from_unified({
        "web": {"enabled": True, "voice": {"tts": {
            "provider": "elevenlabs", "max_buffer_seconds": "25", "future_knob": "x",
        }}},
    })
    assert cfg.voice.tts.max_buffer_seconds == 25   # str → int
    assert cfg.voice.tts.provider == "elevenlabs"


def test_voice_tts_api_key_env_substitution(monkeypatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-secret-xyz")
    cfg = load_from_unified({
        "web": {"enabled": True, "voice": {"tts": {
            "provider": "elevenlabs", "api_key": "${ELEVENLABS_API_KEY}",
        }}},
    })
    assert cfg.voice.tts.api_key == "el-secret-xyz"


def test_voice_tts_unresolved_key_stays_placeholder(monkeypatch) -> None:
    # Pin the env absent — another suite (test_per_tool_telemetry) sets
    # ELEVENLABS_API_KEY, which would bleed and resolve the placeholder in a
    # full run (dispatcher env-var hygiene contract).
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    cfg = load_from_unified({
        "web": {"enabled": True, "voice": {"tts": {
            "provider": "elevenlabs", "api_key": "${ELEVENLABS_API_KEY}",
        }}},
    })
    assert _is_unresolved(cfg.voice.tts.api_key)


# ---------------------------------------------------------------------------
# web.voice.tts.barge_in block (V3)
# ---------------------------------------------------------------------------


def test_barge_in_absent_is_defaults() -> None:
    cfg = load_from_unified({"web": {"enabled": True, "voice": {"tts": {}}}})
    b = cfg.voice.tts.barge_in
    assert b.enabled is False and b.too_early_ms == 700 and b.echo_threshold == 0.8
    assert b.min_words == 2 and b.echo_grace_s == 2.0


def test_barge_in_full_block_and_coercions() -> None:
    cfg = load_from_unified({"web": {"enabled": True, "voice": {"tts": {"barge_in": {
        "enabled": True, "too_early_ms": "500", "min_words": 3, "min_chars": 8,
        "echo_threshold": "0.7", "echo_grace_s": 1.5,
        "interrupt_extra": ["abort", 5], "backchannel_extra": ["cool"],
    }}}}})
    b = cfg.voice.tts.barge_in
    assert b.enabled is True and b.too_early_ms == 500      # str→int
    assert b.echo_threshold == 0.7                          # str→float
    assert b.interrupt_extra == ["abort"]                   # non-str dropped
    assert b.backchannel_extra == ["cool"]


def test_barge_in_schema_tolerance() -> None:
    cfg = load_from_unified({"web": {"enabled": True, "voice": {"tts": {"barge_in": {
        "enabled": True, "future_knob": "x",
    }}}}})
    assert cfg.voice.tts.barge_in.enabled is True
