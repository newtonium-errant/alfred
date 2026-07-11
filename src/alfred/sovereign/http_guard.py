"""Per-call sovereign HTTP guard — loopback-assert before connect.

The config-time barriers in :mod:`alfred.sovereign.boundary` prove the
CONFIGURED endpoints are on-box. This guard is the defense against CODE DRIFT:
a new (or existing) call site that constructs an httpx client and talks to a
hardcoded cloud URL — e.g. ``telegram/transcribe.py``'s hardcoded Groq
endpoint, or the Anthropic SDK's httpx transport — would bypass the config
barriers entirely. Once installed (process-global), every outbound
``httpx.Client.send`` / ``httpx.AsyncClient.send`` AND
``aiohttp.ClientSession._request`` asserts that the request's host is provably
loopback BEFORE the transport connects; a non-loopback host raises
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
BOTH client transports the repo uses:

  * ``httpx.Client.send`` / ``httpx.AsyncClient.send`` — the Anthropic/OpenAI
    SDKs use httpx underneath; ``telegram/transcribe.py``'s hardcoded Groq
    endpoint uses httpx.
  * ``aiohttp.ClientSession._request`` — the web STT/TTS surfaces
    (``web/stt_deepgram.py`` Deepgram, ``web/tts_elevenlabs.py`` ElevenLabs) use
    ``aiohttp.ClientSession`` WebSockets. The WS handshake is an HTTP GET upgrade
    that flows through ``_request`` (``ws_connect -> request -> _request``), so
    wrapping ``_request`` covers regular requests AND WebSocket connects. This
    closes task #40 — the security prerequisite for the STAY-C PWA scribe
    channel (#49). IMPORT-GUARDED: aiohttp is a web dependency that may be absent
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

import httpx
import structlog

from .boundary import SovereignBoundaryError, host_is_loopback

log = structlog.get_logger(__name__)

# Stored originals for reversible install. ``None`` => not installed.
_orig_sync_send: Callable[..., Any] | None = None
_orig_async_send: Callable[..., Any] | None = None
_orig_aiohttp_request: Callable[..., Any] | None = None


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


def _try_import_aiohttp() -> Any | None:
    """Return the ``aiohttp`` module, or ``None`` if it is not installed.

    aiohttp is a web dependency that a given sovereign venv may lack. Factored
    out so the import-absent path is testable (monkeypatch to return ``None``)."""
    try:
        import aiohttp
    except ImportError:
        return None
    return aiohttp


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


def _install_aiohttp_guard() -> bool:
    """Wrap ``aiohttp.ClientSession._request`` IF aiohttp is installed.

    Idempotent (no double-wrap). Returns True iff the aiohttp wrap is live after
    this call. No-ops gracefully (returns False) when aiohttp is absent — the
    guard is httpx-only then, never a crash. A later call (after aiohttp is
    installed) DOES apply the wrap (the ``_orig_aiohttp_request is None`` sentinel
    stays None until the wrap actually lands)."""
    global _orig_aiohttp_request
    if _orig_aiohttp_request is not None:
        return True  # already wrapped
    aiohttp = _try_import_aiohttp()
    if aiohttp is None:
        return False  # not installed → httpx-only coverage (graceful no-op)

    _orig_aiohttp_request = aiohttp.ClientSession._request
    _aiohttp_original = _orig_aiohttp_request

    async def _guarded_request(self: Any, method: str, str_or_url: Any, *args: Any, **kwargs: Any) -> Any:
        _assert_aiohttp_loopback(self, str_or_url)
        return await _aiohttp_original(self, method, str_or_url, *args, **kwargs)

    aiohttp.ClientSession._request = _guarded_request  # type: ignore[method-assign]
    return True


def install_sovereign_http_guard() -> None:
    """Install the process-global loopback guard on httpx AND aiohttp. Idempotent.

    Surfaces coverage via the ``sovereign.http_guard.installed`` log (ILB —
    always emitted so which transports are guarded is greppable in the daemon's
    sovereign attestation). ``aiohttp=False`` means aiohttp is not installed in
    this venv (httpx-only coverage); a later install after a web mount covers it.
    """
    _install_httpx_guard()
    aiohttp_installed = _install_aiohttp_guard()
    log.info(
        "sovereign.http_guard.installed",
        httpx=is_sovereign_http_guard_installed(),
        aiohttp=aiohttp_installed,
    )


def uninstall_sovereign_http_guard() -> None:
    """Restore the original httpx ``send`` + aiohttp ``_request``. No-op if unset."""
    global _orig_sync_send, _orig_async_send, _orig_aiohttp_request
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


def is_sovereign_http_guard_installed() -> bool:
    """Return True iff the httpx guard is currently installed."""
    return _orig_sync_send is not None


def is_aiohttp_guard_installed() -> bool:
    """Return True iff the aiohttp ``_request`` guard is currently installed.

    False when aiohttp is not installed in this venv (httpx-only coverage) — part
    of the sovereign attestation surfaced on ``scribe.daemon.up``."""
    return _orig_aiohttp_request is not None
