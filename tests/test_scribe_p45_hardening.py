"""P4-5 panel fix-round — HIGH hardening pins (load contract + token arming).

H1 — the preset load path NEVER raises: a preset JSON missing any REQUIRED field
classified `corrupt` instead of raising TypeError out of load_preset →
resolve_for_encounter → accumulate_encounter (which previously blocked the encounter
FOREVER, folding zero chunks every sweep — the exact fail-open violation the frozen
contract forbids).

H2 — a scribe bearer token that is YAML-null or an UNRESOLVED ${VAR} placeholder
fail-CLOSES to "" (surface INERT) instead of ARMING the biometric face with a truthy,
publicly-known literal ("None" / "${SCRIBE_ENROLL_TOKEN}"). Plus: equal ingest/enroll
tokens collapse the two-token split → fail-closed.
"""

from __future__ import annotations

import json

import pytest
import structlog

from alfred.scribe import embed_voice
from alfred.scribe import enrollment as en
from alfred.scribe.config import ScribeIngestWebConfig, load_from_unified
from alfred.scribe.ledger import ledger_path, load_ledger
from alfred.scribe.pipeline import accumulate_encounter

_SALT = "DUMMY_SCRIBE_TEST_SALT"
_USER = "np_jamie"


# ═══════════════════════════════════════════════════════════════════════════
# H1 — load contract: a missing required field CLASSIFIES, never raises
# ═══════════════════════════════════════════════════════════════════════════

def _cfg(tmp_path):
    return load_from_unified({"scribe": {
        "mode": "synthetic", "encounter_salt": _SALT,
        "stt": {"provider": "fake"},
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "m"},
        "diarize": {"provider": "fake", "enrollment_dir": str(tmp_path / "enroll")},
    }})


def _good_preset(cfg, user=_USER):
    centroid = en.spherical_mean_centroid(embed_voice.embed_windows(cfg, [b"voice"]))
    now = en._iso_now()
    return en.Preset(
        preset_id=en.mint_preset_id(), user=user, name="Room A", status=en.STATUS_ACTIVE,
        centroids=[centroid], embedding_dim=len(centroid),
        centroid_digest=en.centroid_digest([centroid]), centroid_version=1,
        centroid_source=en.CENTROID_SOURCE_RECORDED, enrolled_at=now, created_at=now,
        updated_at=now, engine=embed_voice.engine_fingerprint(cfg),
        sample_stats={"n_windows": 1, "duration_s": 30.0, "net_speech_s": 30.0,
                      "snr_db_est": 20.0, "spread": 0.0},
        quality={"verdict": "ok", "advisory": {}}, device_hint={},
    )


def test_required_fields_derived_from_dataclass_not_hardcoded():
    # The presence check auto-derives from the dataclass, so a NEW required field can
    # never silently escape the guard.
    assert "name" in en._REQUIRED_PRESET_FIELDS
    assert "engine" in en._REQUIRED_PRESET_FIELDS and "quality" in en._REQUIRED_PRESET_FIELDS
    # fields WITH defaults are not required
    assert "revoked" not in en._REQUIRED_PRESET_FIELDS
    assert "schema_version" not in en._REQUIRED_PRESET_FIELDS


@pytest.mark.parametrize("drop", ["name", "engine", "quality", "sample_stats",
                                  "centroid_version", "centroid_source",
                                  "enrolled_at", "created_at", "updated_at"])
def test_missing_required_field_classifies_corrupt_never_raises(tmp_path, drop):
    cfg = _cfg(tmp_path)
    p = _good_preset(cfg)
    path = en.preset_path(cfg.diarize.enrollment_dir, _USER, p.preset_id)
    d = p.to_dict()
    d.pop(drop)                                   # a hand-edit / partial backup restore
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(d), encoding="utf-8")

    preset, fail = en.load_preset(path)           # MUST NOT raise
    assert preset is None and fail == en.CLASS_CORRUPT


