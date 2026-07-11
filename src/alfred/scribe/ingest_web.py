"""Sovereign loopback PWA ingest server for the STAY-C scribe (#49 Slice A).

A minimal ``aiohttp.web`` server the scribe daemon owns when
``scribe.ingest_web.enabled``. It is a THIN, WRITE-ONLY ingest face: it writes
``chunk_<seq>.<ext>`` audio + ``chunk_<seq>.meta.json`` sidecars into a
per-encounter subdir under ``scribe.input_dir``, and the EXISTING
sweep→accumulate→guard_ingest→local-STT→checkpoint pipeline consumes them. The
server writes NOTHING to the transcript ledger or ``ScribeState`` (the pipeline
owns both). It reads NO transcript/draft/clinical body back out.

SOVEREIGN POSTURE (the browser + the config sub-tree are the #49 attack surface
the daemon httpx/aiohttp guard cannot reach — the guard covers the CLIENT seam;
this is a SERVER):

  * LOOPBACK-ONLY — barrier (e) in the sovereign boundary POSITIVELY asserts the
    bind host is loopback at config-load (a 0.0.0.0 bind fails at the BARRIER →
    exit 79, before any socket binds). This server trusts that gate and binds to
    ``config.ingest_web.host``.
  * R3 DNS-rebind hardening on EVERY route (via one middleware): (1) the ``Host``
    header is pinned to a loopback authority — a rebind request carries the
    attacker DOMAIN as Host and is REJECTED; (2) NO CORS headers are ever emitted
    (defensively stripped); (3) a bearer token (``secrets.compare_digest``,
    constant-time). A per-request loopback peername assert too — but it is
    INSUFFICIENT ALONE (a rebound request runs on-box, so peername IS loopback),
    which is why the Host-pin is the real rebind guard. X-Forwarded-For /
    X-Real-IP are NEVER trusted.
  * R2 WRITE-ONLY egress control — NO route returns a transcript / draft /
    clinical_note / segment. ``GET /scribe/status`` returns ONLY non-PHI (opaque
    encounter id, chunk count, max seq, closed bool, a fixed state string).
  * R6 — the encounter label MUST be a machine-generated token
    (:data:`ENCOUNTER_LABEL_RE`); a client label that is not token-shaped (a
    patient name / DOB / MRN) is REJECTED. The strict charset also blocks path
    traversal and guarantees the load-bearing per-encounter uniqueness.
  * PHI-safe errors — every 4xx/5xx body is an OPAQUE reason code only; never the
    raw label, audio, transcript, or a filesystem path. Logs carry the opaque
    ``encounter_id`` + counts only (NOTE-4).

The synthetic mode-gate runs at the route (``guard_ingest`` — refuse BEFORE disk)
AND is re-run on the persisted sidecar by the accumulator downstream — BOTH gates
authoritative (keep both).
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable

import structlog
from aiohttp import web

from alfred.scribe.config import ScribeConfig
from alfred.scribe.identity import EncounterIdentityError, compute_encounter_id
from alfred.scribe.ingest import ScribeIngestRefused, guard_ingest
from alfred.scribe.pwa_assets import APP_JS, CSP_VALUE, render_index

log = structlog.get_logger(__name__)

# R6 — the encounter label MUST be a machine token: ``enc-<13-digit epoch-ms>-<16
# hex nonce>``. A patient identifier (name / DOB / MRN) cannot match; the strict
# charset also blocks path traversal (no '.', '/', no leading dot per
# pipeline.py:723) and guarantees the load-bearing per-encounter uniqueness
# (epoch-ms + nonce). Slice B (the PWA) MINTS this shape; Slice A REJECTS anything
# else.
#
# ALWAYS test with ``fullmatch`` (never ``match``) — Python's ``$`` matches BEFORE
# a trailing ``\n``, so ``re.match`` would ACCEPT ``enc-…-…\n`` (a distinct dir
# name that splits an encounter). ``fullmatch`` requires the WHOLE string, so a
# trailing/embedded newline is refused (WARN-1 fix). The ``^``/``$`` anchors are
# kept for readability but ``fullmatch`` is the operative guarantee.
ENCOUNTER_LABEL_RE = re.compile(r"^enc-[0-9]{13}-[0-9a-f]{16}$")

# MUST equal pipeline._AUDIO_EXTENSIONS (the sweep only discovers these) — pinned
# in tests. ``.mp4`` (Safari) is deliberately ABSENT (frozen contract #2):
# Chrome/Firefox webm/opus is the path.
ALLOWED_AUDIO_EXTS: frozenset[str] = frozenset({"wav", "ogg", "mp3", "m4a", "flac", "webm"})

# Chunk stem shape — mirrors pipeline._CHUNK_NAME_RE (the sweep parses seq from
# the FILENAME; the route owns contiguity from the same source).
_CHUNK_STEM_RE = re.compile(r"^chunk_(\d+)$")

_CLOSED_SENTINEL = "_CLOSED"
_META_SUFFIX = ".meta.json"

INGEST_CHUNK_ROUTE = "/scribe/ingest-chunk"
CLOSE_ROUTE = "/scribe/close"
STATUS_ROUTE = "/scribe/status"

# Slice B — the loopback PWA static surface (#49). The page GET is a browser
# NAVIGATION that cannot carry a bearer, so these two routes are Host-pinned +
# loopback-asserted but bearer-EXEMPT (the page DELIVERS the token to its JS —
# see pwa_assets + the auth-split rationale). The 3 API routes above stay
# bearer-required and byte-identical.
PAGE_ROUTE = "/"
APP_JS_ROUTE = "/scribe/app.js"
_BEARER_EXEMPT_PATHS: frozenset[str] = frozenset({PAGE_ROUTE, APP_JS_ROUTE})

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


# --- PHI-safe helpers -------------------------------------------------------

def _reject(reason: str, status: int) -> web.Response:
    """A PHI-safe error: an OPAQUE reason code ONLY — never the raw label, audio,
    transcript, or a filesystem path. Fixed ``{"error": <code>}`` body."""
    return web.json_response({"error": reason}, status=status)


def _is_true(v: Any) -> bool:
    return isinstance(v, str) and v.strip().lower() in ("true", "1", "yes", "on")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (temp with a NON-audio ``.tmp``
    suffix → ``os.replace``) so the sweep never sees a partial file. ``.tmp`` is
    not in the audio-ext set, so ``_discover_chunks`` skips the temp (contract #4)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomic text write (temp → ``os.replace``). Used for the sidecar (written
    LAST, contract #7 — the settle gate checks ``is_file()``, so a half-written
    sidecar must never appear) and the ``_CLOSED`` sentinel."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _existing_seqs(enc_dir: Path) -> list[int]:
    """Sorted seq ints of the settled ``chunk_<seq>.<ext>`` files on disk — the
    route's monotonic-contiguity source (the SAME filename view the sweep parses)."""
    if not enc_dir.is_dir():
        return []
    seqs: list[int] = []
    for p in enc_dir.iterdir():
        if p.is_file() and p.suffix.lower().lstrip(".") in ALLOWED_AUDIO_EXTS:
            m = _CHUNK_STEM_RE.match(p.stem)
            if m:
                seqs.append(int(m.group(1)))
    return sorted(seqs)


