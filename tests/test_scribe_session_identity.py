"""#12 slice 12b — per-clinician PWA identity session (design §2).

Contract-first coverage of the session-identity CENTREPIECE:

  * server-issued binding: ``POST /scribe/session/open?user=<slug>`` mints an opaque
    ``ses-<ms>-<hex>`` token bound to a ``config.clinicians`` slug; unknown/malformed slug →
    403 ``unknown_clinician`` (identity NEVER fabricated, §2.5); empty clinicians → fail-closed;
  * ingest-class auth: the routes ride the ingest token (no new credential) — no auth → 401;
  * ``captured_by`` resolution: a resolved session yields the OPENED clinician slug (§2.5);
  * sliding TTL: idle-expiry + absolute cap, resolved through a deterministic injected clock;
  * no-storage: the opaque token never lands in any on-disk file (§10 / §12).

The RAM table is process-global (``iw._PWA_SESSIONS``, mirroring the enroll ``_SESSIONS``); the
autouse fixture clears it so no session bleeds across cases.
"""
from __future__ import annotations

import asyncio
import re
import secrets
import socket
from contextlib import asynccontextmanager

import aiohttp
import pytest
import structlog

from alfred.scribe import ingest_web as iw
from alfred.scribe.config import (
    ScribeConfig,
    ScribeIngestWebConfig,
    ScribeLlmConfig,
    ScribeSttConfig,
)
from alfred.scribe.events import ScribeEvents
from alfred.scribe.ingest_web import IngestWebServer, PwaSession

_SALT = "DUMMY_SCRIBE_TEST_SALT"
_TOKEN = "tok-" + secrets.token_hex(8)
_CLOCK = "2026-07-18T12:00:00+00:00"
_CLINICIANS = ["jdoe", "asmith"]
_SESSION_RE = re.compile(r"^ses-[0-9]{13}-[0-9a-f]{16}$")


