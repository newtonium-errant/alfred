"""ingest_web encounter.* event emissions (event-store design §5.4 / §8 row 10 / §15.5).

Drives the real loopback ingest server with an ACTIVE facade injected via
``IngestWebServer(config, events=ev)`` and pins the encounter.* family:

  encounter.opened (first chunk, seq==1);
  encounter.closed on BOTH seal paths — the close-flag on the final chunk AND the
    /close route — each carrying {final_seq};
  encounter.cap_hit {cap} (chunk-count cap);
  encounter.post_close_chunk_refused {seq} (a chunk POST after the seal);
  events=None → the handlers no-op (byte-identical to pre-#11).
"""

from __future__ import annotations

import secrets
import socket
from contextlib import asynccontextmanager

import aiohttp
import pytest

from alfred.scribe import compute_encounter_id
from alfred.scribe.config import (
    ScribeConfig,
    ScribeIngestWebConfig,
    ScribeLlmConfig,
    ScribeSttConfig,
)
from alfred.scribe import ingest_web as iw
from alfred.scribe.events import ScribeEvents
from alfred.scribe.ingest_web import IngestWebServer

_SALT = "DUMMY_SCRIBE_TEST_SALT"
_TOKEN = "tok-" + secrets.token_hex(8)
_LABEL = "enc-1720000000000-0123456789abcdef"
_CLOCK = "2026-07-16T12:00:00+00:00"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _config(tmp_path, **web_over):
    return ScribeConfig(
        mode="synthetic", input_dir=str(tmp_path / "inbox"),
        stt=ScribeSttConfig(provider="fake"),
        llm=ScribeLlmConfig(base_url="http://127.0.0.1:11434", model="m"),
        ingest_web=ScribeIngestWebConfig(
            enabled=True, host="127.0.0.1", port=_free_port(), token=_TOKEN, **web_over),
        encounter_salt=_SALT,
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


async def _post_chunk(sess, base, *, seq=1, close=None, body=b"AUDIOBYTES"):
    params = {"label": _LABEL, "seq": str(seq), "ext": "webm", "synthetic": "true"}
    if close is not None:
        params["close"] = close
    async with sess.post(base + iw.INGEST_CHUNK_ROUTE, params=params, data=body,
                         headers=_auth()) as r:
        return r.status


async def _post_close(sess, base, *, final_seq=None):
    params = {"label": _LABEL}
    if final_seq is not None:
        params["final_seq"] = str(final_seq)
    async with sess.post(base + iw.CLOSE_ROUTE, params=params, headers=_auth()) as r:
        return r.status


def _eid():
    return compute_encounter_id(_LABEL, salt=_SALT)


def test_encounter_opened_on_first_chunk(tmp_path):
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            assert await _post_chunk(s, base, seq=1) == 200
            assert await _post_chunk(s, base, seq=2) == 200  # NOT a re-open
    import asyncio
    asyncio.run(_run())
    opened = ev.query("clinical", kind="encounter.opened")
    assert len(opened) == 1 and opened[0]["subject_id"] == _eid()


def test_encounter_closed_via_close_flag(tmp_path):
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            assert await _post_chunk(s, base, seq=1, close="true") == 200
    import asyncio
    asyncio.run(_run())
    closed = ev.query("clinical", kind="encounter.closed")
    assert len(closed) == 1 and closed[0]["payload"]["final_seq"] == 1


def test_encounter_closed_via_close_route(tmp_path):
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            assert await _post_chunk(s, base, seq=1) == 200
            assert await _post_close(s, base, final_seq=1) == 200
    import asyncio
    asyncio.run(_run())
    closed = ev.query("clinical", kind="encounter.closed")
    assert len(closed) == 1 and closed[0]["payload"]["final_seq"] == 1


def test_post_close_chunk_refused(tmp_path):
    ev = _facade(tmp_path)
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            assert await _post_chunk(s, base, seq=1, close="true") == 200
            assert await _post_chunk(s, base, seq=2) == 409  # sealed → refused
    import asyncio
    asyncio.run(_run())
    refused = ev.query("clinical", kind="encounter.post_close_chunk_refused")
    assert len(refused) == 1 and refused[0]["payload"]["seq"] == 2


def test_encounter_cap_hit_chunks(tmp_path):
    ev = _facade(tmp_path)
    cfg = _config(tmp_path, max_chunks_per_encounter=1)

    async def _run():
        async with _serve(cfg, ev) as base, aiohttp.ClientSession() as s:
            assert await _post_chunk(s, base, seq=1) == 200
            assert await _post_chunk(s, base, seq=2) == 413  # chunk-count cap
    import asyncio
    asyncio.run(_run())
    caps = ev.query("clinical", kind="encounter.cap_hit")
    assert len(caps) == 1 and caps[0]["payload"]["cap"] == "chunks"


def test_events_none_no_emissions(tmp_path):
    cfg = _config(tmp_path)

    async def _run():
        async with _serve(cfg, None) as base, aiohttp.ClientSession() as s:
            assert await _post_chunk(s, base, seq=1, close="true") == 200
    import asyncio
    asyncio.run(_run())
    assert not (tmp_path / "ev").exists()  # no facade → no store touched
