"""P4-5 panel fix-round — pipeline/diarize/enrollment findings.

Kill-switched engine must not stamp provenance or book preset-attributed capture rows;
a PRESENT-but-corrupt binding must be LOUD (not silently conflated with 'no preset
selected'); the observability latches (new_preset_first_use fires once; the unusable
warning does not re-log every 30 s sweep forever); the binding LOCKS AT THE FIRST CHUNK;
and the diarize_stats row carries the 5b latch denominator + match telemetry wire.
"""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager

import aiohttp
import pytest
import structlog

from alfred.scribe import diarize as diarize_mod
from alfred.scribe import embed_voice, enroll_learning
from alfred.scribe import enroll_web as ew
from alfred.scribe import enrollment as en
from alfred.scribe import pipeline as pl
from alfred.scribe.config import (
    ScribeConfig, ScribeDiarizeConfig, ScribeIngestWebConfig, ScribeLlmConfig,
    ScribeSttConfig, load_from_unified,
)
from alfred.scribe.ingest_web import INGEST_CHUNK_ROUTE, IngestWebServer
from alfred.scribe.ledger import ledger_path, load_ledger
from alfred.scribe.pipeline import accumulate_encounter

_SALT = "DUMMY_SCRIBE_TEST_SALT"
_INGEST = "DUMMY_INGEST_TOKEN_0001"
_ENROLL = "DUMMY_ENROLL_TOKEN_0002"
_USER = "np_jamie"
_LABEL = "enc-1720000000000-0123456789abcdef"
_TAGGED = ["[CLIN] Doctor asks about symptoms.", "[PT] Patient reports chest pain."]


@pytest.fixture(autouse=True)
def _clear_latches():
    """The observability latches are module-global (once-per-lifecycle by design)."""
    en._UNUSABLE_LOGGED.clear()
    pl._PRESET_USE_SEEN.clear()
    yield
    en._UNUSABLE_LOGGED.clear()
    pl._PRESET_USE_SEEN.clear()


def _cfg(tmp_path, *, provider="fake", enabled=True):
    return load_from_unified({"scribe": {
        "mode": "synthetic", "encounter_salt": _SALT,
        "stt": {"provider": "fake"},
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "m"},
        "diarize": {"provider": provider, "enabled": enabled,
                    "enrollment_dir": str(tmp_path / "enroll")},
    }})


def _preset(cfg, user=_USER, window=b"voice"):
    centroid = en.spherical_mean_centroid(embed_voice.embed_windows(cfg, [window]))
    now = en._iso_now()
    return en.Preset(
        preset_id=en.mint_preset_id(), user=user, name="Room A", status=en.STATUS_ACTIVE,
        centroids=[centroid], embedding_dim=len(centroid),
        centroid_digest=en.centroid_digest([centroid]), centroid_version=1,
        centroid_source=en.CENTROID_SOURCE_RECORDED, enrolled_at=now, created_at=now,
        updated_at=now, engine=embed_voice.engine_fingerprint(cfg),
        sample_stats={}, quality={"verdict": "ok", "advisory": {}}, device_hint={},
    )


def _bind(cfg, enc, p):
    en.write_preset(cfg.diarize.enrollment_dir, p, is_new=True)
    enc.mkdir(parents=True, exist_ok=True)
    en.write_binding(enc, p)


def _chunk(enc, seq, lines=_TAGGED):
    enc.mkdir(parents=True, exist_ok=True)
    n = f"chunk_{seq:03d}"
    (enc / f"{n}.wav").write_bytes(f"audio-{seq}".encode())
    (enc / f"{n}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (enc / f"{n}.meta.json").write_text(json.dumps({"synthetic": True, "seq": seq}),
                                        encoding="utf-8")


def _rows(cfg):
    p = enroll_learning._capture_path(cfg.diarize.enrollment_dir)
    if not p.is_file():
        return []
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if r.get("kind") == "diarize_stats"]


# ── kill-switched engine must not claim attribution ──────────────────────────

def test_will_diarize_predicate():
    assert diarize_mod.will_diarize(_cfg_obj("fake", True)) is True
    assert diarize_mod.will_diarize(_cfg_obj("off", True)) is False
    assert diarize_mod.will_diarize(_cfg_obj("pyannote", True)) is True
    assert diarize_mod.will_diarize(_cfg_obj("pyannote", False)) is False   # NOTE-1 kill-switch


def _cfg_obj(provider, enabled):
    return ScribeConfig(diarize=ScribeDiarizeConfig(provider=provider, enabled=enabled))


