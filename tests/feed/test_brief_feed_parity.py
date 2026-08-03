"""Feed Phase A — the load-bearing BRIEF parity gate.

A full ``generate_brief`` run must produce BYTE-IDENTICAL output whether the feed
is enabled or disabled — the SectionResult seam unwraps to the same (name,
markdown) tuples the renderer always took, and the feed emission runs after the
render off the same section objects, so it can never perturb the markdown.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

import socket
from datetime import date
from pathlib import Path

import frontmatter  # type: ignore[import-untyped]
import pytest

from alfred.brief.config import BriefConfig
from alfred.brief.state import StateManager
from alfred.feed import FeedConfig, FeedStore

# Hosts a test may legitimately connect to: itself. The voice/aiortc tests bind
# and dial real LOCAL sockets, so a blanket socket ban is wrong — only
# NON-LOOPBACK egress is a defect.
_LOOPBACK_PREFIXES = ("127.", "::1", "localhost", "0.0.0.0", "::")


def _is_loopback(host: object) -> bool:
    text = str(host)
    return text == "" or text.startswith(_LOOPBACK_PREFIXES)


@pytest.fixture
def recorded_egress(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Refuse every non-loopback connect and RECORD what was attempted.

    Returns the (initially empty) list of hosts; assert on it after the run.

    Asserting on the RECORD rather than letting the refusal propagate is the
    load-bearing part. ``build_narration`` wraps its weather fetch in a bare
    ``except Exception``, so a raised refusal is swallowed exactly like the
    HTTP 400 was — a guard that only raised would report a clean pass while
    the leak carried on. That swallow is why this went unnoticed in the first
    place, so the pin must not depend on it.
    """
    attempted: list[str] = []
    real_connect = socket.socket.connect

    def guarded(self, address, *args, **kwargs):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) else address
        if not _is_loopback(host):
            attempted.append(str(host))
            raise OSError(f"blocked outbound connect to {host} (test egress guard)")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    return attempted


def _write_bit(vault: Path) -> None:
    run = vault / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "Alfred BIT 2026-07-30.md").write_text(
        "---\ntype: run\n---\n\n## Summary\n[OK] curator  (1 ms)\n[WARN] surveyor — ollama 404\n## Detail\n",
        encoding="utf-8",
    )


def _write_task_due_today(vault: Path) -> None:
    (vault / "task").mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    (vault / "task" / "Pay Steph.md").write_text(
        f"---\ntype: task\nstatus: todo\nname: Pay Steph\ndue: {today}\n---\n\n# Pay Steph\n",
        encoding="utf-8",
    )


def _config(tmp_path: Path, *, feed_enabled: bool) -> BriefConfig:
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    _write_bit(vault)
    _write_task_due_today(vault)  # → a T1 slot_suggestion item when the feed is on
    cfg = BriefConfig(vault_path=str(vault), instance_name="salem")
    cfg.state.path = str(tmp_path / "data" / "brief_state.json")
    cfg.primary_telegram_user_id = None  # no push
    cfg.feed = FeedConfig(enabled=feed_enabled, store_path=str(tmp_path / "data" / "feed.jsonl"))
    return cfg


