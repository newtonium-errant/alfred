"""P4-5 panel fix-round — enroll session lifecycle, arming gates, audit completeness.

Binds the frozen contract on the surfaces the panel found unguarded:
arming (dormant store / dormant provider refuse BEFORE the recording), re-record target
validation (no tombstone RESURRECTION, no cap bypass), session-cap semantics (done
sessions hold no custody weight), the finalize TOCTOUs (double-finalize; abandon/TTL vs
in-flight finalize — an explicit discard is TERMINAL), seq idempotency, the 4th advisory
gate, the untested 32 MiB session cap + cap_hit logs, the webm/mp4 sniff dispatch, the
0700 store modes, and the durable audit.log event set.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from contextlib import asynccontextmanager

import aiohttp
import pytest
import structlog

from alfred.scribe import embed_voice, enroll_learning
from alfred.scribe import enroll_web as ew
from alfred.scribe import enrollment as en
from alfred.scribe.config import (
    ScribeConfig, ScribeDiarizeConfig, ScribeIngestWebConfig, ScribeLlmConfig,
    ScribeSttConfig,
)
from alfred.scribe.ingest_web import STATUS_ROUTE, IngestWebServer

_SALT = "DUMMY_SCRIBE_TEST_SALT"
_INGEST = "DUMMY_INGEST_TOKEN_0001"
_ENROLL = "DUMMY_ENROLL_TOKEN_0002"
_USER = "np_jamie"
_LABEL = "enc-1720000000000-0123456789abcdef"


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def _config(tmp_path, *, enrollment_dir="__set__", provider="fake", enroll_token=_ENROLL):
    ed = str(tmp_path / "enroll") if enrollment_dir == "__set__" else enrollment_dir
    return ScribeConfig(
        mode="synthetic", input_dir=str(tmp_path / "inbox"),
        stt=ScribeSttConfig(provider="fake"),
        llm=ScribeLlmConfig(base_url="http://127.0.0.1:11434", model="m"),
        diarize=ScribeDiarizeConfig(provider=provider, enrollment_dir=ed),
        ingest_web=ScribeIngestWebConfig(
            enabled=True, host="127.0.0.1", port=_free_port(),
            token=_INGEST, enroll_token=enroll_token,
        ),
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


def _h(token, port):
    return {"Authorization": f"Bearer {token}", "Host": f"127.0.0.1:{port}"}


def _win(kb):
    return b"a" * (kb * 1024)


async def _start(s, base, p, *, user=_USER, preset=None):
    params = {"user": user}
    if preset:
        params["preset"] = preset
    async with s.post(base + ew.ENROLL_START, params=params, headers=_h(_ENROLL, p)) as r:
        body = await r.json() if r.content_type == "application/json" else {}
        return r.status, body


async def _full(s, base, p, *, kb=130, n=4, name="Room A", preset=None):
    st, b = await _start(s, base, p, preset=preset)
    assert st == 200, b
    sid = b["session"]
    for _ in range(n):
        async with s.post(base + ew.ENROLL_CHUNK, params={"session": sid},
                          data=_win(kb), headers=_h(_ENROLL, p)) as r:
            assert r.status == 200
    async with s.post(base + ew.ENROLL_FINALIZE, params={"session": sid},
                      json={"name": name}, headers=_h(_ENROLL, p)) as r:
        assert r.status == 200, await r.text()
    for _ in range(300):
        async with s.get(base + ew.ENROLL_RESULT, params={"session": sid},
                         headers=_h(_ENROLL, p)) as r:
            body = await r.json()
        if body.get("state") == "done":
            return body
        await asyncio.sleep(0.01)
    raise AssertionError("finalize did not complete")


def _audit_events(enroll_dir):
    p = enroll_dir / enroll_learning.AUDIT_NAME
    if not p.is_file():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


# ── ARMING GATES — refuse BEFORE the recording ────────────────────────────────

@pytest.mark.asyncio
async def test_start_refuses_when_enrollment_dir_unset(tmp_path):
    # Face armed by enroll_token but no store → biometrics would land in the daemon CWD.
    async with _serve(_config(tmp_path, enrollment_dir="")) as (base, cfg):
        async with aiohttp.ClientSession() as s:
            st, _ = await _start(s, base, cfg.ingest_web.port)
    assert st == 503                                   # enrollment_dormant, BEFORE recording


@pytest.mark.asyncio
async def test_start_refuses_when_provider_off(tmp_path):
    # provider='off' is the shipped default and means DORMANT — refusing here is what
    # prevents a consented clinician burning a 45 s recording on a misleading engine_error.
    async with _serve(_config(tmp_path, provider="off")) as (base, cfg):
        async with aiohttp.ClientSession() as s:
            st, _ = await _start(s, base, cfg.ingest_web.port)
    assert st == 503


@pytest.mark.asyncio
async def test_no_biometrics_written_to_cwd_when_dormant(tmp_path, monkeypatch):
    # The custody consequence of the arming gate: nothing may be written relative to CWD.
    monkeypatch.chdir(tmp_path)
    async with _serve(_config(tmp_path, enrollment_dir="")) as (base, cfg):
        async with aiohttp.ClientSession() as s:
            await _start(s, base, cfg.ingest_web.port)
    assert not (tmp_path / _USER).exists()             # no <cwd>/<user>/pst-*.json
    assert not (tmp_path / "audit.log").exists()       # no <cwd>/audit.log


# ── RE-RECORD TARGET validation ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rerecord_of_tombstone_is_refused_not_resurrected(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            res = await _full(s, base, p)
            pid = res["preset_id"]
            en.revoke_preset(cfg.diarize.enrollment_dir, _USER, pid, reason="user_delete")
            st, _ = await _start(s, base, p, preset=pid)
    assert st == 409                                   # preset_revoked — NEVER resurrected
    preset, _f = en.load_preset(en.preset_path(cfg.diarize.enrollment_dir, _USER, pid))
    assert preset.status == en.STATUS_REVOKED and preset.revoked is not None


@pytest.mark.asyncio
async def test_rerecord_of_unknown_id_is_refused(tmp_path):
    # An arbitrary grammar-valid id would otherwise mint a preset via write_preset(
    # is_new=False), bypassing the server id-mint AND the 32/user cap.
    async with _serve(_config(tmp_path)) as (base, cfg):
        async with aiohttp.ClientSession() as s:
            st, _ = await _start(s, base, cfg.ingest_web.port,
                                 preset="pst-1720000000000-0123456789abcdef")
    assert st == 404
    assert not (tmp_path / "enroll" / _USER).exists()   # nothing minted


@pytest.mark.asyncio
async def test_rerecord_of_active_preset_still_works(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            res = await _full(s, base, p)
            res2 = await _full(s, base, p, preset=res["preset_id"], name="v2")
    assert res2["preset_id"] == res["preset_id"]
    preset, _f = en.load_preset(en.preset_path(cfg.diarize.enrollment_dir, _USER,
                                               res["preset_id"]))
    assert preset.centroid_version == 2


# ── SESSION CAP semantics ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_done_sessions_do_not_consume_the_cap(tmp_path):
    # Two COMPLETED enrollments hold zero bytes; a third start must NOT 429 (the memo's
    # own hard-fail → [Record again] retry and the guided multi-preset flow depend on it).
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            await _full(s, base, p, name="A")
            await _full(s, base, p, name="B")
            st, _ = await _start(s, base, p)           # the third, within the TTL
    assert st == 200


@pytest.mark.asyncio
async def test_live_sessions_still_consume_the_cap(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            for _ in range(ew._MAX_SESSIONS):
                assert (await _start(s, base, p))[0] == 200
            st, _ = await _start(s, base, p)           # a third LIVE (recording) session
    assert st == 429


# ── FINALIZE TOCTOUs ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_double_finalize_mints_only_one_preset(tmp_path):
    # The body read yields; two concurrent finalizes must not both pass the 'recording'
    # gate (that minted TWO preset files from ONE session).
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            st, b = await _start(s, base, p)
            sid = b["session"]
            for _ in range(4):
                async with s.post(base + ew.ENROLL_CHUNK, params={"session": sid},
                                  data=_win(130), headers=_h(_ENROLL, p)) as r:
                    assert r.status == 200

            async def _fin():
                async with s.post(base + ew.ENROLL_FINALIZE, params={"session": sid},
                                  json={"name": "X"}, headers=_h(_ENROLL, p)) as r:
                    return r.status
            codes = await asyncio.gather(_fin(), _fin())
            for _ in range(300):
                async with s.get(base + ew.ENROLL_RESULT, params={"session": sid},
                                 headers=_h(_ENROLL, p)) as r:
                    if (await r.json()).get("state") == "done":
                        break
                await asyncio.sleep(0.01)
    assert sorted(codes) == [200, 404]                 # exactly ONE finalize accepted
    files = list((tmp_path / "enroll" / _USER).glob("pst-*.json"))
    assert len(files) == 1                             # ONE preset from one session


@pytest.mark.asyncio
async def test_abandon_during_finalize_persists_no_voiceprint(tmp_path, monkeypatch):
    # An explicit discard is TERMINAL on a biometric surface: the worker's pre-write
    # live-check must prevent the centroid ever reaching disk.
    real = embed_voice.embed_windows

    def _slow(config, windows):
        time.sleep(0.4)                                # keep the worker inside _finalize_sync
        return real(config, windows)
    monkeypatch.setattr(embed_voice, "embed_windows", _slow)

    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            st, b = await _start(s, base, p)
            sid = b["session"]
            for _ in range(4):
                async with s.post(base + ew.ENROLL_CHUNK, params={"session": sid},
                                  data=_win(130), headers=_h(_ENROLL, p)) as r:
                    assert r.status == 200
            async with s.post(base + ew.ENROLL_FINALIZE, params={"session": sid},
                              json={"name": "X"}, headers=_h(_ENROLL, p)) as r:
                assert r.status == 200
            await asyncio.sleep(0.05)                  # finalize in flight
            async with s.post(base + ew.ENROLL_ABANDON, params={"session": sid},
                              headers=_h(_ENROLL, p)) as r:
                assert r.status == 200
            await asyncio.sleep(0.8)                   # let the worker run to completion
    # the abandoned session's voiceprint was NEVER written (the pre-write live-check)
    user_dir = tmp_path / "enroll" / _USER
    assert not user_dir.exists() or not list(user_dir.glob("pst-*.json"))
    # ...and the discard is in the durable custody trail
    assert any(e["event"] == "enroll_aborted" for e in _audit_events(tmp_path / "enroll"))


@pytest.mark.asyncio
async def test_finalize_refuses_when_preset_bound_to_open_encounter(tmp_path):
    # The re-record started while unbound; the preset got bound to a live encounter mid
    # session. Overwriting would de-anchor the LIVE encounter — contract says 409.
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            res = await _full(s, base, p)
            pid = res["preset_id"]
            st, b = await _start(s, base, p, preset=pid)   # unbound at start → OK
            assert st == 200
            sid = b["session"]
            # now bind it to an OPEN encounter (no _CLOSED)
            preset, _f = en.load_preset(en.preset_path(cfg.diarize.enrollment_dir, _USER, pid))
            enc = tmp_path / "inbox" / _LABEL
            enc.mkdir(parents=True, exist_ok=True)
            en.write_binding(enc, preset)
            async with s.post(base + ew.ENROLL_FINALIZE, params={"session": sid},
                              json={"name": "v2"}, headers=_h(_ENROLL, p)) as r:
                assert r.status == 409                     # preset_bound_open_encounter
    preset, _f = en.load_preset(en.preset_path(cfg.diarize.enrollment_dir, _USER, pid))
    assert preset.centroid_version == 1                    # NOT overwritten


# ── SEQ IDEMPOTENCY ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chunk_seq_is_idempotent(tmp_path):
    # A retried window (lost 200 on the WG path) must NOT double-append: a duplicate
    # inflates net-speech past the 10 s HARD gate and biases the centroid.
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            st, b = await _start(s, base, p)
            sid = b["session"]
            for _ in range(2):                             # SAME seq twice
                async with s.post(base + ew.ENROLL_CHUNK,
                                  params={"session": sid, "seq": "1"},
                                  data=_win(50), headers=_h(_ENROLL, p)) as r:
                    assert r.status == 200
                    body = await r.json()
            assert body.get("duplicate") is True
            assert len(ew._SESSIONS[sid].windows) == 1     # appended ONCE


# ── CAPS: the untested 32 MiB session-bytes branch + the cap_hit logs ─────────

@pytest.mark.asyncio
async def test_session_bytes_cap_refuses_and_logs(tmp_path, monkeypatch):
    # Branch logic (total + incoming > cap) exercised at a small cap; the MEMO LITERAL
    # (32 MiB) is pinned separately in the lockstep test — together they bind both.
    monkeypatch.setattr(ew, "_MAX_SESSION_BYTES", 100_000)
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            st, b = await _start(s, base, p)
            sid = b["session"]
            async with s.post(base + ew.ENROLL_CHUNK, params={"session": sid},
                              data=_win(60), headers=_h(_ENROLL, p)) as r:
                assert r.status == 200                     # 60 KB < 100 KB
            with structlog.testing.capture_logs() as cap:
                async with s.post(base + ew.ENROLL_CHUNK, params={"session": sid},
                                  data=_win(60), headers=_h(_ENROLL, p)) as r:
                    assert r.status == 429                 # 120 KB > 100 KB
    hits = [c for c in cap if c.get("event") == "scribe.enroll.cap_hit"]
    assert any(c.get("cap") == "session_bytes" for c in hits), hits


@pytest.mark.asyncio
async def test_window_and_session_cap_hits_are_logged(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            st, b = await _start(s, base, p)
            sid = b["session"]
            for _ in range(ew._MAX_WINDOWS):
                async with s.post(base + ew.ENROLL_CHUNK, params={"session": sid},
                                  data=_win(1), headers=_h(_ENROLL, p)) as r:
                    assert r.status == 200
            with structlog.testing.capture_logs() as cap:
                async with s.post(base + ew.ENROLL_CHUNK, params={"session": sid},
                                  data=_win(1), headers=_h(_ENROLL, p)) as r:
                    assert r.status == 429
    assert any(c.get("event") == "scribe.enroll.cap_hit" and c.get("cap") == "windows"
               for c in cap)


# ── _sniff_container dispatch (operator ruling 2: iPhone emits mp4/AAC) ───────

def test_sniff_container_dispatch():
    assert ew._sniff_container(b"\x1a\x45\xdf\xa3" + b"rest") == "webm"     # EBML magic
    assert ew._sniff_container(b"\x00\x00\x00\x20ftypM4A ") == "mp4"        # ftyp at [4:8]
    assert ew._sniff_container(b"ftyp" + b"\x00" * 8) is None               # ftyp at [0:4] is NOT mp4
    assert ew._sniff_container(b"RIFFxxxxWAVE") is None                     # unrecognized
    assert ew._sniff_container(b"") is None
    assert ew._sniff_container(b"\x1a\x45") is None                         # too short


# ── 4th advisory gate: self-match headroom on the p10 ────────────────────────

def test_quality_headroom_gate_uses_p10_not_mean():
    tau = 0.75
    # mean is fine, but the WORST windows (p10) sit just above tau → marginal.
    stats = {"net_speech_s": 40.0, "snr_db_est": 20.0,
             "self_sim_mean": 0.95, "self_sim_p10": 0.76, "spread": 0.05}
    advisory, verdict = ew._quality(stats, tau=tau)
    assert advisory["self_sim_ok"] is True          # the mean passes
    assert advisory["headroom_ok"] is False         # p10 < tau + 0.05 → the 4th gate bites
    assert verdict == "ok_marginal"
    # clear headroom → ok
    stats["self_sim_p10"] = 0.90
    advisory, verdict = ew._quality(stats, tau=tau)
    assert advisory["headroom_ok"] is True and verdict == "ok"


@pytest.mark.asyncio
async def test_sample_stats_record_the_self_sim_distribution(tmp_path):
    # The memo requires the RAW FACTS be recorded so the first --calibrate can derive the
    # headroom cut-line from accumulated enrollment data.
    async with _serve(_config(tmp_path)) as (base, cfg):
        async with aiohttp.ClientSession() as s:
            res = await _full(s, base, cfg.ingest_web.port)
    for k in ("n_windows", "duration_s", "net_speech_s", "snr_db_est", "spread",
              "self_sim_mean", "self_sim_p10"):
        assert k in res["stats"], k


# ── audit.log completeness + PHI-free rejected-user ───────────────────────────

@pytest.mark.asyncio
async def test_wrong_token_class_lands_in_the_durable_audit(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            async with s.post(base + ew.ENROLL_START, params={"user": _USER},
                              headers=_h(_INGEST, p)) as r:   # INGEST token on ENROLL route
                assert r.status == 401
    events = _audit_events(tmp_path / "enroll")
    assert any(e["event"] == "wrong_token_class" for e in events), events


@pytest.mark.asyncio
async def test_rejected_user_string_is_never_persisted_verbatim(tmp_path):
    leak = "jamie — patient R. Doe follow-up"
    async with _serve(_config(tmp_path)) as (base, cfg):
        async with aiohttp.ClientSession() as s:
            st, _ = await _start(s, base, cfg.ingest_web.port, user=leak)
            assert st == 403
    raw = (tmp_path / "enroll" / enroll_learning.AUDIT_NAME).read_text(encoding="utf-8")
    assert leak not in raw and "Doe" not in raw
    assert ew._AUDIT_INVALID_USER in raw               # a fixed enum instead


@pytest.mark.asyncio
async def test_rejection_subreasons_are_audited(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        async with aiohttp.ClientSession() as s:
            await _start(s, base, cfg.ingest_web.port,
                         preset="pst-1720000000000-0123456789abcdef")   # unknown_preset
    reasons = {e.get("reason") for e in _audit_events(tmp_path / "enroll")}
    assert "unknown_preset" in reasons


# ── store dir modes (0700) ────────────────────────────────────────────────────

def test_store_root_and_learning_dirs_are_0700(tmp_path):
    root = tmp_path / "enroll"
    # first write under the store is an AUDIT row (a rejected start) — the root must still
    # be created 0700, not at the umask default (0755).
    enroll_learning.audit(str(root), "enroll_rejected", user="(invalid)", reason="x")
    assert oct(os.stat(root).st_mode & 0o777) == "0o700"
    enroll_learning.record_diarize_stats(
        str(root), source_id="enc-x", chunk_seq=1, user=None, preset_id=None,
        centroid_version=None, engine_fingerprint={}, n_segments=0, role_counts={},
        best_cosine=None, separation=None, min_purity=None, fail_closed_demotions=0,
    )
    learning = root / enroll_learning.LEARNING_DIRNAME
    assert oct(os.stat(learning).st_mode & 0o777) == "0o700"