def test_corrupt_preset_does_not_block_the_encounter(tmp_path):
    # THE fail-open pin: a bound preset missing a required field must NOT stop the
    # encounter from folding. Before the fix this raised TypeError out of
    # accumulate_encounter and the encounter folded ZERO chunks every sweep, forever.
    cfg = _cfg(tmp_path)
    enc = tmp_path / "inbox" / "enc-A"
    p = _good_preset(cfg)
    en.write_preset(cfg.diarize.enrollment_dir, p, is_new=True)
    enc.mkdir(parents=True, exist_ok=True)
    en.write_binding(enc, p)                      # bind it, THEN corrupt the preset file
    path = en.preset_path(cfg.diarize.enrollment_dir, _USER, p.preset_id)
    d = json.loads(path.read_text(encoding="utf-8"))
    d.pop("name")
    path.write_text(json.dumps(d), encoding="utf-8")

    (enc / "chunk_001.wav").write_bytes(b"audio-1")
    (enc / "chunk_001.txt").write_text("[PT] Chest pain.\n", encoding="utf-8")
    (enc / "chunk_001.meta.json").write_text(json.dumps({"synthetic": True, "seq": 1}),
                                             encoding="utf-8")

    with structlog.testing.capture_logs() as cap:
        r = accumulate_encounter(enc, config=cfg)  # MUST NOT raise

    assert r.folded == 1                          # the encounter FOLDS (fail-open)
    led = load_ledger(ledger_path(enc, r.encounter_id))
    assert led.diarize_preset is None             # un-anchored (no usable preset)
    unusable = [c for c in cap if c.get("event") == "scribe.enrollment.unusable"]
    assert unusable and unusable[0]["reason"] == en.CLASS_CORRUPT
    assert unusable[0]["artifact"] == "preset"


def _corrupt_shapes(tmp_path):
    """The corruption shapes a TORN WRITE actually produces — the ones whose exceptions
    are NOT OSError/JSONDecodeError and so escaped the original guard."""
    return {
        # invalid UTF-8 → read_text raises UnicodeDecodeError (a ValueError). THE canonical
        # torn-write corruption; the guard named it as the threat but did not catch it.
        "invalid_utf8": b"\xff\xfe\x00\x80not utf-8 at all\xff",
        # deeply-nested JSON → json.loads raises RecursionError (a RuntimeError).
        "deep_nesting": (b"[" * 20000) + (b"]" * 20000),
        # truncated mid-write → JSONDecodeError (this one the old guard DID catch)
        "truncated": b'{"schema_version": 1, "preset_id": "pst-',
        # empty file (an interrupted create)
        "empty": b"",
    }


@pytest.mark.parametrize("shape", list(_corrupt_shapes(None).keys()))
def test_torn_write_shapes_classify_corrupt_never_raise(tmp_path, shape):
    # B1: the read+parse must be INSIDE the belt. UnicodeDecodeError / RecursionError are
    # neither OSError nor JSONDecodeError — they escaped and propagated out of the
    # "NEVER raises" load path.
    cfg = _cfg(tmp_path)
    path = en.preset_path(cfg.diarize.enrollment_dir, _USER,
                          "pst-1720000000000-0123456789abcdef")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_corrupt_shapes(tmp_path)[shape])

    preset, fail = en.load_preset(path)                 # MUST NOT raise
    assert preset is None and fail == en.CLASS_CORRUPT
    # every downstream reader stays fail-open on the same file
    assert en.list_user_presets(cfg.diarize.enrollment_dir, _USER,
                                embed_voice.engine_fingerprint(cfg))[0].classification \
        == en.CLASS_CORRUPT
    # ...and the cap check (which now loads each file) must not break FUTURE enrollments
    assert en.count_active_presets(cfg.diarize.enrollment_dir, _USER) == 0


@pytest.mark.parametrize("shape", ["invalid_utf8", "deep_nesting"])
def test_torn_preset_does_not_block_the_encounter(tmp_path, shape):
    # The blocking failure H1 exists to prevent, through the shapes that escaped.
    cfg = _cfg(tmp_path)
    enc = tmp_path / "inbox" / f"enc-{shape}"
    p = _good_preset(cfg)
    en.write_preset(cfg.diarize.enrollment_dir, p, is_new=True)
    enc.mkdir(parents=True, exist_ok=True)
    en.write_binding(enc, p)
    en.preset_path(cfg.diarize.enrollment_dir, _USER, p.preset_id).write_bytes(
        _corrupt_shapes(tmp_path)[shape])

    (enc / "chunk_001.wav").write_bytes(b"audio-1")
    (enc / "chunk_001.txt").write_text("[PT] Chest pain.\n", encoding="utf-8")
    (enc / "chunk_001.meta.json").write_text(json.dumps({"synthetic": True, "seq": 1}),
                                             encoding="utf-8")

    r = accumulate_encounter(enc, config=cfg)           # MUST NOT raise
    assert r.folded == 1                                # the encounter FOLDS (fail-open)


