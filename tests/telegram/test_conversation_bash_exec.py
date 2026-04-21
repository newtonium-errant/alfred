"""Tests for the conversation-loop ``bash_exec`` dispatcher (KAL-LE hotfix c4).

Context: c5 shipped the ``bash_exec`` tool schema and c6 shipped the
safety-critical executor, but the conversation-loop dispatch case was
deferred to dogfood. Without it KAL-LE reports
``bash_exec isn't available in this environment`` — the tool is in the
schema but not callable.

This file covers the dispatcher glue in
:mod:`alfred.telegram.conversation`:

    * Happy path — tool_use block with ``command`` + ``cwd`` routes
      through to :func:`bash_exec.execute` and the tool_result shape
      matches the executor's return dict.
    * Tool-set refusal — a Salem-shaped config
      (``instance.tool_set == "talker"``) must refuse the tool call
      explicitly. Executor is NOT called.
    * Disabled in config — when ``bash_exec`` config is absent the
      dispatcher refuses with a clear message.
    * Non-zero exit logging — subprocess-failure contract per
      builder.md: ``talker.bash_exec.nonzero_exit`` with the
      ``stdout_tail`` sentinel field emitted.
    * Dry-run pass-through — ``dry_run=True`` from the model reaches
      the executor unchanged.
    * End-to-end smoke — real ``execute`` call under a tmp repo that's
      been redirected via ``$HOME``, single allowlisted command
      (``ls init.py``) completes with exit_code=0.

The tool-list exposure (KAL-LE instance sees bash_exec in its tool
schema, Salem does not) is tested in the existing
``test_multi_instance_config.py`` + ``test_kalle_scope.py`` alongside
the ``VAULT_TOOLS_BY_SET`` registry.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.telegram import conversation
from alfred.telegram.config import (
    AnthropicConfig,
    BashExecConfig,
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


def _kalle_config(tmp_path: Path, audit_path: Path) -> TalkerConfig:
    """Build a TalkerConfig shaped like KAL-LE's live config.

    ``tool_set == "kalle"`` + a populated :class:`BashExecConfig` — both
    required for the dispatcher to reach the executor.
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
        instance=InstanceConfig(
            name="KAL-LE",
            canonical="K.A.L.L.E.",
            tool_set="kalle",
        ),
        bash_exec=BashExecConfig(audit_path=str(audit_path)),
    )


