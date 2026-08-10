"""Tests for parity #22 — KAL-LE ticket → web-PWA notify (POLL slice).

Three surfaces, load-bearing first:

* **PRODUCER (KAL-LE)** — ``_handle_ticket_intake`` fires EXACTLY ONE
  best-effort ``kind=notice`` + ``web_notify`` peer_send on ack status
  ``created``; ``exists`` / ``adopted`` / ``recorded_issue_pending`` fire
  ZERO (a VERA re-push must not re-notify). peer_send raising NEVER fails
  the ticket ack; an unconfigured notify peer is a logged skip (ILB).
* **SINK (Salem)** — a ``web_notify``-tagged notice fans out into the
  bounded per-user store keyed to the operator, BESIDE the unchanged
  Telegram relay; sink-not-wired → logged skip, relay still runs.
* **READ ROUTES** — ``GET /chat/notifications`` + ``POST
  /chat/notifications/ack``: peer-pinned (web_ingest 401), fail-closed
  401 like ``/chat/history``, recipient-pinned (vouched rrts_intake
  reporter → explicit 403), ILB empty ``{notifications: [], unread: 0}``,
  ack marks read + unread decrements, cap evicts oldest.

Fixture style mirrors ``test_web_routes_brief.py`` (PRODUCTION peer key
names web / web_ingest / rrts_relay) + ``test_ticket_intake.py`` (the
FakeGitHubClient KAL-LE app).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog
from aiohttp.test_utils import TestClient

from alfred.integrations.github_ops import GitHubOpsConfig
from alfred.telegram.config import (
    AnthropicConfig,
    InstanceConfig,
    LoggingConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
)
from alfred.telegram.state import StateManager
from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    PeerEntry,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.peer_handlers import (
    register_instance_identity,
    register_peer_inbox,
    register_ticket_intake,
    register_vault_path,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState
from alfred.transport.ticket_intake import TicketIntakeConfig
from alfred.web.auth import SESSION_HEADER, USER_HEADER, make_session_token
from alfred.web.config import (
    WebAuthConfig,
    WebConfig,
    WebNotificationsConfig,
    WebUser,
)
from alfred.web.identity import synthetic_chat_id
from alfred.web.keys import KEY_WEB_NOTIFY_STORE
from alfred.web.notify_state import NOTIFY_CAP, WebNotifyStore
from alfred.web.routes_chat import register_web_routes
from alfred.web.state import WebAuthState

from tests.telegram.conftest import (  # shared fake SDK client
    FakeAnthropicClient,
    FakeBlock,
    FakeResponse,
)

# Obviously-fake test secrets — never a real provider prefix (builder.md
# GitGuardian rule).
DUMMY_WEB_PEER_TOKEN = "DUMMY_WEB_PEER_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_0123456"
DUMMY_WEB_INGEST_TOKEN = "DUMMY_WEB_INGEST_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_01234"
DUMMY_RRTS_RELAY_TOKEN = "DUMMY_RRTS_RELAY_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_01234"
DUMMY_KALLE_PEER_TOKEN = "DUMMY_KALLE_PEER_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_01234"
DUMMY_VERA_PEER_TOKEN = "DUMMY_VERA_PEER_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_012345"
DUMMY_SALEM_OUT_TOKEN = "DUMMY_SALEM_OUTBOUND_TOKEN_PLACEHOLDER_FOR_TESTING_ONLY_0123456"
DUMMY_WEB_SIGNING_SECRET = "DUMMY_WEB_SIGNING_SECRET_FOR_TESTING_ONLY_0123456789"

TICKET_UID = "vera-20260719-0001"

_PEER_HEADERS = {
    "Authorization": f"Bearer {DUMMY_WEB_PEER_TOKEN}",
    "X-Alfred-Client": "web",
}


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


def _log_events(captured: list[dict[str, Any]], event: str) -> list[dict[str, Any]]:
    return [c for c in captured if c.get("event") == event]


def _session_headers(name: str = "andrew", role: str = "owner") -> dict[str, str]:
    token = make_session_token(
        name, role, secret=DUMMY_WEB_SIGNING_SECRET, ttl_hours=168
    )
    return {**_PEER_HEADERS, SESSION_HEADER: token}


def _ingest_peer_session_headers() -> dict[str, str]:
    """The escalation attempt: a VALID Layer-1 ``web_ingest`` token +
    ``X-Alfred-Client: web`` + a VALID session token. Must be peer-pinned
    out BEFORE identity resolution."""
    token = make_session_token(
        "andrew", "owner", secret=DUMMY_WEB_SIGNING_SECRET, ttl_hours=168
    )
    return {
        "Authorization": f"Bearer {DUMMY_WEB_INGEST_TOKEN}",
        "X-Alfred-Client": "web",
        SESSION_HEADER: token,
    }


def _kalle_peer_headers() -> dict[str, str]:
    """Inbound peer headers as KAL-LE presents them to Salem."""
    return {
        "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
        "X-Alfred-Client": "kal-le",
    }


# ---------------------------------------------------------------------------
# WebNotifyStore unit behaviour (cap / ack / schema tolerance)
# ---------------------------------------------------------------------------


class TestWebNotifyStore:
    def test_enqueue_list_roundtrip_persists(self, tmp_path: Path) -> None:
        store = WebNotifyStore.create(tmp_path / "notify.json")
        entry = store.enqueue(
            42,
            text="New ticket [bug] Login broken",
            precedence="R",
            source="kal-le",
            ticket_uid=TICKET_UID,
            issue_url="https://github.com/acme/site/issues/7",
        )
        assert entry["id"] and entry["read"] is False and entry["ts"]

        reloaded = WebNotifyStore.create(tmp_path / "notify.json")
        reloaded.load()
        items = reloaded.list_for(42)
        assert len(items) == 1
        assert items[0]["text"] == "New ticket [bug] Login broken"
        assert items[0]["ticket_uid"] == TICKET_UID
        assert reloaded.unread_count(42) == 1
        # Per-user isolation: another key sees nothing.
        assert reloaded.list_for(99) == []

    def test_safe_http_url_scheme_allowlist(self) -> None:
        # #22 XSS guard: only http(s) survives; everything else → "".
        from alfred.web.notify_state import _safe_http_url

        assert _safe_http_url("https://github.com/a/b/issues/1") == "https://github.com/a/b/issues/1"
        assert _safe_http_url("HTTP://x") == "HTTP://x"  # case-insensitive scheme
        assert _safe_http_url("  https://x  ") == "https://x"  # surrounding ws stripped
        assert _safe_http_url("javascript:alert(1)") == ""
        assert _safe_http_url("data:text/html,<script>1</script>") == ""
        assert _safe_http_url("https:evil") == ""  # requires ://
        assert _safe_http_url("") == ""
        assert _safe_http_url(None) == ""

    def test_enqueue_drops_javascript_scheme_issue_url(self, tmp_path: Path) -> None:
        # A peer-supplied javascript: issue_url must be STORED EMPTY (never the
        # dangerous value that would render into the operator's <a href>).
        store = WebNotifyStore.create(tmp_path / "notify.json")
        entry = store.enqueue(1, text="x", issue_url="javascript:alert(document.cookie)")
        assert entry["issue_url"] == ""
        # persists empty across reload...
        reloaded = WebNotifyStore.create(tmp_path / "notify.json")
        reloaded.load()
        assert reloaded.list_for(1)[0]["issue_url"] == ""
        # ...while a valid http(s) url is preserved.
        e2 = store.enqueue(1, text="y", issue_url="https://github.com/a/b/issues/2")
        assert e2["issue_url"] == "https://github.com/a/b/issues/2"

    def test_cap_evicts_oldest(self, tmp_path: Path) -> None:
        store = WebNotifyStore.create(tmp_path / "notify.json")
        ids = [
            store.enqueue(1, text=f"n{i}")["id"] for i in range(NOTIFY_CAP + 5)
        ]
        items = store.list_for(1)
        assert len(items) == NOTIFY_CAP
        kept = {e["id"] for e in items}
        # The OLDEST five are gone; the newest survive.
        assert not kept.intersection(ids[:5])
        assert ids[-1] in kept
        # Newest-first ordering on read.
        assert items[0]["id"] == ids[-1]

    def test_ack_marks_read_and_decrements_unread(self, tmp_path: Path) -> None:
        store = WebNotifyStore.create(tmp_path / "notify.json")
        a = store.enqueue(1, text="a")["id"]
        store.enqueue(1, text="b")
        assert store.unread_count(1) == 2
        assert store.ack(1, [a]) == 1
        assert store.unread_count(1) == 1
        # Idempotent: re-ack acks 0, unknown id acks 0, never raises.
        assert store.ack(1, [a]) == 0
        assert store.ack(1, ["nope"]) == 0
        # Persisted.
        reloaded = WebNotifyStore.create(tmp_path / "notify.json")
        reloaded.load()
        assert reloaded.unread_count(1) == 1

    def test_corrupt_file_tolerated(self, tmp_path: Path) -> None:
        path = tmp_path / "notify.json"
        path.write_text("{not json", encoding="utf-8")
        store = WebNotifyStore.create(path)
        store.load()  # no raise
        assert store.notifications == {}

    def test_malformed_entries_dropped_on_load(self, tmp_path: Path) -> None:
        store = WebNotifyStore.create(tmp_path / "notify.json")
        store.enqueue(1, text="good")
        store.notifications["1"].append("not-a-dict")  # type: ignore[arg-type]
        store.notifications["1"].append({"no": "id"})
        store.save()
        reloaded = WebNotifyStore.create(tmp_path / "notify.json")
        reloaded.load()
        assert [e["text"] for e in reloaded.list_for(1)] == ["good"]


# ---------------------------------------------------------------------------
# Salem-side app fixtures (sink + read routes)
# ---------------------------------------------------------------------------


def _make_talker_config(tmp_path: Path) -> TalkerConfig:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    for sub in ("session", "task", "note", "project"):
        (vault_dir / sub).mkdir(exist_ok=True)
    return TalkerConfig(
        bot_token="test-token",
        allowed_users=[1],
        primary_users=["person/Andrew Newton"],
        anthropic=AnthropicConfig(api_key="test-key", model="claude-sonnet-4-6"),
        stt=STTConfig(api_key="test-stt", model="whisper-large-v3"),
        session=SessionConfig(
            gap_timeout_seconds=1800,
            state_path=str(tmp_path / "talker_state.json"),
        ),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name="Salem", canonical="S.A.L.E.M."),
    )


def _salem_transport_config() -> TransportConfig:
    """PRODUCTION peer key names: chat ``web`` + sibling ``web_ingest``
    (both ``allowed_clients: [web]``) + the inbound ``kal-le`` peer that
    pushes the notice + the vouched ``rrts_relay`` peer."""
    return TransportConfig(
        server=ServerConfig(),
        auth=AuthConfig(
            tokens={
                "web": AuthTokenEntry(
                    token=DUMMY_WEB_PEER_TOKEN, allowed_clients=["web"],
                ),
                "web_ingest": AuthTokenEntry(
                    token=DUMMY_WEB_INGEST_TOKEN, allowed_clients=["web"],
                ),
                "rrts_relay": AuthTokenEntry(
                    token=DUMMY_RRTS_RELAY_TOKEN, allowed_clients=["web"],
                ),
                "kal-le": AuthTokenEntry(
                    token=DUMMY_KALLE_PEER_TOKEN, allowed_clients=["kal-le"],
                ),
            }
        ),
        state=StateConfig(),
    )


def _web_config(**kwargs: Any) -> WebConfig:
    return WebConfig(
        enabled=True,
        users=[WebUser(name="andrew", role="owner")],
        auth=WebAuthConfig(session_secret=DUMMY_WEB_SIGNING_SECRET),
        **kwargs,
    )


def _build_salem_app(
    tmp_path: Path,
    *,
    web_config: WebConfig | None = None,
    data_dir: str | None = None,
    mount_web: bool = True,
):
    """Transport app with a recording fake peer inbox (the Telegram-relay
    stand-in) and, optionally, the web routes + notify store mounted."""
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_salem_transport_config(), tstate)

    relayed: list[dict[str, Any]] = []

    async def _fake_inbox(
        *, kind: str, payload: dict[str, Any], from_peer: str,
        correlation_id: str,
    ) -> dict[str, Any]:
        relayed.append({
            "kind": kind, "payload": payload, "from_peer": from_peer,
        })
        return {"relayed": True, "kind": kind}

    register_peer_inbox(app, _fake_inbox)
    app["_t_relayed"] = relayed

    if mount_web:
        state_mgr = StateManager(tmp_path / "talker_state.json")
        state_mgr.load()
        web_auth_state = WebAuthState.create(tmp_path / "web_auth_state.json")
        web_auth_state.load()
        fake = FakeAnthropicClient(
            [FakeResponse(content=[FakeBlock(type="text", text="hi")])]
        )
        register_web_routes(
            app,
            web_config=web_config if web_config is not None else _web_config(),
            web_auth_state=web_auth_state,
            anthropic_client=fake,
            state_mgr=state_mgr,
            talker_config=_make_talker_config(tmp_path),
            system_prompt_provider=lambda: "SYSTEM PROMPT",
            vault_context_str="VAULT CONTEXT",
            allowed_user_ids=[1],
            data_dir=data_dir,
        )
    return app


def _notice_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": "New ticket [bug] Login button broken — filed as issue #7: "
                "https://github.com/acme/site/issues/7",
        "precedence": "R",
        "source": "kal-le",
        "web_notify": True,
        "ticket_uid": TICKET_UID,
        "issue_url": "https://github.com/acme/site/issues/7",
    }
    payload.update(overrides)
    return payload


async def _push_notice(client: TestClient, payload: dict[str, Any]):  # type: ignore[no-untyped-def]
    return await client.post(
        "/peer/send",
        json={"kind": "notice", "from": "kal-le", "payload": payload},
        headers=_kalle_peer_headers(),
    )


@pytest.fixture
async def salem_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Web-mounted Salem app with the notify store under tmp_path/data."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    app = _build_salem_app(tmp_path, data_dir=str(data_dir))
    app["_t_data_dir"] = str(data_dir)
    return await aiohttp_client(app)


# ---------------------------------------------------------------------------
# Sink: notice + web_notify enqueues to the operator, relay unchanged
# ---------------------------------------------------------------------------


async def test_notice_web_notify_enqueues_and_get_returns_it(
    salem_client,
) -> None:
    """LOAD-BEARING fan-out: a kal-le notice tagged web_notify lands in the
    store keyed to the OPERATOR and comes back on GET — while the Telegram
    relay (the fake inbox) still ran unchanged."""
    resp = await _push_notice(salem_client, _notice_payload())
    assert resp.status == 200

    # The Telegram relay path (peer inbox) ran, unchanged.
    relayed = salem_client.app["_t_relayed"]
    assert len(relayed) == 1
    assert relayed[0]["kind"] == "notice"

    r = await salem_client.get(
        "/chat/notifications", headers=_session_headers(),
    )
    assert r.status == 200
    body = await r.json()
    assert body["unread"] == 1
    assert len(body["notifications"]) == 1
    item = body["notifications"][0]
    assert item["id"]
    assert item["text"].startswith("New ticket [bug]")
    assert item["precedence"] == "R"
    assert item["source"] == "kal-le"
    assert item["ticket_uid"] == TICKET_UID
    assert item["issue_url"] == "https://github.com/acme/site/issues/7"
    assert item["ts"]
    assert item["read"] is False

    # Keyed to the operator's synthetic id (v1 single-user ruling).
    store = salem_client.app[KEY_WEB_NOTIFY_STORE]
    assert str(synthetic_chat_id("andrew")) in store.notifications


async def test_notice_without_web_notify_not_enqueued(salem_client) -> None:
    """An untagged notice relays to Telegram ONLY — no store entry."""
    resp = await _push_notice(
        salem_client, _notice_payload(web_notify=False),
    )
    assert resp.status == 200
    assert len(salem_client.app["_t_relayed"]) == 1
    r = await salem_client.get(
        "/chat/notifications", headers=_session_headers(),
    )
    assert (await r.json()) == {"notifications": [], "unread": 0}


async def test_sink_not_wired_logged_skip_relay_still_runs(
    aiohttp_client, tmp_path,
) -> None:
    """LOAD-BEARING degrade: no web mount (sink absent) → the tagged
    notice still relays to Telegram, the skip is LOGGED, no crash."""
    app = _build_salem_app(tmp_path, mount_web=False)
    client = await aiohttp_client(app)
    with structlog.testing.capture_logs() as captured:
        resp = await _push_notice(client, _notice_payload())
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "accepted"
    assert body["relayed"] is True
    assert len(app["_t_relayed"]) == 1
    skips = _log_events(captured, "transport.peer.web_notify_sink_absent")
    assert len(skips) == 1


async def test_sink_raising_relay_still_runs(
    aiohttp_client, tmp_path,
) -> None:
    """A sink blow-up is swallowed + logged; the relay + ack proceed."""
    from alfred.transport.peer_handlers import register_web_notify_sink

    app = _build_salem_app(tmp_path, mount_web=False)

    def _boom(**_: Any) -> None:
        raise RuntimeError("store disk full")

    register_web_notify_sink(app, _boom)
    client = await aiohttp_client(app)
    with structlog.testing.capture_logs() as captured:
        resp = await _push_notice(client, _notice_payload())
    assert resp.status == 200
    assert len(app["_t_relayed"]) == 1
    fails = _log_events(captured, "transport.peer.web_notify_sink_failed")
    assert len(fails) == 1


# ---------------------------------------------------------------------------
# Read routes: auth layers (peer token, PEER-PIN, session, recipient-pin)
# ---------------------------------------------------------------------------


async def test_notifications_require_peer_token(salem_client) -> None:
    assert (await salem_client.get("/chat/notifications")).status == 401
    assert (
        await salem_client.post("/chat/notifications/ack", json={"ids": ["x"]})
    ).status == 401


async def test_notifications_missing_or_invalid_session_401(
    salem_client,
) -> None:
    """Fail-closed 401 exactly like /chat/history — both routes."""
    for headers in (
        _PEER_HEADERS,
        {**_PEER_HEADERS, SESSION_HEADER: "garbage.token"},
    ):
        r = await salem_client.get("/chat/notifications", headers=headers)
        assert r.status == 401
        assert (await r.json())["error"] == "invalid_session"
        r = await salem_client.post(
            "/chat/notifications/ack", json={"ids": ["x"]}, headers=headers,
        )
        assert r.status == 401
        assert (await r.json())["error"] == "invalid_session"


async def test_notifications_web_ingest_peer_pinned_out(salem_client) -> None:
    """LOAD-BEARING peer-pin: a VALID web_ingest token + X-Alfred-Client:
    web + a VALID session token still cannot read or ack — 401 BEFORE
    identity resolution."""
    await _push_notice(salem_client, _notice_payload())  # real content
    with structlog.testing.capture_logs() as captured:
        r = await salem_client.get(
            "/chat/notifications", headers=_ingest_peer_session_headers(),
        )
    assert r.status == 401
    assert (await r.json())["error"] == "wrong_peer"
    events = _log_events(captured, "web.notify.wrong_peer")
    assert len(events) == 1
    assert events[0]["peer"] == "web_ingest"

    r = await salem_client.post(
        "/chat/notifications/ack",
        json={"ids": ["x"]},
        headers=_ingest_peer_session_headers(),
    )
    assert r.status == 401
    assert (await r.json())["error"] == "wrong_peer"


@pytest.fixture
async def relay_salem_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Relay-mode app (no session secret) so the vouched rrts_relay
    identity actually resolves — the recipient-pin 403 surface."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    web_config = WebConfig(
        enabled=True,
        users=[WebUser(name="andrew", role="owner")],
        auth=WebAuthConfig(mode="relay", session_secret=""),
    )
    app = _build_salem_app(
        tmp_path, web_config=web_config, data_dir=str(data_dir),
    )
    return await aiohttp_client(app)


async def test_vouched_reporter_identity_403_on_read_and_ack(
    relay_salem_client,
) -> None:
    """LOAD-BEARING recipient-pin: the vouched rrts_relay identity
    (RRTS_INTAKE_ROLE) clears the peer layers but is REFUSED with an
    explicit 403 — a reporter must not read (or ack) the operator's
    notifications."""
    headers = {
        "Authorization": f"Bearer {DUMMY_RRTS_RELAY_TOKEN}",
        "X-Alfred-Client": "web",
        USER_HEADER: "Dana Dispatcher",
    }
    with structlog.testing.capture_logs() as captured:
        r = await relay_salem_client.get("/chat/notifications", headers=headers)
    assert r.status == 403
    assert (await r.json())["error"] == "forbidden"
    assert len(_log_events(captured, "web.notify.reporter_refused")) == 1

    r = await relay_salem_client.post(
        "/chat/notifications/ack", json={"ids": ["x"]}, headers=headers,
    )
    assert r.status == 403
    assert (await r.json())["error"] == "forbidden"


async def test_relay_mode_operator_still_reads(relay_salem_client) -> None:
    """The relay-mode OWNER path (web peer + asserted roster name) still
    reads its tray — the 403 pins the ROLE, not relay mode itself."""
    headers = {**_PEER_HEADERS, USER_HEADER: "andrew"}
    r = await relay_salem_client.get("/chat/notifications", headers=headers)
    assert r.status == 200
    assert (await r.json()) == {"notifications": [], "unread": 0}


# ---------------------------------------------------------------------------
# Read routes: ILB empty, ack flow, store-absent, bad ack bodies
# ---------------------------------------------------------------------------


async def test_empty_tray_ilb_200(salem_client) -> None:
    """ILB: an empty tray is an explicit 200 {[], 0} + a log — never 404."""
    with structlog.testing.capture_logs() as captured:
        r = await salem_client.get(
            "/chat/notifications", headers=_session_headers(),
        )
    assert r.status == 200
    assert (await r.json()) == {"notifications": [], "unread": 0}
    assert any(c.get("event") == "web.notify.empty" for c in captured)


async def test_ack_marks_read_and_unread_decrements(salem_client) -> None:
    await _push_notice(salem_client, _notice_payload())
    await _push_notice(
        salem_client,
        _notice_payload(ticket_uid="vera-20260719-0002", text="Second"),
    )
    r = await salem_client.get(
        "/chat/notifications", headers=_session_headers(),
    )
    body = await r.json()
    assert body["unread"] == 2
    first_id = body["notifications"][0]["id"]

    r = await salem_client.post(
        "/chat/notifications/ack",
        json={"ids": [first_id]},
        headers=_session_headers(),
    )
    assert r.status == 200
    assert (await r.json()) == {"acked": 1, "unread": 1}

    r = await salem_client.get(
        "/chat/notifications", headers=_session_headers(),
    )
    body = await r.json()
    assert body["unread"] == 1
    by_id = {e["id"]: e for e in body["notifications"]}
    assert by_id[first_id]["read"] is True

    # Idempotent re-ack.
    r = await salem_client.post(
        "/chat/notifications/ack",
        json={"ids": [first_id]},
        headers=_session_headers(),
    )
    assert (await r.json()) == {"acked": 0, "unread": 1}


@pytest.mark.parametrize(
    "body",
    [
        {},                     # ids absent
        {"ids": "not-a-list"},
        {"ids": [1, 2]},        # non-str entries
        {"ids": [""]},          # empty id
        {"ids": ["x"] * 201},   # over MAX_ACK_IDS
    ],
)
async def test_ack_invalid_ids_400(salem_client, body) -> None:
    r = await salem_client.post(
        "/chat/notifications/ack", json=body, headers=_session_headers(),
    )
    assert r.status == 400
    assert (await r.json())["error"] == "invalid_ids"


async def test_store_absent_no_data_dir_ilb(aiohttp_client, tmp_path) -> None:
    """A mount site that doesn't thread data_dir still serves the ILB
    empty payload (routes mounted, store None, never a crash)."""
    app = _build_salem_app(tmp_path, data_dir=None)
    client = await aiohttp_client(app)
    r = await client.get("/chat/notifications", headers=_session_headers())
    assert r.status == 200
    assert (await r.json()) == {"notifications": [], "unread": 0}
    r = await client.post(
        "/chat/notifications/ack", json={"ids": ["x"]},
        headers=_session_headers(),
    )
    assert r.status == 200
    assert (await r.json()) == {"acked": 0, "unread": 0}


async def test_notifications_disabled_routes_not_mounted(
    aiohttp_client, tmp_path,
) -> None:
    """Opt-out inertness: web.notifications.enabled=false mounts nothing."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    app = _build_salem_app(
        tmp_path,
        web_config=_web_config(
            notifications=WebNotificationsConfig(enabled=False),
        ),
        data_dir=str(data_dir),
    )
    client = await aiohttp_client(app)
    r = await client.get("/chat/notifications", headers=_session_headers())
    assert r.status == 404