def _encounter_bytes(enc_dir: Path) -> int:
    """Total bytes of the encounter's chunk audio on disk (the per-encounter
    byte-cap accounting)."""
    if not enc_dir.is_dir():
        return 0
    total = 0
    for p in enc_dir.iterdir():
        if p.is_file() and p.suffix.lower().lstrip(".") in ALLOWED_AUDIO_EXTS:
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


# --- R3 security middleware -------------------------------------------------

def _bearer(request: web.Request) -> str:
    auth = request.headers.get("Authorization") or ""
    prefix = "Bearer "
    return auth[len(prefix):].strip() if auth.startswith(prefix) else ""


def _peername_is_loopback(request: web.Request) -> bool:
    transport = request.transport
    if transport is None:
        return False
    peer = transport.get_extra_info("peername")
    if not peer:
        return False
    try:
        return ipaddress.ip_address(peer[0]).is_loopback
    except (ValueError, IndexError):
        return False


def _build_security_middleware(config: ScribeConfig):
    """One middleware, EVERY route (R3), with a SPLIT auth policy (Slice B):

      * ALWAYS (every route incl. the static page) — Host-pin (the rebind guard)
        + loopback peername + never-emit-CORS.
      * BEARER — required on the 3 API routes; EXEMPT on the static page + app.js
        (:data:`_BEARER_EXEMPT_PATHS`). A browser NAVIGATION GET can't carry a
        bearer; the page is Host-pinned + loopback-only and is itself the token
        DELIVERY surface. This is rebind-safe: a DNS-rebind request carries the
        attacker domain as ``Host`` → refused here (421) before the page (and its
        token) is served; a cross-origin fetch to 127.0.0.1 gets an opaque
        response (no CORS) so attacker JS can't read the token-bearing HTML.
    """
    web_cfg = config.ingest_web
    port = web_cfg.port
    token = web_cfg.token
    # The pinned loopback authorities — the configured host + the loopback
    # literals, at the bound port. A DNS-rebind request carries the attacker
    # DOMAIN as its Host authority → not in this set → rejected.
    allowed_hosts = {
        f"{web_cfg.host}:{port}",
        f"127.0.0.1:{port}",
        f"localhost:{port}",
        f"[::1]:{port}",
    }

    @web.middleware
    async def _security(request: web.Request, handler: _Handler) -> web.StreamResponse:
        # (R3.1) Host-pin — the rebind guard, on EVERY route incl. the static
        # page. Reject any Host that is not a pinned loopback authority (an
        # attacker domain, a bare IP, a wrong port). MUST run for the page so a
        # rebind can't fetch the token-bearing HTML.
        if (request.headers.get("Host") or "").strip() not in allowed_hosts:
            log.warning("scribe.ingest_web.rejected", route=request.path, reason="wrong_host")
            return _reject("wrong_host", 421)
        # per-request loopback peername (defense-in-depth; INSUFFICIENT alone) —
        # on every route.
        if not _peername_is_loopback(request):
            log.warning("scribe.ingest_web.rejected", route=request.path, reason="non_loopback_peer")
            return _reject("forbidden", 403)
        # (R3.3) bearer token — constant-time compare, fail-closed on an empty
        # configured or provided token. REQUIRED on the API routes; EXEMPT on the
        # static page + app.js (a navigation GET carries no bearer).
        if request.path not in _BEARER_EXEMPT_PATHS:
            provided = _bearer(request)
            if not (token and provided and secrets.compare_digest(provided, token)):
                log.warning("scribe.ingest_web.rejected", route=request.path, reason="bad_token")
                return _reject("unauthorized", 401)
        resp = await handler(request)
        # (R3.2) NEVER emit CORS. Defensively strip any a handler/library added.
        for h in (
            "Access-Control-Allow-Origin", "Access-Control-Allow-Credentials",
            "Access-Control-Allow-Headers", "Access-Control-Allow-Methods",
            "Access-Control-Expose-Headers", "Access-Control-Max-Age",
        ):
            if h in resp.headers:
                del resp.headers[h]
        return resp

    return _security


