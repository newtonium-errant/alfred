"""Shared pytest fixtures for the Alfred test suite.

These fixtures are intentionally minimal — they exist to give tests a
working vault layout and a config dict that mirrors ``config.yaml.example``
without touching the real vault or the user's checked-in config.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import sys
import threading
from pathlib import Path
from textwrap import dedent

import pytest
import structlog
import structlog._config
import yaml


# ---------------------------------------------------------------------------
# structlog cache-bust — see feedback_structlog_assertion_patterns.md
# ---------------------------------------------------------------------------
#
# Root cause (diagnosed 2026-05-13): Alfred's per-tool ``setup_logging``
# helpers call ``structlog.configure(processors=[...])`` with a fresh list
# literal each invocation. ``structlog.configure`` does
# ``_CONFIG.default_processors = processors`` — REFERENCE REASSIGNMENT, not
# in-place mutation. When test A calls ``setup_logging`` (e.g. test_vault_
# cli_audit_log.py's ``cmd_vault`` dispatcher pin), then test B caches a
# module-level ``log = structlog.get_logger(__name__)`` BoundLogger via its
# first ``log.info(...)`` call (per ``cache_logger_on_first_use=True``),
# then test C runs another ``setup_logging`` (or just inherits the new
# config from B), the cached BoundLogger's ``procs`` field references the
# ORIGINAL list — orphaned from the current config.
#
# ``structlog.testing.capture_logs()`` mutates the CURRENT config's list
# in place (per structlog source: "always keep the list instance intact
# to not break references held by bound loggers"). But the orphaned list
# is no longer current — so the cached BoundLogger emits through the
# stale production processor chain, ``capture_logs`` returns empty, and
# the test fails despite the log line appearing in stdout / caplog.
#
# Failure signature: ``Captured stdout call`` shows the rendered log line,
# but ``structlog.testing.capture_logs()`` returns ``[]``. Order-dependent
# because the cache-orphaning only happens after the first ``setup_logging``
# is called in-process.
#
# This fixture runs before every test, walking ``sys.modules`` for any
# ``alfred.*`` module with a module-level ``log`` / ``logger`` /``_log``
# attribute that's a ``BoundLoggerLazyProxy``, and clearing the cached
# ``bind`` override. Next ``log.info(...)`` will re-resolve processors
# from the current ``_CONFIG.default_processors``, restoring coherence
# with ``capture_logs``.
#
# SECOND orphaning vector (diagnosed 2026-07-24) — stale monkeypatched
# log methods. A ``BoundLoggerLazyProxy`` resolves ``log.warning`` /
# ``log.info`` / etc. dynamically via ``__getattr__`` (they are NOT real
# instance attributes). Several suite tests spy on a daemon logger with
# ``monkeypatch.setattr(mod.log, "warning", _spy)`` (e.g.
# ``test_brief_dispatch.py``'s push-failure tests). Because ``hasattr(log,
# "warning")`` is True (via ``__getattr__``), monkeypatch snapshots the
# RESOLVED bound method as the "original" and, on teardown, RESTORES it by
# *setting* ``log.__dict__["warning"] = <that bound method>`` — leaving a
# REAL instance attribute that shadows ``__getattr__`` for the rest of the
# process. That bound method has its ``_processors`` frozen to the list
# that was current when the spy test ran; it never re-resolves, so
# ``capture_logs``'s in-place swap of the CURRENT config list can't reach
# it → the event renders through the frozen chain (caplog shows it) and
# ``capture_logs`` returns ``[]`` — identical failure signature to the
# cache-orphan case above.
#
# This was latent on master (the global processors list was stable across
# tests, so ``capture_logs`` mutated the very list the frozen method held).
# The ``_pin_structlog_off_stdout`` fixture below hands each test a FRESH
# processors list literal, which orphans the spy test's frozen list and
# surfaces the bug (``test_brief_watches`` / ``_weather`` / ``_process_hub``
# crash-guard asserts). So this fixture strips not just ``bind`` but every
# non-dunder public attribute a monkeypatch/cache could have left on the
# proxy (all of the proxy's own state is underscore-prefixed — see
# ``BoundLoggerLazyProxy.__init__`` — so a non-underscore key in
# ``__dict__`` is always an artifact: ``bind`` or a shadowed log method).
#
# Long-term fix (deferred — separate arc): the eight ``setup_logging``
# helpers in ``src/alfred/*/utils.py`` should maintain a module-level
# processor list and mutate it in place rather than passing a new list
# literal each call. That removes the underlying reference-reassignment
# trap and lets the fixture be removed. Per project_next_session.md.


@pytest.fixture(autouse=True)
def _bust_structlog_lazy_proxy_cache():
    """Autouse: reset every ``alfred.*`` module-level structlog proxy to a
    clean lazy state BEFORE each test runs.

    Strips the cached ``bind`` override (``cache_logger_on_first_use`` case)
    AND any stale monkeypatched log-method attribute (``warning`` / ``info``
    / etc.) that a prior test's ``monkeypatch.setattr(log, <method>, ...)``
    restored onto the proxy instance. Both shadow the proxy's dynamic
    ``__getattr__`` resolution and pin ``_processors`` to an orphaned list,
    breaking ``capture_logs``. Deleting them forces re-resolution against
    the CURRENT ``_CONFIG.default_processors`` on next use.

    Cheap — typical run sees ~50 modules under ``alfred.*`` with module-
    level loggers, attribute deletion is O(1). Test setup time impact
    measured at ~0.5ms per test.
    """
    _attr_names = ("log", "logger", "_log")
    for mod_name, mod in list(sys.modules.items()):
        if not mod_name.startswith("alfred"):
            continue
        for attr in _attr_names:
            candidate = getattr(mod, attr, None)
            if isinstance(candidate, structlog._config.BoundLoggerLazyProxy):
                # Every attribute the proxy legitimately owns is
                # underscore-prefixed (``_logger``, ``_processors``, …).
                # Any non-underscore key in ``__dict__`` is an artifact:
                # the ``bind`` cache override (from
                # ``cache_logger_on_first_use=True``) or a stale
                # monkeypatched log method (``warning``/``info``/…) that a
                # prior spy test left behind. Delete them so the proxy
                # re-resolves against the CURRENT config on next use.
                _artifacts = [k for k in candidate.__dict__ if not k.startswith("_")]
                for _key in _artifacts:
                    delattr(candidate, _key)
    yield


# ---------------------------------------------------------------------------
# structlog OFF-stdout baseline — closes the capsys-pollution flake class
# (see feedback_structlog_assertion_patterns.md)
# ---------------------------------------------------------------------------
#
# Root cause (diagnosed 2026-07-24): ``setup_logging`` (the eight
# ``*/utils.py`` helpers, default ``suppress_stdout=False``) does
# ``logging.basicConfig(handlers=[StreamHandler(sys.stdout)], force=True)`` +
# ``structlog.configure(logger_factory=stdlib.LoggerFactory())``. That leaves
# a root-logger ``StreamHandler`` bound to ``sys.stdout`` AND flips structlog
# off its unconfigured ``PrintLoggerFactory`` default — and NOTHING contains or
# restores that global state across tests. Two failure modes result, wanting
# OPPOSITE structlog states, so no single containment baseline serves both:
#
#   * POLLUTION (Class B — the ``json.loads(capsys.readouterr().out)`` tests,
#     e.g. ``routine/test_cli_items``): when structlog is in its PrintLogger
#     default (writes to the CURRENT ``sys.stdout`` each call), a stray
#     structlog event — including one from a background daemon thread spawned
#     by an orchestrator/mail/curator test that outlives its test — interleaves
#     a rendered log line into the JSON on stdout and breaks the parse.
#   * ABSENCE (Class A — tests asserting a structlog EVENT on ``capsys.out``,
#     e.g. ``test_backfill``): once a prior ``setup_logging`` moved structlog to
#     LoggerFactory, its events go to the logging module (caplog), not stdout,
#     so the ``capsys.out`` assertion sees nothing.
#
# This fixture pins a DETERMINISTIC OFF-stdout baseline before every test:
# structlog routes through stdlib logging (so ``caplog`` and
# ``structlog.testing.capture_logs`` both keep working), and no structlog line
# can ever reach ``capsys.out``. Class A must therefore assert via
# ``capture_logs`` (the ``test_backfill`` migration lands with this fixture).
#
# It does NOT touch pytest's own capture handler: only ``StreamHandler``s whose
# ``.stream`` IS the real stdout/stderr are stripped (pytest's LogCaptureHandler
# streams a StringIO; FileHandlers stream a file — both are left intact). A test
# that itself calls ``setup_logging`` mid-body still overrides this baseline for
# its own duration (this only sets the pre-test starting state).


@pytest.fixture(autouse=True)
def _pin_structlog_off_stdout():
    """Autouse: deterministic OFF-stdout structlog baseline before each test."""
    root = logging.getLogger()
    _live_stdio = (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__)
    for handler in list(root.handlers):
        # Strip ONLY handlers bound to the real stdout/stderr (a leaked
        # setup_logging StreamHandler). pytest's capture handler (StringIO
        # stream) and FileHandlers (file stream) are left untouched.
        if isinstance(handler, logging.StreamHandler) and getattr(handler, "stream", None) in _live_stdio:
            root.removeHandler(handler)
    # Pin structlog to stdlib LoggerFactory (NOT the PrintLogger→stdout default)
    # so events route through logging → pytest capture, never direct-to-stdout.
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    yield


# Top-level entity directories the vault ops layer expects to find. We don't
# need every type — just enough that ``vault_create`` / ``vault_search`` /
# ``vault_list`` have somewhere to put a record without blowing up on a
# missing parent.
_VAULT_DIRS = ("person", "task", "project", "note", "inbox")


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    """Return a temp directory laid out as a minimal Alfred vault.

    Includes:
      - empty subdirs for a handful of common record types
      - one sample person record so search/list queries have something to hit
    """
    vault = tmp_path / "vault"
    vault.mkdir()
    for sub in _VAULT_DIRS:
        (vault / sub).mkdir()

    sample_person = dedent(
        """\
        ---
        type: person
        name: Sample Person
        created: 2026-04-18
        tags: []
        related: []
        ---

        # Sample Person

        Fixture record used by the vault_ops smoke tests.
        """
    )
    (vault / "person" / "Sample Person.md").write_text(sample_person, encoding="utf-8")
    return vault


@pytest.fixture
def ephemeral_config(tmp_vault: Path) -> dict:
    """Load ``config.yaml.example`` and repoint ``vault.path`` at ``tmp_vault``.

    Returns the parsed dict — tests can mutate it freely; nothing is written
    back to disk.
    """
    repo_root = Path(__file__).resolve().parent.parent
    example = repo_root / "config.yaml.example"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    raw.setdefault("vault", {})["path"] = str(tmp_vault)
    return raw


# ---------------------------------------------------------------------------
# Suite egress guard (#16) — RECORD, then assert. Never raise mid-call.
# ---------------------------------------------------------------------------
#
# The suite must not talk to the internet. Historically it did, quietly: the
# narration weather fetch (#12), three health tests dialling api.anthropic.com,
# and four talker tests dialling api.telegram.org. Every one of them PASSED
# while leaking, which is the whole problem — a leak has no natural failure
# signal, so it needs a dedicated one.
#
# WHY RECORD RATHER THAN REFUSE. One measured reason, and it is sufficient:
#
#   A refuser produces NO SIGNAL. httpx/anyio resolve DNS on a
#   ThreadPoolExecutor worker, so a raise happens off-thread, is consumed by
#   the transport's own error handling, and never reaches the test. Measured:
#   under a refusing prototype the telegram leak raised in a worker thread and
#   the test still reported PASSED. reviewer-posture measured the same shape
#   independently during #12 — 7 tests attempted egress under its raise-only
#   probe and all 7 passed. A guard whose output is "green" while tests are
#   leaking is a false certificate, which is worse than no guard: it is the
#   #12 failure mode with a badge on it. (The #12 note about bare ``except``
#   blocks swallowing a raise is the same problem by a different route.)
#
#   Note what this argument does NOT claim: refusing usually does stop the
#   packet. The objection is to refusing ALONE — without a record there is
#   nothing to assert on, so the leak stays invisible.
#
# TWO EARLIER GROUNDS WERE RETRACTED after re-measurement, and the correction
# matters more than the original claim did. This comment previously argued that
# refusing (a) shadows the scribe sovereign-guard tests and (b) corrupts the
# scribe egress firewall. Both were artefacts of the PROTOTYPE, which had no
# ip-literal / TEST-NET exemptions — not properties of refusing:
#
#   * a refuser carrying the SAME exemptions as this guard leaves
#     tests/test_scribe_daemon_p1d.py at 15 passed, no failures. The protective
#     element is the exemption set, orthogonal to refuse-vs-record. The original
#     "1 failed" happened because the prototype intercepted the sovereign
#     guard's OWN getaddrinfo classification of the literal 8.8.8.8.
#   * the ``egress_firewall.unverified`` (reason=TimeoutError) and
#     ``egress_firewall.loopback_severed`` (reason=ConnectionRefusedError)
#     warnings appear IDENTICALLY under this record-only guard and under an
#     exempted refuser. They are environmental — ollama is not running on a dev
#     box, and TEST-NET-1 is non-routable by design. The harness never caused
#     them. Only the unexempted prototype changed anything, and only the
#     canary's reason string.
#
# Recorded here rather than quietly deleted because the mistake is instructive:
# both retracted grounds came from ONE observation of an unexempted prototype,
# generalised without isolating the variable. Re-running with the exemptions
# held constant retired both in about a minute.

_LOOPBACK_PREFIXES = ("127.", "::1", "localhost")

# RFC 5737 documentation ranges (TEST-NET-1/2/3). Reserved, non-routable, and
# guaranteed to reach no real service. The scribe egress firewall deliberately
# dials TEST-NET-1 (192.0.2.1:443) as its canary — that is an egress CONTROL
# mechanism, not a leak, so flagging it would be a false positive that pressures
# someone into weakening a real safety check.
_TEST_NET_PREFIXES = ("192.0.2.", "198.51.100.", "203.0.113.")

# WHAT FAILS A TEST: a CONNECT to a non-exempt address. What merely gets
# reported: a DNS resolution with no connection behind it.
#
# The split is not squeamishness, it is what the codebase does. alfred's own
# sovereign boundary — the thing that enforces "this process may not reach the
# cloud" — CLASSIFIES a destination by resolving it and checking whether the
# result is loopback. So ~18 tests of that machinery (test_sovereign_boundary,
# barrier-b/e refusals, host_is_loopback, the aiohttp redirect blockers) resolve
# names like api.openai.com precisely in order to refuse them. Failing them
# would mean the egress guard's first act was to make the real egress control
# untestable, and the pressure would be to weaken the safety check.
#
# A resolution alone also transmits no payload — it tells the configured
# resolver a name, nothing more. Every leak this suite has actually had
# (#12 weather, the anthropic probe, the telegram typing indicator) opened a
# CONNECTION, and connect is recorded BEFORE the syscall, so it still fires on
# an offline runner where the dial would fail. The residual gap is a leak whose
# DNS fails and therefore never reaches connect; the informational DNS report
# below exists so that case is visible rather than silent.
_IMPORT_PHASE = "<import-or-collection>"
_connect_by_test: dict[str, set[str]] = {}
_dns_by_test: dict[str, set[str]] = {}
_current_nodeid = _IMPORT_PHASE
_egress_lock = threading.Lock()


def _egress_exempt(host: object) -> bool:
    """True when ``host`` is loopback or a reserved documentation address.

    Bytes hosts are REAL and decoded first: telegram passes
    ``b'api.telegram.org'`` and the surveyor's ollama probe passes
    ``b'localhost'``. Comparing bytes against str prefixes silently returns
    False, which would classify ``b'localhost'`` as egress and fail a test for
    talking to a local model. Found by measurement, not review.

    An un-decodable / unknown type is treated as NOT exempt — a guard that
    cannot identify a destination should report it, not wave it through.
    """
    if isinstance(host, (bytes, bytearray)):
        try:
            host = host.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return False
    if not isinstance(host, str):
        return False
    if host in ("", "0.0.0.0", "::"):
        return True
    return host.startswith(_LOOPBACK_PREFIXES) or host.startswith(_TEST_NET_PREFIXES)


def _is_ip_literal(host: object) -> bool:
    """True when ``host`` is already a numeric address (no DNS involved)."""
    if isinstance(host, (bytes, bytearray)):
        try:
            host = host.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return False
    if not isinstance(host, str):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _record(bucket: dict[str, set[str]], host: object, port: object) -> None:
    with _egress_lock:
        bucket.setdefault(_current_nodeid, set()).add(f"{host!r}:{port}")


def _install_egress_recorder() -> None:
    """Wrap the three stdlib entry points every network client bottoms out in.

    Patched once at import, never reverted — an autouse fixture that installed
    and removed per test would miss import-time and late background-thread
    egress, which is exactly the traffic hardest to find by reading code.
    """
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _host_port(address):
        if isinstance(address, tuple) and address:
            return address[0], (address[1] if len(address) > 1 else "?")
        return address, "?"

    def getaddrinfo(host, port, *a, **kw):
        # Resolving an IP LITERAL is a local parse, not a DNS query — nothing
        # leaves the machine, so it is not egress. Recording it produced a
        # false positive of the worst kind: alfred.sovereign's own http_guard
        # calls getaddrinfo() on the target to classify it BEFORE refusing, so
        # the guard got flagged for doing its job and
        # test_guard_self_install_blocks_non_loopback_from_scribe_process
        # (which dials the literal 8.8.8.8 and asserts the refusal) errored in
        # teardown. Actual egress to a literal IP is still caught — by connect.
        if not _egress_exempt(host) and not _is_ip_literal(host):
            _record(_dns_by_test, host, port)
        return real_getaddrinfo(host, port, *a, **kw)

    def connect(self, address, *a, **kw):
        host, port = _host_port(address)
        if not _egress_exempt(host):
            _record(_connect_by_test, host, port)
        return real_connect(self, address, *a, **kw)

    def connect_ex(self, address, *a, **kw):
        host, port = _host_port(address)
        if not _egress_exempt(host):
            _record(_connect_by_test, host, port)
        return real_connect_ex(self, address, *a, **kw)

    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    return connect


# Kept so the guard can tell whether it is still the live patch — see
# ``_guard_is_live``. LAST PATCH WINS: a test that installs its own
# ``socket.socket.connect`` patch (tests/feed/test_brief_feed_parity.py does,
# for its own narrower egress pin) REPLACES this one for the duration of that
# test, and this guard sees nothing. That is not hypothetical — it is why
# reviewer-posture's #12 prototype missed the very pin that was attempting
# egress. The guard cannot prevent being shadowed, so it reports it: a test the
# guard could not observe is listed as UNVERIFIED rather than counted as clean.
_OUR_CONNECT_HOOK = _install_egress_recorder()

# nodeids where our patch was not the live one at teardown.
_unverified_tests: list[str] = []


def _guard_is_live() -> bool:
    return socket.socket.connect is _OUR_CONNECT_HOOK


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_network: test INTENTIONALLY reaches a real network service "
        "(key-gated integration runs). Exempt from the suite egress guard.",
    )


# ---------------------------------------------------------------------------
# Credential de-pollution — undo a third-party dotenv side effect (#16)
# ---------------------------------------------------------------------------
#
# ``pymilvus/settings.py`` runs a bare ``load_dotenv()`` at import. ``find_dotenv``
# walks up from the cwd and finds the repo's REAL .env (it locates the main
# repo's file even from inside a worktree, since worktrees nest under it), so
# merely importing alfred.surveyor.embedder injects every production credential
# in that file into os.environ — for the whole pytest process, at collection
# time, before a single test runs.
#
# That silently defeats the suite's own gates. Three integration tests guard on
# ``skipif(not os.environ.get("GROQ_API_KEY"))`` / ELEVENLABS, intending "only
# run when an operator deliberately supplies a key". With the .env injected they
# always run, reaching real Groq and real ElevenLabs with real credentials on
# every full-suite run — billable third-party traffic as a side effect of
# running tests. Measured: those tests SKIP when their file runs alone and
# EXECUTE when the surveyor package is collected first.
#
# PREVENT rather than clean up. Cleaning up afterwards is too late by
# construction: ``pytest.mark.skipif(not os.environ.get("GROQ_API_KEY"))``
# evaluates its condition when the test MODULE is imported, which is before any
# post-collection hook can run. Measured — a collection-finish scrub left the
# two Groq tests un-skipped, and they then failed on ``os.environ["GROQ_API_KEY"]``
# raising KeyError. Removing the value after something has already read it fixes
# nothing.
#
# So neutralise the load itself. This conftest is imported before any test
# module, so replacing ``dotenv.load_dotenv`` here means pymilvus's
# ``from dotenv import load_dotenv`` later binds to the no-op. Precise: alfred's
# own dotenv handling is hand-rolled in ``alfred._env`` (load_dotenv_file /
# auto_load_dotenv) and does not go through python-dotenv at all, so nothing of
# ours changes behaviour — only the third-party import side effect stops.
_SUPPRESSED_DOTENV = False
try:  # python-dotenv is a pymilvus dependency, not a direct one — may be absent
    import dotenv as _dotenv

    def _no_autoload_dotenv(*a, **kw):  # noqa: ANN002, ANN003
        """No-op stand-in for ``dotenv.load_dotenv`` during tests."""
        return False

    _dotenv.load_dotenv = _no_autoload_dotenv
    _SUPPRESSED_DOTENV = True
except ImportError:  # pragma: no cover - depends on optional extras
    pass

# Belt behind the prevention above: anything that still manages to introduce a
# credential during collection gets removed before tests run. Anything an
# operator genuinely exported is untouched (it is in the baseline); only values
# conjured by an import are removed, so someone who exports GROQ_API_KEY on
# purpose to run the live tests still can.
_ENV_BASELINE = dict(os.environ)

# Only credential-shaped names. A test that legitimately depends on some other
# import-time env var keeps working.
_CREDENTIAL_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


# What collection injected, recorded before it is scrubbed. This is the fact
# ``test_collection_leaves_no_credential_env_vars`` asserts on — deliberately a
# RECORDING rather than a live os.environ check at test time. A live check would
# also catch credentials left behind by any earlier TEST, which is a different
# and much broader contract: the CLI dispatchers inject ALFRED_TRANSPORT_TOKEN
# and friends into os.environ by documented design (see CLAUDE.md, "Dispatcher
# env-var injection"). Conflating the two would make the pin fail for a reason
# it is not about, and the fix would be to weaken the pin.
_collection_injected: list[str] = []


def pytest_collection_finish(session):
    """Undo credential env vars introduced during collection/import.

    Runs after every test module is imported and before any test executes —
    the only window where the pollution exists but has not yet been read.
    """
    injected = sorted(
        name
        for name in list(os.environ)
        if name not in _ENV_BASELINE
        and name.endswith(_CREDENTIAL_ENV_SUFFIXES)
    )
    for name in injected:
        del os.environ[name]
    _collection_injected[:] = injected
    if injected:
        # ILB: a silent scrub would be indistinguishable from "the import side
        # effect went away", and the next person would not know why the live
        # tests skip.
        session.config._alfred_scrubbed_env = injected


def pytest_runtest_protocol(item, nextitem):  # noqa: ARG001
    global _current_nodeid
    _current_nodeid = item.nodeid
    return None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Detect shadowing while it is still observable.

    Checked HERE and not in a teardown fixture: the shadowing patch is usually
    installed by ``monkeypatch``, which reverts during teardown. Autouse
    fixtures are set up before explicitly-requested ones and so tear down after
    them, meaning by the time a teardown check runs our hook is already restored
    and the shadow has vanished. Measured — a teardown check reported
    test_brief_feed_parity as clean despite its patch having been live for the
    whole test body. Wrapping the CALL phase looks at the moment that matters.
    """
    yield
    if not _guard_is_live():
        _unverified_tests.append(item.nodeid)