# ---------------------------------------------------------------------------
# PRODUCER (KAL-LE): created-only idempotency + best-effort
# ---------------------------------------------------------------------------


class FakeGitHubClient:
    """Same intake-op surface as test_ticket_intake's fake."""

    def __init__(
        self,
        audit_path: Path,
        *,
        search_result: dict[str, Any] | None = None,
        search_exc: BaseException | None = None,
        create_exc: BaseException | None = None,
    ) -> None:
        self.config = GitHubOpsConfig(
            repo="acme/site",
            pat="DUMMY_GITHUB_TEST_PAT",
            instance="KAL-LE",
            labels=["auto-fix"],
            label_map={"bug": "bug", "high": "priority-high"},
            audit_log_path=str(audit_path),
        )
        self.search_result = search_result
        self.search_exc = search_exc
        self.create_exc = create_exc
        self.create_result: dict[str, Any] = {
            "number": 7,
            "html_url": "https://github.com/acme/site/issues/7",
        }

    async def issue_search_marker(
        self, *, ticket_uid: str, caller: str, correlation_id: str = "",
    ) -> dict[str, Any] | None:
        if self.search_exc is not None:
            raise self.search_exc
        return self.search_result

    async def issue_create(
        self, *, title: str, body: str, labels: list[str], ticket_uid: str,
        caller: str, correlation_id: str = "",
    ) -> dict[str, Any]:
        if self.create_exc is not None:
            raise self.create_exc
        return dict(self.create_result)


