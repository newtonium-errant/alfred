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

import asyncio
import ipaddress
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import structlog

if TYPE_CHECKING:  # scribe→scribe.events (design §2.3); the facade rides app["scribe_events"]
    from alfred.scribe.events import ScribeEvents
from aiohttp import web

from alfred.scribe.close_manifest import (
    CLOSE_SENTINEL_NAME,
    read_close_manifest,
    resolve_require_close_manifest,
    write_close_manifest,
)
from alfred.scribe.config import ScribeConfig
from alfred.scribe.identity import EncounterIdentityError, compute_encounter_id
from alfred.scribe.ingest import ScribeIngestRefused, guard_ingest
from alfred.scribe.pwa_assets import (
    APP_JS,
    APPLE_TOUCH_ICON_PNG,
    APPLE_TOUCH_ICON_PRECOMPOSED_ROUTE,
    APPLE_TOUCH_ICON_ROUTE,
    CSP_VALUE,
    FAVICON_PNG,
    FAVICON_ROUTE,
    ICON_192_PNG,
    ICON_192_ROUTE,
    ICON_512_PNG,
    ICON_512_ROUTE,
    MANIFEST_JSON,
    MANIFEST_ROUTE,
    render_index,
)

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

_META_SUFFIX = ".meta.json"

INGEST_CHUNK_ROUTE = "/scribe/ingest-chunk"
CLOSE_ROUTE = "/scribe/close"
STATUS_ROUTE = "/scribe/status"
# Task #4 — box-local bug capture. INGEST-token gated (the page already holds it), NOT
# exempt, NOT enroll-class: it falls through the middleware to the ingest-class branch, so no
# auth wiring beyond registration. Writes PHI-cautious 0600 files via ``scribe.bug``; NO
# egress (the box watcher surfaces them). Handler-gated on ``bug.enabled`` (404 when off).
BUG_ROUTE = "/scribe/bug"

# --- #12 slice 12b — per-clinician PWA session identity (design §2) -----------
# Server-issued identity binding: the ingest token AUTHENTICATES the page; the session
# IDENTIFIES which config.clinicians slug is acting (it supplies consent ``captured_by`` in
# slice 12c). Both routes are INGEST-class — they ride the ingest token the page already holds
# (design §2.3), add NO new credential, and fall through ``_authorize_route``'s default ingest
# branch. IDENTITY ≠ AUTHENTICATION (design §2.1): on this single-trust loopback surface the
# session answers "who," not "may they" — so the asserted-identity peer-pin escalation concern
# (two token classes sharing a client name) does not apply: there is ONE ingest-token class and
# the session merely records honest, server-resolved attribution, never a privilege.
SESSION_OPEN_ROUTE = "/scribe/session/open"
SESSION_CLOSE_ROUTE = "/scribe/session/close"
# Identity is delivered on subsequent calls via a REQUEST HEADER (not a query param) — it keeps
# the opaque token out of URLs and access logs, the same reasoning as the Authorization bearer.
SESSION_HEADER = "X-Scribe-Session"

# --- #12 slice 12c — consent route + state machine wiring (design §4/§5/§6) ---
# The single consent-transition route: confirmed/declined gate the START of capture; withdrawn
# HALTS it (the durable append MUST land before the stop is acknowledged, §5). Ingest-class
# (rides the ingest token). Identity resolves by decision: confirm/decline name the ACTING
# clinician (the live X-Scribe-Session, §2.5); withdrawal's actor is ALWAYS the durable
# consent.confirmed captured_by — the clinician who OBTAINED consent — NEVER the live session
# (§2.4, the governing rule: the withdrawal is attributed to whoever obtained the consent).
CONSENT_ROUTE = "/scribe/consent"
_CONSENT_DECISIONS: frozenset[str] = frozenset({"confirmed", "declined", "withdrawn"})

# Slice B — the loopback PWA static surface (#49). The page GET is a browser
# NAVIGATION that cannot carry a bearer, so these two routes are Host-pinned +
# loopback-asserted but bearer-EXEMPT (the page DELIVERS the token to its JS —
# see pwa_assets + the auth-split rationale). The 3 API routes above stay
# bearer-required and byte-identical.
PAGE_ROUTE = "/"
APP_JS_ROUTE = "/scribe/app.js"

# Task #1 standalone-install surface — the manifest, its two icons, the favicon (kills the
# /favicon.ico 401 log spam, Task #3), and the two canonical apple-touch-icon paths (the iOS
# home-screen tile + the same 401-spam class for the operator-ruled iPhone). All are
# browser-issued fetches that carry no bearer AND are STATIC + SECRET-FREE (no token, unlike
# the page), so they join the bearer-EXEMPT set. They stay under the SAME every-route
# middleware: Host-pin (the rebind guard) + loopback peername + the Sec-Fetch-Site belt —
# nothing bypasses. Each fetch is same-origin (Sec-Fetch-Site: same-origin/none) → passes.
_INSTALL_ASSET_PATHS: frozenset[str] = frozenset({
    MANIFEST_ROUTE, ICON_192_ROUTE, ICON_512_ROUTE, FAVICON_ROUTE,
    APPLE_TOUCH_ICON_ROUTE, APPLE_TOUCH_ICON_PRECOMPOSED_ROUTE,
})
_BEARER_EXEMPT_PATHS: frozenset[str] = frozenset(
    {PAGE_ROUTE, APP_JS_ROUTE} | _INSTALL_ASSET_PATHS
)

# Conventional-asset probe prefix. WebKit fetches SIZED apple-touch variants
# (/apple-touch-icon-120x120.png, -152x152-precomposed.png, …) — an open-ended family we do
# NOT route (the declared <link rel="apple-touch-icon"> suppresses the probe on modern iOS,
# but a bare/edge probe still arrives). Such a path is NOT one of the two canonical exempt
# apple-touch paths, so it falls to the bearer branch and — no-auth — would log a
# warning-level ``bad_token`` 401 (the favicon-spam class, one probe-family later). A
# no-Authorization GET of this prefix is a browser fetch for an asset that does not exist:
# answer a QUIET 404 (no warning). The two canonical paths are exempt + served 200 above, so
# they never reach this test.
APPLE_TOUCH_ICON_PROBE_PREFIX = "/apple-touch-icon-"

