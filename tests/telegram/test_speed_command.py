"""Tests for the ``/speed`` TTS preference command.

Covers:
    * Parse / validate helpers in :mod:`alfred.telegram.speed_pref`.
    * Range rejection for out-of-range values.
    * Report mode (no arg) — shows default when unset, value + history when set.
    * Set mode — persists to person-record frontmatter under
      ``preferences.voice.speeds.<instance>`` and appends a history entry.
    * Default-reset mode — writes 1.0 with ``by=reset`` history entry.
    * Per-(instance, user) scoping — Salem's speed doesn't affect STAY-C,
      Andrew's doesn't affect a second user.
    * /brief call path — reads the resolved speed and forwards it to
      ``tts.synthesize`` as ``voice_settings.speed``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import frontmatter
import httpx
import pytest

from alfred.telegram import bot, speed_pref, tts
from alfred.telegram.config import InstanceConfig, TtsConfig
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


# --- Fixture helpers -----------------------------------------------------


def _write_person_record(
    vault_path: Path,
    name: str = "Andrew Newton",
    preferences: dict | None = None,
) -> str:
    """Seed a minimal person record; return its vault-relative path."""
    person_dir = vault_path / "person"
    person_dir.mkdir(parents=True, exist_ok=True)
    fm = {
        "type": "person",
        "name": name,
        "created": "2026-04-17",
        "status": "active",
    }
    if preferences is not None:
        fm["preferences"] = preferences
    post = frontmatter.Post(f"# {name}\n", **fm)
    rel = f"person/{name}.md"
    (vault_path / rel).write_text(
        frontmatter.dumps(post) + "\n", encoding="utf-8",
    )
    return rel


def _load_person_prefs(vault_path: Path, rel: str) -> dict:
    """Return the ``preferences`` dict from a person record."""
    post = frontmatter.load(str(vault_path / rel))
    prefs = post.metadata.get("preferences") or {}
    return dict(prefs)


# --- Parse helpers -------------------------------------------------------


def test_parse_speed_no_arg_is_report() -> None:
    mode, value, note = speed_pref.parse_speed_command("/speed")
    assert mode == "report"
    assert value is None
    assert note == ""


def test_parse_speed_numeric_is_set() -> None:
    mode, value, note = speed_pref.parse_speed_command("/speed 1.2")
    assert mode == "set"
    assert value == 1.2
    assert note == ""


def test_parse_speed_numeric_with_note() -> None:
    mode, value, note = speed_pref.parse_speed_command("/speed 1.2 too slow at default")
    assert mode == "set"
    assert value == 1.2
    assert note == "too slow at default"


def test_parse_speed_default_is_reset() -> None:
    mode, value, note = speed_pref.parse_speed_command("/speed default")
    assert mode == "reset"
    assert value is None


def test_parse_speed_garbage_is_error() -> None:
    mode, value, note = speed_pref.parse_speed_command("/speed banana")
    assert mode == "error"
    assert value is None
    assert "banana" in note


def test_parse_tolerates_missing_slash() -> None:
    """Raw body works too (useful for inline-command parsing)."""
    mode, value, _ = speed_pref.parse_speed_command("1.1")
    assert mode == "set"
    assert value == 1.1


# --- Validate ------------------------------------------------------------


def test_validate_happy_path() -> None:
    assert speed_pref.validate_speed(1.0) == 1.0
    assert speed_pref.validate_speed(0.7) == 0.7
    assert speed_pref.validate_speed(1.2) == 1.2


def test_validate_rejects_above_range() -> None:
    with pytest.raises(speed_pref.SpeedValidationError, match="1.5"):
        speed_pref.validate_speed(1.5)


def test_validate_rejects_below_range() -> None:
    with pytest.raises(speed_pref.SpeedValidationError, match="0.5"):
        speed_pref.validate_speed(0.5)


# --- Resolve (read path) -------------------------------------------------


def test_resolve_default_when_no_person_record(tmp_path: Path) -> None:
    """Missing record → default 1.0, no exception."""
    assert speed_pref.resolve_tts_speed(
        tmp_path, "person/Nobody", "Salem",
    ) == speed_pref.SPEED_DEFAULT


def test_resolve_default_when_no_preferences_block(tmp_path: Path) -> None:
    _write_person_record(tmp_path)
    assert speed_pref.resolve_tts_speed(
        tmp_path, "person/Andrew Newton", "Salem",
    ) == speed_pref.SPEED_DEFAULT


def test_resolve_returns_stored_value(tmp_path: Path) -> None:
    _write_person_record(
        tmp_path,
        preferences={"voice": {"speeds": {"salem": 1.15}}},
    )
    assert speed_pref.resolve_tts_speed(
        tmp_path, "person/Andrew Newton", "Salem",
    ) == 1.15


def test_resolve_is_instance_scoped(tmp_path: Path) -> None:
    """STAY-C's stored speed does not leak into Salem's lookup.

    Normalisation produces ``stay-c`` (matches the transport peer-key
    convention: ``kal-le``, ``stay-c``). Matches
    :func:`bot._normalize_instance_name`.
    """
    _write_person_record(
        tmp_path,
        preferences={"voice": {"speeds": {"stay-c": 0.9, "salem": 1.2}}},
    )
    assert speed_pref.resolve_tts_speed(
        tmp_path, "person/Andrew Newton", "Salem",
    ) == 1.2
    assert speed_pref.resolve_tts_speed(
        tmp_path, "person/Andrew Newton", "STAY-C",
    ) == 0.9


# --- Set (write path) ----------------------------------------------------


def test_set_creates_preferences_block_from_scratch(tmp_path: Path) -> None:
    rel = _write_person_record(tmp_path)
    summary = speed_pref.set_tts_speed(
        tmp_path, "person/Andrew Newton", "Salem", 1.2,
    )
    assert summary["written"] is True
    assert summary["speed"] == 1.2

    prefs = _load_person_prefs(tmp_path, rel)
    assert prefs["voice"]["speeds"]["salem"] == 1.2
    assert len(prefs["voice"]["history"]) == 1
    entry = prefs["voice"]["history"][0]
    assert entry["instance"] == "salem"
    assert entry["value"] == 1.2
    assert entry["by"] == "slash_command"
    assert "note" not in entry  # no note supplied


def test_set_preserves_existing_preferences_fields(tmp_path: Path) -> None:
    """Must not nuke unrelated preferences.* keys."""
    rel = _write_person_record(
        tmp_path,
        preferences={
            "reminders": {"quiet_hours": "22:00-07:00"},
        },
    )
    speed_pref.set_tts_speed(
        tmp_path, "person/Andrew Newton", "Salem", 1.1,
    )
    prefs = _load_person_prefs(tmp_path, rel)
    assert prefs["reminders"]["quiet_hours"] == "22:00-07:00"
    assert prefs["voice"]["speeds"]["salem"] == 1.1


def test_set_appends_history_without_nuking_prior_entries(tmp_path: Path) -> None:
    """Two sets → two history entries, both preserved."""
    rel = _write_person_record(tmp_path)
    speed_pref.set_tts_speed(
        tmp_path, "person/Andrew Newton", "Salem", 1.0,
    )
    speed_pref.set_tts_speed(
        tmp_path, "person/Andrew Newton", "Salem", 1.2,
        note="faster now",
    )
    prefs = _load_person_prefs(tmp_path, rel)
    history = prefs["voice"]["history"]
    assert len(history) == 2
    assert history[0]["value"] == 1.0
    assert history[1]["value"] == 1.2
    assert history[1]["note"] == "faster now"
    # Current speed is the latest write.
    assert prefs["voice"]["speeds"]["salem"] == 1.2


def test_set_scoped_per_instance(tmp_path: Path) -> None:
    """Setting Salem's speed doesn't touch STAY-C's."""
    rel = _write_person_record(tmp_path)
    speed_pref.set_tts_speed(
        tmp_path, "person/Andrew Newton", "STAY-C", 0.9,
    )
    speed_pref.set_tts_speed(
        tmp_path, "person/Andrew Newton", "Salem", 1.2,
    )
    prefs = _load_person_prefs(tmp_path, rel)
    # Normalisation: "STAY-C" → "stay-c" (lowercase, dots stripped,
    # spaces→dashes). Matches transport peer-key convention.
    assert prefs["voice"]["speeds"]["stay-c"] == 0.9
    assert prefs["voice"]["speeds"]["salem"] == 1.2