def _kalle_transport_config(*, with_salem_peer: bool = True) -> TransportConfig:
    return TransportConfig(
        server=ServerConfig(),
        auth=AuthConfig(
            tokens={
                "vera": AuthTokenEntry(
                    token=DUMMY_VERA_PEER_TOKEN, allowed_clients=["vera"],
                ),
            }
        ),
        state=StateConfig(),
        peers=(
            {
                "salem": PeerEntry(
                    base_url="http://127.0.0.1:1",
                    token=DUMMY_SALEM_OUT_TOKEN,
                ),
            }
            if with_salem_peer
            else {}
        ),
    )


async def _build_kalle_app(
    aiohttp_client, tmp_path, *, fake_client, with_salem_peer: bool = True,
    public_base_url: str = "",
):  # type: ignore[no-untyped-def]
    config = _kalle_transport_config(with_salem_peer=with_salem_peer)
    state = TransportState.create(tmp_path / "transport_state.json")
    vault_root = tmp_path / "vault"
    vault_root.mkdir(exist_ok=True)
    app = build_app(config, state)
    register_vault_path(app, vault_root)
    register_instance_identity(app, name="KAL-LE")
    register_ticket_intake(
        app,
        intake_config=TicketIntakeConfig(
            enabled=True,
            state_path=str(tmp_path / "ticket_intake_state.json"),
            # #63b — default empty keeps every existing test on the
            # unconfigured path, which is also production's default.
            public_base_url=public_base_url,
        ),
        github_client=fake_client,
    )
    return await aiohttp_client(app)


