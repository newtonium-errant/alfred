"""Tests for the VERA ticket forwarder (pipeline c4).

``run_forward_once`` is exercised with a monkeypatched ``peer_send``
(capturing kwargs) but REAL vault ops — the uid-write and link-back
edits go through the actual ``vault_edit`` under the narrow
``vera_forwarder`` scope, pinning that the c2 4-field allowlist
({ticket_uid, github_issue, github_url, forwarded_at}) actually
suffices for the forwarder's whole write surface.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import frontmatter as fm_lib
import pytest
import structlog

from alfred.transport.exceptions import (
    TransportRejected,
    TransportServerDown,
)
from alfred.transport.ticket_forward import (
    TicketForwardConfig,
    TicketForwardEntry,
    TicketForwardState,
    load_ticket_forward_config,
    mint_ticket_uid,
    run_forward_once,
    scan_tickets,
)


@pytest.fixture(autouse=True)
def _clean_vault_env(monkeypatch):  # type: ignore[no-untyped-def]
    """Dispatcher env-var test-hygiene contract (CLAUDE.md)."""
    for var in (
        "ALFRED_VAULT_PATH",
        "ALFRED_VAULT_SCOPE",
        "ALFRED_VAULT_SESSION",
        "ALFRED_VAULT_AUDIT_LOG",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_ticket(
    vault: Path,
    name: str,
    *,
    status: str = "open",
    created: str = "2026-06-10",
    uid: str | None = None,
) -> Path:
    ticket_dir = vault / "ticket"
    ticket_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        "type: ticket",
        f"title: {name}",
        "ticket_type: bug",
        "reporter: Ben",
        "area: checkout",
        f"status: {status}",
        f"created: {created}",
    ]
    if uid:
        lines.append(f"ticket_uid: {uid}")
    lines += ["---", "", "## Repro", "1. Step one", ""]
    path = ticket_dir / f"{name}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _config(tmp_path: Path) -> TicketForwardConfig:
    return TicketForwardConfig(
        enabled=True,
        self_name="vera",
        target_peer="kalle",
        interval_minutes=15,
        vault_path=str(tmp_path / "vault"),
        state_path=str(tmp_path / "ticket_forward_state.json"),
    )


def _raw(tmp_path: Path) -> dict[str, Any]:
    return {"vault": {"path": str(tmp_path / "vault")}}


def _created_ack(issue_number: int = 7) -> dict[str, Any]:
    return {
        "status": "created",
        "issue_number": issue_number,
        "issue_url": f"https://github.com/acme/site/issues/{issue_number}",
        "kalle_relpath": "ticket/X.md",
        "correlation_id": "cid-1",
    }


class FakePeerSend:
    """Async stand-in for ``client.peer_send`` capturing call kwargs.

    ``script`` items are consumed in order — an exception instance is
    raised, anything else returned. When the script is exhausted the
    ``default`` ack is returned.
    """

    def __init__(
        self,
        script: list[Any] | None = None,
        default: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.script = list(script or [])
        self.default = default if default is not None else _created_ack()

    async def __call__(
        self,
        peer_name: str,
        kind: str,
        payload: dict[str, Any],
        *,
        config: Any = None,
        self_name: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append({
            "peer_name": peer_name,
            "kind": kind,
            "payload": payload,
            "self_name": self_name,
        })
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return dict(self.default)


def _patch_peer_send(monkeypatch, fake: FakePeerSend) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "alfred.transport.ticket_forward.peer_send", fake,
    )


def _log_events(captured: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [c for c in captured if c.get("event") == event]


# ---------------------------------------------------------------------------
# UID mint — stability pin
# ---------------------------------------------------------------------------


def test_mint_ticket_uid_stable_format_and_inputs():  # type: ignore[no-untyped-def]
    uid1 = mint_ticket_uid("ticket/Login broken.md", "2026-06-10")
    uid2 = mint_ticket_uid("ticket/Login broken.md", "2026-06-10")
    # Pure: same inputs → same uid, every call.
    assert uid1 == uid2
    # Pinned shape: vera-YYYYMMDD-<8 hex>.
    assert re.fullmatch(r"vera-\d{8}-[0-9a-f]{8}", uid1)
    assert uid1.startswith("vera-20260610-")
    # Wire-format contract: minted uids pass the intake's schema-gate
    # format pin (peer_handlers rejects payload.ticket_uid outside it).
    from alfred.transport.ticket_intake import TICKET_UID_RE
    assert TICKET_UID_RE.fullmatch(uid1)
    # Pinned derivation: sha256("relpath|created")[:8].
    expected_hash = hashlib.sha256(
        b"ticket/Login broken.md|2026-06-10",
    ).hexdigest()[:8]
    assert uid1.endswith(expected_hash)
    # Different relpath / created → different uid.
    assert mint_ticket_uid("ticket/Other.md", "2026-06-10") != uid1
    assert mint_ticket_uid("ticket/Login broken.md", "2026-06-11") != uid1


def test_mint_ticket_uid_created_fallback_to_today():  # type: ignore[no-untyped-def]
    uid = mint_ticket_uid("ticket/X.md", "")
    assert re.fullmatch(r"vera-\d{8}-[0-9a-f]{8}", uid)


# ---------------------------------------------------------------------------
# Scan selection
# ---------------------------------------------------------------------------


def test_scan_selection_rules(tmp_path):  # type: ignore[no-untyped-def]
    vault = tmp_path / "vault"
    _write_ticket(vault, "Open no uid")
    _write_ticket(vault, "Open linked", uid="vera-20260610-aaaaaaaa")
    _write_ticket(vault, "Open pending", uid="vera-20260610-bbbbbbbb")
    _write_ticket(vault, "In progress", status="in_progress")
    _write_ticket(vault, "Resolved", status="resolved")
    # Malformed frontmatter — must be skipped + logged, never fatal.
    (vault / "ticket" / "broken.md").write_text(
        "---\nfoo: [unclosed\n---\nbody\n", encoding="utf-8",
    )

    state = TicketForwardState(path=tmp_path / "state.json")
    state.entries["vera-20260610-aaaaaaaa"] = TicketForwardEntry(
        relpath="ticket/Open linked.md", issue_number=5,
    )
    state.entries["vera-20260610-bbbbbbbb"] = TicketForwardEntry(
        relpath="ticket/Open pending.md", attempts=1,
    )

    with structlog.testing.capture_logs() as captured:
        scanned, eligible = scan_tickets(vault, state)

    assert scanned == 5  # the malformed file never parses to a ticket
    names = {item["relpath"] for item in eligible}
    assert names == {"ticket/Open no uid.md", "ticket/Open pending.md"}
    # Pending (state entry without issue_number) stays eligible.
    by_relpath = {item["relpath"]: item for item in eligible}
    assert by_relpath["ticket/Open pending.md"]["uid"] == "vera-20260610-bbbbbbbb"
    assert by_relpath["ticket/Open no uid.md"]["uid"] == ""

    parse_failed = _log_events(captured, "ticket_forward.scan_parse_failed")
    assert len(parse_failed) == 1
    assert "broken.md" in parse_failed[0]["path"]


# ---------------------------------------------------------------------------
# First forward — uid mint + write through REAL vault ops, link-back
# ---------------------------------------------------------------------------


async def test_first_forward_end_to_end(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    vault = tmp_path / "vault"
    record_path = _write_ticket(vault, "Login broken")
    fake = FakePeerSend()
    _patch_peer_send(monkeypatch, fake)
    config = _config(tmp_path)

    with structlog.testing.capture_logs() as captured:
        result = await run_forward_once(config, _raw(tmp_path))

    assert result["scanned"] == 1
    assert result["eligible"] == 1
    assert result["forwarded"] == 1
    assert result["failed"] == 0
    assert result["pending"] == 0

    # peer_send called with kind=ticket + the payload contract.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["peer_name"] == "kalle"
    assert call["kind"] == "ticket"
    assert call["self_name"] == "vera"
    payload = call["payload"]
    assert payload["precedence"] == "R"
    assert payload["relpath"] == "ticket/Login broken.md"
    assert "## Repro" in payload["body"]
    expected_uid = mint_ticket_uid("ticket/Login broken.md", "2026-06-10")
    assert payload["ticket_uid"] == expected_uid
    # Full frontmatter travels, INCLUDING the freshly-minted uid, and
    # is JSON-serializable (PyYAML date objects sanitized).
    assert payload["frontmatter"]["ticket_uid"] == expected_uid
    assert payload["frontmatter"]["title"] == "Login broken"
    json.dumps(payload)  # must not raise

    # The uid + link-back landed in the RECORD through the real
    # vera_forwarder scope — pins that the c2 4-field allowlist
    # suffices end-to-end.
    post = fm_lib.load(str(record_path))
    assert post.metadata["ticket_uid"] == expected_uid
    assert post.metadata["github_issue"] == 7
    assert post.metadata["github_url"] == "https://github.com/acme/site/issues/7"
    assert post.metadata["forwarded_at"]
    assert post.metadata["status"] == "open"  # untouched

    # State linked.
    state = TicketForwardState.load(config.state_path)
    entry = state.entries[expected_uid]
    assert entry.relpath == "ticket/Login broken.md"
    assert entry.attempts == 1
    assert entry.issue_number == 7
    assert entry.first_forwarded_at

    linked = _log_events(captured, "ticket_forward.linked")
    assert len(linked) == 1
    assert linked[0]["uid"] == expected_uid
    assert linked[0]["issue_number"] == 7


async def test_existing_uid_reused_never_reminted(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    vault = tmp_path / "vault"
    existing_uid = "vera-20260101-cafe0123"
    record_path = _write_ticket(vault, "Pre-uid ticket", uid=existing_uid)
    fake = FakePeerSend()
    _patch_peer_send(monkeypatch, fake)

    await run_forward_once(_config(tmp_path), _raw(tmp_path))

    assert fake.calls[0]["payload"]["ticket_uid"] == existing_uid
    post = fm_lib.load(str(record_path))
    assert post.metadata["ticket_uid"] == existing_uid


# ---------------------------------------------------------------------------
# Pending ack — stays eligible, re-push IS the retry
# ---------------------------------------------------------------------------


async def test_pending_ack_stays_eligible_then_links(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    vault = tmp_path / "vault"
    record_path = _write_ticket(vault, "Pending ticket")
    fake = FakePeerSend(script=[{
        "status": "recorded_issue_pending",
        "kalle_relpath": "ticket/Pending ticket.md",
        "correlation_id": "cid-1",
    }])
    _patch_peer_send(monkeypatch, fake)
    config = _config(tmp_path)

    with structlog.testing.capture_logs() as captured:
        result = await run_forward_once(config, _raw(tmp_path))
    assert result["pending"] == 1
    assert result["forwarded"] == 0
    pending = _log_events(captured, "ticket_forward.issue_pending")
    assert len(pending) == 1
    assert pending[0]["kalle_relpath"] == "ticket/Pending ticket.md"

    expected_uid = mint_ticket_uid("ticket/Pending ticket.md", "2026-06-10")
    state = TicketForwardState.load(config.state_path)
    assert state.entries[expected_uid].issue_number is None
    assert state.entries[expected_uid].attempts == 1
    # No link-back fields yet.
    post = fm_lib.load(str(record_path))
    assert "github_issue" not in post.metadata

    # Next tick: script exhausted → default created ack → linked.
    result2 = await run_forward_once(config, _raw(tmp_path))
    assert result2["eligible"] == 1  # still eligible — this IS the retry
    assert result2["forwarded"] == 1
    state2 = TicketForwardState.load(config.state_path)
    assert state2.entries[expected_uid].issue_number == 7
    assert state2.entries[expected_uid].attempts == 2


# ---------------------------------------------------------------------------
# Peer not upgraded — 400 aborts the tick, queue intact
# ---------------------------------------------------------------------------


async def test_peer_not_upgraded_aborts_tick_keeps_queue(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    vault = tmp_path / "vault"
    _write_ticket(vault, "A ticket")
    _write_ticket(vault, "B ticket")
    fake = FakePeerSend(script=[
        TransportRejected(
            "HTTP 400 from /peer/send: schema_error",
            status_code=400,
            body='{"reason": "schema_error"}',
        ),
    ])
    _patch_peer_send(monkeypatch, fake)
    config = _config(tmp_path)

    with structlog.testing.capture_logs() as captured:
        result = await run_forward_once(config, _raw(tmp_path))

    # One push attempted, the rest aborted; all tickets stay queued.
    assert len(fake.calls) == 1
    assert result["aborted"] is True
    assert result["eligible"] == 2
    assert result["forwarded"] == 0
    warned = _log_events(captured, "ticket_forward.peer_not_upgraded")
    assert len(warned) == 1
    assert warned[0]["http_status"] == 400
    assert warned[0]["target_peer"] == "kalle"

    # Queue intact: next tick (peer upgraded — default ack) links BOTH.
    result2 = await run_forward_once(config, _raw(tmp_path))
    assert result2["forwarded"] == 2
    assert result2["aborted"] is False


# ---------------------------------------------------------------------------
# Push failure — isolated per ticket
# ---------------------------------------------------------------------------


async def test_push_failure_isolates_per_ticket(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    vault = tmp_path / "vault"
    _write_ticket(vault, "A ticket")
    _write_ticket(vault, "B ticket")
    fake = FakePeerSend(script=[
        TransportServerDown("could not reach kalle"),
    ])
    _patch_peer_send(monkeypatch, fake)

    with structlog.testing.capture_logs() as captured:
        result = await run_forward_once(_config(tmp_path), _raw(tmp_path))

    # First push failed, second still attempted and linked.
    assert len(fake.calls) == 2
    assert result["failed"] == 1
    assert result["forwarded"] == 1
    assert result["aborted"] is False
    failed = _log_events(captured, "ticket_forward.push_failed")
    assert len(failed) == 1
    assert failed[0]["error_type"] == "TransportServerDown"


# ---------------------------------------------------------------------------
# ILB — the tick log fires on zero-work ticks too
# ---------------------------------------------------------------------------


async def test_ilb_tick_logged_on_zero_work(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    (tmp_path / "vault").mkdir()  # no ticket/ dir at all
    fake = FakePeerSend()
    _patch_peer_send(monkeypatch, fake)

    with structlog.testing.capture_logs() as captured:
        result = await run_forward_once(_config(tmp_path), _raw(tmp_path))

    assert fake.calls == []
    ticks = _log_events(captured, "ticket_forward.tick")
    assert len(ticks) == 1
    assert ticks[0]["scanned"] == 0
    assert ticks[0]["eligible"] == 0
    assert ticks[0]["forwarded"] == 0
    assert ticks[0]["failed"] == 0
    assert result["scanned"] == 0
    # Explicit empty-state signal for the absent directory too.
    assert len(_log_events(captured, "ticket_forward.no_ticket_dir")) == 1


# ---------------------------------------------------------------------------
# State — schema tolerance + atomic save
# ---------------------------------------------------------------------------


def test_forward_state_schema_tolerance(tmp_path):  # type: ignore[no-untyped-def]
    path = tmp_path / "ticket_forward_state.json"
    path.write_text(
        json.dumps({
            "entries": {
                "vera-20260610-deadbeef": {
                    "relpath": "ticket/X.md",
                    "attempts": 3,
                    "issue_number": 9,
                    "field_from_the_future": "ignored",
                },
            },
        }),
        encoding="utf-8",
    )
    state = TicketForwardState.load(path)
    entry = state.entries["vera-20260610-deadbeef"]
    assert entry.attempts == 3
    assert entry.issue_number == 9


def test_forward_state_corrupt_starts_empty(tmp_path):  # type: ignore[no-untyped-def]
    path = tmp_path / "ticket_forward_state.json"
    path.write_text("{nope", encoding="utf-8")
    with structlog.testing.capture_logs() as captured:
        state = TicketForwardState.load(path)
    assert state.entries == {}
    failed = _log_events(captured, "ticket_forward.state_load_failed")
    assert len(failed) == 1


def test_forward_state_atomic_roundtrip(tmp_path):  # type: ignore[no-untyped-def]
    path = tmp_path / "data" / "ticket_forward_state.json"
    state = TicketForwardState(path=path)
    state.entries["uid-1"] = TicketForwardEntry(
        relpath="ticket/A.md", attempts=2,
    )
    state.save()
    assert path.exists()
    assert not path.with_suffix(".json.tmp").exists()
    assert TicketForwardState.load(path).entries["uid-1"].attempts == 2


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def test_forward_config_defaults_and_vault_fallback():  # type: ignore[no-untyped-def]
    cfg = load_ticket_forward_config({})
    assert cfg.enabled is False
    assert cfg.target_peer == "kalle"
    assert cfg.interval_minutes == 15
    assert cfg.state_path == "./data/ticket_forward_state.json"
    assert cfg.vault_path == ""

    cfg2 = load_ticket_forward_config({
        "vault": {"path": "/srv/vera-vault"},
        "ticket_forward": {"enabled": True, "self_name": "vera"},
    })
    assert cfg2.enabled is True
    assert cfg2.self_name == "vera"
    assert cfg2.vault_path == "/srv/vera-vault"  # unified fallback

    cfg3 = load_ticket_forward_config({
        "vault": {"path": "/srv/vera-vault"},
        "ticket_forward": {
            "enabled": True,
            "self_name": "vera",
            "target_peer": "kal-le",
            "interval_minutes": 5,
            "vault_path": "/elsewhere",
            "state": {"path": "/tmp/tf.json"},
        },
    })
    assert cfg3.target_peer == "kal-le"
    assert cfg3.interval_minutes == 5
    assert cfg3.vault_path == "/elsewhere"
    assert cfg3.state_path == "/tmp/tf.json"


# ---------------------------------------------------------------------------
# Orchestrator registration pins
# ---------------------------------------------------------------------------


class TestOrchestratorRegistration:
    def test_ticket_forward_runner_registered(self) -> None:
        from alfred import orchestrator
        assert "ticket_forward" in orchestrator.TOOL_RUNNERS
        assert (
            orchestrator.TOOL_RUNNERS["ticket_forward"].__name__
            == "_run_ticket_forward"
        )

    def test_ticket_forward_in_spawn_priority(self) -> None:
        from alfred import orchestrator
        assert "ticket_forward" in orchestrator.SPAWN_PRIORITY

    def test_runner_has_two_arg_signature(self) -> None:
        import inspect

        from alfred import orchestrator
        params = list(
            inspect.signature(
                orchestrator.TOOL_RUNNERS["ticket_forward"],
            ).parameters,
        )
        assert params == ["raw", "suppress_stdout"]


# ---------------------------------------------------------------------------
# CLI smoke — run-once through the real handler
# ---------------------------------------------------------------------------


def test_cli_run_once_smoke(tmp_path, monkeypatch, capsys):  # type: ignore[no-untyped-def]
    vault = tmp_path / "vault"
    _write_ticket(vault, "CLI ticket")
    fake = FakePeerSend()
    _patch_peer_send(monkeypatch, fake)

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        json.dumps({  # YAML is a JSON superset — keeps quoting trivial
            "vault": {"path": str(vault)},
            "logging": {"dir": str(tmp_path / "data")},
            "ticket_forward": {
                "enabled": True,
                "self_name": "vera",
                "target_peer": "kalle",
                "state": {"path": str(tmp_path / "tf_state.json")},
            },
        }),
        encoding="utf-8",
    )

    from alfred.cli import cmd_ticket_forward

    args = argparse.Namespace(
        config=str(config_path),
        json=True,
        ticket_forward_cmd="run-once",
    )
    with pytest.raises(SystemExit) as excinfo:
        cmd_ticket_forward(args)
    assert excinfo.value.code == 0

    out = capsys.readouterr().out
    result = json.loads(out)
    assert result["forwarded"] == 1
    assert result["results"][0]["outcome"] == "linked"
    assert len(fake.calls) == 1


def test_cli_status_smoke(tmp_path, monkeypatch, capsys):  # type: ignore[no-untyped-def]
    vault = tmp_path / "vault"
    _write_ticket(vault, "Status ticket")

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        json.dumps({
            "vault": {"path": str(vault)},
            "logging": {"dir": str(tmp_path / "data")},
            "ticket_forward": {
                "enabled": True,
                "self_name": "vera",
                "state": {"path": str(tmp_path / "tf_state.json")},
            },
        }),
        encoding="utf-8",
    )

    from alfred.cli import cmd_ticket_forward

    args = argparse.Namespace(
        config=str(config_path),
        json=True,
        ticket_forward_cmd="status",
    )
    with pytest.raises(SystemExit) as excinfo:
        cmd_ticket_forward(args)
    assert excinfo.value.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["enabled"] is True
    assert out["tickets_scanned"] == 1
    assert out["eligible_now"] == 1
    assert out["linked"] == 0