def _salem_config(tmp_path: Path) -> TalkerConfig:
    """Salem-shaped config: ``tool_set == "talker"``, no bash_exec section."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    return TalkerConfig(
        bot_token="x",
        allowed_users=[1],
        primary_users=["person/Test"],
        anthropic=AnthropicConfig(api_key="x", model="claude-sonnet-4-6"),
        stt=STTConfig(api_key="x", model="whisper-large-v3"),
        session=SessionConfig(state_path=str(tmp_path / "state.json")),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name="Salem", tool_set="talker"),
    )


def _session(chat_id: int = 1, session_id: str = "sess-1") -> Session:
    now = datetime(2026, 4, 21, tzinfo=timezone.utc)
    return Session(
        session_id=session_id,
        chat_id=chat_id,
        started_at=now,
        last_message_at=now,
        model="claude-opus-4-7",
    )


# --- Dispatcher tests ------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_happy_path_calls_executor(tmp_path, monkeypatch):
    """KAL-LE config + bash_exec tool_use → executor invoked with parsed args."""
    audit = tmp_path / "bash_exec.jsonl"
    config = _kalle_config(tmp_path, audit)
    sess = _session()

    captured_kwargs: dict = {}

    async def fake_execute(**kwargs):
        captured_kwargs.update(kwargs)
        return {
            "exit_code": 0,
            "stdout": "pytest 8.4.0",
            "stderr": "",
            "duration_ms": 42,
            "truncated": False,
            "dry_run": False,
            "reason": "",
            "argv": ["pytest", "--version"],
            "cwd": kwargs["cwd"],
        }

    monkeypatch.setattr(
        "alfred.telegram.bash_exec.execute", fake_execute,
    )

    result_str = await conversation._execute_tool(
        tool_name="bash_exec",
        tool_input={
            "command": "pytest --version",
            "cwd": "/home/andrew/aftermath-lab",
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )

    # Executor received the model's args verbatim, plus the config's
    # audit path + the session id.
    assert captured_kwargs["command"] == "pytest --version"
    assert captured_kwargs["cwd"] == "/home/andrew/aftermath-lab"
    assert captured_kwargs["dry_run"] is False
    assert captured_kwargs["audit_path"] == str(audit)
    assert captured_kwargs["session_id"] == "sess-1"

    # The tool_result content is the JSON-serialised executor return dict
    # so the model can reason about exit_code / stdout / stderr directly.
    parsed = json.loads(result_str)
    assert parsed["exit_code"] == 0
    assert parsed["stdout"] == "pytest 8.4.0"
    assert parsed["reason"] == ""


@pytest.mark.asyncio
async def test_dispatch_refuses_on_talker_tool_set(tmp_path, monkeypatch):
    """Salem's talker tool_set must refuse bash_exec even if a call arrives."""
    config = _salem_config(tmp_path)
    sess = _session()

    call_count = {"n": 0}

    async def fake_execute(**kwargs):
        call_count["n"] += 1
        return {"exit_code": 0, "stdout": "", "stderr": "", "reason": ""}

    monkeypatch.setattr(
        "alfred.telegram.bash_exec.execute", fake_execute,
    )

    result_str = await conversation._execute_tool(
        tool_name="bash_exec",
        tool_input={
            "command": "ls",
            "cwd": "/home/andrew/aftermath-lab",
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )

    parsed = json.loads(result_str)
    assert parsed["error"] == "bash_exec not available on this instance"
    assert parsed["tool_set"] == "talker"
    # Executor was NOT called — the refusal fires before any dispatch.
    assert call_count["n"] == 0


@pytest.mark.asyncio
async def test_dispatch_refuses_when_bash_exec_config_missing(
    tmp_path, monkeypatch,
):
    """kalle tool_set but no bash_exec section → structured refusal."""
    # KAL-LE-shaped instance but no BashExecConfig — simulates a botched
    # config.kalle.yaml that forgot the ``bash_exec:`` block.
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    config = TalkerConfig(
        bot_token="x",
        allowed_users=[1],
        anthropic=AnthropicConfig(api_key="x"),
        stt=STTConfig(api_key="x"),
        session=SessionConfig(state_path=str(tmp_path / "state.json")),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "t.log")),
        instance=InstanceConfig(name="KAL-LE", tool_set="kalle"),
        # bash_exec deliberately left as None.
    )
    sess = _session()

    called = {"n": 0}

    async def fake_execute(**kwargs):
        called["n"] += 1
        return {}

    monkeypatch.setattr(
        "alfred.telegram.bash_exec.execute", fake_execute,
    )

    result_str = await conversation._execute_tool(
        tool_name="bash_exec",
        tool_input={"command": "ls", "cwd": "/home/andrew/aftermath-lab"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["error"] == "bash_exec disabled in config"
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_dispatch_refuses_when_config_is_none(tmp_path, monkeypatch):
    """No config passed at all → fall through to refusal (backwards-compat)."""
    sess = _session()
    called = {"n": 0}

    async def fake_execute(**kwargs):
        called["n"] += 1
        return {}

    monkeypatch.setattr(
        "alfred.telegram.bash_exec.execute", fake_execute,
    )

    result_str = await conversation._execute_tool(
        tool_name="bash_exec",
        tool_input={"command": "ls", "cwd": "/x"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(str(tmp_path / "state.json")),
        session=sess,
        config=None,
    )
    parsed = json.loads(result_str)
    # config=None has no instance.tool_set → refusal on tool-set gate.
    assert "error" in parsed
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_dispatch_logs_nonzero_exit_with_contract_fields(
    tmp_path, monkeypatch,
):
    """Non-zero exit with no gate reason → subprocess-failure-contract log.

    Asserts the log event carries the ``stdout_tail`` sentinel per
    builder.md. Executor-level refusals (reason != "") suppress this
    log because the executor already emits its own gate-specific warning.

    Rather than wrestling with structlog / caplog plumbing (structlog's
    ConsoleRenderer target depends on setup_logging state and logger
    caching, both of which vary between isolated and full-suite runs),
    we intercept the log event at the structlog processor chain. The
    ``_record_events`` processor appends every log call's event dict to
    a list the test asserts against — deterministic, render-agnostic.
    """
    import structlog

    recorded: list[dict] = []

    def _record_events(logger, method_name, event_dict):
        recorded.append(dict(event_dict))
        return event_dict

    # Save + restore the processor chain so we don't leak config
    # into other tests. Inserting before the renderer means we see the
    # fully-bound event dict (with all the structured kwargs).
    original = structlog.get_config()
    try:
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.stdlib.add_log_level,
                _record_events,
                structlog.processors.KeyValueRenderer(),
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=False,
        )

        audit = tmp_path / "bash_exec.jsonl"
        config = _kalle_config(tmp_path, audit)
        sess = _session(session_id="fail-1")

        async def fake_execute(**kwargs):
            # Command ran to completion with a non-zero status —
            # reason="" means "the executor didn't refuse; the command
            # itself failed".
            return {
                "exit_code": 1,
                "stdout": "collected 5 items\n1 failed",
                "stderr": "AssertionError at line 42",
                "duration_ms": 1234,
                "truncated": False,
                "dry_run": False,
                "reason": "",
                "argv": ["pytest", "tests/broken"],
                "cwd": kwargs["cwd"],
            }

        monkeypatch.setattr(
            "alfred.telegram.bash_exec.execute", fake_execute,
        )

        # Rebind the module-level cached logger so it picks up our
        # processor chain. The ``log`` attribute in conversation.py
        # was bound at import time against the default structlog
        # config; cache_logger_on_first_use=False in our test config
        # ensures subsequent .get_logger() calls use the new chain.
        from alfred.telegram import conversation as _conv
        _conv.log = structlog.get_logger("alfred.telegram.conversation")

        result_str = await conversation._execute_tool(
            tool_name="bash_exec",
            tool_input={
                "command": "pytest tests/broken",
                "cwd": "/home/andrew/aftermath-lab",
            },
            vault_path=str(tmp_path / "vault"),
            state=StateManager(config.session.state_path),
            session=sess,
            config=config,
        )

        events = [
            e for e in recorded
            if e.get("event") == "talker.bash_exec.nonzero_exit"
        ]
        assert len(events) == 1, (
            "expected one talker.bash_exec.nonzero_exit event, got "
            f"{[e.get('event') for e in recorded]!r}"
        )
        ev = events[0]
        # Subprocess-failure-contract fields per builder.md.
        assert ev["code"] == 1
        assert ev["stderr"] == "AssertionError at line 42"
        # The ``stdout_tail`` key MUST be present (load-bearing sentinel
        # — grep-able even when empty).
        assert "stdout_tail" in ev
        assert ev["stdout_tail"] == "collected 5 items\n1 failed"
        assert ev["session_id"] == "fail-1"
        assert ev["chat_id"] == 1

        # The executor's shape is still returned to the model.
        parsed = json.loads(result_str)
        assert parsed["exit_code"] == 1

    finally:
        structlog.configure(**{
            k: original[k] for k in (
                "processors", "wrapper_class", "context_class",
                "logger_factory", "cache_logger_on_first_use",
            ) if k in original
        })


@pytest.mark.asyncio
async def test_dispatch_does_not_log_nonzero_when_executor_refused(
    tmp_path, monkeypatch,
):
    """Executor refusals (reason != '') suppress the contract log.

    The executor already emits its own ``talker.bash_exec.denylist`` /
    ``.cwd_rejected`` / ``.allowlist_miss`` / ``.timeout`` events — the
    dispatcher must NOT add a noisy duplicate. Uses the same structlog
    processor-intercept pattern as the positive test above.
    """
    import structlog

    recorded: list[dict] = []

    def _record_events(logger, method_name, event_dict):
        recorded.append(dict(event_dict))
        return event_dict

    original = structlog.get_config()
    try:
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.stdlib.add_log_level,
                _record_events,
                structlog.processors.KeyValueRenderer(),
            ],
            wrapper_class=structlog.stdlib.BoundLogger,
            context_class=dict,
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=False,
        )

        audit = tmp_path / "bash_exec.jsonl"
        config = _kalle_config(tmp_path, audit)
        sess = _session()

        async def fake_execute(**kwargs):
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": "denied: contains 'git push'",
                "duration_ms": 0,
                "truncated": False,
                "dry_run": False,
                "reason": "denylist:git push",
                "argv": [],
                "cwd": kwargs["cwd"],
            }

        monkeypatch.setattr(
            "alfred.telegram.bash_exec.execute", fake_execute,
        )

        from alfred.telegram import conversation as _conv
        _conv.log = structlog.get_logger("alfred.telegram.conversation")

        await conversation._execute_tool(
            tool_name="bash_exec",
            tool_input={
                "command": "git push origin master",
                "cwd": "/home/andrew/aftermath-lab",
            },
            vault_path=str(tmp_path / "vault"),
            state=StateManager(config.session.state_path),
            session=sess,
            config=config,
        )

        hits = [
            e for e in recorded
            if e.get("event") == "talker.bash_exec.nonzero_exit"
        ]
        assert hits == [], (
            f"unexpected contract log when executor refused: {hits!r}"
        )

    finally:
        structlog.configure(**{
            k: original[k] for k in (
                "processors", "wrapper_class", "context_class",
                "logger_factory", "cache_logger_on_first_use",
            ) if k in original
        })