def _ticket_payload(**overrides: Any) -> dict[str, Any]:
    frontmatter = {
        "type": "ticket",
        "title": "Login button broken",
        "ticket_type": "bug",
        "reporter": "Ben",
        "area": "checkout",
        "ticket_uid": TICKET_UID,
    }
    payload: dict[str, Any] = {
        "precedence": "R",
        "ticket_uid": TICKET_UID,
        "relpath": "ticket/Login button broken.md",
        "frontmatter": frontmatter,
        "body": "## Repro\n1. Click login\n",
    }
    payload.update(overrides)
    return payload


async def _push_ticket(client, payload):  # type: ignore[no-untyped-def]
    return await client.post(
        "/peer/send",
        json={"kind": "ticket", "from": "vera", "payload": payload},
        headers={
            "Authorization": f"Bearer {DUMMY_VERA_PEER_TOKEN}",
            "X-Alfred-Client": "vera",
        },
    )


@pytest.fixture
def notify_recorder(monkeypatch):  # type: ignore[no-untyped-def]
    """Record every outbound notify peer_send instead of hitting HTTP."""
    calls: list[dict[str, Any]] = []

    async def _fake_peer_send(
        peer_name: str, kind: str, payload: dict[str, Any], *,
        config: Any = None, self_name: str = "", correlation_id: str | None = None,
    ) -> dict[str, Any]:
        calls.append({
            "peer_name": peer_name, "kind": kind, "payload": payload,
            "self_name": self_name, "correlation_id": correlation_id,
        })
        return {"status": "accepted"}

    monkeypatch.setattr("alfred.transport.client.peer_send", _fake_peer_send)
    return calls


