"""Per-call sovereign HTTP guard — loopback-assert before connect.

The config-time barriers in :mod:`alfred.sovereign.boundary` prove the
CONFIGURED endpoints are on-box. This guard is the defense against CODE DRIFT:
a new (or existing) call site that constructs an httpx client and talks to a
hardcoded cloud URL — e.g. ``telegram/transcribe.py``'s hardcoded Groq
endpoint, or the Anthropic SDK's httpx transport — would bypass the config
barriers entirely. Once installed (process-global), every outbound
``httpx.Client.send`` / ``httpx.AsyncClient.send`` asserts that the request's
host is provably loopback BEFORE the transport connects; a non-loopback host
raises :class:`SovereignBoundaryError` (``reason="http_guard"``) so no bytes
leave the box.

Install is idempotent and reversible (``uninstall_...`` restores the original
``send`` methods — used by tests).

PROCESS COVERAGE (honest, walked against source — do NOT claim spawn
self-reinstall). The ONLY install site today is the run_all PARENT process
(``orchestrator._enforce_sovereign_boundary_or_exit`` installs it after the
boundary passes). It propagates to tool child processes ONLY via FORK
inheritance — the current Linux ``multiprocessing`` default, so children fork
with the already-patched ``httpx`` and are covered. It does NOT auto-reinstall:
a child launched under the ``spawn`` start method would start with a fresh,
UNGUARDED ``httpx``. A sovereign DAEMON process must therefore install the
guard itself — the scribe daemon runner self-installs it in P1-d. With the
barrier-(d) allowlist the scribe tool is the ONLY daemon a sovereign config
can run, so P1-d's per-process self-install is the real coverage guarantee;
the parent-fork inheritance is a belt on top of it, not the load-bearing path.

SCOPE + KNOWN GAPS (honest, not a completeness claim). This guard wraps
``httpx.Client.send`` / ``httpx.AsyncClient.send`` ONLY. It catches httpx-backed
cloud paths (the Anthropic/OpenAI SDKs use httpx underneath; ``telegram/
transcribe.py``'s hardcoded Groq endpoint uses httpx). It does NOT cover, and
each of these is instead neutralised by barrier (d) denying the wiring config
section at LOAD:

  * ``aiohttp`` — the web STT/TTS surfaces (``web/stt_deepgram.py`` Deepgram,
    ``web/tts_elevenlabs.py`` ElevenLabs). Denied at LOAD by the barrier-(d)
    allowlist (``web`` is not sovereign-safe, so it is not in
    SOVEREIGN_ALLOWED_SECTIONS). An aiohttp guard extension is a HARD P2
    BLOCKER (task #40) before the scribe web UI may route PHI.
  * ``googleapiclient`` — GCal (``integrations/gcal.py``). Denied at LOAD by
    the barrier-(d) allowlist (``gcal`` / ``integrations`` are not
    allowlisted).
  * the ``claude -p`` SUBPROCESS — a separate process the guard cannot see.
    ⚠️ It is NOT neutralised by stripping the credential: ``subprocess_env``
    strips the API key precisely so ``claude -p`` REROUTES to cached OAuth
    creds (~/.claude) and STILL reaches api.anthropic.com. It is neutralised
    by barrier (d) denying ``agent`` / ``curator`` / ``janitor`` / ``distiller``
    / ``instructor`` — NOT by barrier (c).
"""

from __future__ import annotations

from typing import Any, Callable

import httpx

from .boundary import SovereignBoundaryError, host_is_loopback

# Stored originals for reversible install. ``None`` => not installed.
_orig_sync_send: Callable[..., Any] | None = None
_orig_async_send: Callable[..., Any] | None = None


def _assert_request_loopback(request: httpx.Request) -> None:
    """Raise if ``request`` targets a non-loopback host. Fail-closed."""
    host = request.url.host or ""
    if not host_is_loopback(host):
        raise SovereignBoundaryError(
            "http_guard",
            f"outbound HTTP to non-loopback host {host or '(none)'!r} "
            f"(url={str(request.url)!r}) refused on a sovereign process. "
            f"No cloud fallback by design — this request would have left "
            f"the box.",
        )


def install_sovereign_http_guard() -> None:
    """Install the process-global loopback guard on httpx. Idempotent."""
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


def uninstall_sovereign_http_guard() -> None:
    """Restore the original httpx ``send`` methods. No-op if not installed."""
    global _orig_sync_send, _orig_async_send
    if _orig_sync_send is None:
        return
    httpx.Client.send = _orig_sync_send  # type: ignore[method-assign]
    httpx.AsyncClient.send = _orig_async_send  # type: ignore[method-assign]
    _orig_sync_send = None
    _orig_async_send = None


def is_sovereign_http_guard_installed() -> bool:
    """Return True iff the guard is currently installed."""
    return _orig_sync_send is not None
