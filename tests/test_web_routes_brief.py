"""Tests for ``alfred.web.routes_brief`` + ``alfred.web.outbound_store`` (#30).

PWA outbound READ-ON-OPEN: the brief / daily_sync daemons best-effort
spool their latest rendered markdown under ``<data_dir>/web_outbound/``;
``GET /web/outbound/{kind}/latest`` serves it back to the signed-in web
user.

Load-bearing pins (per the #30 design):

* **PEER-PIN** — the route pins ``transport_peer == "web"``
  (``WEB_CHAT_PEER``) BEFORE identity/read. The fixture configures the
  PRODUCTION peer key names (``web`` / ``web_ingest``, both
  ``allowed_clients: [web]`` — CLAUDE.md peer-pin requirement), so the
  wrong-peer test presents a token that genuinely clears Layer 1.
* **ROUND-TRIP** — a REAL ``generate_brief`` / ``fire_once`` run spools
  markdown the endpoint returns BYTE-IDENTICAL.
* **REGRESSION SWALLOW** — a ``write_latest`` failure inside
  ``generate_brief`` is swallowed + logged; the vault write AND the
  Telegram push still succeed.
* **ILB EMPTY** — an empty spool is 200 ``{date: null, markdown: null}``,
  never 404.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
from alfred.web.auth import SESSION_HEADER, make_session_token
from alfred.web.config import WebAuthConfig, WebConfig, WebUser
from alfred.web.outbound_store import read_latest, write_latest
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
DUMMY_WEB_SIGNING_SECRET = "DUMMY_WEB_SIGNING_SECRET_FOR_TESTING_ONLY_0123456789"

_PEER_HEADERS = {
    "Authorization": f"Bearer {DUMMY_WEB_PEER_TOKEN}",
    "X-Alfred-Client": "web",
}


def _session_headers(name: str = "andrew", role: str = "owner") -> dict[str, str]:
    """Peer headers + a valid Layer-2 session token for ``name``."""
    token = make_session_token(
        name, role, secret=DUMMY_WEB_SIGNING_SECRET, ttl_hours=168
    )
    return {**_PEER_HEADERS, SESSION_HEADER: token}


def _ingest_peer_session_headers() -> dict[str, str]:
    """The escalation attempt: a VALID Layer-1 ``web_ingest`` token +
    ``X-Alfred-Client: web`` (clears auth_middleware as peer
    ``web_ingest``) + a VALID session token for a known user. Must be
    peer-pinned out BEFORE identity resolution."""
    token = make_session_token(
        "andrew", "owner", secret=DUMMY_WEB_SIGNING_SECRET, ttl_hours=168
    )
    return {
        "Authorization": f"Bearer {DUMMY_WEB_INGEST_TOKEN}",
        "X-Alfred-Client": "web",
        SESSION_HEADER: token,
    }


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


def _transport_config() -> TransportConfig:
    """Transport config with the PRODUCTION peer key names: the chat
    ``web`` peer AND the sibling ``web_ingest`` peer, BOTH carrying
    ``allowed_clients: [web]`` — so the peer-pin test presents a
    ``web_ingest`` token that genuinely clears Layer 1 (CLAUDE.md
    "pin the test fixture's peer NAME to the production peer key")."""
    return TransportConfig(
        server=ServerConfig(),
        auth=AuthConfig(
            tokens={
                "web": AuthTokenEntry(
                    token=DUMMY_WEB_PEER_TOKEN,
                    allowed_clients=["web"],
                ),
                "web_ingest": AuthTokenEntry(
                    token=DUMMY_WEB_INGEST_TOKEN,
                    allowed_clients=["web"],
                ),
            }
        ),
        state=StateConfig(),
    )


def _web_config() -> WebConfig:
    return WebConfig(
        enabled=True,
        users=[WebUser(name="andrew", role="owner")],
        auth=WebAuthConfig(session_secret=DUMMY_WEB_SIGNING_SECRET),
    )


def _build_web_app(tmp_path: Path, data_dir: str | None):
    """A transport app with web routes mounted, outbound spool at data_dir."""
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)

    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    talker_config = _make_talker_config(tmp_path)
    web_auth_state = WebAuthState.create(tmp_path / "web_auth_state.json")
    web_auth_state.load()
    fake = FakeAnthropicClient(
        [FakeResponse(content=[FakeBlock(type="text", text="hello from salem")])]
    )
    register_web_routes(
        app,
        web_config=_web_config(),
        web_auth_state=web_auth_state,
        anthropic_client=fake,
        state_mgr=state_mgr,
        talker_config=talker_config,
        system_prompt_provider=lambda: "SYSTEM PROMPT",
        vault_context_str="VAULT CONTEXT",
        allowed_user_ids=[1],
        data_dir=data_dir,
    )
    return app


@pytest.fixture
async def outbound_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Web app whose outbound spool lives at ``tmp_path/data``."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    app = _build_web_app(tmp_path, str(data_dir))
    # Stash BEFORE aiohttp_client starts (and freezes) the app.
    app["_t_data_dir"] = str(data_dir)
    return await aiohttp_client(app)


@pytest.fixture
async def outbound_client_no_data_dir(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Web app registered WITHOUT data_dir (legacy call-site shape)."""
    return await aiohttp_client(_build_web_app(tmp_path, None))