async def test_created_emits_exactly_one_notify(
    aiohttp_client, tmp_path, notify_recorder,
) -> None:
    """LOAD-BEARING created-only: the ``created`` ack emits EXACTLY ONE
    notify peer_send with the ratified payload shape."""
    fake = FakeGitHubClient(tmp_path / "audit.jsonl")
    client = await _build_kalle_app(aiohttp_client, tmp_path, fake_client=fake)

    resp = await _push_ticket(client, _ticket_payload())
    assert resp.status == 200
    assert (await resp.json())["status"] == "created"

    assert len(notify_recorder) == 1
    call = notify_recorder[0]
    assert call["peer_name"] == "salem"
    assert call["kind"] == "notice"
    assert call["self_name"] == "kal-le"
    p = call["payload"]
    assert p["web_notify"] is True
    assert p["precedence"] == "R"
    assert p["source"] == "kal-le"
    assert p["ticket_uid"] == TICKET_UID
    assert p["issue_url"] == "https://github.com/acme/site/issues/7"
    assert "Login button broken" in p["text"]
    assert "#7" in p["text"]


# --- #63b: the link the operator's PHONE receives -------------------------
# These drive the REAL notify path end-to-end rather than the URL helper alone.
# The helper is thoroughly unit-pinned in tests/test_ticket_notify_public_url.py,
# and every one of those pins stays green if `_notify_ticket_created_web` never
# calls it — a pure function nobody invokes is the classic accepted-then-ignored
# shape. Only a test that pushes a real ticket and reads the emitted payload
# can tell the difference.

