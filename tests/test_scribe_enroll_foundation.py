"""P4-5a foundation — config delta + the embed_voice provider seam.

Torch-free (fake seam). Covers the config schema change (enrollment_dir added,
enroll_token added + allowlist lockstep, enrollment_path dropped with a loud
deprecation warning) and the deterministic fake voice-embedding seam that makes the
whole enrollment/registry/match surface CI-testable.
"""

from __future__ import annotations

import math

import pytest
import structlog

from alfred.scribe import embed_voice
from alfred.scribe.config import (
    INGEST_WEB_ALLOWED_KEYS,
    ScribeDiarizeConfig,
    load_from_unified,
)
from alfred.scribe.embed_voice import (
    EMBED_DIM,
    SCRIBE_EMBED_PROVIDERS,
    EmbedError,
    embed_windows,
    engine_fingerprint,
)

_SALT = "DUMMY_SCRIBE_TEST_SALT"


def _cfg(*, provider="fake", enrollment_dir="", enroll_token="", diarize_extra=None):
    diarize = {"provider": provider, "enrollment_dir": enrollment_dir}
    if diarize_extra:
        diarize.update(diarize_extra)
    return load_from_unified({"scribe": {
        "mode": "synthetic", "encounter_salt": _SALT,
        "stt": {"provider": "fake"},
        "diarize": diarize,
        "ingest_web": {"enroll_token": enroll_token},
    }})


# ---------------------------------------------------------------------------
# Config delta
# ---------------------------------------------------------------------------

def test_enrollment_dir_loads_and_defaults_empty():
    assert _cfg().diarize.enrollment_dir == ""
    assert _cfg(enrollment_dir="/data/enroll").diarize.enrollment_dir == "/data/enroll"


def test_enroll_token_loads_and_defaults_empty():
    assert _cfg().ingest_web.enroll_token == ""
    assert _cfg(enroll_token="DUMMY_ENROLL_TOKEN").ingest_web.enroll_token == "DUMMY_ENROLL_TOKEN"


def test_enroll_token_in_allowlist_lockstep():
    # barrier-e: the field + the allowlist move together, else an enabled server
    # with enroll_token set would breach barrier-e as an unknown key.
    assert "enroll_token" in INGEST_WEB_ALLOWED_KEYS
    assert "enroll_token" in ScribeDiarizeConfig.__dataclass_fields__ or True  # (on ingest_web, checked below)
    from alfred.scribe.config import ScribeIngestWebConfig
    assert "enroll_token" in ScribeIngestWebConfig.__dataclass_fields__


def test_enrollment_path_dropped_from_schema():
    # The P4-4 single-path field is GONE (multi-preset supersedes it).
    assert "enrollment_path" not in ScribeDiarizeConfig.__dataclass_fields__


def test_enrollment_path_deprecation_warning_fires():
    # A stale enrollment_path under diarize → a loud deprecation event BEFORE the
    # schema-tolerance filter silently eats it (operator learns it's inert).
    with structlog.testing.capture_logs() as caps:
        load_from_unified({"scribe": {
            "encounter_salt": _SALT, "stt": {"provider": "fake"},
            "diarize": {"provider": "off", "enrollment_path": "/old/enroll.npy"},
        }})
    dep = [c for c in caps if c.get("event") == "scribe.config.enrollment_path_deprecated"]
    assert len(dep) == 1


def test_no_deprecation_warning_without_stale_key():
    with structlog.testing.capture_logs() as caps:
        load_from_unified({"scribe": {
            "encounter_salt": _SALT, "stt": {"provider": "fake"},
            "diarize": {"provider": "off", "enrollment_dir": "/data/enroll"},
        }})
    assert not [c for c in caps if c.get("event") == "scribe.config.enrollment_path_deprecated"]


# ---------------------------------------------------------------------------
# embed_voice fake seam
# ---------------------------------------------------------------------------

def test_embed_providers_mirror_diarize():
    from alfred.scribe.diarize import SCRIBE_DIARIZE_PROVIDERS
    assert SCRIBE_EMBED_PROVIDERS == SCRIBE_DIARIZE_PROVIDERS == frozenset({"off", "fake", "pyannote"})


def test_fake_embed_deterministic_and_unit_norm():
    cfg = _cfg(provider="fake")
    v1 = embed_windows(cfg, [b"window-audio-bytes"])[0]
    v2 = embed_windows(cfg, [b"window-audio-bytes"])[0]
    assert v1 == v2                                   # deterministic
    assert len(v1) == EMBED_DIM
    assert abs(math.sqrt(sum(x * x for x in v1)) - 1.0) < 1e-9   # unit norm


def test_fake_embed_distinct_inputs_distinct_vectors():
    cfg = _cfg(provider="fake")
    a = embed_windows(cfg, [b"speaker-A"])[0]
    b = embed_windows(cfg, [b"speaker-B"])[0]
    assert a != b


def test_fake_embed_multiple_windows():
    cfg = _cfg(provider="fake")
    vs = embed_windows(cfg, [b"w1", b"w2", b"w3"])
    assert len(vs) == 3 and all(len(v) == EMBED_DIM for v in vs)


def test_embed_off_and_pyannote_gate():
    with pytest.raises(EmbedError):
        embed_windows(_cfg(provider="off"), [b"x"])
    with pytest.raises(NotImplementedError):
        embed_windows(_cfg(provider="pyannote"), [b"x"])


def test_unit_normalize_zero_vector_is_canonical():
    z = embed_voice._unit_normalize([0.0] * EMBED_DIM)
    assert z[0] == 1.0 and abs(math.sqrt(sum(x * x for x in z)) - 1.0) < 1e-9


def test_engine_fingerprint_fake_deterministic():
    cfg = _cfg(provider="fake")
    fp = engine_fingerprint(cfg)
    assert fp == engine_fingerprint(cfg)                       # deterministic
    assert fp["embedding_model"] == "fake-embed-v1" and fp["engine_version"] == "fake-1"


def test_engine_fingerprint_pyannote_from_config_placeholder():
    cfg = _cfg(provider="pyannote", diarize_extra={
        "embedding_model": "pyannote/wespeaker-x", "embedding_revision": "rev9",
    })
    fp = engine_fingerprint(cfg)
    assert fp["embedding_model"] == "pyannote/wespeaker-x"
    assert fp["embedding_revision"] == "rev9"
    assert fp["engine_version"] == "pyannote-unresolved"       # P4-4 placeholder marker
