"""Shared pytest fixtures for the Alfred test suite.

These fixtures are intentionally minimal — they exist to give tests a
working vault layout and a config dict that mirrors ``config.yaml.example``
without touching the real vault or the user's checked-in config.
"""

from __future__ import annotations

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
# Long-term fix (deferred — separate arc): the eight ``setup_logging``
# helpers in ``src/alfred/*/utils.py`` should maintain a module-level
# processor list and mutate it in place rather than passing a new list
# literal each call. That removes the underlying reference-reassignment
# trap and lets the fixture be removed. Per project_next_session.md.


@pytest.fixture(autouse=True)
def _bust_structlog_lazy_proxy_cache():
    """Autouse: bust cached ``bind`` on every ``alfred.*`` module-level
    structlog logger BEFORE each test runs.

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
                # ``bind`` override is set on the instance when
                # ``cache_logger_on_first_use=True`` was active at the
                # logger's first usage. Deletion forces re-resolution
                # against the CURRENT ``_CONFIG.default_processors``
                # on the next ``log.info(...)`` call. Safe even if no
                # instance override exists — guard with ``in __dict__``.
                if "bind" in candidate.__dict__:
                    del candidate.bind
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
