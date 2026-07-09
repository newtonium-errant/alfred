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
``send`` methods — used by tests). On Linux the orchestrator installs this in
the parent process before forking children, so every sovereign child inherits
the wrapped methods; a child launched fresh (spawn) re-installs on its own.

httpx is the right seam: the tools' cloud paths (STT via httpx, ElevenLabs via
httpx, and the Anthropic/OpenAI SDKs, which all use httpx underneath) route
through ``Client.send`` / ``AsyncClient.send``. The ``claude -p`` SUBPROCESS
path is out of scope here (a separate process) — it is neutralised by
barrier (c) stripping the credential, not by this in-process guard.
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