def test_set_scoped_per_user(tmp_path: Path) -> None:
    """Andrew's speed doesn't affect a second user's person record."""
    _write_person_record(tmp_path, name="Andrew Newton")
    _write_person_record(tmp_path, name="Jamie Test")
    speed_pref.set_tts_speed(
        tmp_path, "person/Andrew Newton", "Salem", 1.2,
    )
    andrew_prefs = _load_person_prefs(tmp_path, "person/Andrew Newton.md")
    jamie_prefs = _load_person_prefs(tmp_path, "person/Jamie Test.md")
    assert andrew_prefs["voice"]["speeds"]["salem"] == 1.2
    assert jamie_prefs == {}


def test_reset_writes_default_with_by_reset(tmp_path: Path) -> None:
    rel = _write_person_record(tmp_path)
    speed_pref.set_tts_speed(
        tmp_path, "person/Andrew Newton", "Salem", 1.2,
    )
    speed_pref.set_tts_speed(
        tmp_path, "person/Andrew Newton", "Salem",
        speed_pref.SPEED_DEFAULT, by="reset",
    )
    prefs = _load_person_prefs(tmp_path, rel)
    assert prefs["voice"]["speeds"]["salem"] == 1.0
    last = prefs["voice"]["history"][-1]
    assert last["by"] == "reset"
    assert last["value"] == 1.0