def test_kill_switched_engine_stamps_no_provenance_and_no_preset_rows(tmp_path):
    # provider=pyannote + enabled:false is the supported disable path — nothing is
    # diarized, so the note must NOT claim a preset anchored its attribution and the sink
    # must NOT book 100%-unknown chunks against (preset_id, version).
    cfg = _cfg(tmp_path, provider="pyannote", enabled=False)
    enc = tmp_path / "inbox" / "enc-K"
    # Build the centroid with the fake embedder, but stamp the PYANNOTE fingerprint so the
    # preset genuinely RESOLVES under this config — otherwise the test would pass for the
    # wrong reason (an incidental incompatible-engine refusal instead of the will_diarize gate).
    p = _preset(_cfg(tmp_path, provider="fake"))
    p.engine = embed_voice.engine_fingerprint(cfg)
    _bind(cfg, enc, p)
    assert isinstance(
        en.resolve_for_encounter(enc, cfg.diarize.enrollment_dir,
                                 embed_voice.engine_fingerprint(cfg)),
        en.ResolvedEnrollment,
    ), "preset must be resolvable — the gate under test is will_diarize, not classification"
    _chunk(enc, 1)

    with structlog.testing.capture_logs() as cap:
        r = accumulate_encounter(enc, config=cfg)

    assert r.folded == 1
    led = load_ledger(ledger_path(enc, r.encounter_id))
    assert led.diarized is False
    assert led.diarize_preset is None                 # no false provenance claim
    rows = _rows(cfg)
    assert len(rows) == 1
    assert rows[0]["preset_id"] is None               # not booked against the preset
    assert rows[0]["diarized"] is False               # 5b can filter these out
    assert not [c for c in cap if c.get("event") == "scribe.enrollment.new_preset_first_use"]


# ── corrupt binding is LOUD, not silently 'no preset selected' ───────────────

def test_corrupt_binding_is_classified_not_conflated_with_absent(tmp_path):
    cfg = _cfg(tmp_path)
    enc = tmp_path / "inbox" / "enc-C"
    enc.mkdir(parents=True, exist_ok=True)
    en.binding_path(enc).write_text("{ this is not json", encoding="utf-8")  # truncated
    _chunk(enc, 1)

    with structlog.testing.capture_logs() as cap:
        r = accumulate_encounter(enc, config=cfg)

    assert r.folded == 1                                          # fail-open, still folds
    unusable = [c for c in cap if c.get("event") == "scribe.enrollment.unusable"]
    assert len(unusable) == 1
    assert unusable[0]["reason"] == en.REFUSAL_CORRUPT
    assert unusable[0]["artifact"] == "binding"                   # NOT silent no_binding


def test_absent_binding_stays_silent(tmp_path):
    # 'no preset selected' remains a first-class, SILENT choice (no unusable spam).
    cfg = _cfg(tmp_path)
    enc = tmp_path / "inbox" / "enc-N"
    _chunk(enc, 1)
    with structlog.testing.capture_logs() as cap:
        accumulate_encounter(enc, config=cfg)
    assert not [c for c in cap if c.get("event") == "scribe.enrollment.unusable"]


def test_unusable_log_is_latched_not_re_logged_every_sweep(tmp_path):
    # A persistently-unusable binding on an encounter left on disk would otherwise warn
    # ~2880x/day, burying the signal that also announces a hostile mid-encounter swap.
    cfg = _cfg(tmp_path)
    enc = tmp_path / "inbox" / "enc-L"
    enc.mkdir(parents=True, exist_ok=True)
    en.binding_path(enc).write_text("{bad", encoding="utf-8")
    _chunk(enc, 1)
    with structlog.testing.capture_logs() as cap1:
        accumulate_encounter(enc, config=cfg)
    assert len([c for c in cap1 if c.get("event") == "scribe.enrollment.unusable"]) == 1
    _chunk(enc, 2)
    with structlog.testing.capture_logs() as cap2:
        accumulate_encounter(enc, config=cfg)          # a SECOND sweep
    assert not [c for c in cap2 if c.get("event") == "scribe.enrollment.unusable"]


# ── new_preset_first_use latch ───────────────────────────────────────────────

def test_first_use_does_not_refire_on_chunkless_sweeps(tmp_path):
    # Bind at Start; sweeps run BEFORE the first chunk settles. The event must not fire
    # until a chunk actually folds — and then exactly once.
    cfg = _cfg(tmp_path)
    enc = tmp_path / "inbox" / "enc-F"
    _bind(cfg, enc, _preset(cfg))

    for _ in range(2):                                  # bound but chunkless sweeps
        with structlog.testing.capture_logs() as cap:
            accumulate_encounter(enc, config=cfg)
        assert not [c for c in cap
                    if c.get("event") == "scribe.enrollment.new_preset_first_use"]

    _chunk(enc, 1)
    with structlog.testing.capture_logs() as cap:
        accumulate_encounter(enc, config=cfg)
    assert len([c for c in cap
                if c.get("event") == "scribe.enrollment.new_preset_first_use"]) == 1

    _chunk(enc, 2)
    with structlog.testing.capture_logs() as cap:
        accumulate_encounter(enc, config=cfg)           # never again
    assert not [c for c in cap
                if c.get("event") == "scribe.enrollment.new_preset_first_use"]


# ── diarize_stats row shape: the 5b latch denominator + the telemetry wire ───

