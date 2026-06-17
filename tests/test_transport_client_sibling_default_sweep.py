"""Regression pins — transport/client.py sibling-default sweep
(2026-05-21, Tier A #1).

Direct follow-through from the 2026-05-20 ``"hypatia"`` literal-default
sweep (commit ``51bc719``) which removed the hardcoded default on
``peer_propose_event.self_name``. Code-reviewer flagged the threshold
pattern: six sibling functions in the same file still defaulted
``self_name`` to a literal instance string (``"salem"`` or ``"kal-le"``).
Same antipattern, same memo (``feedback_hardcoding_and_alfred_naming.md``),
same fix shape — remove the default; make the kwarg required.

Ten total sites swept:

* ``peer_send`` (was ``"salem"``)
* ``peer_query`` (was ``"salem"``)
* ``peer_handshake`` (was ``"salem"``)
* ``peer_get_canonical_person`` (was ``"kal-le"``)
* ``peer_get_canonical_record`` (was ``"kal-le"``)
* ``peer_propose_canonical_record`` (was ``"kal-le"``)
* ``peer_propose_canonical_person`` (was ``"kal-le"``)
* ``resolve_or_propose_canonical_record`` (was ``"kal-le"``)
* ``resolve_or_propose_canonical_person`` (was ``"kal-le"``)
* ``peer_resolve_pending_item`` (was ``"salem"``)

These pins exercise:

1. **Defaults removed** — each helper raises ``TypeError`` when called
   without ``self_name=``, catching default re-introduction.
2. **Production-caller wiring** — the one production caller that had
   been relying on the default (the Daily Sync reply dispatcher's
   ``_resolve_pending_item_via_peer``) now plumbs ``self_instance``
   through. The pin asserts the helper forwards it into the peer call.

Per ``feedback_hardcoding_and_alfred_naming.md`` — single-instance
literal defaults are antipattern even on unreachable paths because
(a) they escape future rename-greps, (b) new instance addition
silently inherits one instance's routing, and (c) they weaken the
fail-loud-on-missing-name guarantee.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest


# ===========================================================================
# Layer 1 — TypeError pins: each helper requires self_name=
# ===========================================================================
#
# Python's required-keyword-arg enforcement is what catches future drift
# back toward defaulting. We don't need a live server — the call fails
# at signature-validation time before any network I/O is attempted.


@pytest.mark.asyncio
async def test_peer_send_requires_self_name() -> None:
    from alfred.transport.client import peer_send
    with pytest.raises(TypeError, match="self_name"):
        await peer_send(  # type: ignore[call-arg]
            "kal-le",
            kind="message",
            payload={"text": "x"},
        )


@pytest.mark.asyncio
async def test_peer_query_requires_self_name() -> None:
    from alfred.transport.client import peer_query
    with pytest.raises(TypeError, match="self_name"):
        await peer_query(  # type: ignore[call-arg]
            "kal-le",
            record_type="person",
            name="Andrew Newton",
        )


@pytest.mark.asyncio
async def test_peer_handshake_requires_self_name() -> None:
    from alfred.transport.client import peer_handshake
    with pytest.raises(TypeError, match="self_name"):
        await peer_handshake("kal-le")  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_peer_get_canonical_person_requires_self_name() -> None:
    from alfred.transport.client import peer_get_canonical_person
    with pytest.raises(TypeError, match="self_name"):
        await peer_get_canonical_person(  # type: ignore[call-arg]
            "salem", "Andrew Newton",
        )


@pytest.mark.asyncio
async def test_peer_get_canonical_record_requires_self_name() -> None:
    from alfred.transport.client import peer_get_canonical_record
    with pytest.raises(TypeError, match="self_name"):
        await peer_get_canonical_record(  # type: ignore[call-arg]
            "salem", "person", "Andrew Newton",
        )


@pytest.mark.asyncio
async def test_peer_propose_canonical_record_requires_self_name() -> None:
    from alfred.transport.client import peer_propose_canonical_record
    with pytest.raises(TypeError, match="self_name"):
        await peer_propose_canonical_record(  # type: ignore[call-arg]
            "salem", "person", "Andrew Newton",
        )


@pytest.mark.asyncio
async def test_peer_propose_canonical_person_requires_self_name() -> None:
    from alfred.transport.client import peer_propose_canonical_person
    with pytest.raises(TypeError, match="self_name"):
        await peer_propose_canonical_person(  # type: ignore[call-arg]
            "salem", "Andrew Newton",
        )


@pytest.mark.asyncio
async def test_resolve_or_propose_canonical_record_requires_self_name() -> None:
    from alfred.transport.client import resolve_or_propose_canonical_record
    with pytest.raises(TypeError, match="self_name"):
        await resolve_or_propose_canonical_record(  # type: ignore[call-arg]
            "salem", "person", "Andrew Newton",
        )


@pytest.mark.asyncio
async def test_resolve_or_propose_canonical_person_requires_self_name() -> None:
    from alfred.transport.client import resolve_or_propose_canonical_person
    with pytest.raises(TypeError, match="self_name"):
        await resolve_or_propose_canonical_person(  # type: ignore[call-arg]
            "salem", "Andrew Newton",
        )


@pytest.mark.asyncio
async def test_peer_resolve_pending_item_requires_self_name() -> None:
    from alfred.transport.client import peer_resolve_pending_item
    with pytest.raises(TypeError, match="self_name"):
        await peer_resolve_pending_item(  # type: ignore[call-arg]
            "kal-le",
            item_id="item-42",
            resolution="noted",
        )


# ===========================================================================
# Layer 2 — Production caller wiring: reply_dispatch threads self_instance
# ===========================================================================
#
# Before this sweep, ``_resolve_pending_item_via_peer`` omitted
# ``self_name`` on its ``peer_resolve_pending_item`` call. The helper
# silently used the ``"salem"`` default — happens to be correct on
# Salem (the only sender today), but the silent reliance was a real
# bug surfaced by the sweep, not just antipattern cleanup. The fix
# adds ``self_instance`` as a required parameter to the helper and
# threads it into the peer call.


def test_resolve_pending_item_via_peer_requires_self_instance() -> None:
    """The internal helper must reject calls without ``self_instance=``.

    Mirrors the public-API pattern: the kwarg is required, the helper
    can't fall back silently.
    """
    from alfred.daily_sync.reply_dispatch import _resolve_pending_item_via_peer

    with pytest.raises(TypeError, match="self_instance"):
        _resolve_pending_item_via_peer(  # type: ignore[call-arg]
            item_id="item-42",
            resolution_id="noted",
            peer_name="kal-le",
        )


def test_resolve_pending_item_via_peer_forwards_self_instance() -> None:
    """``self_instance`` plumbed into the ``peer_resolve_pending_item`` call.

    Captures the kwargs the helper passes to ``peer_resolve_pending_item``
    so the regression pin catches a future code change that drops the
    ``self_name`` forwarding (which would re-introduce the
    silent-default reliance the sweep just eliminated).
    """
    from alfred.daily_sync import reply_dispatch as rd

    captured_kwargs: dict[str, Any] = {}

    async def _fake_resolve(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        captured_kwargs.update(kwargs)
        return {"executed": True, "summary": "noted by peer"}

    raw_config = {
        "vault": {"path": "/tmp/test-vault"},
        "transport": {
            "auth": {
                "tokens": {
                    "local": {"token": "DUMMY_LOCAL_TEST_TOKEN_PLACEHOLDER"},
                },
            },
            "peers": {
                "kal-le": {
                    "base_url": "http://127.0.0.1:8892",
                    "token": "DUMMY_PEER_TEST_TOKEN_PLACEHOLDER",
                },
            },
        },
    }

    # ``_run_coro_sync`` actually awaits the coroutine; we patch it to
    # do exactly that so ``_fake_resolve`` populates ``captured_kwargs``
    # before returning the fake response dict.
    #
    # Use ``asyncio.run`` (creates + tears down its own loop) rather than
    # ``asyncio.get_event_loop().run_until_complete``. In a full-suite run,
    # prior pytest-asyncio tests close the global event loop, so
    # ``get_event_loop()`` returns a CLOSED loop and ``run_until_complete``
    # raises ``RuntimeError: Event loop is closed`` — an isolated-pass /
    # full-suite-fail ordering pollution. ``asyncio.run`` is immune because
    # it never touches the contaminated global loop.
    import asyncio

    def _await_inline(coro: Any) -> dict[str, Any]:
        return asyncio.run(coro)

    with patch(
        "alfred.transport.client.peer_resolve_pending_item",
        new=_fake_resolve,
    ), patch.object(rd, "_run_coro_sync", side_effect=_await_inline):
        result = rd._resolve_pending_item_via_peer(
            item_id="item-42",
            resolution_id="noted",
            peer_name="kal-le",
            self_instance="salem",
            raw_config=raw_config,
        )

    # The helper appended "(via kal-le)" suffix per the dispatcher's
    # contract.
    assert "noted by peer" in result
    assert "kal-le" in result

    # The load-bearing assertion: self_name was forwarded.
    assert captured_kwargs.get("self_name") == "salem", (
        f"Expected self_name='salem' threaded through; got "
        f"{captured_kwargs.get('self_name')!r}. The sweep ensures the "
        f"helper plumbs the running instance's identity rather than "
        f"relying on the now-removed default."
    )


# ===========================================================================
# Layer 3 — Test-fixture sanity: explicit self_name still works
# ===========================================================================
#
# Yesterday's 5b ship updated tests in test_peer_client.py to pass
# self_name explicitly; we re-import-pin those tests' patterns to
# confirm the sweep doesn't break the existing fixture shape.


@pytest.mark.asyncio
async def test_peer_send_with_explicit_self_name_typechecks() -> None:
    """Sanity: explicit ``self_name=`` still type-checks and reaches the
    network path (we don't run a server here; we just confirm the
    signature accepts the kwarg without a TypeError).

    This is the complement to the Layer-1 TypeError tests — those
    catch the regression-direction (default reintroduced), this catches
    the over-correction-direction (kwarg renamed or removed).
    """
    from alfred.transport.client import peer_send

    # We deliberately call with an unreachable URL — the test asserts
    # the helper accepts the kwargs and proceeds past signature
    # validation. Any subsequent error is a transport-layer concern,
    # not the contract we're pinning.
    from alfred.transport.config import (
        AuthConfig,
        PeerEntry,
        SchedulerConfig,
        ServerConfig,
        StateConfig,
        TransportConfig,
    )
    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={}),
        state=StateConfig(),
        peers={
            "kal-le": PeerEntry(
                base_url="http://127.0.0.1:1",  # RFC 1149 — unreachable
                token="DUMMY_PEER_TEST_TOKEN_PLACEHOLDER",
            ),
        },
    )
    # Expect a transport-layer error (ServerDown), NOT a TypeError.
    from alfred.transport.exceptions import TransportError
    with pytest.raises(TransportError):
        await peer_send(
            "kal-le",
            kind="message",
            payload={"text": "x"},
            config=config,
            self_name="salem",  # test-fixture-only — production paths plumb instance name
        )
