"""The RRTS invoices-export receiver — ``POST /rrts/export``.

Every pin here drives the ROUTE, through the real transport app and the
real ``auth_middleware``, with a real Bearer token. That is deliberate and
it is the difference between testing the peer-pin and testing a function
that happens to contain one: a handler can pin correctly while the route is
mounted somewhere the middleware never runs, and only a request through the
app can tell you which world you are in.

The properties, in the order they matter:

  1. **One peer, one thing.** A valid token belonging to any OTHER peer is
     refused 401 with a logged reason — and the file is not touched. The
     escalation this closes is the shared-``allowed_clients`` one the
     CLAUDE.md relay doctrine names: two peers can present the same client,
     so the peer NAME is what distinguishes them.
  2. **The write is atomic and total.** Read-back equals what was posted,
     byte for byte, and no ``.tmp`` survives.
  3. **Exactly two validations.** JSON, and a top-level ``exported_at``.
     The document's shape is RRTS's; a receiver enforcing more would break
     the day they add a field.
  4. **A backwards export is written AND logged.** Both halves, because
     either alone is a different (wrong) behaviour.

Token literals are obviously-fake ``DUMMY_*`` constants per the house rule:
scanners match on prefix + entropy and cannot tell a fixture from a leak.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import structlog

from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.routes_rrts import (
    EXPORT_FILENAME,
    RRTS_EXPORT_PEER_NAME,
    export_path,
    read_stored_exported_at,
    register_rrts_routes,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState

DUMMY_RRTS_EXPORT_TOKEN = "DUMMY_RRTS_EXPORT_TEST_TOKEN"
DUMMY_OTHER_PEER_TOKEN = "DUMMY_OTHER_PEER_TEST_TOKEN"

_RRTS_HEADERS = {
    "Authorization": f"Bearer {DUMMY_RRTS_EXPORT_TOKEN}",
    "X-Alfred-Client": "rrts",
}
#: A DIFFERENT peer that shares the same client name — the escalation shape
#: the peer-pin exists for. Valid at Layer 1, wrong at Layer 2.
_OTHER_PEER_HEADERS = {
    "Authorization": f"Bearer {DUMMY_OTHER_PEER_TOKEN}",
    "X-Alfred-Client": "rrts",
}


def _config() -> TransportConfig:
    return TransportConfig(
        server=ServerConfig(),
        auth=AuthConfig(
            tokens={
                RRTS_EXPORT_PEER_NAME: AuthTokenEntry(
                    token=DUMMY_RRTS_EXPORT_TOKEN, allowed_clients=["rrts"],
                ),
                # Same allowed_clients, different peer. Without the pin this
                # token would clear Layer 1 and drive the write.
                "some_other_peer": AuthTokenEntry(
                    token=DUMMY_OTHER_PEER_TOKEN, allowed_clients=["rrts"],
                ),
            }
        ),
        state=StateConfig(),
    )


@pytest.fixture
async def rrts_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_config(), tstate)
    export_dir = tmp_path / "rrts-export"
    mounted = register_rrts_routes(
        app, enabled=True, export_dir=str(export_dir), max_bytes=4096,
    )
    assert mounted is True
    app["_export_dir"] = export_dir
    return await aiohttp_client(app)


def _document(**overrides):
    base = {
        "exported_at": "2026-08-13T02:30:00Z",
        "invoices": [
            {
                "invoice_no": "487",
                "client_name": "Aldenshaw, Marisol",
                "date_sent": "2026-06-01",
                "status": "sent",
                "total": "1150.00",
                "line_items": [
                    {"date_of_service": "2026-05-28", "amount": "1150.00",
                     "benefit_code": "700409"},
                ],
            },
        ],
    }
    base.update(overrides)
    return base


# --- one peer, one thing ---------------------------------------------------


async def test_the_right_peer_can_post(rrts_client) -> None:
    resp = await rrts_client.post(
        "/rrts/export", json=_document(), headers=_RRTS_HEADERS
    )
    assert resp.status == 200
    payload = await resp.json()
    assert payload["ok"] is True
    assert payload["exported_at"] == "2026-08-13T02:30:00Z"
    assert payload["bytes"] > 0


async def test_a_different_peer_is_refused_and_writes_nothing(rrts_client) -> None:
    """The escalation the pin closes. This token is VALID — it clears Layer
    1 and resolves to a peer — it is simply not this route's peer. The
    assertion that matters is the second one: nothing was written."""
    with structlog.testing.capture_logs() as captured:
        resp = await rrts_client.post(
            "/rrts/export", json=_document(), headers=_OTHER_PEER_HEADERS
        )
    assert resp.status == 401
    assert (await resp.json())["error"] == "wrong_peer"

    rejected = [
        c for c in captured
        if c.get("event") == "transport.rrts_export.rejected"
    ]
    assert any(r.get("reason") == "wrong_peer" for r in rejected)

    export_dir = rrts_client.app["_export_dir"]
    assert not export_path(export_dir).exists()
    # And no debris either — a refused write must touch NOTHING out there.
    assert not export_dir.exists() or list(export_dir.iterdir()) == []


async def test_no_token_at_all_is_refused(rrts_client) -> None:
    """Layer 1's own refusal, asserted so the two layers are known to be
    distinct — a 401 here proves the middleware runs on this route, which
    is what makes the peer-pin test above meaningful."""
    resp = await rrts_client.post("/rrts/export", json=_document())
    assert resp.status == 401
    assert not export_path(rrts_client.app["_export_dir"]).exists()


# --- the write ----------------------------------------------------------------


async def test_the_stored_document_is_byte_identical(rrts_client) -> None:
    """Full replace, captured not interpreted: what lands is what was
    posted, including fields this receiver knows nothing about."""
    doc = _document(their_future_field={"nested": [1, 2, 3]})
    resp = await rrts_client.post(
        "/rrts/export", json=doc, headers=_RRTS_HEADERS
    )
    assert resp.status == 200
    # BYTES, not just structure. Structural equality passes even if the
    # receiver re-serialised the document — which would silently normalise
    # key order, whitespace and number formatting on a file another system
    # produced. The stronger assertion is free, so it is the one to make.
    raw_posted = json.dumps(doc).encode("utf-8")
    stored_bytes = export_path(rrts_client.app["_export_dir"]).read_bytes()
    assert stored_bytes == raw_posted

    stored = json.loads(stored_bytes.decode("utf-8"))
    assert stored == doc
    assert stored["their_future_field"] == {"nested": [1, 2, 3]}


async def test_no_temp_file_survives_the_write(rrts_client) -> None:
    """The atomicity convention: tmp-write then replace. A surviving .tmp
    means a reader could catch a partial file."""
    await rrts_client.post("/rrts/export", json=_document(), headers=_RRTS_HEADERS)
    export_dir = rrts_client.app["_export_dir"]
    assert [p.name for p in export_dir.iterdir()] == [EXPORT_FILENAME]


async def test_a_second_export_fully_replaces_the_first(rrts_client) -> None:
    """Full replace is the contract — not a merge. An invoice absent from
    the new snapshot is absent because RRTS says so."""
    await rrts_client.post(
        "/rrts/export",
        json=_document(exported_at="2026-08-12T02:30:00Z"),
        headers=_RRTS_HEADERS,
    )
    await rrts_client.post(
        "/rrts/export",
        json={"exported_at": "2026-08-13T02:30:00Z", "invoices": []},
        headers=_RRTS_HEADERS,
    )
    stored = json.loads(
        export_path(rrts_client.app["_export_dir"]).read_text(encoding="utf-8")
    )
    assert stored["invoices"] == []
    assert stored["exported_at"] == "2026-08-13T02:30:00Z"


# --- exactly two validations ---------------------------------------------------


async def test_malformed_json_is_refused(rrts_client) -> None:
    with structlog.testing.capture_logs() as captured:
        resp = await rrts_client.post(
            "/rrts/export", data=b"{not json at all", headers=_RRTS_HEADERS
        )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_json"
    assert any(
        c.get("reason") == "invalid_json" for c in captured
        if c.get("event") == "transport.rrts_export.rejected"
    )
    assert not export_path(rrts_client.app["_export_dir"]).exists()


async def test_a_json_array_is_refused(rrts_client) -> None:
    """Top-level must be an object — an array has nowhere to carry
    ``exported_at``."""
    resp = await rrts_client.post(
        "/rrts/export", json=[{"invoice_no": "487"}], headers=_RRTS_HEADERS
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_json"


@pytest.mark.parametrize(
    "bad", [{}, {"exported_at": ""}, {"exported_at": "   "}, {"exported_at": 42}],
)
async def test_missing_or_unusable_exported_at_is_refused(rrts_client, bad) -> None:
    """The ONE required field. Without it a snapshot cannot be ordered
    against its predecessor, so the staleness signal is impossible."""
    with structlog.testing.capture_logs() as captured:
        resp = await rrts_client.post(
            "/rrts/export", json=bad, headers=_RRTS_HEADERS
        )
    assert resp.status == 400
    assert (await resp.json())["error"] == "missing_exported_at"
    assert any(
        c.get("reason") == "missing_exported_at" for c in captured
        if c.get("event") == "transport.rrts_export.rejected"
    )
    assert not export_path(rrts_client.app["_export_dir"]).exists()


async def test_an_unknown_shape_is_NOT_refused(rrts_client) -> None:
    """The control for the two validations: capture-not-interpret means a
    document carrying nothing we recognise beyond ``exported_at`` is
    perfectly acceptable. Enforcing more would break the day RRTS adds a
    field, and would enforce a contract nobody agreed to."""
    resp = await rrts_client.post(
        "/rrts/export",
        json={"exported_at": "2026-08-13T02:30:00Z", "something_else": True},
        headers=_RRTS_HEADERS,
    )
    assert resp.status == 200


# --- the size cap ---------------------------------------------------------------


async def test_an_oversize_document_is_refused(rrts_client) -> None:
    big = {"exported_at": "2026-08-13T02:30:00Z", "pad": "x" * 8000}
    with structlog.testing.capture_logs() as captured:
        resp = await rrts_client.post(
            "/rrts/export", json=big, headers=_RRTS_HEADERS
        )
    assert resp.status == 413
    assert (await resp.json())["error"] == "payload_too_large"
    assert any(
        c.get("reason") == "payload_too_large" for c in captured
        if c.get("event") == "transport.rrts_export.rejected"
    )
    assert not export_path(rrts_client.app["_export_dir"]).exists()


async def test_a_document_under_the_cap_is_accepted(rrts_client) -> None:
    """Positive control — the cap must refuse SOME sizes, not all."""
    ok = {"exported_at": "2026-08-13T02:30:00Z", "pad": "x" * 100}
    resp = await rrts_client.post("/rrts/export", json=ok, headers=_RRTS_HEADERS)
    assert resp.status == 200


# --- the backwards export: BOTH halves --------------------------------------------


async def test_a_backwards_export_is_still_written(rrts_client) -> None:
    """Half one. Full replace is the agreed contract, and refusing would
    leave the operator with stale data AND no signal."""
    await rrts_client.post(
        "/rrts/export",
        json=_document(exported_at="2026-08-13T02:30:00Z"),
        headers=_RRTS_HEADERS,
    )
    resp = await rrts_client.post(
        "/rrts/export",
        json={"exported_at": "2026-08-01T02:30:00Z", "invoices": []},
        headers=_RRTS_HEADERS,
    )
    assert resp.status == 200
    stored = json.loads(
        export_path(rrts_client.app["_export_dir"]).read_text(encoding="utf-8")
    )
    assert stored["exported_at"] == "2026-08-01T02:30:00Z"


async def test_a_backwards_export_is_logged_loudly(rrts_client) -> None:
    """Half two, and the half that makes it a finding rather than a silent
    regression: an upstream job re-ran or a clock moved, and that is the
    operator's to know."""
    await rrts_client.post(
        "/rrts/export",
        json=_document(exported_at="2026-08-13T02:30:00Z"),
        headers=_RRTS_HEADERS,
    )
    with structlog.testing.capture_logs() as captured:
        await rrts_client.post(
            "/rrts/export",
            json={"exported_at": "2026-08-01T02:30:00Z", "invoices": []},
            headers=_RRTS_HEADERS,
        )
    stale = [
        c for c in captured
        if c.get("event") == "transport.rrts_export.stale_export"
    ]
    assert len(stale) == 1
    assert stale[0]["exported_at"] == "2026-08-01T02:30:00Z"
    assert stale[0]["previous_exported_at"] == "2026-08-13T02:30:00Z"