def test_torn_binding_does_not_block_the_encounter(tmp_path):
    # read_binding had the identical too-narrow guard.
    cfg = _cfg(tmp_path)
    enc = tmp_path / "inbox" / "enc-tb"
    enc.mkdir(parents=True, exist_ok=True)
    en.binding_path(enc).write_bytes(b"\xff\xfe invalid utf-8 binding")
    (enc / "chunk_001.wav").write_bytes(b"audio-1")
    (enc / "chunk_001.txt").write_text("[PT] Chest pain.\n", encoding="utf-8")
    (enc / "chunk_001.meta.json").write_text(json.dumps({"synthetic": True, "seq": 1}),
                                             encoding="utf-8")
    r = accumulate_encounter(enc, config=cfg)           # MUST NOT raise
    assert r.folded == 1


def test_torn_chunk_meta_sidecar_does_not_block_the_encounter(tmp_path):
    # _read_provenance had the same shape; a torn sidecar propagated out of accumulate.
    cfg = _cfg(tmp_path)
    enc = tmp_path / "inbox" / "enc-tm"
    enc.mkdir(parents=True, exist_ok=True)
    (enc / "chunk_001.wav").write_bytes(b"audio-1")
    (enc / "chunk_001.txt").write_text("[PT] Chest pain.\n", encoding="utf-8")
    (enc / "chunk_001.meta.json").write_bytes(b"\xff\xfe torn sidecar")
    r = accumulate_encounter(enc, config=cfg)           # MUST NOT raise
    # provenance unreadable → {} → guard_ingest REFUSES in synthetic mode (fail-closed),
    # but the SWEEP survives — that is the invariant under test.
    assert r.refused == 1 and r.folded == 0


# ═══════════════════════════════════════════════════════════════════════════
# N1/N2 — ABSENT vs CORRUPT on the close-manifest (the medico-legal promise)
# ═══════════════════════════════════════════════════════════════════════════

def test_torn_close_sentinel_is_corrupt_not_absent(tmp_path):
    # N1: a TORN (invalid-UTF-8) _CLOSED is a CORRUPT PROMISE → fail-closed ALWAYS
    # (ambiguous=True) regardless of `require`, exactly like malformed JSON. Conflating it
    # with ABSENT let it read as a legacy empty close in non-strict mode and FINALIZE READY.
    from alfred.scribe.close_manifest import read_close_manifest
    sentinel = tmp_path / "_CLOSED"
    sentinel.write_bytes(b"\xff\xfe torn sentinel \x80")
    assert read_close_manifest(sentinel, require=False) == (None, True)   # ← was (None, False)
    assert read_close_manifest(sentinel, require=True) == (None, True)


def test_absent_close_sentinel_stays_legacy_tolerant(tmp_path):
    # The ABSENT case must be UNCHANGED: a vanished sentinel has no promise to honour.
    from alfred.scribe.close_manifest import read_close_manifest
    gone = tmp_path / "_CLOSED"                       # never created → OSError on read
    assert read_close_manifest(gone, require=False) == (None, False)
    assert read_close_manifest(gone, require=True) == (None, True)


def test_empty_legacy_sentinel_still_upgradable(tmp_path):
    # An EMPTY sentinel is not corrupt — it must stay legacy-tolerant (and remain
    # upgradable by a later /close), else the encounter wedges.
    from alfred.scribe.close_manifest import read_close_manifest
    sentinel = tmp_path / "_CLOSED"
    sentinel.write_text("", encoding="utf-8")
    assert read_close_manifest(sentinel, require=False) == (None, False)


def test_torn_close_sentinel_never_finalizes_ready(tmp_path):
    # The end-to-end invariant: a corrupt promise can NEVER reach READY, even in the
    # non-strict (legacy) mode where the conflation used to let it through.
    from alfred.scribe.close_manifest import CLOSE_SENTINEL_NAME
    cfg = _cfg(tmp_path)                              # require_close_manifest defaults False
    enc = tmp_path / "inbox" / "enc-torn"
    enc.mkdir(parents=True, exist_ok=True)
    (enc / "chunk_001.wav").write_bytes(b"audio-1")
    (enc / "chunk_001.txt").write_text("[PT] Chest pain.\n", encoding="utf-8")
    (enc / "chunk_001.meta.json").write_text(json.dumps({"synthetic": True, "seq": 1}),
                                             encoding="utf-8")
    (enc / CLOSE_SENTINEL_NAME).write_bytes(b"\xff\xfe torn")

    r = accumulate_encounter(enc, config=cfg)
    assert r.closed is True
    assert r.close_ambiguous is True                  # fail-closed → the gate blocks READY