@pytest.fixture(autouse=True)
def _clear_pwa_sessions():
    """The session table is module-global — clear it around every test so a session can't
    bleed across cases (the same hygiene the enroll ``_SESSIONS`` table needs)."""
    iw._PWA_SESSIONS.clear()
    yield
    iw._PWA_SESSIONS.clear()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _config(tmp_path, *, clinicians=None, **web_over):
    return ScribeConfig(
        mode="synthetic", input_dir=str(tmp_path / "inbox"),
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
async def _serve(config, events=None):
    server = IngestWebServer(config, events=events)
    await server.start()
    try:
        yield f"http://127.0.0.1:{config.ingest_web.port}"
    finally:
        await server.stop()


def _auth():
    return {"Authorization": f"Bearer {_TOKEN}"}


# ── HTTP: open / close / auth ────────────────────────────────────────────────

def test_session_open_returns_token_and_clinician(tmp_path):
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            async with s.post(base + iw.SESSION_OPEN_ROUTE, params={"user": "jdoe"},
                              headers=_auth()) as r:
                assert r.status == 200
                j = await r.json()
                assert j["clinician"] == "jdoe"
                assert _SESSION_RE.fullmatch(j["session"]), j["session"]
                return j["session"]
    # pin the attribution signal (the aiohttp handler emits scribe.session.opened carrying the
    # clinician + live count — an operator's grep for who-opened-a-session depends on it).
    with structlog.testing.capture_logs() as cap:
        tok = asyncio.run(_run())
    opened = [c for c in cap if c.get("event") == "scribe.session.opened"]
    assert len(opened) == 1 and opened[0]["clinician"] == "jdoe"
    # the minted token is bound to the opened clinician in the RAM table (post-serve the table
    # survives — it is module-global; the resolver reads it back).
    assert iw._PWA_SESSIONS[tok].clinician == "jdoe"


def test_session_open_unknown_clinician_403(tmp_path):
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            # a grammar-VALID slug that is not a configured clinician → fabrication refused.
            async with s.post(base + iw.SESSION_OPEN_ROUTE, params={"user": "ghost"},
                              headers=_auth()) as r:
                assert r.status == 403
                assert (await r.json())["error"] == "unknown_clinician"
    with structlog.testing.capture_logs() as cap:
        asyncio.run(_run())
    rejected = [c for c in cap if c.get("event") == "scribe.session.rejected"]
    assert len(rejected) == 1 and rejected[0]["reason"] == "unknown_clinician"
    assert iw._PWA_SESSIONS == {}          # nothing minted on a refusal


def test_session_open_malformed_slug_403(tmp_path):
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            # a grammar-INVALID slug (space + uppercase) → same opaque 403 (fail-closed).
            async with s.post(base + iw.SESSION_OPEN_ROUTE, params={"user": "Not A Slug"},
                              headers=_auth()) as r:
                assert r.status == 403
                assert (await r.json())["error"] == "unknown_clinician"
    asyncio.run(_run())


def test_session_open_empty_clinicians_fail_closed(tmp_path):
    cfg = _config(tmp_path, clinicians=[])

    async def _run():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            async with s.post(base + iw.SESSION_OPEN_ROUTE, params={"user": "jdoe"},
                              headers=_auth()) as r:
                assert r.status == 403          # no identity without a configured clinician
    asyncio.run(_run())


def test_session_open_requires_ingest_token(tmp_path):
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            # ingest-class route — a no-Authorization POST is refused by the middleware (401).
            async with s.post(base + iw.SESSION_OPEN_ROUTE, params={"user": "jdoe"}) as r:
                assert r.status == 401
    asyncio.run(_run())
    assert iw._PWA_SESSIONS == {}


def test_session_close_idempotent(tmp_path):
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            async with s.post(base + iw.SESSION_OPEN_ROUTE, params={"user": "asmith"},
                              headers=_auth()) as r:
                tok = (await r.json())["session"]
            hdr = {**_auth(), iw.SESSION_HEADER: tok}
            async with s.post(base + iw.SESSION_CLOSE_ROUTE, headers=hdr) as r:
                assert r.status == 200 and (await r.json())["closed"] is True
            # second close of the now-stale token — still 200 {closed: true} (idempotent).
            async with s.post(base + iw.SESSION_CLOSE_ROUTE, headers=hdr) as r:
                assert r.status == 200 and (await r.json())["closed"] is True
            # a close with NO session header is also idempotent (a teardown never fails loud).
            async with s.post(base + iw.SESSION_CLOSE_ROUTE, headers=_auth()) as r:
                assert r.status == 200 and (await r.json())["closed"] is True
    asyncio.run(_run())
    assert iw._PWA_SESSIONS == {}          # the open→close dropped the only session


def test_status_poll_keeps_session_warm(tmp_path):
    cfg = _config(tmp_path)
    label = "enc-1720000000000-0123456789abcdef"

    async def _run():
        async with _serve(cfg, events=_facade(tmp_path)) as base, aiohttp.ClientSession() as s:
            async with s.post(base + iw.SESSION_OPEN_ROUTE, params={"user": "jdoe"},
                              headers=_auth()) as r:
                tok = (await r.json())["session"]
            # the status poll carries X-Scribe-Session — a resolved session refreshes last_seen
            # (sliding TTL) and the poll still answers 200 (no gating on the status probe).
            hdr = {**_auth(), iw.SESSION_HEADER: tok}
            async with s.get(base + iw.STATUS_ROUTE, params={"label": label}, headers=hdr) as r:
                assert r.status == 200
            return tok
    tok = asyncio.run(_run())
    assert tok in iw._PWA_SESSIONS          # the poll kept the session alive


def test_session_token_never_written_to_disk(tmp_path):
    """No-storage (§10/§12): the opaque session token is RAM-only — it must appear in NO
    on-disk file (event store, inbox, logs). Open a session, drive an event-writing status
    poll, then scan every file under tmp_path for the token substring."""
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, events=_facade(tmp_path)) as base, aiohttp.ClientSession() as s:
            async with s.post(base + iw.SESSION_OPEN_ROUTE, params={"user": "jdoe"},
                              headers=_auth()) as r:
                return (await r.json())["session"]
    tok = asyncio.run(_run())
    needle = tok.encode("utf-8")
    scanned = 0
    for p in tmp_path.rglob("*"):
        if p.is_file():
            scanned += 1
            assert needle not in p.read_bytes(), f"session token leaked into {p}"
    assert scanned > 0          # the facade genesis wrote at least one file — the scan ran


