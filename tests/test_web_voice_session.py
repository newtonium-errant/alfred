"""Unit tests for ``alfred.web.voice_session`` — the VoiceSessionManager.

UNCONDITIONAL (no aiortc — regression pins). A ``FakePC`` factory + an
injected ``description_factory`` (the only aiortc-typed construction on the
negotiate path) + an injected monotonic ``clock`` let these exercise the
cap / in-flight reservation / same-user replacement / negotiation-timeout /
reaper / close-reason logic with ZERO aiortc installed. The advertised_ip
SDP-rewrite pure function is tested here too.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import structlog

from alfred.web.config import VoiceIceConfig, WebVoiceConfig
from alfred.web.identity import WebIdentity
from alfred.web.voice_session import (
    NegotiationFailed,
    TooManySessions,
    VoiceOfferTimeout,
    VoiceSessionManager,
    rewrite_answer_sdp_advertised_ip,
)

# A minimal answer SDP the FakePC "gathers".
_ANSWER_SDP = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
_OFFER_SDP = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


def _identity(name: str, chat_id: int) -> WebIdentity:
    return WebIdentity(user=name, role="owner", synthetic_chat_id=chat_id)


def _desc_factory(sdp: str, kind: str):
    """Duck-typed RTCSessionDescription stand-in (no aiortc)."""
    return SimpleNamespace(sdp=sdp, type=kind)


class FakePC:
    """A duck-typed RTCPeerConnection for aiortc-free manager tests."""

    def __init__(self) -> None:
        self.connectionState = "new"
        self._handlers: dict[str, object] = {}
        self.localDescription = SimpleNamespace(sdp=_ANSWER_SDP, type="answer")
        self.close_calls = 0
        self.added_tracks: list[object] = []

    def on(self, event: str):
        def _register(fn):
            self._handlers[event] = fn
            return fn
        return _register

    def addTrack(self, track) -> None:  # pragma: no cover - not hit w/o media
        self.added_tracks.append(track)

    async def setRemoteDescription(self, desc) -> None:
        self.remote = desc

    async def createAnswer(self):
        return SimpleNamespace(sdp=_ANSWER_SDP, type="answer")

    async def setLocalDescription(self, answer) -> None:
        self.localDescription = SimpleNamespace(sdp=_ANSWER_SDP, type="answer")

    async def close(self) -> None:
        self.close_calls += 1
        self.connectionState = "closed"

    async def fire(self, event: str) -> None:
        """Test helper — invoke a registered event handler."""
        handler = self._handlers.get(event)
        if handler is None:
            return
        result = handler()
        if asyncio.iscoroutine(result):
            await result


def _voice_config(**overrides) -> WebVoiceConfig:
    base = dict(
        enabled=True,
        max_sessions=2,
        pipeline="echo",
        offer_timeout_seconds=10,
        connect_deadline_seconds=30,
        idle_timeout_seconds=120,
        max_session_seconds=1800,
        reaper_interval_seconds=0,  # reaper OFF by default in unit tests
        ice=VoiceIceConfig(),
    )
    base.update(overrides)
    return WebVoiceConfig(**base)


def _manager(pc_factory=None, clock=None, **cfg) -> VoiceSessionManager:
    return VoiceSessionManager(
        _voice_config(**cfg),
        pc_factory=pc_factory or (lambda: FakePC()),
        description_factory=_desc_factory,
        clock=clock or (lambda: 1000.0),
    )


# ---------------------------------------------------------------------------
# Happy path + cap
# ---------------------------------------------------------------------------


async def test_open_session_registers_and_returns_answer() -> None:
    mgr = _manager()
    vid, answer = await mgr.open_session(_identity("andrew", 1), _OFFER_SDP)
    assert len(vid) == 32  # uuid4().hex
    assert answer == _ANSWER_SDP
    assert mgr.active_count() == 1
    assert mgr.sessions_for(1)[0].voice_session_id == vid


async def test_cap_raises_too_many_sessions() -> None:
    mgr = _manager(max_sessions=1)
    await mgr.open_session(_identity("a", 1), _OFFER_SDP)
    with pytest.raises(TooManySessions) as exc:
        await mgr.open_session(_identity("b", 2), _OFFER_SDP)
    assert exc.value.max_sessions == 1
    assert mgr.active_count() == 1


async def test_in_flight_reservation_counts_toward_cap() -> None:
    """A slot reserved by an IN-FLIGHT negotiation must block a second offer
    even before the first session is registered (security W2)."""
    gate = asyncio.Event()

    class GatedPC(FakePC):
        async def setLocalDescription(self, answer) -> None:
            await gate.wait()
            await super().setLocalDescription(answer)

    mgr = _manager(pc_factory=lambda: GatedPC(), max_sessions=1)
    t1 = asyncio.create_task(mgr.open_session(_identity("a", 1), _OFFER_SDP))
    await asyncio.sleep(0.02)  # let t1 reserve the slot + block in negotiation
    # No session registered yet, but the in-flight reservation holds the slot.
    assert mgr.active_count() == 0
    with pytest.raises(TooManySessions):
        await mgr.open_session(_identity("b", 2), _OFFER_SDP)
    gate.set()
    vid, _ = await t1
    assert mgr.active_count() == 1
    assert vid


# ---------------------------------------------------------------------------
# Same-user replacement
# ---------------------------------------------------------------------------


async def test_same_user_reoffer_replaces_previous() -> None:
    made: list[FakePC] = []

    def factory() -> FakePC:
        pc = FakePC()
        made.append(pc)
        return pc

    mgr = _manager(pc_factory=factory, max_sessions=1)
    with structlog.testing.capture_logs() as cap:
        vid1, _ = await mgr.open_session(_identity("andrew", 7), _OFFER_SDP)
        vid2, _ = await mgr.open_session(_identity("andrew", 7), _OFFER_SDP)
    assert vid1 != vid2
    assert mgr.active_count() == 1  # not 2 — the reload did NOT hit the cap
    assert mgr.sessions_for(7)[0].voice_session_id == vid2
    assert made[0].close_calls == 1  # old pc torn down
    replaced = [c for c in cap if c.get("event") == "web.voice.session.replaced"]
    assert len(replaced) == 1
    assert replaced[0]["voice_session_id"] == vid1


# ---------------------------------------------------------------------------
# Negotiation timeout → slot released
# ---------------------------------------------------------------------------


async def test_negotiation_timeout_releases_slot() -> None:
    class HangingPC(FakePC):
        async def setLocalDescription(self, answer) -> None:
            await asyncio.sleep(3600)  # never completes

    mgr = _manager(pc_factory=lambda: HangingPC(), offer_timeout_seconds=0.05)
    with structlog.testing.capture_logs() as cap:
        with pytest.raises(VoiceOfferTimeout):
            await mgr.open_session(_identity("a", 1), _OFFER_SDP)
    assert mgr.active_count() == 0
    assert mgr._in_flight == 0  # slot released
    fails = [c for c in cap if c.get("event") == "web.voice.session.fail"]
    assert any(c.get("reason") == "offer_timeout" for c in fails)


async def test_negotiation_error_raises_and_releases_slot() -> None:
    class BoomPC(FakePC):
        async def createAnswer(self):
            raise RuntimeError("boom")

    boom = BoomPC()
    mgr = _manager(pc_factory=lambda: boom)
    with pytest.raises(NegotiationFailed):
        await mgr.open_session(_identity("a", 1), _OFFER_SDP)
    assert mgr.active_count() == 0
    assert mgr._in_flight == 0
    assert boom.close_calls == 1  # failed pc torn down


# ---------------------------------------------------------------------------
# Close — reasons, owner-binding, idempotency, pc.close once
# ---------------------------------------------------------------------------


async def test_close_owned_true_for_owner() -> None:
    pc = FakePC()
    mgr = _manager(pc_factory=lambda: pc)
    vid, _ = await mgr.open_session(_identity("andrew", 5), _OFFER_SDP)
    assert await mgr.close_owned(vid, 5, reason="client_close") is True
    assert mgr.active_count() == 0
    assert pc.close_calls == 1


async def test_close_owned_false_for_wrong_user_no_leak() -> None:
    mgr = _manager()
    vid, _ = await mgr.open_session(_identity("andrew", 5), _OFFER_SDP)
    with structlog.testing.capture_logs() as cap:
        # id exists but caller (99) is not the owner (5) → not_found, logged.
        assert await mgr.close_owned(vid, 99, reason="client_close") is False
    assert mgr.active_count() == 1  # still live — a stranger cannot close it
    wrong = [c for c in cap if c.get("event") == "web.voice.close_wrong_user"]
    assert len(wrong) == 1


async def test_close_owned_false_for_unknown_id() -> None:
    mgr = _manager()
    assert await mgr.close_owned("deadbeef", 5, reason="client_close") is False


async def test_close_is_idempotent_pc_closed_once() -> None:
    pc = FakePC()
    mgr = _manager(pc_factory=lambda: pc)
    vid, _ = await mgr.open_session(_identity("a", 1), _OFFER_SDP)
    assert await mgr.close(vid, reason="client_close") is True
    assert await mgr.close(vid, reason="client_close") is False  # already gone
    assert pc.close_calls == 1  # NOT re-closed


async def test_close_all_drains_every_session() -> None:
    mgr = _manager(max_sessions=5)
    await mgr.open_session(_identity("a", 1), _OFFER_SDP)
    await mgr.open_session(_identity("b", 2), _OFFER_SDP)
    await mgr.close_all(reason="daemon_shutdown")
    assert mgr.active_count() == 0


async def test_close_all_empty_emits_signal() -> None:
    mgr = _manager()
    with structlog.testing.capture_logs() as cap:
        await mgr.close_all(reason="daemon_shutdown")
    empty = [c for c in cap if c.get("event") == "web.voice.close_all_empty"]
    assert len(empty) == 1  # intentionally-left-blank


async def test_pc_close_error_swallowed() -> None:
    class BadClosePC(FakePC):
        async def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("close boom")

    pc = BadClosePC()
    mgr = _manager(pc_factory=lambda: pc)
    vid, _ = await mgr.open_session(_identity("a", 1), _OFFER_SDP)
    with structlog.testing.capture_logs() as cap:
        assert await mgr.close(vid, reason="client_close") is True  # survives
    assert mgr.active_count() == 0
    errs = [c for c in cap if c.get("event") == "web.voice.pc_close_error"]
    assert len(errs) == 1


# ---------------------------------------------------------------------------
# connectionstatechange handler
# ---------------------------------------------------------------------------


async def test_connectionstatechange_connected_sets_state() -> None:
    pc = FakePC()
    mgr = _manager(pc_factory=lambda: pc)
    vid, _ = await mgr.open_session(_identity("a", 1), _OFFER_SDP)
    pc.connectionState = "connected"
    with structlog.testing.capture_logs() as cap:
        await pc.fire("connectionstatechange")
    s = mgr.sessions_for(1)[0]
    assert s.connection_state == "connected"
    assert s.connected_once is True
    assert any(c.get("event") == "web.voice.session.connected" for c in cap)


async def test_connectionstatechange_failed_closes_session() -> None:
    pc = FakePC()
    mgr = _manager(pc_factory=lambda: pc)
    vid, _ = await mgr.open_session(_identity("a", 1), _OFFER_SDP)
    pc.connectionState = "failed"
    await pc.fire("connectionstatechange")
    await asyncio.sleep(0.01)  # let the detached close task run
    assert mgr.active_count() == 0
    assert pc.close_calls == 1


# ---------------------------------------------------------------------------
# Reaper — timeout classes + poisoned-iteration survival
# ---------------------------------------------------------------------------


async def test_reaper_closes_absolute_timeout() -> None:
    now = [1000.0]
    mgr = _manager(clock=lambda: now[0], max_session_seconds=1800)
    vid, _ = await mgr.open_session(_identity("a", 1), _OFFER_SDP)
    now[0] = 1000.0 + 1801  # past absolute
    with structlog.testing.capture_logs() as cap:
        await mgr._reap_once()
    assert mgr.active_count() == 0
    closes = [c for c in cap if c.get("event") == "web.voice.session.close"]
    assert closes and closes[0]["reason"] == "absolute_timeout"


async def test_reaper_closes_connect_deadline() -> None:
    now = [1000.0]
    mgr = _manager(
        clock=lambda: now[0], connect_deadline_seconds=30, max_session_seconds=1800,
    )
    vid, _ = await mgr.open_session(_identity("a", 1), _OFFER_SDP)
    # never connected (connected_once stays False); past the connect deadline.
    now[0] = 1000.0 + 31
    with structlog.testing.capture_logs() as cap:
        await mgr._reap_once()
    assert mgr.active_count() == 0
    closes = [c for c in cap if c.get("event") == "web.voice.session.close"]
    assert closes[0]["reason"] == "connect_deadline"


async def test_reaper_closes_idle_after_connected() -> None:
    now = [1000.0]
    mgr = _manager(
        clock=lambda: now[0], idle_timeout_seconds=120, max_session_seconds=1800,
    )
    vid, _ = await mgr.open_session(_identity("a", 1), _OFFER_SDP)
    s = mgr.sessions_for(1)[0]
    s.connected_once = True
    s.connection_state = "disconnected"
    s.last_state_change = now[0]
    now[0] = 1000.0 + 121  # idle past the timeout, but under absolute
    with structlog.testing.capture_logs() as cap:
        await mgr._reap_once()
    assert mgr.active_count() == 0
    closes = [c for c in cap if c.get("event") == "web.voice.session.close"]
    assert closes[0]["reason"] == "idle_timeout"


async def test_reaper_keeps_healthy_connected_session() -> None:
    now = [1000.0]
    mgr = _manager(clock=lambda: now[0])
    vid, _ = await mgr.open_session(_identity("a", 1), _OFFER_SDP)
    s = mgr.sessions_for(1)[0]
    s.connected_once = True
    s.connection_state = "connected"
    s.last_state_change = now[0]
    now[0] = 1000.0 + 200  # past idle window but still connected → keep
    await mgr._reap_once()
    assert mgr.active_count() == 1


async def test_reaper_loop_survives_poisoned_iteration(monkeypatch) -> None:
    mgr = _manager(reaper_interval_seconds=1)
    mgr.reaper_interval = 0.01  # spin fast for the test
    calls: list[int] = []

    async def flaky_reap() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("poisoned sweep")

    monkeypatch.setattr(mgr, "_reap_once", flaky_reap)
    with structlog.testing.capture_logs() as cap:
        task = asyncio.ensure_future(mgr._reaper_loop())
        await asyncio.sleep(0.05)  # allow several iterations
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert len(calls) >= 2  # survived the poison and kept sweeping
    errs = [c for c in cap if c.get("event") == "web.voice.reaper_error"]
    assert len(errs) >= 1


async def test_reaper_lazy_starts_on_first_open() -> None:
    mgr = _manager(reaper_interval_seconds=30)
    assert mgr.reaper_alive() is False
    with structlog.testing.capture_logs() as cap:
        await mgr.open_session(_identity("a", 1), _OFFER_SDP)
    assert mgr.reaper_alive() is True
    assert any(c.get("event") == "web.voice.reaper_started" for c in cap)
    mgr.stop_reaper()
    assert mgr.reaper_alive() is False


async def test_aclose_awaits_reaper_and_drains_bg_tasks() -> None:
    """Shutdown drain: aclose cancels+AWAITS the reaper and awaits the
    detached connection-state close tasks, so the loop never tears down with
    a pending task (code-reviewer NOTE 1)."""
    pc = FakePC()
    mgr = _manager(pc_factory=lambda: pc, reaper_interval_seconds=30)
    await mgr.open_session(_identity("a", 1), _OFFER_SDP)
    assert mgr.reaper_alive() is True
    # A failed-state change spawns a detached close task into _bg_tasks; it is
    # still pending immediately after fire() (no await point yielded to it).
    pc.connectionState = "failed"
    await pc.fire("connectionstatechange")
    bg = list(mgr._bg_tasks)
    assert bg, "expected a detached close task from the failed-state handler"
    await mgr.aclose()
    assert all(t.done() for t in bg)  # aclose awaited them
    assert mgr.reaper_alive() is False  # reaper cancelled + awaited


# ---------------------------------------------------------------------------
# advertised_ip SDP rewrite — pure function
# ---------------------------------------------------------------------------


def test_rewrite_advertised_ip_rewrites_host_candidates() -> None:
    sdp = (
        "v=0\r\n"
        "a=candidate:1 1 udp 2130706431 192.168.1.5 54321 typ host\r\n"
        "a=candidate:2 1 udp 1694498815 1.2.3.4 54321 typ srflx "
        "raddr 192.168.1.5 rport 54321\r\n"
    )
    out = rewrite_answer_sdp_advertised_ip(sdp, "203.0.113.9")
    assert "203.0.113.9 54321 typ host" in out
    # srflx candidate (already public/reflexive) is left untouched.
    assert "1.2.3.4 54321 typ srflx" in out
    # CRLF endings preserved.
    assert out.split("\r\n")[1].endswith("typ host")


def test_rewrite_advertised_ip_noop_when_empty() -> None:
    sdp = "a=candidate:1 1 udp 2130706431 192.168.1.5 54321 typ host\r\n"
    assert rewrite_answer_sdp_advertised_ip(sdp, "") == sdp


def test_rewrite_advertised_ip_multiple_host_candidates() -> None:
    sdp = (
        "a=candidate:1 1 udp 2130706431 192.168.1.5 5 typ host\r\n"
        "a=candidate:2 2 udp 2130706430 10.0.0.9 6 typ host\r\n"
    )
    out = rewrite_answer_sdp_advertised_ip(sdp, "203.0.113.9")
    assert "203.0.113.9 5 typ host" in out
    assert "203.0.113.9 6 typ host" in out
    assert "192.168.1.5" not in out and "10.0.0.9" not in out