def test_close_manifest_corrupt_cannot_lower_the_monotonic_bar(tmp_path):
    # N2: the ambiguity flag was DISCARDED, so a corrupt existing sentinel yielded
    # existing_efs=None, the monotonic guard was skipped, and a 2nd /close with a LOWER
    # final_seq silently RESET the completeness bar. It must now REFUSE.
    import asyncio as _asyncio
    import socket as _socket

    import aiohttp as _aiohttp

    from alfred.scribe.close_manifest import CLOSE_SENTINEL_NAME
    from alfred.scribe.config import (
        ScribeIngestWebConfig, ScribeLlmConfig, ScribeSttConfig,
    )
    from alfred.scribe.config import ScribeConfig as _SC
    from alfred.scribe.ingest_web import CLOSE_ROUTE, IngestWebServer

    s = _socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    label = "enc-1720000000000-0123456789abcdef"
    cfg = _SC(
        mode="synthetic", input_dir=str(tmp_path / "inbox"),
        stt=ScribeSttConfig(provider="fake"),
        llm=ScribeLlmConfig(base_url="http://127.0.0.1:11434", model="m"),
        ingest_web=ScribeIngestWebConfig(enabled=True, host="127.0.0.1", port=port,
                                         token="DUMMY_INGEST_TOKEN_0001"),
        encounter_salt=_SALT,
    )
    enc = tmp_path / "inbox" / label
    enc.mkdir(parents=True, exist_ok=True)
    (enc / CLOSE_SENTINEL_NAME).write_bytes(b"\xff\xfe torn promise")   # corrupt existing bar

    async def _go():
        server = IngestWebServer(cfg)
        await server.start()
        try:
            base = f"http://127.0.0.1:{port}"
            h = {"Authorization": "Bearer DUMMY_INGEST_TOKEN_0001",
                 "Host": f"127.0.0.1:{port}"}
            async with _aiohttp.ClientSession() as sess:
                async with sess.post(base + CLOSE_ROUTE,
                                     params={"label": label, "final_seq": "1"},
                                     headers=h) as r:
                    return r.status
        finally:
            await server.stop()

    assert _asyncio.run(_go()) == 409                  # close_manifest_corrupt — bar NOT reset
    # the corrupt sentinel was NOT overwritten with a lower bar
    assert (enc / CLOSE_SENTINEL_NAME).read_bytes() == b"\xff\xfe torn promise"