BOX_LOCAL_ISSUE = "http://localhost:3001/andrew/algernon/issues/7"


async def test_box_local_issue_link_is_labelled_when_no_public_base(
    aiohttp_client, tmp_path, notify_recorder,
) -> None:
    """THE #63b pin. On-box forgejo returns a localhost URL; the notice goes to
    the operator's phone. With no public origin configured the link is still
    carried — it works at the desk — but it arrives SAID to be box-local, so a
    tap that fails is expected rather than baffling."""
    fake = FakeGitHubClient(tmp_path / "audit.jsonl")
    fake.create_result = {"number": 7, "html_url": BOX_LOCAL_ISSUE}
    client = await _build_kalle_app(aiohttp_client, tmp_path, fake_client=fake)

    assert (await (await _push_ticket(client, _ticket_payload())).json())[
        "status"
    ] == "created"

    p = notify_recorder[0]["payload"]
    assert "box-local" in p["text"].lower(), "an unreachable link must say so"
    assert BOX_LOCAL_ISSUE in p["text"]
    assert p["issue_url"] == BOX_LOCAL_ISSUE, "labelled, not withheld"


async def test_a_configured_public_base_rewrites_the_link_end_to_end(
    aiohttp_client, tmp_path, notify_recorder,
) -> None:
    """With the public origin configured the operator gets a link that works
    from anywhere — and no apology, because none is owed.

    Asserts BOTH surfaces: the prose (Telegram relay) and the structured
    ``issue_url`` (the PWA's anchor). Rewriting one and not the other would
    show him a working link and navigate him to a dead one, or vice-versa.
    """
    fake = FakeGitHubClient(tmp_path / "audit.jsonl")
    fake.create_result = {"number": 7, "html_url": BOX_LOCAL_ISSUE}
    client = await _build_kalle_app(
        aiohttp_client, tmp_path, fake_client=fake,
        public_base_url="https://forge.example.com",
    )

    assert (await (await _push_ticket(client, _ticket_payload())).json())[
        "status"
    ] == "created"

    p = notify_recorder[0]["payload"]
    expected = "https://forge.example.com/andrew/algernon/issues/7"
    assert p["issue_url"] == expected
    assert expected in p["text"]
    assert "localhost" not in p["text"]
    assert "box-local" not in p["text"].lower()


