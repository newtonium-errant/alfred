"""Tests for the conversation-loop inter-instance peer-tool dispatcher.

Phase A inter-instance comms — KAL-LE / Hypatia route ``query_canonical``
+ ``propose_*`` tools through the transport client to Salem. This file
covers the dispatcher glue in
:func:`alfred.telegram.conversation._dispatch_peer_inter_instance_tool`:

    * Tool-set gating — Salem (talker scope) refuses; KAL-LE / Hypatia
      both accept.
    * ``query_canonical`` — found vs not-found pass-through.
    * ``propose_event`` — created vs conflict pass-through.
    * ``propose_org`` / ``propose_location`` — queued (status=pending).
    * Schema errors at the dispatcher boundary (missing required input).

Tests stub out the transport client functions rather than spinning up
an aiohttp test app — the client-layer integration is covered in
``tests/test_peer_handlers.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

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


# --- Fixtures --------------------------------------------------------------


def _peer_config(tmp_path: Path, *, tool_set: str, name: str) -> TalkerConfig:
    """Build a TalkerConfig with the requested ``tool_set``."""
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
            name=name,
            canonical=name,
            tool_set=tool_set,
        ),
    )


def _session(chat_id: int = 1, session_id: str = "sess-peer-1") -> Session:
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    return Session(
        session_id=session_id,
        chat_id=chat_id,
        started_at=now,
        last_message_at=now,
        model="claude-opus-4-7",
    )


def _stub_transport_config(monkeypatch):
    """Stub out the transport config loader (peer dispatcher loads lazily).

    Accepts ``*args`` because the dispatcher (post-2026-05-01 P0 fix)
    threads a config path through to ``load_config(path)``; the stub
    ignores it because tests don't exercise the path-routing here —
    that's covered separately in
    ``test_peer_dispatcher_uses_config_path``.
    """
    monkeypatch.setattr(
        "alfred.transport.config.load_config",
        lambda *args, **kwargs: object(),  # opaque sentinel
    )


# --- Tool-set gating -------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_tool_refuses_on_talker_tool_set(tmp_path, monkeypatch):
    """Salem (tool_set='talker') must refuse all peer tools — Salem IS canonical."""
    _stub_transport_config(monkeypatch)
    config = _peer_config(tmp_path, tool_set="talker", name="Salem")

    # Even though we'd normally never expose these tools to Salem, a
    # prompt-injection or registry typo could deliver one — the dispatcher
    # is the second-line defence.
    result_str = await conversation._execute_tool(
        tool_name="propose_event",
        tool_input={
            "title": "x",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
        },
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert "not available on this instance" in parsed["error"]
    assert parsed["tool_set"] == "talker"


@pytest.mark.asyncio
async def test_peer_tool_accepts_kalle(tmp_path, monkeypatch):
    """KAL-LE tool_set passes the gate."""
    _stub_transport_config(monkeypatch)
    config = _peer_config(tmp_path, tool_set="kalle", name="KAL-LE")

    captured_kwargs: dict = {}

    async def fake_get_record(peer_name, record_type, name, **kwargs):
        captured_kwargs["peer"] = peer_name
        captured_kwargs["self_name"] = kwargs.get("self_name")
        return None  # not found

    monkeypatch.setattr(
        "alfred.transport.client.peer_get_canonical_record", fake_get_record,
    )

    result_str = await conversation._execute_tool(
        tool_name="query_canonical",
        tool_input={"record_type": "person", "name": "Andrew Newton"},
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["status"] == "not_found"
    assert captured_kwargs["peer"] == "salem"
    assert captured_kwargs["self_name"] == "kalle"


@pytest.mark.asyncio
async def test_peer_tool_accepts_hypatia(tmp_path, monkeypatch):
    """Hypatia tool_set passes the gate; self_name is 'hypatia'."""
    _stub_transport_config(monkeypatch)
    config = _peer_config(tmp_path, tool_set="hypatia", name="Hypatia")

    captured_kwargs: dict = {}

    async def fake_get_record(peer_name, record_type, name, **kwargs):
        captured_kwargs["self_name"] = kwargs.get("self_name")
        return {
            "type": "person",
            "name": "Andrew Newton",
            "frontmatter": {"name": "Andrew Newton", "email": "a@x"},
            "granted": ["name", "email"],
            "correlation_id": "cid-1",
        }

    monkeypatch.setattr(
        "alfred.transport.client.peer_get_canonical_record", fake_get_record,
    )

    result_str = await conversation._execute_tool(
        tool_name="query_canonical",
        tool_input={"record_type": "person", "name": "Andrew Newton"},
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["status"] == "found"
    assert parsed["frontmatter"]["email"] == "a@x"
    assert captured_kwargs["self_name"] == "hypatia"


# --- query_canonical -------------------------------------------------------


@pytest.mark.asyncio
async def test_query_canonical_missing_record_type(tmp_path, monkeypatch):
    _stub_transport_config(monkeypatch)
    config = _peer_config(tmp_path, tool_set="kalle", name="KAL-LE")

    result_str = await conversation._execute_tool(
        tool_name="query_canonical",
        tool_input={"name": "Andrew Newton"},
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert "record_type" in parsed["error"]


# --- propose_event ---------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_event_created_pass_through(tmp_path, monkeypatch):
    """propose_event happy path returns the server's {status: created} verbatim."""
    _stub_transport_config(monkeypatch)
    config = _peer_config(tmp_path, tool_set="hypatia", name="Hypatia")

    captured: dict = {}

    async def fake_propose_event(peer_name, **kwargs):
        captured["peer"] = peer_name
        captured.update(kwargs)
        return {
            "status": "created",
            "path": "event/VAC marketing call 2026-05-04.md",
            "correlation_id": "hypatia-propose-event-aaa111",
        }

    monkeypatch.setattr(
        "alfred.transport.client.peer_propose_event", fake_propose_event,
    )

    result_str = await conversation._execute_tool(
        tool_name="propose_event",
        tool_input={
            "title": "VAC marketing call",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "summary": "Q2 outreach plan",
            "origin_context": "marketing strategy session 2026-04-30",
        },
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["status"] == "created"
    assert "VAC marketing call" in parsed["path"]
    assert captured["peer"] == "salem"
    assert captured["self_name"] == "hypatia"
    assert captured["title"] == "VAC marketing call"
    assert captured["summary"] == "Q2 outreach plan"


@pytest.mark.asyncio
async def test_propose_event_conflict_pass_through(tmp_path, monkeypatch):
    """Conflict response is forwarded verbatim — caller surfaces inline."""
    _stub_transport_config(monkeypatch)
    config = _peer_config(tmp_path, tool_set="kalle", name="KAL-LE")

    async def fake_propose_event(peer_name, **kwargs):
        return {
            "status": "conflict",
            "conflicts": [
                {
                    "title": "EI Call",
                    "start": "2026-05-04T14:00:00-03:00",
                    "end": "2026-05-04T14:30:00-03:00",
                    "path": "event/EI Call 2026-05-04.md",
                },
            ],
            "correlation_id": "kal-le-propose-event-bbb222",
        }

    monkeypatch.setattr(
        "alfred.transport.client.peer_propose_event", fake_propose_event,
    )

    result_str = await conversation._execute_tool(
        tool_name="propose_event",
        tool_input={
            "title": "Conflicting Call",
            "start": "2026-05-04T14:15:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
        },
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["status"] == "conflict"
    assert len(parsed["conflicts"]) == 1
    assert parsed["conflicts"][0]["title"] == "EI Call"


@pytest.mark.asyncio
async def test_propose_event_missing_title(tmp_path, monkeypatch):
    """Schema error at dispatcher boundary — never reaches transport."""
    _stub_transport_config(monkeypatch)
    config = _peer_config(tmp_path, tool_set="kalle", name="KAL-LE")

    call_count = {"n": 0}

    async def fake_propose_event(peer_name, **kwargs):
        call_count["n"] += 1
        return {}

    monkeypatch.setattr(
        "alfred.transport.client.peer_propose_event", fake_propose_event,
    )

    result_str = await conversation._execute_tool(
        tool_name="propose_event",
        tool_input={
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
        },
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert "title" in parsed["error"]
    assert call_count["n"] == 0


# --- propose_org / propose_location / propose_person ----------------------


@pytest.mark.asyncio
async def test_propose_org_pass_through(tmp_path, monkeypatch):
    _stub_transport_config(monkeypatch)
    config = _peer_config(tmp_path, tool_set="kalle", name="KAL-LE")

    captured: dict = {}

    async def fake_propose_record(peer_name, record_type, name, **kwargs):
        captured["peer"] = peer_name
        captured["type"] = record_type
        captured["name"] = name
        captured["fields"] = kwargs.get("proposed_fields")
        captured["self_name"] = kwargs.get("self_name")
        return {
            "status": "pending",
            "correlation_id": "kal-le-propose-org-xxx",
        }

    monkeypatch.setattr(
        "alfred.transport.client.peer_propose_canonical_record",
        fake_propose_record,
    )

    result_str = await conversation._execute_tool(
        tool_name="propose_org",
        tool_input={
            "name": "Aftermath Labs Inc",
            "fields": {"description": "Andrew's consulting practice"},
            "source": "KAL-LE commit message",
        },
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["status"] == "pending"
    assert captured["type"] == "org"
    assert captured["name"] == "Aftermath Labs Inc"
    assert captured["fields"]["description"] == "Andrew's consulting practice"
    assert captured["self_name"] == "kalle"


@pytest.mark.asyncio
async def test_propose_location_pass_through(tmp_path, monkeypatch):
    _stub_transport_config(monkeypatch)
    config = _peer_config(tmp_path, tool_set="hypatia", name="Hypatia")

    captured: dict = {}

    async def fake_propose_record(peer_name, record_type, name, **kwargs):
        captured["type"] = record_type
        captured["self_name"] = kwargs.get("self_name")
        return {"status": "pending", "correlation_id": "z"}

    monkeypatch.setattr(
        "alfred.transport.client.peer_propose_canonical_record",
        fake_propose_record,
    )

    result_str = await conversation._execute_tool(
        tool_name="propose_location",
        tool_input={"name": "Halifax Convention Centre"},
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["status"] == "pending"
    assert captured["type"] == "location"
    assert captured["self_name"] == "hypatia"


@pytest.mark.asyncio
async def test_propose_person_pass_through(tmp_path, monkeypatch):
    """propose_person also routes via the generic record dispatcher."""
    _stub_transport_config(monkeypatch)
    config = _peer_config(tmp_path, tool_set="kalle", name="KAL-LE")

    captured: dict = {}

    async def fake_propose_record(peer_name, record_type, name, **kwargs):
        captured["type"] = record_type
        return {"status": "pending", "correlation_id": "p"}

    monkeypatch.setattr(
        "alfred.transport.client.peer_propose_canonical_record",
        fake_propose_record,
    )

    result_str = await conversation._execute_tool(
        tool_name="propose_person",
        tool_input={"name": "Elena Brighton"},
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )
    parsed = json.loads(result_str)
    assert parsed["status"] == "pending"
    assert captured["type"] == "person"


# --- Tool surface registration --------------------------------------------


def test_kalle_tool_set_includes_inter_instance_tools():
    """KAL-LE's tool list exposes the 5 peer tools."""
    names = {t["name"] for t in conversation.tools_for_set("kalle")}
    assert "query_canonical" in names
    assert "propose_person" in names
    assert "propose_org" in names
    assert "propose_location" in names
    assert "propose_event" in names
    # Plus existing kalle tools.
    assert "vault_search" in names
    assert "bash_exec" in names


def test_hypatia_tool_set_includes_inter_instance_tools():
    """Hypatia's tool list exposes the 5 peer tools (no bash_exec)."""
    names = {t["name"] for t in conversation.tools_for_set("hypatia")}
    assert "query_canonical" in names
    assert "propose_event" in names
    assert "propose_org" in names
    assert "propose_location" in names
    assert "propose_person" in names
    # Hypatia does NOT get bash_exec — that's KAL-LE-only.
    assert "bash_exec" not in names


def test_talker_tool_set_excludes_inter_instance_tools():
    """Salem (talker) must NOT see the inter-instance tools — it IS canonical."""
    names = {t["name"] for t in conversation.tools_for_set("talker")}
    assert "query_canonical" not in names
    assert "propose_person" not in names
    assert "propose_org" not in names
    assert "propose_location" not in names
    assert "propose_event" not in names


# --- P0 regression: dispatcher routes load_transport_config to the correct path
# Before the 2026-05-01 fix, the dispatcher called ``load_transport_config()``
# with no path, defaulting to ``"config.yaml"`` regardless of which config
# file the daemon was started with. A Hypatia daemon launched with
# ``--config config.hypatia.yaml`` silently re-read Salem's config and
# returned ``transport_error: unknown peer 'salem'`` for every peer tool
# call. The fix stamps the resolved path onto ``TalkerConfig.config_path``
# at load time and the dispatcher threads it into ``load_transport_config``.


@pytest.mark.asyncio
async def test_peer_dispatcher_uses_config_path_from_talker_config(
    tmp_path, monkeypatch,
):
    """Dispatcher must load transport config from ``TalkerConfig.config_path``,
    NOT from the default ``config.yaml``. P0 regression — see commit log."""
    # Build a TalkerConfig pointing at a NON-default path.
    fake_config_path = str(tmp_path / "config.hypatia.yaml")
    config = _peer_config(tmp_path, tool_set="hypatia", name="Hypatia")
    config.config_path = fake_config_path

    captured_paths: list = []

    def fake_load_transport_config(path="config.yaml"):
        captured_paths.append(path)
        # Return a transport-config-shaped object — we never actually
        # call into the transport layer because we stub the client too.
        return object()

    monkeypatch.setattr(
        "alfred.transport.config.load_config",
        fake_load_transport_config,
    )

    async def fake_get_record(peer_name, record_type, name, **kwargs):
        return None

    monkeypatch.setattr(
        "alfred.transport.client.peer_get_canonical_record",
        fake_get_record,
    )

    await conversation._execute_tool(
        tool_name="query_canonical",
        tool_input={"record_type": "person", "name": "Andrew Newton"},
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )

    assert len(captured_paths) == 1, (
        "load_transport_config called wrong number of times"
    )
    assert captured_paths[0] == fake_config_path, (
        f"dispatcher passed {captured_paths[0]!r} — expected the "
        f"TalkerConfig.config_path {fake_config_path!r}; the P0 "
        f"'unknown peer salem' bug is back"
    )


@pytest.mark.asyncio
async def test_peer_dispatcher_falls_back_to_default_when_config_path_unset(
    tmp_path, monkeypatch,
):
    """If ``TalkerConfig.config_path`` is None (e.g. test fixtures that
    don't go through the CLI), the dispatcher falls back to the legacy
    default ``"config.yaml"``. Backward-compat guard."""
    config = _peer_config(tmp_path, tool_set="hypatia", name="Hypatia")
    # Default — no path stamped.
    assert config.config_path is None

    captured_paths: list = []

    def fake_load_transport_config(path="config.yaml"):
        captured_paths.append(path)
        return object()

    monkeypatch.setattr(
        "alfred.transport.config.load_config",
        fake_load_transport_config,
    )

    async def fake_get_record(peer_name, record_type, name, **kwargs):
        return None

    monkeypatch.setattr(
        "alfred.transport.client.peer_get_canonical_record",
        fake_get_record,
    )

    await conversation._execute_tool(
        tool_name="query_canonical",
        tool_input={"record_type": "person", "name": "Andrew Newton"},
        vault_path=str(tmp_path / "vault"),
        state=None,
        session=_session(),
        config=config,
    )

    assert captured_paths == ["config.yaml"]


def test_talker_config_load_config_stamps_path(tmp_path):
    """``load_config(path)`` populates ``TalkerConfig.config_path`` with
    the resolved absolute path. Tests the load-side half of the fix."""
    from alfred.telegram.config import load_config

    config_file = tmp_path / "config.test.yaml"
    config_file.write_text(
        "telegram:\n"
        "  bot_token: 'DUMMY_TEST_TOKEN'\n"
        "  instance:\n"
        "    name: TestBot\n"
        "vault:\n"
        "  path: /tmp/v\n",
        encoding="utf-8",
    )

    cfg = load_config(config_file)
    assert cfg.config_path == str(config_file.resolve())


def test_talker_config_load_from_unified_picks_up_synthetic_path():
    """``load_from_unified`` reads ``_config_path`` from the raw dict —
    set by the CLI before handing raw to the orchestrator. Tests the
    multiprocessing-pickle path where the path can't be a function arg."""
    from alfred.telegram.config import load_from_unified

    raw = {
        "_config_path": "/etc/alfred/config.hypatia.yaml",
        "telegram": {
            "bot_token": "DUMMY_TEST_TOKEN",
            "instance": {"name": "Hypatia"},
        },
        "vault": {"path": "/tmp/v"},
    }
    cfg = load_from_unified(raw)
    assert cfg.config_path == "/etc/alfred/config.hypatia.yaml"


def test_talker_config_load_from_unified_no_synthetic_path_keeps_none():
    """Without ``_config_path`` in raw, ``config_path`` stays None —
    backward compat for tests that build raw dicts manually."""
    from alfred.telegram.config import load_from_unified

    raw = {
        "telegram": {
            "bot_token": "DUMMY_TEST_TOKEN",
            "instance": {"name": "TestBot"},
        },
        "vault": {"path": "/tmp/v"},
    }
    cfg = load_from_unified(raw)
    assert cfg.config_path is None