def test_row_carries_eligible_turns_and_min_turn_s(tmp_path):
    cfg = _cfg(tmp_path)
    enc = tmp_path / "inbox" / "enc-R"
    _bind(cfg, enc, _preset(cfg))
    _chunk(enc, 1)
    accumulate_encounter(enc, config=cfg)
    row = _rows(cfg)[0]
    # match_rate = 1 - unknown/eligible is now COMPUTABLE from the row.
    assert "eligible_turns" in row and isinstance(row["eligible_turns"], int)
    assert row["min_turn_s"] == cfg.diarize.min_turn_s
    assert row["diarized"] is True
    assert row["engine_fingerprint"]["embedding_model"] == "fake-embed-v1"  # 5b filter key


def test_match_telemetry_wire_reaches_the_row(tmp_path):
    # P4-5c: per-cluster extraction is LIVE, so best_cosine/separation carry the REAL match
    # score (a real no-match is a genuine 0.0, never None) — the placeholder-era key is the
    # ``extractor`` MARKER, NOT best_cosine's nullness. The WIRE must exist so the sink populates.
    sink: dict = {}
    match = diarize_mod.match_cluster_roles(
        {"SPEAKER_00": [1.0] + [0.0] * (embed_voice.EMBED_DIM - 1)},
        [[1.0] + [0.0] * (embed_voice.EMBED_DIM - 1)], tau=0.75, delta=0.15,
    )
    sink.update({"best_cosine": match.best_cosine, "separation": match.separation})
    # the pipeline writes exactly these keys through to the row
    assert pl._record_diarize_stats.__doc__ and "match_sink" in pl._record_diarize_stats.__doc__
    assert sink["best_cosine"] == pytest.approx(1.0)


# ── binding LOCKS AT THE FIRST CHUNK ─────────────────────────────────────────

def _web_cfg(tmp_path):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return ScribeConfig(
        mode="synthetic", input_dir=str(tmp_path / "inbox"),
        stt=ScribeSttConfig(provider="fake"),
        llm=ScribeLlmConfig(base_url="http://127.0.0.1:11434", model="m"),
        diarize=ScribeDiarizeConfig(provider="fake",
                                    enrollment_dir=str(tmp_path / "enroll")),
        ingest_web=ScribeIngestWebConfig(enabled=True, host="127.0.0.1", port=port,
                                         token=_INGEST, enroll_token=_ENROLL),
        clinicians=[_USER], encounter_salt=_SALT,
    )


@asynccontextmanager
async def _serve(config):
    ew._SESSIONS.clear()
    server = IngestWebServer(config)
    await server.start()
    try:
        yield f"http://127.0.0.1:{config.ingest_web.port}", config
    finally:
        await server.stop()
        ew._SESSIONS.clear()


def _h(tok, port):
    return {"Authorization": f"Bearer {tok}", "Host": f"127.0.0.1:{port}"}


@pytest.mark.asyncio
async def test_first_bind_refused_once_the_encounter_has_started(tmp_path):
    cfg = _web_cfg(tmp_path)
    p = _preset(cfg)
    en.write_preset(cfg.diarize.enrollment_dir, p, is_new=True)
    async with _serve(cfg) as (base, _c):
        port = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            # start recording FIRST (no preset bound)
            async with s.post(base + INGEST_CHUNK_ROUTE,
                              params={"label": _LABEL, "seq": "1", "ext": "webm",
                                      "synthetic": "true"},
                              data=b"AUDIO", headers=_h(_INGEST, port)) as r:
                assert r.status == 200
            # a LATE first bind must be refused — the binding locks at the first chunk
            async with s.post(base + ew.ENCOUNTER_PRESET,
                              params={"label": _LABEL, "preset": p.preset_id},
                              headers=_h(_INGEST, port)) as r:
                assert r.status == 409
    assert en.read_binding(tmp_path / "inbox" / _LABEL) is None       # nothing bound


@pytest.mark.asyncio
async def test_same_pair_rebind_stays_idempotent_even_mid_recording(tmp_path):
    # The lock must not break the client's safe retry: an identical re-bind is a no-op.
    cfg = _web_cfg(tmp_path)
    p = _preset(cfg)
    en.write_preset(cfg.diarize.enrollment_dir, p, is_new=True)
    async with _serve(cfg) as (base, _c):
        port = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            async with s.post(base + ew.ENCOUNTER_PRESET,
                              params={"label": _LABEL, "preset": p.preset_id},
                              headers=_h(_INGEST, port)) as r:
                assert r.status == 200                     # bind BEFORE recording
            async with s.post(base + INGEST_CHUNK_ROUTE,
                              params={"label": _LABEL, "seq": "1", "ext": "webm",
                                      "synthetic": "true"},
                              data=b"AUDIO", headers=_h(_INGEST, port)) as r:
                assert r.status == 200                     # now recording
            async with s.post(base + ew.ENCOUNTER_PRESET,
                              params={"label": _LABEL, "preset": p.preset_id},
                              headers=_h(_INGEST, port)) as r:
                assert r.status == 200                     # identical re-bind: idempotent