# ---------------------------------------------------------------------------
# outbound_store unit behaviour
# ---------------------------------------------------------------------------


class TestOutboundStore:
    def test_write_read_roundtrip_byte_identical(self, tmp_path: Path) -> None:
        markdown = "# Brief\n\nUnicode — ✦ café\n\ntrailing newline\n"
        write_latest(tmp_path, "brief", "2026-07-19", markdown)
        got = read_latest(tmp_path, "brief")
        assert got is not None
        assert got["date"] == "2026-07-19"
        assert got["markdown"] == markdown  # byte-identical

    def test_rewrite_replaces_previous(self, tmp_path: Path) -> None:
        write_latest(tmp_path, "brief", "2026-07-18", "old")
        write_latest(tmp_path, "brief", "2026-07-19", "new")
        got = read_latest(tmp_path, "brief")
        assert got == {"date": "2026-07-19", "markdown": "new"}

    def test_read_absent_returns_none(self, tmp_path: Path) -> None:
        assert read_latest(tmp_path, "brief") is None
        assert read_latest(None, "brief") is None

    def test_corrupt_sidecar_treated_absent(self, tmp_path: Path) -> None:
        write_latest(tmp_path, "brief", "2026-07-19", "content")
        sidecar = tmp_path / "web_outbound" / "brief.json"
        sidecar.write_text("{not json", encoding="utf-8")
        assert read_latest(tmp_path, "brief") is None

    def test_corrupt_markdown_non_utf8_treated_absent(self, tmp_path: Path) -> None:
        # #25 read-class: a tampered / non-UTF-8 <kind>.md must degrade to
        # None. ``except OSError`` alone missed UnicodeDecodeError (a
        # ValueError) → the "never raises" contract leaked → route 500.
        write_latest(tmp_path, "brief", "2026-07-19", "content")
        (tmp_path / "web_outbound" / "brief.md").write_bytes(b"\xff\xfe\x00\x01 not utf-8")
        assert read_latest(tmp_path, "brief") is None

    def test_missing_sidecar_treated_absent(self, tmp_path: Path) -> None:
        write_latest(tmp_path, "brief", "2026-07-19", "content")
        (tmp_path / "web_outbound" / "brief.json").unlink()
        assert read_latest(tmp_path, "brief") is None

    def test_sidecar_without_date_treated_absent(self, tmp_path: Path) -> None:
        write_latest(tmp_path, "brief", "2026-07-19", "content")
        sidecar = tmp_path / "web_outbound" / "brief.json"
        sidecar.write_text(json.dumps({"other": 1}), encoding="utf-8")
        assert read_latest(tmp_path, "brief") is None

    def test_kinds_are_independent(self, tmp_path: Path) -> None:
        write_latest(tmp_path, "brief", "2026-07-19", "the brief")
        write_latest(tmp_path, "daily_sync", "2026-07-19", "the sync")
        assert read_latest(tmp_path, "brief")["markdown"] == "the brief"
        assert read_latest(tmp_path, "daily_sync")["markdown"] == "the sync"


# ---------------------------------------------------------------------------
# Route: auth layers (peer token, PEER-PIN, session)
# ---------------------------------------------------------------------------


async def test_outbound_requires_peer_token(outbound_client) -> None:
    # No Authorization header → Layer-1 auth_middleware rejects.
    resp = await outbound_client.get("/web/outbound/brief/latest")
    assert resp.status == 401