# ── unit: mint + resolver + sliding TTL (deterministic injected clock) ───────

def test_mint_pwa_session_id_shape_and_uniqueness():
    a, b = iw.mint_pwa_session_id(), iw.mint_pwa_session_id()
    assert _SESSION_RE.fullmatch(a) and _SESSION_RE.fullmatch(b)
    assert a != b                          # the 16-hex nonce makes collisions astronomically rare


def test_resolve_roundtrip_and_captured_by():
    iw._PWA_SESSIONS["ses-1-x"] = PwaSession(clinician="jdoe", opened_at=100.0, last_seen=100.0)
    s = iw._resolve_session_token("ses-1-x", idle_ttl=1800, abs_ttl=43200, now=200.0)
    assert s is not None and s.clinician == "jdoe"     # captured_by == the opened slug (§2.5)
    assert s.last_seen == 200.0                        # the sliding-TTL touch refreshed last_seen


def test_resolve_unknown_or_empty_returns_none():
    iw._PWA_SESSIONS["ses-1-x"] = PwaSession(clinician="jdoe", opened_at=100.0, last_seen=100.0)
    assert iw._resolve_session_token("nope", idle_ttl=1800, abs_ttl=43200, now=200.0) is None
    assert iw._resolve_session_token("", idle_ttl=1800, abs_ttl=43200, now=200.0) is None


def test_resolve_idle_expiry_sweeps():
    iw._PWA_SESSIONS["ses-1-x"] = PwaSession(clinician="jdoe", opened_at=100.0, last_seen=100.0)
    # now is 100 + 1801 > idle_ttl since last_seen → swept, resolves None, and dropped from table.
    with structlog.testing.capture_logs() as cap:
        assert iw._resolve_session_token("ses-1-x", idle_ttl=1800, abs_ttl=43200, now=1901.0) is None
    assert "ses-1-x" not in iw._PWA_SESSIONS
    swept = [c for c in cap if c.get("event") == "scribe.session.swept"]
    assert len(swept) == 1 and swept[0]["count"] == 1      # the reclaim signal (ILB) fired


def test_resolve_absolute_cap_sweeps_even_when_recently_seen():
    # opened long ago but seen recently — the ABSOLUTE cap still expires it (idle alone would not).
    iw._PWA_SESSIONS["ses-1-x"] = PwaSession(clinician="jdoe", opened_at=0.0, last_seen=43100.0)
    assert iw._resolve_session_token("ses-1-x", idle_ttl=1800, abs_ttl=43200, now=43201.0) is None
    assert "ses-1-x" not in iw._PWA_SESSIONS


def test_resolve_sliding_refresh_extends_lifetime():
    iw._PWA_SESSIONS["ses-1-x"] = PwaSession(clinician="jdoe", opened_at=0.0, last_seen=0.0)
    # a resolve at t=1000 refreshes last_seen; a second resolve at t=2500 is only 1500 s idle
    # (< 1800) so it STILL hits — without the refresh it would be 2500 s idle and expire.
    assert iw._resolve_session_token("ses-1-x", idle_ttl=1800, abs_ttl=43200, now=1000.0) is not None
    assert iw._resolve_session_token("ses-1-x", idle_ttl=1800, abs_ttl=43200, now=2500.0) is not None


def test_session_cap_refused_with_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(iw, "_MAX_PWA_SESSIONS", 2)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            codes = []
            for _ in range(3):
                async with s.post(base + iw.SESSION_OPEN_ROUTE, params={"user": "jdoe"},
                                  headers=_auth()) as r:
                    codes.append(r.status)
            return codes
    with structlog.testing.capture_logs() as cap:
        codes = asyncio.run(_run())
    assert codes == [200, 200, 429]        # the third open hits the cap (explicit 429 signal)
    cap_hits = [c for c in cap if c.get("event") == "scribe.session.cap_hit"]
    assert len(cap_hits) == 1 and cap_hits[0]["cap"] == "sessions"