# --- P4-5a — enrollment rides THIS server; TWO-TOKEN capability split ---------
# The enroll routes require the ``enroll_token`` (biometric-custody capability),
# DISTINCT from the ingest ``token`` (encounter capability): the `web`/`web_ingest`
# peer-pin lesson applied to this standalone server, so page possession (which
# carries the ingest token) can never grant biometric mutation. Handlers live in
# ``enroll_web``; ``create_ingest_app`` registers them. Classification HERE is the
# security gate. A token valid for the OTHER class → ``wrong_token_class`` 401.
ENROLL_TOKEN_ROUTES: frozenset[str] = frozenset({
    "/scribe/enroll/start", "/scribe/enroll/chunk", "/scribe/enroll/finalize",
    "/scribe/enroll/result", "/scribe/enroll/abandon",
    "/scribe/presets/rename", "/scribe/presets/delete",
})
PRESETS_LIST_ROUTE = "/scribe/presets"                   # auth: EITHER token (metadata only)
ENCOUNTER_PRESET_ROUTE = "/scribe/encounter/preset"      # auth: ingest token (encounter-class)
# Every enroll-face path (for the inert-when-tokenless 404 gate).
_ALL_ENROLL_PATHS: frozenset[str] = ENROLL_TOKEN_ROUTES | {PRESETS_LIST_ROUTE}

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


def _authorize_route(
    path: str, provided: str, *, ingest_tok: str, enroll_tok: str,
) -> tuple[bool, str]:
    """Two-token authorization for a non-exempt API route → ``(ok, reason)``.

    A token valid for the OTHER capability class → ``wrong_token_class`` (the
    ``*_wrong_peer`` analog — a real privilege boundary, not a typo); a WRONG token
    → ``bad_token``; NO token at all → ``no_token`` (the split lets the log tell a
    credential probe from an unauthenticated browser fetch — the two are very
    different signals). Constant-time compares; fail-closed on an empty
    configured/provided token."""
    def _match(tok: str) -> bool:
        return bool(tok and provided and secrets.compare_digest(provided, tok))
    # NO Authorization vs a WRONG one — a benign no-auth browser fetch should not read
    # as the same event as someone presenting an invalid credential.
    miss = "bad_token" if provided else "no_token"
    is_ingest, is_enroll = _match(ingest_tok), _match(enroll_tok)
    if path in ENROLL_TOKEN_ROUTES:
        if is_enroll:
            return True, ""
        return False, "wrong_token_class" if is_ingest else miss
    if path == PRESETS_LIST_ROUTE:                       # either token clears metadata
        return (True, "") if (is_enroll or is_ingest) else (False, miss)
    if is_ingest:                                        # ingest-class (chunk/close/status/encounter-preset)
        return True, ""
    return False, "wrong_token_class" if is_enroll else miss


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


# --- #12 slice 12b — PWA identity session: RAM table, mint, sliding-TTL sweep, resolver ---
# The RAM-only session table (design §2.2): {session_token: PwaSession}. NO persistence — a
# server restart drops every session (the durable ``consent.confirmed`` event is the real
# encounter→clinician binding, design §2.4; a lapsed session just re-opens on the next call).
# Mirrors the enroll table's on-access-sweep pattern (``enroll_web._sweep_expired``): there is
# NO reaper thread — the sweep runs at the top of every open / close / resolve.
_MAX_PWA_SESSIONS = 64   # RAM bound — a client that opens without ever closing cannot grow the
                         # table without limit (a self-DoS on the sovereign loopback surface,
                         # not an attack; the idle TTL reclaims leaked sessions regardless).


@dataclass
class PwaSession:
    """One server-issued identity binding: an opaque token → a ``config.clinicians`` slug.
    RAM-only (never persisted on-device or on-disk, design §10). ``opened_at`` anchors the
    absolute cap; ``last_seen`` (refreshed on every session-authenticated request) anchors the
    sliding idle TTL. Holds NO PHI — only a STAFF slug, the consent ``captured_by`` / event
    ``actor`` (design §2.5)."""

    clinician: str
    opened_at: float          # time.monotonic() at open — the absolute-cap anchor
    last_seen: float          # time.monotonic() of the last access — the idle-TTL anchor


_PWA_SESSIONS: dict[str, PwaSession] = {}


def mint_pwa_session_id() -> str:
    """A fresh opaque ``ses-<13-digit-ms>-<16hex>`` session token (crypto-random, unguessable
    via ``secrets.token_hex``). Mirrors ``enrollment.mint_session_id``'s construction with a
    DISTINCT ``ses-`` prefix so an identity-session token can never be confused with an
    enroll-session id (design §2.2)."""
    return f"ses-{int(time.time() * 1000):013d}-{secrets.token_hex(8)}"


def _sweep_pwa_sessions(now: float, *, idle_ttl: float, abs_ttl: float) -> None:
    """Drop every session past its sliding idle TTL (``now - last_seen > idle_ttl``) OR its
    absolute cap (``now - opened_at > abs_ttl``), whichever first. On-access sweep — the same
    no-reaper pattern the enroll table uses. Emits a single count line when it reclaims (ILB —
    a silent reclaim would be indistinguishable from a table that never grows)."""
    expired = [
        tok for tok, s in _PWA_SESSIONS.items()
        if (now - s.last_seen) > idle_ttl or (now - s.opened_at) > abs_ttl
    ]
    for tok in expired:
        _PWA_SESSIONS.pop(tok, None)
    if expired:
        log.info("scribe.session.swept", count=len(expired),
                 detail="PWA identity session(s) TTL-swept (idle or absolute cap) — RAM reclaimed")


def _resolve_session_token(token: str, *, idle_ttl: float, abs_ttl: float,
                           now: float | None = None) -> PwaSession | None:
    """Pure resolver over the RAM table: SWEEP expired first (so an expired token can never
    resolve), then look up ``token`` and refresh its ``last_seen`` (the sliding-TTL touch).
    Returns the bound :class:`PwaSession`, or ``None`` (header absent / unknown / already
    expired). ``now`` is injectable for deterministic TTL tests."""
    now = time.monotonic() if now is None else now
    _sweep_pwa_sessions(now, idle_ttl=idle_ttl, abs_ttl=abs_ttl)
    s = _PWA_SESSIONS.get((token or "").strip())
    if s is None:
        return None
    s.last_seen = now
    return s


def _session_ttls(config: ScribeConfig) -> tuple[float, float]:
    """The (idle, absolute) session TTLs in seconds from config."""
    return (float(config.ingest_web.session_idle_ttl_s),
            float(config.ingest_web.session_absolute_ttl_s))


def _resolve_session(request: web.Request) -> PwaSession | None:
    """Resolve the ``X-Scribe-Session`` header → the bound session (design §2.3), refreshing
    the sliding TTL and sweeping on access. ``None`` when absent / unknown / expired — slice
    12c's consent route turns that into a 401 ``no_session``; slice 12b only rides it to keep
    a live encounter's session warm."""
    idle_ttl, abs_ttl = _session_ttls(request.app["scribe_config"])
    return _resolve_session_token(request.headers.get(SESSION_HEADER) or "",
                                  idle_ttl=idle_ttl, abs_ttl=abs_ttl)