async def test_outbound_web_ingest_peer_pinned_out(outbound_client) -> None:
    """LOAD-BEARING peer-pin: a VALID Layer-1 ``web_ingest`` token +
    ``X-Alfred-Client: web`` + a VALID session token still cannot read
    the outbound spool — fail-closed 401 BEFORE identity resolution."""
    # Spool real content so a pin regression would actually leak it.
    write_latest(
        outbound_client.app["_t_data_dir"], "brief", "2026-07-19", "SECRET BRIEF",
    )
    with structlog.testing.capture_logs() as captured:
        resp = await outbound_client.get(
            "/web/outbound/brief/latest",
            headers=_ingest_peer_session_headers(),
        )
    assert resp.status == 401
    body = await resp.json()
    assert body["error"] == "wrong_peer"
    events = [c for c in captured if c.get("event") == "web.outbound.wrong_peer"]
    assert len(events) == 1
    assert events[0]["peer"] == "web_ingest"


async def test_outbound_missing_session_401(outbound_client) -> None:
    # Valid web peer token but NO X-Alfred-Session → fail-closed 401.
    resp = await outbound_client.get(
        "/web/outbound/brief/latest", headers=_PEER_HEADERS,
    )
    assert resp.status == 401
    assert (await resp.json())["error"] == "invalid_session"


async def test_outbound_invalid_session_401(outbound_client) -> None:
    resp = await outbound_client.get(
        "/web/outbound/brief/latest",
        headers={**_PEER_HEADERS, SESSION_HEADER: "garbage.token"},
    )
    assert resp.status == 401
    assert (await resp.json())["error"] == "invalid_session"


async def test_outbound_web_peer_valid_session_200(outbound_client) -> None:
    write_latest(
        outbound_client.app["_t_data_dir"], "brief", "2026-07-19", "# Hi\n",
    )
    resp = await outbound_client.get(
        "/web/outbound/brief/latest", headers=_session_headers(),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"kind": "brief", "date": "2026-07-19", "markdown": "# Hi\n"}


# ---------------------------------------------------------------------------
# Route: kind allowlist + ILB empty
# ---------------------------------------------------------------------------


