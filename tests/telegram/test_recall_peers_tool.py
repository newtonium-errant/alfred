"""Tests for the recall ask-side dispatch (#20 S2) — ``recall_peers`` tool.

Covers :func:`alfred.telegram.conversation._dispatch_recall_peers_tool` and
the surface gating in ``tools_for_set`` / ``_resolve_recall_ask_enabled_for_run_turn``:

    * Fan-out — asks every configured peer in deterministic order, merges
      per-instance-attributed results.
    * Explicit ask — a single named peer target.
    * Type-narrowing — the asker never widens beyond its interest set;
      no-overlap skips the peer without contacting it.
    * Graceful degradation — peer-down (offline) vs wrong_peer (misconfig)
      vs unavailable, each distinct, none crashing the turn.
    * ILB — reached-but-empty and all-unreachable are both explicit.
    * No-write — the recall path never touches a vault write op.
    * Surface gating — the tool is shown ONLY when recall.ask is configured.

Tests stub the transport client + config loader rather than spinning up an
aiohttp app (client integration is covered in tests/test_peer_client.py).
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
from alfred.transport.config import (
    RecallAskConfig,
    RecallAskPeerRules,
    RecallConfig,
    TransportConfig,
)
from alfred.transport.exceptions import (
    TransportRejected,
    TransportServerDown,
    TransportUnavailable,
)


def _talker_config(tmp_path: Path, *, name: str, tool_set: str = "talker") -> TalkerConfig:
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
        instance=InstanceConfig(name=name, canonical=name, tool_set=tool_set),
    )


def _session() -> Session:
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return Session(
        session_id="sess-recall-1",
        chat_id=1,
        started_at=now,
        last_message_at=now,
        model="claude-opus-4-7",
    )


def _stub_transport(monkeypatch, ask_peers: dict[str, list[str]]) -> None:
    """Stub load_config → a TransportConfig whose recall.ask has ``ask_peers``."""
    tconfig = TransportConfig(
        recall=RecallConfig(
            ask=RecallAskConfig(
                peers={
                    name: RecallAskPeerRules(types=list(types))
                    for name, types in ask_peers.items()
                }
            )
        )
    )
    monkeypatch.setattr(
        "alfred.transport.config.load_config", lambda *a, **k: tconfig,
    )


def _match(name: str, source: str) -> dict:
    return {
        "type": "person",
        "name": name,
        "snippet": f"snippet about {name}",
        "truncated": False,
        "record_pointer": {"instance": source, "path": f"person/{name}.md"},
    }


async def _run(tmp_path, monkeypatch, *, name="Salem", tool_input=None):
    return await conversation._dispatch_recall_peers_tool(
        tool_input=tool_input or {"query": "Andrew"},
        session=_session(),
        config=_talker_config(tmp_path, name=name),
    )


# ---------------------------------------------------------------------------
# Fan-out + attribution + deterministic order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_peers_fans_out_deterministic_order(tmp_path, monkeypatch):
    _stub_transport(monkeypatch, {"kal-le": ["person"], "hypatia": ["note"]})
    calls: list[str] = []

    async def fake_recall(peer, query, *, types=None, config=None, self_name, correlation_id=None):
        calls.append(peer)
        return {"status": "ok", "instance": peer.upper(), "count": 1,
                "matches": [_match("Andrew Newton", peer.upper())]}

    monkeypatch.setattr("alfred.transport.client.peer_recall", fake_recall)
    out = json.loads(await _run(tmp_path, monkeypatch))

    # Deterministic (sorted) order: hypatia before kal-le.
    assert calls == ["hypatia", "kal-le"]
    assert out["status"] == "ok"
    assert out["total_matches"] == 2
    assert [r["instance"] for r in out["results"]] == ["HYPATIA", "KAL-LE"]
    # Attribution: each match carries its source instance path pointer.
    assert out["results"][0]["matches"][0]["path"] == "person/Andrew Newton.md"


@pytest.mark.asyncio
async def test_recall_peers_self_name_is_lowercased_instance(tmp_path, monkeypatch):
    _stub_transport(monkeypatch, {"kal-le": ["person"]})
    captured: dict = {}

    async def fake_recall(peer, query, *, types=None, config=None, self_name, correlation_id=None):
        captured["self_name"] = self_name
        return {"status": "ok", "instance": "KAL-LE", "count": 0, "matches": []}

    monkeypatch.setattr("alfred.transport.client.peer_recall", fake_recall)
    await _run(tmp_path, monkeypatch, name="Salem")
    assert captured["self_name"] == "salem"


@pytest.mark.asyncio
async def test_recall_peers_explicit_single_target(tmp_path, monkeypatch):
    _stub_transport(monkeypatch, {"kal-le": ["person"], "hypatia": ["note"]})
    calls: list[str] = []

    async def fake_recall(peer, query, *, types=None, config=None, self_name, correlation_id=None):
        calls.append(peer)
        return {"status": "ok", "instance": peer, "count": 0, "matches": []}

    monkeypatch.setattr("alfred.transport.client.peer_recall", fake_recall)
    out = json.loads(await _run(
        tmp_path, monkeypatch, tool_input={"query": "x", "peer": "kal-le"},
    ))
    assert calls == ["kal-le"]
    assert out["reached"] == ["kal-le"]


@pytest.mark.asyncio
async def test_recall_peers_unknown_target_errors(tmp_path, monkeypatch):
    _stub_transport(monkeypatch, {"kal-le": ["person"]})
    out = json.loads(await _run(
        tmp_path, monkeypatch, tool_input={"query": "x", "peer": "nope"},
    ))
    assert "not a configured recall peer" in out["error"]
    assert out["configured_peers"] == ["kal-le"]


# ---------------------------------------------------------------------------
# Type-narrowing — asker never widens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_peers_narrows_types_to_interest(tmp_path, monkeypatch):
    _stub_transport(monkeypatch, {"kal-le": ["person", "project"]})
    captured: dict = {}

    async def fake_recall(peer, query, *, types=None, config=None, self_name, correlation_id=None):
        captured["types"] = types
        return {"status": "ok", "instance": peer, "count": 0, "matches": []}

    monkeypatch.setattr("alfred.transport.client.peer_recall", fake_recall)
    # Model asks for [person, task] but interest is [person, project] →
    # sent = [person] (task dropped; asker cannot widen to task).
    await _run(tmp_path, monkeypatch,
               tool_input={"query": "x", "types": ["person", "task"]})
    assert captured["types"] == ["person"]


@pytest.mark.asyncio
async def test_recall_peers_default_sends_interest_set(tmp_path, monkeypatch):
    _stub_transport(monkeypatch, {"kal-le": ["person", "project"]})
    captured: dict = {}

    async def fake_recall(peer, query, *, types=None, config=None, self_name, correlation_id=None):
        captured["types"] = types
        return {"status": "ok", "instance": peer, "count": 0, "matches": []}

    monkeypatch.setattr("alfred.transport.client.peer_recall", fake_recall)
    await _run(tmp_path, monkeypatch)  # no model types → send interest set
    assert captured["types"] == ["person", "project"]


@pytest.mark.asyncio
async def test_recall_peers_no_type_overlap_skips_peer(tmp_path, monkeypatch):
    _stub_transport(monkeypatch, {"kal-le": ["person"]})
    calls: list[str] = []

    async def fake_recall(peer, query, *, types=None, config=None, self_name, correlation_id=None):
        calls.append(peer)
        return {"status": "ok", "instance": peer, "count": 0, "matches": []}

    monkeypatch.setattr("alfred.transport.client.peer_recall", fake_recall)
    # Model asks ONLY for a type outside the interest set → peer not contacted
    # (avoids the empty-list-omitted→widen-to-full trap).
    out = json.loads(await _run(
        tmp_path, monkeypatch, tool_input={"query": "x", "types": ["task"]},
    ))
    assert calls == []  # never contacted
    assert out["results"][0]["note"] == "no_type_overlap"
    assert out["total_matches"] == 0


# ---------------------------------------------------------------------------
# Graceful degradation — distinct failure buckets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_peers_peer_down_is_unreachable(tmp_path, monkeypatch):
    _stub_transport(monkeypatch, {"kal-le": ["person"]})

    async def fake_recall(*a, **k):
        raise TransportServerDown("connection refused")

    monkeypatch.setattr("alfred.transport.client.peer_recall", fake_recall)
    out = json.loads(await _run(tmp_path, monkeypatch))
    assert out["status"] == "ok"  # turn does not crash
    assert out["reached"] == []
    assert out["unreachable"] == [{"instance": "kal-le", "reason": "offline"}]


@pytest.mark.asyncio
async def test_recall_peers_wrong_peer_is_misconfig(tmp_path, monkeypatch):
    _stub_transport(monkeypatch, {"kal-le": ["person"]})

    async def fake_recall(*a, **k):
        raise TransportRejected("HTTP 401", status_code=401, body='{"reason":"wrong_peer"}')

    monkeypatch.setattr("alfred.transport.client.peer_recall", fake_recall)
    import structlog
    with structlog.testing.capture_logs() as captured:
        out = json.loads(await _run(tmp_path, monkeypatch))
    assert out["misconfigured"] == [{"instance": "kal-le", "reason": "wrong_peer"}]
    assert out["unreachable"] == []
    logs = [c for c in captured if c.get("event") == "talker.recall_peers.misconfigured"]
    assert logs and logs[0]["reason"] == "wrong_peer"


@pytest.mark.asyncio
async def test_recall_peers_unavailable_bucket(tmp_path, monkeypatch):
    _stub_transport(monkeypatch, {"kal-le": ["person"]})

    async def fake_recall(*a, **k):
        raise TransportUnavailable("HTTP 503")

    monkeypatch.setattr("alfred.transport.client.peer_recall", fake_recall)
    out = json.loads(await _run(tmp_path, monkeypatch))
    assert out["unreachable"] == [{"instance": "kal-le", "reason": "unavailable"}]


@pytest.mark.asyncio
async def test_recall_peers_mixed_degradation_one_up_one_down(tmp_path, monkeypatch):
    _stub_transport(monkeypatch, {"kal-le": ["person"], "hypatia": ["note"]})

    async def fake_recall(peer, query, *, types=None, config=None, self_name, correlation_id=None):
        if peer == "hypatia":
            raise TransportServerDown("down")
        return {"status": "ok", "instance": "KAL-LE", "count": 1,
                "matches": [_match("Andrew", "KAL-LE")]}

    monkeypatch.setattr("alfred.transport.client.peer_recall", fake_recall)
    out = json.loads(await _run(tmp_path, monkeypatch))
    # kal-le answered; hypatia down — both surfaced, neither silently dropped.
    assert out["reached"] == ["kal-le"]
    assert out["unreachable"] == [{"instance": "hypatia", "reason": "offline"}]
    assert out["total_matches"] == 1


# ---------------------------------------------------------------------------
# ILB — reached-but-empty vs all-unreachable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_peers_all_empty_is_honest(tmp_path, monkeypatch):
    _stub_transport(monkeypatch, {"kal-le": ["person"], "hypatia": ["note"]})

    async def fake_recall(peer, query, *, types=None, config=None, self_name, correlation_id=None):
        return {"status": "ok", "instance": peer, "count": 0, "matches": []}

    monkeypatch.setattr("alfred.transport.client.peer_recall", fake_recall)
    out = json.loads(await _run(tmp_path, monkeypatch))
    assert out["total_matches"] == 0
    assert sorted(out["reached"]) == ["hypatia", "kal-le"]  # reached, just empty
    assert out["unreachable"] == []


# ---------------------------------------------------------------------------
# No-write — the recall path never touches a vault mutation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_peers_never_writes_vault(tmp_path, monkeypatch):
    _stub_transport(monkeypatch, {"kal-le": ["person"]})

    async def fake_recall(peer, query, *, types=None, config=None, self_name, correlation_id=None):
        return {"status": "ok", "instance": peer, "count": 1,
                "matches": [_match("Andrew", "KAL-LE")]}

    monkeypatch.setattr("alfred.transport.client.peer_recall", fake_recall)

    # Any vault write op firing on the recall path reddens this pin.
    def _boom(*a, **k):
        raise AssertionError("recall path attempted a vault write (no-write ruling)")

    import alfred.vault.ops as ops
    for op in ("vault_create", "vault_edit", "vault_move", "vault_delete"):
        monkeypatch.setattr(ops, op, _boom, raising=False)

    out = json.loads(await _run(tmp_path, monkeypatch))
    assert out["status"] == "ok"
    assert out["total_matches"] == 1  # completed without any write


# ---------------------------------------------------------------------------
# Input validation + fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_peers_requires_query(tmp_path, monkeypatch):
    _stub_transport(monkeypatch, {"kal-le": ["person"]})
    out = json.loads(await _run(tmp_path, monkeypatch, tool_input={"query": "  "}))
    assert "non-empty 'query'" in out["error"]


@pytest.mark.asyncio
async def test_recall_peers_no_ask_config_refuses(tmp_path, monkeypatch):
    _stub_transport(monkeypatch, {})  # empty ask edge list
    out = json.loads(await _run(tmp_path, monkeypatch))
    assert "not configured on this instance" in out["error"]


# ---------------------------------------------------------------------------
# Surface gating — tool shown only when recall.ask configured
# ---------------------------------------------------------------------------


def test_recall_peers_tool_surfaced_when_enabled():
    tools = conversation.tools_for_set("talker", recall_ask_enabled=True)
    names = {t["name"] for t in tools}
    assert "recall_peers" in names


def test_recall_peers_tool_absent_when_disabled():
    tools = conversation.tools_for_set("talker", recall_ask_enabled=False)
    names = {t["name"] for t in tools}
    assert "recall_peers" not in names


def test_resolve_recall_ask_enabled_true_when_configured(tmp_path, monkeypatch):
    _stub_transport(monkeypatch, {"kal-le": ["person"]})
    config = _talker_config(tmp_path, name="Salem")
    assert conversation._resolve_recall_ask_enabled_for_run_turn(config) is True


def test_resolve_recall_ask_enabled_false_when_empty(tmp_path, monkeypatch):
    _stub_transport(monkeypatch, {})
    config = _talker_config(tmp_path, name="Salem")
    assert conversation._resolve_recall_ask_enabled_for_run_turn(config) is False


def test_resolve_recall_ask_enabled_false_on_load_error(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("config exploded")

    monkeypatch.setattr("alfred.transport.config.load_config", _boom)
    config = _talker_config(tmp_path, name="Salem")
    # Fail-closed: any load error → tool not surfaced.
    assert conversation._resolve_recall_ask_enabled_for_run_turn(config) is False
