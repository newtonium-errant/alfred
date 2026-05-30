"""Tests for the conversation-loop ``routine_done`` dispatcher (Phase 2B B1, 2026-05-30).

Conversational completion path. The dispatcher subprocess-invokes
``alfred routine done`` and routes on the structured ``kind`` canary
discriminator. These tests mock ``subprocess.run`` so no real CLI
fires — the contract being pinned is the dispatcher-glue shape:

  * Tool-set gating — KAL-LE / Hypatia refused; Salem / no-tool-set
    accepted (talker default).
  * Argument validation — non-empty ``item`` required.
  * Argv shape — ``[python, '-m', 'alfred', 'routine', 'done', ...args,
    '--json']`` (NEVER ``alfred.cli`` per the 2026-05-28 silent-no-op
    lesson from migrate_tier_phase1.py).
  * Env threading — ``ALFRED_VAULT_SCOPE=talker_routine_completion``
    set on subprocess env.
  * Canary pass-through — JSON kind discriminator returned verbatim
    to the model so it can route on it.
  * Reversed-JSON-line scan — structlog pollution defense (the same
    pattern migrate_tier_phase1.py shipped).
  * Failure contract — non-zero exit WITHOUT a canary → logged per
    builder.md (stdout_tail sentinel + stderr).
  * Timeout — subprocess.TimeoutExpired surfaced as structured error.

The CLI itself is tested in ``tests/routine/test_cli.py`` — these
tests pin ONLY the dispatcher adapter shape.
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


# --- Fixtures --------------------------------------------------------------


def _salem_config(tmp_path: Path) -> TalkerConfig:
    """Salem-shaped config — tool_set='talker' (the default).

    Required-positional-field defaults filled minimally; only the
    fields touched by ``_dispatch_routine_done`` matter for these
    tests (``instance.tool_set``).
    """
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
    """KAL-LE shape — tool_set='kalle'. routine_done refuses."""
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
    """Hypatia shape — tool_set='hypatia'. routine_done refuses."""
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
    """Construct a ``subprocess.CompletedProcess`` look-alike."""
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


# --- Tool-set gating -------------------------------------------------------


@pytest.mark.asyncio
async def test_routine_done_refuses_on_kalle_tool_set(tmp_path, monkeypatch):
    """KAL-LE config must refuse routine_done — Salem-only tool."""
    config = _kalle_config(tmp_path)
    sess = _session()

    call_count = {"n": 0}

    def fake_run(*args, **kwargs):
        call_count["n"] += 1
        return _fake_completed_proc()

    monkeypatch.setattr("subprocess.run", fake_run)

    result_str = await conversation._execute_tool(
        tool_name="routine_done",
        tool_input={"item": "Walk dog"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert "routine_done is Salem-only" in parsed.get("error", "")
    assert parsed.get("tool_set") == "kalle"
    # Subprocess NOT invoked.
    assert call_count["n"] == 0


@pytest.mark.asyncio
async def test_routine_done_refuses_on_hypatia_tool_set(
    tmp_path, monkeypatch,
):
    """Hypatia config must refuse routine_done — Salem-only tool."""
    config = _hypatia_config(tmp_path)
    sess = _session()

    call_count = {"n": 0}

    def fake_run(*args, **kwargs):
        call_count["n"] += 1
        return _fake_completed_proc()

    monkeypatch.setattr("subprocess.run", fake_run)

    result_str = await conversation._execute_tool(
        tool_name="routine_done",
        tool_input={"item": "Walk dog"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert "routine_done is Salem-only" in parsed.get("error", "")
    assert call_count["n"] == 0


# --- Argument validation ---------------------------------------------------


@pytest.mark.asyncio
async def test_routine_done_rejects_empty_item(tmp_path, monkeypatch):
    """``item`` arg required + must be non-empty."""
    config = _salem_config(tmp_path)
    sess = _session()

    result_str = await conversation._execute_tool(
        tool_name="routine_done",
        tool_input={"item": ""},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert "non-empty 'item'" in parsed.get("error", "")


@pytest.mark.asyncio
async def test_routine_done_rejects_non_dict_tool_input(tmp_path):
    """Defensive — ``tool_input`` must be a dict (Anthropic SDK should
    always supply one, but the dispatcher guards against the
    occasional protocol weirdness)."""
    config = _salem_config(tmp_path)
    sess = _session()

    result_str = await conversation._execute_tool(
        tool_name="routine_done",
        tool_input="not a dict",  # type: ignore[arg-type]
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert "error" in parsed


# --- Argv shape -----------------------------------------------------------


@pytest.mark.asyncio
async def test_routine_done_argv_shape_item_only(tmp_path, monkeypatch):
    """When only ``item`` supplied, argv carries: alfred routine done
    <item> --json (no <record>, no --completed-at)."""
    config = _salem_config(tmp_path)
    sess = _session()

    captured = {}

    def fake_run(argv, *args, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs.get("env", {})
        return _fake_completed_proc(
            stdout=json.dumps({
                "ok": True, "kind": "success",
                "record": "Self Care", "item": "Walk dog",
                "date": "2026-05-30", "appended": True,
            }),
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    result_str = await conversation._execute_tool(
        tool_name="routine_done",
        tool_input={"item": "Walk dog"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    # Argv carries the python module form, NOT alfred.cli (the silent
    # no-op trap shape per migrate_tier_phase1.py).
    argv = captured["argv"]
    m_idx = argv.index("-m")
    assert argv[m_idx + 1] == "alfred", (
        f"Expected module path 'alfred' (canonical __main__.py dispatch); "
        f"got {argv[m_idx + 1]!r}. The 'alfred.cli' shape was the silent "
        f"no-op trap diagnosed in the 2026-05-28 migration incident."
    )
    assert argv[m_idx + 2] == "routine"
    assert argv[m_idx + 3] == "done"
    # When item-only, the next arg IS the item text.
    assert "Walk dog" in argv
    assert "--json" in argv
    # ``record`` arg NOT in the argv.
    # The CLI distinguishes by positional count: 1 positional = item;
    # 2 positionals = record + item.
    positional_count = sum(
        1 for a in argv[m_idx + 4:]
        if not a.startswith("--") and a not in ("done",)
    )
    # Exactly one positional argument (the item text).
    assert positional_count == 1, (
        f"Item-only invocation should produce ONE positional argument "
        f"(the item text). Got argv tail: {argv[m_idx + 4:]!r}"
    )
    # Result body returned verbatim.
    parsed = json.loads(result_str)
    assert parsed["kind"] == "success"


@pytest.mark.asyncio
async def test_routine_done_argv_shape_with_record(tmp_path, monkeypatch):
    """When both ``record`` and ``item`` supplied, argv has both."""
    config = _salem_config(tmp_path)
    sess = _session()

    captured = {}

    def fake_run(argv, *args, **kwargs):
        captured["argv"] = argv
        return _fake_completed_proc(
            stdout=json.dumps({
                "ok": True, "kind": "success",
                "record": "Self Care", "item": "Walk dog",
                "date": "2026-05-30", "appended": True,
            }),
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    await conversation._execute_tool(
        tool_name="routine_done",
        tool_input={"item": "Walk dog", "record": "Self Care"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    argv = captured["argv"]
    assert "Self Care" in argv
    assert "Walk dog" in argv
    # Record comes BEFORE item in the argv.
    rec_idx = argv.index("Self Care")
    item_idx = argv.index("Walk dog")
    assert rec_idx < item_idx


@pytest.mark.asyncio
async def test_routine_done_argv_shape_with_completed_at(
    tmp_path, monkeypatch,
):
    """``completed_at`` flag threads through as ``--completed-at <iso>``."""
    config = _salem_config(tmp_path)
    sess = _session()

    captured = {}

    def fake_run(argv, *args, **kwargs):
        captured["argv"] = argv
        return _fake_completed_proc(
            stdout=json.dumps({
                "ok": True, "kind": "success",
                "record": "Daily", "item": "Walk dog",
                "date": "2026-05-29", "appended": True,
            }),
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    await conversation._execute_tool(
        tool_name="routine_done",
        tool_input={
            "item": "Walk dog",
            "completed_at": "2026-05-29",
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    argv = captured["argv"]
    assert "--completed-at" in argv
    flag_idx = argv.index("--completed-at")
    assert argv[flag_idx + 1] == "2026-05-29"


# --- Env threading --------------------------------------------------------


@pytest.mark.asyncio
async def test_routine_done_env_carries_narrow_scope(tmp_path, monkeypatch):
    """``ALFRED_VAULT_SCOPE=talker_routine_completion`` is plumbed
    through to the subprocess env so future-when-CLI-goes-through-
    vault-edit, the scope gate fires correctly."""
    config = _salem_config(tmp_path)
    sess = _session()

    captured = {}

    def fake_run(argv, *args, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return _fake_completed_proc(
            stdout=json.dumps({
                "ok": True, "kind": "success",
                "record": "Daily", "item": "Walk dog",
                "date": "2026-05-30", "appended": True,
            }),
        )

    monkeypatch.setattr("subprocess.run", fake_run)

    await conversation._execute_tool(
        tool_name="routine_done",
        tool_input={"item": "Walk dog"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    env = captured["env"]
    assert env.get("ALFRED_VAULT_SCOPE") == "talker_routine_completion"


# --- Canary pass-through --------------------------------------------------


@pytest.mark.asyncio
async def test_routine_done_canary_success_passed_through(
    tmp_path, monkeypatch,
):
    """Success canary returned to the model verbatim."""
    config = _salem_config(tmp_path)
    sess = _session()

    payload = {
        "ok": True, "kind": "success",
        "record": "Self Care", "item": "Walk dog",
        "date": "2026-05-30", "appended": True,
        "path": "routine/Self Care.md",
    }

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed_proc(stdout=json.dumps(payload)),
    )

    result_str = await conversation._execute_tool(
        tool_name="routine_done",
        tool_input={"item": "Walk dog"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed == payload


@pytest.mark.asyncio
async def test_routine_done_canary_ambiguous_passed_through(
    tmp_path, monkeypatch,
):
    """Ambiguous canary with candidate list returned verbatim — the
    SKILL recognises ``kind=ambiguous_item`` and asks back."""
    config = _salem_config(tmp_path)
    sess = _session()

    payload = {
        "ok": False, "kind": "ambiguous_item",
        "item_text_input": "walked",
        "candidates": [
            {"record": "Self Care", "item": "Walk dog"},
            {"record": "Outdoor", "item": "Walk to coffee shop"},
        ],
        "error": "'walked' matches 2 routine items",
    }

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed_proc(
            stdout=json.dumps(payload), returncode=1,
        ),
    )

    result_str = await conversation._execute_tool(
        tool_name="routine_done",
        tool_input={"item": "walked"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["kind"] == "ambiguous_item"
    assert len(parsed["candidates"]) == 2


@pytest.mark.asyncio
async def test_routine_done_canary_idempotent_noop_passed_through(
    tmp_path, monkeypatch,
):
    """Idempotent noop canary returned (CLI exit 0; the canary
    distinguishes 'success' from 'no-op')."""
    config = _salem_config(tmp_path)
    sess = _session()

    payload = {
        "ok": True, "kind": "idempotent_noop",
        "record": "Daily", "item": "Walk dog",
        "date": "2026-05-30",
        "message": "Already logged: Daily / Walk dog @ 2026-05-30",
    }

    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **kw: _fake_completed_proc(
            stdout=json.dumps(payload), returncode=0,
        ),
    )

    result_str = await conversation._execute_tool(
        tool_name="routine_done",
        tool_input={"item": "Walk dog"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["kind"] == "idempotent_noop"
    assert parsed["ok"] is True


# --- Reversed-JSON-line scan (structlog pollution defense) -----------------


@pytest.mark.asyncio
async def test_routine_done_reversed_scan_returns_last_parseable_line(
    tmp_path, monkeypatch,
):
    """When stdout has a non-JSON log line ABOVE the JSON payload (the
    structlog pollution shape from migrate_tier_phase1.py), the
    reversed scan returns the LAST parseable JSON line. The payload
    always lands last per the CLI's response shape."""
    config = _salem_config(tmp_path)
    sess = _session()

    payload = {
        "ok": True, "kind": "success",
        "record": "Daily", "item": "Walk dog",
        "date": "2026-05-30", "appended": True,
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
        tool_name="routine_done",
        tool_input={"item": "Walk dog"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    # The JSON line (not the log line) was returned.
    assert parsed["kind"] == "success"


# --- Subprocess failure contract ------------------------------------------


@pytest.mark.asyncio
async def test_routine_done_nonzero_exit_without_canary_logged(
    tmp_path, monkeypatch, caplog,
):
    """Non-zero exit + no parseable JSON canary → logged per
    builder.md contract (stdout_tail sentinel + stderr) + returns
    structured subprocess_error."""
    import logging
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
            tool_name="routine_done",
            tool_input={"item": "Walk dog"},
            vault_path=str(tmp_path / "vault"),
            state=StateManager(config.session.state_path),
            session=sess,
            config=config,
        )
    # Structured error returned to model.
    parsed = json.loads(result_str)
    assert parsed["kind"] == "subprocess_error"
    assert "Import failed" in parsed["error"]

    # Failure log emitted per builder.md subprocess-contract.
    failure_events = [
        c for c in captured
        if c.get("event") == "talker.routine_done.nonzero_exit_no_canary"
    ]
    assert len(failure_events) == 1
    ev = failure_events[0]
    assert ev["code"] == 2
    assert "Import failed" in ev["stderr"]
    # stdout_tail sentinel emitted explicitly even when empty
    # (load-bearing per builder.md — "no diagnostic output at all"
    # signature is grep-able as stdout_tail='').
    assert "stdout_tail" in ev
    assert ev["stdout_tail"] == ""


@pytest.mark.asyncio
async def test_routine_done_timeout_returns_structured_error(
    tmp_path, monkeypatch,
):
    """``subprocess.TimeoutExpired`` → structured timeout error
    surfaced to the model (NOT propagated as an exception)."""
    import subprocess as sp

    config = _salem_config(tmp_path)
    sess = _session()

    def fake_run(*args, **kwargs):
        raise sp.TimeoutExpired(cmd=args[0], timeout=30)

    monkeypatch.setattr("subprocess.run", fake_run)

    result_str = await conversation._execute_tool(
        tool_name="routine_done",
        tool_input={"item": "Walk dog"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["kind"] == "timeout"
    assert "30s" in parsed["error"]


# --- Tool registration ----------------------------------------------------


def test_routine_done_in_salem_tool_set():
    """Salem (``talker``) tool set MUST include ``routine_done``;
    KAL-LE / Hypatia tool sets MUST NOT (the tool is Salem-only)."""
    talker_tools = conversation.VAULT_TOOLS_BY_SET["talker"]
    kalle_tools = conversation.VAULT_TOOLS_BY_SET["kalle"]
    hypatia_tools = conversation.VAULT_TOOLS_BY_SET["hypatia"]

    talker_names = {t["name"] for t in talker_tools}
    kalle_names = {t["name"] for t in kalle_tools}
    hypatia_names = {t["name"] for t in hypatia_tools}

    assert "routine_done" in talker_names
    assert "routine_done" not in kalle_names
    assert "routine_done" not in hypatia_names


def test_routine_done_schema_required_field():
    """Tool schema MUST require ``item``; ``record`` + ``completed_at``
    optional."""
    schema = conversation._ROUTINE_DONE_TOOL_SCHEMA["input_schema"]
    required = schema.get("required", [])
    assert required == ["item"]
    props = schema["properties"]
    assert "item" in props
    assert "record" in props
    assert "completed_at" in props
