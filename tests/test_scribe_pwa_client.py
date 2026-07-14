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
import secrets
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
# Runtime-generated so NO credential-shaped literal is committed — a static
# credential-shaped value trips GitGuardian's generic-password scanner as a FALSE
# positive (this token authorizes nothing: loopback-only + synthetic-mode, in-memory
# test config). See tests/test_scribe_ingest_web.py + .gitguardian.yaml.
_TOKEN = "tok-" + secrets.token_hex(8)
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


def _code(js: str = APP_JS) -> str:
    """``APP_JS`` with comments stripped, so a pin binds actual CODE, not PROSE.

    This matters: the original R5 pin grepped the raw source for ``localStorage`` and so
    was tripped by a COMMENT that merely said "no localStorage" — a banned-token scan
    over raw source cannot tell a call from a sentence. (No string literal in APP_JS
    contains ``//``, so stripping line comments is safe here.)"""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
    return re.sub(r"(?m)//.*$", "", js)


def test_pwa_one_recorder_per_window_no_timeslice():
    # B2 — a FRESH MediaRecorder per window, started with NO timeslice arg (so a
    # single complete ondataavailable blob per window). A timeslice would produce
    # headerless clusters that fail chunk-by-chunk decode.
    code = _code()
    assert "new MediaRecorder(" in code                     # a recorder is constructed
    # NO timeslice anywhere: `.start(<arg>)` must not appear on ANY recorder.
    assert not re.search(r"\.start\(\s*[0-9A-Za-z_]", code)
    assert re.search(r"\.start\(\)\s*;", code)              # started with NO argument
    # a NEW recorder per window on BOTH capture paths (encounter + enrolment) — the
    # recorder is re-created inside the onstop re-entry, never reused.
    assert "if (recording) { startWindow(); }" in code      # encounter loop
    assert "if (live) { windowOnce(); }" in code            # enrolment loop


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
    code = _code()
    assert "close" not in code.split("/scribe/ingest-chunk")[1].split("for (let attempt")[0]
    assert "isFinal" not in code                            # the dead parameter is gone
    # the chunk POST carries the seq AND the ext of the ACTUAL negotiated container.
    assert re.search(r"postChunk\(captured, chunkSeq, ext\)", code)


def test_pwa_no_browser_storage():
    # R5 — memory-only. NO persistence of audio/status/TOKEN in the browser. Scanned
    # over CODE (comments stripped): the prose legitimately names these APIs to say it
    # does not use them, and a raw-source grep cannot tell a call from a sentence.
    code = _code()
    for banned in ("localStorage", "sessionStorage", "indexedDB", "IndexedDB",
                   "serviceWorker", "caches.", "CacheStorage", ".register("):
        assert banned not in code, f"R5 violation: {banned} present in CODE"
    assert "cache: 'no-store'" in code                      # belt on every fetch
    html = render_index(_TOKEN)
    assert "serviceWorker" not in html and "manifest" not in html


def test_pwa_record_view_has_no_free_text_field_and_label_is_machine_minted():
    # R6 (EVOLVED for the enrolment UI — the old pin asserted the page had ZERO form
    # controls, which a preset PICKER and a voiceprint NAME field necessarily break).
    # The invariant that actually matters is narrower and stronger:
    #   (1) NOTHING captures a patient identifier into the ENCOUNTER path, and
    #   (2) NO DOM value feeds the encounter LABEL — it stays a machine token.
    html = render_index(_TOKEN)
    record = html.split('<section id="view-record">')[1].split("</section>")[0].lower()
    # the record view has NO free-text input at all (only a <select> whose options are
    # SERVER-provided preset names — it cannot capture typed text).
    for control in ("<input", "<textarea", "contenteditable"):
        assert control not in record, f"R6: free-text control in the RECORD view: {control}"
    # the label is minted from a crypto nonce; no element value feeds it.
    code = _code()
    assert re.search(r"function newLabel\(\)[^}]*crypto\.getRandomValues", code, re.DOTALL)
    assert "getElementById" not in code.split("function newLabel()")[1].split("}")[0]


def test_pwa_only_free_text_inputs_are_token_and_voiceprint_name():
    # The ONLY free-text inputs in the whole client are (a) the enrol-token paste and
    # (b) the voiceprint NAME. Pin their identity + their safety properties, so a future
    # edit cannot quietly add a third (e.g. a patient field) without failing here.
    code = _code()
    ids = set(re.findall(r"<input id=\"([a-z0-9_-]+)\"", code))
    assert ids == {"tok", "nm", "nm2"}, f"unexpected free-text input(s): {ids}"
    # the token field is a password box, never autofilled, and never persisted.
    assert re.search(r"<input id=\"tok\" type=\"password\" autocomplete=\"off\"", code)
    # the NAME fields carry the memo's guidance + the backend's 64-char cap.
    assert code.count("name the place and mic, not a patient") == 2   # rename + verdict
    assert code.count('maxlength="64"') == 2
    # the name is enrolment metadata only — it NEVER feeds the encounter label.
    assert "newLabel" not in code.split("nm2-ok")[1]