# --- #12 slice 12c — consent hot cache + per-encounter lock (design §5.3/§6.2) ---
# The chunk route and the consent route run in the SAME single-threaded aiohttp event loop, so a
# per-encounter ``asyncio.Lock`` makes their critical sections mutually exclusive: the chunk's
# (consent-check → write) and the withdrawal's (durable append → cache flip) can never interleave
# for one encounter. Therefore no un-consented chunk can land after the durable ``withdrawn``
# commits (§5.3 — the race is closed). Both structures live on the APP (``app["enc_locks"]`` /
# ``app["consent_cache"]``), NOT module RAM: an ``asyncio.Lock`` is bound to the loop it was
# created on, and per-app scoping (a) keeps every lock on its own server's loop and (b) gives the
# exact §5.4 restart-empty semantics for free (a fresh server = a fresh, empty cache). The durable
# event store is the source of truth; the cache is a disposable hot layer (a MISS falls back to
# ``events.consent_state`` — the server-restart correctness backstop, §5.4).


def _enc_lock(locks: dict, enc_id: str) -> "asyncio.Lock":
    """The per-encounter lock from the app's ``enc_locks`` dict (created on demand — the standard
    aiohttp per-key-lock idiom). Safe to create lazily in the single-threaded loop: no two
    coroutines run the get-or-create concurrently (no await between the ``.get`` and the assign)."""
    lock = locks.get(enc_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[enc_id] = lock
    return lock


def _consent_state_cached(cache: dict, enc_id: str, events: "ScribeEvents | None") -> str:
    """Hot-path consent state: the app's RAM cache → fallback to the DURABLE
    ``events.consent_state`` on a miss (repopulating the cache). The cache is a performance layer,
    NEVER the source of truth: a server restart drops it and the miss path reads the true state
    back from the durable store (§5.4/§6.2). ``''`` (no consent) when the store is inactive/absent."""
    st = cache.get(enc_id)
    if st is not None:
        return st
    st = events.consent_state(enc_id) if events is not None and events.active else ""
    if st:
        cache[enc_id] = st
    return st


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
    enroll_tok = web_cfg.enroll_token   # P4-5a biometric-custody capability (may be "")
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
        if request.path in _BEARER_EXEMPT_PATHS:
            # NOTE-1 belt — the static page carries the ingest token, so refuse to
            # even SERVE it to a CROSS-ORIGIN fetch (SOP-blocks-READ stays the real
            # guarantee; this is defense-in-depth). ``Sec-Fetch-Site`` is ``none``
            # for a direct operator navigation and ``same-origin`` for the app.js
            # subresource; ``cross-site`` / ``same-site`` is a cross-origin fetch →
            # refused. FAIL-OPEN when the header is ABSENT (older browsers /
            # non-browser clients omit it — must not break the real page load).
            sfs = request.headers.get("Sec-Fetch-Site")
            if sfs is not None and sfs not in ("same-origin", "none"):
                log.warning("scribe.ingest_web.rejected", route=request.path, reason="cross_origin_fetch")
                return _reject("cross_origin", 421)
        else:
            # (R3.3) bearer token — TWO-TOKEN split (P4-5a). Each route class pins ITS
            # token; a token valid for the OTHER class → wrong_token_class 401.
            provided = _bearer(request)
            path = request.path
            # QUIET the browser's conventional-asset probes: a no-Authorization GET of a
            # sized apple-touch variant is a fetch for an asset that does not exist — a plain
            # 404, not a warning-level bad_token 401. Gated on GET + no-auth so it can never
            # mask a real credentialed request to any route.
            if (request.method == "GET" and not provided
                    and path.startswith(APPLE_TOUCH_ICON_PROBE_PREFIX)):
                return _reject("not_found", 404)
            # INERT enrollment face: enroll_token unset ⇒ the enroll-face routes are
            # 404 (the biometric face is ABSENT, not merely unauthorized). The
            # ingest-class routes stay bearer-required as before.
            if path in _ALL_ENROLL_PATHS and not enroll_tok:
                return _reject("enroll_inert", 404)
            ok, reason = _authorize_route(path, provided, ingest_tok=token, enroll_tok=enroll_tok)
            if not ok:
                log.warning("scribe.ingest_web.rejected", route=path, reason=reason)
                if reason == "wrong_token_class":
                    # A PRIVILEGE-BOUNDARY PROBE — the exact event the two-token split
                    # exists to catch, and a frozen audit.log event. It must land in the
                    # DURABLE biometric-custody trail, not only the rotating daemon
                    # structlog (after rotation a custody audit would show zero evidence).
                    # ids/enums only; fail-silent; no-ops when the store is dormant.
                    from alfred.scribe import enroll_learning
                    enroll_learning.audit(
                        config.diarize.enrollment_dir, "wrong_token_class", route=path,
                    )
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
    events: "ScribeEvents | None" = request.app.get("scribe_events")
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
        log.warning("scribe.ingest_web.rejected", route=INGEST_CHUNK_ROUTE, reason="invalid_seq")
        return _reject("invalid_seq", 400)
    if seq < 1:
        log.warning("scribe.ingest_web.rejected", route=INGEST_CHUNK_ROUTE, reason="invalid_seq")
        return _reject("invalid_seq", 400)

    # ext — an accepted audio ext (.mp4 deliberately absent).
    ext = (q.get("ext", "") or "").lower().lstrip(".")
    if ext not in ALLOWED_AUDIO_EXTS:
        log.warning("scribe.ingest_web.rejected", route=INGEST_CHUNK_ROUTE, reason="unsupported_ext")
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

    # #12 slice 12c — the CONSENT GATE + the chunk write run UNDER the per-encounter lock, so no
    # chunk can interleave with a withdrawal's (durable append → cache flip). The lock spans the
    # whole write: a chunk that passed its consent check completes BEFORE a withdrawal can flip
    # the cache, and a chunk whose check runs after the flip is refused — so no un-consented
    # chunk lands after the durable `withdrawn` commits (§5.3, the race is closed).
    async with _enc_lock(request.app["enc_locks"], encounter_id):
        # SERVER enforcement of "no consent state → the mic never opens" (§6.1): the client gate
        # (§4) is UX; THIS is the guarantee. Even a client that skipped the panel and POSTed a
        # chunk directly is refused at seq=1 because no `consent.confirmed` exists. It equally
        # enforces withdrawal (§5) and declined. Cache → durable fallback. Enforced only when
        # CLINICAL mode AND an ACTIVE event store exists: a synthetic/test encounter has no consent
        # flow (§7.2/§10: its consent state is ""), so gating it would refuse every synthetic chunk;
        # and consent can only be enforced against a store that can record it (clinical mode ALWAYS
        # has an active store — it fails LOUD at open — so this never weakens the production gate).
        if config.is_clinical and events is not None and events.active:
            state = _consent_state_cached(request.app["consent_cache"], encounter_id, events)
            if state != "confirmed":
                # best-effort violation marker (a refused chunk, never a state); opaque PHI-safe 403.
                if events is not None:
                    events.consent_violation_refused(subject_id=encounter_id, seq=seq)
                log.warning("scribe.consent.chunk_refused", route=INGEST_CHUNK_ROUTE,
                            encounter_id=encounter_id, seq=seq, state=state or "none",
                            detail="chunk refused — consent state is not 'confirmed' (mic must "
                                   "not have opened, or a withdrawal has halted the feed)")
                return _reject("consent_required", 403)

        # #57 SEAL — once `_CLOSED` exists the encounter is sealed to NEW audio. A chunk
        # after close is refused (409), eliminating the post-close-chunk path that could
        # manufacture folded seqs BEYOND the promised final_seq. (Safe for the serial
        # PWA: it never POSTs a chunk after /close — onstop is latched by `stopped` and
        # /close is chained after the last chunk link.)
        if (enc_dir / CLOSE_SENTINEL_NAME).exists():
            log.warning("scribe.ingest_web.rejected", route=INGEST_CHUNK_ROUTE,
                        reason="encounter_closed", encounter_id=encounter_id)
            if events is not None:  # encounter.post_close_chunk_refused (design §5.4)
                events.encounter_post_close_chunk_refused(subject_id=encounter_id, seq=seq)
            return _reject("encounter_closed", 409)

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
            if events is not None:  # encounter.cap_hit (design §5.4)
                events.encounter_cap_hit(subject_id=encounter_id, cap="chunks")
            return _reject("chunk_cap", 413)

        # read the body (bounded by client_max_size → HTTPRequestEntityTooLarge).
        try:
            body = await request.read()
        except web.HTTPRequestEntityTooLarge:
            log.warning("scribe.ingest_web.cap_hit", encounter_id=encounter_id, cap="chunk_bytes")
            if events is not None:  # encounter.cap_hit (design §5.4)
                events.encounter_cap_hit(subject_id=encounter_id, cap="chunk_bytes")
            return _reject("chunk_too_large", 413)
        if not body:
            log.warning("scribe.ingest_web.rejected", route=INGEST_CHUNK_ROUTE,
                        reason="empty_chunk", encounter_id=encounter_id)
            return _reject("empty_chunk", 400)

        # N3 cap — per-encounter total bytes.
        if _encounter_bytes(enc_dir) + len(body) > web_cfg.max_encounter_bytes:
            log.warning("scribe.ingest_web.cap_hit", encounter_id=encounter_id, cap="encounter_bytes")
            if events is not None:  # encounter.cap_hit (design §5.4)
                events.encounter_cap_hit(subject_id=encounter_id, cap="encounter_bytes")
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
        # encounter.opened (design §5.4) — the first accepted chunk (seq==1, after the
        # atomic chunk+meta write). seq==1 reliably means "first chunk" (the gap-free
        # seq gate above refuses any seq != expected, and expected==1 only when empty).
        if events is not None and seq == 1:
            events.encounter_opened(subject_id=encounter_id)

        closed = _is_true(q.get("close"))
        if closed:  # B3 — close-flag on the final chunk. THIS chunk IS the final, so
            # the manifest's final_seq = this seq (trivially correct — gives the
            # close-flag path the #57 structural completeness gate too).
            write_close_manifest(enc_dir, seq)
            # encounter.closed (design §5.4) — the close-flag seal path. A close-flag
            # seal with no event would leave a capture-but-no-`closed` timeline (an
            # intentionally-left-blank violation on the CMPA demo query).
            if events is not None:
                events.encounter_closed(subject_id=encounter_id, final_seq=seq)

    log.info(
        "scribe.ingest_web.chunk_written",
        encounter_id=encounter_id, seq=seq, bytes=len(body), closed=closed,
    )
    return web.json_response({"encounter_id": encounter_id, "seq": seq}, status=200)


async def _handle_close(request: web.Request) -> web.StreamResponse:
    """``POST /scribe/close`` — seal the encounter (B3 + #57 close-manifest).

    Query: ``label`` + (``final_seq`` — the client's asserted final seq). Writes the
    versioned ``_CLOSED`` manifest so the checkpoint gate finalizes READY only once
    seqs ``1..final_seq`` are ALL folded (structural "ready ⇒ complete").

    STRICT (clinical / require): ``final_seq`` is REQUIRED — absent → 400, nothing
    written (the encounter stays OPEN, can never reach READY). LEGACY (synthetic):
    absent ``final_seq`` writes a manifest with the on-disk max (or a byte-identical
    empty ``_CLOSED`` for a zero-chunk close). MONOTONIC write-once + consistency
    belts guard against an adversarial 2nd close lowering the completeness bar."""
    config: ScribeConfig = request.app["scribe_config"]
    events: "ScribeEvents | None" = request.app.get("scribe_events")
    label = request.query.get("label", "")
    if not ENCOUNTER_LABEL_RE.fullmatch(label):
        log.warning("scribe.ingest_web.rejected", route=CLOSE_ROUTE, reason="invalid_label")
        return _reject("invalid_label", 400)
    try:
        encounter_id = compute_encounter_id(label, salt=config.encounter_salt)
    except EncounterIdentityError:
        return _reject("identity_unavailable", 500)
    enc_dir = Path(config.input_dir) / label
    if not enc_dir.is_dir():
        return _reject("unknown_encounter", 404)

    require = resolve_require_close_manifest(config)
    raw_fs = request.query.get("final_seq")
    sentinel = enc_dir / CLOSE_SENTINEL_NAME

    if raw_fs is None or not raw_fs.strip():
        if require:
            # (a) STRICT + no final_seq → REFUSE, write NOTHING. The encounter stays
            # OPEN (no sentinel) so it can never reach READY (constraint-5).
            log.warning("scribe.ingest_web.rejected", route=CLOSE_ROUTE,
                        reason="final_seq_required", encounter_id=encounter_id)
            return _reject("final_seq_required", 400)
        # (b) LEGACY (not strict): manifest with the on-disk max, or empty-legacy
        # _CLOSED for a zero-chunk close (byte-identical to the old behavior).
        existing = _existing_seqs(enc_dir)
        disk_max = existing[-1] if existing else 0
        if disk_max >= 1:
            write_close_manifest(enc_dir, disk_max)
        else:
            _atomic_write_text(sentinel, "")
        log.info("scribe.ingest_web.closed", encounter_id=encounter_id,
                 protocol=1, final_seq=disk_max)
        # encounter.closed (design §5.4) — the /close seal path (legacy/non-strict);
        # final_seq = the on-disk max (0 for a zero-chunk close).
        if events is not None:
            events.encounter_closed(subject_id=encounter_id, final_seq=disk_max)
        return web.json_response({"encounter_id": encounter_id, "closed": True}, status=200)

    # (c) final_seq PRESENT → validate + consistency + monotonic write-once.
    try:
        final_seq = int(raw_fs)
    except (TypeError, ValueError):
        log.warning("scribe.ingest_web.rejected", route=CLOSE_ROUTE,
                    reason="invalid_final_seq", encounter_id=encounter_id)
        return _reject("invalid_final_seq", 400)
    if final_seq < 1:
        log.warning("scribe.ingest_web.rejected", route=CLOSE_ROUTE,
                    reason="invalid_final_seq", encounter_id=encounter_id)
        return _reject("invalid_final_seq", 400)
    # consistency belt: a final_seq BELOW the on-disk max contradicts gap-free-from-1
    # (the legit tail-not-yet-landed case has final_seq > disk_max → never blocked).
    existing = _existing_seqs(enc_dir)
    if existing and final_seq < existing[-1]:
        log.warning("scribe.ingest_web.rejected", route=CLOSE_ROUTE,
                    reason="final_seq_below_disk", encounter_id=encounter_id)
        return _reject("final_seq_below_disk", 409)
    # monotonic write-once: a 2nd /close can never LOWER the completeness bar.
    existing_efs = None
    if sentinel.exists():
        # ``require=False`` DELIBERATELY (not the caller's ``require``): here we only need
        # to know whether the EXISTING sentinel is PRESENT-but-CORRUPT. With require=False,
        # ``ambiguous`` is True ONLY for corrupt content — an EMPTY legacy sentinel returns
        # False, and UPGRADING an empty legacy close to a real manifest is exactly right
        # (refusing that would wedge the encounter). ``existing_efs`` is unaffected by the
        # flag: a valid manifest yields its final_seq under either value.
        existing_efs, existing_corrupt = read_close_manifest(sentinel, require=False)
        if existing_corrupt:
            # We CANNOT read the bar the existing sentinel promised, so we cannot prove
            # this /close does not LOWER it. Previously the ambiguity flag was DISCARDED,
            # so a corrupt sentinel yielded existing_efs=None, the monotonic guard below
            # was skipped, and a 2nd /close with a LOWER final_seq silently RESET the
            # completeness bar — defeating "a 2nd /close can never LOWER the bar" on a
            # medico-legal surface. REFUSE instead; the operator must resolve it.
            log.warning("scribe.ingest_web.rejected", route=CLOSE_ROUTE,
                        reason="close_manifest_corrupt", encounter_id=encounter_id)
            return _reject("close_manifest_corrupt", 409)
    if existing_efs is not None and final_seq < existing_efs:
        log.warning("scribe.ingest_web.rejected", route=CLOSE_ROUTE,
                    reason="final_seq_lowered", encounter_id=encounter_id)
        return _reject("final_seq_lowered", 409)
    bar = max(existing_efs or 0, final_seq)
    write_close_manifest(enc_dir, bar)
    log.info("scribe.ingest_web.closed", encounter_id=encounter_id, protocol=2, final_seq=bar)
    # encounter.closed (design §5.4) — the /close seal path (final_seq present).
    if events is not None:
        events.encounter_closed(subject_id=encounter_id, final_seq=bar)
    return web.json_response({"encounter_id": encounter_id, "closed": True}, status=200)


async def _handle_status(request: web.Request) -> web.StreamResponse:
    """``GET /scribe/status`` — NON-PHI status ONLY (R2/N4). Query: ``label``.

    Returns the opaque encounter id, chunk count, max seq, closed bool, and a
    fixed state string. NEVER a transcript / draft / segment / clinical body."""
    config: ScribeConfig = request.app["scribe_config"]
    # #12 slice 12b — sliding-TTL keep-warm: the ~3 s status poll refreshes an active
    # encounter's identity session (design §2.2) so a live capture never lets its session
    # lapse mid-encounter. NO gating here — status is non-PHI and always answers; a
    # resolved-None simply means no session is bound (slice 12c's consent route is where
    # a missing session 401s, not the status probe).
    _resolve_session(request)
    label = request.query.get("label", "")
    if not ENCOUNTER_LABEL_RE.fullmatch(label):
        log.warning("scribe.ingest_web.rejected", route=STATUS_ROUTE, reason="invalid_label")
        return _reject("invalid_label", 400)
    try:
        encounter_id = compute_encounter_id(label, salt=config.encounter_salt)
    except EncounterIdentityError:
        return _reject("identity_unavailable", 500)
    enc_dir = Path(config.input_dir) / label
    existing = _existing_seqs(enc_dir)
    closed = (enc_dir / CLOSE_SENTINEL_NAME).exists()
    state = "closed" if closed else ("recording" if existing else "pending")
    # P4-5a preset_fit (unarmed|ok in 5a). Non-logging status probe; fail-safe to
    # 'unarmed' so status never breaks on an enrollment-layer hiccup.
    preset_fit = "unarmed"
    try:
        from alfred.scribe import embed_voice as _ev
        from alfred.scribe import enrollment as _en
        preset_fit = _en.preset_fit_for_status(
            enc_dir, config.diarize.enrollment_dir, _ev.engine_fingerprint(config))
    except Exception:  # noqa: BLE001 — status must never 500 on the enrollment layer
        preset_fit = "unarmed"
    return web.json_response(
        {
            "encounter_id": encounter_id,
            "chunks": len(existing),
            "max_seq": existing[-1] if existing else 0,
            "closed": closed,
            "state": state,
            "preset_fit": preset_fit,
        },
        status=200,
    )


async def _handle_bug(request: web.Request) -> web.StreamResponse:
    """``POST /scribe/bug`` — capture a box-local bug report (task #4). INGEST-token gated by
    the middleware (ingest-class). Body: JSON ``{summary, detail, context{}, events[]}``. The
    per-POST byte cap is enforced HERE (Content-Length pre-check + post-read length) before the
    open-report disk backstop in ``scribe.bug``. Returns ``{bug_id}`` (200) or an OPAQUE 4xx
    the UI renders. NO PHI in logs (the summary/detail may carry PHI despite the page caution)."""
    config: ScribeConfig = request.app["scribe_config"]
    bug_cfg = config.bug
    # INERT toggle — an operator can 404 the route without disabling the whole server.
    if not bug_cfg.enabled:
        return _reject("bug_inert", 404)
    # cheap Content-Length pre-check so an honest oversized POST is refused BEFORE buffering.
    if request.content_length is not None and request.content_length > bug_cfg.max_body_bytes:
        log.warning("scribe.ingest_web.rejected", route=BUG_ROUTE, reason="bug_too_large")
        return _reject("bug_too_large", 413)
    try:
        raw = await request.read()                       # globally bounded by client_max_size
    except web.HTTPRequestEntityTooLarge:
        return _reject("bug_too_large", 413)
    if len(raw) > bug_cfg.max_body_bytes:                # the lying-Content-Length backstop
        log.warning("scribe.ingest_web.rejected", route=BUG_ROUTE, reason="bug_too_large")
        return _reject("bug_too_large", 413)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        log.warning("scribe.ingest_web.rejected", route=BUG_ROUTE, reason="invalid_json")
        return _reject("invalid_json", 400)
    if not isinstance(payload, dict):
        return _reject("invalid_json", 400)

    summary = payload.get("summary", "")
    detail = payload.get("detail", "")
    # TYPE-VALIDATE the free-text fields → opaque 400 (R6). A truthy NON-str summary (a dict /
    # list / number) would otherwise pass the emptiness check and blow up downstream as an
    # aiohttp 500 HTML body, breaking the "every 4xx/5xx is an opaque reason code" contract.
    if not isinstance(summary, str) or not isinstance(detail, str):
        log.warning("scribe.ingest_web.rejected", route=BUG_ROUTE, reason="invalid_payload")
        return _reject("invalid_payload", 400)
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    events = payload.get("events") if isinstance(payload.get("events"), list) else []
    # ILB — an entirely empty report is refused VISIBLY (the UI renders the 4xx), never a
    # silent 200 that writes a contentless file.
    if not summary.strip() and not detail.strip():
        log.warning("scribe.ingest_web.rejected", route=BUG_ROUTE, reason="empty_report")
        return _reject("empty_report", 400)

    from alfred.scribe import bug as bug_mod
    try:
        _, bug_id = bug_mod.write_bug_report(
            config, summary=summary, detail=detail, context=context, events=events)
    except bug_mod.BugCapRefused as e:
        log.warning("scribe.ingest_web.rejected", route=BUG_ROUTE, reason=e.reason)
        return _reject(e.reason, 429)                    # over-cap → explicit 4xx the UI renders
    return web.json_response({"bug_id": bug_id}, status=200)


# --- #12 slice 12b — PWA identity session routes (ingest-class) --------------

async def _handle_session_open(request: web.Request) -> web.StreamResponse:
    """``POST /scribe/session/open?user=<slug>`` — mint a server-issued identity session bound
    to a ``config.clinicians`` slug (design §2.3). Ingest-class (rides the ingest token the page
    already holds). Returns ``{session, clinician}``.

    FAIL-CLOSED 403 ``unknown_clinician`` on a slug that is not a ``config.clinicians`` entry
    VERBATIM (case-sensitive, matching attest/enroll) — identity is NEVER fabricated (design
    §2.5). An empty ``clinicians`` list ⇒ every open 403s (no identity without a configured
    clinician)."""
    config: ScribeConfig = request.app["scribe_config"]
    user = request.query.get("user", "")
    # Grammar + verbatim-allowlist gate. ``enrollment.valid_user`` is the SHARED identity-slug
    # regex (one grammar across enroll/consent/session — no re-derived regex).
    from alfred.scribe import enrollment as _en
    if not _en.valid_user(user) or user not in set(config.clinicians):
        log.warning("scribe.session.rejected", route=SESSION_OPEN_ROUTE, reason="unknown_clinician")
        return _reject("unknown_clinician", 403)
    idle_ttl, abs_ttl = _session_ttls(config)
    now = time.monotonic()
    _sweep_pwa_sessions(now, idle_ttl=idle_ttl, abs_ttl=abs_ttl)
    if len(_PWA_SESSIONS) >= _MAX_PWA_SESSIONS:
        # Explicit cap signal (never a silent drop) — the RAM table is full of STILL-LIVE
        # sessions (post-sweep). On the single-trust loopback surface this is a client leak,
        # not an attack; the operator sees the cap_hit and the idle TTL reclaims regardless.
        log.warning("scribe.session.cap_hit", cap="sessions", live=len(_PWA_SESSIONS))
        return _reject("session_cap", 429)
    token = mint_pwa_session_id()
    _PWA_SESSIONS[token] = PwaSession(clinician=user, opened_at=now, last_seen=now)
    log.info("scribe.session.opened", clinician=user, live=len(_PWA_SESSIONS),
             detail="PWA identity session minted (RAM-only) — supplies consent captured_by")
    return web.json_response({"session": token, "clinician": user}, status=200)


async def _handle_session_close(request: web.Request) -> web.StreamResponse:
    """``POST /scribe/session/close`` (``X-Scribe-Session`` header) — drop the session from RAM
    (explicit end-of-day / clinician-switch teardown, design §2.3). IDEMPOTENT: an absent /
    unknown / already-expired token still returns ``{closed: true}`` — a teardown must never
    fail-loud (the client is releasing identity; the server obliges regardless)."""
    token = (request.headers.get(SESSION_HEADER) or "").strip()
    existed = _PWA_SESSIONS.pop(token, None) is not None
    log.info("scribe.session.closed", existed=existed,
             detail="PWA identity session dropped (idempotent teardown)")
    return web.json_response({"closed": True}, status=200)


# --- #12 slice 12c — consent transition route -------------------------------

async def _handle_consent(request: web.Request) -> web.StreamResponse:
    """``POST /scribe/consent?label&decision={confirmed|declined|withdrawn}`` — the per-encounter
    consent state transition (design §4/§5). Ingest-class + ``X-Scribe-Session`` for identity.

    Ordering contract (§5): the DURABLE consent append MUST land before this route acknowledges.
    On a store failure the durable emitter RAISES → 5xx, the cache is NOT flipped, and capture is
    never told it stopped (the client keeps the encounter open and retries). The hot cache flips
    ONLY after the durable append committed — so the chunk gate (§6.1) can never see a state the
    store does not hold. Everything is under the per-encounter lock so no chunk write interleaves.
    """
    config: ScribeConfig = request.app["scribe_config"]
    events: "ScribeEvents | None" = request.app.get("scribe_events")

    label = request.query.get("label", "")
    if not ENCOUNTER_LABEL_RE.fullmatch(label):
        log.warning("scribe.ingest_web.rejected", route=CONSENT_ROUTE, reason="invalid_label")
        return _reject("invalid_label", 400)
    decision = (request.query.get("decision") or "").strip().lower()
    if decision not in _CONSENT_DECISIONS:
        log.warning("scribe.ingest_web.rejected", route=CONSENT_ROUTE, reason="invalid_decision")
        return _reject("invalid_decision", 400)
    try:
        enc_id = compute_encounter_id(label, salt=config.encounter_salt)
    except EncounterIdentityError:
        log.error("scribe.ingest_web.identity_unavailable", route=CONSENT_ROUTE)
        return _reject("identity_unavailable", 500)

    # Consent is DURABLE evidence — with no active event store we cannot record it, so we must
    # not let it appear captured. Fail-closed (§4: no consent state → the mic never opens).
    if events is None or not events.active:
        log.error("scribe.consent.unavailable", route=CONSENT_ROUTE, encounter_id=enc_id,
                  detail="consent requested but the event store is inactive — REFUSING "
                         "(consent must be durably recorded before the act it gates)")
        return _reject("consent_unavailable", 503)

    # Identity — the two decisions resolve it from DIFFERENT sources by design:
    if decision in ("confirmed", "declined"):
        # confirm/decline: the ACTING clinician IS the live session (§2.5) — REQUIRED (consent
        # evidence names a real clinician, never a null identity). No/expired session → 401.
        session = _resolve_session(request)
        if session is None:
            log.warning("scribe.consent.no_session", route=CONSENT_ROUTE, decision=decision)
            return _reject("no_session", 401)
        captured_by = session.clinician
    else:  # withdrawn
        # §2.4 GOVERNS: the withdrawal actor is ALWAYS the durable ``consent.confirmed``
        # captured_by (the clinician who OBTAINED consent), NEVER the live session. The session
        # is deliberately NOT read here — a shared device that rebinds to another clinician
        # mid-encounter must not falsely attribute the withdrawal to whoever holds the session
        # now (that would durably chain a verify-PASSING record with confirmed≠withdrawn actors —
        # the exact falsified-attribution this arc exists to prevent). Empty ⇒ no confirmed event
        # to withdraw; the facade's transition-legality guard rejects that as an illegal
        # transition (409) inside the lock below (∅/declined → withdrawn is not legal).
        captured_by = events.consent_captured_by(enc_id)

    from alfred.evstore import EventStoreError
    from alfred.scribe.events import ConsentTransitionError

    async with _enc_lock(request.app["enc_locks"], enc_id):
        try:
            if decision == "confirmed":
                events.consent_confirmed(subject_id=enc_id, captured_by=captured_by)
            elif decision == "declined":
                events.consent_declined(subject_id=enc_id, captured_by=captured_by)
            else:  # withdrawn — at_seq is the on-disk max chunk seq the withdrawal saw (§5.2)
                existing = _existing_seqs(Path(config.input_dir) / label)
                at_seq = existing[-1] if existing else 0
                events.consent_withdrawn(subject_id=enc_id, at_seq=at_seq, actor=captured_by)
        except ConsentTransitionError:
            # An illegal transition (double-confirm, withdraw-on-∅/declined, any terminal move)
            # — refused at the facade (§3.1). ConsentTransitionError subclasses EventStoreError,
            # so it MUST be caught first (a 409, not a 5xx — the client asked for something the
            # state machine forbids, not a store failure).
            log.warning("scribe.consent.illegal_transition", route=CONSENT_ROUTE, decision=decision,
                        encounter_id=enc_id)
            return _reject("illegal_transition", 409)
        except EventStoreError:
            # The DURABLE append FAILED. The withdrawal ordering contract (§5): NOT acknowledged,
            # NO cache flip — capture is never told it stopped on an unrecorded withdrawal; the
            # client keeps the encounter open and can retry. 5xx (fail-loud, PHI-safe code).
            log.error("scribe.consent.write_failed", route=CONSENT_ROUTE, decision=decision,
                      encounter_id=enc_id)
            return _reject("consent_write_failed", 503)
        # ONLY after the durable append committed: flip the hot gate (§6.2). Inside the lock, so
        # a chunk's consent-check (also under the lock) sees the flip atomically w.r.t. the append.
        request.app["consent_cache"][enc_id] = decision
    log.info("scribe.consent.recorded", route=CONSENT_ROUTE, decision=decision, encounter_id=enc_id,
             detail="durable consent transition recorded + hot cache flipped")
    return web.json_response(
        {"encounter_id": enc_id, "decision": decision, "captured_by": captured_by}, status=200)


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
    # The INGEST token (encounter capability) is embedded for the JS; the ENROLL token is
    # NEVER embedded (page possession must not grant biometric mutation). The clinician
    # slugs are embedded so the enrolment view can OFFER the identity instead of making it
    # hand-typed — the server matches them VERBATIM, so a typo would fail-close a
    # consented recording with 403.
    body = render_index(config.ingest_web.token, config.clinicians,
                        bug_max_per_session=config.bug.max_per_session)
    return web.Response(text=body, content_type="text/html", charset="utf-8",
                        headers=_static_headers())


async def _handle_app_js(request: web.Request) -> web.StreamResponse:
    """``GET /scribe/app.js`` — the same-origin PWA logic (bearer-EXEMPT;
    Host-pinned + loopback). No token here (the token lives in the page's data
    attribute); this is just code. CSP-served (script-src 'self')."""
    return web.Response(text=APP_JS, content_type="application/javascript",
                        charset="utf-8", headers=_static_headers())


def _asset_headers() -> dict[str, str]:
    """Headers for the STATIC install assets (manifest / icons / favicon). No CSP is
    needed (these are SECRET-FREE data, not the token-bearing page — CSP protects the
    PAGE), but ``no-store`` keeps the no-residue posture (nothing an icon fetch leaves
    behind) and ``nosniff`` pins the declared content type."""
    return {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


async def _handle_manifest(request: web.Request) -> web.StreamResponse:
    """``GET /manifest.webmanifest`` — the Web App Manifest (bearer-EXEMPT; Host-pinned +
    loopback). STATIC + SECRET-FREE; ``display: standalone`` is what makes Chrome install
    the app without a URL bar. NO service worker / storage key (installability only)."""
    return web.Response(text=MANIFEST_JSON, content_type="application/manifest+json",
                        charset="utf-8", headers=_asset_headers())


async def _handle_icon_192(request: web.Request) -> web.StreamResponse:
    """``GET /scribe/icon-192.png`` — the 192px maskable install icon (STATIC, SECRET-FREE)."""
    return web.Response(body=ICON_192_PNG, content_type="image/png", headers=_asset_headers())


async def _handle_icon_512(request: web.Request) -> web.StreamResponse:
    """``GET /scribe/icon-512.png`` — the 512px maskable install icon (STATIC, SECRET-FREE)."""
    return web.Response(body=ICON_512_PNG, content_type="image/png", headers=_asset_headers())


async def _handle_favicon(request: web.Request) -> web.StreamResponse:
    """``GET /favicon.ico`` — a tiny STATIC, SECRET-FREE favicon (PNG bytes; browsers sniff
    by content, so the ``.ico`` path is fine). Serving it 200 kills the per-page-load
    ``/favicon.ico`` 401 → ``scribe.ingest_web.rejected reason=bad_token`` log spam (Task
    #3): Chrome auto-fetches it and it is no longer an un-exempt bearer-required route."""
    return web.Response(body=FAVICON_PNG, content_type="image/png", headers=_asset_headers())


async def _handle_apple_touch_icon(request: web.Request) -> web.StreamResponse:
    """``GET /apple-touch-icon.png`` (and ``-precomposed.png``) — the 180px iOS home-screen
    tile (STATIC, SECRET-FREE). Serving it 200 fixes BOTH the operator-iPhone tile (iOS uses
    apple-touch-icon, not the manifest icons member) AND the same 401 warning-spam class the
    favicon fix closed (WebKit auto-probes these paths). One handler for both canonical
    paths — they serve identical bytes; the sized-variant probes are suppressed by the
    ``<link rel="apple-touch-icon">`` in the head, so no per-size route is needed."""
    return web.Response(body=APPLE_TOUCH_ICON_PNG, content_type="image/png",
                        headers=_asset_headers())


# --- app + server lifecycle -------------------------------------------------

def create_ingest_app(
    config: ScribeConfig, *, events: "ScribeEvents | None" = None,
) -> web.Application:
    """Build the ingest ``web.Application`` — the 3 bearer-required API routes +
    the 2 bearer-exempt static PWA routes (Slice B) + the 6 bearer-exempt
    standalone-install assets (manifest/icons/favicon/apple-touch-icon), the
    split-policy security middleware, and ``client_max_size`` pinned to the
    per-chunk byte cap (N3).

    Only instantiated when ``ingest_web.enabled`` (the daemon starts the server
    solely then), so the static surface is INERT by default — no server, no
    page."""
    app = web.Application(
        client_max_size=config.ingest_web.max_chunk_bytes,
        middlewares=[_build_security_middleware(config)],
    )
    app["scribe_config"] = config
    # The medico-legal event-store facade (event-store design §2.2 / §5.4). The four
    # handlers read it via request.app.get("scribe_events") and emit encounter.*
    # best-effort. None (default / non-clinical / degraded) → the handlers no-op.
    app["scribe_events"] = events
    # #12 slice 12c — per-encounter consent serialization locks + the hot consent-state cache
    # (design §5.3/§6.2). App-scoped (not module RAM): the locks bind to this server's loop, and a
    # fresh server (restart) starts with empty caches — the durable store is the source of truth
    # and the chunk gate falls back to it on a cache miss (§5.4).
    app["enc_locks"] = {}
    app["consent_cache"] = {}
    # 3 API routes — bearer-required, byte-identical to Slice A.
    app.router.add_post(INGEST_CHUNK_ROUTE, _handle_ingest_chunk)
    app.router.add_post(CLOSE_ROUTE, _handle_close)
    app.router.add_get(STATUS_ROUTE, _handle_status)
    # Task #4 — bug capture (ingest-token gated, handler-gated on bug.enabled).
    app.router.add_post(BUG_ROUTE, _handle_bug)
    # #12 slice 12b — per-clinician PWA identity sessions (ingest-class; ride the ingest token,
    # fall through _authorize_route's default ingest branch — no new credential, design §2.3).
    app.router.add_post(SESSION_OPEN_ROUTE, _handle_session_open)
    app.router.add_post(SESSION_CLOSE_ROUTE, _handle_session_close)
    # #12 slice 12c — the consent transition route (ingest-class; confirmed/declined gate capture
    # start, withdrawn halts it under the durable-before-ack ordering contract, design §4/§5).
    app.router.add_post(CONSENT_ROUTE, _handle_consent)
    # 2 static PWA routes — bearer-exempt (Host-pinned + loopback), Slice B.
    app.router.add_get(PAGE_ROUTE, _handle_page)
    app.router.add_get(APP_JS_ROUTE, _handle_app_js)
    # 6 standalone-install assets — bearer-exempt (Host-pinned + loopback), STATIC +
    # SECRET-FREE (Task #1 manifest/icons + Task #3 favicon + apple-touch-icon).
    app.router.add_get(MANIFEST_ROUTE, _handle_manifest)
    app.router.add_get(ICON_192_ROUTE, _handle_icon_192)
    app.router.add_get(ICON_512_ROUTE, _handle_icon_512)
    app.router.add_get(FAVICON_ROUTE, _handle_favicon)
    app.router.add_get(APPLE_TOUCH_ICON_ROUTE, _handle_apple_touch_icon)
    app.router.add_get(APPLE_TOUCH_ICON_PRECOMPOSED_ROUTE, _handle_apple_touch_icon)
    # P4-5a enrollment face (biometric-custody capability). Registered ONLY when
    # enroll_token is set — DEFENCE IN DEPTH with the middleware's inert gate, which 404s
    # the enroll-face paths independently (either alone suffices; keep BOTH). Lazy import
    # avoids an enroll_web↔ingest_web cycle.
    if config.ingest_web.enroll_token:
        from alfred.scribe import enroll_web
        enroll_web.register_enroll_routes(app)
    else:
        log.info(
            "scribe.enroll.inert",
            detail="scribe.ingest_web.enroll_token is unset — the voice-enrollment "
                   "face is INERT (enroll routes 404). Set enroll_token to arm it.",
        )
    return app


class IngestWebServer:
    """Owns the ``aiohttp`` ``AppRunner`` lifecycle, bound to the loopback host.

    Started by the scribe daemon (on the daemon's own event loop) when
    ``ingest_web.enabled``; stopped in the daemon's shutdown ``finally``."""

    def __init__(
        self, config: ScribeConfig, *, events: "ScribeEvents | None" = None,
    ) -> None:
        self._config = config
        self._events = events
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        web_cfg = self._config.ingest_web
        app = create_ingest_app(self._config, events=self._events)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, web_cfg.host, web_cfg.port)
        await site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
