"""Tests for the conversation-loop ``tier_confirm`` dispatcher (#21, 2026-08-01).

The chat side of C2 slot-accept: commit an auto-surfaced tier candidate onto
today's shortlist through ``tier.tier_confirm.confirm_slot_candidate`` — the SAME
writer the board's ``/feed/act`` accept calls (one writer per verb). Dispatch is
IN-PROCESS (the tier module is a clean fcntl-locked atomic writer, like
``tier_done``). These tests pin the dispatcher-glue contract:

  * Tool-set gating — KAL-LE / Hypatia refused (Salem-only); Salem accepted.
  * Per-tier round-trip via the REAL writer — T1 confirmed entry, T2/T3 operator.
  * Idempotency — a re-confirm is ``idempotent_noop`` (no duplicate).
  * Fail-loud — ``invalid_tier`` / ``thin_evidence`` surface the writer's kinds.
  * Source-sanitize (the #21 ruling, mutation-pinned) — an LLM-invented source
    persists as "operator", never as fake provenance.
  * Identity pin OUTPUT-BOUND — the tool's reply reflects the writer's RETURN
    (sentinel), not a re-derivation from tool_input.
  * Cross-writer shape — a tool-committed entry == a board-committed entry
    (byte-equivalent T2/T3; T1 confirmed:true invariant, source preserved when
    the same source is passed).
  * Malformed ``date`` fails loud (not a silent today-fallback).
  * Tool-schema surfacing — ``tier_confirm`` in the Salem tool set only.

The writer logic itself is tested in ``tests/test_daily_sync/test_slot_accept.py``
(C2) + ``tests/tier/`` — these pin ONLY the dispatcher adapter + sanitize glue.

Clock-robust: dates derive from the real Halifax "today" the dispatcher resolves
(no injectable clock), mirroring the ``tier_done`` dispatcher tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import structlog

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
from alfred.tier.daily_curation import load_daily_curation
from alfred.tier.tier_confirm import (
    CONFIRM_KIND_SUCCESS,
    ConfirmResult,
    confirm_slot_candidate,
    sanitize_source,
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
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return Session(
        session_id=session_id,
        chat_id=chat_id,
        started_at=now,
        last_message_at=now,
        model="claude-opus-4-8",
    )


def _halifax_today():
    """The date the dispatcher resolves as 'today' (Halifax tz, no injectable
    clock) — matches the tier_done dispatcher tests."""
    return datetime.now(ZoneInfo("America/Halifax")).date()


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
async def test_confirm_refuses_on_kalle(tmp_path):
    config = _kalle_config(tmp_path)
    with structlog.testing.capture_logs() as cap:
        out = await _call(
            config, _session(), tool_name="tier_confirm",
            tool_input={"tier": 1, "origin": "task", "name": "X"},
        )
    parsed = json.loads(out)
    assert "tier_confirm is Salem-only" in parsed.get("error", "")
    assert parsed.get("tool_set") == "kalle"
    # Log-emission pin (security signal must stay grep-able across refactors).
    warns = [c for c in cap if c.get("event") == "talker.tier_confirm.wrong_tool_set"]
    assert len(warns) == 1 and warns[0]["tool_set"] == "kalle"


@pytest.mark.asyncio
async def test_confirm_refuses_on_hypatia(tmp_path):
    config = _hypatia_config(tmp_path)
    out = await _call(
        config, _session(), tool_name="tier_confirm",
        tool_input={"tier": 1, "origin": "task", "name": "X"},
    )
    parsed = json.loads(out)
    assert "tier_confirm is Salem-only" in parsed.get("error", "")


# --- Per-tier round-trip via the REAL writer -------------------------------


@pytest.mark.asyncio
async def test_confirm_t1_task_writes_confirmed_entry(tmp_path):
    config = _salem_config(tmp_path)
    vault = Path(config.vault.path)
    day = _halifax_today()
    with structlog.testing.capture_logs() as cap:
        out = await _call(
            config, _session(), tool_name="tier_confirm",
            tool_input={"tier": 1, "origin": "task", "name": "Interview", "source": "auto-due"},
        )
    parsed = json.loads(out)
    assert parsed["kind"] == "success"
    assert parsed["tier"] == 1 and parsed["name"] == "Interview"
    cur = load_daily_curation(vault, day)
    assert len(cur.t1) == 1
    assert cur.t1[0].task == "[[task/Interview]]"
    assert cur.t1[0].confirmed is True
    assert cur.t1[0].source == "auto-due"  # auto provenance preserved on T1
    # Log-emission pin (the ILB "ran, here's the outcome" signal).
    results = [c for c in cap if c.get("event") == "talker.tier_confirm.result"]
    assert len(results) == 1 and results[0]["kind"] == "success" and results[0]["tier"] == 1


@pytest.mark.asyncio
async def test_confirm_t2_routine_writes_operator_entry(tmp_path):
    config = _salem_config(tmp_path)
    vault = Path(config.vault.path)
    day = _halifax_today()
    out = await _call(
        config, _session(), tool_name="tier_confirm",
        tool_input={
            "tier": 2, "origin": "routine_item",
            "routine_record": "Bills", "item_text": "Pay Rent",
            "source": "auto-surface-routine",
        },
    )
    parsed = json.loads(out)
    assert parsed["kind"] == "success" and parsed["tier"] == 2
    cur = load_daily_curation(vault, day)
    assert len(cur.t2) == 1 and not cur.t1
    assert cur.t2[0].routine_item == {"record": "Bills", "text": "Pay Rent"}
    assert cur.t2[0].source == "operator"  # committed, not the auto source
    assert cur.t2[0].confirmed is None


@pytest.mark.asyncio
async def test_confirm_t3_routine_writes_free_text(tmp_path):
    config = _salem_config(tmp_path)
    vault = Path(config.vault.path)
    day = _halifax_today()
    out = await _call(
        config, _session(), tool_name="tier_confirm",
        tool_input={
            "tier": 3, "origin": "routine_item",
            "routine_record": "Self Care", "item_text": "Meditate",
            "source": "self-care",
        },
    )
    parsed = json.loads(out)
    assert parsed["kind"] == "success" and parsed["tier"] == 3
    cur = load_daily_curation(vault, day)
    assert len(cur.t3) == 1
    assert cur.t3[0].item == "Meditate"
    assert cur.t3[0].source == "operator"


# --- Idempotency -----------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_idempotent_reconfirm(tmp_path):
    config = _salem_config(tmp_path)
    vault = Path(config.vault.path)
    day = _halifax_today()
    args = {"tier": 1, "origin": "task", "name": "Interview", "source": "auto-due"}
    first = json.loads(await _call(config, _session(), tool_name="tier_confirm", tool_input=args))
    second = json.loads(await _call(config, _session(), tool_name="tier_confirm", tool_input=args))
    assert first["kind"] == "success"
    assert second["kind"] == "idempotent_noop"
    assert len(load_daily_curation(vault, day).t1) == 1  # no duplicate


# --- Fail-loud -------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_invalid_tier(tmp_path):
    config = _salem_config(tmp_path)
    vault = Path(config.vault.path)
    day = _halifax_today()
    out = await _call(
        config, _session(), tool_name="tier_confirm",
        tool_input={"tier": 5, "origin": "task", "name": "X"},
    )
    assert json.loads(out)["kind"] == "invalid_tier"
    assert load_daily_curation(vault, day) is None  # no write


@pytest.mark.asyncio
async def test_confirm_thin_evidence_t1_no_identity(tmp_path):
    config = _salem_config(tmp_path)
    vault = Path(config.vault.path)
    day = _halifax_today()
    out = await _call(
        config, _session(), tool_name="tier_confirm",
        tool_input={"tier": 1, "origin": "routine_item"},  # no name / record+text
    )
    assert json.loads(out)["kind"] == "thin_evidence"
    assert load_daily_curation(vault, day) is None  # no write


@pytest.mark.asyncio
async def test_confirm_malformed_date_fails_loud(tmp_path):
    config = _salem_config(tmp_path)
    out = await _call(
        config, _session(), tool_name="tier_confirm",
        tool_input={"tier": 1, "origin": "task", "name": "X", "date": "tomorrow"},
    )
    assert "YYYY-MM-DD" in json.loads(out).get("error", "")


# --- Source-sanitize (the #21 ruling, mutation-pinned) ---------------------


@pytest.mark.asyncio
async def test_confirm_sanitizes_garbage_source_to_operator(tmp_path):
    """A T1 confirm with an LLM-invented source persists "operator", NEVER the
    garbage — fake provenance must not reach the daily file. Mutation-verified:
    drop the sanitize and the invented string lands as the entry source."""
    config = _salem_config(tmp_path)
    vault = Path(config.vault.path)
    day = _halifax_today()
    await _call(
        config, _session(), tool_name="tier_confirm",
        tool_input={
            "tier": 1, "origin": "task", "name": "Interview",
            "source": "totally-invented-enum",
        },
    )
    entry = load_daily_curation(vault, day).t1[0]
    assert entry.source == "operator"  # coerced, NOT the garbage
    assert entry.source != "totally-invented-enum"
    assert entry.confirmed is True


@pytest.mark.asyncio
async def test_confirm_preserves_valid_t1_source(tmp_path):
    config = _salem_config(tmp_path)
    vault = Path(config.vault.path)
    day = _halifax_today()
    await _call(
        config, _session(), tool_name="tier_confirm",
        tool_input={"tier": 1, "origin": "task", "name": "Interview", "source": "auto-escalate"},
    )
    assert load_daily_curation(vault, day).t1[0].source == "auto-escalate"


def test_sanitize_source_vocab():
    """Unit: known vocab passes; everything else → operator."""
    for ok in ("auto-due", "auto-escalate", "auto-due-routine",
               "auto-surface-routine", "auto-cadence-routine", "self-care", "operator"):
        assert sanitize_source(ok) == ok
    for bad in ("due today", "totally-invented", "", "  ", "rollover", "operator-adhoc"):
        assert sanitize_source(bad) == "operator"
    assert sanitize_source(None) == "operator"


# --- Identity pin (OUTPUT-BOUND) -------------------------------------------


@pytest.mark.asyncio
async def test_confirm_output_bound_to_writer_return(tmp_path, monkeypatch):
    """The tool's reply is BOUND to ``confirm_slot_candidate``'s RETURN, not
    re-derived from tool_input. Patch the writer to a SENTINEL that cannot arise
    from the input → the tool's JSON reflects the sentinel (tier/name/date). A
    fork that echoes tool_input instead reddens."""
    from alfred.tier import tier_confirm as tc

    sentinel = ConfirmResult(
        kind=CONFIRM_KIND_SUCCESS, tier=2, name="SENTINEL-NAME",
        date="2099-01-01", changed=True,
    )
    # The dispatcher lazy-imports confirm_slot_candidate from the module at call
    # time, so patching the source binds through to the dispatch.
    monkeypatch.setattr(tc, "confirm_slot_candidate", lambda *a, **k: sentinel)

    config = _salem_config(tmp_path)
    out = await _call(
        config, _session(), tool_name="tier_confirm",
        tool_input={"tier": 1, "origin": "task", "name": "Interview"},  # input: T1/Interview
    )
    parsed = json.loads(out)
    assert parsed["kind"] == "success"
    assert parsed["tier"] == 2            # from sentinel, NOT input tier 1
    assert parsed["name"] == "SENTINEL-NAME"  # from sentinel, NOT input "Interview"
    assert parsed["date"] == "2099-01-01"


# --- Cross-writer shape (tool == board, one writer per verb) ---------------


@pytest.mark.asyncio
async def test_cross_writer_shape_tool_equals_board(tmp_path):
    """A tool-committed entry and a board-committed entry (both via
    confirm_slot_candidate) are byte-equivalent per lane: T2 operator exact; T1
    confirmed:true + source preserved when the same source is passed. Proves the
    tool routes through the SAME writer with faithful arg passing."""
    day = _halifax_today()
    config = _salem_config(tmp_path)
    vault_tool = Path(config.vault.path)
    vault_board = tmp_path / "vault_board"
    (vault_board / "daily").mkdir(parents=True, exist_ok=True)

    # T2 routine — same candidate both ways.
    await _call(
        config, _session(), tool_name="tier_confirm",
        tool_input={
            "tier": 2, "origin": "routine_item",
            "routine_record": "Bills", "item_text": "Pay Rent",
            "source": "auto-surface-routine",
        },
    )
    confirm_slot_candidate(
        vault_board, tier=2, origin="routine_item", name="Pay Rent",
        path="routine/Bills.md", routine_record="Bills", item_text="Pay Rent",
        source="auto-surface-routine", date=day,
    )
    assert (
        load_daily_curation(vault_tool, day).t2[0].to_dict()
        == load_daily_curation(vault_board, day).t2[0].to_dict()
    )  # byte-equivalent (both source=operator)

    # T1 task — same source passed → byte-equivalent (both auto-due, confirmed=true).
    await _call(
        config, _session(), tool_name="tier_confirm",
        tool_input={"tier": 1, "origin": "task", "name": "Interview", "source": "auto-due"},
    )
    confirm_slot_candidate(
        vault_board, tier=1, origin="task", name="Interview",
        path="task/Interview.md", routine_record=None, item_text=None,
        source="auto-due", date=day,
    )
    tool_t1 = load_daily_curation(vault_tool, day).t1[0].to_dict()
    board_t1 = load_daily_curation(vault_board, day).t1[0].to_dict()
    assert tool_t1 == board_t1
    assert tool_t1["confirmed"] is True  # the T1 semantic invariant


# --- Tool-schema surfacing -------------------------------------------------


def test_tier_confirm_in_salem_set_only():
    def _names(tool_set):
        return {t["name"] for t in conversation.VAULT_TOOLS_BY_SET[tool_set]}

    assert "tier_confirm" in _names("talker")
    assert "tier_confirm" not in _names("kalle")
    assert "tier_confirm" not in _names("hypatia")


def test_tier_confirm_schema_shape():
    schema = conversation._TIER_CONFIRM_TOOL_SCHEMA
    assert schema["name"] == "tier_confirm"
    props = schema["input_schema"]["properties"]
    assert set(schema["input_schema"]["required"]) == {"tier", "origin"}
    for field in ("tier", "origin", "name", "path", "routine_record", "item_text", "source", "date"):
        assert field in props
    assert props["tier"]["enum"] == [1, 2, 3]
    assert props["origin"]["enum"] == ["task", "routine_item"]