def test_pwa_enroll_token_is_never_embedded_in_the_page():
    # The two-token split's whole point: page possession must NOT grant biometric
    # mutation. The INGEST token is embedded (the JS needs it); the ENROLL token is
    # pasted, memory-only.
    html = render_index("INGEST_SECRET", ["np_jamie"])
    assert 'data-ingest-token="INGEST_SECRET"' in html
    assert "enroll-token" not in html and "enrollToken" not in html
    assert "data-enroll" not in html
    # ...and the client only ever obtains it from the paste field, never the DOM dataset.
    code = _code()
    assert "dataset.enroll" not in code
    assert re.search(r"enrollToken = \$\('tok'\)\.value", code)


def test_pwa_clinicians_embedded_for_identity_picker():
    # Staff slugs (never PHI) are embedded so the enrol view OFFERS the identity: the
    # server matches scribe.clinicians VERBATIM, so a hand-typed typo would fail-close a
    # consented recording with 403.
    html = render_index(_TOKEN, ["np_jamie", "dr_x"])
    assert "np_jamie" in html and "dr_x" in html
    assert 'data-clinicians="' in html
    # embedded as ESCAPED JSON in a data- attribute (never an inline script — CSP).
    assert "&quot;np_jamie&quot;" in html
    assert "<script>" not in html


def test_pwa_mru_default_is_server_derived_not_client_stored():
    # R5 is ABSOLUTE (no localStorage), so the picker's default MUST come from the
    # server (mru_preset_id), not from client memory of the last choice.
    code = _code()
    assert "mru_preset_id" in code
    assert re.search(r"mruPresetId && usable\.some", code)   # preselect only if still usable


def test_pwa_preset_fit_forward_tolerates_unknown_values():
    # preset_fit is a 5-value enum; 5a emits only unarmed|ok. The client MUST tolerate
    # warming/weak/none TODAY so 5b ships without a client change.
    code = _code()
    for v in ("unarmed", "ok", "warming", "weak", "none"):
        assert v in code
    # an UNRECOGNISED value must fall back, never throw / render undefined.
    assert re.search(r"known\[fit\] \|\| known\.unarmed", code)


def test_pwa_does_not_hardcode_webm_and_maps_ios_mp4_to_m4a():
    # Operator ruling: the phone is an iPhone → MediaRecorder emits audio/mp4 (AAC), not
    # webm. The ENCOUNTER ext allowlist has no 'mp4' — but 'm4a' IS AAC-in-MP4, IS on the
    # allowlist, IS swept, and IS decoded. So audio/mp4 → ext=m4a, honouring the iPhone
    # ruling WITHOUT reopening the frozen #49 ext contract.
    code = _code()
    assert "isTypeSupported" in code                        # negotiates, doesn't assume
    assert "'audio/mp4'" in code
    assert re.search(r"indexOf\('mp4'\) >= 0.*return 'm4a'", code, re.DOTALL)
    # the ext sent is derived from the ACTUAL negotiated mimeType, not a constant.
    assert "extFor(recorder.mimeType)" in code
    # every ext the client can emit is on the backend allowlist.
    for ext in ("webm", "m4a", "ogg"):
        assert ext in iw.ALLOWED_AUDIO_EXTS


def test_pwa_presets_routes_carry_required_user_param():
    # The as-built API REQUIRES ?user on presets list/rename/delete (the store is
    # user-keyed). Without it every call 400s — the divergence the API doc flags.
    code = _code()
    assert "'/scribe/presets?user=' + encodeURIComponent(user)" in code
    assert code.count("'?user=' + encodeURIComponent(user) + '&preset='") >= 1


def test_pwa_enroll_chunks_always_carry_seq():
    # ?seq makes a retried window IDEMPOTENT. Without it a lost 200 double-appends,
    # inflating net-speech past the 10s HARD gate and biasing the centroid.
    code = _code()
    assert re.search(r"/scribe/enroll/chunk' \+ p", code)
    assert re.search(r"'\?session=' \+ encodeURIComponent\(session\) \+ '&seq=' \+ String\(eseq\)", code)


# ---------------------------------------------------------------------------
# BEHAVIOURAL — the REAL app.js driven in node against a DOM/fetch shim.
#
# The structural pins above bind the security floor; THESE bind what the client actually
# DOES — the fetch sequence, which is the server contract (docs/scribe_enroll_api.md).
# String-pinning alone is the "passes by construction, catches nothing" failure mode.
#
# The shim is SELF-CONTAINED (no jsdom: the only copy here is a transitive dep of the
# separate web/ tree, absent from this worktree). Its load-bearing safety property:
# getElementById THROWS on an unknown id, so a shim gap is a hard error, never a false pass.
# ---------------------------------------------------------------------------