# --- route handlers ---------------------------------------------------------

async def _handle_ingest_chunk(request: web.Request) -> web.StreamResponse:
    """``POST /scribe/ingest-chunk`` — write one settled chunk + sidecar.

    Query: ``label`` (token-shape), ``seq`` (int ≥ 1, server-validated
    monotonic/gap-free), ``ext`` (allowed audio ext), ``synthetic`` (bool),
    optional ``close`` (write ``_CLOSED`` after this chunk). Body: raw audio
    bytes (bounded by ``client_max_size``). Returns NON-PHI ``{encounter_id, seq}``.
    """
    config: ScribeConfig = request.app["scribe_config"]
    web_cfg = config.ingest_web
    q = request.query

    # R6 — label token-shape (rejects PHI labels + path traversal).
    label = q.get("label", "")
    if not ENCOUNTER_LABEL_RE.fullmatch(label):
        log.warning("scribe.ingest_web.rejected", route=INGEST_CHUNK_ROUTE, reason="invalid_label")
        return _reject("invalid_label", 400)

    # seq — server-parsed int ≥ 1.
    try:
        seq = int(q.get("seq", ""))
    except (TypeError, ValueError):
        return _reject("invalid_seq", 400)
    if seq < 1:
        return _reject("invalid_seq", 400)

    # ext — an accepted audio ext (.mp4 deliberately absent).
    ext = (q.get("ext", "") or "").lower().lstrip(".")
    if ext not in ALLOWED_AUDIO_EXTS:
        return _reject("unsupported_ext", 400)

    # opaque encounter id (fail-loud on a missing salt → opaque 500, never leak).
    try:
        encounter_id = compute_encounter_id(label, salt=config.encounter_salt)
    except EncounterIdentityError:
        log.error("scribe.ingest_web.identity_unavailable", route=INGEST_CHUNK_ROUTE)
        return _reject("identity_unavailable", 500)

    # SYNTHETIC GATE AT THE ROUTE — refuse BEFORE disk. REUSE guard_ingest (the
    # SAME gate the accumulator re-runs on the persisted sidecar downstream —
    # BOTH gates authoritative). ``synthetic`` is a strict literal-bool provenance.
    synthetic = _is_true(q.get("synthetic"))
    try:
        guard_ingest(config, provenance={"synthetic": synthetic}, source_id=encounter_id)
    except ScribeIngestRefused:
        return _reject("synthetic_required", 403)  # guard_ingest already logged the decision

    enc_dir = Path(config.input_dir) / label

    # server-validated MONOTONIC / GAP-FREE seq (contract #3) — the next contiguous
    # value from the on-disk filenames (seq is authoritative from the FILENAME).
    existing = _existing_seqs(enc_dir)
    expected = (existing[-1] + 1) if existing else 1
    if seq != expected:
        log.warning(
            "scribe.ingest_web.rejected", route=INGEST_CHUNK_ROUTE,
            reason="seq_out_of_order", encounter_id=encounter_id,
        )
        return _reject("seq_out_of_order", 409)

    # N3 cap — chunk count (explicit signal, never a silent drop).
    if len(existing) >= web_cfg.max_chunks_per_encounter:
        log.warning("scribe.ingest_web.cap_hit", encounter_id=encounter_id, cap="chunks")
        return _reject("chunk_cap", 413)

    # read the body (bounded by client_max_size → HTTPRequestEntityTooLarge).
    try:
        body = await request.read()
    except web.HTTPRequestEntityTooLarge:
        log.warning("scribe.ingest_web.cap_hit", encounter_id=encounter_id, cap="chunk_bytes")
        return _reject("chunk_too_large", 413)
    if not body:
        return _reject("empty_chunk", 400)

    # N3 cap — per-encounter total bytes.
    if _encounter_bytes(enc_dir) + len(body) > web_cfg.max_encounter_bytes:
        log.warning("scribe.ingest_web.cap_hit", encounter_id=encounter_id, cap="encounter_bytes")
        return _reject("encounter_cap", 413)

    # WRITE — audio atomically FIRST, sidecar atomically LAST (contract #4/#7). The
    # sweep acts only once the sidecar (the settle commit-marker) lands, so a
    # partial audio is never folded.
    enc_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_bytes(enc_dir / f"chunk_{seq}.{ext}", body)
    _atomic_write_text(
        enc_dir / f"chunk_{seq}{_META_SUFFIX}",
        json.dumps({"synthetic": synthetic, "seq": seq}),
    )

    closed = _is_true(q.get("close"))
    if closed:  # B3 — close-flag on the final chunk.
        _atomic_write_text(enc_dir / _CLOSED_SENTINEL, "")

    log.info(
        "scribe.ingest_web.chunk_written",
        encounter_id=encounter_id, seq=seq, bytes=len(body), closed=closed,
    )
    return web.json_response({"encounter_id": encounter_id, "seq": seq}, status=200)


