"""Tests for ``alfred.web.routes_day`` — the C4 ``/day/*`` surface.

Load-bearing pins:

* **PEER-PIN BEFORE IDENTITY** — the fixture configures the PRODUCTION peer key
  names (``web`` / ``web_ingest`` / ``web_feed``, all carrying
  ``allowed_clients: [web]``), so the wrong-peer requests present tokens that
  genuinely clear Layer 1. The ``web_feed`` case matters most here: the design
  brief said to copy the feed routes' auth, and the feed peer carries NO user
  identity — pinning it out is what stops a token with no operator behind it
  reading the operator's day. Refusals assert the LOGGED REASON, not just the
  401: a denial for an unrelated cause looks identical from the outside.
* **RECIPIENT-PIN** — a vouched ``rrts_intake`` reporter is 403'd. They are
  authenticated enough to chat and must never read the operator's day nor write
  into the operator's own contact log.
* **THE CLIENT NEVER SENDS STATE** — the triggering-state snapshot is computed
  server-side, so a client cannot write the evidence its own patterns are
  detected from.
* **AN UNWIRED STORE IS A 200 THAT SAYS SO** — never a 500, never a silent
  success.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import structlog

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
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState
from alfred.vault.scope import RRTS_INTAKE_ROLE
from alfred.web.auth import SESSION_HEADER, make_session_token
from alfred.web.config import (
    WebAuthConfig,
    WebConfig,
    WebContactRouterConfig,
    WebUser,
)
from alfred.web.contact_state import (
    RULE_DEFAULT,
    RULE_RESUME_PENDING_CAPTURE,
    SURFACE_BRIEF,
    SURFACE_CHAT,
    SURFACE_FEED,
    WebContactStore,
)
from alfred.web.identity import synthetic_chat_id
from alfred.web.routes_chat import register_web_routes
from alfred.web.state import WebAuthState

from tests.telegram.conftest import (
    FakeAnthropicClient,
    FakeBlock,
    FakeResponse,
)

# Obviously-fake test secrets — never a real provider prefix (builder.md rule).
DUMMY_WEB_PEER_TOKEN = "DUMMY_WEB_PEER_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_0123456"
DUMMY_WEB_INGEST_TOKEN = "DUMMY_WEB_INGEST_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_01234"
DUMMY_WEB_FEED_TOKEN = "DUMMY_WEB_FEED_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_012345"
DUMMY_WEB_SIGNING_SECRET = "DUMMY_WEB_SIGNING_SECRET_FOR_TESTING_ONLY_0123456789"

_PEER_HEADERS = {
    "Authorization": f"Bearer {DUMMY_WEB_PEER_TOKEN}",
    "X-Alfred-Client": "web",
}

OPERATOR = "andrew"
OPERATOR_KEY = str(synthetic_chat_id(OPERATOR))


def _session_headers(name: str = OPERATOR, role: str = "owner") -> dict[str, str]:
    token = make_session_token(
        name, role, secret=DUMMY_WEB_SIGNING_SECRET, ttl_hours=168
    )
    return {**_PEER_HEADERS, SESSION_HEADER: token}


def _other_peer_headers(token: str) -> dict[str, str]:
    """A VALID Layer-1 token for a sibling peer + a VALID session token.

    Both siblings carry ``allowed_clients: [web]``, so these requests clear
    ``auth_middleware`` and are refused only by the handler's peer-pin.
    """
    session = make_session_token(
        OPERATOR, "owner", secret=DUMMY_WEB_SIGNING_SECRET, ttl_hours=168
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Alfred-Client": "web",
        SESSION_HEADER: session,
    }


def _make_talker_config(tmp_path: Path) -> TalkerConfig:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    for sub in ("session", "task", "note", "preference"):
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


def _transport_config() -> TransportConfig:
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
                "web_feed": AuthTokenEntry(
                    token=DUMMY_WEB_FEED_TOKEN, allowed_clients=["web"],
                ),
            }
        ),
        state=StateConfig(),
    )


def _web_config(state_path: str, *, enabled: bool = True) -> WebConfig:
    return WebConfig(
        enabled=True,
        users=[
            WebUser(name=OPERATOR, role="owner"),
            WebUser(name="reporter", role=RRTS_INTAKE_ROLE),
        ],
        auth=WebAuthConfig(session_secret=DUMMY_WEB_SIGNING_SECRET),
        contact_router=WebContactRouterConfig(
            enabled=enabled, state_path=state_path,
        ),
    )


def _build(tmp_path: Path, *, state_path: str, enabled: bool = True):
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    web_auth_state = WebAuthState.create(tmp_path / "web_auth_state.json")
    web_auth_state.load()
    register_web_routes(
        app,
        web_config=_web_config(state_path, enabled=enabled),
        web_auth_state=web_auth_state,
        anthropic_client=FakeAnthropicClient(
            [FakeResponse(content=[FakeBlock(type="text", text="hi")])]
        ),
        state_mgr=state_mgr,
        talker_config=_make_talker_config(tmp_path),
        system_prompt_provider=lambda: "SYSTEM",
        vault_context_str="CTX",
        allowed_user_ids=[1],
        data_dir=str(tmp_path),
    )
    return app


@pytest.fixture
async def client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Web app with the contact store wired at ``tmp_path/contacts.json``."""
    path = tmp_path / "contacts.json"
    app = _build(tmp_path, state_path=str(path))
    c = await aiohttp_client(app)
    c._contact_path = path  # type: ignore[attr-defined]
    return c


