"""Requester side of the NL lane — ``peer_nl_query`` + ``peer_ask_canonical``.

Pins the wire contract (kind=query_nl payload shape, P precedence,
mailbox await) and the talker tool surface (tool-set gating — requester
instances only; Salem, the canonical authority, never sees the tool).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from alfred.transport.client import peer_nl_query
from alfred.transport.exceptions import TransportRejected
from alfred.transport.peers import register_response


# ---------------------------------------------------------------------------
# peer_nl_query — wire contract
# ---------------------------------------------------------------------------


def _capture_send(monkeypatch, *, reply: dict[str, Any] | None = None,
                  raise_exc: Exception | None = None) -> list[dict[str, Any]]:
    """Patch client.peer_send; optionally pre-deliver the mailbox reply."""
    captured: list[dict[str, Any]] = []

    async def _fake_peer_send(
        peer_name: str, kind: str, payload: dict[str, Any], **kwargs: Any,
    ) -> dict[str, Any]:
        captured.append({
            "peer_name": peer_name, "kind": kind, "payload": payload,
            "correlation_id": kwargs.get("correlation_id"),
        })
        if raise_exc is not None:
            raise raise_exc
        if reply is not None:
            # Holder replies before the requester starts awaiting —
            # the orphan buffer hands it to await_response immediately.
            register_response(kwargs["correlation_id"], reply)
        return {"status": "accepted"}

    import alfred.transport.client as client_mod
    monkeypatch.setattr(client_mod, "peer_send", _fake_peer_send)
    return captured


async def test_peer_nl_query_sends_query_nl_kind_with_question(monkeypatch) -> None:
    reply = {"status": "ok", "answer": "May 26.", "outcome": "answered"}
    captured = _capture_send(monkeypatch, reply=reply)

    result = await peer_nl_query(
        "salem",
        "When did Andrew last meet Ben?",
        record_type_hint="event",
        self_name="hypatia",
        correlation_id="cid-req-1",
        timeout=1.0,
    )

    assert len(captured) == 1
    sent = captured[0]
    assert sent["peer_name"] == "salem"
    assert sent["kind"] == "query_nl"  # Decision A: new kind, not a mode flag
    assert sent["correlation_id"] == "cid-req-1"
    assert sent["payload"] == {
        "question": "When did Andrew last meet Ben?",
        "precedence": "P",
        "record_type_hint": "event",
    }
    assert result == reply


async def test_peer_nl_query_omits_absent_hint(monkeypatch) -> None:
    captured = _capture_send(
        monkeypatch, reply={"status": "ok", "answer": "x", "outcome": "answered"},
    )
    await peer_nl_query(
        "salem", "anything?", self_name="hypatia",
        correlation_id="cid-req-2", timeout=1.0,
    )
    assert "record_type_hint" not in captured[0]["payload"]


async def test_peer_nl_query_times_out_explicitly(monkeypatch) -> None:
    _capture_send(monkeypatch)  # holder never replies
    result = await peer_nl_query(
        "salem", "anything?", self_name="hypatia",
        correlation_id="cid-req-3", timeout=0.01,
    )
    assert result == {"status": "timeout", "correlation_id": "cid-req-3"}


async def test_peer_nl_query_surfaces_sync_rejection(monkeypatch) -> None:
    """An older holder (no query_nl in its kind enum) 400s the send —
    surfaced immediately as send_rejected, not a silent mailbox timeout."""
    _capture_send(monkeypatch, raise_exc=TransportRejected(
        "HTTP 400 from /peer/send: kind must be message | query | ...",
        status_code=400,
    ))
    result = await peer_nl_query(
        "salem", "anything?", self_name="hypatia",
        correlation_id="cid-req-4", timeout=1.0,
    )
    assert result["status"] == "failed"
    assert result["code"] == "send_rejected"
    assert result["http_status"] == 400
    assert result["correlation_id"] == "cid-req-4"


# ---------------------------------------------------------------------------
# Talker tool surface — registry + gating + input validation
# ---------------------------------------------------------------------------


def test_peer_ask_tool_registered_for_requester_instances_only() -> None:
    """Registry pin: hypatia + kalle carry peer_ask_canonical; Salem
    (the canonical authority — the HOLDER) must never see it."""
    from alfred.telegram.conversation import VAULT_TOOLS_BY_SET

    def _names(set_name: str) -> set[str]:
        return {t["name"] for t in VAULT_TOOLS_BY_SET[set_name]}

    assert "peer_ask_canonical" in _names("hypatia")
    assert "peer_ask_canonical" in _names("kalle")
    assert "peer_ask_canonical" not in _names("talker")


def test_peer_ask_tool_description_steers_structured_first() -> None:
    """The SKILL-layer contract starts at the tool description: structured
    lookup stays the default; the NL lane is for genuinely fuzzy asks."""
    from alfred.telegram.conversation import _PEER_ASK_CANONICAL_TOOL

    desc = _PEER_ASK_CANONICAL_TOOL["description"]
    assert "peer_search_canonical" in desc
    assert "fuzzy" in desc.lower()
    assert _PEER_ASK_CANONICAL_TOOL["input_schema"]["required"] == ["question"]


async def test_dispatch_gates_on_tool_set() -> None:
    """Salem-shaped config (talker tool_set) is refused at the dispatcher."""
    from alfred.telegram.conversation import _dispatch_peer_inter_instance_tool

    config = SimpleNamespace(
        instance=SimpleNamespace(tool_set="talker"),
        config_path="config.yaml",
    )
    session = SimpleNamespace(session_id="s-1")
    out = await _dispatch_peer_inter_instance_tool(
        tool_name="peer_ask_canonical",
        tool_input={"question": "anything?"},
        session=session,
        config=config,  # type: ignore[arg-type]
    )
    assert "not available on this instance" in out


async def test_ask_handler_validates_question() -> None:
    from alfred.telegram.conversation import _peer_tool_ask_canonical

    out = await _peer_tool_ask_canonical({}, None, "hypatia")
    assert "requires a 'question'" in out
    out = await _peer_tool_ask_canonical(
        {"question": "  q?  ", "record_type_hint": 42}, None, "hypatia",
    )
    assert "record_type_hint must be a string" in out


async def test_ask_handler_forwards_to_client(monkeypatch) -> None:
    from alfred.telegram.conversation import _peer_tool_ask_canonical

    seen: dict[str, Any] = {}

    async def _fake_nl_query(peer_name: str, question: str, **kwargs: Any):
        seen.update({"peer": peer_name, "question": question, **kwargs})
        return {"status": "ok", "answer": "May 26.", "outcome": "answered"}

    monkeypatch.setattr(
        "alfred.transport.client.peer_nl_query", _fake_nl_query,
    )
    out = await _peer_tool_ask_canonical(
        {"question": "  When did Andrew last meet Ben?  ",
         "record_type_hint": "event"},
        transport_config=None,
        self_name="hypatia",
    )
    assert seen["peer"] == "salem"
    assert seen["question"] == "When did Andrew last meet Ben?"  # stripped
    assert seen["record_type_hint"] == "event"
    assert seen["precedence"] == "P"
    assert seen["self_name"] == "hypatia"
    assert '"answer": "May 26."' in out