async def _handle_close(request: web.Request) -> web.StreamResponse:
    """``POST /scribe/close`` — write the ``_CLOSED`` sentinel so the encounter
    finalizes to ``ready`` (B3). Query: ``label``."""
    config: ScribeConfig = request.app["scribe_config"]
    label = request.query.get("label", "")
    if not ENCOUNTER_LABEL_RE.fullmatch(label):
        return _reject("invalid_label", 400)
    try:
        encounter_id = compute_encounter_id(label, salt=config.encounter_salt)
    except EncounterIdentityError:
        return _reject("identity_unavailable", 500)
    enc_dir = Path(config.input_dir) / label
    if not enc_dir.is_dir():
        return _reject("unknown_encounter", 404)
    _atomic_write_text(enc_dir / _CLOSED_SENTINEL, "")
    log.info("scribe.ingest_web.closed", encounter_id=encounter_id)
    return web.json_response({"encounter_id": encounter_id, "closed": True}, status=200)


async def _handle_status(request: web.Request) -> web.StreamResponse:
    """``GET /scribe/status`` — NON-PHI status ONLY (R2/N4). Query: ``label``.

    Returns the opaque encounter id, chunk count, max seq, closed bool, and a
    fixed state string. NEVER a transcript / draft / segment / clinical body."""
    config: ScribeConfig = request.app["scribe_config"]
    label = request.query.get("label", "")
    if not ENCOUNTER_LABEL_RE.fullmatch(label):
        return _reject("invalid_label", 400)
    try:
        encounter_id = compute_encounter_id(label, salt=config.encounter_salt)
    except EncounterIdentityError:
        return _reject("identity_unavailable", 500)
    enc_dir = Path(config.input_dir) / label
    existing = _existing_seqs(enc_dir)
    closed = (enc_dir / _CLOSED_SENTINEL).exists()
    state = "closed" if closed else ("recording" if existing else "pending")
    return web.json_response(
        {
            "encounter_id": encounter_id,
            "chunks": len(existing),
            "max_seq": existing[-1] if existing else 0,
            "closed": closed,
            "state": state,
        },
        status=200,
    )