async def test_an_already_public_issue_link_is_untouched_and_unlabelled(
    aiohttp_client, tmp_path, notify_recorder,
) -> None:
    """A GitHub-backed instance needs no config and must gain no label —
    labelling a working link teaches the operator to distrust good ones."""
    fake = FakeGitHubClient(tmp_path / "audit.jsonl")  # public html_url default
    client = await _build_kalle_app(aiohttp_client, tmp_path, fake_client=fake)

    assert (await (await _push_ticket(client, _ticket_payload())).json())[
        "status"
    ] == "created"

    p = notify_recorder[0]["payload"]
    assert p["issue_url"] == "https://github.com/acme/site/issues/7"
    assert "box-local" not in p["text"].lower()


async def test_exists_repush_emits_zero_notifies(
    aiohttp_client, tmp_path, notify_recorder,
) -> None:
    """LOAD-BEARING idempotency: a VERA re-push landing on the ``exists``
    dedupe path must NOT re-notify — total stays at the create's one."""
    fake = FakeGitHubClient(tmp_path / "audit.jsonl")
    client = await _build_kalle_app(aiohttp_client, tmp_path, fake_client=fake)

    assert (await (await _push_ticket(client, _ticket_payload())).json())[
        "status"
    ] == "created"
    assert len(notify_recorder) == 1

    resp2 = await _push_ticket(client, _ticket_payload())
    assert (await resp2.json())["status"] == "exists"
    assert len(notify_recorder) == 1  # ZERO new — created-only