async def test_outbound_unknown_kind_400(outbound_client) -> None:
    resp = await outbound_client.get(
        "/web/outbound/ticket/latest", headers=_session_headers(),
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "unknown_kind"


async def test_outbound_empty_returns_ilb_200(outbound_client) -> None:
    """ILB: no spool yet → 200 with explicit nulls, NEVER 404."""
    for kind in ("brief", "daily_sync"):
        with structlog.testing.capture_logs() as captured:
            resp = await outbound_client.get(
                f"/web/outbound/{kind}/latest", headers=_session_headers(),
            )
        assert resp.status == 200
        assert await resp.json() == {
            "kind": kind, "date": None, "markdown": None,
        }
        assert any(
            c.get("event") == "web.outbound.empty" for c in captured
        )


async def test_outbound_corrupt_markdown_returns_ilb_200(outbound_client) -> None:
    """#25 read-class at the route: a tampered / non-UTF-8 spooled ``<kind>.md``
    must NOT 500 — ``read_latest`` degrades to None and the route serves the ILB
    empty payload. Without the ``(OSError, ValueError)`` widen this 500s."""
    data_dir = outbound_client.app["_t_data_dir"]
    write_latest(data_dir, "brief", "2026-07-19", "# real\n")
    (Path(data_dir) / "web_outbound" / "brief.md").write_bytes(b"\xff\xfe\x00\x01 not utf-8")
    resp = await outbound_client.get(
        "/web/outbound/brief/latest", headers=_session_headers(),
    )
    assert resp.status == 200
    assert await resp.json() == {"kind": "brief", "date": None, "markdown": None}


async def test_outbound_no_data_dir_returns_ilb_200(
    outbound_client_no_data_dir,
) -> None:
    """A call site that doesn't thread data_dir still serves the ILB
    empty payload (never crashes)."""
    resp = await outbound_client_no_data_dir.get(
        "/web/outbound/brief/latest", headers=_session_headers(),
    )
    assert resp.status == 200
    assert await resp.json() == {"kind": "brief", "date": None, "markdown": None}


# ---------------------------------------------------------------------------
# ROUND-TRIP: generate_brief → spool → endpoint, byte-identical
# ---------------------------------------------------------------------------


def _brief_config(tmp_path: Path, **kwargs: Any):
    from alfred.brief.config import BriefConfig
    from alfred.brief.config import StateConfig as BriefStateConfig

    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    return BriefConfig(
        vault_path=str(vault),
        state=BriefStateConfig(path=str(data_dir / "brief_state.json")),
        **kwargs,
    )


async def test_generate_brief_roundtrip_byte_identical(
    outbound_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A REAL generate_brief run spools markdown that read_latest AND the
    endpoint return BYTE-IDENTICAL to the vault artifact."""
    from alfred.brief import daemon as brief_daemon_mod
    from alfred.brief.state import StateManager as BriefStateManager

    async def _fake_weather(config):  # type: ignore[no-untyped-def]
        # COLLECTING form: (markdown, parsed TAFs) — generate_brief takes
        # this one now so the feed reuses its fetch.
        return "Sunny, 21C.", []

    monkeypatch.setattr(brief_daemon_mod, "fetch_and_format_collect", _fake_weather)

    # The NARRATION path reaches weather by a function-local import of
    # alfred.brief.weather.fetch_metars, which the daemon patch above cannot
    # cover — without this, generate_brief makes a live aviationweather.gov
    # call. Rationale: tests/feed/test_brief_feed_parity.py::_patch_weather.
    async def _no_metars(_wc):  # type: ignore[no-untyped-def]
        return []

    monkeypatch.setattr("alfred.brief.weather.fetch_metars", _no_metars)

    # The brief's data dir IS the app fixture's spool dir (state file
    # lives under it → generate_brief derives data_dir from its parent).
    config = _brief_config(tmp_path)
    state_mgr = BriefStateManager(config.state.path)

    rel_path = await brief_daemon_mod.generate_brief(config, state_mgr)
    assert rel_path is not None
    vault_content = (
        Path(config.vault_path) / rel_path
    ).read_text(encoding="utf-8")

    data_dir = outbound_client.app["_t_data_dir"]
    spooled = read_latest(data_dir, "brief")
    assert spooled is not None
    assert spooled["markdown"] == vault_content  # byte-identical

    resp = await outbound_client.get(
        "/web/outbound/brief/latest", headers=_session_headers(),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["kind"] == "brief"
    assert body["date"] == spooled["date"]
    assert body["markdown"] == vault_content  # byte-identical over the wire


async def test_fire_once_roundtrip_byte_identical(
    outbound_client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A REAL daily_sync fire_once run spools the assembled body that the
    endpoint returns BYTE-IDENTICAL."""
    from datetime import date as date_cls

    import alfred.transport.client as transport_client_mod
    from alfred.daily_sync.config import DailySyncConfig
    from alfred.daily_sync.daemon import fire_once

    async def _fake_send_batch(
        user_id: int,
        chunks: list[str],
        *,
        dedupe_key: str | None = None,
        client_name: str | None = None,
    ) -> dict[str, Any]:
        return {"telegram_message_ids": [9001]}

    monkeypatch.setattr(
        transport_client_mod, "send_outbound_batch", _fake_send_batch,
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(data_dir / "corpus.jsonl")
    cfg.state.path = str(data_dir / "daily_sync_state.json")

    today = date_cls(2026, 7, 19)
    result = await fire_once(cfg, tmp_path / "vault", user_id=42, today=today)
    assert result["ok"] is True
    assert result["body"]

    spooled = read_latest(str(data_dir), "daily_sync")
    assert spooled is not None
    assert spooled["date"] == "2026-07-19"
    assert spooled["markdown"] == result["body"]  # byte-identical

    resp = await outbound_client.get(
        "/web/outbound/daily_sync/latest", headers=_session_headers(),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["kind"] == "daily_sync"
    assert body["date"] == "2026-07-19"
    assert body["markdown"] == result["body"]  # byte-identical over the wire


# ---------------------------------------------------------------------------
# REGRESSION SWALLOW: spool failure never breaks the producers
# ---------------------------------------------------------------------------


async def test_brief_spool_failure_swallowed_vault_and_push_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOAD-BEARING: write_latest raising (unwritable spool path) inside
    generate_brief is swallowed + logged; the brief's vault write AND the
    Telegram push still succeed."""
    from alfred.brief import daemon as brief_daemon_mod
    from alfred.brief.state import StateManager as BriefStateManager
    import alfred.transport.client as transport_client_mod

    async def _fake_weather(config):  # type: ignore[no-untyped-def]
        # COLLECTING form: (markdown, parsed TAFs) — generate_brief takes
        # this one now so the feed reuses its fetch.
        return "Sunny, 21C.", []

    monkeypatch.setattr(brief_daemon_mod, "fetch_and_format_collect", _fake_weather)

    # The NARRATION path reaches weather by a function-local import of
    # alfred.brief.weather.fetch_metars, which the daemon patch above cannot
    # cover — without this, generate_brief makes a live aviationweather.gov
    # call. Rationale: tests/feed/test_brief_feed_parity.py::_patch_weather.
    async def _no_metars(_wc):  # type: ignore[no-untyped-def]
        return []

    monkeypatch.setattr("alfred.brief.weather.fetch_metars", _no_metars)

    pushed: list[dict[str, Any]] = []

    async def _fake_send_batch(
        user_id: int,
        chunks: list[str],
        *,
        dedupe_key: str | None = None,
        client_name: str | None = None,
    ) -> dict[str, Any]:
        pushed.append({"user_id": user_id, "dedupe_key": dedupe_key})
        return {"telegram_message_ids": [9001]}

    monkeypatch.setattr(
        transport_client_mod, "send_outbound_batch", _fake_send_batch,
    )

    config = _brief_config(tmp_path, primary_telegram_user_id=42)
    # Force write_latest to raise: plant a plain FILE where the spool
    # DIRECTORY must go → mkdir(parents=True, exist_ok=True) raises.
    data_dir = Path(config.state.path).parent
    (data_dir / "web_outbound").write_text("not a directory", encoding="utf-8")

    state_mgr = BriefStateManager(config.state.path)
    with structlog.testing.capture_logs() as captured:
        rel_path = await brief_daemon_mod.generate_brief(config, state_mgr)

    # The run SURVIVED and the vault artifact exists.
    assert rel_path is not None
    assert (Path(config.vault_path) / rel_path).exists()
    # The Telegram push still fired.
    assert len(pushed) == 1
    assert pushed[0]["user_id"] == 42
    # The swallow is LOGGED (never silent). Two best-effort spools now ride the
    # shared helper (the #30 brief spool + the C3a narration spool); both fail on
    # the planted broken dir and both swallow. This test targets the BRIEF spool
    # — filter to kind="brief" (the narration spool has its own C3a coverage).
    fails = [
        c for c in captured
        if c.get("event") == "brief.web_outbound_write_failed"
        and c.get("kind") == "brief"
    ]
    assert len(fails) == 1
    # And the success line did NOT fire.
    assert not any(
        c.get("event") == "brief.web_outbound_written" for c in captured
    )


async def test_daily_sync_spool_failure_swallowed_fire_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric swallow: a daily_sync spool failure never breaks the fire
    (push + state save still run)."""
    from datetime import date as date_cls

    import alfred.transport.client as transport_client_mod
    from alfred.daily_sync.config import DailySyncConfig
    from alfred.daily_sync.confidence import load_state
    from alfred.daily_sync.daemon import fire_once

    pushed: list[int] = []

    async def _fake_send_batch(
        user_id: int,
        chunks: list[str],
        *,
        dedupe_key: str | None = None,
        client_name: str | None = None,
    ) -> dict[str, Any]:
        pushed.append(user_id)
        return {"telegram_message_ids": [9001]}

    monkeypatch.setattr(
        transport_client_mod, "send_outbound_batch", _fake_send_batch,
    )

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(data_dir / "corpus.jsonl")
    cfg.state.path = str(data_dir / "daily_sync_state.json")
    (data_dir / "web_outbound").write_text("not a directory", encoding="utf-8")

    today = date_cls(2026, 7, 19)
    with structlog.testing.capture_logs() as captured:
        result = await fire_once(
            cfg, tmp_path / "vault", user_id=42, today=today,
        )

    assert result["ok"] is True
    assert pushed == [42]
    state = load_state(cfg.state.path)
    assert state.get("last_fired_date") == "2026-07-19"
    fails = [
        c for c in captured
        if c.get("event") == "daily_sync.web_outbound_write_failed"
    ]
    assert len(fails) == 1