# --- Report formatting ---------------------------------------------------


def test_format_report_unset(tmp_path: Path) -> None:
    _write_person_record(tmp_path)
    reply = speed_pref.format_report(tmp_path, "person/Andrew Newton", "Salem")
    assert "default 1.0" in reply
    assert "not yet customized" in reply


def test_format_report_with_history(tmp_path: Path) -> None:
    _write_person_record(tmp_path)
    for v in (1.0, 1.1, 1.15, 1.2):
        speed_pref.set_tts_speed(
            tmp_path, "person/Andrew Newton", "Salem", v,
        )
    reply = speed_pref.format_report(tmp_path, "person/Andrew Newton", "Salem")
    assert "Salem speed: 1.2" in reply
    # Only the last 3 entries render; 1.0 is dropped.
    assert "1.1" in reply
    assert "1.15" in reply
    assert "1.2" in reply
    assert reply.count("slash_command") == 3


def test_format_report_filters_by_instance(tmp_path: Path) -> None:
    """History for STAY-C shouldn't leak into a Salem report."""
    _write_person_record(tmp_path)
    speed_pref.set_tts_speed(
        tmp_path, "person/Andrew Newton", "STAY-C", 0.9,
    )
    reply = speed_pref.format_report(tmp_path, "person/Andrew Newton", "Salem")
    assert "not yet customized" in reply
    assert "0.9" not in reply


# --- on_speed handler dispatch ------------------------------------------


def _make_update(text: str) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat.id = 1
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _make_ctx(talker_config, state_mgr) -> MagicMock:
    ctx = MagicMock()
    ctx.application.bot_data = {
        "config": talker_config,
        "state_mgr": state_mgr,
        "anthropic_client": FakeAnthropicClient([]),
        "system_prompt": "sys",
        "vault_context_str": "",
        "chat_locks": {},
    }
    return ctx


@pytest.mark.asyncio
async def test_on_speed_report_when_unset(talker_config, state_mgr) -> None:
    _write_person_record(Path(talker_config.vault.path))
    talker_config.instance = InstanceConfig(name="Salem", canonical="S.A.L.E.M.")

    update = _make_update("/speed")
    ctx = _make_ctx(talker_config, state_mgr)
    await bot.on_speed(update, ctx)

    reply = update.message.reply_text.call_args.args[0]
    assert "default 1.0" in reply
    assert "not yet customized" in reply


@pytest.mark.asyncio
async def test_on_speed_set_happy_path(talker_config, state_mgr) -> None:
    rel = _write_person_record(Path(talker_config.vault.path))
    talker_config.instance = InstanceConfig(name="Salem", canonical="S.A.L.E.M.")

    update = _make_update("/speed 1.2")
    ctx = _make_ctx(talker_config, state_mgr)
    await bot.on_speed(update, ctx)

    reply = update.message.reply_text.call_args.args[0]
    assert "1.2" in reply
    prefs = _load_person_prefs(Path(talker_config.vault.path), rel)
    assert prefs["voice"]["speeds"]["salem"] == 1.2


@pytest.mark.asyncio
async def test_on_speed_set_with_note(talker_config, state_mgr) -> None:
    rel = _write_person_record(Path(talker_config.vault.path))
    talker_config.instance = InstanceConfig(name="Salem", canonical="S.A.L.E.M.")

    update = _make_update("/speed 1.2 too slow at default")
    ctx = _make_ctx(talker_config, state_mgr)
    await bot.on_speed(update, ctx)

    prefs = _load_person_prefs(Path(talker_config.vault.path), rel)
    history = prefs["voice"]["history"]
    assert history[-1]["note"] == "too slow at default"