async def test_adopted_emits_zero_notifies(
    aiohttp_client, tmp_path, notify_recorder,
) -> None:
    """The marker-search adopt path (issue already on GitHub) must not
    notify — the original create either already did or was lost with the
    state; adopting is not creating."""
    fake = FakeGitHubClient(
        tmp_path / "audit.jsonl",
        search_result={
            "number": 55,
            "html_url": "https://github.com/acme/site/issues/55",
        },
    )
    client = await _build_kalle_app(aiohttp_client, tmp_path, fake_client=fake)
    resp = await _push_ticket(client, _ticket_payload())
    assert (await resp.json())["status"] == "adopted"
    assert notify_recorder == []


async def test_pending_then_repush_notifies_once_on_the_create(
    aiohttp_client, tmp_path, notify_recorder,
) -> None:
    """recorded_issue_pending emits ZERO; the successful re-push's
    ``created`` emits the single notify."""
    import httpx

    fake = FakeGitHubClient(
        tmp_path / "audit.jsonl",
        create_exc=httpx.ConnectError("github down"),
    )
    client = await _build_kalle_app(aiohttp_client, tmp_path, fake_client=fake)

    resp = await _push_ticket(client, _ticket_payload())
    assert (await resp.json())["status"] == "recorded_issue_pending"
    assert notify_recorder == []

    fake.create_exc = None  # GitHub recovers
    resp2 = await _push_ticket(client, _ticket_payload())
    assert (await resp2.json())["status"] == "created"
    assert len(notify_recorder) == 1


async def test_notify_peer_unconfigured_logged_skip(
    aiohttp_client, tmp_path, notify_recorder,
) -> None:
    """ILB: no ``salem`` in transport.peers → logged skip, no send, ack
    unaffected."""
    fake = FakeGitHubClient(tmp_path / "audit.jsonl")
    client = await _build_kalle_app(
        aiohttp_client, tmp_path, fake_client=fake, with_salem_peer=False,
    )
    with structlog.testing.capture_logs() as captured:
        resp = await _push_ticket(client, _ticket_payload())
    assert (await resp.json())["status"] == "created"
    assert notify_recorder == []
    skips = _log_events(captured, "transport.ticket.web_notify_skipped")
    assert len(skips) == 1
    assert skips[0]["notify_peer"] == "salem"


async def test_notify_failure_never_fails_the_ack(
    aiohttp_client, tmp_path, monkeypatch,
) -> None:
    """LOAD-BEARING best-effort: peer_send raising is swallowed + logged;
    the ticket ack is still a clean 200 ``created``."""

    async def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("salem unreachable")

    monkeypatch.setattr("alfred.transport.client.peer_send", _boom)

    fake = FakeGitHubClient(tmp_path / "audit.jsonl")
    client = await _build_kalle_app(aiohttp_client, tmp_path, fake_client=fake)
    with structlog.testing.capture_logs() as captured:
        resp = await _push_ticket(client, _ticket_payload())
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "created"
    assert body["issue_number"] == 7
    fails = _log_events(captured, "transport.ticket.web_notify_failed")
    assert len(fails) == 1
    assert "salem unreachable" in fails[0]["error"]