@pytest.mark.asyncio
async def test_dispatch_passes_dry_run_through_to_executor(
    tmp_path, monkeypatch,
):
    """Model ``dry_run: True`` reaches the executor unchanged."""
    audit = tmp_path / "bash_exec.jsonl"
    config = _kalle_config(tmp_path, audit)
    sess = _session()

    seen: dict = {}

    async def fake_execute(**kwargs):
        seen.update(kwargs)
        return {
            "exit_code": 0, "stdout": "", "stderr": "",
            "duration_ms": 0, "truncated": False,
            "dry_run": True, "reason": "dry_run",
            "argv": ["ls"], "cwd": kwargs["cwd"],
        }

    monkeypatch.setattr(
        "alfred.telegram.bash_exec.execute", fake_execute,
    )

    await conversation._execute_tool(
        tool_name="bash_exec",
        tool_input={
            "command": "ls",
            "cwd": "/home/andrew/aftermath-lab",
            "dry_run": True,
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    assert seen["dry_run"] is True


@pytest.mark.asyncio
async def test_dispatch_rejects_empty_command(tmp_path, monkeypatch):
    """Empty ``command`` arg → refusal before executor call."""
    audit = tmp_path / "bash_exec.jsonl"
    config = _kalle_config(tmp_path, audit)
    sess = _session()

    called = {"n": 0}

    async def fake_execute(**kwargs):
        called["n"] += 1
        return {}

    monkeypatch.setattr("alfred.telegram.bash_exec.execute", fake_execute)

    result_str = await conversation._execute_tool(
        tool_name="bash_exec",
        tool_input={"command": "  ", "cwd": "/home/andrew/aftermath-lab"},
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert "non-empty 'command'" in parsed["error"]
    assert called["n"] == 0


# --- Run-turn integration --------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_uses_kalle_tool_set(tmp_path, monkeypatch):
    """run_turn on a kalle-configured instance sends bash_exec in its tools list."""
    audit = tmp_path / "bash_exec.jsonl"
    config = _kalle_config(tmp_path, audit)
    sess = _session()

    # Seed a plain text end_turn so we don't get into a tool_use loop.
    class _Blk:
        def __init__(self, t, txt=""):
            self.type = t
            self.text = txt
        def model_dump(self):
            return {"type": self.type, "text": self.text}

    class _Resp:
        stop_reason = "end_turn"
        content = [_Blk("text", "ready")]

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=_Resp())

    state_mgr = StateManager(config.session.state_path)
    await conversation.run_turn(
        client=client,
        state=state_mgr,
        session=sess,
        user_message="hi",
        config=config,
        vault_context_str="",
        system_prompt="sys",
    )

    kwargs = client.messages.create.call_args.kwargs
    tool_names = [t["name"] for t in kwargs["tools"]]
    assert "bash_exec" in tool_names, (
        f"KAL-LE tool list missing bash_exec: {tool_names}"
    )
    # Vault tools are still there too (KAL-LE can still curate records).
    assert "vault_search" in tool_names
    assert "vault_read" in tool_names
    assert "vault_create" in tool_names
    assert "vault_edit" in tool_names


@pytest.mark.asyncio
async def test_run_turn_talker_instance_excludes_bash_exec(
    tmp_path, monkeypatch,
):
    """Salem's talker tool_set MUST NOT surface bash_exec to the model.

    Belt-and-braces: the executor refuses if invoked, but the first
    line of defence is not handing the tool to the model at all.
    """
    config = _salem_config(tmp_path)
    sess = _session()

    class _Blk:
        def __init__(self, t, txt=""):
            self.type = t
            self.text = txt
        def model_dump(self):
            return {"type": self.type, "text": self.text}

    class _Resp:
        stop_reason = "end_turn"
        content = [_Blk("text", "ok")]

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=_Resp())

    state_mgr = StateManager(config.session.state_path)
    await conversation.run_turn(
        client=client,
        state=state_mgr,
        session=sess,
        user_message="hi",
        config=config,
        vault_context_str="",
        system_prompt="sys",
    )

    kwargs = client.messages.create.call_args.kwargs
    tool_names = [t["name"] for t in kwargs["tools"]]
    assert "bash_exec" not in tool_names, (
        f"Salem's tool list leaked bash_exec: {tool_names}"
    )
    assert "vault_search" in tool_names  # vault tools still present


# --- End-to-end smoke ------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_ls_through_dispatcher(tmp_path, monkeypatch):
    """Smoke: real ``bash_exec.execute`` round-trip through the dispatcher.

    Redirect ``$HOME`` to a tmp dir, scaffold an ``aftermath-lab/`` repo
    with one file, run a single allowlisted ``ls`` command. Asserts the
    executor actually runs (exit_code=0, stdout contains the filename)
    and the audit log is written at the config-declared path.
    """
    home = tmp_path / "home"
    home.mkdir()
    repo = home / "aftermath-lab"
    repo.mkdir()
    (repo / "init.py").write_text("# marker\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    audit = tmp_path / "kalle_bash_exec.jsonl"
    config = _kalle_config(tmp_path, audit)
    sess = _session(session_id="smoke-1")

    result_str = await conversation._execute_tool(
        tool_name="bash_exec",
        tool_input={
            "command": "ls init.py",
            "cwd": str(repo),
        },
        vault_path=str(tmp_path / "vault"),
        state=StateManager(config.session.state_path),
        session=sess,
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["exit_code"] == 0, f"unexpected failure: {parsed}"
    assert "init.py" in parsed["stdout"]
    assert parsed["reason"] == ""

    # Audit log landed at the KAL-LE-configured path.
    assert audit.exists(), "audit log not written"
    lines = audit.read_text(encoding="utf-8").strip().splitlines()
    assert lines, "audit log empty"
    entry = json.loads(lines[-1])
    assert entry["command"] == "ls init.py"
    assert entry["exit_code"] == 0
    assert entry["session_id"] == "smoke-1"
