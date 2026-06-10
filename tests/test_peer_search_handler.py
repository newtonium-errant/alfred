"""End-to-end tests for POST /peer/search (P1, 2026-06-09).

Drives the aiohttp handler with a Salem-style canonical-owner app:
filtered event query happy path, the three fail-closed gates (type not
queryable / dim denied / op denied), field-gate intersection, and the
``kind: "search"`` audit extension. Mirrors the harness in
``test_peer_handlers.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient

from alfred.transport.canonical_audit import read_audit
from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    CanonicalConfig,
    FilterDimRule,
    PeerEntry,
    PeerFieldRules,
    PeerQueryRules,
    SchedulerConfig,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.peer_handlers import (
    register_instance_identity,
    register_vault_path,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState


DUMMY_SALEM_PEER_TOKEN = "DUMMY_SALEM_PEER_TEST_TOKEN_PLACEHOLDER_NOT_REAL_0123456789"
DUMMY_HYPATIA_PEER_TOKEN = "DUMMY_HYPATIA_PEER_TEST_TOKEN_PLACEHOLDER_NOT_REAL_01234"


def _hypatia_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {DUMMY_HYPATIA_PEER_TOKEN}",
        "X-Alfred-Client": "hypatia",
    }


@pytest.fixture
async def salem_search_app(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Salem app: canonical owner, Hypatia permissioned to SEARCH events."""
    audit_path = tmp_path / "canonical_audit.jsonl"
    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={
            "hypatia": AuthTokenEntry(
                token=DUMMY_HYPATIA_PEER_TOKEN,
                allowed_clients=["hypatia"],
            ),
        }),
        state=StateConfig(),
        canonical=CanonicalConfig(
            owner=True,
            audit_log_path=str(audit_path),
            peer_permissions={
                "hypatia": {
                    "event": PeerFieldRules(
                        fields=["name", "title", "date", "participants"],
                        query=PeerQueryRules(
                            filter_dims={
                                "participants": FilterDimRule(op=["eq", "contains"]),
                                "date": FilterDimRule(op=["gte", "lte", "between"]),
                            },
                            sort=["date"],
                            max_limit=10,
                            default_limit=5,
                        ),
                    ),
                    # person has fields but NO query block → not searchable.
                    "person": PeerFieldRules(fields=["name", "email"]),
                },
            },
        ),
        peers={},
    )
    state = TransportState.create(tmp_path / "transport_state.json")

    vault_root = tmp_path / "vault"
    (vault_root / "event").mkdir(parents=True)

    def _event(name: str, date: str, participants: list[str], title: str) -> None:
        plist = "\n".join(f"  - '{p}'" for p in participants)
        (vault_root / "event" / f"{name}.md").write_text(
            f"---\nname: {name}\ntype: event\ntitle: {title}\n"
            f"date: {date}\nsecret_notes: do not leak\n"
            f"participants:\n{plist}\n---\nBody never exposed.\n",
            encoding="utf-8",
        )

    _event("Coffee with Andrew", "2026-05-30",
           ["[[person/Andrew Newton]]"], "Coffee chat")
    _event("Old meeting", "2025-02-01",
           ["[[person/Andrew Newton]]"], "Old one")
    _event("Jamie sync", "2026-06-01",
           ["[[person/Jamie Newton]]"], "Jamie only")

    app = build_app(config, state)
    register_vault_path(app, vault_root)
    register_instance_identity(app, name="S.A.L.E.M.", alias="Salem")
    app["_audit_path"] = audit_path

    tc: TestClient = await aiohttp_client(app)
    return tc


# ---------------------------------------------------------------------------
# Happy path — filtered event search
# ---------------------------------------------------------------------------


