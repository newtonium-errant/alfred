"""P4-5a enroll_web — CONTRACT-FIRST verification (memo-derived, not WIP-mirrored).

These tests bind the FROZEN CONTRACT in project_p45_enrollment_design (ROUTES+AUTH
table, two-token split, RAM custody, verdict set, binding lock) — authored from the
memo, NOT by reading the (saturated-builder) implementation, so they FAIL if the
committed code diverges from the spec. They deliberately target the surface a
code-mirroring test set passes-by-construction over:

  * the FULL token-class matrix incl. the EMPTY-token branch and every route class
    (enroll / ingest / EITHER-token list / encounter-select);
  * inert-404-BEFORE-401 precedence when enroll_token is unset (even a VALID ingest
    token on an enroll-face path → 404, not 401);
  * chunk POSTs carry ZERO preset semantics (provably preset-blind — a `?preset=`
    on an ingest chunk never re-binds and never 409s);
  * 409 preset_locked lives ONLY on the selection route;
  * bytes cleared on EVERY session-terminating exit (finalize-success, finalize-FAIL,
    TTL sweep, abandon) + a cap-refusal never retains the rejected window;
  * the degenerate verdicts (no_speech / engine_error) + the 5-key sample_stats pin
    on a NON-ok path;
  * PHI-free logs/audit — preset_id only, NEVER a name/label in any enroll event
    (log-emission pinned via capture, per feedback_log_emission_test_pattern).
"""

from __future__ import annotations

import asyncio
import socket
import time
from contextlib import asynccontextmanager

import aiohttp
import pytest
import structlog

from alfred.scribe import embed_voice
from alfred.scribe import enroll_web as ew
from alfred.scribe import enrollment as en
from alfred.scribe.config import (
    ScribeConfig, ScribeDiarizeConfig, ScribeIngestWebConfig, ScribeLlmConfig,
    ScribeSttConfig,
)
from alfred.scribe.ingest_web import (
    CLOSE_ROUTE, INGEST_CHUNK_ROUTE, STATUS_ROUTE, IngestWebServer, create_ingest_app,
)

# Obviously-fake credential-shaped literals (builder.md test-fixture rule — no realistic
# provider prefix, so the scanner can't mistake them for real leaked keys).
_SALT = "DUMMY_SCRIBE_TEST_SALT"
_INGEST = "DUMMY_INGEST_TOKEN_0001"
_ENROLL = "DUMMY_ENROLL_TOKEN_0002"
_GARBAGE = "DUMMY_GARBAGE_TOKEN_XXXX"
_USER = "np_jamie"
_LABEL = "enc-1720000000000-0123456789abcdef"


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close()
    return p


def _config(tmp_path, *, enroll_token=_ENROLL, clinicians=(_USER,), port=None) -> ScribeConfig:
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
    ew._SESSIONS.clear()                        # RAM custody isolation between tests
    server = IngestWebServer(config)
    await server.start()
    try:
        yield f"http://127.0.0.1:{config.ingest_web.port}", config
    finally:
        await server.stop()
        ew._SESSIONS.clear()


def _h(token, port):
    """Auth+Host headers. token=None → NO Authorization header (the empty branch)."""
    h = {"Host": f"127.0.0.1:{port}"}
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    return h


def _win(kb: int) -> bytes:
    return b"a" * (kb * 1024)


async def _start(sess, base, port, *, user=_USER, token=_ENROLL, preset=None):
    params = {"user": user}
    if preset:
        params["preset"] = preset
    async with sess.post(base + ew.ENROLL_START, params=params, headers=_h(token, port)) as r:
        return r.status, (await r.json() if r.content_type == "application/json" else None)


async def _enroll_full(sess, base, port, *, kb_each=130, n=4, name="Room A", preset=None):
    st, body = await _start(sess, base, port, preset=preset)
    assert st == 200, body
    session = body["session"]
    for _ in range(n):
        async with sess.post(base + ew.ENROLL_CHUNK, params={"session": session},
                             data=_win(kb_each), headers=_h(_ENROLL, port)) as r:
            assert r.status == 200, await r.text()
    async with sess.post(base + ew.ENROLL_FINALIZE, params={"session": session},
                         json={"name": name}, headers=_h(_ENROLL, port)) as r:
        assert r.status == 200
    for _ in range(300):
        async with sess.get(base + ew.ENROLL_RESULT, params={"session": session},
                            headers=_h(_ENROLL, port)) as r:
            body = await r.json()
        if body.get("state") == "done":
            return body
        await asyncio.sleep(0.01)
    raise AssertionError("finalize did not complete")