async def test_a_forwards_export_is_NOT_logged_as_stale(rrts_client) -> None:
    """The control — if it fired on every export it would say nothing."""
    await rrts_client.post(
        "/rrts/export",
        json=_document(exported_at="2026-08-01T02:30:00Z"),
        headers=_RRTS_HEADERS,
    )
    with structlog.testing.capture_logs() as captured:
        await rrts_client.post(
            "/rrts/export",
            json={"exported_at": "2026-08-13T02:30:00Z", "invoices": []},
            headers=_RRTS_HEADERS,
        )
    assert not [
        c for c in captured
        if c.get("event") == "transport.rrts_export.stale_export"
    ]


async def test_the_first_export_is_not_stale(rrts_client) -> None:
    """Nothing to compare against is not a regression."""
    with structlog.testing.capture_logs() as captured:
        await rrts_client.post(
            "/rrts/export", json=_document(), headers=_RRTS_HEADERS
        )
    assert not [
        c for c in captured
        if c.get("event") == "transport.rrts_export.stale_export"
    ]


# --- unresolved directory + opt-in inertness ---------------------------------------


async def test_an_unresolved_export_dir_refuses_loudly(aiohttp_client, tmp_path) -> None:
    """503 rather than a guessed path. Writing to a fallback location is
    how an instance's export lands where nothing looks for it."""
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_config(), tstate)
    register_rrts_routes(app, enabled=True, export_dir="")
    client = await aiohttp_client(app)
    with structlog.testing.capture_logs() as captured:
        resp = await client.post(
            "/rrts/export", json=_document(), headers=_RRTS_HEADERS
        )
    assert resp.status == 503
    assert (await resp.json())["error"] == "export_dir_not_configured"
    assert any(
        c.get("reason") == "export_dir_not_configured" for c in captured
        if c.get("event") == "transport.rrts_export.rejected"
    )