def _patch_weather(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub BOTH weather paths. There are two, and they resolve differently.

    ``daemon.fetch_and_format`` is bound at MODULE import (``brief/daemon.py``
    ``from .weather import fetch_and_format``), so patching the daemon's own
    attribute catches the markdown weather section.

    ``narration`` reaches weather by a FUNCTION-LOCAL import instead
    (``brief/narration.py`` ``from .weather import fetch_metars``, inside
    ``build_narration``), which re-reads the attribute off ``alfred.brief.weather``
    at CALL time — so the daemon patch cannot reach it. It was therefore making a
    REAL request to aviationweather.gov on every suite run, twice per parity test
    (once per generate_brief), with an empty ``ids=`` because the fixture config
    has no stations. Measured before the fix: 2 DNS lookups + 4 TCP connects, and
    14.19s of wall clock against 0.64s once egress is refused.

    Nothing failed loudly because ``build_narration`` wraps the fetch in a bare
    ``except Exception`` and degrades to an omitted weather slide — so the leak
    was invisible while still injecting live-network latency between the two runs
    this test requires to be comparable. Patch the SOURCE module attribute, which
    is what the late import binds against.
    """
    async def _fake_weather(_weather_config):
        return "*Weather: fixed for the test.*"

    async def _fake_metars(_weather_config):
        return []

    monkeypatch.setattr("alfred.brief.daemon.fetch_and_format", _fake_weather)
    # String target + default raising=True: if `fetch_metars` is ever renamed or
    # moved, this raises AttributeError instead of silently becoming a no-op and
    # letting the live call back in.
    monkeypatch.setattr("alfred.brief.weather.fetch_metars", _fake_metars)


async def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, feed_enabled: bool) -> str:
    from alfred.brief.daemon import generate_brief

    _patch_weather(monkeypatch)
    cfg = _config(tmp_path, feed_enabled=feed_enabled)
    rel = await generate_brief(cfg, StateManager(cfg.state.path))
    return (Path(cfg.vault_path) / rel).read_text(encoding="utf-8")


async def test_brief_output_byte_identical_feed_on_vs_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    on = await _run(tmp_path / "on", monkeypatch, feed_enabled=True)
    off = await _run(tmp_path / "off", monkeypatch, feed_enabled=False)

    on_doc, off_doc = frontmatter.loads(on), frontmatter.loads(off)
    # The rendered markdown BODY is byte-identical (the render is what the
    # parity gate protects).
    assert on_doc.content == off_doc.content
    # Frontmatter is identical too, modulo the wall-clock ``started`` stamp
    # (per-run, orthogonal to the feed — same class as A2's fired_at).
    on_fm, off_fm = dict(on_doc.metadata), dict(off_doc.metadata)
    on_fm.pop("started", None)
    off_fm.pop("started", None)
    assert on_fm == off_fm

    # Non-vacuous: the feed actually ran when enabled (a health WARN item landed)
    # and did NOT when disabled (no store file).
    assert (tmp_path / "on" / "data" / "feed.jsonl").is_file()
    assert not (tmp_path / "off" / "data" / "feed.jsonl").exists()

    folded = FeedStore(tmp_path / "on" / "data" / "feed.jsonl").load()
    health = [it for it in folded.values() if it.kind == "health"]
    assert len(health) == 1
    assert health[0].id == "health:surveyor"
    assert health[0].state == "open"
    # All four converted sections fed the store — slot_suggestion present too.
    slots = [it for it in folded.values() if it.kind == "slot_suggestion"]
    assert any(it.id == "slot_suggestion:task:task/Pay Steph.md" for it in slots)


async def test_brief_generation_makes_no_outbound_network_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded_egress: list[str],
) -> None:
    """A full generate_brief must reach ZERO non-loopback hosts.

    The pin for the leak ``_patch_weather`` closes, and deliberately broader than
    that one fix: it guards the whole brief run, so a NEW unmocked egress path
    added anywhere under generate_brief fails here too, not just a regression on
    the weather stub.

    Live network in this test is not merely impure — it is the flake mechanism.
    The parity test above generates the brief TWICE and compares the results
    byte-for-byte, and the brief's content is derived from the wall clock and
    from ``date.today()``. Two live HTTP calls put ~14s of variable latency
    between those runs, widening the window for a date or hour boundary to fall
    between them and make two legitimately-generated briefs differ.
    """
    await _run(tmp_path, monkeypatch, feed_enabled=True)

    assert recorded_egress == [], (
        "generate_brief attempted outbound network connections to "
        f"{sorted(set(recorded_egress))} — every external fetch must be stubbed. "
        "Note the narration weather fetch resolves via a function-local import "
        "of alfred.brief.weather.fetch_metars, so patching the daemon's own "
        "fetch_and_format attribute does NOT cover it."
    )


async def test_egress_guard_itself_catches_a_real_connect(
    recorded_egress: list[str],
) -> None:
    """The guard is not vacuous — it records a genuine non-loopback attempt.

    Without this, a guard that silently stopped intercepting (a socket API
    change, a wrong patch target) would leave the pin above passing forever on
    an empty list. Uses a documentation-range IP (TEST-NET-3, RFC 5737) and
    never completes a connection: the guard refuses before the syscall.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError, match="test egress guard"):
            sock.connect(("203.0.113.1", 80))
    finally:
        sock.close()

    assert recorded_egress == ["203.0.113.1"]

    # Loopback still passes THROUGH the guard (the aiortc/voice tests need it),
    # so the guard is selective rather than a blanket socket ban. Connecting to a
    # closed loopback port raises ConnectionRefused from the OS — NOT our guard —
    # which is the proof that the call reached the real syscall.
    sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(OSError) as excinfo:
            sock2.connect(("127.0.0.1", 9))
        assert "test egress guard" not in str(excinfo.value)
    finally:
        sock2.close()
    assert recorded_egress == ["203.0.113.1"]  # loopback was not recorded