@pytest.fixture(autouse=True)
def _assert_no_egress(request):
    """Fail a test that reached a non-loopback address.

    Asserted in teardown rather than mid-call so the test body runs exactly as
    it does without the guard — see the module comment on why refusing loses.

    ATTRIBUTION CAVEAT, stated because it is real: egress from a background
    thread lands on whichever test is current when the thread runs, which is
    usually but not always the test that caused it. The nodeid is a strong lead,
    not a proof — chase the destination, not only the test name.
    """
    yield
    if request.node.get_closest_marker("live_network") is not None:
        return
    with _egress_lock:
        seen = sorted(_connect_by_test.get(request.node.nodeid, ()))
    if seen:
        pytest.fail(
            "suite egress guard: this test CONNECTED to a non-loopback address:\n  "
            + "\n  ".join(seen)
            + "\n\nTests must not talk to the internet. Stub the transport "
              "(tests/telegram/conftest.py stubs a client library's request "
              "layer; tests/health/conftest.py stubs the probe function — those "
              "are the two shapes), or mark the test @pytest.mark.live_network "
              "if reaching the real service IS the point.",
            pytrace=False,
        )


def pytest_terminal_summary(terminalreporter, *a, **kw):  # noqa: ARG001
    """Report what could not be pinned on an individual test.

    Two things land here. Connects during import/collection belong to no test,
    so nothing could be failed for them — they are reported red. DNS-without-
    connect is reported plainly: it is mostly alfred's own boundary code
    classifying a destination before refusing it, but it is also the one shape a
    connect-only guard would miss (a leak whose resolution fails), so it is
    shown rather than dropped.

    ILB: the clean case says so out loud. A guard that is silent when healthy is
    indistinguishable from a guard that never installed.
    """
    with _egress_lock:
        stray = sorted(_connect_by_test.get(_IMPORT_PHASE, ()))
        dns_tests = {k: v for k, v in _dns_by_test.items()
                     if not _connect_by_test.get(k)}
        connected_any = any(_connect_by_test.values())

    w = terminalreporter.write_line

    scrubbed = getattr(terminalreporter.config, "_alfred_scrubbed_env", None)
    if scrubbed:
        w("")
        w(f"suite env hygiene: scrubbed {len(scrubbed)} credential var(s) injected "
          f"during collection (pymilvus imports dotenv and loads the repo .env): "
          f"{', '.join(scrubbed)}.")
        w("  Key-gated live tests therefore SKIP unless you exported a key yourself.")

    if stray:
        w("")
        w("SUITE EGRESS GUARD: connect during import/collection "
          "(attributable to no single test):", red=True)
        for dest in stray:
            w(f"    {dest}", red=True)

    if dns_tests:
        total = sum(len(v) for v in dns_tests.values())
        w("")
        w(f"suite egress guard: {total} DNS resolution(s) with no connection, "
          f"across {len(dns_tests)} test(s) — informational, not failures.")
        hosts = sorted({d for v in dns_tests.values() for d in v})
        for dest in hosts[:10]:
            w(f"    {dest}")
        if len(hosts) > 10:
            w(f"    ... and {len(hosts) - 10} more")

    if _unverified_tests:
        w("")
        w(f"suite egress guard: {len(_unverified_tests)} test(s) UNVERIFIED — "
          f"another socket patch was live, so this guard could not observe them "
          f"(last patch wins). Not a failure; those tests carry their own pin.")
        for nodeid in _unverified_tests[:5]:
            w(f"    {nodeid}")
        if len(_unverified_tests) > 5:
            w(f"    ... and {len(_unverified_tests) - 5} more")

    if not stray and not connected_any and not dns_tests and not _unverified_tests:
        w("")
        w("suite egress guard: ACTIVE, no non-loopback traffic observed.")
