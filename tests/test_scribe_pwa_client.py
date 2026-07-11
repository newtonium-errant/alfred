"""Tests for the STAY-C loopback PWA client + static-serve auth-split (#49 Slice B).

Unit-gated here (merge gate):
  * backend static surface — page + app.js served on loopback + Host-pin, strict
    CSP, bearer-EXEMPT for the page GET but STILL bearer-required on the 3 API
    routes (the auth-split); token-in-page not reachable cross-origin/rebind;
    INERT-default (no server when disabled); route-table pin.
  * client JS static contract (browser-gated for full e2e — Playwright/#54 — so
    pinned by reading): R6 label shape, B2 one-recorder-per-window / no-timeslice,
    serial-in-flight + 409-advance, R5 no browser storage, R6 no patient field.

Deploy/smoke-gated (NOT a unit gate):
  * B2 DEFINITIVE real-speech decode — a committed webm/opus utterance → route →
    REAL faster-whisper decode → NON-EMPTY transcript. Gated on the [scribe] extra
    + a staged model + the fixture; runs at the #54 on-box smoke.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import pytest

from alfred.scribe.config import (
    ScribeConfig,
    ScribeIngestWebConfig,
    ScribeLlmConfig,
    ScribeSttConfig,
)
from alfred.scribe import ingest_web as iw
from alfred.scribe.ingest_web import IngestWebServer, create_ingest_app
from alfred.scribe import pwa_assets
from alfred.scribe.pwa_assets import APP_JS, CSP_VALUE, render_index

_SALT = "DUMMY_SCRIBE_TEST_SALT"
_TOKEN = "secret-ingest-token-xyz"
_LABEL = "enc-1720000000000-0123456789abcdef"
# The backend label contract (must stay in lockstep with ingest_web.ENCOUNTER_LABEL_RE).
_LABEL_RE = re.compile(r"^enc-[0-9]{13}-[0-9a-f]{16}$")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _config(tmp_path=None, *, enabled=True, token=_TOKEN, host="127.0.0.1", port=None):
    return ScribeConfig(
        mode="synthetic",
        input_dir=str((tmp_path or Path("/tmp")) / "inbox"),
        stt=ScribeSttConfig(provider="fake"),
        llm=ScribeLlmConfig(base_url="http://127.0.0.1:11434"),
        ingest_web=ScribeIngestWebConfig(
            enabled=enabled, host=host, port=port or _free_port(), token=token),
        encounter_salt=_SALT,
    )


@asynccontextmanager
async def _serve(config):
    server = IngestWebServer(config)
    await server.start()
    try:
        yield f"http://127.0.0.1:{config.ingest_web.port}"
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# backend static surface + auth-split
# ---------------------------------------------------------------------------

def test_page_served_no_bearer_with_csp_and_token(tmp_path):
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            async with s.get(base + iw.PAGE_ROUTE) as r:   # NO bearer
                return r.status, r.headers.get("Content-Security-Policy"), \
                    r.headers.get("Content-Type"), await r.text()

    st, csp, ctype, body = asyncio.run(_go())
    assert st == 200                                       # page GET needs no bearer
    assert csp == CSP_VALUE                                # strict CSP present
    assert "text/html" in ctype
    assert f'data-ingest-token="{_TOKEN}"' in body         # token embedded for the JS
    assert '<script src="/scribe/app.js"></script>' in body  # same-origin JS


def test_app_js_served_no_bearer(tmp_path):
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            async with s.get(base + iw.APP_JS_ROUTE) as r:  # NO bearer
                return r.status, r.headers.get("Content-Type"), await r.text()

    st, ctype, body = asyncio.run(_go())
    assert st == 200 and "javascript" in ctype
    assert "MediaRecorder" in body                          # it's the PWA logic


def test_api_routes_still_require_bearer(tmp_path):
    # The auth-split's teeth: the page is bearer-exempt, but ingest/close/status
    # STILL require the bearer. Pin BOTH halves.
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            results = {}
            # API without bearer → 401
            async with s.post(base + iw.INGEST_CHUNK_ROUTE,
                             params={"label": _LABEL, "seq": "1", "ext": "webm", "synthetic": "true"},
                             data=b"x") as r:
                results["ingest_no_bearer"] = r.status
            async with s.post(base + iw.CLOSE_ROUTE, params={"label": _LABEL}) as r:
                results["close_no_bearer"] = r.status
            async with s.get(base + iw.STATUS_ROUTE, params={"label": _LABEL}) as r:
                results["status_no_bearer"] = r.status
            # status WITH bearer → 200 (the split lets valid API calls through)
            async with s.get(base + iw.STATUS_ROUTE, params={"label": _LABEL},
                            headers={"Authorization": f"Bearer {_TOKEN}"}) as r:
                results["status_bearer"] = r.status
            return results

    r = asyncio.run(_go())
    assert r["ingest_no_bearer"] == 401
    assert r["close_no_bearer"] == 401
    assert r["status_no_bearer"] == 401
    assert r["status_bearer"] == 200


def test_static_route_host_pin_rebind_blocked(tmp_path):
    # A DNS-rebind request carries the attacker domain as Host → the page (and its
    # embedded token) is refused BEFORE it is served.
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            async with s.get(base + iw.PAGE_ROUTE,
                            headers={"Host": f"evil.example.com:{cfg.ingest_web.port}"}) as r:
                return r.status, await r.text()

    st, body = asyncio.run(_go())
    assert st == 421                                       # rebind blocked
    assert _TOKEN not in body                              # token NOT leaked


def test_page_not_reachable_cross_origin_no_cors(tmp_path):
    # Cross-origin protection layer 2: even a same-Host request bearing an
    # attacker Origin gets NO Access-Control-Allow-Origin, so a cross-origin
    # fetch's JS can't read the token-bearing HTML (SOP-enforced).
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            async with s.get(base + iw.PAGE_ROUTE, headers={"Origin": "https://evil.example.com"}) as r:
                return r.status, dict(r.headers)

    st, headers = asyncio.run(_go())
    assert st == 200
    assert not any(k.lower().startswith("access-control-") for k in headers)


def test_static_page_rejects_cross_origin_sec_fetch_site(tmp_path):
    # NOTE-1 belt: the token-bearing page is not even SERVED to a CROSS-ORIGIN
    # fetch (Sec-Fetch-Site cross-site/same-site → refused), while a direct
    # operator nav ('none' / absent) and the same-origin app.js subresource still
    # load. FAIL-OPEN on an absent header (older nav) — must not break the page.
    cfg = _config(tmp_path)

    async def _go():
        out = {}
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            for name, sfs in (("cross", "cross-site"), ("samesite", "same-site")):
                async with s.get(base + iw.PAGE_ROUTE, headers={"Sec-Fetch-Site": sfs}) as r:
                    out[name] = (r.status, _TOKEN in await r.text())
            # direct nav (Sec-Fetch-Site: none) → served
            async with s.get(base + iw.PAGE_ROUTE, headers={"Sec-Fetch-Site": "none"}) as r:
                out["none"] = r.status
            # same-origin app.js subresource → served
            async with s.get(base + iw.APP_JS_ROUTE, headers={"Sec-Fetch-Site": "same-origin"}) as r:
                out["appjs_same_origin"] = r.status
            # absent header (older nav / non-browser) → FAIL-OPEN, served
            async with s.get(base + iw.PAGE_ROUTE) as r:
                out["absent"] = r.status
        return out

    out = asyncio.run(_go())
    assert out["cross"] == (421, False)        # cross-site refused, token NOT served
    assert out["samesite"] == (421, False)     # same-site (cross-origin) refused
    assert out["none"] == 200                  # direct operator nav served
    assert out["appjs_same_origin"] == 200     # same-origin subresource served
    assert out["absent"] == 200                # fail-open — real page load not broken


def test_csp_value_is_strict():
    # The exact directives the sovereign lens requires. Note: NO script-src
    # override → scripts inherit default-src 'self' (inline scripts blocked).
    for directive in (
        "default-src 'self'",
        "connect-src 'self'",           # browser refuses off-box fetch
        "img-src 'self' data:",
        "style-src 'self' 'unsafe-inline'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",       # NOTE-2: clickjacking — page can't be framed
    ):
        assert directive in CSP_VALUE
    assert "script-src" not in CSP_VALUE                   # scripts fall back to 'self' (no unsafe-inline)
    assert "unsafe-eval" not in CSP_VALUE


def test_page_loads_zero_external_resources():
    # R4 — the served HTML references NOTHING off-box: no absolute URL, no CDN,
    # no web font, and the only script is the same-origin /scribe/app.js.
    html = render_index(_TOKEN)
    assert "http://" not in html and "https://" not in html
    assert "//cdn" not in html and "fonts." not in html
    # exactly one script tag, same-origin.
    scripts = re.findall(r"<script[^>]*>", html)
    assert scripts == ['<script src="/scribe/app.js">']


def test_route_table_pins(tmp_path):
    # 3 API routes (byte-identical to Slice A) + 2 static PWA routes.
    app = create_ingest_app(_config(tmp_path))
    got = {(r.method, r.get_info().get("path")) for r in app.router.routes()
           if r.method in ("GET", "POST")}
    assert ("POST", iw.INGEST_CHUNK_ROUTE) in got
    assert ("POST", iw.CLOSE_ROUTE) in got
    assert ("GET", iw.STATUS_ROUTE) in got
    assert ("GET", iw.PAGE_ROUTE) in got
    assert ("GET", iw.APP_JS_ROUTE) in got
    # no extra GET/POST routes crept in.
    assert len(got) == 5


def test_inert_default_no_static_surface(tmp_path):
    # INERT default: the daemon starts NO server when disabled → no page, no
    # static routes exist to serve.
    from alfred.scribe.daemon import _maybe_start_ingest_server
    cfg = _config(tmp_path, enabled=False)
    server = asyncio.run(_maybe_start_ingest_server(cfg))
    assert server is None


# ---------------------------------------------------------------------------
# client JS static contract (browser-gated → pinned by reading)
# ---------------------------------------------------------------------------

def test_pwa_label_gen_shape_matches_backend_regex():
    # R6 — the client label contract. The JS mints enc-<13d ms>-<16 hex>. Emulate
    # the SAME construction in Python and assert it fullmatches the backend regex,
    # AND pin the exact JS construction tokens so the shape can't drift.
    import os
    for _ in range(1000):
        ms = str(1_000_000_000_000 + int.from_bytes(os.urandom(2), "big"))  # 13 digits
        hexnonce = os.urandom(8).hex()                                       # 16 hex
        label = f"enc-{ms}-{hexnonce}"
        assert _LABEL_RE.fullmatch(label)
    # the JS uses exactly this recipe (Date.now = 13-digit ms; 8 random bytes → 16 hex).
    assert "Date.now().toString()" in APP_JS
    assert "crypto.getRandomValues" in APP_JS
    assert "new Uint8Array(8)" in APP_JS
    assert "padStart(2, '0')" in APP_JS
    assert "'enc-'" in APP_JS
    # the backend regex the JS must satisfy is the one shipped in ingest_web.
    assert iw.ENCOUNTER_LABEL_RE.pattern == _LABEL_RE.pattern


def test_pwa_one_recorder_per_window_no_timeslice():
    # B2 — a FRESH MediaRecorder per window, started with NO timeslice arg (so a
    # single complete ondataavailable blob per window). A timeslice would produce
    # headerless clusters that fail chunk-by-chunk decode.
    assert "new MediaRecorder(stream" in APP_JS
    assert "recorder.start();" in APP_JS                    # NO argument = one blob at stop
    # no timeslice: recorder.start(<number/var>) must NOT appear.
    assert not re.search(r"\.start\(\s*[0-9A-Za-z_]", APP_JS)
    # the recorder is re-created each window (startWindow re-entered on onstop).
    assert "if (recording) { startWindow(); }" in APP_JS


def test_pwa_serial_in_flight_and_409_advance():
    # Serial-in-flight — a strict per-encounter promise chain (each await-ed before
    # the next); retry-same-seq on error; a 409 (retry after a lost 200) advances.
    assert "chain = chain.then(" in APP_JS                  # serial chain
    assert "for (let attempt = 0" in APP_JS                 # retry loop
    assert "resp.status === 409" in APP_JS                  # 409 handled
    assert "resp.status === 200" in APP_JS
    # 409 returns true (advance) — same branch shape as 200.
    assert re.search(r"if \(resp\.status === 409\) \{ return true; \}", APP_JS)
    # SEQ MUST be computed INSIDE the chain (after the prior chunk advanced it),
    # NOT at onstop time — else continuous windows collide on the same seq
    # (check-then-write across the await). Pin: `const chunkSeq = seq + 1` occurs
    # AFTER `chain = chain.then` (inside the closure), never before it.
    assert re.search(r"chain = chain\.then\(async \(\) => \{.*?const chunkSeq = seq \+ 1;",
                     APP_JS, re.DOTALL)
    assert "const chunkSeq = seq + 1" not in APP_JS.split("chain = chain.then")[0]
    # close is enqueued on the chain AFTER the last chunk drains.
    assert "/scribe/close?label=" in APP_JS


def test_pwa_terminal_4xx_actually_stops():
    # N1 (FIX 3) — a TERMINAL 4xx (cap/reject) or exhausted retries returns false
    # from postChunk, and the caller ACTUALLY stops (halt recording + close) rather
    # than keep hammering the same seq against the cap.
    assert "if (resp.status >= 400 && resp.status < 500) { return false; }" in APP_JS
    # the chunk closure calls stopEncounter on a non-ok result.
    assert re.search(r"if \(ok\) \{ seq = chunkSeq; \}\s*else \{ stopEncounter\(", APP_JS)
    # stopEncounter halts recording (latched) and enqueues the close.
    assert "function stopEncounter(" in APP_JS
    assert "stopped = true;" in APP_JS and "recording = false;" in APP_JS


def test_pwa_no_dead_close_flag_on_chunk():
    # N2 (FIX 4) — the dead close-on-chunk branch is removed. The client NEVER sets
    # close=true on an ingest-chunk; the encounter is finalized solely by the
    # dedicated /scribe/close (robust even if a final chunk's 200 was lost).
    assert "close" not in APP_JS.split("/scribe/ingest-chunk")[1].split("for (let attempt")[0]
    assert "isFinal" not in APP_JS                          # the dead parameter is gone
    assert "postChunk(captured, chunkSeq)" in APP_JS        # call site has no isFinal arg


def test_pwa_no_browser_storage():
    # R5 — memory-only. NO persistence of audio/status/token in the browser.
    for banned in ("localStorage", "sessionStorage", "indexedDB", "IndexedDB",
                   "serviceWorker", "navigator.serviceWorker", "caches.",
                   "CacheStorage", ".register("):
        assert banned not in APP_JS, f"R5 violation: {banned} present"
    # cache:'no-store' on every fetch (belt).
    assert "cache: 'no-store'" in APP_JS
    # no offline-caching manifest / SW in the page either.
    html = render_index(_TOKEN)
    assert "serviceWorker" not in html and "manifest" not in html


def test_pwa_has_no_patient_identifier_field():
    # R6 — NO patient-identifier field feeds the label; the id is a machine token.
    # The correct check is "no form control that could capture an identifier"
    # (an input/textarea/select/contenteditable). Reassuring COPY that mentions
    # "patient identifiers" (to state there are none) is fine — it's not a field.
    html = render_index(_TOKEN).lower()
    for control in ("<input", "<textarea", "<select", "contenteditable"):
        assert control not in html, f"R6: identifier-capable form control present: {control}"


# ---------------------------------------------------------------------------
# B2 DEFINITIVE — real-speech decode (deploy/smoke-gated, #54)
# ---------------------------------------------------------------------------

_SPEECH_FIXTURE = Path(__file__).parent / "fixtures" / "scribe_speech_sample.webm"


@pytest.mark.skipif(
    importlib.util.find_spec("faster_whisper") is None or not _SPEECH_FIXTURE.is_file(),
    reason="B2 real-speech decode needs the [scribe] extra + a staged model + a "
           "committed real-speech webm fixture (recorded on-box; runs at #54 smoke)",
)
def test_b2_real_speech_decode_nonempty_transcript(tmp_path, monkeypatch):
    # B2 DEFINITIVE (reviewer-design): a REAL recorded utterance POSTed through the
    # route → REAL faster-whisper decode → assert NON-EMPTY transcript text. This
    # is where B2's teeth are (the Slice-A real-decode used silent WAV + skip).
    import alfred.distiller.backends.ollama as ollama_mod
    from alfred.scribe import ScribeState, compute_encounter_id, ledger_path, load_ledger, run_sweep

    async def _fake_ollama(prompt, system=None, model="", endpoint="", **kw):
        return ('{"subjective": [], "objective": [], "assessment": [], "plan": [],'
                ' "assessment_reasoning_stated": false}', {"stop_reason": "stop", "prompt_eval_count": 500})
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama)

    cfg = _config(tmp_path)
    cfg.stt.provider = "faster-whisper"
    webm = _SPEECH_FIXTURE.read_bytes()

    async def _ingest():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            async with s.post(base + iw.INGEST_CHUNK_ROUTE,
                             params={"label": _LABEL, "seq": "1", "ext": "webm",
                                     "synthetic": "true", "close": "true"},
                             data=webm, headers={"Authorization": f"Bearer {_TOKEN}"}) as r:
                assert r.status == 200
    asyncio.run(_ingest())

    state = ScribeState(str(tmp_path / "state.json"))
    asyncio.run(run_sweep(cfg, state, tmp_path / "vault"))

    enc_dir = Path(cfg.input_dir) / _LABEL
    ledger = load_ledger(ledger_path(enc_dir, compute_encounter_id(_LABEL, salt=_SALT)))
    assert ledger is not None and ledger.segments
    text = " ".join(seg.text for seg in ledger.segments).strip()
    assert text, "B2: real decode must yield NON-EMPTY transcript text"
