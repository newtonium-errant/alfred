"""Per-call sovereign HTTP guard — loopback-assert before connect.

The config-time barriers in :mod:`alfred.sovereign.boundary` prove the
CONFIGURED endpoints are on-box. This guard is the defense against CODE DRIFT:
a new (or existing) call site that constructs an httpx client and talks to a
hardcoded cloud URL — e.g. ``telegram/transcribe.py``'s hardcoded Groq
endpoint, or the Anthropic SDK's httpx transport — would bypass the config
barriers entirely. Once installed (process-global), every outbound
``httpx.Client.send`` / ``httpx.AsyncClient.send``, ``aiohttp.ClientSession._request``
AND ``requests.Session.send`` asserts that the request's host is provably loopback
BEFORE the transport connects; a non-loopback host raises
:class:`SovereignBoundaryError` (``reason="http_guard"``) so no bytes leave the
box.

Install is idempotent and reversible (``uninstall_...`` restores the original
``send`` / ``_request`` methods — used by tests).

PROCESS COVERAGE (honest, walked against source — do NOT claim spawn
self-reinstall). The ONLY install site today is the run_all PARENT process
(``orchestrator._enforce_sovereign_boundary_or_exit`` installs it after the
boundary passes). It propagates to tool child processes ONLY via FORK
inheritance — the current Linux ``multiprocessing`` default, so children fork
with the already-patched ``httpx`` / ``aiohttp`` and are covered. It does NOT
auto-reinstall: a child launched under the ``spawn`` start method would start
with a fresh, UNGUARDED transport. A sovereign DAEMON process must therefore
install the guard itself — the scribe daemon runner self-installs it in P1-d.
With the barrier-(d) allowlist the scribe tool is the ONLY daemon a sovereign
config can run, so P1-d's per-process self-install is the real coverage
guarantee; the parent-fork inheritance is a belt on top of it, not the
load-bearing path.

SCOPE + COVERAGE (honest, walked against source — feedback_credential_strip #3:
security docstrings must be EXECUTED/greppable, not plausible). This guard wraps
the THREE client transports live in the repo:

  * ``httpx.Client.send`` / ``httpx.AsyncClient.send`` — the Anthropic/OpenAI
    SDKs use httpx underneath; ``telegram/transcribe.py``'s hardcoded Groq
    endpoint uses httpx.
  * ``requests.Session.send`` — the transport huggingface_hub uses (a transitive
    dep of faster-whisper, live in the scribe process). One wrap of ``Session.send``
    covers the initial request AND every redirect hop (``resolve_redirects``
    re-enters ``send``). This closes a real blind spot: the ``scribe`` config
    section is barrier-(d) ALLOWLISTED, so a hardcoded/transitive ``requests``
    cloud call inside a scribe code path (e.g. a model auto-download, an HF
    revision-check GET, a diarization/embedding fetch) is NOT denied by config —
    the guard is the only backstop, and until the audit fix it did not cover
    requests at all. IMPORT-GUARDED (no-op if requests absent).
  * ``aiohttp.ClientSession._request`` + ``ClientSession.__init__`` — the web
    STT/TTS surfaces (``web/stt_deepgram.py`` Deepgram, ``web/tts_elevenlabs.py``
    ElevenLabs) use ``aiohttp.ClientSession`` WebSockets. The WS handshake is an
    HTTP GET upgrade that flows through ``_request`` (``ws_connect -> request ->
    _request``), so the ``_request`` wrap covers regular requests AND WebSocket
    connects on the INITIAL url. REDIRECT TARGETS ARE RE-ASSERTED PER-HOP (W1),
    RETROACTIVELY: aiohttp follows 3xx inside ``_request``'s own loop (default
    ``allow_redirects=True``), so the initial-URL wrap never re-fires on the
    target. A SINGLE seam closes this — the ``_request`` wrap lazily injects a
    guard ``TraceConfig`` into the LIVE session's ``_trace_configs`` on each
    request (``_request`` rebuilds its per-request trace list from
    ``self._trace_configs`` EVERY call — verified against aiohttp 3.13.5 source),
    whose ``on_request_redirect`` re-asserts loopback on every 3xx target; a
    raising callback aborts the follow BEFORE the next socket. Because the inject
    rides the retroactive ``_request`` wrap (the CLASS method — it fires for
    sessions constructed BEFORE the guard installed too), the redirect re-assert
    is exactly as retroactive as the initial-URL assert: NO pre-install-session
    blind spot (an earlier ``__init__``-only injection had one — a session built
    before install leaked its redirects). So a loopback URL that 3xx-redirects to
    a cloud host is REFUSED, not followed — full parity with the httpx path (which
    defaults follow_redirects=False), retroactive on both. Hermetically verified
    on aiohttp 3.13.5 INCLUDING the pre-install-session case: 0 cloud dials. This
    closes task #40 — the security prerequisite for the STAY-C PWA scribe channel
    (#49). IMPORT-GUARDED: aiohttp
    is a web dependency that may be absent
    from a given sovereign venv, so the aiohttp wrap is applied ONLY if aiohttp
    is importable; if absent it NO-OPS gracefully (httpx-only coverage, never a
    crash). ``is_aiohttp_guard_installed()`` + the ``sovereign.http_guard.installed``
    log surface which wraps are live; a fresh ``install_...`` AFTER aiohttp is
    later installed (a web mount) DOES then cover it. This is defense-in-depth on
    top of barrier (d), which still denies the ``web`` config section at LOAD.

STILL-UNCOVERED transports (honest — each is neutralised by barrier (d) denying
the wiring config section at LOAD, NOT by this guard):

  * ``googleapiclient`` / ``httplib2`` — GCal (``integrations/gcal.py``). Denied
    at LOAD by the barrier-(d) allowlist (``gcal`` / ``integrations`` are not
    allowlisted). A distinct transport this guard does not patch.
  * the ``claude -p`` SUBPROCESS — a separate process the guard cannot see.
    ⚠️ It is NOT neutralised by stripping the credential: ``subprocess_env``
    strips the API key precisely so ``claude -p`` REROUTES to cached OAuth
    creds (~/.claude) and STILL reaches api.anthropic.com. It is neutralised
    by barrier (d) denying ``agent`` / ``curator`` / ``janitor`` / ``distiller``
    / ``instructor`` — a STRUCTURAL deny, NOT credential-strip (barrier c).
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
import structlog

from .boundary import SovereignBoundaryError, host_is_loopback

log = structlog.get_logger(__name__)

# Stored originals for reversible install. ``None`` => not installed.
_orig_sync_send: Callable[..., Any] | None = None
_orig_async_send: Callable[..., Any] | None = None
_orig_aiohttp_request: Callable[..., Any] | None = None
_orig_requests_send: Callable[..., Any] | None = None


def _assert_host_loopback(host: str, url_for_msg: str) -> None:
    """The CORE loopback assert — raise if ``host`` is not provably loopback.

    Fail-closed: an empty / unresolvable host raises (see ``host_is_loopback``).
    Shared by the httpx and aiohttp adapters so there is ONE assert, ONE error.
    """
    if not host_is_loopback(host):
        raise SovereignBoundaryError(
            "http_guard",
            f"outbound HTTP to non-loopback host {host or '(none)'!r} "
            f"(url={url_for_msg!r}) refused on a sovereign process. "
            f"No cloud fallback by design — this request would have left "
            f"the box.",
        )


def _assert_request_loopback(request: httpx.Request) -> None:
    """httpx adapter — raise if ``request`` targets a non-loopback host."""
    _assert_host_loopback(request.url.host or "", str(request.url))


def _assert_aiohttp_loopback(session: Any, str_or_url: Any) -> None:
    """aiohttp adapter — raise if the request targets a non-loopback host.

    ``str_or_url`` may be a ``str`` or ``yarl.URL``, and may be RELATIVE (the
    session's ``base_url`` supplies the host). We resolve it exactly as aiohttp
    does — via the session's own ``_build_url`` — so a loopback ``base_url`` +
    relative path is NOT false-blocked, and a cloud ``base_url`` + relative path
    IS blocked. If the internal builder is ever absent, fall back to the raw URL
    (a relative URL then has host ``None`` → fail-closed raise — safe)."""
    build = getattr(session, "_build_url", None)
    if callable(build):
        url = build(str_or_url)
    else:  # pragma: no cover — defensive fallback for a future aiohttp
        import yarl
        url = yarl.URL(str_or_url)
    _assert_host_loopback(url.host or "", str(url))


async def _assert_aiohttp_redirect_loopback(session: Any, ctx: Any, params: Any) -> None:
    """aiohttp ``on_request_redirect`` callback — RE-ASSERT loopback on the
    redirect TARGET, per hop (W1 fix).

    aiohttp follows 3xx redirects INSIDE ``_request``'s own loop (default
    ``allow_redirects=True``), reassigning the URL internally — so the
    ``_request`` initial-URL wrap never re-fires on the target. Without this, a
    loopback URL that redirects to a cloud host would be followed PAST the guard
    (materially weaker than the httpx path, which defaults follow_redirects=False).

    ``params.url`` is the redirect ORIGIN (the URL that returned the 3xx); the
    TARGET is the ``Location`` header, resolved relative against the origin
    EXACTLY as aiohttp resolves it (``origin.join(location)``). RAISING here
    aborts the follow BEFORE the next connection — HERMETICALLY VERIFIED on
    aiohttp 3.13.5 (the raising trace callback yields 0 sockets to the cloud
    host; the redirect mutation-bound test proves it). This callback is carried
    by a guard ``TraceConfig`` that ``_ensure_aiohttp_redirect_guard`` injects
    into the live session on its first guarded request (retroactive — see
    ``_install_aiohttp_guard``)."""
    import yarl
    location = params.response.headers.get("Location", "")
    target = params.url.join(yarl.URL(location))
    _assert_host_loopback(target.host or "", str(target))


def _assert_requests_loopback(prepared: Any) -> None:
    """requests adapter — raise if the PreparedRequest targets a non-loopback host.

    ``requests.PreparedRequest.url`` is a fully-resolved absolute URL string;
    wrapping ``Session.send`` covers the INITIAL request AND every redirect hop
    (requests' ``resolve_redirects`` re-enters ``Session.send`` per hop with the
    new absolute URL), so there is no separate redirect seam to close."""
    url = getattr(prepared, "url", "") or ""
    _assert_host_loopback(urlsplit(url).hostname or "", str(url))


def _try_import_aiohttp() -> Any | None:
    """Return the ``aiohttp`` module, or ``None`` if it is not installed.

    aiohttp is a web dependency that a given sovereign venv may lack. Factored
    out so the import-absent path is testable (monkeypatch to return ``None``)."""
    try:
        import aiohttp
    except ImportError:
        return None
    return aiohttp


def _try_import_requests() -> Any | None:
    """Return the ``requests`` module, or ``None`` if it is not installed.

    requests IS installed in the current venv (it is a transitive dep of
    huggingface_hub, which faster-whisper pulls), but the guard import-guards it
    anyway so a stripped sovereign venv without it NO-OPS gracefully. Factored out
    so the import-absent path is testable (monkeypatch to return ``None``)."""
    try:
        import requests
    except ImportError:
        return None
    return requests


def _install_httpx_guard() -> None:
    """Wrap httpx ``send``. Idempotent (no double-wrap)."""
    global _orig_sync_send, _orig_async_send
    if _orig_sync_send is not None:
        return  # already installed

    _orig_sync_send = httpx.Client.send
    _orig_async_send = httpx.AsyncClient.send

    _sync_original = _orig_sync_send
    _async_original = _orig_async_send

    def _guarded_sync_send(self: httpx.Client, request: httpx.Request, *args: Any, **kwargs: Any) -> Any:
        _assert_request_loopback(request)
        return _sync_original(self, request, *args, **kwargs)

    async def _guarded_async_send(self: httpx.AsyncClient, request: httpx.Request, *args: Any, **kwargs: Any) -> Any:
        _assert_request_loopback(request)
        return await _async_original(self, request, *args, **kwargs)

    httpx.Client.send = _guarded_sync_send  # type: ignore[method-assign]
    httpx.AsyncClient.send = _guarded_async_send  # type: ignore[method-assign]


def _ensure_aiohttp_redirect_guard(session: Any, aiohttp: Any) -> None:
    """Lazily ensure ``session`` carries the guard redirect ``TraceConfig`` (W1).

    RETROACTIVE by design: called from the ``_request`` wrap (which fires for
    EVERY session — even ones constructed BEFORE the guard installed), so there
    is no ``__init__``-only blind spot. aiohttp's ``_request`` rebuilds its
    per-request trace list from ``self._trace_configs`` on every call (verified
    against aiohttp 3.13.5 source, ``_request`` lines ~128-134), so injecting the
    frozen guard trace here — before delegating to the real ``_request`` — makes
    ``on_request_redirect`` fire on this request's redirect hops.

    Idempotent via membership scan (no per-request re-append, no foreign sentinel
    attribute): if a trace already carries our callback we return. The trace list
    is REASSIGNED to a fresh list (not mutated in place) so a session whose
    ``_trace_configs`` is a tuple is handled without an ``AttributeError``; the
    guard trace we append is frozen by us (aiohttp freezes user configs at
    ``__init__``; a post-construction frozen trace fires correctly — verified)."""
    trace_configs = getattr(session, "_trace_configs", None)
    if trace_configs is None:  # pragma: no cover — defensive for a future aiohttp
        return
    for tc in trace_configs:
        if _assert_aiohttp_redirect_loopback in tc.on_request_redirect:
            return  # already guarded — idempotent, no accumulation
    guard_tc = aiohttp.TraceConfig()
    guard_tc.on_request_redirect.append(_assert_aiohttp_redirect_loopback)
    guard_tc.freeze()
    session._trace_configs = [*trace_configs, guard_tc]


def _install_aiohttp_guard() -> bool:
    """Wrap aiohttp IF installed — a SINGLE retroactive ``_request`` seam.

    One wrap, two asserts, both retroactive (they ride the CLASS method, so they
    fire for sessions built BEFORE the guard installed too):
      * INITIAL url — ``_assert_aiohttp_loopback`` on the request URL.
      * per-hop REDIRECT — ``_ensure_aiohttp_redirect_guard`` lazily injects a
        guard ``TraceConfig`` into this session's ``_trace_configs`` so
        ``on_request_redirect`` re-asserts loopback on every 3xx target; a raising
        callback aborts the follow BEFORE the next socket (hermetically verified
        on aiohttp 3.13.5 — 0 cloud dials, incl. the pre-install-session case).

    No ``__init__`` wrap — the lazy inject on the retroactive ``_request`` seam
    fully subsumes it (an ``__init__``-only injection left a pre-install-session
    blind spot). Idempotent (no double-wrap). Returns True iff the aiohttp wrap is
    live after this call. No-ops gracefully (returns False) when aiohttp is absent
    — the guard is httpx-only then, never a crash. A later call (after aiohttp is
    installed) DOES apply the wrap (the ``_orig_aiohttp_request is None`` sentinel
    stays None until the wrap actually lands)."""
    global _orig_aiohttp_request
    if _orig_aiohttp_request is not None:
        return True  # already wrapped
    aiohttp = _try_import_aiohttp()
    if aiohttp is None:
        return False  # not installed → httpx-only coverage (graceful no-op)

    _orig_aiohttp_request = aiohttp.ClientSession._request
    _aiohttp_request_original = _orig_aiohttp_request

    async def _guarded_request(self: Any, method: str, str_or_url: Any, *args: Any, **kwargs: Any) -> Any:
        # Retrofit the redirect guard onto THIS (possibly pre-install) session
        # before delegating — _request rebuilds its trace list from
        # self._trace_configs each call, so the inject takes effect this request.
        _ensure_aiohttp_redirect_guard(self, aiohttp)
        _assert_aiohttp_loopback(self, str_or_url)
        return await _aiohttp_request_original(self, method, str_or_url, *args, **kwargs)

    aiohttp.ClientSession._request = _guarded_request  # type: ignore[method-assign]
    return True


def _install_requests_guard() -> bool:
    """Wrap ``requests.Session.send`` IF requests is installed. Idempotent.

    ``Session.send`` is the choke every ``requests`` call funnels through
    (``requests.get`` / ``.post`` → ``Session.request`` → ``Session.send``), AND
    every redirect hop re-enters it (``resolve_redirects``), so one wrap covers
    initial + redirects. Closes the scribe-allowlisted-section requests-egress
    surface (barrier-d allowlists ``scribe``, so a hardcoded/transitive requests
    cloud call inside a scribe code path — e.g. huggingface_hub's revision-check
    GET — is NOT denied by config; this guard is the backstop). Returns True iff
    the requests wrap is live after this call; NO-OPS (returns False) when requests
    is absent."""
    global _orig_requests_send
    if _orig_requests_send is not None:
        return True  # already wrapped
    requests = _try_import_requests()
    if requests is None:
        return False  # not installed → graceful no-op

    _orig_requests_send = requests.Session.send
    _original = _orig_requests_send

    def _guarded_send(self: Any, request: Any, **kwargs: Any) -> Any:
        _assert_requests_loopback(request)
        return _original(self, request, **kwargs)

    requests.Session.send = _guarded_send  # type: ignore[method-assign]
    return True


def install_sovereign_http_guard() -> None:
    """Install the process-global loopback guard on httpx, aiohttp AND requests.
    Idempotent.

    Surfaces coverage via the ``sovereign.http_guard.installed`` log (ILB —
    always emitted so which transports are guarded is greppable in the daemon's
    sovereign attestation). ``aiohttp=False`` / ``requests=False`` means that
    transport is not installed in this venv; a later install after the dep lands
    covers it.
    """
    _install_httpx_guard()
    aiohttp_installed = _install_aiohttp_guard()
    requests_installed = _install_requests_guard()
    log.info(
        "sovereign.http_guard.installed",
        httpx=is_sovereign_http_guard_installed(),
        aiohttp=aiohttp_installed,
        requests=requests_installed,
    )


def uninstall_sovereign_http_guard() -> None:
    """Restore the original httpx ``send`` + aiohttp ``_request`` + requests
    ``Session.send``. No-op if unset.

    (The aiohttp redirect guard rides ``_request`` via a lazily-injected
    ``TraceConfig``; restoring ``_request`` removes the inject site. Sessions
    constructed while the guard was live keep a harmless frozen guard trace on
    their ``_trace_configs`` for their lifetime — it only re-asserts loopback, and
    those sessions are themselves torn down; no live ``__init__``/class patch
    remains.)"""
    global _orig_sync_send, _orig_async_send, _orig_aiohttp_request, _orig_requests_send
    if _orig_sync_send is not None:
        httpx.Client.send = _orig_sync_send  # type: ignore[method-assign]
        httpx.AsyncClient.send = _orig_async_send  # type: ignore[method-assign]
        _orig_sync_send = None
        _orig_async_send = None
    if _orig_aiohttp_request is not None:
        aiohttp = _try_import_aiohttp()
        if aiohttp is not None:
            aiohttp.ClientSession._request = _orig_aiohttp_request  # type: ignore[method-assign]
        _orig_aiohttp_request = None
    if _orig_requests_send is not None:
        requests = _try_import_requests()
        if requests is not None:
            requests.Session.send = _orig_requests_send  # type: ignore[method-assign]
        _orig_requests_send = None


def is_sovereign_http_guard_installed() -> bool:
    """Return True iff the httpx guard is currently installed."""
    return _orig_sync_send is not None


def is_aiohttp_guard_installed() -> bool:
    """Return True iff the aiohttp ``_request`` guard is currently installed.

    False when aiohttp is not installed in this venv (httpx-only coverage) — part
    of the sovereign attestation surfaced on ``scribe.daemon.up``."""
    return _orig_aiohttp_request is not None


def is_requests_guard_installed() -> bool:
    """Return True iff the ``requests.Session.send`` guard is currently installed.

    False when requests is not installed in this venv. Closes the
    huggingface_hub/faster-whisper (requests.Session) transport blind spot inside
    the barrier-d-allowlisted ``scribe`` section."""
    return _orig_requests_send is not None
