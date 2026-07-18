"""#12 slice 12c — consent gate + state machine + withdrawal ordering (design §4/§5/§6).

Drives the real loopback ingest server in CLINICAL mode (the gate is clinical-only) with an
active facade, and pins:

  * the consent route (confirmed/declined/withdrawn) — records the durable event, resolves
    ``captured_by`` from the session, illegal transitions → 409, no session → 401, no store → 503;
  * the chunk-ingest fail-closed belt — a chunk with no ``consent.confirmed`` → 403
    ``consent_required`` + ``consent.violation_refused``; confirmed → seqs accepted;
  * the withdrawal ordering contract (§5) — durable append lands before the ack; a store failure
    → 5xx with NO cache flip and capture NOT halted; ``at_seq`` == on-disk max at withdrawal;
  * the per-encounter lock race invariant — no chunk lands beyond the withdrawn boundary;
  * server-restart robustness — the empty cache falls back to the durable state (§5.4);
  * synthetic mode is NOT gated (a synthetic/test encounter has no consent flow, §7.2/§10).
"""
from __future__ import annotations

import asyncio
import secrets
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import pytest
import structlog

from alfred.evstore import EventStoreError
from alfred.scribe import compute_encounter_id
from alfred.scribe import ingest_web as iw
from alfred.scribe.config import (
    ScribeConfig,
    ScribeIngestWebConfig,
    ScribeLlmConfig,
    ScribeSttConfig,
)
from alfred.scribe.events import ScribeEvents
from alfred.scribe.ingest_web import IngestWebServer

_SALT = "DUMMY_SCRIBE_TEST_SALT"
_TOKEN = "tok-" + secrets.token_hex(8)
_LABEL = "enc-1720000000000-0123456789abcdef"
_CLOCK = "2026-07-18T12:00:00+00:00"
_CLINICIANS = ["jdoe", "asmith"]


@pytest.fixture(autouse=True)
def _clear_pwa_sessions():
    iw._PWA_SESSIONS.clear()          # module-global; the app-scoped consent cache/locks reset per server
    yield
    iw._PWA_SESSIONS.clear()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _config(tmp_path, *, mode="clinical", clinicians=None, **web_over):
    return ScribeConfig(
        mode=mode, input_dir=str(tmp_path / "inbox"),
        stt=ScribeSttConfig(provider="fake"),
        llm=ScribeLlmConfig(base_url="http://127.0.0.1:11434", model="m"),
        ingest_web=ScribeIngestWebConfig(
            enabled=True, host="127.0.0.1", port=_free_port(), token=_TOKEN, **web_over),
        encounter_salt=_SALT,
        clinicians=_CLINICIANS if clinicians is None else clinicians,
    )


def _facade(tmp_path):
    raw = {"scribe": {"mode": "clinical", "encounter_salt": _SALT,
                      "events": {"dir": str(tmp_path / "ev")}}}
    return ScribeEvents.from_config(raw, log_dir=str(tmp_path / "logs"), clock=lambda: _CLOCK)


@asynccontextmanager
async def _serve(config, events):
    server = IngestWebServer(config, events=events)
    await server.start()
    try:
        yield f"http://127.0.0.1:{config.ingest_web.port}"
    finally:
        await server.stop()


def _auth():
    return {"Authorization": f"Bearer {_TOKEN}"}


def _eid():
    return compute_encounter_id(_LABEL, salt=_SALT)


async def _open_session(s, base, user="jdoe"):
    async with s.post(base + iw.SESSION_OPEN_ROUTE, params={"user": user}, headers=_auth()) as r:
        return (await r.json())["session"]


async def _post_consent(s, base, decision, *, session=None, label=_LABEL):
    hdr = dict(_auth())
    if session:
        hdr[iw.SESSION_HEADER] = session
    async with s.post(base + iw.CONSENT_ROUTE, params={"label": label, "decision": decision},
                      headers=hdr) as r:
        body = await r.json()
        return r.status, body


async def _post_chunk(s, base, *, seq=1, session=None, label=_LABEL, close=None, body=b"AUDIOBYTES"):
    params = {"label": label, "seq": str(seq), "ext": "webm"}
    if close is not None:
        params["close"] = close
    hdr = dict(_auth())
    if session:
        hdr[iw.SESSION_HEADER] = session
    async with s.post(base + iw.INGEST_CHUNK_ROUTE, params=params, data=body, headers=hdr) as r:
        return r.status


