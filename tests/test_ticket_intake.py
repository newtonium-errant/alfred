"""End-to-end tests for the kind=ticket KAL-LE intake (pipeline c3).

Harness mirrors tests/test_peer_handlers.py: build the real aiohttp
app via ``build_app``, register the intake with a FAKE github client
(same method names as ``GitHubOpsClient``), and POST /peer/send with
``kind=ticket`` through the real auth middleware. Vault writes go
through the REAL vault ops under scope ``kalle`` so the c2 type/scope
gates are exercised end-to-end.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import frontmatter as fm_lib
import httpx
import pytest
import structlog
from aiohttp.test_utils import TestClient

from alfred.integrations.github_ops import GitHubOpsConfig, read_github_audit
from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    CanonicalConfig,
    SchedulerConfig,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.peer_handlers import (
    register_instance_identity,
    register_ticket_intake,
    register_vault_path,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState
from alfred.transport.ticket_intake import (
    TicketIntakeConfig,
    TicketIntakeEntry,
    TicketIntakeState,
)


DUMMY_VERA_PEER_TOKEN = "DUMMY_VERA_PEER_TEST_TOKEN_PLACEHOLDER_NOT_REAL_0123456789"

TICKET_UID = "vera-20260611-abcd1234"


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
# Fakes + factories
# ---------------------------------------------------------------------------


class FakeGitHubClient:
    """Same method names + signatures as GitHubOpsClient's intake ops.

    Mutable failure injection: tests flip ``search_exc`` /
    ``create_exc`` between pushes to simulate GitHub recovering.
    """

    def __init__(
        self,
        audit_path: Path,
        *,
        search_result: dict[str, Any] | None = None,
        search_exc: BaseException | None = None,
        create_exc: BaseException | None = None,
        label_map: dict[str, str] | None = None,
    ) -> None:
        self.config = GitHubOpsConfig(
            repo="acme/site",
            pat="DUMMY_GITHUB_TEST_PAT",
            instance="KAL-LE",
            labels=["auto-fix"],
            label_map=(
                label_map
                if label_map is not None
                else {"bug": "bug", "high": "priority-high"}
            ),
            audit_log_path=str(audit_path),
        )
        self.search_result = search_result
        self.search_exc = search_exc
        self.create_exc = create_exc
        self.create_result: dict[str, Any] = {
            "number": 7,
            "html_url": "https://github.com/acme/site/issues/7",
        }
        self.search_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []

    async def issue_search_marker(
        self, *, ticket_uid: str, caller: str, correlation_id: str = "",
    ) -> dict[str, Any] | None:
        self.search_calls.append({"ticket_uid": ticket_uid, "caller": caller})
        if self.search_exc is not None:
            raise self.search_exc
        return self.search_result

    async def issue_create(
        self,
        *,
        title: str,
        body: str,
        labels: list[str],
        ticket_uid: str,
        caller: str,
        correlation_id: str = "",
    ) -> dict[str, Any]:
        self.create_calls.append({
            "title": title,
            "body": body,
            "labels": labels,
            "ticket_uid": ticket_uid,
            "caller": caller,
        })
        if self.create_exc is not None:
            raise self.create_exc
        return dict(self.create_result)


def _build_config() -> TransportConfig:
    tokens = {
        "vera": AuthTokenEntry(
            token=DUMMY_VERA_PEER_TOKEN,
            allowed_clients=["vera"],
        ),
    }
    return TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens=tokens),
        state=StateConfig(),
        canonical=CanonicalConfig(owner=False),
        peers={},
    )


async def _build_kalle_app(
    aiohttp_client, tmp_path, *, fake_client=None, register=True,
):  # type: ignore[no-untyped-def]
    """KAL-LE-style app: vault wired, intake registered (or not)."""
    config = _build_config()
    state = TransportState.create(tmp_path / "transport_state.json")
    vault_root = tmp_path / "vault"
    vault_root.mkdir(exist_ok=True)
    app = build_app(config, state)
    register_vault_path(app, vault_root)
    register_instance_identity(app, name="KAL-LE")
    intake_config = TicketIntakeConfig(
        enabled=True,
        state_path=str(tmp_path / "ticket_intake_state.json"),
    )
    if register:
        register_ticket_intake(
            app, intake_config=intake_config, github_client=fake_client,
        )
    tc: TestClient = await aiohttp_client(app)
    return tc, vault_root, intake_config


def _ticket_payload(**overrides: Any) -> dict[str, Any]:
    frontmatter = {
        "type": "ticket",
        "title": "Login button broken",
        "ticket_type": "bug",
        "reporter": "Ben",
        "area": "checkout",
        "priority": "high",
        "status": "open",
        "created": "2026-06-11",
        "ticket_uid": TICKET_UID,
    }
    payload: dict[str, Any] = {
        "precedence": "R",
        "ticket_uid": TICKET_UID,
        "relpath": "ticket/Login button broken.md",
        "frontmatter": frontmatter,
        "body": "## Repro\n1. Click login\n2. Nothing happens\n",
    }
    payload.update(overrides)
    return payload


async def _push(client: TestClient, payload: dict[str, Any]):  # type: ignore[no-untyped-def]
    return await client.post(
        "/peer/send",
        json={"kind": "ticket", "from": "vera", "payload": payload},
        headers={
            "Authorization": f"Bearer {DUMMY_VERA_PEER_TOKEN}",
            "X-Alfred-Client": "vera",
        },
    )


def _log_events(captured: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [c for c in captured if c.get("event") == event]


# ---------------------------------------------------------------------------
# 501 — intake not registered
# ---------------------------------------------------------------------------


async def test_ticket_501_when_unregistered(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    client, _, _ = await _build_kalle_app(
        aiohttp_client, tmp_path, register=False,
    )
    resp = await _push(client, _ticket_payload())
    assert resp.status == 501
    body = await resp.json()
    assert body["reason"] == "ticket_intake_unavailable"


# ---------------------------------------------------------------------------
# Schema gate — 400 per missing/invalid field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "detail_fragment"),
    [
        ({"ticket_uid": ""}, "ticket_uid"),
        ({"ticket_uid": None}, "ticket_uid"),
        ({"relpath": 123}, "relpath"),
        ({"frontmatter": "not-a-dict"}, "frontmatter must be an object"),
        ({"body": None}, "body"),
    ],
)
async def test_ticket_schema_gate_payload_fields(
    aiohttp_client, tmp_path, mutate, detail_fragment,
):  # type: ignore[no-untyped-def]
    fake = FakeGitHubClient(tmp_path / "audit.jsonl")
    client, _, _ = await _build_kalle_app(
        aiohttp_client, tmp_path, fake_client=fake,
    )
    resp = await _push(client, _ticket_payload(**mutate))
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "schema_error"
    assert detail_fragment in body["detail"]
    # Schema failures never reach GitHub or the vault.
    assert fake.search_calls == []
    assert fake.create_calls == []


@pytest.mark.parametrize(
    "missing_field", ["title", "ticket_type", "reporter", "area"],
)
async def test_ticket_schema_gate_frontmatter_required_fields(
    aiohttp_client, tmp_path, missing_field,
):  # type: ignore[no-untyped-def]
    fake = FakeGitHubClient(tmp_path / "audit.jsonl")
    client, _, _ = await _build_kalle_app(
        aiohttp_client, tmp_path, fake_client=fake,
    )
    payload = _ticket_payload()
    del payload["frontmatter"][missing_field]
    resp = await _push(client, payload)
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "schema_error"
    assert f"frontmatter.{missing_field}" in body["detail"]


# ---------------------------------------------------------------------------
# Happy path — record + issue + state + ack
# ---------------------------------------------------------------------------


async def test_ticket_happy_path_end_to_end(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    fake = FakeGitHubClient(tmp_path / "audit.jsonl")
    client, vault_root, intake_config = await _build_kalle_app(
        aiohttp_client, tmp_path, fake_client=fake,
    )

    with structlog.testing.capture_logs() as captured:
        resp = await _push(client, _ticket_payload())
    assert resp.status == 200
    body = await resp.json()

    # Ack shape.
    assert body["status"] == "created"
    assert body["issue_number"] == 7
    assert body["issue_url"] == "https://github.com/acme/site/issues/7"
    assert body["kalle_relpath"] == "ticket/Login button broken.md"
    assert body["correlation_id"]

    # Marker-search guard ran FIRST, then the create.
    assert len(fake.search_calls) == 1
    assert fake.search_calls[0]["caller"] == "ticket_intake"
    assert fake.search_calls[0]["ticket_uid"] == TICKET_UID
    assert len(fake.create_calls) == 1
    call = fake.create_calls[0]

    # Issue title + body composition (deterministic header + verbatim
    # body + marker line; absent optionals omitted).
    assert call["title"] == "[bug] Login button broken"
    assert "Reported by: Ben" in call["body"]
    assert "Area: checkout" in call["body"]
    assert "Priority: high" in call["body"]
    assert "Source: ticket/Login button broken.md" in call["body"]
    assert "Filed: 2026-06-11" in call["body"]
    assert "Origin instance: vera" in call["body"]
    assert "Environment:" not in call["body"]  # absent optional omitted
    assert "## Repro\n1. Click login" in call["body"]
    assert f"<!-- algernon-ticket: {TICKET_UID} -->" in call["body"]
    # No empty-trailing-colon lines anywhere (absent optionals are
    # omitted entirely, never rendered as "Label:").
    import re as _re
    for line in call["body"].splitlines():
        assert not _re.fullmatch(r"[A-Za-z ]+:\s*", line), line

    # Label assembly: base + label_map hits on ticket_type + priority.
    assert call["labels"] == ["auto-fix", "bug", "priority-high"]

    # Vault record created under scope kalle with origin fields.
    record_path = vault_root / "ticket" / "Login button broken.md"
    assert record_path.exists()
    post = fm_lib.load(str(record_path))
    assert post.metadata["type"] == "ticket"
    assert post.metadata["title"] == "Login button broken"
    assert post.metadata["origin"] == "vera"
    assert post.metadata["origin_relpath"] == "ticket/Login button broken.md"
    assert post.metadata["ticket_uid"] == TICKET_UID
    assert post.metadata["github_issue"] == 7
    assert post.metadata["github_url"] == "https://github.com/acme/site/issues/7"
    assert "## Repro" in post.content  # body verbatim

    # State written.
    state = TicketIntakeState.load(intake_config.state_path)
    entry = state.entries[TICKET_UID]
    assert entry.issue_number == 7
    assert entry.kalle_relpath == "ticket/Login button broken.md"
    assert entry.recorded_at
    assert entry.issue_created_at
    assert entry.retry_count == 0

    # Log pins — received / recorded / issue_created, with key fields.
    received = _log_events(captured, "transport.peer.received")
    assert len(received) == 1
    assert received[0]["kind"] == "ticket"
    assert received[0]["precedence"] == "R"
    recorded = _log_events(captured, "transport.ticket.recorded")
    assert len(recorded) == 1
    assert recorded[0]["ticket_uid"] == TICKET_UID
    assert recorded[0]["path"] == "ticket/Login button broken.md"
    issue_created = _log_events(captured, "transport.ticket.issue_created")
    assert len(issue_created) == 1
    assert issue_created[0]["ticket_uid"] == TICKET_UID
    assert issue_created[0]["issue_number"] == 7


async def test_ticket_label_unmapped_logged_and_proceeds(
    aiohttp_client, tmp_path,
):  # type: ignore[no-untyped-def]
    fake = FakeGitHubClient(
        tmp_path / "audit.jsonl", label_map={"bug": "bug"},
    )
    client, _, _ = await _build_kalle_app(
        aiohttp_client, tmp_path, fake_client=fake,
    )
    with structlog.testing.capture_logs() as captured:
        resp = await _push(client, _ticket_payload())
    assert resp.status == 200
    assert (await resp.json())["status"] == "created"
    # priority "high" has no mapping — logged, create still proceeded
    # with the mapped subset only.
    assert fake.create_calls[0]["labels"] == ["auto-fix", "bug"]
    unmapped = _log_events(captured, "transport.ticket.label_unmapped")
    assert len(unmapped) == 1
    assert unmapped[0]["value"] == "high"
    assert unmapped[0]["field"] == "priority"


# ---------------------------------------------------------------------------
# Dedupe — uid known with issue_number
# ---------------------------------------------------------------------------


async def test_ticket_dedupe_exists_no_github_no_vault_write(
    aiohttp_client, tmp_path,
):  # type: ignore[no-untyped-def]
    fake = FakeGitHubClient(tmp_path / "audit.jsonl")
    client, vault_root, intake_config = await _build_kalle_app(
        aiohttp_client, tmp_path, fake_client=fake,
    )
    # Pre-seed state: issue already filed for this uid.
    state = TicketIntakeState.load(intake_config.state_path)
    state.entries[TICKET_UID] = TicketIntakeEntry(
        recorded_at="2026-06-11T00:00:00+00:00",
        kalle_relpath="ticket/Login button broken.md",
        issue_number=7,
        issue_url="https://github.com/acme/site/issues/7",
    )
    state.save()

    with structlog.testing.capture_logs() as captured:
        resp = await _push(client, _ticket_payload())
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "exists"
    assert body["issue_number"] == 7
    assert body["issue_url"] == "https://github.com/acme/site/issues/7"
    assert body["kalle_relpath"] == "ticket/Login button broken.md"

    # No GitHub call, no vault write.
    assert fake.search_calls == []
    assert fake.create_calls == []
    assert not (vault_root / "ticket").exists()

    # Log + audit pins.
    dedupe = _log_events(captured, "transport.ticket.dedupe_hit")
    assert len(dedupe) == 1
    assert dedupe[0]["ticket_uid"] == TICKET_UID
    assert dedupe[0]["issue_number"] == 7
    rows = read_github_audit(fake.config.audit_log_path)
    exists_rows = [r for r in rows if r.get("outcome") == "exists"]
    assert len(exists_rows) == 1
    assert exists_rows[0]["op"] == "issue_create"
    assert exists_rows[0]["from_peer"] == "vera"
    assert exists_rows[0]["ticket_uid"] == TICKET_UID


# ---------------------------------------------------------------------------
# GitHub down — record-then-pending-ack, re-push retries
# ---------------------------------------------------------------------------


async def test_ticket_pending_then_repush_creates_without_duplicate(
    aiohttp_client, tmp_path,
):  # type: ignore[no-untyped-def]
    fake = FakeGitHubClient(
        tmp_path / "audit.jsonl",
        search_exc=httpx.ConnectError("github down"),
        create_exc=httpx.ConnectError("github down"),
    )
    client, vault_root, intake_config = await _build_kalle_app(
        aiohttp_client, tmp_path, fake_client=fake,
    )

    # First push: GitHub down — recorded, pending.
    with structlog.testing.capture_logs() as captured:
        resp = await _push(client, _ticket_payload())
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "recorded_issue_pending"
    assert body["kalle_relpath"] == "ticket/Login button broken.md"

    record_path = vault_root / "ticket" / "Login button broken.md"
    assert record_path.exists()
    state = TicketIntakeState.load(intake_config.state_path)
    entry = state.entries[TICKET_UID]
    assert entry.issue_number is None
    assert entry.retry_count == 1
    assert entry.kalle_relpath == "ticket/Login button broken.md"

    pending = _log_events(captured, "transport.ticket.issue_pending")
    assert len(pending) == 1
    assert pending[0]["ticket_uid"] == TICKET_UID
    assert pending[0]["error_type"] == "ConnectError"
    assert pending[0]["retry_count"] == 1
    assert "detail" in pending[0]

    # Second push (VERA's re-push) with GitHub back up.
    fake.search_exc = None
    fake.create_exc = None
    resp2 = await _push(client, _ticket_payload())
    assert resp2.status == 200
    body2 = await resp2.json()
    assert body2["status"] == "created"
    assert body2["issue_number"] == 7

    # Path (d): recorded entry skips the marker search AND the vault
    # create — no duplicate record.
    assert len(fake.search_calls) == 1  # only the failed first attempt
    assert len(fake.create_calls) == 1  # only the successful retry
    ticket_files = list((vault_root / "ticket").glob("*.md"))
    assert len(ticket_files) == 1

    state2 = TicketIntakeState.load(intake_config.state_path)
    assert state2.entries[TICKET_UID].issue_number == 7


async def test_ticket_pending_detail_carries_http_status_head(
    aiohttp_client, tmp_path,
):  # type: ignore[no-untyped-def]
    """Rate-limit/abuse text from GitHub must be grep-able in the log."""
    request = httpx.Request("POST", "https://api.github.com/repos/acme/site/issues")
    response = httpx.Response(403, text="rate limit exceeded", request=request)
    fake = FakeGitHubClient(
        tmp_path / "audit.jsonl",
        search_result=None,
        create_exc=httpx.HTTPStatusError(
            "403 Forbidden", request=request, response=response,
        ),
    )
    client, _, _ = await _build_kalle_app(
        aiohttp_client, tmp_path, fake_client=fake,
    )
    with structlog.testing.capture_logs() as captured:
        resp = await _push(client, _ticket_payload())
    assert (await resp.json())["status"] == "recorded_issue_pending"
    pending = _log_events(captured, "transport.ticket.issue_pending")
    assert len(pending) == 1
    assert pending[0]["http_status"] == 403
    assert "rate limit exceeded" in pending[0]["detail"]


# ---------------------------------------------------------------------------
# Adopt — marker search recovers after state deletion
# ---------------------------------------------------------------------------


async def test_ticket_adopt_from_marker_search(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    fake = FakeGitHubClient(
        tmp_path / "audit.jsonl",
        search_result={
            "number": 42,
            "html_url": "https://github.com/acme/site/issues/42",
            "state": "open",
        },
    )
    client, vault_root, intake_config = await _build_kalle_app(
        aiohttp_client, tmp_path, fake_client=fake,
    )

    with structlog.testing.capture_logs() as captured:
        resp = await _push(client, _ticket_payload())
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "adopted"
    assert body["issue_number"] == 42
    assert body["kalle_relpath"] == "ticket/Login button broken.md"

    # No duplicate issue minted.
    assert fake.create_calls == []
    # Vault ticket still recorded.
    assert (vault_root / "ticket" / "Login button broken.md").exists()
    # State adopted.
    state = TicketIntakeState.load(intake_config.state_path)
    assert state.entries[TICKET_UID].issue_number == 42

    adopted = _log_events(captured, "transport.ticket.adopted")
    assert len(adopted) == 1
    assert adopted[0]["ticket_uid"] == TICKET_UID
    assert adopted[0]["issue_number"] == 42
    rows = read_github_audit(fake.config.audit_log_path)
    adopted_rows = [r for r in rows if r.get("outcome") == "adopted"]
    assert len(adopted_rows) == 1
    assert adopted_rows[0]["from_peer"] == "vera"


# ---------------------------------------------------------------------------
# Capability advertisement
# ---------------------------------------------------------------------------


async def _handshake_capabilities(client: TestClient) -> list[str]:
    resp = await client.post(
        "/peer/handshake",
        json={"from": "vera"},
        headers={
            "Authorization": f"Bearer {DUMMY_VERA_PEER_TOKEN}",
            "X-Alfred-Client": "vera",
        },
    )
    assert resp.status == 200
    return (await resp.json())["capabilities"]


async def test_capability_present_when_registered(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    fake = FakeGitHubClient(tmp_path / "audit.jsonl")
    client, _, _ = await _build_kalle_app(
        aiohttp_client, tmp_path, fake_client=fake,
    )
    assert "ticket_intake" in await _handshake_capabilities(client)


async def test_capability_absent_when_not_registered(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    client, _, _ = await _build_kalle_app(
        aiohttp_client, tmp_path, register=False,
    )
    assert "ticket_intake" not in await _handshake_capabilities(client)


# ---------------------------------------------------------------------------
# State module — schema tolerance + atomic save
# ---------------------------------------------------------------------------


def test_intake_state_schema_tolerance(tmp_path):  # type: ignore[no-untyped-def]
    """Unknown fields from a newer version are ignored on load."""
    path = tmp_path / "ticket_intake_state.json"
    path.write_text(
        json.dumps({
            "entries": {
                "vera-20260611-deadbeef": {
                    "recorded_at": "2026-06-11T00:00:00+00:00",
                    "kalle_relpath": "ticket/X.md",
                    "issue_number": 3,
                    "field_from_the_future": True,
                },
            },
        }),
        encoding="utf-8",
    )
    state = TicketIntakeState.load(path)
    entry = state.entries["vera-20260611-deadbeef"]
    assert entry.issue_number == 3
    assert entry.kalle_relpath == "ticket/X.md"
    # c5 reserved fields default cleanly.
    assert entry.disposition == ""
    assert entry.pr_number is None


def test_intake_state_corrupt_file_starts_empty(tmp_path):  # type: ignore[no-untyped-def]
    path = tmp_path / "ticket_intake_state.json"
    path.write_text("{not json", encoding="utf-8")
    with structlog.testing.capture_logs() as captured:
        state = TicketIntakeState.load(path)
    assert state.entries == {}
    failed = _log_events(captured, "ticket_intake.state_load_failed")
    assert len(failed) == 1
    assert failed[0]["path"] == str(path)


def test_intake_state_atomic_roundtrip(tmp_path):  # type: ignore[no-untyped-def]
    path = tmp_path / "data" / "ticket_intake_state.json"
    state = TicketIntakeState(path=path)
    state.entries["uid-1"] = TicketIntakeEntry(
        recorded_at="now", kalle_relpath="ticket/A.md", retry_count=2,
    )
    state.save()
    assert path.exists()
    assert not path.with_suffix(".json.tmp").exists()
    loaded = TicketIntakeState.load(path)
    assert loaded.entries["uid-1"].retry_count == 2


def test_intake_config_loader_defaults_and_overrides():  # type: ignore[no-untyped-def]
    from alfred.transport.ticket_intake import load_ticket_intake_config

    assert load_ticket_intake_config({}).enabled is False
    assert (
        load_ticket_intake_config({}).state_path
        == "./data/ticket_intake_state.json"
    )
    cfg = load_ticket_intake_config({
        "ticket_intake": {
            "enabled": True,
            "state": {"path": "/tmp/x.json"},
        },
    })
    assert cfg.enabled is True
    assert cfg.state_path == "/tmp/x.json"