async def test_search_filters_by_participant_most_recent(salem_search_app):  # type: ignore[no-untyped-def]
    """The operator example: events Andrew attended, most recent first."""
    resp = await salem_search_app.post(
        "/peer/search",
        json={
            "record_type": "event",
            "filter": [
                {"dim": "participants", "op": "contains", "value": "Andrew Newton"},
            ],
            "sort": {"by": "date", "dir": "desc"},
            "limit": 1,
        },
        headers=_hypatia_headers(),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"
    assert body["count"] == 1
    # Most-recent Andrew event is the 2026-05-30 coffee, NOT the 2025 one.
    assert body["records"][0]["date"] == "2026-05-30"
    # Jamie's event is excluded.
    assert all("Jamie" not in str(r) for r in body["records"])


async def test_search_field_gate_excludes_unpermitted_field(salem_search_app):  # type: ignore[no-untyped-def]
    """``secret_notes`` is in the record but NOT in the fields allowlist."""
    resp = await salem_search_app.post(
        "/peer/search",
        json={
            "record_type": "event",
            "filter": [
                {"dim": "participants", "op": "contains", "value": "Andrew Newton"},
            ],
        },
        headers=_hypatia_headers(),
    )
    body = await resp.json()
    for record in body["records"]:
        assert "secret_notes" not in record


async def test_search_date_range_filter(salem_search_app):  # type: ignore[no-untyped-def]
    resp = await salem_search_app.post(
        "/peer/search",
        json={
            "record_type": "event",
            "filter": [
                {"dim": "date", "op": "gte", "value": "2026-01-01"},
            ],
            "sort": {"by": "date", "dir": "asc"},
        },
        headers=_hypatia_headers(),
    )
    body = await resp.json()
    # 2025 event excluded; the two 2026 events remain.
    assert body["count"] == 2


# ---------------------------------------------------------------------------
# Fail-closed gate 1 — type not queryable
# ---------------------------------------------------------------------------


async def test_search_denied_when_type_has_no_query_block(salem_search_app):  # type: ignore[no-untyped-def]
    """person has fields but no query block → 403 filtered_query_not_permitted."""
    resp = await salem_search_app.post(
        "/peer/search",
        json={
            "record_type": "person",
            "filter": [{"dim": "name", "op": "eq", "value": "x"}],
        },
        headers=_hypatia_headers(),
    )
    assert resp.status == 403
    body = await resp.json()
    # Canonical transport error envelope is ``{"reason": <code>, ...}`` —
    # same shape /peer/query + _serve_canonical use via _json_error.
    assert body["reason"] == "filtered_query_not_permitted"


# ---------------------------------------------------------------------------
# Fail-closed gate 2 — dimension / operator denied
# ---------------------------------------------------------------------------


async def test_search_denied_on_unlisted_filter_dim(salem_search_app):  # type: ignore[no-untyped-def]
    """Filtering on secret_notes (not in filter_dims) → 403 filter_dim_denied."""
    resp = await salem_search_app.post(
        "/peer/search",
        json={
            "record_type": "event",
            "filter": [{"dim": "secret_notes", "op": "contains", "value": "x"}],
        },
        headers=_hypatia_headers(),
    )
    assert resp.status == 403
    body = await resp.json()
    assert body["reason"] == "filter_dim_denied"


async def test_search_denied_on_disallowed_operator(salem_search_app):  # type: ignore[no-untyped-def]
    """participants allows eq/contains, NOT gte → 403."""
    resp = await salem_search_app.post(
        "/peer/search",
        json={
            "record_type": "event",
            "filter": [{"dim": "participants", "op": "gte", "value": "x"}],
        },
        headers=_hypatia_headers(),
    )
    assert resp.status == 403
    body = await resp.json()
    assert body["reason"] == "filter_dim_denied"


async def test_search_denied_on_unlisted_sort_field(salem_search_app):  # type: ignore[no-untyped-def]
    resp = await salem_search_app.post(
        "/peer/search",
        json={
            "record_type": "event",
            "sort": {"by": "secret_notes", "dir": "desc"},
        },
        headers=_hypatia_headers(),
    )
    assert resp.status == 403


# ---------------------------------------------------------------------------
# Audit extension — kind:"search" + predicate + match_count
# ---------------------------------------------------------------------------


async def test_search_audits_with_kind_and_predicate(salem_search_app):  # type: ignore[no-untyped-def]
    await salem_search_app.post(
        "/peer/search",
        json={
            "record_type": "event",
            "filter": [
                {"dim": "participants", "op": "contains", "value": "Andrew Newton"},
            ],
            "limit": 1,
        },
        headers=_hypatia_headers(),
    )
    audit = read_audit(salem_search_app.app["_audit_path"])
    search_rows = [r for r in audit if r.get("kind") == "search"]
    assert len(search_rows) == 1
    row = search_rows[0]
    assert row["peer"] == "hypatia"
    assert row["type"] == "event"
    assert row["match_count"] == 1
    assert row["filter"][0]["dim"] == "participants"
    # The filter value is logged verbatim (Decision F).
    assert row["filter"][0]["value"] == "Andrew Newton"


async def test_search_denial_is_audited(salem_search_app):  # type: ignore[no-untyped-def]
    await salem_search_app.post(
        "/peer/search",
        json={
            "record_type": "event",
            "filter": [{"dim": "secret_notes", "op": "contains", "value": "x"}],
        },
        headers=_hypatia_headers(),
    )
    audit = read_audit(salem_search_app.app["_audit_path"])
    search_rows = [r for r in audit if r.get("kind") == "search"]
    assert len(search_rows) == 1
    assert search_rows[0]["denied_dims"] == ["secret_notes"]
    assert search_rows[0]["match_count"] == 0