def test_disabled_mounts_nothing() -> None:
    """Opt-in inertness: an instance that receives no RRTS export has a
    byte-unchanged transport server."""
    from aiohttp import web

    app = web.Application()
    with structlog.testing.capture_logs() as captured:
        mounted = register_rrts_routes(app, enabled=False)
    assert mounted is False
    assert [r for r in app.router.routes()] == []
    assert any(
        c.get("event") == "transport.rrts_export.disabled" for c in captured
    )


def test_enabled_mounts_exactly_one_route(tmp_path) -> None:
    """One peer, one thing — asserted structurally, not just by behaviour."""
    from aiohttp import web

    app = web.Application()
    assert register_rrts_routes(
        app, enabled=True, export_dir=str(tmp_path),
    ) is True
    routes = [
        (r.method, r.resource.canonical) for r in app.router.routes()
    ]
    assert routes == [("POST", "/rrts/export")]


# --- the stored-exported_at reader ---------------------------------------------------


def test_read_stored_exported_at_tolerates_everything(tmp_path) -> None:
    """Never raises: a missing, unreadable or malformed prior export must
    not block the write that would replace it."""
    assert read_stored_exported_at(tmp_path) == ""
    (tmp_path / EXPORT_FILENAME).write_text("{not json", encoding="utf-8")
    assert read_stored_exported_at(tmp_path) == ""
    (tmp_path / EXPORT_FILENAME).write_text('["array"]', encoding="utf-8")
    assert read_stored_exported_at(tmp_path) == ""
    (tmp_path / EXPORT_FILENAME).write_text('{"exported_at": 42}', encoding="utf-8")
    assert read_stored_exported_at(tmp_path) == ""
    # Positive control: a well-formed one IS read.
    (tmp_path / EXPORT_FILENAME).write_text(
        '{"exported_at": "2026-08-13T02:30:00Z"}', encoding="utf-8"
    )
    assert read_stored_exported_at(tmp_path) == "2026-08-13T02:30:00Z"