# ── consent route + state machine ───────────────────────────────────────────

def test_consent_confirmed_records_and_gates_open(tmp_path):
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            tok = await _open_session(s, base)
            status, body = await _post_consent(s, base, "confirmed", session=tok)
            assert status == 200 and body["captured_by"] == "jdoe" and body["decision"] == "confirmed"
            # cache now confirmed → chunks flow.
            assert await _post_chunk(s, base, seq=1, session=tok) == 200
    with structlog.testing.capture_logs() as cap:
        asyncio.run(_run())
    conf = ev.query("clinical", kind="consent.confirmed")
    assert len(conf) == 1 and conf[0]["subject_id"] == _eid()
    assert conf[0]["payload"] == {"method": "verbal", "captured_by": "jdoe"}
    assert any(c.get("event") == "scribe.consent.recorded" and c.get("decision") == "confirmed"
               for c in cap)


def test_consent_confirmed_requires_session(tmp_path):
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            status, body = await _post_consent(s, base, "confirmed")   # no X-Scribe-Session
            assert status == 401 and body["error"] == "no_session"
    with structlog.testing.capture_logs() as cap:
        asyncio.run(_run())
    assert ev.query("clinical", family="consent") == []     # nothing recorded on a refusal
    assert any(c.get("event") == "scribe.consent.no_session" for c in cap)


def test_consent_invalid_decision_400(tmp_path):
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            tok = await _open_session(s, base)
            status, body = await _post_consent(s, base, "maybe", session=tok)
            assert status == 400 and body["error"] == "invalid_decision"
    asyncio.run(_run())


def test_consent_invalid_label_400(tmp_path):
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            tok = await _open_session(s, base)
            status, body = await _post_consent(s, base, "confirmed", session=tok, label="NOT-A-LABEL")
            assert status == 400 and body["error"] == "invalid_label"
    asyncio.run(_run())


def test_consent_illegal_transition_409(tmp_path):
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            tok = await _open_session(s, base)
            assert (await _post_consent(s, base, "confirmed", session=tok))[0] == 200
            status, body = await _post_consent(s, base, "confirmed", session=tok)   # double confirm
            assert status == 409 and body["error"] == "illegal_transition"
    with structlog.testing.capture_logs() as cap:
        asyncio.run(_run())
    assert len(ev.query("clinical", kind="consent.confirmed")) == 1     # the 2nd never recorded
    assert any(c.get("event") == "scribe.consent.illegal_transition" for c in cap)


def test_consent_events_inactive_503(tmp_path):
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, None) as base, aiohttp.ClientSession() as s:  # NO facade
            tok = await _open_session(s, base)
            status, body = await _post_consent(s, base, "confirmed", session=tok)
            assert status == 503 and body["error"] == "consent_unavailable"
    asyncio.run(_run())


def test_consent_declined_terminal_and_chunk_refused(tmp_path):
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            tok = await _open_session(s, base)
            assert (await _post_consent(s, base, "declined", session=tok))[0] == 200
            # a declined encounter never opens the mic — a chunk POST is refused.
            assert await _post_chunk(s, base, seq=1, session=tok) == 403
    asyncio.run(_run())
    assert len(ev.query("clinical", kind="consent.declined")) == 1


# ── chunk gate ──────────────────────────────────────────────────────────────

def test_chunk_refused_without_consent_emits_violation(tmp_path):
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            # no consent at all → the very first chunk is refused (the structural guarantee §6.1).
            assert await _post_chunk(s, base, seq=1) == 403
    with structlog.testing.capture_logs() as cap:
        asyncio.run(_run())
    refused = ev.query("clinical", kind="consent.violation_refused")
    assert len(refused) == 1 and refused[0]["payload"]["seq"] == 1
    assert not ev.query("clinical", kind="encounter.opened")     # no encounter ever opened
    assert any(c.get("event") == "scribe.consent.chunk_refused" and c.get("seq") == 1 for c in cap)


def test_chunk_accepted_after_confirmed_multiseq(tmp_path):
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            tok = await _open_session(s, base)
            assert (await _post_consent(s, base, "confirmed", session=tok))[0] == 200
            for n in (1, 2, 3):
                assert await _post_chunk(s, base, seq=n, session=tok) == 200
    asyncio.run(_run())
    assert len(iw._existing_seqs(Path(cfg.input_dir) / _LABEL)) == 3