@pytest.mark.asyncio
async def test_on_speed_rejects_above_range(talker_config, state_mgr) -> None:
    _write_person_record(Path(talker_config.vault.path))
    update = _make_update("/speed 1.5")
    ctx = _make_ctx(talker_config, state_mgr)
    await bot.on_speed(update, ctx)

    reply = update.message.reply_text.call_args.args[0]
    assert "0.7" in reply
    assert "1.2" in reply
    assert "1.5" in reply


@pytest.mark.asyncio
async def test_on_speed_rejects_below_range(talker_config, state_mgr) -> None:
    _write_person_record(Path(talker_config.vault.path))
    update = _make_update("/speed 0.5")
    ctx = _make_ctx(talker_config, state_mgr)
    await bot.on_speed(update, ctx)

    reply = update.message.reply_text.call_args.args[0]
    assert "0.5" in reply


@pytest.mark.asyncio
async def test_on_speed_default_resets(talker_config, state_mgr) -> None:
    rel = _write_person_record(Path(talker_config.vault.path))
    talker_config.instance = InstanceConfig(name="Salem", canonical="S.A.L.E.M.")

    # First set to 1.2, then /speed default.
    await bot.on_speed(_make_update("/speed 1.2"), _make_ctx(talker_config, state_mgr))
    await bot.on_speed(_make_update("/speed default"), _make_ctx(talker_config, state_mgr))

    prefs = _load_person_prefs(Path(talker_config.vault.path), rel)
    assert prefs["voice"]["speeds"]["salem"] == 1.0
    assert prefs["voice"]["history"][-1]["by"] == "reset"


@pytest.mark.asyncio
async def test_on_speed_unauthorized_user_silent(talker_config, state_mgr) -> None:
    """Users not in allowed_users get no reply and no write."""
    rel = _write_person_record(Path(talker_config.vault.path))

    update = _make_update("/speed 1.2")
    update.effective_user.id = 99999  # not in allowlist

    await bot.on_speed(update, _make_ctx(talker_config, state_mgr))

    update.message.reply_text.assert_not_called()
    # No preference written.
    prefs = _load_person_prefs(Path(talker_config.vault.path), rel)
    assert prefs == {}


# --- /brief integration: speed flows into ElevenLabs call ---------------


def _write_capture_session(vault_path: Path, name: str) -> str:
    from alfred.telegram import capture_batch
    (vault_path / "session").mkdir(exist_ok=True, parents=True)
    rel = f"session/{name}.md"
    body = (
        f"{capture_batch.SUMMARY_MARKER_START}\n\n"
        "## Structured Summary\n\n### Topics\n- a\n\n"
        f"{capture_batch.SUMMARY_MARKER_END}\n\n"
        "# Transcript\n\n**Andrew** (10:00 · voice): hi\n"
    )
    (vault_path / rel).write_text(
        "---\ntype: session\nstatus: completed\n"
        f"name: {name}\ncreated: '2026-04-20'\n"
        "session_type: capture\n---\n\n" + body,
        encoding="utf-8",
    )
    return rel


def _seed_closed_session(state_mgr, short_id: str, rel_path: str) -> None:
    state_mgr.state.setdefault("closed_sessions", []).append({
        "session_id": f"{short_id}-full-uuid",
        "chat_id": 1,
        "started_at": "2026-04-20T10:00:00+00:00",
        "ended_at":   "2026-04-20T10:30:00+00:00",
        "reason": "explicit",
        "record_path": rel_path,
        "message_count": 5,
        "vault_ops": 0,
        "session_type": "capture",
        "continues_from": None,
        "opening_model": "claude-sonnet-4-6",
        "closing_model": "claude-sonnet-4-6",
    })
    state_mgr.save()


