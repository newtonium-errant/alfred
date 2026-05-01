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
    """Stub out the transport config loader (peer dispatcher loads lazily)."""
    monkeypatch.setattr(
        "alfred.transport.config.load_config",
        lambda: object(),  # opaque sentinel — the dispatcher passes it through
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