def test_chunk_gate_bypassed_in_synthetic_mode(tmp_path):
    # a synthetic/test encounter has NO consent flow (§7.2/§10) — the gate is clinical-only, so a
    # synthetic chunk flows without any consent event. (Pins the clinical-only decision.)
    ev = _facade(tmp_path)
    cfg = _config(tmp_path, mode="synthetic")

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            params = {"label": _LABEL, "seq": "1", "ext": "webm", "synthetic": "true"}
            async with s.post(base + iw.INGEST_CHUNK_ROUTE, params=params, data=b"AUDIO",
                              headers=_auth()) as r:
                assert r.status == 200          # accepted — no consent required in synthetic mode
    asyncio.run(_run())
    assert ev.query("clinical", kind="consent.violation_refused") == []


# ── withdrawal ordering (the hazard, §5) ─────────────────────────────────────

def test_withdraw_records_halts_and_at_seq(tmp_path):
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            tok = await _open_session(s, base)
            assert (await _post_consent(s, base, "confirmed", session=tok))[0] == 200
            assert await _post_chunk(s, base, seq=1, session=tok) == 200
            assert await _post_chunk(s, base, seq=2, session=tok) == 200
            status, body = await _post_consent(s, base, "withdrawn", session=tok)
            assert status == 200
            # every subsequent chunk is refused (the feed is halted).
            assert await _post_chunk(s, base, seq=3, session=tok) == 403
    asyncio.run(_run())
    wd = ev.query("clinical", kind="consent.withdrawn")
    assert len(wd) == 1 and wd[0]["payload"]["at_seq"] == 2        # the on-disk max at withdrawal
    assert wd[0]["actor"] == "jdoe"
    refused = ev.query("clinical", kind="consent.violation_refused")
    assert len(refused) == 1 and refused[0]["payload"]["seq"] == 3


def test_withdraw_illegal_without_confirm_409(tmp_path):
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            tok = await _open_session(s, base)
            # withdraw on ∅ (no confirm). The session is IGNORED for withdrawal identity (§2.4);
            # there is no durable confirmed event → the transition ∅→withdrawn is illegal → 409.
            status, body = await _post_consent(s, base, "withdrawn", session=tok)
            assert status == 409 and body["error"] == "illegal_transition"
    asyncio.run(_run())


def test_withdraw_no_confirm_is_illegal_transition_409(tmp_path):
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            # no session AND no durable confirmed event → you cannot withdraw a consent that was
            # never confirmed. §2.4: withdrawal identity is the durable confirmed event (absent
            # here), so the legality guard rejects ∅→withdrawn as an illegal transition (409),
            # NOT a missing session (identity for withdrawal never comes from a session).
            status, body = await _post_consent(s, base, "withdrawn")
            assert status == 409 and body["error"] == "illegal_transition"
    asyncio.run(_run())


def test_withdraw_identity_from_durable_confirmed_when_session_absent(tmp_path):
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            tok = await _open_session(s, base)
            assert (await _post_consent(s, base, "confirmed", session=tok))[0] == 200
            assert await _post_chunk(s, base, seq=1, session=tok) == 200
            # withdraw with NO session header — identity is read back from the durable confirmed
            # event (§2.4: the durable event IS the encounter→clinician binding).
            status, body = await _post_consent(s, base, "withdrawn")
            assert status == 200 and body["captured_by"] == "jdoe"
    asyncio.run(_run())
    wd = ev.query("clinical", kind="consent.withdrawn")
    assert len(wd) == 1 and wd[0]["actor"] == "jdoe"


def test_withdraw_actor_is_durable_confirmed_not_rebound_session(tmp_path):
    # §2.4 — THE falsified-attribution pin (the class this whole arc exists to prevent). jdoe
    # obtains consent; the shared device rebinds to asmith mid-encounter (a different LIVE
    # session); asmith taps Withdraw. The withdrawal actor MUST be jdoe — the clinician who
    # OBTAINED consent, read from the durable confirmed event — NEVER asmith (the live session).
    # A regression to session-preference makes consent.withdrawn.actor == asmith, chaining a
    # verify-PASSING record with confirmed=jdoe / withdrawn=asmith: this pin fails on that.
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            jdoe = await _open_session(s, base, user="jdoe")
            assert (await _post_consent(s, base, "confirmed", session=jdoe))[0] == 200
            assert await _post_chunk(s, base, seq=1, session=jdoe) == 200
            asmith = await _open_session(s, base, user="asmith")   # device rebinds mid-encounter
            status, body = await _post_consent(s, base, "withdrawn", session=asmith)
            assert status == 200 and body["captured_by"] == "jdoe"    # NOT asmith
    asyncio.run(_run())
    wd = ev.query("clinical", kind="consent.withdrawn")
    conf = ev.query("clinical", kind="consent.confirmed")
    # the durable chain attributes BOTH transitions to jdoe — internally consistent, not falsified.
    assert wd[0]["actor"] == "jdoe" and conf[0]["payload"]["captured_by"] == "jdoe"


