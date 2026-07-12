"""Tests for the sovereign no-egress boundary spine (scribe P1-a).

SECURITY-CRITICAL. These pins prove the fail-closed no-egress guarantee:
a bug in the boundary means PHI leaks to a cloud provider, so every barrier
gets a positive (holds) AND negative (breach) pin, plus the unconditional
regression pin (any cloud key present => refuse) and the .env-reintroduction
ordering pin.

Fixture credential values are OBVIOUSLY FAKE (DUMMY_*), never realistic
provider prefixes — scanner-hygiene per builder discipline.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import structlog

from alfred.sovereign import (
    CLOUD_KEY_ENV_VARS,
    EGRESS_CONFIG_SECTIONS,
    SOVEREIGN_ALLOWED_SECTIONS,
    SOVEREIGN_STT_ALLOWLIST,
    SovereignBoundaryError,
    host_is_loopback,
    install_sovereign_http_guard,
    is_aiohttp_guard_installed,
    is_sovereign_http_guard_installed,
    uninstall_sovereign_http_guard,
    validate_sovereign_boundary,
)
from alfred.sovereign.http_guard import _assert_aiohttp_loopback, _assert_request_loopback
from alfred import orchestrator


# --- helpers ----------------------------------------------------------------

def _sovereign_raw(**scribe_overrides):
    """A minimal config that PASSES all four barriers (clean baseline)."""
    scribe = {
        "mode": "synthetic",
        "stt": {"provider": "faster-whisper"},
        "llm": {"base_url": "http://127.0.0.1:11434"},
    }
    scribe.update(scribe_overrides)
    return {
        "sovereign": {"enabled": True},
        "scribe": scribe,
    }


_CLEAN_ENV: dict[str, str] = {}


# --- happy path + gating ----------------------------------------------------

def test_all_barriers_hold_passes():
    validate_sovereign_boundary(_sovereign_raw(), env=_CLEAN_ENV)  # no raise


def test_sovereign_ok_signal_emitted():
    with structlog.testing.capture_logs() as caps:
        validate_sovereign_boundary(_sovereign_raw(), env=_CLEAN_ENV)
    ok = [c for c in caps if c.get("event") == "sovereign_ok"]
    assert len(ok) == 1
    assert ok[0]["stt_provider"] == "faster-whisper"
    assert ok[0]["llm_host"] == "127.0.0.1"
    assert ok[0]["egress_clear"] is True


def test_not_sovereign_is_noop_even_with_cloud_key():
    # No sovereign block => boundary not enforced, even with a cloud key
    # present. Salem/KAL-LE must never be blocked.
    raw = {"scribe": {"stt": {"provider": "groq"}}, "transport": {}}
    validate_sovereign_boundary(
        raw, env={"ANTHROPIC_API_KEY": "DUMMY_ANTHROPIC_TEST_KEY"}
    )  # no raise


def test_sovereign_enabled_false_is_noop():
    raw = _sovereign_raw()
    raw["sovereign"] = {"enabled": False}
    validate_sovereign_boundary(
        raw, env={"GROQ_API_KEY": "DUMMY_GROQ_TEST_KEY"}
    )  # no raise


# --- barrier (a) STT allowlist ----------------------------------------------

@pytest.mark.parametrize("provider", sorted(SOVEREIGN_STT_ALLOWLIST))
def test_barrier_a_local_providers_pass(provider):
    raw = _sovereign_raw(stt={"provider": provider})
    validate_sovereign_boundary(raw, env=_CLEAN_ENV)  # no raise


@pytest.mark.parametrize("provider", ["groq", "deepgram", "elevenlabs", "openai", ""])
def test_barrier_a_cloud_or_missing_provider_refused(provider):
    raw = _sovereign_raw(stt={"provider": provider})
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(raw, env=_CLEAN_ENV)
    assert exc.value.reason == "barrier_a"


def test_barrier_a_missing_stt_block_refused():
    raw = _sovereign_raw()
    raw["scribe"].pop("stt")
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(raw, env=_CLEAN_ENV)
    assert exc.value.reason == "barrier_a"


# --- barrier (b) LLM loopback -----------------------------------------------

@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:11434",
        "http://localhost:11434",
        "http://[::1]:11434",
    ],
)
def test_barrier_b_loopback_passes(base_url):
    raw = _sovereign_raw(llm={"base_url": base_url})
    validate_sovereign_boundary(raw, env=_CLEAN_ENV)  # no raise


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.openai.com/v1",
        "http://8.8.8.8:11434",
        "https://openrouter.ai/api/v1",
        "http://model.invalid:11434",  # unresolvable => fail-closed
        "",                             # unset => fail-closed
    ],
)
def test_barrier_b_non_loopback_or_missing_refused(base_url):
    raw = _sovereign_raw(llm={"base_url": base_url})
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(raw, env=_CLEAN_ENV)
    assert exc.value.reason == "barrier_b"


# --- barrier (c) no cloud key — THE UNCONDITIONAL REGRESSION PIN -------------

@pytest.mark.parametrize("key", CLOUD_KEY_ENV_VARS)
def test_barrier_c_any_cloud_key_in_env_refuses(key):
    """Regression pin: EVERY cloud key, when present in the process env,
    breaches the boundary at load. Unconditional (no importorskip)."""
    raw = _sovereign_raw()
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(raw, env={key: "DUMMY_CLOUD_TEST_VALUE"})
    assert exc.value.reason == "barrier_c"


def test_barrier_c_empty_string_cloud_key_is_not_present():
    # An explicitly-empty key is treated as absent (operator emptied it) —
    # only a NON-empty value breaches.
    raw = _sovereign_raw()
    validate_sovereign_boundary(raw, env={"ANTHROPIC_API_KEY": ""})  # no raise


@pytest.mark.parametrize("key", CLOUD_KEY_ENV_VARS)
def test_barrier_c_cloud_key_placeholder_in_config_refuses(key):
    raw = _sovereign_raw()
    raw["scribe"]["llm"]["api_key"] = "${" + key + "}"
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(raw, env=_CLEAN_ENV)
    assert exc.value.reason == "barrier_c"


def test_barrier_c_distinct_var_name_does_not_false_match():
    # ${ANTHROPIC_API_KEY_DISTILLER_REBUILD} is a DIFFERENT var (not in the
    # frozen set) — an exact-name placeholder scan must not false-match it.
    raw = _sovereign_raw()
    raw["scribe"]["llm"]["note"] = "${ANTHROPIC_API_KEY_DISTILLER_REBUILD}"
    validate_sovereign_boundary(raw, env=_CLEAN_ENV)  # no raise


def test_barrier_c_refused_signal_emitted():
    raw = _sovereign_raw()
    with structlog.testing.capture_logs() as caps:
        with pytest.raises(SovereignBoundaryError):
            validate_sovereign_boundary(
                raw, env={"GROQ_API_KEY": "DUMMY_GROQ_TEST_KEY"}
            )
    refused = [c for c in caps if c.get("event") == "sovereign_boundary_refused"]
    assert len(refused) == 1
    assert refused[0]["reason"] == "barrier_c"


# --- barrier (c) .env-REINTRODUCTION ORDERING PIN ---------------------------

def test_barrier_c_env_reintroduction_after_dotenv(tmp_path, monkeypatch):
    """Proves the boundary runs AFTER the config-sibling .env auto-load:
    the launch wrapper scrubs the shell env (env -u), but a .env carrying a
    cloud key re-introduces it into os.environ (auto_load_dotenv gap-fill).
    Barrier (c), reading the LIVE os.environ, must still refuse."""
    from alfred._env import auto_load_dotenv

    # Simulate `env -u ANTHROPIC_API_KEY ...` — the shell env is scrubbed.
    for key in CLOUD_KEY_ENV_VARS:
        monkeypatch.delenv(key, raising=False)

    # A config-sibling .env that (mistakenly) still carries a cloud key.
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=DUMMY_ANTHROPIC_TEST_KEY\n")

    # This is what orchestrator.py:1359 does, BEFORE the boundary gate.
    loaded, _ = auto_load_dotenv(env_file, override=False)
    assert loaded == 1  # the key is now back in os.environ

    raw = _sovereign_raw()
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(raw)  # env=None => reads live os.environ
    assert exc.value.reason == "barrier_c"


# --- barrier (d) no egress wired --------------------------------------------

@pytest.mark.parametrize("section", EGRESS_CONFIG_SECTIONS)
def test_barrier_d_egress_section_refused(section):
    raw = _sovereign_raw()
    raw[section] = {"enabled": True}
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(raw, env=_CLEAN_ENV)
    assert exc.value.reason == "barrier_d"


def test_barrier_d_telegram_is_egress():
    # A cloud Telegram bot is definitionally non-sovereign — pinned so a
    # future sovereign-talker carve-out is a deliberate diff.
    assert "telegram" in EGRESS_CONFIG_SECTIONS


def test_barrier_d_agent_block_refused():
    # P1-a review BLOCK-1 headline pin: an ``agent:`` block (the claude-p
    # backend selector) breaches. Stripping the API key REROUTES claude -p to
    # cached OAuth creds (still reaches api.anthropic.com) — barrier (c) does
    # NOT catch it; barrier (d) must.
    raw = _sovereign_raw()
    raw["agent"] = {"backend": "claude"}
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(raw, env=_CLEAN_ENV)
    assert exc.value.reason == "barrier_d"


@pytest.mark.parametrize("tool", ["curator", "janitor", "distiller", "instructor"])
def test_barrier_d_agent_backed_tool_refused(tool):
    # The real hole denying ``agent`` alone would leave open: these tools
    # auto-start on their OWN block presence and default to backend=claude
    # WITHOUT an ``agent:`` block — so each must be denied at barrier (d).
    raw = _sovereign_raw()
    raw[tool] = {"schedule": {}}  # no ``agent`` block — defaults to claude
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(raw, env=_CLEAN_ENV)
    assert exc.value.reason == "barrier_d"


@pytest.mark.parametrize("section", ["web", "gcal", "integrations"])
def test_barrier_d_non_httpx_transport_refused(section):
    # P1-a review BLOCK-2 / WARN-3: aiohttp (web STT/TTS) + googleapiclient
    # (gcal) escape the httpx guard, so they are fail-closed at load.
    raw = _sovereign_raw()
    raw[section] = {"enabled": True}
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(raw, env=_CLEAN_ENV)
    assert exc.value.reason == "barrier_d"


def test_barrier_c_resend_key_refused():
    # P1-a review WARN-4: RESEND_API_KEY (web/email.py → api.resend.com) is a
    # cloud egress a PHI email body can ride. Present in env => barrier_c.
    assert "RESEND_API_KEY" in CLOUD_KEY_ENV_VARS
    raw = _sovereign_raw()
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(
            raw, env={"RESEND_API_KEY": "DUMMY_RESEND_TEST_KEY"}
        )
    assert exc.value.reason == "barrier_c"


# --- barrier (d) is an ALLOWLIST (P1-a review r2, BLOCK-A) -------------------

def test_barrier_d_allowlist_only_config_passes():
    # A realistic minimal sovereign config with EVERY allowlisted section
    # (filesystem + identity + the workload) passes all four barriers.
    raw = {
        "_config_path": "/data/algernon/vera-clinical/config.vera-clinical.yaml",
        "sovereign": {"enabled": True},
        "scribe": {
            "mode": "synthetic",
            "stt": {"provider": "faster-whisper"},
            "llm": {"base_url": "http://127.0.0.1:11434"},
        },
        "vault": {"path": "/data/algernon/vera-clinical/vault"},
        "logging": {"level": "INFO", "dir": "/data/algernon/vera-clinical/data"},
        "daemon": {"pid_path": "/data/algernon/vera-clinical/data/alfred.pid"},
        "instance": {"name": "VERA-clinical", "canonical": "V.E.R.A.-clinical"},
    }
    validate_sovereign_boundary(raw, env=_CLEAN_ENV)  # no raise


def test_barrier_d_config_path_synthetic_key_allowed():
    # ``_config_path`` is stamped onto EVERY raw config by
    # _load_unified_config (cli.py) — the real run_all shape. It must be
    # allowlisted or every sovereign config would refuse at load.
    assert "_config_path" in SOVEREIGN_ALLOWED_SECTIONS
    raw = _sovereign_raw()
    raw["_config_path"] = "/data/algernon/vera-clinical/config.vera-clinical.yaml"
    validate_sovereign_boundary(raw, env=_CLEAN_ENV)  # no raise


@pytest.mark.parametrize(
    "section",
    # surveyor/brief/cloudflared = the denylist misses that motivated the
    # allowlist; the last two = arbitrary FUTURE daemons nobody has vetted.
    ["surveyor", "brief", "cloudflared", "some_future_tool", "totally_new_daemon"],
)
def test_barrier_d_non_allowlisted_section_refused(section):
    # Mutation-verify: ANY top-level section not in the allowlist refuses —
    # fail-closed BY DEFAULT, including tools that don't exist yet.
    raw = _sovereign_raw()
    raw[section] = {"enabled": True}
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(raw, env=_CLEAN_ENV)
    assert exc.value.reason == "barrier_d"


def test_egress_catalog_disjoint_from_allowlist():
    # The documented known-egress catalog and the enforcement allowlist must
    # never intersect — an egress section slipping into the allowlist would
    # silently reopen the boundary.
    assert not (set(EGRESS_CONFIG_SECTIONS) & SOVEREIGN_ALLOWED_SECTIONS)


def test_review_misses_are_catalogued():
    # surveyor (OpenRouter) / brief (weather API) / cloudflared (tunnel) —
    # the denylist misses — are pinned in the catalog + denied by the allowlist.
    for section in ("surveyor", "brief", "cloudflared"):
        assert section in EGRESS_CONFIG_SECTIONS
        assert section not in SOVEREIGN_ALLOWED_SECTIONS


# --- host_is_loopback helper ------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", "LOCALHOST"])
def test_host_is_loopback_true(host):
    assert host_is_loopback(host) is True


@pytest.mark.parametrize("host", ["8.8.8.8", "api.openai.com", "model.invalid", ""])
def test_host_is_loopback_false(host):
    assert host_is_loopback(host) is False


# --- orchestrator exit-79 no-restart contract -------------------------------

def test_sovereign_breach_exit_code_is_79():
    assert orchestrator._SOVEREIGN_BREACH_EXIT == 79
    assert orchestrator._SOVEREIGN_BREACH_EXIT != orchestrator._MISSING_DEPS_EXIT


@pytest.mark.parametrize("code,expected", [(78, True), (79, True), (0, False), (1, False), (None, False)])
def test_is_no_restart_exit(code, expected):
    assert orchestrator._is_no_restart_exit(code) is expected


# --- SovereignHttpGuard -----------------------------------------------------

@pytest.fixture
def guard_cleanup():
    yield
    uninstall_sovereign_http_guard()


def test_http_guard_install_idempotent_and_reversible(guard_cleanup):
    assert is_sovereign_http_guard_installed() is False
    install_sovereign_http_guard()
    assert is_sovereign_http_guard_installed() is True
    wrapped = httpx.Client.send
    install_sovereign_http_guard()  # second call is a no-op, no double-wrap
    assert httpx.Client.send is wrapped
    uninstall_sovereign_http_guard()
    assert is_sovereign_http_guard_installed() is False


def test_http_guard_refuses_non_loopback_request(guard_cleanup):
    install_sovereign_http_guard()
    # Literal public IP => guard fires BEFORE any connect (offline, no DNS).
    with pytest.raises(SovereignBoundaryError) as exc:
        httpx.Client(timeout=1.0).get("http://8.8.8.8/")
    assert exc.value.reason == "http_guard"


def test_assert_request_loopback_permits_loopback():
    req = httpx.Request("GET", "http://127.0.0.1:11434/v1/chat")
    _assert_request_loopback(req)  # no raise


def test_assert_request_loopback_refuses_cloud():
    req = httpx.Request("GET", "https://api.openai.com/v1/chat")
    with pytest.raises(SovereignBoundaryError) as exc:
        _assert_request_loopback(req)
    assert exc.value.reason == "http_guard"


# --- SovereignHttpGuard: requests coverage (audit — huggingface_hub transport) --

def test_requests_guard_blocks_non_loopback(guard_cleanup):
    # Audit FIX 2: the guard must cover requests.Session.send (huggingface_hub's
    # transport, live in the scribe process via faster-whisper). A cloud requests
    # call → SovereignBoundaryError BEFORE any connect. MUTATION-BIND: remove the
    # requests wrap → this egress is NOT blocked → RED.
    import requests
    from alfred.sovereign import is_requests_guard_installed
    install_sovereign_http_guard()
    assert is_requests_guard_installed() is True
    with pytest.raises(SovereignBoundaryError) as exc:
        requests.get("https://api.deepgram.com/v1/listen", timeout=1.0)
    assert exc.value.reason == "http_guard"


def test_requests_guard_allows_loopback(guard_cleanup):
    # A loopback requests call PASSES the guard (fails at the transport with a
    # connection error since nothing is listening — NOT a SovereignBoundaryError).
    import requests
    install_sovereign_http_guard()
    with pytest.raises(requests.exceptions.RequestException):
        requests.get("http://127.0.0.1:1/x", timeout=1.0)


def test_requests_guard_reversible(guard_cleanup):
    import requests
    from alfred.sovereign import is_requests_guard_installed
    orig = requests.Session.send
    assert is_requests_guard_installed() is False
    install_sovereign_http_guard()
    assert is_requests_guard_installed() is True and requests.Session.send is not orig
    uninstall_sovereign_http_guard()
    assert is_requests_guard_installed() is False and requests.Session.send is orig


def test_requests_guard_noop_when_absent(guard_cleanup, monkeypatch):
    # requests absent in a stripped venv → install cleanly (no crash), requests=False.
    import alfred.sovereign.http_guard as hg
    from alfred.sovereign import is_requests_guard_installed
    monkeypatch.setattr(hg, "_try_import_requests", lambda: None)
    with structlog.testing.capture_logs() as caps:
        install_sovereign_http_guard()
    assert is_requests_guard_installed() is False           # no-op'd, no crash
    ev = [c for c in caps if c.get("event") == "sovereign.http_guard.installed"]
    assert ev and ev[0]["requests"] is False


# --- SovereignHttpGuard: aiohttp coverage (#40, PWA scribe prereq) ----------

class _FakeSession:
    """Mimics aiohttp.ClientSession's ``_build_url`` (base_url + relative join)
    for a loop-free unit test of the aiohttp loopback assert."""

    def __init__(self, base=None):
        import yarl
        self._base_url = yarl.URL(base) if base else None

    def _build_url(self, str_or_url):
        import yarl
        url = yarl.URL(str_or_url)
        if self._base_url and not url.absolute:
            return self._base_url.join(url)
        return url


def test_assert_aiohttp_loopback_permits_loopback():
    import yarl
    s = _FakeSession()
    _assert_aiohttp_loopback(s, "http://127.0.0.1:11434/v1/chat")   # str, no raise
    _assert_aiohttp_loopback(s, yarl.URL("http://localhost:8000/x"))  # yarl.URL, no raise
    _assert_aiohttp_loopback(s, "wss://[::1]:9000/live")            # ipv6 loopback ws


def test_assert_aiohttp_loopback_refuses_cloud():
    s = _FakeSession()
    with pytest.raises(SovereignBoundaryError) as exc:
        _assert_aiohttp_loopback(s, "wss://api.deepgram.com/v1/listen")
    assert exc.value.reason == "http_guard"


def test_assert_aiohttp_loopback_resolves_base_url_relative():
    # base_url + relative path — a LOOPBACK base is not false-blocked; a CLOUD
    # base IS blocked (resolved exactly as aiohttp resolves it).
    _assert_aiohttp_loopback(_FakeSession("http://127.0.0.1:11434"), "/v1/chat")  # no raise
    with pytest.raises(SovereignBoundaryError):
        _assert_aiohttp_loopback(_FakeSession("https://api.elevenlabs.io"), "/v1/tts")


def test_aiohttp_guard_installed_and_reversible(guard_cleanup):
    import aiohttp
    orig_request = aiohttp.ClientSession._request
    orig_init = aiohttp.ClientSession.__init__
    assert is_aiohttp_guard_installed() is False
    install_sovereign_http_guard()
    assert is_aiohttp_guard_installed() is True
    assert aiohttp.ClientSession._request is not orig_request    # request wrapped (the SINGLE seam)
    # SINGLE-SEAM design (D1): the redirect guard rides the retroactive _request
    # wrap via a lazily-injected TraceConfig — __init__ is NOT patched (an
    # __init__-only injection had a pre-install-session blind spot).
    assert aiohttp.ClientSession.__init__ is orig_init           # __init__ untouched
    wrapped = aiohttp.ClientSession._request
    install_sovereign_http_guard()                              # idempotent, no double-wrap
    assert aiohttp.ClientSession._request is wrapped
    uninstall_sovereign_http_guard()
    assert is_aiohttp_guard_installed() is False
    assert aiohttp.ClientSession._request is orig_request        # request restored
    assert aiohttp.ClientSession.__init__ is orig_init           # __init__ still untouched


_LOOPBACK_DIAL = frozenset({"127.0.0.1", "localhost", "::1"})


def _install_connect_tripwire(monkeypatch):
    """Patch aiohttp.TCPConnector.connect to RECORD every dialed host and REFUSE
    any non-loopback dial (so a silently-regressed guard can NEVER really egress,
    even in a future CI GREEN). Returns the ``dialed`` host list (the socket
    tripwire — N2 belt)."""
    import aiohttp
    dialed: list[str] = []
    orig = aiohttp.TCPConnector.connect

    async def _tw(self, req, traces, timeout):
        dialed.append(req.url.host)
        if req.url.host not in _LOOPBACK_DIAL:
            raise RuntimeError(f"socket tripwire: refused cloud dial to {req.url.host}")
        return await orig(self, req, traces, timeout)

    monkeypatch.setattr(aiohttp.TCPConnector, "connect", _tw)
    return dialed


def test_aiohttp_guard_blocks_non_loopback_request(guard_cleanup, monkeypatch):
    # THE mutation-bind: remove the aiohttp wrap → this non-loopback aiohttp
    # request is NOT blocked (proceeds to connect) → RED.
    import aiohttp
    dialed = _install_connect_tripwire(monkeypatch)   # N2 belt: 0-socket proof
    install_sovereign_http_guard()

    async def _go():
        async with aiohttp.ClientSession() as s:
            await s.get("https://api.deepgram.com/v1/listen")

    with pytest.raises(SovereignBoundaryError) as exc:
        asyncio.run(_go())
    assert exc.value.reason == "http_guard"
    assert "api.deepgram.com" not in dialed           # ZERO sockets to the cloud host


def test_aiohttp_guard_blocks_non_loopback_websocket(guard_cleanup, monkeypatch):
    # The web STT/TTS surfaces use ws_connect (deepgram/elevenlabs); the WS
    # handshake flows through _request → the guard must block it too.
    import aiohttp
    dialed = _install_connect_tripwire(monkeypatch)   # N2 belt: 0-socket proof
    install_sovereign_http_guard()

    async def _go():
        async with aiohttp.ClientSession() as s:
            await s.ws_connect("wss://api.deepgram.com/v1/listen")

    with pytest.raises(SovereignBoundaryError) as exc:
        asyncio.run(_go())
    assert exc.value.reason == "http_guard"
    assert "api.deepgram.com" not in dialed           # ZERO sockets to the cloud host


def test_aiohttp_guard_allows_loopback_request(guard_cleanup):
    # A loopback aiohttp request PASSES the guard (fails at the transport with a
    # connection error since nothing is listening — NOT a SovereignBoundaryError).
    import aiohttp
    install_sovereign_http_guard()

    async def _go():
        async with aiohttp.ClientSession() as s:
            await s.get("http://127.0.0.1:1/x")

    with pytest.raises(aiohttp.ClientError):     # transport error, guard passed
        asyncio.run(_go())


def test_aiohttp_guard_noop_when_aiohttp_absent(guard_cleanup, monkeypatch):
    # aiohttp not installed in this venv → install cleanly (httpx-only), no crash.
    import alfred.sovereign.http_guard as hg
    monkeypatch.setattr(hg, "_try_import_aiohttp", lambda: None)
    with structlog.testing.capture_logs() as caps:
        install_sovereign_http_guard()
    assert is_sovereign_http_guard_installed() is True     # httpx still wrapped
    assert is_aiohttp_guard_installed() is False           # aiohttp no-op'd, no crash
    ev = [c for c in caps if c.get("event") == "sovereign.http_guard.installed"]
    assert len(ev) == 1 and ev[0]["httpx"] is True and ev[0]["aiohttp"] is False


def test_aiohttp_guard_covers_on_reinstall_after_available(guard_cleanup, monkeypatch):
    # The "aiohttp installed later (web mount)" case: a fresh install AFTER
    # aiohttp becomes importable must then cover it.
    import aiohttp
    import alfred.sovereign.http_guard as hg
    monkeypatch.setattr(hg, "_try_import_aiohttp", lambda: None)
    install_sovereign_http_guard()                 # httpx-only (aiohttp "absent")
    assert is_aiohttp_guard_installed() is False
    monkeypatch.setattr(hg, "_try_import_aiohttp", lambda: aiohttp)  # now "available"
    install_sovereign_http_guard()                 # re-install → covers aiohttp
    assert is_aiohttp_guard_installed() is True


def test_http_guard_install_logs_coverage(guard_cleanup):
    # Observability pin (#9): install surfaces which transports are guarded.
    with structlog.testing.capture_logs() as caps:
        install_sovereign_http_guard()
    ev = [c for c in caps if c.get("event") == "sovereign.http_guard.installed"]
    assert len(ev) == 1
    assert ev[0]["httpx"] is True and ev[0]["aiohttp"] is True and ev[0]["requests"] is True


# --- W1: redirect targets re-asserted per-hop (hermetic, socket-tripwire) ----

async def _serve_redirect_and_get(status, location):
    """Run a loopback server that returns ``status`` with ``Location: location``,
    then GET it under the installed guard. Returns (resp_status, body) if the
    request completes, else the guard/transport exception propagates."""
    import aiohttp
    from aiohttp import web

    app = web.Application()

    async def _redirect(request):
        return web.Response(status=status, headers={"Location": location})

    async def _final(request):
        return web.Response(text="OK")

    app.router.add_route("*", "/enc", _redirect)
    app.router.add_route("*", "/final", _final)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = list(runner.addresses)[0][1]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"http://127.0.0.1:{port}/enc") as resp:
                return resp.status, await resp.text()
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("status", [301, 302, 307])  # 307 preserves body = the PHI case
def test_aiohttp_guard_blocks_redirect_to_cloud(status, guard_cleanup, monkeypatch):
    # W1: a LOOPBACK url that 3xx-redirects to a CLOUD host must be REFUSED, with
    # ZERO sockets to the cloud host. MUTATION-BIND: remove the redirect re-assert
    # (the lazy TraceConfig injection in _ensure_aiohttp_redirect_guard) → aiohttp
    # follows the redirect → the tripwire records the cloud dial + raises
    # RuntimeError (not SovereignBoundaryError) → RED.
    dialed = _install_connect_tripwire(monkeypatch)
    install_sovereign_http_guard()

    with pytest.raises(SovereignBoundaryError) as exc:
        asyncio.run(_serve_redirect_and_get(
            status, "https://api.deepgram.com/leak"))
    assert exc.value.reason == "http_guard"
    assert "api.deepgram.com" not in dialed          # 0 sockets to the cloud host
    assert "127.0.0.1" in dialed                      # the loopback origin WAS dialed


def test_aiohttp_guard_follows_loopback_to_loopback_redirect(guard_cleanup, monkeypatch):
    # Per-hop re-assert is TRANSPARENT: a loopback→loopback redirect still
    # auto-follows (relative Location resolves to the loopback origin).
    dialed = _install_connect_tripwire(monkeypatch)
    install_sovereign_http_guard()
    status, body = asyncio.run(_serve_redirect_and_get(307, "/final"))
    assert status == 200 and body == "OK"             # followed the loopback redirect
    assert all(h in _LOOPBACK_DIAL for h in dialed)   # every dial stayed loopback


def test_aiohttp_guard_blocks_redirect_on_pre_install_session(guard_cleanup, monkeypatch):
    # D1: the redirect re-assert must be RETROACTIVE. A ClientSession constructed
    # BEFORE the guard installs (the reviewer's repro) must STILL have its redirects
    # blocked. The old __init__-only injection missed this (the session was built
    # before __init__ was patched, so it carried no guard trace); the lazy inject on
    # the retroactive _request wrap covers it. MUTATION-BIND: drop the lazy inject
    # (or gate it behind __init__) → the pre-install session follows the redirect →
    # tripwire records the cloud dial + raises RuntimeError (not
    # SovereignBoundaryError) → RED.
    import aiohttp
    from aiohttp import web
    dialed = _install_connect_tripwire(monkeypatch)

    async def _go():
        app = web.Application()

        async def _redirect(request):
            return web.Response(status=307, headers={"Location": "https://api.deepgram.com/leak"})

        app.router.add_route("*", "/enc", _redirect)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = list(runner.addresses)[0][1]
        # THE pre-install repro: build the session while the guard is NOT installed.
        session = aiohttp.ClientSession()
        assert is_aiohttp_guard_installed() is False
        install_sovereign_http_guard()          # install AFTER the session exists
        try:
            async with session:
                async with session.get(f"http://127.0.0.1:{port}/enc") as resp:
                    await resp.text()
        finally:
            await runner.cleanup()

    with pytest.raises(SovereignBoundaryError) as exc:
        asyncio.run(_go())
    assert exc.value.reason == "http_guard"
    assert "api.deepgram.com" not in dialed          # 0 sockets to the cloud host
    assert "127.0.0.1" in dialed                      # the loopback origin WAS dialed


def test_assert_aiohttp_redirect_loopback_blocks_cloud_location():
    # Unit test of the redirect callback: a cloud Location → SovereignBoundaryError;
    # a loopback (relative) Location resolved against a loopback origin → no raise.
    import asyncio as _asyncio
    import yarl
    from types import SimpleNamespace
    from alfred.sovereign.http_guard import _assert_aiohttp_redirect_loopback

    def _params(origin, location):
        resp = SimpleNamespace(headers={"Location": location})
        return SimpleNamespace(url=yarl.URL(origin), response=resp)

    # cloud absolute Location → blocked
    with pytest.raises(SovereignBoundaryError):
        _asyncio.run(_assert_aiohttp_redirect_loopback(
            None, None, _params("http://127.0.0.1:8000/enc", "https://api.deepgram.com/x")))
    # relative Location off a loopback origin → resolves loopback → no raise
    _asyncio.run(_assert_aiohttp_redirect_loopback(
        None, None, _params("http://127.0.0.1:8000/enc", "/final")))
