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
import struct
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp
import pytest
import structlog

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
    # 3 API routes (byte-identical to Slice A) + POST /scribe/bug (task #4) + 2 PWA identity
    # session routes (#12 12b) + 2 static PWA routes + 6 standalone-install assets
    # (manifest/icons/favicon/apple-touch-icon ×2).
    app = create_ingest_app(_config(tmp_path))
    got = {(r.method, r.get_info().get("path")) for r in app.router.routes()
           if r.method in ("GET", "POST")}
    assert ("POST", iw.INGEST_CHUNK_ROUTE) in got
    assert ("POST", iw.CLOSE_ROUTE) in got
    assert ("GET", iw.STATUS_ROUTE) in got
    assert ("POST", iw.BUG_ROUTE) in got
    assert ("POST", iw.SESSION_OPEN_ROUTE) in got               # #12 12b — ingest-class
    assert ("POST", iw.SESSION_CLOSE_ROUTE) in got              # #12 12b — ingest-class
    assert ("POST", iw.CONSENT_ROUTE) in got                    # #12 12c — ingest-class
    assert ("GET", iw.PAGE_ROUTE) in got
    assert ("GET", iw.APP_JS_ROUTE) in got
    assert ("GET", iw.MANIFEST_ROUTE) in got
    assert ("GET", iw.ICON_192_ROUTE) in got
    assert ("GET", iw.ICON_512_ROUTE) in got
    assert ("GET", iw.FAVICON_ROUTE) in got
    assert ("GET", iw.APPLE_TOUCH_ICON_ROUTE) in got
    assert ("GET", iw.APPLE_TOUCH_ICON_PRECOMPOSED_ROUTE) in got
    # every install asset has a registered GET route (derive from the set so a new asset
    # can't be added to _INSTALL_ASSET_PATHS without also being routed).
    for route in iw._INSTALL_ASSET_PATHS:
        assert ("GET", route) in got, route
    # no extra GET/POST routes crept in — in particular NO /sw.js (no service worker).
    assert len(got) == 15
    assert not any("sw.js" in path or "serviceworker" in path.lower()
                   for _, path in got), got


def test_inert_default_no_static_surface(tmp_path):
    # INERT default: the daemon starts NO server when disabled → no page, no
    # static routes exist to serve.
    from alfred.scribe.daemon import _maybe_start_ingest_server
    cfg = _config(tmp_path, enabled=False)
    server = asyncio.run(_maybe_start_ingest_server(cfg))
    assert server is None


# ---------------------------------------------------------------------------
# Task #1 — standalone-install surface (manifest + icons) + Task #3 favicon
# ---------------------------------------------------------------------------

def test_exempt_paths_include_install_assets_and_still_middleware_covered():
    # The bearer-exempt set EXPANDS to the manifest, both icons, and the favicon — but
    # they stay under the SAME every-route middleware (Host-pin + loopback + Sec-Fetch-Site),
    # so no bypass. Pin the additions AND that the page/app.js stay exempt too.
    assert iw.PAGE_ROUTE in iw._BEARER_EXEMPT_PATHS
    assert iw.APP_JS_ROUTE in iw._BEARER_EXEMPT_PATHS
    for route in iw._INSTALL_ASSET_PATHS:
        assert route in iw._BEARER_EXEMPT_PATHS, route
    # exactly page + app.js + the install-asset set — nothing else silently joined.
    assert iw._BEARER_EXEMPT_PATHS == frozenset({iw.PAGE_ROUTE, iw.APP_JS_ROUTE}) | iw._INSTALL_ASSET_PATHS
    # and the install-asset set is exactly the six standalone-install assets.
    assert iw._INSTALL_ASSET_PATHS == frozenset({
        iw.MANIFEST_ROUTE, iw.ICON_192_ROUTE, iw.ICON_512_ROUTE, iw.FAVICON_ROUTE,
        iw.APPLE_TOUCH_ICON_ROUTE, iw.APPLE_TOUCH_ICON_PRECOMPOSED_ROUTE,
    })


def test_manifest_served_standalone_and_secret_free(tmp_path):
    # GET /manifest.webmanifest — NO bearer needed, correct content type, and the
    # standalone-install fields Chrome ≥108 needs. HARD INVARIANT: SECRET-FREE (neither
    # token value appears in the served bytes).
    import json as _json
    cfg = _config(tmp_path, token="TOKEN_ABC")
    cfg.ingest_web.enroll_token = "ENROLL_XYZ"      # a 2nd secret that must also never leak

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            async with s.get(base + iw.MANIFEST_ROUTE) as r:   # NO bearer
                return r.status, r.headers.get("Content-Type"), await r.text()

    st, ctype, body = asyncio.run(_go())
    assert st == 200
    assert "application/manifest+json" in ctype
    m = _json.loads(body)
    assert m["display"] == "standalone"             # what drops the URL bar
    assert m["name"] == "STAY-C" and m["short_name"] == "STAY-C"
    assert m["start_url"] == "/"
    assert {i["sizes"] for i in m["icons"]} == {"192x192", "512x512"}
    assert all("maskable" in i["purpose"] for i in m["icons"])   # maskable install icons
    # SECRET-FREE — grep the served bytes for BOTH token values.
    assert "TOKEN_ABC" not in body and "ENROLL_XYZ" not in body


def test_icons_served_valid_png_and_secret_free(tmp_path):
    # Both icon routes serve real PNG bytes (192 + 512), no bearer, and SECRET-FREE.
    import struct
    cfg = _config(tmp_path, token="TOKEN_ABC")
    cfg.ingest_web.enroll_token = "ENROLL_XYZ"

    async def _go():
        out = {}
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            for route, size in ((iw.ICON_192_ROUTE, 192), (iw.ICON_512_ROUTE, 512)):
                async with s.get(base + route) as r:            # NO bearer
                    out[size] = (r.status, r.headers.get("Content-Type"), await r.read())
        return out

    out = asyncio.run(_go())
    for size, (st, ctype, body) in out.items():
        assert st == 200 and ctype == "image/png"
        assert body[:8] == b"\x89PNG\r\n\x1a\n"                 # valid PNG signature
        w, h = struct.unpack(">II", body[16:24])                # IHDR width/height
        assert (w, h) == (size, size), (size, w, h)
        # SECRET-FREE — the raw image bytes carry neither token.
        assert b"TOKEN_ABC" not in body and b"ENROLL_XYZ" not in body


def _parse_png(b: bytes) -> list[tuple[bytes, bytes]]:
    """Parse a PNG into a list of (chunk-tag, chunk-data), verifying every chunk CRC (computed
    over tag+data). Raises AssertionError on a bad signature or CRC — the structural belt
    under _solid_png that a signature+IHDR-only check misses."""
    import binascii
    assert b[:8] == b"\x89PNG\r\n\x1a\n", "bad PNG signature"
    i, chunks = 8, []
    while i < len(b):
        ln = struct.unpack(">I", b[i:i + 4])[0]
        tag = b[i + 4:i + 8]
        data = b[i + 8:i + 8 + ln]
        crc = struct.unpack(">I", b[i + 8 + ln:i + 12 + ln])[0]
        assert crc == (binascii.crc32(tag + data) & 0xFFFFFFFF), f"bad CRC on {tag!r}"
        chunks.append((tag, data))
        i += 12 + ln
    return chunks