# ═══════════════════════════════════════════════════════════════════════════
# TOKEN-CLASS MATRIX — right / wrong-class / empty / garbage × each route class
# ═══════════════════════════════════════════════════════════════════════════
# Contract (ROUTES+AUTH table + two-token split): each route class pins ITS token;
# a token valid for the OTHER class → 401 (wrong_token_class log); empty/garbage →
# 401 (bad_token). GET /scribe/presets clears on EITHER token.

# Representative routes per class (method, path, class).
_ENROLL_ROUTE = ("post", ew.ENROLL_START, {"user": _USER})
_INGEST_ROUTE = ("get", STATUS_ROUTE, {"label": _LABEL})           # ingest-class, side-effect-free
_ENCPRESET_ROUTE = ("post", ew.ENCOUNTER_PRESET, {"label": _LABEL, "preset": "pst-0000000000000-0123456789abcdef"})
_LIST_ROUTE = ("get", ew.PRESETS_LIST, {"user": _USER})


async def _call(sess, base, port, route, token):
    method, path, params = route
    fn = sess.get if method == "get" else sess.post
    async with fn(base + path, params=params, headers=_h(token, port)) as r:
        return r.status


@pytest.mark.asyncio
async def test_matrix_enroll_route_requires_enroll_token(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            assert await _call(s, base, p, _ENROLL_ROUTE, _ENROLL) == 200      # right class
            assert await _call(s, base, p, _ENROLL_ROUTE, _INGEST) == 401      # wrong class
            assert await _call(s, base, p, _ENROLL_ROUTE, _GARBAGE) == 401     # garbage
            assert await _call(s, base, p, _ENROLL_ROUTE, None) == 401         # empty (no header)


@pytest.mark.asyncio
async def test_matrix_ingest_route_requires_ingest_token(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            assert await _call(s, base, p, _INGEST_ROUTE, _INGEST) == 200      # right class (status)
            assert await _call(s, base, p, _INGEST_ROUTE, _ENROLL) == 401      # wrong class
            assert await _call(s, base, p, _INGEST_ROUTE, _GARBAGE) == 401     # garbage
            assert await _call(s, base, p, _INGEST_ROUTE, None) == 401         # empty


@pytest.mark.asyncio
async def test_matrix_encounter_preset_is_ingest_class(tmp_path):
    # The selection route is ENCOUNTER-class (ingest token), NOT enroll — an enroll
    # token here is wrong_token_class. (preset id absent → the request never reaches
    # a 2xx; we assert only the AUTH outcome: enroll token is refused, ingest passes auth.)
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            assert await _call(s, base, p, _ENCPRESET_ROUTE, _ENROLL) == 401   # wrong class
            assert await _call(s, base, p, _ENCPRESET_ROUTE, None) == 401      # empty
            # ingest token CLEARS auth (409 preset_unusable — the preset doesn't exist —
            # is a post-auth outcome, NOT 401; that's the point: auth passed).
            assert await _call(s, base, p, _ENCPRESET_ROUTE, _INGEST) != 401


@pytest.mark.asyncio
async def test_matrix_presets_list_accepts_either_token_but_not_garbage(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            assert await _call(s, base, p, _LIST_ROUTE, _ENROLL) == 200        # EITHER
            assert await _call(s, base, p, _LIST_ROUTE, _INGEST) == 200        # EITHER
            assert await _call(s, base, p, _LIST_ROUTE, _GARBAGE) == 401       # neither
            assert await _call(s, base, p, _LIST_ROUTE, None) == 401           # empty


@pytest.mark.asyncio
async def test_wrong_token_class_emits_log(tmp_path):
    # Per feedback_log_emission_test_pattern: the wrong-but-valid-token 401 MUST log
    # reason="wrong_token_class" (the *_wrong_peer analog — a privilege boundary, not
    # a typo). Drive the production path + pin the emission.
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        with structlog.testing.capture_logs() as cap:
            async with aiohttp.ClientSession() as s:
                await _call(s, base, p, _ENROLL_ROUTE, _INGEST)   # ingest token on enroll route
    rejected = [c for c in cap if c.get("event") == "scribe.ingest_web.rejected"]
    assert any(c.get("reason") == "wrong_token_class" for c in rejected), rejected


# ═══════════════════════════════════════════════════════════════════════════
# INERT-404 PRECEDENCE — enroll_token unset ⇒ the enroll FACE is ABSENT (404),
# BEFORE token classification. Ingest routes are unaffected.
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_inert_404_beats_401_even_with_valid_ingest_token(tmp_path):
    # The biometric face is INERT (enroll_token=""). An enroll-face path 404s
    # REGARDLESS of the token — even a VALID ingest token (404 precedence over 401).
    async with _serve(_config(tmp_path, enroll_token="")) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            assert await _call(s, base, p, _ENROLL_ROUTE, _INGEST) == 404   # valid ingest → still 404
            assert await _call(s, base, p, _ENROLL_ROUTE, _ENROLL) == 404   # the (absent) enroll tok
            assert await _call(s, base, p, _ENROLL_ROUTE, None) == 404      # no token
            # presets list is part of the enrollment face → 404 when inert (even ingest tok).
            assert await _call(s, base, p, _LIST_ROUTE, _INGEST) == 404


@pytest.mark.asyncio
async def test_inert_face_does_not_disarm_ingest_routes(tmp_path):
    # Tokenless enroll face must NOT break the ingest side: status still requires the
    # ingest token (401 on wrong/none), and clears on the right one.
    async with _serve(_config(tmp_path, enroll_token="")) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            assert await _call(s, base, p, _INGEST_ROUTE, _INGEST) == 200   # ingest still works
            assert await _call(s, base, p, _INGEST_ROUTE, None) == 401      # still bearer-required


@pytest.mark.asyncio
async def test_inert_startup_log_emitted_when_tokenless(tmp_path):
    # create_ingest_app emits scribe.enroll.inert exactly once when enroll_token unset.
    with structlog.testing.capture_logs() as cap:
        create_ingest_app(_config(tmp_path, enroll_token=""))
    inert = [c for c in cap if c.get("event") == "scribe.enroll.inert"]
    assert len(inert) == 1, inert
    # ...and NOT emitted when armed.
    with structlog.testing.capture_logs() as cap2:
        create_ingest_app(_config(tmp_path))
    assert not [c for c in cap2 if c.get("event") == "scribe.enroll.inert"]


# ═══════════════════════════════════════════════════════════════════════════
# CHUNK POSTs ARE PRESET-BLIND — the binding is written ONLY by the selection
# route; an ingest chunk POST carries ZERO preset semantics (never re-binds, never 409).
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_chunk_post_is_preset_blind(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            # enroll TWO presets, bind A to the encounter.
            a = await _enroll_full(s, base, p, name="A")
            b = await _enroll_full(s, base, p, name="B")
            async with s.post(base + ew.ENCOUNTER_PRESET,
                              params={"label": _LABEL, "preset": a["preset_id"]},
                              headers=_h(_INGEST, p)) as r:
                assert r.status == 200
            # POST an ingest chunk with a spurious ?preset=<B> — it must be IGNORED
            # (the chunk route has no preset semantics): 200, no re-bind, no 409.
            async with s.post(base + INGEST_CHUNK_ROUTE,
                              params={"label": _LABEL, "seq": "1", "ext": "webm",
                                      "synthetic": "true", "preset": b["preset_id"]},
                              data=b"AUDIO", headers=_h(_INGEST, p)) as r:
                assert r.status == 200, await r.text()
    # the binding on disk is STILL A (the chunk's ?preset=B did nothing).
    binding = en.read_binding(tmp_path / "inbox" / _LABEL)
    assert binding is not None and binding["preset_id"] == a["preset_id"]


@pytest.mark.asyncio
async def test_409_preset_locked_only_on_selection_route_and_logs(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            a = await _enroll_full(s, base, p, name="A")
            b = await _enroll_full(s, base, p, name="B")
            async with s.post(base + ew.ENCOUNTER_PRESET,
                              params={"label": _LABEL, "preset": a["preset_id"]},
                              headers=_h(_INGEST, p)) as r:
                assert r.status == 200
            # same pair → idempotent 200 (no 409).
            async with s.post(base + ew.ENCOUNTER_PRESET,
                              params={"label": _LABEL, "preset": a["preset_id"]},
                              headers=_h(_INGEST, p)) as r:
                assert r.status == 200
            # DIFFERENT pair → 409 preset_locked + loud log (only HERE).
            with structlog.testing.capture_logs() as cap:
                async with s.post(base + ew.ENCOUNTER_PRESET,
                                  params={"label": _LABEL, "preset": b["preset_id"]},
                                  headers=_h(_INGEST, p)) as r:
                    assert r.status == 409
    assert any(c.get("event") == "scribe.enroll.rejected" and c.get("reason") == "preset_locked"
               for c in cap), cap


# ═══════════════════════════════════════════════════════════════════════════
# RAM CUSTODY — bytes cleared on EVERY session-terminating exit path
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_bytes_cleared_on_finalize_success(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            await _enroll_full(s, base, p, kb_each=130, n=4)
        assert all(not sess.windows for sess in ew._SESSIONS.values())


@pytest.mark.asyncio
async def test_bytes_cleared_on_finalize_failure(tmp_path, monkeypatch):
    # Force the embed engine to RAISE mid-finalize → verdict engine_error, and the
    # session bytes MUST still be cleared (RAM custody holds on the failure path too).
    def _boom(config, windows):
        raise RuntimeError("engine exploded")
    monkeypatch.setattr(embed_voice, "embed_windows", _boom)
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            res = await _enroll_full(s, base, p, kb_each=130, n=4)
        assert res["verdict"] == "engine_error" and "preset_id" not in res
        assert all(not sess.windows for sess in ew._SESSIONS.values())
        # no preset persisted on the failure path.
        assert not (tmp_path / "enroll" / _USER).exists()


@pytest.mark.asyncio
async def test_bytes_cleared_on_abandon(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            _, body = await _start(s, base, p)
            session = body["session"]
            async with s.post(base + ew.ENROLL_CHUNK, params={"session": session},
                             data=_win(50), headers=_h(_ENROLL, p)) as r:
                assert r.status == 200
            held = ew._SESSIONS[session]
            assert held.windows                      # bytes present before abandon
            async with s.post(base + ew.ENROLL_ABANDON, params={"session": session},
                             headers=_h(_ENROLL, p)) as r:
                assert r.status == 200
            assert session not in ew._SESSIONS       # dropped
            assert not held.windows                  # and the held reference is cleared


@pytest.mark.asyncio
async def test_bytes_cleared_on_ttl_sweep(tmp_path):
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            _, body = await _start(s, base, p)
            session = body["session"]
            async with s.post(base + ew.ENROLL_CHUNK, params={"session": session},
                             data=_win(50), headers=_h(_ENROLL, p)) as r:
                assert r.status == 200
        held = ew._SESSIONS[session]
        # age the session past the TTL, then sweep (the sweep runs on every route access).
        held.created_at = time.monotonic() - ew._SESSION_TTL_S - 1
        ew._sweep_expired()
        assert session not in ew._SESSIONS and not held.windows


@pytest.mark.asyncio
async def test_cap_refusal_does_not_retain_rejected_window(tmp_path):
    # An oversized window is 429'd and must NOT be appended (the rejected bytes are
    # never retained in the session). The session stays live (a cap refusal is not a
    # terminating exit — the client may keep going or abandon).
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            _, body = await _start(s, base, p)
            session = body["session"]
            before = ew._SESSIONS[session].total_bytes()
            async with s.post(base + ew.ENROLL_CHUNK, params={"session": session},
                             data=_win((ew._MAX_WINDOW_BYTES // 1024) + 64),
                             headers=_h(_ENROLL, p)) as r:
                assert r.status == 429
            assert ew._SESSIONS[session].total_bytes() == before   # rejected bytes not retained
            assert session in ew._SESSIONS                          # session still live


# ═══════════════════════════════════════════════════════════════════════════
# VERDICT PATHS — degenerate hard gates + the 5-key sample_stats pin on a NON-ok path
# ═══════════════════════════════════════════════════════════════════════════

_STAT_KEYS = ("n_windows", "duration_s", "net_speech_s", "snr_db_est", "spread")


@pytest.mark.asyncio
async def test_no_speech_verdict_zero_windows(tmp_path):
    # finalize with ZERO windows → no_speech (a HARD degenerate gate), no preset, and
    # ALL 5 sample_stats keys STILL present (a finalize regression can't silently drop them).
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            _, body = await _start(s, base, p)
            session = body["session"]
            async with s.post(base + ew.ENROLL_FINALIZE, params={"session": session},
                             json={"name": "x"}, headers=_h(_ENROLL, p)) as r:
                assert r.status == 200
            for _ in range(300):
                async with s.get(base + ew.ENROLL_RESULT, params={"session": session},
                                headers=_h(_ENROLL, p)) as r:
                    res = await r.json()
                if res.get("state") == "done":
                    break
                await asyncio.sleep(0.01)
    assert res["verdict"] == "no_speech" and "preset_id" not in res
    assert all(k in res["stats"] for k in _STAT_KEYS)


@pytest.mark.asyncio
async def test_ok_marginal_populates_all_five_stats(tmp_path):
    # ~12.5 s (4×50KB): clears the 10 s HARD gate, fails the 30 s advisory → ok_marginal
    # + a PERSISTED preset with the badge (a marginal preset must persist — calibrate
    # needs its own data). All 5 stats present.
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            res = await _enroll_full(s, base, p, kb_each=50, n=4)
    assert res["verdict"] == "ok_marginal" and res["preset_id"].startswith("pst-")
    assert all(k in res["stats"] for k in _STAT_KEYS)
    preset, _ = en.load_preset(en.preset_path(tmp_path / "enroll", _USER, res["preset_id"]))
    assert preset.quality["verdict"] == "ok_marginal"


# ═══════════════════════════════════════════════════════════════════════════
# PHI-FREE — preset_id ONLY, never a name/label in the audit OR any enroll structlog
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_no_name_in_audit_or_structlog(tmp_path):
    secret_name = "MRS-SMITH-ROOM-3B"        # a name that must NEVER appear in logs/audit
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        with structlog.testing.capture_logs() as cap:
            async with aiohttp.ClientSession() as s:
                res = await _enroll_full(s, base, p, name=secret_name)
                # also drive a rename + a select, which audit.
                async with s.post(base + ew.PRESETS_RENAME,
                                  params={"user": _USER, "preset": res["preset_id"]},
                                  json={"name": secret_name + "-v2"}, headers=_h(_ENROLL, p)) as r:
                    assert r.status == 200
                async with s.post(base + ew.ENCOUNTER_PRESET,
                                  params={"label": _LABEL, "preset": res["preset_id"]},
                                  headers=_h(_INGEST, p)) as r:
                    assert r.status == 200
    # (1) NO structlog event anywhere carries the name (in any field value).
    for c in cap:
        for v in c.values():
            assert secret_name not in str(v), f"name leaked into structlog event {c}"
    # (2) the on-disk audit.log carries preset_id but NEVER the name.
    audit = (tmp_path / "enroll" / "audit.log").read_text(encoding="utf-8")
    assert res["preset_id"] in audit          # id IS recorded (join-at-display)
    assert secret_name not in audit           # the name is NEVER recorded


@pytest.mark.asyncio
async def test_preset_bound_open_encounter_refuses_rerecord(tmp_path):
    # Re-record refuses (409) while the preset is bound to an OPEN (un-_CLOSED)
    # encounter — a mid-encounter swap must never re-anchor a live recording.
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            res = await _enroll_full(s, base, p, name="A")
            pid = res["preset_id"]
            async with s.post(base + ew.ENCOUNTER_PRESET,
                              params={"label": _LABEL, "preset": pid},
                              headers=_h(_INGEST, p)) as r:
                assert r.status == 200                 # bound, encounter still OPEN (no _CLOSED)
            st, _ = await _start(s, base, p, preset=pid)
            assert st == 409                           # re-record refused while bound-open


# ═══════════════════════════════════════════════════════════════════════════
# MEMO-LITERAL LOCKSTEP — the RAM caps + enrollment gates are module constants;
# pin the FROZEN MEMO's LITERAL values here (NOT ew._MAX_* — a code-derived pin
# passes even if a constant drifted from the contract). Constant-vs-memo drift → red.
# ═══════════════════════════════════════════════════════════════════════════

def test_ram_custody_caps_match_memo_literals():
    # project_p45_enrollment_design: 2 sessions / 8 windows / 8 MiB per window /
    # 32 MiB per session / 10 min TTL.
    assert ew._MAX_SESSIONS == 2
    assert ew._MAX_WINDOWS == 8
    assert ew._MAX_WINDOW_BYTES == 8 * 1024 * 1024
    assert ew._MAX_SESSION_BYTES == 32 * 1024 * 1024
    assert ew._SESSION_TTL_S == 600


def test_enrollment_gates_match_memo_literals():
    # HARD gate = <10 s net speech → too_short; advisory-until-calibrate target 30 s,
    # SNR ≥10 dB, self-sim ≥0.80 (calibrate flips the advisory ones hard on-box).
    assert ew._MIN_NET_SPEECH_S == 10.0
    assert ew._TARGET_DURATION_S == 30.0
    assert ew._ADVISORY_SNR_DB == 10.0
    assert ew._ADVISORY_SELF_SIM == 0.80


def test_fake_bytes_per_sec_is_fake_path_only():
    # Audit sharpener: _FAKE_BYTES_PER_SEC is CI test math ONLY. The real (pyannote)
    # net-speech proxy uses a SEPARATE on-box placeholder constant, so the fake constant
    # never leaks into the real too_short contract surface. (Source-level pin: the fake
    # constant appears exactly once in _prepare_windows — the fake branch.)
    import inspect
    src = inspect.getsource(ew._prepare_windows)
    assert src.count("_FAKE_BYTES_PER_SEC") == 1                       # fake branch only
    assert "_ONBOX_NET_SPEECH_PLACEHOLDER_BYTES_PER_SEC" in src        # real branch uses the on-box one


# ═══════════════════════════════════════════════════════════════════════════
# SERVER-SIDE MRU (the picker's default) — R5 forbids the client remembering it
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_mru_is_the_most_recently_bound_usable_preset(tmp_path):
    # The PWA picker pre-selects the MRU. R5 is ABSOLUTE (no localStorage), so the
    # client CANNOT remember the last choice — the server derives it from the BINDING
    # files (the real "last used", not merely the last edited).
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            a = await _enroll_full(s, base, p, name="A")
            b = await _enroll_full(s, base, p, name="B")
            # with NO binding yet there is no MRU (the picker defaults to "no preset").
            async with s.get(base + ew.PRESETS_LIST, params={"user": _USER},
                             headers=_h(_ENROLL, p)) as r:
                assert (await r.json())["mru_preset_id"] is None
            # bind B to an encounter → B becomes the MRU.
            async with s.post(base + ew.ENCOUNTER_PRESET,
                              params={"label": _LABEL, "preset": b["preset_id"]},
                              headers=_h(_INGEST, p)) as r:
                assert r.status == 200
            async with s.get(base + ew.PRESETS_LIST, params={"user": _USER},
                             headers=_h(_ENROLL, p)) as r:
                body = await r.json()
    assert body["mru_preset_id"] == b["preset_id"]
    assert body["mru_preset_id"] != a["preset_id"]


@pytest.mark.asyncio
async def test_mru_never_offers_an_unusable_preset(tmp_path):
    # A revoked/incompatible preset must NOT be the default — it would strand the
    # clinician on a preset they must re-record before it can attribute anything.
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            a = await _enroll_full(s, base, p, name="A")
            async with s.post(base + ew.ENCOUNTER_PRESET,
                              params={"label": _LABEL, "preset": a["preset_id"]},
                              headers=_h(_INGEST, p)) as r:
                assert r.status == 200
            # now DELETE it (tombstone) — the binding still names it, but it is unusable.
            en.revoke_preset(cfg.diarize.enrollment_dir, _USER, a["preset_id"],
                             reason="user_delete")
            async with s.get(base + ew.PRESETS_LIST, params={"user": _USER},
                             headers=_h(_ENROLL, p)) as r:
                body = await r.json()
    assert body["mru_preset_id"] is None            # not offered as the default


@pytest.mark.asyncio
async def test_mru_survives_a_corrupt_binding_preset_id(tmp_path):
    # W1 — the narrower-guard class AGAIN: `pid in usable_ids` raises TypeError on an
    # UNHASHABLE preset_id (a hand-edited / torn binding can carry a list or dict), which
    # escaped the OSError-only guard and 500'd the presets route — taking down the list
    # the whole UI depends on. The MRU is a CONVENIENCE: any problem means "no default".
    import json as _json
    async with _serve(_config(tmp_path)) as (base, cfg):
        p = cfg.ingest_web.port
        async with aiohttp.ClientSession() as s:
            a = await _enroll_full(s, base, p, name="A")
            enc = tmp_path / "inbox" / _LABEL
            enc.mkdir(parents=True, exist_ok=True)
            # an UNHASHABLE preset_id (list) in an otherwise well-formed binding
            en.binding_path(enc).write_text(_json.dumps({
                "schema_version": 1, "user": _USER, "preset_id": ["not", "a", "string"],
                "centroid_version": 1, "centroid_digest": "x", "bound_at": "2026-07-14T00:00:00Z",
            }), encoding="utf-8")
            async with s.get(base + ew.PRESETS_LIST, params={"user": _USER},
                             headers=_h(_ENROLL, p)) as r:
                assert r.status == 200                 # NOT a 500
                body = await r.json()
    assert body["mru_preset_id"] is None               # no default, list still served
    assert len(body["presets"]) == 1 and body["presets"][0]["preset_id"] == a["preset_id"]
