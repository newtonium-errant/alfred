"""Tests for the conversation-loop ``routine_undone`` dispatcher (un-log, 2026-06).

The inverse of ``routine_done``. Subprocess-invokes ``alfred routine undone``
and routes on the structured ``kind`` canary (unlogged / not_logged / …). These
mock ``subprocess.run`` — they pin the dispatcher-glue shape only (the CLI
itself is tested in ``tests/routine/test_undone.py``):

  * Tool-set gating — KAL-LE / Hypatia refused; Salem accepted.
  * Argument validation — non-empty ``item`` required.
  * Argv shape — ``[python, -m, alfred, routine, undone, ...args, --json]``
    (NEVER ``alfred.cli`` per the 2026-05-28 silent-no-op lesson).
  * ``--date`` threading when supplied.
  * Env threading — ``ALFRED_VAULT_SCOPE=talker_routine_completion`` (the same
    narrow completion-only scope as routine_done; un-log touches only
    completion_log).
  * Canary pass-through — JSON kind returned verbatim.
  * Failure contract — non-zero exit WITHOUT a canary → structured error.
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


def _config(tmp_path: Path, *, name: str, tool_set: str) -> TalkerConfig:
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
        instance=InstanceConfig(name=name, tool_set=tool_set),
    )


def _salem(tmp_path: Path) -> TalkerConfig:
    return _config(tmp_path, name="Salem", tool_set="talker")


def _session() -> Session:
    now = datetime(2026, 6, 28, tzinfo=timezone.utc)
    return Session(
        session_id="sess-1", chat_id=1, started_at=now,
        last_message_at=now, model="claude-opus-4-7",
    )


def _fake_proc(*, stdout: str = "", stderr: str = "", returncode: int = 0):
    proc = MagicMock()
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


async def _dispatch(config, tmp_path, tool_input):
    return await conversation._execute_tool(
        tool_name="routine_undone",
        tool_input=tool_input,
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=_session(),
        config=config,
    )


# --- tool-set gating -------------------------------------------------------


@pytest.mark.asyncio
async def test_undone_refuses_on_kalle(tmp_path, monkeypatch):
    config = _config(tmp_path, name="KAL-LE", tool_set="kalle")
    calls = {"n": 0}
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), _fake_proc())[1],
    )
    parsed = json.loads(await _dispatch(config, tmp_path, {"item": "Walk dog"}))
    assert "routine_undone is Salem-only" in parsed.get("error", "")
    assert parsed.get("tool_set") == "kalle"
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_undone_refuses_on_hypatia(tmp_path, monkeypatch):
    config = _config(tmp_path, name="Hypatia", tool_set="hypatia")
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _fake_proc())
    parsed = json.loads(await _dispatch(config, tmp_path, {"item": "Walk dog"}))
    assert "routine_undone is Salem-only" in parsed.get("error", "")


# --- argument validation ---------------------------------------------------


@pytest.mark.asyncio
async def test_undone_rejects_empty_item(tmp_path):
    config = _salem(tmp_path)
    parsed = json.loads(await _dispatch(config, tmp_path, {"item": ""}))
    assert "non-empty 'item'" in parsed.get("error", "")


# --- argv + env shape ------------------------------------------------------


@pytest.mark.asyncio
async def test_undone_argv_item_only(tmp_path, monkeypatch):
    config = _salem(tmp_path)
    captured: dict = {}

    def fake_run(argv, *a, **k):
        captured["argv"] = argv
        captured["env"] = k.get("env", {})
        return _fake_proc(stdout=json.dumps({"kind": "unlogged", "removed": True}))

    monkeypatch.setattr("subprocess.run", fake_run)
    await _dispatch(config, tmp_path, {"item": "Walk dog"})

    argv = captured["argv"]
    m = argv.index("-m")
    assert argv[m + 1] == "alfred"  # NOT alfred.cli (silent-no-op lesson)
    assert argv[m + 2: m + 4] == ["routine", "undone"]
    assert "Walk dog" in argv
    assert "--json" in argv
    assert "--date" not in argv  # omitted → CLI defaults to today
    # narrow completion-only scope (un-log touches only completion_log).
    assert captured["env"].get("ALFRED_VAULT_SCOPE") == "talker_routine_completion"


@pytest.mark.asyncio
async def test_undone_argv_with_record_and_date(tmp_path, monkeypatch):
    config = _salem(tmp_path)
    captured: dict = {}

    def fake_run(argv, *a, **k):
        captured["argv"] = argv
        return _fake_proc(stdout=json.dumps({"kind": "unlogged"}))

    monkeypatch.setattr("subprocess.run", fake_run)
    await _dispatch(
        config, tmp_path,
        {"item": "Walk dog", "record": "Self Care", "date": "2026-06-27"},
    )

    argv = captured["argv"]
    # record precedes item; --date threaded.
    assert argv.index("Self Care") < argv.index("Walk dog")
    assert "--date" in argv
    assert argv[argv.index("--date") + 1] == "2026-06-27"


# --- canary pass-through + failure contract --------------------------------


@pytest.mark.asyncio
async def test_undone_passes_canary_verbatim(tmp_path, monkeypatch):
    config = _salem(tmp_path)
    canary = {"kind": "not_logged", "removed": False, "item": "Walk dog"}
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: _fake_proc(stdout=json.dumps(canary)),
    )
    parsed = json.loads(await _dispatch(config, tmp_path, {"item": "Walk dog"}))
    assert parsed["kind"] == "not_logged"
    assert parsed["removed"] is False


@pytest.mark.asyncio
async def test_undone_nonzero_without_canary_is_error(tmp_path, monkeypatch):
    config = _salem(tmp_path)
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: _fake_proc(stdout="", stderr="boom", returncode=1),
    )
    parsed = json.loads(await _dispatch(config, tmp_path, {"item": "Walk dog"}))
    assert "failed without canary" in parsed.get("error", "")