# --- Slice B static PWA surface (page + app.js) -----------------------------

def _static_headers() -> dict[str, str]:
    """Headers for the static PWA responses. Strict CSP (R4 — ``connect-src
    'self'`` makes the browser itself refuse any off-box fetch), plus no-store so
    the token-bearing page / JS is never cached (belt for R5)."""
    return {
        "Content-Security-Policy": CSP_VALUE,
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }


async def _handle_page(request: web.Request) -> web.StreamResponse:
    """``GET /`` — the inlined loopback PWA (bearer-EXEMPT; Host-pinned + loopback
    by the middleware). Embeds the ingest token for the same-origin JS. Strict
    CSP + no-store. R4: zero external resources."""
    config: ScribeConfig = request.app["scribe_config"]
    body = render_index(config.ingest_web.token)
    return web.Response(text=body, content_type="text/html", charset="utf-8",
                        headers=_static_headers())


async def _handle_app_js(request: web.Request) -> web.StreamResponse:
    """``GET /scribe/app.js`` — the same-origin PWA logic (bearer-EXEMPT;
    Host-pinned + loopback). No token here (the token lives in the page's data
    attribute); this is just code. CSP-served (script-src 'self')."""
    return web.Response(text=APP_JS, content_type="application/javascript",
                        charset="utf-8", headers=_static_headers())


# --- app + server lifecycle -------------------------------------------------

def create_ingest_app(config: ScribeConfig) -> web.Application:
    """Build the ingest ``web.Application`` — the 3 bearer-required API routes +
    the 2 bearer-exempt static PWA routes (Slice B), the split-policy security
    middleware, and ``client_max_size`` pinned to the per-chunk byte cap (N3).

    Only instantiated when ``ingest_web.enabled`` (the daemon starts the server
    solely then), so the static surface is INERT by default — no server, no
    page."""
    app = web.Application(
        client_max_size=config.ingest_web.max_chunk_bytes,
        middlewares=[_build_security_middleware(config)],
    )
    app["scribe_config"] = config
    # 3 API routes — bearer-required, byte-identical to Slice A.
    app.router.add_post(INGEST_CHUNK_ROUTE, _handle_ingest_chunk)
    app.router.add_post(CLOSE_ROUTE, _handle_close)
    app.router.add_get(STATUS_ROUTE, _handle_status)
    # 2 static PWA routes — bearer-exempt (Host-pinned + loopback), Slice B.
    app.router.add_get(PAGE_ROUTE, _handle_page)
    app.router.add_get(APP_JS_ROUTE, _handle_app_js)
    return app


class IngestWebServer:
    """Owns the ``aiohttp`` ``AppRunner`` lifecycle, bound to the loopback host.

    Started by the scribe daemon (on the daemon's own event loop) when
    ``ingest_web.enabled``; stopped in the daemon's shutdown ``finally``."""

    def __init__(self, config: ScribeConfig) -> None:
        self._config = config
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        web_cfg = self._config.ingest_web
        app = create_ingest_app(self._config)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, web_cfg.host, web_cfg.port)
        await site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