def test_corrupt_preset_listing_does_not_raise(tmp_path):
    # list_user_presets (presets CLI + GET /scribe/presets) must classify, not 500.
    cfg = _cfg(tmp_path)
    p = _good_preset(cfg)
    path = en.preset_path(cfg.diarize.enrollment_dir, _USER, p.preset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    d = p.to_dict(); d.pop("engine")
    path.write_text(json.dumps(d), encoding="utf-8")
    entries = en.list_user_presets(cfg.diarize.enrollment_dir, _USER,
                                   embed_voice.engine_fingerprint(cfg))
    assert len(entries) == 1 and entries[0].classification == en.CLASS_CORRUPT


def test_load_belt_swallows_any_construction_failure(tmp_path, monkeypatch):
    # BELT: even if _structural_ok's teeth ever miss a shape, the load path still
    # CLASSIFIES rather than propagating (the "NEVER raises" contract is load-bearing).
    cfg = _cfg(tmp_path)
    p = _good_preset(cfg)
    path = en.preset_path(cfg.diarize.enrollment_dir, _USER, p.preset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(p.to_dict()), encoding="utf-8")

    def _boom(data, path):
        raise RuntimeError("teeth exploded")
    monkeypatch.setattr(en, "_structural_ok", _boom)
    preset, fail = en.load_preset(path)           # MUST NOT raise
    assert preset is None and fail == en.CLASS_CORRUPT


# ═══════════════════════════════════════════════════════════════════════════
# H2 — token arming: YAML-null / unresolved ${VAR} / equal tokens fail CLOSED
# ═══════════════════════════════════════════════════════════════════════════

def _web(raw_ingest_web, monkeypatch):
    monkeypatch.delenv("SCRIBE_ENROLL_TOKEN", raising=False)
    monkeypatch.delenv("SCRIBE_INGEST_TOKEN", raising=False)
    cfg = load_from_unified({"scribe": {
        "encounter_salt": _SALT, "stt": {"provider": "fake"},
        "ingest_web": raw_ingest_web,
    }})
    return cfg.ingest_web


def test_yaml_null_enroll_token_is_empty_not_the_string_None(monkeypatch):
    # `enroll_token:` (bare key) → None → str(None) == "None" would ARM the face with a
    # guessable bearer. Must fail-closed to "" (INERT).
    web = _web({"enabled": True, "token": "t", "enroll_token": None}, monkeypatch)
    assert web.enroll_token == ""                 # NOT "None"
    assert web.enroll_token != "None"


def test_yaml_null_ingest_token_is_empty(monkeypatch):
    web = _web({"enabled": True, "token": None}, monkeypatch)
    assert web.token == "" and web.token != "None"


def test_unresolved_placeholder_enroll_token_fails_closed_and_logs(monkeypatch):
    # env var absent → substitution leaves the LITERAL "${SCRIBE_ENROLL_TOKEN}" (truthy +
    # publicly known). Must fail-closed to "" with a loud, actionable error.
    with structlog.testing.capture_logs() as cap:
        web = _web({"enabled": True, "token": "t",
                    "enroll_token": "${SCRIBE_ENROLL_TOKEN}"}, monkeypatch)
    assert web.enroll_token == ""
    errs = [c for c in cap if c.get("event") == "scribe.config.unresolved_token_placeholder"]
    assert len(errs) == 1 and errs[0]["field"] == "ingest_web.enroll_token"


def test_unresolved_placeholder_ingest_token_fails_closed(monkeypatch):
    web = _web({"enabled": True, "token": "${SCRIBE_INGEST_TOKEN}"}, monkeypatch)
    assert web.token == ""                        # barrier-e then refuses the enabled server


@pytest.mark.parametrize("blank", ["   ", "\t", "\n", " \t\n "])
def test_whitespace_only_token_is_inert_not_armed(monkeypatch, blank):
    # B2(a): "   " is TRUTHY — it would ARM the biometric face with a blank-ish bearer.
    web = _web({"enabled": True, "token": "t", "enroll_token": blank}, monkeypatch)
    assert web.enroll_token == ""


@pytest.mark.parametrize("val", ["pre-${SCRIBE_ENROLL_TOKEN}", "abc${SUFFIX}",
                                 "x${A}y", "tok_${ENV}"])
def test_placeholder_anywhere_in_the_value_fails_closed(monkeypatch, val):
    # B2(b): startswith("${") was NARROWER than the threat — an unresolved placeholder is
    # just as guessable when it is not at position 0.
    with structlog.testing.capture_logs() as cap:
        web = _web({"enabled": True, "token": "t", "enroll_token": val}, monkeypatch)
    assert web.enroll_token == ""
    assert [c for c in cap if c.get("event") == "scribe.config.unresolved_token_placeholder"]


def test_token_is_stripped_not_rejected(monkeypatch):
    # A real token with stray surrounding whitespace still works (stripped, not blanked).
    web = _web({"enabled": True, "token": "  DUMMY_INGEST_0001  "}, monkeypatch)
    assert web.token == "DUMMY_INGEST_0001"


def test_real_token_values_survive(monkeypatch):
    web = _web({"enabled": True, "token": "DUMMY_INGEST_0001",
                "enroll_token": "DUMMY_ENROLL_0002"}, monkeypatch)
    assert web.token == "DUMMY_INGEST_0001" and web.enroll_token == "DUMMY_ENROLL_0002"


def test_equal_tokens_collapse_the_split_so_enroll_fails_closed(monkeypatch):
    # The page EMBEDS the ingest token; an equal enroll token would let page possession
    # alone drive biometric mutation. Fail-closed: enroll face INERT + loud error.
    with structlog.testing.capture_logs() as cap:
        web = _web({"enabled": True, "token": "SAME_DUMMY_TOKEN",
                    "enroll_token": "SAME_DUMMY_TOKEN"}, monkeypatch)
    assert web.enroll_token == ""                 # INERT, not armed-by-page-token
    assert web.token == "SAME_DUMMY_TOKEN"        # ingest side unaffected
    assert [c for c in cap if c.get("event") == "scribe.config.enroll_token_equals_ingest_token"]


def test_defaults_stay_inert():
    c = ScribeIngestWebConfig()
    assert c.token == "" and c.enroll_token == ""