# --- the atomicity pin (reviewer WARN from the receiver's gate) -------------------
#
# The read-back tests above pass against a PLAIN DIRECT WRITE — they only
# prove the bytes arrived, not how. Atomicity is the property that a reader
# never catches a partial file, and the only way to assert it from outside is
# to watch the mechanism: the write goes to a temp path and is put in place
# with os.replace. The reviewer proved the gap by leaving all 31 green with
# the rename removed.


async def test_the_write_goes_through_os_replace(rrts_client, monkeypatch) -> None:
    """Spy the mechanism, because the outcome cannot distinguish it.

    Asserts BOTH arguments: the source is the ``.tmp`` sibling and the
    destination is the final file. A rename from somewhere else would be a
    different bug with the same read-back.
    """
    from alfred.transport import routes_rrts

    calls: list[tuple[str, str]] = []
    real_replace = routes_rrts.os.replace

    def _spy(src, dst):  # type: ignore[no-untyped-def]
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(routes_rrts.os, "replace", _spy)

    resp = await rrts_client.post(
        "/rrts/export", json=_document(), headers=_RRTS_HEADERS
    )
    assert resp.status == 200

    final = export_path(rrts_client.app["_export_dir"])
    assert len(calls) == 1, "exactly one rename per export"
    src, dst = calls[0]
    assert dst == str(final)
    assert src == str(final) + ".tmp"


async def test_the_temp_file_lives_beside_its_destination(rrts_client) -> None:
    """``os.replace`` is only atomic WITHIN one filesystem. A temp file in
    the system temp dir would degrade the rename to copy-then-delete across
    a device boundary — reintroducing exactly the torn read this prevents,
    on the machines where it matters and nowhere else."""
    from alfred.transport import routes_rrts

    seen: list[str] = []
    real_replace = routes_rrts.os.replace

    def _spy(src, dst):  # type: ignore[no-untyped-def]
        seen.append(str(src))
        return real_replace(src, dst)

    import pytest as _pytest  # local, to keep the fixture list unchanged

    mp = _pytest.MonkeyPatch()
    mp.setattr(routes_rrts.os, "replace", _spy)
    try:
        await rrts_client.post(
            "/rrts/export", json=_document(), headers=_RRTS_HEADERS
        )
    finally:
        mp.undo()

    export_dir = rrts_client.app["_export_dir"]
    assert len(seen) == 1
    assert Path(seen[0]).parent == Path(export_dir)
