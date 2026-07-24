"""Shared pytest fixtures for the Alfred test suite.

These fixtures are intentionally minimal — they exist to give tests a
working vault layout and a config dict that mirrors ``config.yaml.example``
without touching the real vault or the user's checked-in config.
"""

from __future__ import annotations

import logging
import sys
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
