"""P4-5a foundation — config delta + the embed_voice provider seam.

Torch-free (fake seam). Covers the config schema change (enrollment_dir added,
enroll_token added + allowlist lockstep, enrollment_path dropped with a loud
deprecation warning) and the deterministic fake voice-embedding seam that makes the
whole enrollment/registry/match surface CI-testable.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

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
    from alfred.scribe.config import ScribeIngestWebConfig
    # enroll_token lives on ScribeIngestWebConfig (the SERVER block), NOT on the diarize
    # block — assert BOTH directions (the old `... or True` here asserted nothing).
    assert "enroll_token" in ScribeIngestWebConfig.__dataclass_fields__
    assert "enroll_token" not in ScribeDiarizeConfig.__dataclass_fields__


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


def test_embed_off_gate_and_pyannote_requires_staged_engine():
    with pytest.raises(EmbedError):
        embed_windows(_cfg(provider="off"), [b"x"])
    # pyannote embed now runs the REAL engine (was NotImplementedError). With no staged
    # pipeline_config it fails LOUD (EmbedError) BEFORE any torch import — the config gate
    # is torch-free, so this asserts the fail-loud in torch-free CI.
    with pytest.raises(EmbedError):
        embed_windows(_cfg(provider="pyannote"), [b"x"])


def test_unit_normalize_zero_vector_raises_degenerate():
    # F1 (2026-07-16) — a zero / non-finite raw embedding must RAISE (fail-closed), NEVER
    # canonicalize to the e1 attractor: two independent degenerate embeddings both coerced
    # to e1 and scored cosine=1.0, a max-confidence WRONG clinician attribution. The
    # extraction seam omits the cluster / the enrollment window path skips the window.
    with pytest.raises(embed_voice.DegenerateEmbeddingError):
        embed_voice._unit_normalize([0.0] * EMBED_DIM)
    with pytest.raises(embed_voice.DegenerateEmbeddingError):
        embed_voice._unit_normalize([float("nan")] + [0.0] * (EMBED_DIM - 1))


def test_engine_fingerprint_fake_deterministic():
    cfg = _cfg(provider="fake")
    fp = engine_fingerprint(cfg)
    assert fp == engine_fingerprint(cfg)                       # deterministic
    assert fp["embedding_model"] == "fake-embed-v1" and fp["engine_version"] == "fake-1"


def test_engine_fingerprint_pyannote_resolves_from_staged_engine(tmp_path):
    # The RESOLVED accessor (replaces the placeholder). Torch-free: the model id comes from
    # config (the identity), the REVISION is the staged checkpoint's CONTENT digest (not the
    # raw config revision — an unpinned revision must still invalidate), engine_version reads
    # package metadata (no torch import). Deterministic + stable; a checkpoint change moves it.
    ckpt = tmp_path / "wespeaker.bin"
    ckpt.write_bytes(b"weights-v1")
    cfgp = tmp_path / "pipeline.local.yaml"
    cfgp.write_text(f"pipeline:\n  params:\n    embedding: {ckpt}\n", encoding="utf-8")
    cfg = _cfg(provider="pyannote", diarize_extra={
        "embedding_model": "pyannote/wespeaker-x",
        "embedding_revision": "",              # UNPINNED — must still invalidate on change
        "pipeline_config": str(cfgp),
    })
    fp = engine_fingerprint(cfg)
    assert fp["embedding_model"] == "pyannote/wespeaker-x"         # identity from config
    assert fp["embedding_revision"].startswith("sha256:")         # RESOLVED from the checkpoint
    assert fp == engine_fingerprint(cfg)                          # deterministic / stable
    # A DIFFERENT staged checkpoint → a different revision → presets correctly invalidate,
    # even though the config's embedding_revision stayed "".
    ckpt.write_bytes(b"weights-v2-upgraded")
    assert engine_fingerprint(cfg)["embedding_revision"] != fp["embedding_revision"]


def test_engine_fingerprint_pyannote_no_staged_model_degrades_not_raises():
    # No staged pipeline_config → the fingerprint DEGRADES (does NOT raise): provider=pyannote
    # + enabled:false boots without a staged model, and the pipeline stamps provenance on such
    # un-diarized encounters via this accessor — it must never crash a read/provenance path.
    # (embed_windows stays fail-loud — you can stamp 'unresolved' but you cannot EMBED.)
    fp = engine_fingerprint(_cfg(provider="pyannote"))
    assert fp["embedding_revision"] == ""                  # degraded — no resolved revision
    assert fp == engine_fingerprint(_cfg(provider="pyannote"))   # ...and STABLE


# ---------------------------------------------------------------------------
# the REAL pyannote embedder — glue verified TORCH-FREE (mocked engine)
#
# The wespeaker forward pass runs on-box (the IT at the bottom). These mock the embedder +
# torchaudio to pin MY glue — decode → mono → resample → embed → unit-norm 256 → per-window
# skip — without torch. The OUTPUT SHAPE MUST MATCH the fake seam (256, unit-norm) or the
# registry/digest/matcher (all built against the fake contract) break.
# ---------------------------------------------------------------------------

class _FakeWave:
    """Torch-free waveform stand-in with the tensor methods the embedder glue calls."""

    def __init__(self, channels, samples):
        self._s = (channels, samples)

    @property
    def ndim(self):
        return 2

    @property
    def shape(self):
        return self._s

    def mean(self, dim, keepdim):
        return _FakeWave(1, self._s[1])       # the (C,T)->(1,T) downmix (diarize._to_mono)

    def unsqueeze(self, d):
        return self                            # batch dim — the fake embedder ignores it


class _FakeEmb:
    """A ``(1, dim)`` embedder output stand-in — ``ndim==2`` + ``[0]`` → the row."""

    ndim = 2

    def __init__(self, row):
        self._row = row

    def __getitem__(self, i):
        return self._row


def _install_fake_engine(monkeypatch, *, load=None, embed_row=None):
    import types

    monkeypatch.setattr(embed_voice, "_resolve_embedding_model_path",
                        lambda cfg: Path("/staged/wespeaker.bin"))
    monkeypatch.setattr(embed_voice, "_load_embedder_cached",
                        lambda p: (lambda batch: _FakeEmb(
                            list(embed_row) if embed_row is not None else [0.5] * EMBED_DIM)))
    fake_ta = types.ModuleType("torchaudio")
    fake_ta.load = load or (lambda bio: (_FakeWave(2, 48000), 48000))   # stereo @ 48k default
    fake_ta.functional = types.SimpleNamespace(
        resample=lambda w, a, b: _FakeWave(1, 16000))
    monkeypatch.setitem(sys.modules, "torchaudio", fake_ta)


def test_pyannote_embed_glue_produces_unit_norm_256_per_window(monkeypatch):
    _install_fake_engine(monkeypatch)
    vecs = embed_windows(_cfg(provider="pyannote"), [b"webm-window-1", b"webm-window-2"])
    assert len(vecs) == 2                                   # one vector per window
    for v in vecs:
        assert len(v) == EMBED_DIM                          # matches the fake seam's 256
        assert abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-9   # unit-norm


def test_pyannote_embed_glue_skips_a_bad_window_but_keeps_the_rest(monkeypatch):
    calls = {"n": 0}

    def _load(bio):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("could not decode this window")   # first window undecodable
        return (_FakeWave(2, 48000), 48000)

    _install_fake_engine(monkeypatch, load=_load)
    vecs = embed_windows(_cfg(provider="pyannote"), [b"bad", b"good"])
    assert len(vecs) == 1                                   # degraded, not fatal (skip + log)


def test_pyannote_embed_glue_all_windows_bad_raises_engine_error(monkeypatch):
    def _load(bio):
        raise RuntimeError("undecodable")

    _install_fake_engine(monkeypatch, load=_load)
    # every window failed → EmbedError, which the finalize worker maps to engine_error.
    with pytest.raises(EmbedError):
        embed_windows(_cfg(provider="pyannote"), [b"x", b"y"])


# ---------------------------------------------------------------------------
# real wespeaker embedder + fingerprint stability — on-box IT (skip-gated)
# ---------------------------------------------------------------------------

_DIARIZE_FIXTURES = Path(__file__).parent / "fixtures" / "diarize"


@pytest.mark.skipif(
    not os.environ.get("ALFRED_SCRIBE_DIARIZE_IT"),
    reason="real wespeaker embedder — set ALFRED_SCRIBE_DIARIZE_IT=1 on-box with the "
           "[scribe-diarize] extra + $ALFRED_SCRIBE_DIARIZE_PIPELINE_CONFIG (materialized) "
           "+ the committed tests/fixtures/diarize/short_speech.{webm,m4a,wav}",
)
@pytest.mark.parametrize("container", ["webm", "m4a", "wav"])
def test_real_embedder_and_fingerprint_on_box(container):
    # A REAL ~1s clip in EACH device container → a 256-dim unit-norm vector, DETERMINISTIC
    # (same clip → same vector), and engine_fingerprint STABLE across two calls on the same
    # staged model. This is the real-embed proof (infra runs it post-deploy).
    fixture = _DIARIZE_FIXTURES / f"short_speech.{container}"
    if not fixture.is_file():
        pytest.skip(f"missing embed fixture {fixture} (commit the device-container clips)")
    cfg = _cfg(provider="pyannote", diarize_extra={
        "pipeline_config": os.environ["ALFRED_SCRIBE_DIARIZE_PIPELINE_CONFIG"],
    })
    window = fixture.read_bytes()
    v = embed_windows(cfg, [window])
    assert len(v) == 1 and len(v[0]) == EMBED_DIM                      # 256-dim
    assert abs(math.sqrt(sum(x * x for x in v[0])) - 1.0) < 1e-6       # unit-norm
    v2 = embed_windows(cfg, [window])[0]
    assert max(abs(a - b) for a, b in zip(v[0], v2)) < 1e-5           # deterministic
    assert engine_fingerprint(cfg) == engine_fingerprint(cfg)         # fingerprint stable