@pytest.fixture
async def unwired_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Router enabled but NO state path anchored — the honest-inert posture."""
    return await aiohttp_client(_build(tmp_path, state_path=""))


# ---------------------------------------------------------------------------
# Auth spine
# ---------------------------------------------------------------------------


class TestAuthSpine:
    @pytest.mark.parametrize(
        "method,path,body",
        [
            ("get", "/day/state", None),
            ("post", "/day/contact", {"rule": RULE_DEFAULT, "surface": SURFACE_CHAT}),
            ("post", "/day/override", {"contact_id": "x", "surface": SURFACE_CHAT}),
        ],
    )
    async def test_no_session_is_401_on_every_route(self, client, method, path, body):
        resp = await getattr(client, method)(path, headers=_PEER_HEADERS, json=body)
        assert resp.status == 401
        assert (await resp.json())["error"] == "invalid_session"

    @pytest.mark.parametrize(
        "token,peer",
        [
            (DUMMY_WEB_INGEST_TOKEN, "web_ingest"),
            (DUMMY_WEB_FEED_TOKEN, "web_feed"),
        ],
    )
    async def test_a_sibling_peer_is_pinned_out_for_the_RIGHT_reason(
        self, client, token, peer
    ):
        """A 401 alone is not evidence the pin fired — an unrelated refusal
        looks identical. The logged reason is what distinguishes them."""
        with structlog.testing.capture_logs() as captured:
            resp = await client.get("/day/state", headers=_other_peer_headers(token))
        assert resp.status == 401
        assert (await resp.json())["error"] == "wrong_peer"
        denials = [c for c in captured if c["event"] == "web.day.wrong_peer"]
        assert len(denials) == 1
        assert denials[0]["reason"] == "wrong_peer"
        assert denials[0]["peer"] == peer

    async def test_the_chat_web_peer_IS_accepted(self, client):
        """Positive control for the pin: the peer the routes DO want clears it,
        so the refusals above are the pin biting rather than a broken route."""
        resp = await client.get("/day/state", headers=_session_headers())
        assert resp.status == 200

    async def test_a_vouched_reporter_is_refused_403_on_read(self, client):
        with structlog.testing.capture_logs() as captured:
            resp = await client.get(
                "/day/state",
                headers=_session_headers("reporter", RRTS_INTAKE_ROLE),
            )
        assert resp.status == 403
        assert (await resp.json())["error"] == "forbidden"
        assert any(c["event"] == "web.day.reporter_refused" for c in captured)

    async def test_a_vouched_reporter_cannot_write_a_contact(self, client):
        resp = await client.post(
            "/day/contact",
            headers=_session_headers("reporter", RRTS_INTAKE_ROLE),
            json={"rule": RULE_DEFAULT, "surface": SURFACE_CHAT},
        )
        assert resp.status == 403
        # And nothing landed in the operator's log.
        store = WebContactStore.create(client._contact_path)
        store.load()
        assert store.contacts_for(OPERATOR_KEY) == []


# ---------------------------------------------------------------------------
# GET /day/state
# ---------------------------------------------------------------------------


class TestDayState:
    async def test_it_serves_the_armed_rules_and_the_levers(self, client):
        body = await (await client.get("/day/state", headers=_session_headers())).json()
        assert body["configured"] is True
        assert RULE_RESUME_PENDING_CAPTURE not in body["armed_rules"]
        assert RULE_RESUME_PENDING_CAPTURE in body["unarmed_rules"]
        assert body["levers"]["gap_hours_new_day"] > 0

    async def test_an_unwired_router_says_configured_false(self, unwired_client):
        resp = await unwired_client.get("/day/state", headers=_session_headers())
        assert resp.status == 200
        assert (await resp.json())["configured"] is False


# ---------------------------------------------------------------------------
# POST /day/contact
# ---------------------------------------------------------------------------


class TestContact:
    async def test_a_contact_is_recorded_against_the_callers_own_key(self, client):
        resp = await client.post(
            "/day/contact",
            headers=_session_headers(),
            json={"rule": RULE_DEFAULT, "surface": SURFACE_CHAT},
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["recorded"] is True
        assert body["contact_id"]

        store = WebContactStore.create(client._contact_path)
        store.load()
        [entry] = store.contacts_for(OPERATOR_KEY)
        assert entry["id"] == body["contact_id"]
        assert entry["rule"] == RULE_DEFAULT
        assert entry["surface"] == SURFACE_CHAT

    async def test_the_triggering_state_is_the_SERVERS_not_the_clients(self, client):
        """A client-supplied snapshot would let a buggy or replayed client write
        the evidence its own patterns are later detected from."""
        resp = await client.post(
            "/day/contact",
            headers=_session_headers(),
            json={
                "rule": RULE_DEFAULT,
                "surface": SURFACE_CHAT,
                "state": {"unresolved_flagged_notifications": 99, "hacked": True},
            },
        )
        assert resp.status == 200
        store = WebContactStore.create(client._contact_path)
        store.load()
        [entry] = store.contacts_for(OPERATOR_KEY)
        assert entry["state"]["unresolved_flagged_notifications"] == 0
        assert "hacked" not in entry["state"]

    @pytest.mark.parametrize(
        "body,err",
        [
            ({"surface": SURFACE_CHAT}, "invalid_rule"),
            ({"rule": "made_up", "surface": SURFACE_CHAT}, "invalid_rule"),
            # An UNARMED rule is refused: the two sides disagree about what is
            # live, and storing it would poison the pattern evidence.
            (
                {"rule": RULE_RESUME_PENDING_CAPTURE, "surface": SURFACE_CHAT},
                "invalid_rule",
            ),
            ({"rule": RULE_DEFAULT}, "invalid_surface"),
            ({"rule": RULE_DEFAULT, "surface": "hologram"}, "invalid_surface"),
        ],
    )
    async def test_bad_vocabulary_is_400(self, client, body, err):
        resp = await client.post(
            "/day/contact", headers=_session_headers(), json=body
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == err

    async def test_a_malformed_body_is_400(self, client):
        resp = await client.post(
            "/day/contact",
            headers={**_session_headers(), "Content-Type": "application/json"},
            data="{not json",
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == "invalid_json"

    async def test_an_unwired_store_answers_200_recorded_false(self, unwired_client):
        with structlog.testing.capture_logs() as captured:
            resp = await unwired_client.post(
                "/day/contact",
                headers=_session_headers(),
                json={"rule": RULE_DEFAULT, "surface": SURFACE_CHAT},
            )
        assert resp.status == 200
        body = await resp.json()
        assert body == {"contact_id": "", "recorded": False}
        assert any(c["event"] == "web.day.contact_not_recorded" for c in captured)


# ---------------------------------------------------------------------------
# POST /day/override
# ---------------------------------------------------------------------------


class TestOverride:
    async def _contact(self, client) -> str:
        resp = await client.post(
            "/day/contact",
            headers=_session_headers(),
            json={"rule": RULE_DEFAULT, "surface": SURFACE_CHAT},
        )
        return (await resp.json())["contact_id"]

    async def test_an_override_moves_landed(self, client):
        cid = await self._contact(client)
        resp = await client.post(
            "/day/override",
            headers=_session_headers(),
            json={"contact_id": cid, "surface": SURFACE_FEED},
        )
        assert resp.status == 200
        assert (await resp.json())["recorded"] is True

        store = WebContactStore.create(client._contact_path)
        store.load()
        [entry] = store.contacts_for(OPERATOR_KEY)
        assert entry["overridden"] is True
        assert entry["landed"] == SURFACE_FEED
        assert entry["surface"] == SURFACE_CHAT

    async def test_an_unknown_contact_is_404_and_mints_nothing(self, client):
        with structlog.testing.capture_logs() as captured:
            resp = await client.post(
                "/day/override",
                headers=_session_headers(),
                json={"contact_id": "no-such-id", "surface": SURFACE_FEED},
            )
        assert resp.status == 404
        assert (await resp.json())["error"] == "unknown_contact"
        assert any(
            c["event"] == "web.day.override_unknown_contact" for c in captured
        )
        store = WebContactStore.create(client._contact_path)
        store.load()
        assert store.contacts_for(OPERATOR_KEY) == []

    @pytest.mark.parametrize(
        "body,err",
        [
            ({"surface": SURFACE_FEED}, "missing_contact_id"),
            ({"contact_id": "  ", "surface": SURFACE_FEED}, "missing_contact_id"),
            ({"contact_id": "x"}, "invalid_surface"),
            ({"contact_id": "x", "surface": "hologram"}, "invalid_surface"),
        ],
    )
    async def test_bad_override_bodies_are_400(self, client, body, err):
        resp = await client.post(
            "/day/override", headers=_session_headers(), json=body
        )
        assert resp.status == 400
        assert (await resp.json())["error"] == err

    async def test_with_no_feed_wired_the_override_still_records_and_says_so(
        self, client
    ):
        """The belt, at the route level: pattern-surfacing is an enrichment and
        the override is the payload."""
        cid = await self._contact(client)
        with structlog.testing.capture_logs() as captured:
            resp = await client.post(
                "/day/override",
                headers=_session_headers(),
                json={"contact_id": cid, "surface": SURFACE_BRIEF},
            )
        body = await resp.json()
        assert body["recorded"] is True
        assert body["patterns_surfaced"] == 0
        assert any(
            c["event"] == "web.day.pattern_surfacing_skipped" for c in captured
        )


# ---------------------------------------------------------------------------
# Mount inertness
# ---------------------------------------------------------------------------


class TestMounting:
    async def test_disabled_mounts_nothing(self, aiohttp_client, tmp_path):
        app = _build(tmp_path, state_path=str(tmp_path / "c.json"), enabled=False)
        c = await aiohttp_client(app)
        resp = await c.get("/day/state", headers=_session_headers())
        assert resp.status == 404

    async def test_the_wiring_logs_which_posture_it_took(self, tmp_path):
        with structlog.testing.capture_logs() as captured:
            _build(tmp_path, state_path=str(tmp_path / "c.json"))
        wired = [c for c in captured if c["event"] == "web.routes.contact_store_wired"]
        assert len(wired) == 1
        assert wired[0]["state_path"] == str(tmp_path / "c.json")

    async def test_an_unanchored_path_logs_the_skip(self, tmp_path):
        with structlog.testing.capture_logs() as captured:
            _build(tmp_path, state_path="")
        skips = [
            c for c in captured if c["event"] == "web.routes.contact_store_skipped"
        ]
        assert len(skips) == 1
        assert "no state path anchored" in skips[0]["reason"]
