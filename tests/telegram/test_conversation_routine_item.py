"""Tests for the conversation-loop ``routine_item`` dispatcher (Phase 2B B3, 2026-05-30).

Conversational item-CRUD path. The dispatcher subprocess-invokes
``alfred routine item <action>`` and routes on the structured ``kind``
canary discriminator. These tests mock ``subprocess.run`` so no real
CLI fires — the contract being pinned is the dispatcher-glue shape
(mirror of test_conversation_routine_done.py):

  * Tool-set gating — KAL-LE / Hypatia refused; Salem accepted.
  * Argument validation — action must be add/remove/edit; item required.
  * Argv shape per action (add: record + text; remove/edit:
    two-positional OR one-positional fuzzy form).
  * Env threading — ``ALFRED_VAULT_SCOPE=talker_routine_item``.
  * Fields-dict → flag serialisation: ``--priority``,
    ``--target-cadence-days``, ``--surface-at-days``,
    ``--escalate-at-days``, ``--due-pattern`` (JSON-encoded),
    ``--text`` (edit only — rename), ``--clear-due-pattern``,
    ``--clear-target-cadence-days``.
  * Canary pass-through (added / removed / edited / cadence_conflict).
  * Reversed-JSON-line scan.
  * Failure contract logging.
  * Timeout.
  * Tool registration in SALEM_VAULT_TOOLS.
  * Schema required-fields pin (action + item).

The CLI itself is tested in tests/routine/test_cli_items.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

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


# --- Fixtures (mirror B1's file) -------------------------------------------


def _salem_config(tmp_path: Path) -> TalkerConfig:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    return TalkerConfig(
        bot_token="x",
        allowed_users=[1],
        primary_users=["person/Test"],
        anthropic=AnthropicConfig(api_key="x", model="claude-opus-4-7"),
        stt=STTConfig(api_key="x", model="whisper-large-v3"),
        session=SessionConfig(state_path=str(tmp_path / "state.json")),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name="Salem", tool_set="talker"),
    )


def _kalle_config(tmp_path: Path) -> TalkerConfig:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    return TalkerConfig(
        bot_token="x",
        allowed_users=[1],
        primary_users=["person/Test"],
        anthropic=AnthropicConfig(api_key="x", model="claude-opus-4-7"),
        stt=STTConfig(api_key="x", model="whisper-large-v3"),
        session=SessionConfig(state_path=str(tmp_path / "state.json")),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name="KAL-LE", tool_set="kalle"),
    )


def _hypatia_config(tmp_path: Path) -> TalkerConfig:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    return TalkerConfig(
        bot_token="x",
        allowed_users=[1],
        primary_users=["person/Test"],
        anthropic=AnthropicConfig(api_key="x", model="claude-opus-4-7"),
        stt=STTConfig(api_key="x", model="whisper-large-v3"),
        session=SessionConfig(state_path=str(tmp_path / "state.json")),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name="Hypatia", tool_set="hypatia"),
    )


def _session(chat_id: int = 1, session_id: str = "sess-1") -> Session:
    now = datetime(2026, 5, 30, tzinfo=timezone.utc)
    return Session(
        session_id=session_id,
        chat_id=chat_id,
        started_at=now,
        last_message_at=now,
        model="claude-opus-4-7",
    )


def _fake_completed_proc(
    *, stdout: str = "", stderr: str = "", returncode: int = 0,
):
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


# --- Tool-set gating -------------------------------------------------------


@pytest.mark.asyncio
async def test_routine_item_refuses_on_kalle_tool_set(tmp_path, monkeypatch):
    config = _kalle_config(tmp_path)
    sess = _session()

    call_count = {"n": 0}

    def fake_run(*args, **kwargs):
        call_count["n"] += 1
        return _fake_completed_proc()

    monkeypatch.setattr("subprocess.run", fake_run)

    result_str = await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={"action": "add", "record": "X", "item": "Y"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert "routine_item is Salem-only" in parsed.get("error", "")
    assert parsed.get("tool_set") == "kalle"
    assert call_count["n"] == 0


@pytest.mark.asyncio
async def test_routine_item_refuses_on_hypatia_tool_set(
    tmp_path, monkeypatch,
):
    config = _hypatia_config(tmp_path)
    sess = _session()

    call_count = {"n": 0}

    def fake_run(*args, **kwargs):
        call_count["n"] += 1
        return _fake_completed_proc()

    monkeypatch.setattr("subprocess.run", fake_run)

    result_str = await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={"action": "add", "record": "X", "item": "Y"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert "routine_item is Salem-only" in parsed.get("error", "")
    assert call_count["n"] == 0


# --- Argument validation ---------------------------------------------------


@pytest.mark.asyncio
async def test_routine_item_rejects_invalid_action(tmp_path):
    config = _salem_config(tmp_path)
    sess = _session()

    result_str = await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={"action": "rename", "item": "X"},  # not valid
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert "action must be add/remove/edit" in parsed.get("error", "")


@pytest.mark.asyncio
async def test_routine_item_rejects_empty_item(tmp_path):
    config = _salem_config(tmp_path)
    sess = _session()

    result_str = await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={"action": "add", "record": "X", "item": ""},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert "non-empty 'item'" in parsed.get("error", "")


@pytest.mark.asyncio
async def test_routine_item_rejects_non_dict_tool_input(tmp_path):
    config = _salem_config(tmp_path)
    sess = _session()

    result_str = await conversation._execute_tool(
        tool_name="routine_item",
        tool_input="not a dict",  # type: ignore[arg-type]
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert "error" in parsed


@pytest.mark.asyncio
async def test_routine_item_add_empty_record_short_circuits(
    tmp_path, monkeypatch,
):
    """Add with empty record short-circuits at the dispatcher (before
    subprocess). Returns unknown_record kind WITHOUT spawning the
    subprocess."""
    config = _salem_config(tmp_path)
    sess = _session()

    call_count = {"n": 0}

    def fake_run(*args, **kwargs):
        call_count["n"] += 1
        return _fake_completed_proc()

    monkeypatch.setattr("subprocess.run", fake_run)

    result_str = await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={"action": "add", "record": "", "item": "X"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["kind"] == "unknown_record"
    # Subprocess NOT invoked (short-circuit at the dispatcher).
    assert call_count["n"] == 0


# --- Argv shape (add) -----------------------------------------------------


@pytest.mark.asyncio
async def test_routine_item_add_argv_shape_basic(tmp_path, monkeypatch):
    """Add: argv = [..., 'routine', 'item', 'add', record, text, --json]"""
    config = _salem_config(tmp_path)
    sess = _session()

    captured = {}

    def fake_run(argv, *args, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env", {})
        return _fake_completed_proc(
            stdout=json.dumps({
                "ok": True, "kind": "added",
                "record": "Self Care", "item": "Walk dog",
            }),
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={
            "action": "add",
            "record": "Self Care",
            "item": "Walk dog",
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    argv = captured["argv"]
    m_idx = argv.index("-m")
    # NEVER alfred.cli per the 2026-05-28 silent-no-op lesson.
    assert argv[m_idx + 1] == "alfred"
    assert argv[m_idx + 2] == "routine"
    assert argv[m_idx + 3] == "item"
    assert argv[m_idx + 4] == "add"
    assert argv[m_idx + 5] == "Self Care"
    assert argv[m_idx + 6] == "Walk dog"
    assert "--json" in argv


@pytest.mark.asyncio
async def test_routine_item_add_argv_with_fields(tmp_path, monkeypatch):
    """Add with fields dict serialises to --flag form."""
    config = _salem_config(tmp_path)
    sess = _session()

    captured = {}

    def fake_run(argv, *args, **kwargs):
        captured["argv"] = argv
        return _fake_completed_proc(
            stdout=json.dumps({
                "ok": True, "kind": "added",
                "record": "Self Care", "item": "X",
            }),
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={
            "action": "add",
            "record": "Self Care",
            "item": "X",
            "fields": {
                "priority": "aspirational",
                "target_cadence_days": 3,
            },
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    argv = captured["argv"]
    assert "--priority" in argv
    assert "aspirational" in argv
    assert "--target-cadence-days" in argv
    assert "3" in argv


@pytest.mark.asyncio
async def test_routine_item_add_argv_with_due_pattern_dict(
    tmp_path, monkeypatch,
):
    """Dict due_pattern serialised to JSON string for the CLI."""
    config = _salem_config(tmp_path)
    sess = _session()

    captured = {}

    def fake_run(argv, *args, **kwargs):
        captured["argv"] = argv
        return _fake_completed_proc(
            stdout=json.dumps({
                "ok": True, "kind": "added",
                "record": "X", "item": "Y",
            }),
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={
            "action": "add",
            "record": "X",
            "item": "Y",
            "fields": {
                "due_pattern": {"type": "weekly", "day": "thu"},
                "escalate_at_days": 0,
            },
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    argv = captured["argv"]
    assert "--due-pattern" in argv
    dp_idx = argv.index("--due-pattern")
    dp_value = argv[dp_idx + 1]
    # The value is a JSON string the CLI's _validate_due_pattern
    # decodes.
    decoded = json.loads(dp_value)
    assert decoded == {"type": "weekly", "day": "thu"}
    assert "--escalate-at-days" in argv
    assert "0" in argv


# --- Argv shape (remove) --------------------------------------------------


@pytest.mark.asyncio
async def test_routine_item_remove_argv_with_record(tmp_path, monkeypatch):
    """Remove with record: two-positional argv form."""
    config = _salem_config(tmp_path)
    sess = _session()

    captured = {}

    def fake_run(argv, *args, **kwargs):
        captured["argv"] = argv
        return _fake_completed_proc(
            stdout=json.dumps({
                "ok": True, "kind": "removed",
                "record": "X", "item": "Y",
            }),
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={
            "action": "remove",
            "record": "X",
            "item": "Y",
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    argv = captured["argv"]
    m_idx = argv.index("-m")
    assert argv[m_idx + 4] == "remove"
    assert argv[m_idx + 5] == "X"
    assert argv[m_idx + 6] == "Y"


@pytest.mark.asyncio
async def test_routine_item_remove_argv_vault_wide_fuzzy(
    tmp_path, monkeypatch,
):
    """Remove without record: one-positional form (vault-wide fuzzy
    falls through to the CLI)."""
    config = _salem_config(tmp_path)
    sess = _session()

    captured = {}

    def fake_run(argv, *args, **kwargs):
        captured["argv"] = argv
        return _fake_completed_proc(
            stdout=json.dumps({
                "ok": True, "kind": "removed",
                "record": "X", "item": "Y",
            }),
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={
            "action": "remove",
            "item": "Y",
            # no record
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    argv = captured["argv"]
    m_idx = argv.index("-m")
    assert argv[m_idx + 4] == "remove"
    # ONE positional (just item, no record).
    assert argv[m_idx + 5] == "Y"
    # Verify the very next arg is --json (no positional after item).
    assert argv[m_idx + 6] == "--json"


# --- Argv shape (edit) ----------------------------------------------------


@pytest.mark.asyncio
async def test_routine_item_edit_argv_with_text_rename(
    tmp_path, monkeypatch,
):
    """Edit with text rename: --text NEW threads through."""
    config = _salem_config(tmp_path)
    sess = _session()

    captured = {}

    def fake_run(argv, *args, **kwargs):
        captured["argv"] = argv
        return _fake_completed_proc(
            stdout=json.dumps({
                "ok": True, "kind": "edited",
                "record": "X", "item": "Old", "renamed_to": "New",
            }),
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={
            "action": "edit",
            "record": "X",
            "item": "Old",
            "fields": {"text": "New"},
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    argv = captured["argv"]
    assert "--text" in argv
    text_idx = argv.index("--text")
    assert argv[text_idx + 1] == "New"


@pytest.mark.asyncio
async def test_routine_item_edit_argv_with_clear_flags(
    tmp_path, monkeypatch,
):
    """Both clear flags threaded as boolean store_true args."""
    config = _salem_config(tmp_path)
    sess = _session()

    captured = {}

    def fake_run(argv, *args, **kwargs):
        captured["argv"] = argv
        return _fake_completed_proc(
            stdout=json.dumps({
                "ok": True, "kind": "edited",
                "record": "X", "item": "Y",
            }),
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={
            "action": "edit",
            "record": "X",
            "item": "Y",
            "fields": {
                "target_cadence_days": 3,
                "clear_due_pattern": True,
            },
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    argv = captured["argv"]
    assert "--clear-due-pattern" in argv
    assert "--target-cadence-days" in argv


# --- Env threading --------------------------------------------------------


@pytest.mark.asyncio
async def test_routine_item_env_carries_narrow_scope(
    tmp_path, monkeypatch,
):
    """ALFRED_VAULT_SCOPE=talker_routine_item plumbed through."""
    config = _salem_config(tmp_path)
    sess = _session()

    captured = {}

    def fake_run(argv, *args, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return _fake_completed_proc(
            stdout=json.dumps({
                "ok": True, "kind": "added",
                "record": "X", "item": "Y",
            }),
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={
            "action": "add", "record": "X", "item": "Y",
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    env = captured["env"]
    assert env.get("ALFRED_VAULT_SCOPE") == "talker_routine_item"


# --- Canary pass-through --------------------------------------------------


@pytest.mark.asyncio
async def test_routine_item_canary_added_passed_through(
    tmp_path, monkeypatch,
):
    config = _salem_config(tmp_path)
    sess = _session()

    payload = {
        "ok": True, "kind": "added",
        "record": "Self Care", "item": "Walk dog",
        "path": "routine/Self Care.md",
        "new_item": {
            "text": "Walk dog",
            "priority": "aspirational",
            "target_cadence_days": 3,
        },
    }

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed_proc(stdout=json.dumps(payload)),
    )

    result_str = await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={
            "action": "add", "record": "Self Care", "item": "Walk dog",
            "fields": {
                "priority": "aspirational",
                "target_cadence_days": 3,
            },
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed == payload


@pytest.mark.asyncio
async def test_routine_item_canary_cadence_conflict_passed_through(
    tmp_path, monkeypatch,
):
    """Cadence-conflict canary returned verbatim so the SKILL can
    ask back with the clear-flag offer."""
    config = _salem_config(tmp_path)
    sess = _session()

    payload = {
        "ok": False, "kind": "cadence_conflict",
        "record": "Self Care", "item": "Walk dog",
        "error": "Item currently uses a hard deadline...",
    }

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed_proc(
            stdout=json.dumps(payload), returncode=1,
        ),
    )

    result_str = await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={
            "action": "edit",
            "item": "Walk dog",
            "fields": {"target_cadence_days": 3},
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["kind"] == "cadence_conflict"
    assert parsed["ok"] is False


@pytest.mark.asyncio
async def test_routine_item_canary_ambiguous_passed_through(
    tmp_path, monkeypatch,
):
    config = _salem_config(tmp_path)
    sess = _session()

    payload = {
        "ok": False, "kind": "ambiguous_item",
        "item_text_input": "walked",
        "candidates": [
            {"record": "Self Care", "item": "Walk dog"},
            {"record": "Outdoor", "item": "Walk to coffee shop"},
        ],
    }

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed_proc(
            stdout=json.dumps(payload), returncode=1,
        ),
    )

    result_str = await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={
            "action": "remove", "item": "walked",
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["kind"] == "ambiguous_item"
    assert len(parsed["candidates"]) == 2


# --- Reversed-JSON-line scan ----------------------------------------------


@pytest.mark.asyncio
async def test_routine_item_reversed_scan_returns_last_json_line(
    tmp_path, monkeypatch,
):
    """Structlog-pollution defense — same pattern as B1's pin."""
    config = _salem_config(tmp_path)
    sess = _session()

    payload = {
        "ok": True, "kind": "added",
        "record": "X", "item": "Y",
    }
    stdout = (
        "INFO some.event field=value\n"
        f"{json.dumps(payload)}\n"
    )

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed_proc(stdout=stdout),
    )

    result_str = await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={"action": "add", "record": "X", "item": "Y"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["kind"] == "added"


# --- Subprocess failure contract ------------------------------------------


@pytest.mark.asyncio
async def test_routine_item_nonzero_exit_without_canary_logged(
    tmp_path, monkeypatch,
):
    """Non-zero exit + no parseable JSON → logged per builder.md
    (stdout_tail sentinel + stderr) + returns subprocess_error."""
    import structlog

    config = _salem_config(tmp_path)
    sess = _session()

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed_proc(
            stdout="", stderr="Import failed\n",
            returncode=2,
        ),
    )

    with structlog.testing.capture_logs() as captured:
        result_str = await conversation._execute_tool(
            tool_name="routine_item",
            tool_input={"action": "add", "record": "X", "item": "Y"},
            vault_path=str(tmp_path / "vault"),
            state=StateManager(config.session.state_path),
            session=sess,
            config=config,
        )
    parsed = json.loads(result_str)
    assert parsed["kind"] == "subprocess_error"
    assert "Import failed" in parsed["error"]

    failure_events = [
        c for c in captured
        if c.get("event") == "talker.routine_item.nonzero_exit_no_canary"
    ]
    assert len(failure_events) == 1
    ev = failure_events[0]
    assert ev["code"] == 2
    assert "Import failed" in ev["stderr"]
    assert "stdout_tail" in ev
    assert ev["stdout_tail"] == ""  # sentinel for the no-output case