def test_solid_png_internals_are_structurally_valid():
    # QA finding 6 — pin the PNG INTERNALS, not just the signature + IHDR dims. A _solid_png
    # mutant (CRC over data-without-tag, `raw = row * (size - 1)`, wrong colour-type/stride)
    # produces an undecodable icon → Chrome silently drops the >=144px install criterion and
    # stops offering the install prompt, with zero log signal. Parse every chunk, verify CRCs,
    # and confirm the decompressed pixel stream matches the declared truecolor dimensions.
    import zlib
    for name, png, size in (
        ("icon-192", pwa_assets.ICON_192_PNG, 192),
        ("icon-512", pwa_assets.ICON_512_PNG, 512),
        ("favicon", pwa_assets.FAVICON_PNG, 32),
        ("apple-touch", pwa_assets.APPLE_TOUCH_ICON_PNG, 180),
    ):
        chunks = _parse_png(png)                                 # verifies signature + all CRCs
        assert [t for t, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"], (name, chunks)
        ihdr = dict(chunks)[b"IHDR"]
        w, h, bit_depth, colour_type = struct.unpack(">IIBB", ihdr[:10])
        assert (w, h) == (size, size), (name, w, h)
        assert bit_depth == 8 and colour_type == 2, (name, bit_depth, colour_type)  # 8-bit truecolor
        # the IDAT zlib stream decompresses to exactly `size` rows of (1 filter byte + 3*size RGB).
        raw = zlib.decompress(dict(chunks)[b"IDAT"])
        assert len(raw) == size * (1 + 3 * size), (name, len(raw), size)


def test_favicon_served_200_and_no_reject_log(tmp_path):
    # Task #3 — Chrome auto-fetches /favicon.ico on every page load. Before, it was an
    # un-exempt bearer-required route → 401 → a warning-level scribe.ingest_web.rejected
    # (reason=bad_token) log per load (4+/session observed). Now it is served 200 and emits
    # NO rejected log for /favicon.ico. It is ALSO SECRET-FREE — the served bytes are grepped
    # for BOTH real token values (QA finding 2: a serve-time interpolation mutant must die
    # here, not just against the module-level constant).
    cfg = _config(tmp_path, token="TOKEN_ABC")
    cfg.ingest_web.enroll_token = "ENROLL_XYZ"

    async def _go():
        with structlog.testing.capture_logs() as caps:
            async with _serve(cfg) as base, aiohttp.ClientSession() as s:
                # a browser favicon fetch carries NO Authorization header.
                async with s.get(base + iw.FAVICON_ROUTE) as r:
                    st, ctype, body = r.status, r.headers.get("Content-Type"), await r.read()
        return st, ctype, body, caps

    st, ctype, body, caps = asyncio.run(_go())
    assert st == 200 and ctype == "image/png"
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
    assert b"TOKEN_ABC" not in body and b"ENROLL_XYZ" not in body   # SECRET-FREE served bytes
    rejects = [c for c in caps
               if c.get("event") == "scribe.ingest_web.rejected" and c.get("route") == iw.FAVICON_ROUTE]
    assert rejects == [], f"favicon must not emit a rejected log: {rejects}"


def test_all_install_assets_served_secret_free(tmp_path):
    # QA findings 2/3 root — the secret-free-served-bytes invariant must hold for EVERY route
    # in _INSTALL_ASSET_PATHS (so a new asset can't be added without inheriting the check), and
    # it must be checked at SERVE time (a serve-time interpolation mutant can't be seen by the
    # module-constant unit belt). Configure BOTH real tokens and grep every served body.
    cfg = _config(tmp_path, token="TOKEN_ABC")
    cfg.ingest_web.enroll_token = "ENROLL_XYZ"

    async def _go():
        out = {}
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            for route in sorted(iw._INSTALL_ASSET_PATHS):
                async with s.get(base + route) as r:             # NO bearer
                    out[route] = (r.status, await r.read())
        return out

    out = asyncio.run(_go())
    assert out, "no install assets to check"
    for route, (st, body) in out.items():
        assert st == 200, (route, st)
        assert b"TOKEN_ABC" not in body and b"ENROLL_XYZ" not in body, route


def test_install_assets_emit_no_rejected_log_on_browser_fetch(tmp_path):
    # QA finding 9 (finding 1's log-half) — generalise the favicon no-reject-log pin to the
    # apple-touch paths, the batch's headline new browser-probe routes (WebKit auto-fetches them
    # on Add-to-Home-Screen for the operator-ruled iPhone). Finding 1's concern is the
    # warning-level scribe.ingest_web.rejected spam class, so a plain browser fetch (no bearer,
    # no Sec-Fetch-Site) of each probe path must be served 200 AND emit NO rejected log for that
    # route. Enumerate the probe paths BY CONSTANT (not via _INSTALL_ASSET_PATHS): a removal from
    # the exempt set would drop the path from a set-derived loop and pass vacuously — this pin
    # must fail if apple-touch/favicon ever falls back to the bearer branch (401 + spam). The
    # exact-set membership pin (test_exempt_paths_…) covers set drift; this covers the behaviour.
    cfg = _config(tmp_path)
    probe_paths = (iw.FAVICON_ROUTE, iw.APPLE_TOUCH_ICON_ROUTE, iw.APPLE_TOUCH_ICON_PRECOMPOSED_ROUTE)

    async def _go():
        out = {}
        with structlog.testing.capture_logs() as caps:
            async with _serve(cfg) as base, aiohttp.ClientSession() as s:
                for route in probe_paths:
                    async with s.get(base + route) as r:          # NO bearer, no Sec-Fetch-Site
                        out[route] = r.status
        return out, caps

    out, caps = asyncio.run(_go())
    for route in probe_paths:
        assert out[route] == 200, (route, out[route])
    rejects = [(c.get("route"), c.get("reason")) for c in caps
               if c.get("event") == "scribe.ingest_web.rejected" and c.get("route") in probe_paths]
    assert rejects == [], f"browser-probe assets must not emit a rejected log: {rejects}"


def test_install_assets_still_host_pinned_rebind_blocked(tmp_path):
    # The expanded exempt surface stays under the Host-pin (the rebind guard): a DNS-rebind
    # request carrying an attacker domain as Host is refused (421) on EVERY install asset.
    # Derive the loop from _INSTALL_ASSET_PATHS (QA finding 3 — icon-512 was silently omitted;
    # deriving from the set means no current or future asset can be forgotten).
    cfg = _config(tmp_path)

    async def _go():
        out = {}
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            for route in sorted(iw._INSTALL_ASSET_PATHS):
                async with s.get(base + route,
                                 headers={"Host": f"evil.example.com:{cfg.ingest_web.port}"}) as r:
                    out[route] = r.status
        return out

    out = asyncio.run(_go())
    assert out and all(st == 421 for st in out.values()), out


def test_bearer_exempt_routes_all_sec_fetch_site_covered(tmp_path):
    # The Sec-Fetch-Site belt covers EVERY bearer-exempt route (page, app.js, and all six
    # install assets) — a cross-site fetch of any of them is refused (421). Derived from
    # _BEARER_EXEMPT_PATHS so dropping the belt from any single route (QA findings 3/8:
    # icon-512, favicon, apple-touch, and app.js were all unpinned) cannot survive.
    cfg = _config(tmp_path)

    async def _go():
        out = {}
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            for route in sorted(iw._BEARER_EXEMPT_PATHS):
                async with s.get(base + route, headers={"Sec-Fetch-Site": "cross-site"}) as r:
                    out[route] = r.status
        return out

    out = asyncio.run(_go())
    assert out and all(st == 421 for st in out.values()), out    # cross-origin refused everywhere


def test_install_assets_same_origin_and_absent_header_served(tmp_path):
    # ...and the REAL install fetch is not broken: a same-origin fetch (Sec-Fetch-Site:
    # same-origin, how the browser actually fetches the manifest/icons) and an absent-header
    # fetch (older browsers → fail-open) are BOTH served 200 for every install asset.
    cfg = _config(tmp_path)

    async def _go():
        out = {}
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            for route in sorted(iw._INSTALL_ASSET_PATHS):
                async with s.get(base + route, headers={"Sec-Fetch-Site": "same-origin"}) as r:
                    same = r.status
                async with s.get(base + route) as r:              # absent header → fail-open
                    absent = r.status
                out[route] = (same, absent)
        return out

    out = asyncio.run(_go())
    assert out and all(v == (200, 200) for v in out.values()), out


def test_page_head_links_manifest_and_theme_color():
    # The served HTML head wires the standalone install: <link rel="manifest">, <link
    # rel="icon">, <link rel="apple-touch-icon">, and a <meta name="theme-color">. The link
    # hrefs are BAKED from the route constants (QA findings 4/9 — a route rename must
    # propagate to the page, never leave it linking a now-un-exempt path); build the expected
    # literals FROM the constants so this pin fails the moment the page hardcodes a path.
    import json as _json
    html = render_index(_TOKEN)
    assert f'<link rel="manifest" href="{iw.MANIFEST_ROUTE}">' in html
    assert f'<link rel="icon" href="{iw.FAVICON_ROUTE}" sizes="any">' in html
    assert f'<link rel="apple-touch-icon" href="{iw.APPLE_TOUCH_ICON_ROUTE}">' in html
    # no route/theme placeholder survives into the served page (all baked out).
    for ph in (pwa_assets._THEME_COLOR_PLACEHOLDER, pwa_assets._MANIFEST_ROUTE_PLACEHOLDER,
               pwa_assets._FAVICON_ROUTE_PLACEHOLDER, pwa_assets._APPLE_TOUCH_ICON_ROUTE_PLACEHOLDER):
        assert ph not in html, ph
    # theme-color meta is the SAME constant the manifest uses (single source of truth).
    assert f'<meta name="theme-color" content="{pwa_assets.THEME_COLOR}">' in html
    assert _json.loads(pwa_assets.MANIFEST_JSON)["theme_color"] == pwa_assets.THEME_COLOR


def test_browser_convention_asset_paths_are_pinned_literals():
    # QA findings 1/4 completion — the finding-4 fix BAKES the <link> hrefs from the route
    # constants, which makes a LINK-DRIVEN path (the manifest, whose only fetch is via
    # <link rel="manifest">) rename-safe: the served page follows the constant. But favicon and
    # apple-touch are fetched by the browser at HARDCODED conventional literals INDEPENDENT of
    # any <link>: Chrome auto-requests /favicon.ico on every page load (the observed source of
    # the Task #3 401 spam), and WebKit probes /apple-touch-icon.png and the no-<link>
    # /apple-touch-icon-precomposed.png sibling on Add-to-Home-Screen. For these three, baking
    # the <link> is NOT sufficient — the constant itself must stay the conventional literal, or a
    # rename silently un-exempts the path the browser still hits and reintroduces the exact 401
    # warning-spam / degraded home-screen tile this batch shipped to kill, with a green suite
    # (finding 1's failure scenario). Pin the convention. MANIFEST_ROUTE / ICON_* are
    # deliberately NOT pinned here — they are link/manifest-driven and rename-safe via the bake.
    assert iw.FAVICON_ROUTE == "/favicon.ico"
    assert iw.APPLE_TOUCH_ICON_ROUTE == "/apple-touch-icon.png"
    assert iw.APPLE_TOUCH_ICON_PRECOMPOSED_ROUTE == "/apple-touch-icon-precomposed.png"


def test_sized_apple_touch_probe_is_quiet_404_no_warning(tmp_path):
    # Bundled nit — the ingest server never 404'd an unknown path: a SIZED apple-touch probe
    # (/apple-touch-icon-120x120.png; NOT one of the two canonical exempt paths) is a no-auth
    # browser fetch for an asset that does not exist, but it used to land in the bearer branch
    # and log a warning-level rejected(bad_token) 401 — the favicon-spam class one probe-family
    # later. It must now be a QUIET 404 with NO warning-level rejected log.
    cfg = _config(tmp_path)

    async def _go():
        with structlog.testing.capture_logs() as caps:
            async with _serve(cfg) as base, aiohttp.ClientSession() as s:
                async with s.get(base + "/apple-touch-icon-120x120.png") as r:   # NO bearer
                    st = r.status
        return st, caps

    st, caps = asyncio.run(_go())
    assert st == 404
    rejects = [c for c in caps if c.get("event") == "scribe.ingest_web.rejected"
               and "apple-touch-icon-120x120" in str(c.get("route", ""))]
    assert rejects == [], f"a sized apple-touch probe must not warn: {rejects}"


def test_sized_apple_touch_probe_with_token_is_not_masked(tmp_path):
    # The quiet-404 is gated on no-auth + GET so it can NEVER swallow a credentialed request:
    # a sized apple-touch path WITH a valid ingest token still reaches normal routing (404 from
    # the router — no such route — but NOT via the silent-probe short-circuit).
    cfg = _config(tmp_path)

    async def _go():
        async with _serve(cfg) as base, aiohttp.ClientSession() as s:
            async with s.get(base + "/apple-touch-icon-120x120.png",
                             headers={"Authorization": f"Bearer {_TOKEN}"}) as r:
                return r.status

    assert asyncio.run(_go()) == 404     # router 404 (no route) — the request was authorized first


def test_bearer_reason_split_no_token_vs_bad_token(tmp_path):
    # Bundled nit — the rejected-log reason now distinguishes NO Authorization (a benign
    # unauthenticated fetch) from a WRONG token (a credential probe). Both still 401 on an API
    # route, but the log reads no_token vs bad_token so the two signals are separable.
    cfg = _config(tmp_path)

    async def _go():
        out = {}
        with structlog.testing.capture_logs() as caps:
            async with _serve(cfg) as base, aiohttp.ClientSession() as s:
                async with s.get(base + iw.STATUS_ROUTE, params={"label": _LABEL}) as r:  # no auth
                    out["no_auth"] = r.status
                async with s.get(base + iw.STATUS_ROUTE, params={"label": _LABEL},
                                 headers={"Authorization": "Bearer WRONG-TOKEN"}) as r:   # wrong
                    out["wrong"] = r.status
        return out, caps

    out, caps = asyncio.run(_go())
    assert out["no_auth"] == 401 and out["wrong"] == 401
    reasons = [c.get("reason") for c in caps if c.get("event") == "scribe.ingest_web.rejected"
               and c.get("route") == iw.STATUS_ROUTE]
    assert "no_token" in reasons     # the unauthenticated fetch
    assert "bad_token" in reasons    # the wrong-credential probe


def test_manifest_install_has_no_service_worker():
    # The no-residue posture is intact: the install rides the MANIFEST ALONE. No service
    # worker route exists, and no serviceWorker registration / sw.js / Cache-API appears in
    # the manifest, the served page, or the app JS.
    import json as _json
    m = _json.loads(pwa_assets.MANIFEST_JSON)
    assert "serviceworker" not in _json.dumps(m).lower()     # no SW key in the manifest
    html = render_index(_TOKEN)
    js = pwa_assets.APP_JS
    for hay, where in ((html, "page"), (js, "app.js")):
        for banned in ("serviceWorker", "sw.js", ".register(", "caches.", "CacheStorage"):
            assert banned not in hay, f"no-SW violation: {banned} in {where}"


def test_manifest_and_icon_bytes_are_secret_free_unit():
    # UNIT belt (no server) — the module-level asset bytes themselves carry neither token,
    # regardless of what any live config embeds. A future edit that interpolated a secret
    # into an asset fails here even without a served request.
    for asset in (pwa_assets.MANIFEST_JSON, pwa_assets.ICON_192_PNG,
                  pwa_assets.ICON_512_PNG, pwa_assets.FAVICON_PNG,
                  pwa_assets.APPLE_TOUCH_ICON_PNG):
        raw = asset.encode() if isinstance(asset, str) else asset
        # a secret-shaped token embedded via render_index would be the leak vector; the assets
        # are STATIC (never see the token), so no bearer-shaped substring can be present.
        assert b"Bearer" not in raw
        assert b"data-ingest-token" not in raw


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


def test_pwa_runenroll_no_clinician_guard_is_not_silent():
    # Task #3 — the `if (!user) { return; }` silent no-op in runEnroll is gone (it was an
    # intentionally-left-blank violation: with scribe.clinicians empty, "Create a voiceprint"
    # did literally nothing). The guard now renders explicit feedback via
    # refuseEnrollNoClinician. (loadPresets keeps its OWN silent `if (!user) return` — it is
    # the DATA layer, and renderPresets already speaks; so scope this to runEnroll's body.)
    code = _code()
    run_body = code.split("async function runEnroll(rerecordId) {")[1].split("async function captureEnroll")[0]
    assert "if (!user) { return; }" not in run_body                # the silent no-op is gone
    assert "return refuseEnrollNoClinician()" in run_body          # ...replaced by the speaking guard
    helper_body = code.split("function refuseEnrollNoClinician() {")[1].split("\n  }")[0]
    assert "No clinicians are configured on this machine" in helper_body   # the ONE reachable case
    # QA finding 5 — the 'Select a clinician first' variant was UNREACHABLE (fillWho
    # auto-selects CLINICIANS[0] in EVERY non-empty case, so !user fires only when the list is
    # empty). The dead branch is removed rather than kept as a comment-lies trap.
    assert "Select a clinician first" not in code


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
    # NO service worker anywhere in the served page (the no-residue posture). The manifest
    # link IS now deliberately present (Task #1 standalone install) but carries no storage /
    # SW semantics — that install-without-a-service-worker invariant is pinned separately in
    # test_pwa_manifest_install_has_no_service_worker.
    assert "serviceWorker" not in html
    assert "sw.js" not in html
    assert ".register(" not in html


def test_pwa_session_token_is_memory_only_closure():
    # #12 12b — the server-issued identity session token lives ONLY in the `sessionToken`
    # closure var (memory-only, R5), exactly like enrollToken. Pinned in the SAME no-storage
    # class: it is a plain `let`, and the session additions introduced no storage API.
    code = _code()
    assert re.search(r"let sessionToken = '';", code)          # a plain closure let, not storage
    for banned in ("localStorage", "sessionStorage", "indexedDB", "IndexedDB",
                   "serviceWorker", "caches.", "CacheStorage", ".register("):
        assert banned not in code, f"R5 violation via session code: {banned}"


def test_pwa_session_open_close_wired_ingest_class():
    # #12 12b — the client opens/closes the identity session against the ingest-class routes
    # and carries X-Scribe-Session on subsequent calls (design §2.3).
    code = _code()
    assert "/scribe/session/open?user=" in code                # open binds a clinician slug
    assert "/scribe/session/close" in code                     # explicit teardown
    assert "X-Scribe-Session" in code                          # identity rides a header, not a query
    # the header is injected from the memory-only sessionToken (guarded so an explicit close
    # header wins) — never hardcoded, never read from storage.
    assert re.search(r"headers\['X-Scribe-Session'\] = sessionToken;", code)


def test_pwa_session_autobind_single_explicit_multi():
    # #12 12b Q3 — auto-bind when EXACTLY ONE clinician is configured; require an explicit
    # clinician selection when >1 (the who/who2 change handler binds). Pin both halves.
    code = _code()
    assert "if (CLINICIANS.length === 1) { bindSession(CLINICIANS[0]); }" in code   # auto@1
    who_handlers = code.split("$('who').addEventListener('change'")[1].split("$('new-preset')")[0]
    assert who_handlers.count("bindSession(user)") == 2        # both who + who2 change → re-bind


def test_pwa_consent_buttons_have_reentrancy_latch():
    # #12 12c — a double-tap on Confirmed/Declined must not double-POST (the 2nd POST's 409 would
    # run `label = null` after the first already started recording, poisoning the live encounter).
    code = _code()
    assert "let consentActing = false;" in code
    assert code.count("if (consentActing) { return; }") == 2   # both handlers latch at entry
    # the latch is cleared when returning to idle so a fresh visit is never wedged.
    stop_body = code.split("function stopEncounter")[1].split("async function stop(")[0]
    assert "consentActing = false" in stop_body


def test_pwa_session_rebind_is_close_then_open():
    # atomic re-bind on a clinician switch (design §2.4): bindSession closes the old session
    # BEFORE opening the new, and closeSession clears the closure var FIRST so a concurrent
    # re-bind cannot reuse the dropped token.
    code = _code()
    bind_body = code.split("async function bindSession(clin) {")[1].split("}")[0]
    assert "await closeSession();" in bind_body
    assert bind_body.index("closeSession") < bind_body.index("openSession")
    close_body = code.split("async function closeSession() {")[1].split("async function bindSession")[0]
    assert re.search(r"sessionToken = '';.*if \(!t\)", close_body, re.DOTALL)   # cleared first


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
    # COUNT every <input, not just the id-first ones: the old regex required `<input id="`,
    # so `<input type="text" id="patient">` would have been INVISIBLE to it.
    assert code.count("<input") == 3, "a new <input> appeared in the client"
    ids = set(re.findall(r"<input[^>]*\bid=\"([a-z0-9_-]+)\"", code))
    assert ids == {"tok", "nm", "nm2"}, f"unexpected free-text input(s): {ids}"
    # no text-capturing control may be injected by the JS at RUNTIME (a JS-built patient field
    # is the real injection surface — the served-HTML scan cannot see runtime-built controls).
    for banned in ("<textarea", "contenteditable"):
        assert banned not in code, f"JS-injected text control: {banned}"
    # The ONLY static-HTML free-text beyond the JS ones is the task-#4 bug-report form: exactly
    # one <input id="bug-summary"> + one <textarea id="bug-detail">, and NOTHING else (no
    # contenteditable, no second textarea). It is a diagnostic surface with its own PHI-caution
    # banner and lives OUTSIDE the record view (pinned above) — never a patient/encounter field.
    html = render_index(_TOKEN)
    assert html.lower().count("<textarea") == 1               # ONLY the bug detail
    assert 'id="bug-detail"' in html
    assert "contenteditable" not in html.lower()
    static_inputs = set(re.findall(r"<input[^>]*\bid=\"([a-z0-9_-]+)\"", html))
    assert static_inputs == {"bug-summary"}, f"unexpected static <input>(s): {static_inputs}"
    # the bug form carries the "no patient details" caution + is not in the record view.
    bug_section = html.split('<section id="bug"')[1].split("</section>")[0].lower()
    assert "patient details" in bug_section and "banner" in bug_section
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
    # the token is READ from the paste field into a local, then assigned to the memory-only
    # closure var (finding-7 refactor: empty-paste is caught before the assignment, so the
    # source is `$('tok').value` via `val`, never the DOM dataset).
    assert "const val = $('tok').value" in code
    assert "enrollToken = val;" in code


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
    # an UNRECOGNISED value must fall back, never throw / render undefined — and the
    # lookup must be hasOwnProperty, not `CHIP_COPY[fit] ||` (which would resolve a
    # PROTOTYPE key like "constructor" and render a function body into the chip).
    assert "hasOwnProperty.call(CHIP_COPY, fit)" in code
    assert "CHIP_COPY[fit] ||" not in code


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
    # EXACT count, not >=1: with `>=1` a mutant could drop ?user from ONE of the call
    # sites and still pass on the strength of the others (mutation-proven). There are
    # exactly three user-keyed preset routes: rename (presets view), delete, rename
    # (post-verdict naming).
    assert code.count("'?user=' + encodeURIComponent(user) + '&preset='") == 3


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


def test_node_is_available_for_the_behavioural_layer():
    # UNCONDITIONAL (feedback_regression_pin_unconditional): the behavioural tests below
    # are the ONLY thing that binds what the client actually DOES. If they were skip-gated
    # on node, a node-less merge gate would silently drop the entire layer and the suite
    # would still go green. Fail LOUD instead — a missing node is a broken gate, not a
    # reason to stop checking.
    assert _NODE is not None, (
        "node is required for the PWA behavioural harness (tests/pwa_enroll_harness.mjs). "
        "Install node, or the client's real behaviour is untested."
    )
    assert _HARNESS.is_file()


def test_behaviour_binds_preset_before_the_first_chunk(tmp_path):
    # THE ordering invariant: the server LOCKS the binding at the first chunk, so a bind
    # that lands after chunk 1 is a 409 and the note's diarize_provenance is permanently
    # absent. The client MUST bind first.
    res = _drive("record_binds_before_first_chunk", tmp_path)
    order = [c["url"].split("?")[0] for c in res["calls"]]
    bind = order.index("/scribe/encounter/preset")
    chunk = order.index("/scribe/ingest-chunk")
    assert bind < chunk, f"bind must precede the first chunk, got {order}"


def test_behaviour_no_preset_never_binds_and_still_records(tmp_path):
    # "No preset — attribution off" is a FIRST-CLASS choice: no binding call at all, and
    # the encounter still records normally.
    res = _drive("record_no_preset_never_binds", tmp_path)
    assert _urls(res, "/scribe/encounter/preset") == []
    assert _urls(res, "/scribe/ingest-chunk")          # chunks still flow


def test_behaviour_bind_failure_never_blocks_the_encounter(tmp_path):
    # A 409/failed binding must NEVER block Start — the encounter simply runs un-anchored
    # (audio is never lost to a preset problem).
    res = _drive("record_bind_failure_does_not_block", tmp_path)
    assert _urls(res, "/scribe/encounter/preset")      # it tried
    assert _urls(res, "/scribe/ingest-chunk")          # ...and recorded anyway


def test_behaviour_iphone_container_maps_to_an_allowlisted_ext(tmp_path):
    # iOS supports ONLY audio/mp4 → the chunk must go out as ext=m4a (on the allowlist),
    # never ext=mp4 (which the ingest route rejects with unsupported_ext).
    res = _drive("record_ios_maps_mp4_to_m4a", tmp_path)
    chunks = _urls(res, "/scribe/ingest-chunk")
    assert chunks and all("ext=m4a" in u for u in chunks), chunks
    assert not any("ext=mp4" in u for u in chunks)


def test_behaviour_unknown_preset_fit_does_not_break_the_chip(tmp_path):
    # 5b will emit warming/weak/none. A 5a client must render something sane, not crash.
    res = _drive("record_unknown_preset_fit_tolerated", tmp_path)
    assert res["chip"] and "undefined" not in res["chip"].lower()


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
    # the rename lands on the WIRE with ?user (the store is user-keyed; without it the
    # server 400s and the voiceprint silently keeps its placeholder name).
    rename = _urls(res, "/scribe/presets/rename")
    assert rename and all("user=np_jamie" in u for u in rename), rename
    # the name is prefilled with the MIC LABEL + date (memo: "name the place and mic"),
    # never a patient. The harness's mic reports "Built-in Mic".
    assert "Built-in Mic" in res["namePrefill"]
    assert re.search(r"\d{4}-\d{2}-\d{2}", res["namePrefill"])


# ── B1: the record view NEVER goes silent about attribution ─────────────────

@pytest.mark.parametrize("scenario", ["registry_empty", "registry_all_revoked",
                                      "registry_all_corrupt", "registry_all_stale"])
def test_behaviour_every_unusable_registry_state_shows_a_banner(scenario, tmp_path):
    # INTENTIONALLY-LEFT-BLANK. The original code only banner-ed the engine-INCOMPATIBLE
    # cause, so an all-REVOKED or all-CORRUPT registry fell through to an EMPTY message:
    # the WORST state emitted LESS signal than the empty one, and the clinician recorded
    # with no indication attribution was off. Every unusable state must speak.
    res = _drive(scenario, tmp_path)
    assert "banner" in res["presetMsg"], f"{scenario} rendered NO signal: {res['presetMsg']!r}"
    assert res["startDisabled"] is False          # ...and Start is NEVER blocked


# ── B2: a finalize refusal fails FAST (no 150-second poll to a generic error) ─

def test_behaviour_finalize_409_fails_fast_with_the_right_copy(tmp_path):
    # The server answers 409 and leaves the session "recording", so /enroll/result would
    # say {state:"processing"} FOREVER. The client used to poll 300x500ms and then show a
    # generic error. It must fail immediately, with the copy that already exists for 409.
    res = _drive("enroll_finalize_409", tmp_path)
    paths = [c["url"].split("?")[0] for c in res["calls"]]
    assert "/scribe/enroll/finalize" in paths
    assert "/scribe/enroll/result" not in paths           # NO polling a doomed session
    assert "in use by a recording" in res["enrollBody"]   # the specific 409 copy
    assert "/scribe/enroll/abandon" in paths              # ...and the RAM bytes are dropped


# ── picker / MRU (the layer B3's broken harness never exercised) ─────────────

def test_behaviour_mru_is_preselected(tmp_path):
    res = _drive("mru_preselected", tmp_path)
    assert res["pickerValue"] == "pst-b"                  # the server's MRU, not the first
    assert res["pickerHtml"].count("<option") == 3        # no-preset + 2 usable


def test_behaviour_unusable_server_mru_is_defended(tmp_path):
    # Even if the server wrongly offers an unusable preset as the MRU, the client must not
    # select it — it cannot attribute, and Jamie would never know.
    res = _drive("mru_points_at_unusable", tmp_path)
    assert res["pickerValue"] == ""                       # falls back to "no preset"


def test_behaviour_picker_offers_only_usable_presets(tmp_path):
    # M5: revoked / corrupt / engine-incompatible presets must NEVER be selectable.
    res = _drive("picker_excludes_unusable", tmp_path)
    assert res["pickerHtml"].count("<option") == 2        # no-preset + the ONE usable
    assert "pst-a" in res["pickerHtml"]
    for bad in ("pst-r", "pst-c", "pst-e"):
        assert bad not in res["pickerHtml"], f"unusable preset {bad} was offered"


def test_behaviour_picker_is_locked_while_recording(tmp_path):
    # M4: mid-encounter the preset (and clinician) must not change — the binding is
    # already locked server-side, so an editable picker would only mislead.
    res = _drive("picker_locked_while_recording", tmp_path)
    assert res["pickerDisabled"] is True
    assert res["whoDisabled"] is True


def test_behaviour_prototype_preset_fit_does_not_leak_a_function(tmp_path):
    # `CHIP_COPY[fit] ||` would resolve "constructor" off the PROTOTYPE and render a
    # function body into the chip. hasOwnProperty closes it.
    res = _drive("record_prototype_preset_fit_tolerated", tmp_path)
    assert "function" not in res["chip"].lower()
    assert res["chip"] == "attribution: unarmed"


# ── W2: enrolment and encounter recording are MUTUALLY EXCLUSIVE ────────────

def test_behaviour_cannot_enroll_while_an_encounter_is_recording(tmp_path):
    # The enrolment buffer would capture LIVE PATIENT SPEECH — on a surface whose entire
    # consent basis is "the enrolling clinician's own voice".
    res = _drive("enroll_blocked_during_encounter", tmp_path)
    paths = [c["url"].split("?")[0] for c in res["calls"]]
    assert "/scribe/enroll/start" not in paths           # refused BEFORE any session
    assert "encounter is recording" in res["enrollBody"]


def test_behaviour_staged_enroll_screen_cannot_fire_during_a_live_encounter(tmp_path):
    # THE COMPOSED PATH — a consent violation reachable by ORDINARY NAVIGATION, no race:
    # stage the enrolment intro on #/presets (its [Start recording] listener goes live while
    # `recording` is still false), start an encounter on #/record, come back, and tap the
    # staged button. The guard in runEnroll() fires at the INTENT moment (screen opens); the
    # microphone is acquired in captureEnroll(), arbitrarily later. Only a re-check AT THE
    # ACQUISITION closes it — otherwise a second recorder opens on the live patient mic and
    # those windows are folded into a PERMANENT biometric centroid.
    res = _drive("enroll_staged_then_encounter_composed", tmp_path)
    assert res["staged"] is True, "the scenario never staged the intro — it proves nothing"
    paths = [c["url"].split("?")[0] for c in res["calls"]]
    assert "/scribe/enroll/start" not in paths          # no enrolment session was opened
    assert "/scribe/enroll/chunk" not in paths          # no patient audio was uploaded
    assert res["micOpensOnStagedClick"] == 0            # the mic was never even acquired
    assert "encounter is recording" in res["enrollBody"]


def test_behaviour_cannot_start_an_encounter_while_enrolling(tmp_path):
    # The reverse direction. NOTE: routing away from a live capture now tears it down, so a
    # user cannot NAVIGATE to Start with an enrolment running — this drives the (hidden-view)
    # Start button directly, pinning start()'s own guard as the belt under that teardown.
    res = _drive("encounter_blocked_during_enroll", tmp_path)
    paths = [c["url"].split("?")[0] for c in res["calls"]]
    assert "/scribe/ingest-chunk" not in paths           # the encounter never starts
    assert "voiceprint recording first" in res["status"]
    assert res["micOpens"] == 1                          # only the enrolment's own mic open


def test_behaviour_encounter_start_loses_the_race_against_a_live_enrolment(tmp_path):
    # `enrollSession` is set only AFTER getUserMedia + /enroll/start resolve. A Start clicked
    # inside that window sees no session — so a guard that reads it alone is a check-then-act
    # across a world-changing await. The mic CLAIM is taken synchronously, before the awaits.
    res = _drive("encounter_start_races_enroll_capture", tmp_path)
    paths = [c["url"].split("?")[0] for c in res["calls"]]
    assert "/scribe/enroll/start" in paths              # the enrolment proceeded...
    assert "/scribe/ingest-chunk" not in paths          # ...and the encounter did NOT start
    assert "voiceprint recording first" in res["status"]
    assert res["micOpens"] == 1                         # ONE mic open, not two


def test_behaviour_staged_enrolment_loses_the_race_against_encounter_start(tmp_path):
    # The mirror: `recording` is set only AFTER start()'s getUserMedia await, so a staged
    # [Start recording] fired inside that window reads `recording === false`.
    res = _drive("enroll_capture_races_encounter_start", tmp_path)
    paths = [c["url"].split("?")[0] for c in res["calls"]]
    assert "/scribe/enroll/start" not in paths          # no enrolment session was opened
    assert "/scribe/ingest-chunk" in paths              # the encounter ran normally
    assert res["micOpens"] == 1                         # ONE mic open, not two
    assert "encounter is recording" in res["enrollBody"]


def test_behaviour_routing_away_mid_enrolment_halts_and_abandons(tmp_path):
    # A capture left running behind a HIDDEN view keeps recording the room and POSTing
    # enrolment chunks, and its RAM-held bytes sit resident until the 10-minute TTL (two of
    # them 429 the next attempt against the 2-session cap). Leaving the view ends the capture.
    res = _drive("route_away_mid_enroll_abandons", tmp_path)
    assert res["recorderRecordingBefore"] is True        # ...it really was capturing
    assert res["recorderRecordingAfter"] is False        # the recorder is stopped
    paths = [c["url"].split("?")[0] for c in res["calls"]]
    assert "/scribe/enroll/abandon" in paths             # ...and the RAM bytes are dropped


# ── BLOCK: a teardown fired DURING captureEnroll's awaits (the generation token) ─
# captureEnroll claims micOwner synchronously but registers enrollHalt only AFTER its
# getUserMedia + /enroll/start awaits. A route() in that window used to leave micOwner stuck
# and (post-await) a live capture behind a hidden view — the patient-recording path DoS'd
# ("cancel the voiceprint" with none to cancel). Each pin fires the teardown mid-await and
# proves the flow bails cleanly AND a following encounter can still record (mic released).

def test_behaviour_teardown_during_getusermedia_releases_the_mic(tmp_path):
    res = _drive("teardown_during_getusermedia", tmp_path)
    paths = [c["url"].split("?")[0] for c in res["calls"]]
    assert "/scribe/enroll/start" not in paths           # bailed at GEN CHECK #1 (before start)
    assert "/scribe/enroll/chunk" not in paths           # no enrolment window ever ran
    assert "/scribe/ingest-chunk" in paths               # ...and the encounter DID start (mic freed)


def test_behaviour_teardown_during_enroll_start_abandons_and_releases(tmp_path):
    res = _drive("teardown_during_enroll_start", tmp_path)
    paths = [c["url"].split("?")[0] for c in res["calls"]]
    assert "/scribe/enroll/start" in paths               # the session was opened (held mid-await)
    assert "/scribe/enroll/abandon" in paths             # ...GEN CHECK #2 drops its RAM bytes
    assert "/scribe/enroll/chunk" not in paths           # no window ran behind the hidden view
    assert "/scribe/ingest-chunk" in paths               # ...and the encounter started (mic freed)


def test_behaviour_teardown_during_finalize_does_not_render_or_abandon(tmp_path):
    # Residual #5. The naming form must NOT render into a torn-down body, and the FINALIZING
    # session must NOT be abandoned (the worker is writing the centroid — abandon would race it
    # and silently lose the voiceprint; it lands under its placeholder name, recoverable via Rename).
    res = _drive("teardown_during_finalize_poll", tmp_path)
    assert "Voiceprint made" not in res["enrollBody"]     # no verdict UI rendered post-teardown
    assert "nm2" not in res["enrollBody"]                 # no naming form in the dead body
    paths = [c["url"].split("?")[0] for c in res["calls"]]
    assert "/scribe/enroll/finalize" in paths             # ...finalize really was issued
    assert "/scribe/enroll/abandon" not in paths          # the finalizing session is NOT abandoned


def test_behaviour_teardown_during_finalize_call_does_not_render_the_refusal(tmp_path):
    # The PRE-poll gen check (distinct from the in-poll one): a route-away DURING the finalize
    # POST, on a !ok finalize (409), would otherwise render the 409 copy via enrollFailed into
    # the torn-down body. The flow never reaches the poll on a !ok finalize, so only the pre-poll
    # check can catch it.
    res = _drive("teardown_during_finalize_call", tmp_path)
    assert "in use by a recording" not in res["enrollBody"]   # the 409 copy never rendered
    assert res["enrollBody"] == ""                            # body stays the torn-down empty
    paths = [c["url"].split("?")[0] for c in res["calls"]]
    assert "/scribe/enroll/finalize" in paths


def test_behaviour_stale_bail_never_clobbers_a_newer_enrolments_mic_claim(tmp_path):
    # THE OWNERSHIP-DISCIPLINE INVARIANT — the fix's load-bearing property, and precisely the
    # "survives a green suite" class: every OTHER await-teardown scenario is single-capture, so
    # a bail that wrongly clears micOwner is a no-op there. This is the multi-capture race that
    # gives the invariant teeth. Capture A parks on getUserMedia across a teardown (the belt
    # frees micOwner); capture B claims the freed mic and parks on /enroll/start; A resumes at
    # GEN CHECK #1, STALE. A must stop only its OWN stream — never write micOwner, which B now
    # owns. An unconditional clear reopens the 2-mic consent breach the whole fix closed.
    res = _drive("ownership_stale_bail_keeps_newer_claim", tmp_path)
    paths = [c["url"].split("?")[0] for c in res["calls"]]
    assert "/scribe/enroll/start" in paths               # capture B reached the claim-holding state
    assert "/scribe/ingest-chunk" not in paths           # the encounter was REFUSED (B owns the mic)
    assert "voiceprint recording first" in res["status"] # ...with the mutual-exclusion copy
    assert res["micOpens"] == 2                           # A + B only; a 3rd = the encounter's = breach


def test_behaviour_stale_bail_at_gen_check_2_never_clobbers_a_newer_claim(tmp_path):
    # THE SAME INVARIANT ONE AWAIT LATER — closing the CLASS, not just the GEN CHECK #1 instance.
    # GEN CHECK #2's stale bail carries the identical ownership discipline, and it is the more
    # consequential path: A has already opened a server session, so a clobber there races a flow
    # that owns RAM bytes. A parks on /enroll/start (not getUserMedia), teardown frees micOwner,
    # B claims it and parks on ITS /enroll/start, A resumes stale at GEN CHECK #2. A must abandon
    # its own session and stop its stream but NEVER write micOwner (B owns it).
    res = _drive("ownership_stale_bail_2_keeps_newer_claim", tmp_path)
    paths = [c["url"].split("?")[0] for c in res["calls"]]
    assert paths.count("/scribe/enroll/start") == 2      # BOTH captures opened a session
    assert "/scribe/enroll/abandon" in paths             # A's GEN CHECK #2 dropped its OWN bytes
    assert "/scribe/ingest-chunk" not in paths           # the encounter was REFUSED (B owns the mic)
    assert "voiceprint recording first" in res["status"]
    assert res["micOpens"] == 2                           # A + B only; a 3rd = breach
    # A abandoned ITS session (enr-1), never B's (enr-2, still live).
    abandons = [c["url"] for c in res["calls"] if "/scribe/enroll/abandon" in c["url"]]
    assert any("enr-1-abc" in u for u in abandons), abandons
    assert not any("enr-2-abc" in u for u in abandons), abandons


# ── WARN-1: hash routing (the shim's no-op addEventListener hid this entirely) ─

def test_behaviour_hash_routing_toggles_the_two_views(tmp_path):
    # If the hashchange handler is dropped (or the hide-toggles inverted), the clinician can
    # NEVER reach the presets view — the whole enrolment feature is unreachable — and every
    # click-driven test still passes, because route() ran once at boot.
    res = _drive("routing_toggles_views", tmp_path)
    assert res["atBoot"] == {"record": True, "presets": False,
                             "navRecordOn": True, "navPresetsOn": False}
    assert res["atPresets"] == {"record": False, "presets": True,
                                "navRecordOn": False, "navPresetsOn": True}
    assert res["backAtRecord"] == res["atBoot"]          # ...and back again


# ── WARN-2: the INERT box (enroll_token unset) is the DEFAULT ship posture ───

def test_behaviour_inert_box_record_view_offers_no_enrolment(tmp_path):
    # With enroll_token unset EVERY enroll-face path 404s (/scribe/presets included). The
    # record view must not offer "Create one" — that walks the clinician through a token
    # paste AND a mic-permission prompt only to die on a 404. Say what is true, offer nothing.
    res = _drive("inert_record_view", tmp_path)
    assert "banner" in res["presetMsg"]                  # still SPEAKS (attribution is off)
    assert "not set up on this machine" in res["presetMsg"]
    assert "Create one" not in res["presetMsg"]          # ...but invites nothing
    assert res["startDisabled"] is False                 # recording is unaffected
    assert res["pickerHtml"].count("<option") == 1       # just "No preset"


def test_behaviour_inert_box_presets_view_hides_create_and_refuses_early(tmp_path):
    res = _drive("inert_presets_view", tmp_path)
    assert res["newPresetHidden"] is True                # the CREATE button is not offered
    assert "not set up on this machine" in res["presetsList"]
    # ...and even if the button is reached anyway, the refusal comes BEFORE the token paste
    # and BEFORE the microphone prompt.
    assert "not set up on this machine" in res["enrollBody"]
    assert res["micOpens"] == 0
    paths = [c["url"].split("?")[0] for c in res["calls"]]
    assert "/scribe/enroll/start" not in paths


# ── Task #3: the "Create a voiceprint" button never silently no-ops ──────────

def test_behaviour_no_clinician_configured_shows_explicit_feedback(tmp_path):
    # THE live 2026-07-16 root cause: scribe.clinicians empty ⇒ `user` unset ⇒ the old
    # `if (!user) { return; }` made "Create a voiceprint" do literally nothing. It must now
    # render an explicit, actionable message AND never open the mic or a token prompt (the
    # !user guard fires BEFORE needEnrollToken + getUserMedia).
    res = _drive("enroll_no_clinician_configured", tmp_path)
    assert res["enrollTitle"] == "No clinician configured"
    assert "No clinicians are configured on this machine" in res["enrollBody"]   # the reachable copy
    assert res["micOpens"] == 0                          # the mic was never acquired
    assert res["hasTokenPrompt"] is False               # ...nor a token paste demanded
    paths = [c["url"].split("?")[0] for c in res["calls"]]
    assert "/scribe/enroll/start" not in paths          # no enrolment session was opened


def test_behaviour_empty_token_paste_is_not_a_dead_continue(tmp_path):
    # QA finding 7 — pre-existing sibling silent no-op in needEnrollToken: clicking Continue
    # with an EMPTY token field used to resolve false AND leave the form up, so every later
    # Continue click hit `!pendingToken` and did literally nothing (a permanently dead button —
    # the exact 'button does nothing' this fix round exists to eliminate). Now: empty → a
    # visible message + the prompt stays live; a REAL paste on the SAME button then proceeds.
    res = _drive("enroll_empty_token_then_valid", tmp_path)
    assert "Enter the enrolment token to continue" in res["tokMsgAfterEmpty"]
    assert res["hasTokAfterEmpty"] is True               # the prompt stayed on-screen (not consumed)
    assert res["enrollStartAfterEmpty"] is False         # an empty paste opened no session
    assert res["micOpensAfterEmpty"] == 0                # ...and no mic
    assert res["hasEnGoAfterValid"] is True              # the SAME Continue then advanced — not dead


# ── Task #4: bug report — capture UI (button both views, ring buffer, visible confirm/fail) ─

def test_behaviour_bug_report_carries_phi_free_context_and_ring_and_confirms(tmp_path):
    # The incident, end to end: with NO clinician configured, tapping "Create a voiceprint"
    # rings a breadcrumb; the bug report then POSTs that trace + PHI-FREE auto-context and
    # confirms VISIBLY. This is exactly the diagnosability the 2026-07-16 dead-button bug lacked.
    res = _drive("bug_report_flow", tmp_path)
    assert res["bugOpenNotHidden"] is True                    # the form opened (visible)
    posts = res["bugPosts"]
    assert len(posts) == 1, posts
    post = posts[0]
    assert post["summary"] == "button does nothing"
    ctx = post["context"]
    assert ctx["view"] == "#/presets" and ctx["clinicians_len"] == 0 and ctx["user"] == ""
    assert "HarnessUA" in ctx["ua"] and ctx["client_ts"]      # PHI-free context attached
    # the RAM ring snapshot rode along AND carried the exact code-path breadcrumb.
    assert isinstance(post["events"], list) and post["events"]
    assert any("blocked: no clinician configured" in e for e in post["events"])
    assert "Thank you" in res["bugMsg"]                       # VISIBLE confirm


def test_behaviour_bug_report_empty_is_not_a_silent_noop(tmp_path):
    # ILB — sending an empty report renders a message and POSTs nothing (never a silent no-op).
    res = _drive("bug_report_empty", tmp_path)
    assert "Please describe the problem" in res["bugMsg"]
    assert res["bugPosts"] == []                              # nothing sent


def test_behaviour_bug_report_server_error_shows_visible_failure(tmp_path):
    # A non-2xx response renders a VISIBLE failure (intentionally-left-blank), never a silent
    # swallow — the operator knows the report did not land.
    res = _drive("bug_report_server_error", tmp_path)
    assert len(res["bugPosts"]) == 1                          # it tried
    assert "Could not send" in res["bugMsg"] and "500" in res["bugMsg"]


def test_behaviour_bug_report_session_cap_blocks_visibly(tmp_path):
    # Client-side per-session cap (spec: ~10/session; a stuck client must not fill the disk).
    # With the cap at 2, the 3rd submit is blocked with a VISIBLE message and does NOT POST —
    # only the first two land. The server's max_open_reports 429 is the independent backstop.
    res = _drive("bug_report_session_cap", tmp_path)
    assert len(res["bugPosts"]) == 2                         # only up to the cap were sent
    assert "maximum number of reports" in res["bugMsg"]      # ...and the 3rd said so, visibly


def test_bug_max_per_session_is_embedded_from_config():
    # the cap number lives in ONE place (config) and is embedded into the page for the client
    # to read — a config change propagates (no hardcoded client literal).
    html = render_index(_TOKEN, ["np_jamie"], bug_max_per_session=3)
    assert 'data-bug-max="3"' in html
    assert pwa_assets._BUG_MAX_PLACEHOLDER not in html       # baked out
    code = _code()
    assert "BUG_MAX_PER_SESSION" in code and "bugSubmitCount" in code
    assert "dataset.bugMax" in code                          # read from the page, not hardcoded


def test_pwa_bug_affordance_on_both_views_and_ring_is_memory_only():
    # STRUCTURAL — a "Report a problem" affordance on BOTH views, and the ring buffer is
    # memory-only (no storage), PHI-free (status-code + breadcrumb, capped).
    html = render_index(_TOKEN)
    record = html.split('<section id="view-record">')[1].split("</section>")[0]
    presets = html.split('<section id="view-presets"')[1].split("</section>")[0]
    assert 'id="bug-open-record"' in record and 'id="bug-open-presets"' in presets
    code = _code()
    # ring is an in-memory array, capped, memory-only (no storage APIs — covered by the
    # no-browser-storage test; here pin the cap + snapshot-on-submit).
    assert "const bugRing = []" in code and "BUG_RING_MAX" in code
    assert "bugRing.slice()" in code                          # a SNAPSHOT rides the POST
    assert "'/scribe/bug'" in code                            # posts to the ingest-token route


# ── N6: esc() is the attribute-context belt ─────────────────────────────────

def test_behaviour_quote_bearing_preset_id_cannot_break_out_of_an_attribute(tmp_path):
    # Every use site interpolates into an ATTRIBUTE (<option value="…">, data-id="…"). The
    # config-load clinician gate closed the exploitable path for SLUGS, but preset ids/names
    # come from the preset store — esc() must escape quotes, or a quote-bearing value breaks
    # out of the attribute and injects an event handler.
    res = _drive("quote_in_preset_id", tmp_path)
    for html in (res["pickerHtml"], res["presetsList"]):
        # escaped → `onfocus=&quot;steal()` (inert text). Unescaped → `onfocus="steal()`,
        # a live handler: the raw quote closed value=" and the rest became markup.
        assert 'onfocus="' not in html, html
        assert "&quot;" in html                          # the quotes were escaped, not raw


def test_pwa_esc_escapes_quotes_for_attribute_contexts():
    # The structural belt under the behavioural pin above: the textContent->innerHTML trick
    # (the shape this replaced) escapes & < > but NOT quotes — safe in a TEXT context, unsafe
    # in the attribute contexts this client actually uses.
    code = _code()
    esc_body = code.split("function esc(s)")[1].split("}")[0]
    for entity in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
        assert entity in esc_body, f"esc() does not produce {entity}"
    assert "textContent" not in esc_body                 # not the quote-blind trick


# ── RAM custody: /enroll/abandon is actually WIRED ──────────────────────────

def test_behaviour_cancel_abandons_the_session_now(tmp_path):
    # RAM-only custody is the security centrepiece; "drop the bytes NOW" must not be a
    # route nobody calls (the bytes would otherwise sit until the 10-minute TTL, and two
    # abandoned attempts would 429 the next one against the 2-session cap).
    res = _drive("enroll_cancel_abandons", tmp_path)
    paths = [c["url"].split("?")[0] for c in res["calls"]]
    assert "/scribe/enroll/abandon" in paths


def test_behaviour_bind_route_uses_the_INGEST_token(tmp_path):
    # M8 — the security half. The binding is an ENCOUNTER-class capability. Sending the
    # ENROLL token here is a 401 wrong_token_class and attribution SILENTLY never arms.
    res = _drive("record_binds_before_first_chunk", tmp_path)
    binds = [c for c in res["calls"] if c["url"].startswith("/scribe/encounter/preset")]
    assert binds and all(c["token"] == "INGEST_TOK" for c in binds), binds


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


# ---------------------------------------------------------------------------
# clinicians: DUAL-USE identity — validated at config load (fail-closed)
# ---------------------------------------------------------------------------

def test_non_slug_clinician_is_dropped_loudly():
    # The id is the attest identity AND the enrolment identity AND a directory name AND
    # is embedded in the served page. "NP Jamie" would attest fine but fail-close EVERY
    # enrolment with 403 — after the clinician had already been asked to record.
    import structlog

    from alfred.scribe.config import load_from_unified
    with structlog.testing.capture_logs() as cap:
        cfg = load_from_unified({"scribe": {
            "encounter_salt": _SALT, "stt": {"provider": "fake"},
            "clinicians": ["np_jamie", "NP Jamie", 'bad"quote', "dr_x"],
        }})
    assert cfg.clinicians == ["np_jamie", "dr_x"]          # invalid entries DROPPED
    errs = [c for c in cap if c.get("event") == "scribe.config.invalid_clinician"]
    assert len(errs) == 2                                   # ...loudly, one per entry


def test_clinician_regex_is_lockstep_with_the_enrolment_identity():
    from alfred.scribe.config import _CLINICIAN_RE
    from alfred.scribe.enrollment import USER_RE
    assert _CLINICIAN_RE.pattern == USER_RE.pattern


def test_page_cannot_be_broken_out_of_by_a_clinician_slug():
    # Defence in depth: even if a quote-bearing entry reached render_index, the JSON is
    # HTML-attribute escaped (quote=True), so it cannot break out of data-clinicians.
    html = render_index(_TOKEN, ['bad"quote'])
    assert 'bad"quote' not in html                          # the raw quote never appears
    assert "&quot;" in html
