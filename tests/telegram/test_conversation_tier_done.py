"""Tests for the conversation-loop ``tier_done`` / ``tier_undone`` dispatcher
(Arc #20, 2026-07-22).

Free-text T3 ad-hoc done-state. UNLIKE ``routine_done`` (subprocess), this
dispatcher calls ``tier.daily_curation.mark_t3_done`` / ``mark_t3_undone``
IN-PROCESS (the tier module is already a clean fcntl-locked atomic writer).
These tests pin the dispatcher-glue contract:

  * Tool-set gating — KAL-LE / Hypatia refused (Salem-only); Salem accepted.
  * Argument validation — non-empty ``item`` required; malformed
    ``completed_at`` fails loud (not a silent today-fallback).
  * In-process round-trip — a matched T3 item is flipped ``done_at`` on
    disk and the ``kind`` discriminator is returned to the model.
  * Honest #19 dead-end — an item not on the T3 list → ``unknown_item``.
  * Future-date gate — ``completed_at`` past ``today`` → rejected.
  * Undo — ``tier_undone`` clears the flip.
  * **Tight-allowlist invariant (the crux)** — a successful ``tier_done``
    writes ``done_at`` NESTED inside ``tier_curation`` and adds NO
    top-level ``done`` key; the ``TALKER_TIER_CURATION_FIELDS`` allowlist
    is untouched (the top-level ``done``-still-denied pin lives in
    ``tests/test_scope.py``).
  * Tool-schema surfacing — ``tier_done`` / ``tier_undone`` are in the
    Salem tool set only.

The mutator logic itself is tested in ``tests/tier/test_t3_done_state.py``
— these tests pin ONLY the dispatcher adapter shape.

Clock-robust: dates are computed RELATIVE to the real Halifax "today" the
dispatcher resolves (the dispatcher has no injectable clock), so a
back-dated success + a future-date rejection stay deterministic across
any run date.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from alfred.telegram import conversation
from alfred.telegram.config import (
    AnthropicConfig,
    InstanceConfig,
    LoggingConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
)
from alfred.telegram.session import Session
from alfred.telegram.state import StateManager
from alfred.tier.daily_curation import (
    DailyCuration,
    T3Entry,
    load_daily_curation,
    save_tier_curation,
)


# --- Fixtures --------------------------------------------------------------


def _config(tmp_path: Path, *, name: str, tool_set: str) -> TalkerConfig:
    vault_dir = tmp_path / "vault"
    (vault_dir / "daily").mkdir(parents=True, exist_ok=True)
    return TalkerConfig(
        bot_token="x",
        allowed_users=[1],
        primary_users=["person/Test"],
        anthropic=AnthropicConfig(api_key="x", model="claude-opus-4-8"),
        stt=STTConfig(api_key="x", model="whisper-large-v3"),
        session=SessionConfig(state_path=str(tmp_path / "state.json")),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name=name, tool_set=tool_set),
    )


def _salem_config(tmp_path: Path) -> TalkerConfig:
    return _config(tmp_path, name="Salem", tool_set="talker")


def _kalle_config(tmp_path: Path) -> TalkerConfig:
    return _config(tmp_path, name="KAL-LE", tool_set="kalle")


def _hypatia_config(tmp_path: Path) -> TalkerConfig:
    return _config(tmp_path, name="Hypatia", tool_set="hypatia")


def _session(chat_id: int = 1, session_id: str = "sess-1") -> Session:
    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    return Session(
        session_id=session_id,
        chat_id=chat_id,
        started_at=now,
        last_message_at=now,
        model="claude-opus-4-8",
    )


def _halifax_today():
    """The date the dispatcher resolves as 'today' (Halifax tz, no
    injectable clock). Tests derive back/future dates relative to this."""
    return datetime.now(ZoneInfo("America/Halifax")).date()


def _seed_t3(vault: Path, day, items: list[tuple[str, str]]) -> None:
    save_tier_curation(
        vault, day,
        DailyCuration(t3=[T3Entry(item=i, source=s) for i, s in items]),
    )


async def _call(config, sess, *, tool_name, tool_input):
    return await conversation._execute_tool(
        tool_name=tool_name,
        tool_input=tool_input,
        vault_path=config.vault.path,
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )


# --- Tool-set gating -------------------------------------------------------


@pytest.mark.asyncio
async def test_tier_done_refuses_on_kalle(tmp_path):
    config = _kalle_config(tmp_path)
    out = await _call(
        config, _session(), tool_name="tier_done",
        tool_input={"item": "Rake leaves"},
    )
    parsed = json.loads(out)
    assert "tier_done is Salem-only" in parsed.get("error", "")
    assert parsed.get("tool_set") == "kalle"


@pytest.mark.asyncio
async def test_tier_done_refuses_on_hypatia(tmp_path):
    config = _hypatia_config(tmp_path)
    out = await _call(
        config, _session(), tool_name="tier_done",
        tool_input={"item": "Rake leaves"},
    )
    parsed = json.loads(out)
    assert "tier_done is Salem-only" in parsed.get("error", "")


@pytest.mark.asyncio
async def test_tier_undone_refuses_on_kalle(tmp_path):
    config = _kalle_config(tmp_path)
    out = await _call(
        config, _session(), tool_name="tier_undone",
        tool_input={"item": "Rake leaves"},
    )
    parsed = json.loads(out)
    assert "tier_undone is Salem-only" in parsed.get("error", "")


# --- Argument validation ---------------------------------------------------


@pytest.mark.asyncio
async def test_tier_done_rejects_empty_item(tmp_path):
    config = _salem_config(tmp_path)
    out = await _call(
        config, _session(), tool_name="tier_done", tool_input={"item": "  "},
    )
    parsed = json.loads(out)
    assert "non-empty 'item'" in parsed.get("error", "")


@pytest.mark.asyncio
async def test_tier_done_rejects_malformed_completed_at(tmp_path):
    """A bad date fails loud — NOT a silent today-fallback."""
    config = _salem_config(tmp_path)
    out = await _call(
        config, _session(), tool_name="tier_done",
        tool_input={"item": "Rake leaves", "completed_at": "yesterday"},
    )
    parsed = json.loads(out)
    assert "YYYY-MM-DD" in parsed.get("error", "")


# --- In-process round-trip -------------------------------------------------


@pytest.mark.asyncio
async def test_tier_done_success_roundtrip(tmp_path):
    """A matched free-text T3 item is flipped ``done_at`` on disk and the
    ``success`` kind is returned."""
    config = _salem_config(tmp_path)
    vault = Path(config.vault.path)
    day = _halifax_today() - timedelta(days=1)  # a past day (not future)
    _seed_t3(vault, day, [("Rake leaves", "operator-adhoc")])

    out = await _call(
        config, _session(), tool_name="tier_done",
        tool_input={"item": "rake leaves", "completed_at": day.isoformat()},
    )
    parsed = json.loads(out)
    assert parsed["kind"] == "success"
    assert parsed["item"] == "Rake leaves"
    assert parsed["done_at"] == day.isoformat()
    # Persisted to disk.
    loaded = load_daily_curation(vault, day)
    assert loaded.t3[0].done_at == day.isoformat()


@pytest.mark.asyncio
async def test_tier_done_defaults_to_today(tmp_path):
    """Omitting ``completed_at`` targets the real Halifax today."""
    config = _salem_config(tmp_path)
    vault = Path(config.vault.path)
    today = _halifax_today()
    _seed_t3(vault, today, [("Rake leaves", "operator-adhoc")])

    out = await _call(
        config, _session(), tool_name="tier_done",
        tool_input={"item": "rake leaves"},
    )
    parsed = json.loads(out)
    assert parsed["kind"] == "success"
    assert parsed["done_at"] == today.isoformat()


@pytest.mark.asyncio
async def test_tier_done_unknown_item_is_honest_deadend(tmp_path):
    """Item not on the T3 list → unknown_item (the #19 honest close)."""
    config = _salem_config(tmp_path)
    vault = Path(config.vault.path)
    day = _halifax_today()
    _seed_t3(vault, day, [("Rake leaves", "operator-adhoc")])

    out = await _call(
        config, _session(), tool_name="tier_done",
        tool_input={"item": "wash the car"},
    )
    parsed = json.loads(out)
    assert parsed["kind"] == "unknown_item"
    assert "Rake leaves" in parsed["candidates"]


@pytest.mark.asyncio
async def test_tier_done_future_date_rejected(tmp_path):
    config = _salem_config(tmp_path)
    future = (_halifax_today() + timedelta(days=2)).isoformat()

    out = await _call(
        config, _session(), tool_name="tier_done",
        tool_input={"item": "rake leaves", "completed_at": future},
    )
    parsed = json.loads(out)
    assert parsed["kind"] == "future_date_rejected"
    assert "future" in parsed.get("error", "")


@pytest.mark.asyncio
async def test_tier_done_crash_returns_internal_error(tmp_path, monkeypatch):
    """A crash in the in-process mutator surfaces as the documented,
    pinned ``internal_error`` kind (NOT the misnamed 'subprocess_error' —
    there is no subprocess) + a human-readable ``error``."""
    config = _salem_config(tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    # The dispatcher lazy-imports mark_t3_done from the tier module at
    # call time, so patching the source binds through to the dispatch.
    monkeypatch.setattr("alfred.tier.daily_curation.mark_t3_done", _boom)

    out = await _call(
        config, _session(), tool_name="tier_done",
        tool_input={"item": "rake leaves"},
    )
    parsed = json.loads(out)
    assert parsed["kind"] == "internal_error"
    assert "crashed" in parsed.get("error", "")


@pytest.mark.asyncio
async def test_tier_undone_reverses(tmp_path):
    config = _salem_config(tmp_path)
    vault = Path(config.vault.path)
    day = _halifax_today()
    _seed_t3(vault, day, [("Rake leaves", "operator-adhoc")])

    await _call(
        config, _session(), tool_name="tier_done",
        tool_input={"item": "rake leaves"},
    )
    out = await _call(
        config, _session(), tool_name="tier_undone",
        tool_input={"item": "rake leaves"},
    )
    parsed = json.loads(out)
    assert parsed["kind"] == "unmarked"
    assert load_daily_curation(vault, day).t3[0].done_at is None


# --- Tight-allowlist invariant (the crux) ----------------------------------


@pytest.mark.asyncio
async def test_tier_done_writes_nested_not_top_level_done(tmp_path):
    """The crux invariant: a successful tier_done writes ``done_at``
    NESTED inside the tier_curation block and adds NO top-level ``done``
    key — so it rides the existing allowlist with zero widening. (The
    top-level-``done``-STILL-denied scope pin lives in test_scope.py.)"""
    import frontmatter

    config = _salem_config(tmp_path)
    vault = Path(config.vault.path)
    day = _halifax_today()
    _seed_t3(vault, day, [("Rake leaves", "operator-adhoc")])

    await _call(
        config, _session(), tool_name="tier_done",
        tool_input={"item": "rake leaves"},
    )
    post = frontmatter.load(str(vault / "daily" / f"{day.isoformat()}.md"))
    # NO sibling top-level ``done`` / ``done_at`` key on the record.
    assert "done" not in post.metadata
    assert "done_at" not in post.metadata
    # The done-state is NESTED inside the tier_curation T3 entry.
    assert post.metadata["tier_curation"]["t3"][0]["done_at"] == day.isoformat()
    # The scope allowlist was not widened.
    from alfred.vault.scope import TALKER_TIER_CURATION_FIELDS
    assert TALKER_TIER_CURATION_FIELDS == {"tier_curation"}


# --- Tool-schema surfacing -------------------------------------------------


def test_tier_tools_in_salem_set_only():
    """``tier_done`` / ``tier_undone`` surface in the Salem tool set only —
    KAL-LE / Hypatia don't get them (Salem-only capability)."""
    def _names(tool_set):
        return {t["name"] for t in conversation.VAULT_TOOLS_BY_SET[tool_set]}

    salem = _names("talker")
    assert "tier_done" in salem
    assert "tier_undone" in salem
    assert "tier_done" not in _names("kalle")
    assert "tier_undone" not in _names("kalle")
    assert "tier_done" not in _names("hypatia")
    assert "tier_undone" not in _names("hypatia")


def test_tier_done_schema_shape():
    """Schema pins: name + required ``item`` + optional ``completed_at``."""
    schema = conversation._TIER_DONE_TOOL_SCHEMA
    assert schema["name"] == "tier_done"
    props = schema["input_schema"]["properties"]
    assert set(schema["input_schema"]["required"]) == {"item"}
    assert "item" in props and "completed_at" in props
    # No ``record`` (unlike routine_done — T3 items are recordless).
    assert "record" not in props
