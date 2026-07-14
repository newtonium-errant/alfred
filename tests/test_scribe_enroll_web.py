"""P4-5a enroll_web routes — two-token split, RAM custody, caps, full flow (fake seam).

Live-server tests (mirror test_scribe_ingest_web). Torch-free: the fake embed
provider skips PyAV decode and embeds raw window bytes. Security-critical surface:
the two-token wrong_token_class 401s, inert-when-tokenless 404, RAM-only custody
(no disk residue), caps, binding lock.
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import asynccontextmanager

import aiohttp
import pytest

from alfred.scribe import embed_voice
from alfred.scribe import enrollment as en
from alfred.scribe import enroll_web as ew
from alfred.scribe.config import (
    ScribeConfig, ScribeDiarizeConfig, ScribeIngestWebConfig, ScribeLlmConfig,
    ScribeSttConfig,
)
from alfred.scribe.ingest_web import IngestWebServer

_SALT = "DUMMY_SCRIBE_TEST_SALT"
_INGEST = "DUMMY_INGEST_TOKEN_0001"
_ENROLL = "DUMMY_ENROLL_TOKEN_0002"
_USER = "np_jamie"
_LABEL = "enc-1720000000000-0123456789abcdef"


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def _config(tmp_path, *, enroll_token=_ENROLL, clinicians=(_USER,), port=None):
    return ScribeConfig(
        mode="synthetic", input_dir=str(tmp_path / "inbox"),
        stt=ScribeSttConfig(provider="fake"),
        llm=ScribeLlmConfig(base_url="http://127.0.0.1:11434", model="m"),
        diarize=ScribeDiarizeConfig(provider="fake", enrollment_dir=str(tmp_path / "enroll")),
        ingest_web=ScribeIngestWebConfig(
            enabled=True, host="127.0.0.1", port=port or _free_port(),
            token=_INGEST, enroll_token=enroll_token,
        ),
        clinicians=list(clinicians), encounter_salt=_SALT,
    )


@asynccontextmanager
async def _serve(config):
    ew._SESSIONS.clear()                    # RAM custody isolation between tests
    server = IngestWebServer(config)
    await server.start()
    try:
        yield f"http://127.0.0.1:{config.ingest_web.port}", config
    finally:
        await server.stop()


def _h(token, port):
    return {"Authorization": f"Bearer {token}", "Host": f"127.0.0.1:{port}"}


def _win(kb):
    return b"a" * (kb * 1024)


async def _enroll_full(sess, base, port, *, kb_each=130, n=4, user=_USER, preset=None, name="Room A"):
    """Run start→chunk×n→finalize→poll. Returns the final result dict."""
    params = {"user": user}
    if preset:
        params["preset"] = preset
    async with sess.post(base + ew.ENROLL_START, params=params, headers=_h(_ENROLL, port)) as r:
        assert r.status == 200, await r.text()
        session = (await r.json())["session"]
    for _ in range(n):
        async with sess.post(base + ew.ENROLL_CHUNK, params={"session": session},
                             data=_win(kb_each), headers=_h(_ENROLL, port)) as r:
            assert r.status == 200, await r.text()
    async with sess.post(base + ew.ENROLL_FINALIZE, params={"session": session},
                         json={"name": name}, headers=_h(_ENROLL, port)) as r:
        assert r.status == 200 and (await r.json())["state"] == "processing"
    for _ in range(200):
        async with sess.get(base + ew.ENROLL_RESULT, params={"session": session},
                            headers=_h(_ENROLL, port)) as r:
            body = await r.json()
        if body.get("state") == "done":
            return body
        await asyncio.sleep(0.01)
    raise AssertionError("finalize did not complete")


# ---------------------------------------------------------------------------
# two-token split (the security heart)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrong_token_class_ingest_token_on_enroll_route_401(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        async with aiohttp.ClientSession() as sess:
            # the INGEST token on an ENROLL route → 401 (wrong_token_class)
            async with sess.post(base + ew.ENROLL_START, params={"user": _USER},
                                 headers=_h(_INGEST, cfg.ingest_web.port)) as r:
                assert r.status == 401


@pytest.mark.asyncio
async def test_wrong_token_class_enroll_token_on_ingest_route_401(tmp_path):
    from alfred.scribe.ingest_web import STATUS_ROUTE
    async with _serve(_config(tmp_path)) as (base, cfg):
        async with aiohttp.ClientSession() as sess:
            # the ENROLL token on an INGEST route (status) → 401
            async with sess.get(base + STATUS_ROUTE, params={"label": _LABEL},
                                headers=_h(_ENROLL, cfg.ingest_web.port)) as r:
                assert r.status == 401


@pytest.mark.asyncio
async def test_presets_list_accepts_either_token(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        async with aiohttp.ClientSession() as sess:
            for tok in (_INGEST, _ENROLL):
                async with sess.get(base + ew.PRESETS_LIST, params={"user": _USER},
                                    headers=_h(tok, cfg.ingest_web.port)) as r:
                    assert r.status == 200


@pytest.mark.asyncio
async def test_enroll_face_inert_when_tokenless(tmp_path):
    async with _serve(_config(tmp_path, enroll_token="")) as (base, cfg):
        async with aiohttp.ClientSession() as sess:
            async with sess.post(base + ew.ENROLL_START, params={"user": _USER},
                                 headers=_h(_INGEST, cfg.ingest_web.port)) as r:
                assert r.status == 404              # inert, not 401


# ---------------------------------------------------------------------------
# full flow + RAM custody + stats
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_enroll_writes_preset_ram_custody_no_disk_residue(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        async with aiohttp.ClientSession() as sess:
            res = await _enroll_full(sess, base, cfg.ingest_web.port, kb_each=130, n=4)
    assert res["verdict"] == "ok" and res["preset_id"].startswith("pst-")
    # the preset file was written
    d = tmp_path / "enroll" / _USER
    files = [p.name for p in d.iterdir()]
    assert files == [f"{res['preset_id']}.json"]
    # RAM custody: NO raw-audio residue anywhere under enrollment_dir (only preset +
    # learning + audit files exist).
    all_paths = list((tmp_path / "enroll").rglob("*"))
    audio_ext = {".webm", ".mp4", ".wav", ".ogg", ".m4a", ".raw", ".pcm", ".tmp"}
    assert not any(p.suffix.lower() in audio_ext for p in all_paths if p.is_file())
    # the session bytes were cleared
    assert all(not s.windows for s in ew._SESSIONS.values())


@pytest.mark.asyncio
async def test_finalize_populates_all_five_sample_stats(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        async with aiohttp.ClientSession() as sess:
            res = await _enroll_full(sess, base, cfg.ingest_web.port, kb_each=130, n=4)
    stats = res["stats"]
    for key in ("n_windows", "duration_s", "net_speech_s", "snr_db_est", "spread"):
        assert key in stats, f"missing sample_stats key {key}"
    assert stats["n_windows"] == 4


@pytest.mark.asyncio
async def test_marginal_verdict_when_short_of_30s(tmp_path):
    # 4×50KB = 200KB → ~12.5s: passes the 10s HARD gate, fails the 30s advisory.
    async with _serve(_config(tmp_path)) as (base, cfg):
        async with aiohttp.ClientSession() as sess:
            res = await _enroll_full(sess, base, cfg.ingest_web.port, kb_each=50, n=4)
    assert res["verdict"] == "ok_marginal"


@pytest.mark.asyncio
async def test_too_short_hard_gate_writes_no_preset(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        async with aiohttp.ClientSession() as sess:
            res = await _enroll_full(sess, base, cfg.ingest_web.port, kb_each=20, n=1)  # ~1.3s
    assert res["verdict"] == "too_short" and "preset_id" not in res
    assert not (tmp_path / "enroll" / _USER).exists()


# ---------------------------------------------------------------------------
# gates + caps
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_refuses_non_clinician(tmp_path):
    async with _serve(_config(tmp_path, clinicians=("someone_else",))) as (base, cfg):
        async with aiohttp.ClientSession() as sess:
            async with sess.post(base + ew.ENROLL_START, params={"user": _USER},
                                 headers=_h(_ENROLL, cfg.ingest_web.port)) as r:
                assert r.status == 403


@pytest.mark.asyncio
async def test_window_count_cap(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        port = cfg.ingest_web.port
        async with aiohttp.ClientSession() as sess:
            async with sess.post(base + ew.ENROLL_START, params={"user": _USER},
                                 headers=_h(_ENROLL, port)) as r:
                session = (await r.json())["session"]
            for i in range(ew._MAX_WINDOWS):
                async with sess.post(base + ew.ENROLL_CHUNK, params={"session": session},
                                     data=_win(1), headers=_h(_ENROLL, port)) as r:
                    assert r.status == 200
            async with sess.post(base + ew.ENROLL_CHUNK, params={"session": session},
                                 data=_win(1), headers=_h(_ENROLL, port)) as r:
                assert r.status == 429              # the (MAX+1)th window


@pytest.mark.asyncio
async def test_session_count_cap(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        port = cfg.ingest_web.port
        async with aiohttp.ClientSession() as sess:
            for _ in range(ew._MAX_SESSIONS):
                async with sess.post(base + ew.ENROLL_START, params={"user": _USER},
                                     headers=_h(_ENROLL, port)) as r:
                    assert r.status == 200
            async with sess.post(base + ew.ENROLL_START, params={"user": _USER},
                                 headers=_h(_ENROLL, port)) as r:
                assert r.status == 429


@pytest.mark.asyncio
async def test_abandon_drops_session(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        port = cfg.ingest_web.port
        async with aiohttp.ClientSession() as sess:
            async with sess.post(base + ew.ENROLL_START, params={"user": _USER},
                                 headers=_h(_ENROLL, port)) as r:
                session = (await r.json())["session"]
            async with sess.post(base + ew.ENROLL_ABANDON, params={"session": session},
                                 headers=_h(_ENROLL, port)) as r:
                assert r.status == 200
            async with sess.get(base + ew.ENROLL_RESULT, params={"session": session},
                                headers=_h(_ENROLL, port)) as r:
                assert (await r.json())["state"] == "unknown_session"


# ---------------------------------------------------------------------------
# encounter/preset binding lock + preset_fit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_binding_lock_and_preset_fit(tmp_path):
    from alfred.scribe.ingest_web import STATUS_ROUTE
    async with _serve(_config(tmp_path)) as (base, cfg):
        port = cfg.ingest_web.port
        async with aiohttp.ClientSession() as sess:
            res = await _enroll_full(sess, base, port, kb_each=130, n=4)
            pid = res["preset_id"]
            # bind (ingest token)
            async with sess.post(base + ew.ENCOUNTER_PRESET, params={"label": _LABEL, "preset": pid},
                                 headers=_h(_INGEST, port)) as r:
                assert r.status == 200 and (await r.json())["state"] == "bound"
            # same preset again → idempotent 200
            async with sess.post(base + ew.ENCOUNTER_PRESET, params={"label": _LABEL, "preset": pid},
                                 headers=_h(_INGEST, port)) as r:
                assert r.status == 200
            # a DIFFERENT preset on the locked encounter → 409
            res2 = await _enroll_full(sess, base, port, kb_each=130, n=4, name="Room B")
            async with sess.post(base + ew.ENCOUNTER_PRESET,
                                 params={"label": _LABEL, "preset": res2["preset_id"]},
                                 headers=_h(_INGEST, port)) as r:
                assert r.status == 409
            # preset_fit flips unarmed→ok on the bound encounter
            async with sess.get(base + STATUS_ROUTE, params={"label": _LABEL},
                                headers=_h(_INGEST, port)) as r:
                assert (await r.json())["preset_fit"] == "ok"


@pytest.mark.asyncio
async def test_rerecord_bumps_centroid_version(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        port = cfg.ingest_web.port
        async with aiohttp.ClientSession() as sess:
            res = await _enroll_full(sess, base, port, kb_each=130, n=4)
            pid = res["preset_id"]
            res2 = await _enroll_full(sess, base, port, kb_each=130, n=4, preset=pid, name="Room A v2")
    assert res2["preset_id"] == pid                 # SAME id
    preset, _ = en.load_preset(en.preset_path(tmp_path / "enroll", _USER, pid))
    assert preset.centroid_version == 2             # bumped


@pytest.mark.asyncio
async def test_presets_list_never_returns_centroid(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        port = cfg.ingest_web.port
        async with aiohttp.ClientSession() as sess:
            await _enroll_full(sess, base, port, kb_each=130, n=4)
            async with sess.get(base + ew.PRESETS_LIST, params={"user": _USER},
                                headers=_h(_ENROLL, port)) as r:
                body = await r.json()
    assert body["state"] == "ok" and len(body["presets"]) == 1
    p = body["presets"][0]
    assert "centroids" not in p and "centroid_digest" not in p   # R2 extended to biometrics
