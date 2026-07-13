"""Barge telemetry sink — structural privacy + fire-and-forget isolation.

Sink-LEVEL pins for ``alfred.web.voice_barge_telemetry.VoiceBargeTelemetry``,
mirroring the endpoint-hold telemetry sink tests (``test_web_endpoint_hold.py``).
The DRIVER-side feature derivation + emission-point wiring is pinned in
``test_web_voice_turns.py`` (it needs the VoiceTurnDriver + barge settings).

The load-bearing contract here is PRIVACY: the sink is features-only. A caller
that hands ``emit`` a raw ``text`` / ``norm`` / ``transcript`` field must NEVER
leak it — the ``_BARGE_ALLOWED_FIELDS`` allowlist drops it structurally.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace


async def _drain() -> None:
    from alfred.web.voice_barge_telemetry import _BARGE_TASKS
    for _ in range(50):
        pending = [t for t in list(_BARGE_TASKS) if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)


def test_barge_sink_drops_raw_text_fields(tmp_path: Path) -> None:
    """THE privacy pin: even if a careless caller passes raw/normalized text
    fields, the allowlist drops them before write — only features survive."""
    from alfred.web.voice_barge_telemetry import VoiceBargeTelemetry

    async def _run():
        tel = VoiceBargeTelemetry(
            corpus_dir=str(tmp_path), web_user="u",
            voice_session_id="v", instance_name="Salem")
        tel.emit({
            "decision": "suppress", "reason": "backchannel",
            "starts_with_backchannel": True, "word_count": 3,
            # These MUST be dropped — the utterance must never reach disk.
            "text": "SECRET RAW UTTERANCE", "norm": "secret normalized",
            "transcript": "SECRET TRANSCRIPT",
        })
        await _drain()

    asyncio.run(_run())
    raw = (tmp_path / "events.jsonl").read_text()
    assert "SECRET" not in raw and "secret" not in raw   # no raw/normalized text
    rec = json.loads(raw.splitlines()[-1])
    assert rec["event_family"] == "barge"
    assert rec["decision"] == "suppress" and rec["reason"] == "backchannel"
    assert rec["starts_with_backchannel"] is True and rec["word_count"] == 3
    assert "text" not in rec and "norm" not in rec and "transcript" not in rec
    # Session-identifying envelope fields ARE written (per-user calibration key).
    assert rec["web_user"] == "u" and rec["voice_session_id"] == "v"
    assert rec["instance"] == "Salem"


def test_barge_sink_writes_only_allowlisted_features(tmp_path: Path) -> None:
    """A record carrying the full feature set round-trips exactly — and nothing
    outside the allowlist is admitted even under many junk keys."""
    from alfred.web.voice_barge_telemetry import VoiceBargeTelemetry

    async def _run():
        tel = VoiceBargeTelemetry(
            corpus_dir=str(tmp_path), web_user="andrew", voice_session_id="v9")
        tel.emit({
            "decision": "barge", "reason": "confirmed",
            "ms_into_speaking": 1500, "echo_score": 0.12,
            "word_count": 5, "char_count": 31,
            "starts_with_backchannel": False, "is_backchannel_exact": False,
            "matched_interrupt_phrase": False,
            "cfg_too_early_ms": 700, "cfg_echo_threshold": 0.8,
            "utterance_id": "u1", "turn_id": "t1",
            "junk": "DROP", "raw_tail": "DROP", "utterance": "DROP",
        })
        await _drain()

    asyncio.run(_run())
    rec = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    assert rec["decision"] == "barge" and rec["reason"] == "confirmed"
    assert rec["ms_into_speaking"] == 1500 and rec["echo_score"] == 0.12
    assert rec["matched_interrupt_phrase"] is False
    assert rec["cfg_too_early_ms"] == 700 and rec["cfg_echo_threshold"] == 0.8
    for junk in ("junk", "raw_tail", "utterance"):
        assert junk not in rec
    assert "DROP" not in (tmp_path / "events.jsonl").read_text()


def test_barge_sink_emit_never_raises_on_unwritable_dir(tmp_path: Path) -> None:
    """Fire-and-forget isolation: a corpus dir that can never be created (its
    parent is a FILE) makes the write fail — but ``emit`` must NOT raise into
    the live turn, and the failure is swallowed inside the task."""
    from alfred.web.voice_barge_telemetry import VoiceBargeTelemetry

    blocker = tmp_path / "blocker"
    blocker.write_text("x")            # a FILE where a dir would need to be
    bad_dir = blocker / "sub"          # mkdir(parents=True) on this must fail

    async def _run():
        tel = VoiceBargeTelemetry(
            corpus_dir=str(bad_dir), web_user="u", voice_session_id="v")
        tel.emit({"decision": "barge", "reason": "confirmed"})   # must NOT raise
        await _drain()

    asyncio.run(_run())                # returns cleanly ⇒ isolation held
    assert not (bad_dir / "events.jsonl").exists()


def test_build_barge_telemetry_gating(tmp_path: Path) -> None:
    """The per-request wiring seam: a sink is built ONLY when barge is enabled
    (settings not None) AND a corpus dir resolved at mount. Otherwise None ⇒ the
    driver's emit seams are no-ops."""
    from alfred.web import routes_voice as rv
    from alfred.web.keys import KEY_WEB_TALKER_CONFIG
    from alfred.web.voice_barge_telemetry import VoiceBargeTelemetry

    ident = SimpleNamespace(user="andrew")
    # Barge disabled (settings None) → no sink regardless of dir.
    app = {rv._KEY_WEB_BARGE_TELEMETRY_DIR: str(tmp_path)}
    assert rv._build_barge_telemetry(app, ident, "v1", None) is None
    # Barge enabled but no dir → no sink.
    assert rv._build_barge_telemetry(
        {rv._KEY_WEB_BARGE_TELEMETRY_DIR: ""}, ident, "v1", object()) is None
    # Barge enabled + dir → a sink wired to that dir, web_user, instance.
    app = {
        rv._KEY_WEB_BARGE_TELEMETRY_DIR: str(tmp_path),
        KEY_WEB_TALKER_CONFIG: SimpleNamespace(
            instance=SimpleNamespace(name="Salem")),
    }
    tel = rv._build_barge_telemetry(app, ident, "v7", object())
    assert isinstance(tel, VoiceBargeTelemetry)
    assert tel._web_user == "andrew" and tel._vid == "v7"
    assert tel._instance == "Salem" and str(tel._dir) == str(tmp_path)