def test_withdraw_durable_failure_no_ack_no_halt(tmp_path, monkeypatch):
    # THE ordering contract (§5): if the durable withdrawn append FAILS, the route 5xxs, the cache
    # is NOT flipped, and capture is NOT halted (never tell the clinician "stopped" on an
    # unrecorded withdrawal). A subsequent chunk still flows (state stayed confirmed).
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    def _boom(*a, **k):
        raise EventStoreError("simulated durable-append failure")

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            tok = await _open_session(s, base)
            assert (await _post_consent(s, base, "confirmed", session=tok))[0] == 200
            assert await _post_chunk(s, base, seq=1, session=tok) == 200
            monkeypatch.setattr(ev, "consent_withdrawn", _boom)
            status, body = await _post_consent(s, base, "withdrawn", session=tok)
            assert status == 503 and body["error"] == "consent_write_failed"
            # capture NOT halted — the withdrawal was never durably recorded.
            assert await _post_chunk(s, base, seq=2, session=tok) == 200
    with structlog.testing.capture_logs() as cap:
        asyncio.run(_run())
    assert ev.query("clinical", kind="consent.withdrawn") == []      # nothing landed
    assert any(c.get("event") == "scribe.consent.write_failed" for c in cap)


# ── the race (per-encounter lock) + restart robustness ───────────────────────

def test_race_no_chunk_beyond_withdraw_boundary(tmp_path):
    # fire withdraw + a chunk CONCURRENTLY: the per-encounter lock serializes them, so the on-disk
    # chunk count can never exceed the recorded at_seq (no un-consented chunk past the boundary).
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            tok = await _open_session(s, base)
            assert (await _post_consent(s, base, "confirmed", session=tok))[0] == 200
            assert await _post_chunk(s, base, seq=1, session=tok) == 200
            (wstatus, _), cstatus = await asyncio.gather(
                _post_consent(s, base, "withdrawn", session=tok),
                _post_chunk(s, base, seq=2, session=tok),
            )
            return wstatus, cstatus
    wstatus, cstatus = asyncio.run(_run())
    assert wstatus == 200
    wd = ev.query("clinical", kind="consent.withdrawn")
    assert len(wd) == 1
    at_seq = wd[0]["payload"]["at_seq"]
    on_disk = len(iw._existing_seqs(Path(cfg.input_dir) / _LABEL))
    assert on_disk == at_seq                        # every landed chunk is within the boundary
    assert (cstatus, at_seq) in ((200, 2), (403, 1))  # chunk2 either landed-before or refused-after


def test_restart_robustness_withdrawn_survives_empty_cache(tmp_path):
    # §5.4 — a server restart empties the RAM cache; the chunk gate falls back to the DURABLE
    # state. A withdrawn encounter still reads back "withdrawn" from an empty cache.
    ev = _facade(tmp_path)
    ev.consent_confirmed(subject_id="enc-x", captured_by="jdoe")
    ev.consent_withdrawn(subject_id="enc-x", at_seq=2, actor="jdoe")
    assert iw._consent_state_cached({}, "enc-x", ev) == "withdrawn"     # durable fallback


def test_consent_state_cached_miss_populates(tmp_path):
    ev = _facade(tmp_path)
    ev.consent_confirmed(subject_id="enc-y", captured_by="asmith")
    cache: dict[str, str] = {}
    assert iw._consent_state_cached(cache, "enc-y", ev) == "confirmed"
    assert cache["enc-y"] == "confirmed"            # the miss populated the hot cache


def test_enc_lock_same_instance_per_encounter():
    locks: dict = {}
    a = iw._enc_lock(locks, "enc-a")
    assert iw._enc_lock(locks, "enc-a") is a         # same encounter → same lock
    assert iw._enc_lock(locks, "enc-b") is not a     # distinct encounter → distinct lock