import json as _json
import shutil
import subprocess

_HARNESS = Path(__file__).parent / "pwa_enroll_harness.mjs"
_NODE = shutil.which("node")


def _drive(scenario: str, tmp_path) -> dict:
    app = tmp_path / "app.js"
    app.write_text(APP_JS, encoding="utf-8")
    out = subprocess.run(
        [_NODE, str(_HARNESS), str(app), scenario],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, f"harness failed: {out.stderr[:800]}"
    res = _json.loads(out.stdout)
    assert not res.get("error"), f"app threw: {res['error'][:800]}"
    return res


def _urls(res, needle):
    return [c["url"] for c in res["calls"] if needle in c["url"]]


pytestmark_node = pytest.mark.skipif(
    _NODE is None, reason="node not installed (behavioural PWA harness); the structural "
                          "contract pins above run unconditionally",
)


@pytestmark_node
def test_behaviour_binds_preset_before_the_first_chunk(tmp_path):
    # THE ordering invariant: the server LOCKS the binding at the first chunk, so a bind
    # that lands after chunk 1 is a 409 and the note's diarize_provenance is permanently
    # absent. The client MUST bind first.
    res = _drive("record_binds_before_first_chunk", tmp_path)
    order = [c["url"].split("?")[0] for c in res["calls"]]
    bind = order.index("/scribe/encounter/preset")
    chunk = order.index("/scribe/ingest-chunk")
    assert bind < chunk, f"bind must precede the first chunk, got {order}"


@pytestmark_node
def test_behaviour_no_preset_never_binds_and_still_records(tmp_path):
    # "No preset — attribution off" is a FIRST-CLASS choice: no binding call at all, and
    # the encounter still records normally.
    res = _drive("record_no_preset_never_binds", tmp_path)
    assert _urls(res, "/scribe/encounter/preset") == []
    assert _urls(res, "/scribe/ingest-chunk")          # chunks still flow


@pytestmark_node
def test_behaviour_bind_failure_never_blocks_the_encounter(tmp_path):
    # A 409/failed binding must NEVER block Start — the encounter simply runs un-anchored
    # (audio is never lost to a preset problem).
    res = _drive("record_bind_failure_does_not_block", tmp_path)
    assert _urls(res, "/scribe/encounter/preset")      # it tried
    assert _urls(res, "/scribe/ingest-chunk")          # ...and recorded anyway


@pytestmark_node
def test_behaviour_iphone_container_maps_to_an_allowlisted_ext(tmp_path):
    # iOS supports ONLY audio/mp4 → the chunk must go out as ext=m4a (on the allowlist),
    # never ext=mp4 (which the ingest route rejects with unsupported_ext).
    res = _drive("record_ios_maps_mp4_to_m4a", tmp_path)
    chunks = _urls(res, "/scribe/ingest-chunk")
    assert chunks and all("ext=m4a" in u for u in chunks), chunks
    assert not any("ext=mp4" in u for u in chunks)


@pytestmark_node
def test_behaviour_unknown_preset_fit_does_not_break_the_chip(tmp_path):
    # 5b will emit warming/weak/none. A 5a client must render something sane, not crash.
    res = _drive("record_unknown_preset_fit_tolerated", tmp_path)
    assert res["chip"] and "undefined" not in res["chip"].lower()


@pytestmark_node
def test_behaviour_enroll_flow_wire_contract(tmp_path):
    # The whole enrolment wire, end to end: start → chunks(seq) → finalize → poll →
    # name-last rename. Token CLASS per route is the security half.
    res = _drive("enroll_flow", tmp_path)
    calls = res["calls"]
    paths = [c["url"].split("?")[0] for c in calls]
    for expected in ("/scribe/enroll/start", "/scribe/enroll/chunk",
                     "/scribe/enroll/finalize", "/scribe/enroll/result",
                     "/scribe/presets/rename"):
        assert expected in paths, f"{expected} missing from {paths}"
    # record-first, NAME-LAST: the rename happens AFTER the verdict.
    assert paths.index("/scribe/presets/rename") > paths.index("/scribe/enroll/result")
    # every enroll-face call carries the ENROLL token (never the page's ingest token).
    for c in calls:
        if c["url"].startswith("/scribe/enroll/") or "/presets/rename" in c["url"]:
            assert c["token"] == "ENROLL_TOK", f"wrong token class on {c['url']}"
    # chunks are seq-numbered 1..N (idempotent retry)
    seqs = [u.split("seq=")[1] for u in _urls(res, "/scribe/enroll/chunk")]
    assert seqs == ["1", "2"], seqs
    # the name is prefilled with the mic label + date (never a patient)
    assert res["namePrefill"].startswith("Room mic ")


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