@pytest.mark.asyncio
async def test_routine_item_timeout_returns_structured_error(
    tmp_path, monkeypatch,
):
    import subprocess as sp

    config = _salem_config(tmp_path)
    sess = _session()

    def fake_run(*args, **kwargs):
        raise sp.TimeoutExpired(cmd=args[0], timeout=30)

    monkeypatch.setattr("subprocess.run", fake_run)

    result_str = await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={"action": "add", "record": "X", "item": "Y"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["kind"] == "timeout"
    assert "30s" in parsed["error"]


# --- Tool registration + schema --------------------------------------------


def test_routine_item_in_salem_tool_set():
    """Salem tool set MUST include routine_item; KAL-LE / Hypatia
    MUST NOT (Salem-only)."""
    talker_tools = conversation.VAULT_TOOLS_BY_SET["talker"]
    kalle_tools = conversation.VAULT_TOOLS_BY_SET["kalle"]
    hypatia_tools = conversation.VAULT_TOOLS_BY_SET["hypatia"]

    talker_names = {t["name"] for t in talker_tools}
    kalle_names = {t["name"] for t in kalle_tools}
    hypatia_names = {t["name"] for t in hypatia_tools}

    assert "routine_item" in talker_names
    assert "routine_item" not in kalle_names
    assert "routine_item" not in hypatia_names


def test_routine_item_schema_required_fields():
    """Schema MUST require action + item; record + fields optional."""
    schema = conversation._ROUTINE_ITEM_TOOL_SCHEMA["input_schema"]
    required = schema.get("required", [])
    assert sorted(required) == ["action", "item"]
    props = schema["properties"]
    assert "action" in props
    assert "item" in props
    assert "record" in props
    assert "fields" in props


def test_routine_item_schema_action_enum():
    """Schema action enum locked to add/remove/edit."""
    schema = conversation._ROUTINE_ITEM_TOOL_SCHEMA["input_schema"]
    enum = schema["properties"]["action"]["enum"]
    assert sorted(enum) == ["add", "edit", "remove"]


# --- Cross-agent contract: ITEM_KIND_* lockstep ---------------------------


def test_item_kind_constants_match_skill_routing_table() -> None:
    """Set-difference lockstep pin between ITEM_KIND_* constants and
    the SKILL's 'routing on canary' table. Drift in either direction
    surfaces here — either:
      * A new ITEM_KIND_* exists in code but SKILL doesn't route on it
        (operator sees a kind Salem doesn't know how to handle) — list
        the missing entry.
      * The SKILL routes on a kind that doesn't exist in code (talker
        will never see it) — list the extra entry.

    The 'expected' set lives in this test, not imported from the
    SKILL — keeping it static here forces a deliberate test update
    when adding new kinds.

    Per feedback_set_difference_lockstep_pin (twice-flagged 2026-05-30):
    pin set-difference, never literal-membership; the literal-pin shape
    only catches THIS addition and lets future drift through.
    """
    from alfred.routine.cli import (
        ITEM_KIND_ADDED,
        ITEM_KIND_AMBIGUOUS_ITEM,
        ITEM_KIND_CADENCE_CONFLICT,
        ITEM_KIND_DUPLICATE_ITEM,
        ITEM_KIND_EDITED,
        ITEM_KIND_INVALID_FIELD,
        ITEM_KIND_REMOVED,
        ITEM_KIND_UNKNOWN_ITEM,
        ITEM_KIND_UNKNOWN_RECORD,
    )

    # The 9 kinds the SKILL's 'Routing on the canary kind discriminator'
    # subsection documents (Adjusting routines section).
    skill_routed_kinds = {
        # Success kinds (one per action).
        "added", "removed", "edited",
        # Refusal / disambiguation kinds.
        "ambiguous_item", "unknown_item", "unknown_record",
        # B3-specific refusal kinds.
        "cadence_conflict", "duplicate_item", "invalid_field",
    }
    code_kinds = {
        ITEM_KIND_ADDED, ITEM_KIND_REMOVED, ITEM_KIND_EDITED,
        ITEM_KIND_AMBIGUOUS_ITEM, ITEM_KIND_UNKNOWN_ITEM,
        ITEM_KIND_UNKNOWN_RECORD, ITEM_KIND_CADENCE_CONFLICT,
        ITEM_KIND_DUPLICATE_ITEM, ITEM_KIND_INVALID_FIELD,
    }

    missing_from_skill = code_kinds - skill_routed_kinds
    extra_in_skill = skill_routed_kinds - code_kinds
    assert not missing_from_skill, (
        f"ITEM_KIND_* constants exist in code but the SKILL's "
        f"'Routing on canary' table doesn't document them: "
        f"{sorted(missing_from_skill)!r}. Operator would see Salem "
        f"get a kind it doesn't know how to phrase. Add to the SKILL "
        f"or rename/remove the constant."
    )
    assert not extra_in_skill, (
        f"SKILL's routing table documents kinds that don't exist in "
        f"code: {sorted(extra_in_skill)!r}. Talker will never see "
        f"these. Add the constant or remove from the SKILL table."
    )


# --- self_care SET-path (06-27 gap) ----------------------------------------


@pytest.mark.asyncio
async def test_routine_item_add_argv_self_care(tmp_path, monkeypatch):
    """fields={'self_care': true} on add → argv carries --self-care."""
    config = _salem_config(tmp_path)
    sess = _session()
    captured = {}

    def fake_run(argv, *args, **kwargs):
        captured["argv"] = argv
        return _fake_completed_proc(
            stdout=json.dumps({
                "ok": True, "kind": "added", "record": "Self Care",
                "item": "Meditate",
            }),
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={
            "action": "add", "record": "Self Care", "item": "Meditate",
            "fields": {"priority": "aspirational", "self_care": True},
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess, config=config,
    )
    assert "--self-care" in captured["argv"]
    assert "--no-self-care" not in captured["argv"]


@pytest.mark.asyncio
async def test_routine_item_edit_argv_no_self_care(tmp_path, monkeypatch):
    """fields={'self_care': false} on edit → argv carries --no-self-care
    (the explicit-off form; add has no off-flag)."""
    config = _salem_config(tmp_path)
    sess = _session()
    captured = {}

    def fake_run(argv, *args, **kwargs):
        captured["argv"] = argv
        return _fake_completed_proc(
            stdout=json.dumps({
                "ok": True, "kind": "edited", "record": "Daily",
                "item": "Stretch",
            }),
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    await conversation._execute_tool(
        tool_name="routine_item",
        tool_input={
            "action": "edit", "record": "Daily", "item": "Stretch",
            "fields": {"self_care": False},
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess, config=config,
    )
    assert "--no-self-care" in captured["argv"]
    assert "--self-care" not in captured["argv"]


def test_routine_item_schema_fields_documents_self_care():
    """The tool's ``fields`` schema must advertise self_care so the talker
    knows it can pass it (the SET-path's prompt-facing surface)."""
    schema = conversation._ROUTINE_ITEM_TOOL_SCHEMA["input_schema"]
    fields_desc = schema["properties"]["fields"]["description"]
    assert "self_care" in fields_desc