@pytest.mark.asyncio
async def test_brief_forwards_stored_speed_to_synthesize(
    talker_config, state_mgr, monkeypatch,
) -> None:
    """Andrew has salem.speed=1.2; /brief forwards it to ElevenLabs."""
    vault_path = Path(talker_config.vault.path)
    _write_person_record(
        vault_path,
        preferences={"voice": {"speeds": {"salem": 1.2}}},
    )
    rel = _write_capture_session(
        vault_path, "Voice Session — 2026-04-20 1000 spd11111",
    )
    _seed_closed_session(state_mgr, "spd11111", rel)

    talker_config.instance = InstanceConfig(name="Salem", canonical="S.A.L.E.M.")
    talker_config.tts = TtsConfig(
        api_key="DUMMY_ELEVENLABS_TEST_KEY",
        voice_id="Rachel",
        model="eleven_turbo_v2_5",
        summary_word_target=300,
    )

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="compressed prose")]),
    ])

    captured_speed: dict = {}

    async def _fake_synth(text, cfg, *, speed=None):
        captured_speed["value"] = speed
        return b"FAKE-MP3"
    monkeypatch.setattr(tts, "synthesize", _fake_synth)

    update = _make_update("/brief spd11111")
    update.message.message_id = 1
    ctx = _make_ctx(talker_config, state_mgr)
    ctx.application.bot_data["anthropic_client"] = client
    ctx.args = ["spd11111"]
    ctx.bot.send_voice = AsyncMock()
    ctx.bot.send_document = AsyncMock()

    await bot.on_brief(update, ctx)

    assert captured_speed["value"] == 1.2


@pytest.mark.asyncio
async def test_brief_uses_default_speed_when_unset(
    talker_config, state_mgr, monkeypatch,
) -> None:
    """No stored preference → /brief still synthesises with default 1.0."""
    vault_path = Path(talker_config.vault.path)
    _write_person_record(vault_path)  # no preferences
    rel = _write_capture_session(
        vault_path, "Voice Session — 2026-04-20 1000 spd22222",
    )
    _seed_closed_session(state_mgr, "spd22222", rel)

    talker_config.instance = InstanceConfig(name="Salem", canonical="S.A.L.E.M.")
    talker_config.tts = TtsConfig(
        api_key="DUMMY_ELEVENLABS_TEST_KEY",
        voice_id="Rachel",
        model="eleven_turbo_v2_5",
        summary_word_target=300,
    )

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="compressed prose")]),
    ])

    captured_speed: dict = {}

    async def _fake_synth(text, cfg, *, speed=None):
        captured_speed["value"] = speed
        return b"FAKE-MP3"
    monkeypatch.setattr(tts, "synthesize", _fake_synth)

    update = _make_update("/brief spd22222")
    update.message.message_id = 1
    ctx = _make_ctx(talker_config, state_mgr)
    ctx.application.bot_data["anthropic_client"] = client
    ctx.args = ["spd22222"]
    ctx.bot.send_voice = AsyncMock()
    ctx.bot.send_document = AsyncMock()

    await bot.on_brief(update, ctx)

    assert captured_speed["value"] == speed_pref.SPEED_DEFAULT


# --- synthesize forwards voice_settings.speed ----------------------------


@pytest.mark.asyncio
async def test_synthesize_forwards_speed_as_voice_setting(monkeypatch) -> None:
    captured: dict = {}

    async def _fake_post(self, url, **kwargs):
        captured["json"] = kwargs.get("json", {})
        return httpx.Response(200, content=b"FAKE-MP3")

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    cfg = TtsConfig(api_key="DUMMY_ELEVENLABS_TEST_KEY", voice_id="Rachel")
    await tts.synthesize("hi", cfg, speed=1.2)

    assert captured["json"]["voice_settings"]["speed"] == 1.2


@pytest.mark.asyncio
async def test_synthesize_omits_speed_when_none(monkeypatch) -> None:
    """No speed arg → voice_settings carries no ``speed`` key (ElevenLabs default applies)."""
    captured: dict = {}

    async def _fake_post(self, url, **kwargs):
        captured["json"] = kwargs.get("json", {})
        return httpx.Response(200, content=b"FAKE-MP3")

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    cfg = TtsConfig(api_key="DUMMY_ELEVENLABS_TEST_KEY", voice_id="Rachel")
    await tts.synthesize("hi", cfg)

    assert "speed" not in captured["json"]["voice_settings"]


# --- Inline-command dispatch -------------------------------------------


def test_detect_inline_speed_plain() -> None:
    """``Good. /speed`` (no arg) routes to the speed handler."""
    assert bot._detect_inline_command("Good. /speed") == "speed"


def test_detect_inline_speed_with_arg() -> None:
    """``Good. /speed 1.2`` routes to the speed handler."""
    assert bot._detect_inline_command("Good. /speed 1.2") == "speed"


def test_detect_inline_speed_with_note() -> None:
    """Trailing free-text note doesn't break detection."""
    assert bot._detect_inline_command("OK. /speed 1.2 too slow") == "speed"
