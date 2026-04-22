"""Regression tests for talker (Telegram daemon) logging contract.

Background: the surveyor previously had a silent-writer bug where its
``setup_logging`` wired structlog through ``PrintLoggerFactory``, writing
events to stdout. In daemon mode the orchestrator redirects stdout to
``/dev/null`` (see ``alfred.orchestrator._silence_stdio``), so every
structured event was silently dropped — only ``httpx`` debug lines (which
use stdlib logging) ever reached ``data/surveyor.log``. The fix routed
structlog through stdlib logging so the configured ``FileHandler`` actually
received the events (commit ``e6b5ad6``).

The talker has the exact same exposure shape: the orchestrator wraps
``_run_talker`` with ``_silence_stdio`` then asyncio-runs the daemon, and
the daemon's own ``setup_logging`` configures structlog. If anyone ever
"simplifies" the talker's helper to use ``PrintLoggerFactory`` again,
``data/talker.log`` will silently lose every ``talker.bot.*`` /
``talker.session.*`` / ``talker.capture.*`` event — same operational
impact as the surveyor regression, same diagnosis cost.

These tests pin the contract so a future refactor can't put us back into
the silent-talker state.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import structlog

from alfred.telegram.utils import setup_logging, get_logger


@pytest.fixture(autouse=True)
def _reset_logging():
    """Each test gets a fresh stdlib + structlog config.

    ``setup_logging`` calls ``logging.basicConfig(force=True)`` which
    replaces handlers, but structlog itself is process-global with
    ``cache_logger_on_first_use=True``. The reset here is defensive — if a
    later test in the suite touches structlog config, prior tests' loggers
    won't leak captured handlers into this module.
    """
    yield
    structlog.reset_defaults()
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def test_setup_logging_routes_structlog_events_to_log_file(tmp_path: Path):
    """The headline regression: structlog events must land in the log file.

    This is the exact contract the surveyor's silent-writer bug violated.
    If this test starts failing, the talker is back to writing
    ``talker.bot.inbound`` / ``talker.session.opened`` / ``talker.capture.*``
    invisibly while the daemon appears alive from the outside.
    """
    log_file = tmp_path / "talker.log"
    setup_logging(level="INFO", log_file=str(log_file), suppress_stdout=True)

    log = structlog.get_logger("alfred.telegram.bot")
    log.info(
        "talker.bot.inbound",
        chat_id=8661018406,
        kind="text",
        length=11,
        user_id=8661018406,
    )
    log.info(
        "talker.session.opened",
        chat_id=8661018406,
        model="claude-sonnet-4-6",
        session_id="test-session-id",
    )

    # Force any buffered handlers to disk before reading.
    for h in logging.getLogger().handlers:
        h.flush()

    contents = log_file.read_text(encoding="utf-8")
    assert "talker.bot.inbound" in contents, (
        "talker.bot.inbound event missing from log file. The talker daemon "
        "appears alive but logs nothing — same shape as the surveyor "
        "silent-writer regression. Check that setup_logging uses "
        "structlog.stdlib.LoggerFactory (not PrintLoggerFactory) so events "
        f"flow through the FileHandler. Contents:\n{contents}"
    )
    assert "talker.session.opened" in contents
    assert "test-session-id" in contents


def test_setup_logging_works_after_silence_stdio(tmp_path: Path):
    """The orchestrator runs ``_silence_stdio`` BEFORE handing control to the
    talker daemon. After that redirect, ``sys.stdout`` points at /dev/null
    and ``sys.stderr`` points at the log file. The talker's ``setup_logging``
    must still produce structured events that land in the FileHandler — the
    silent-writer regression manifested precisely because the previous
    surveyor helper depended on a real stdout, which by that point was
    /dev/null.

    This test simulates the orchestrator's environment (``suppress_stdout=True``,
    no usable stdout) and asserts the round-trip still works.
    """
    import os
    import sys

    log_file = tmp_path / "talker.log"

    # Mirror ``orchestrator._silence_stdio`` — redirect stdout to devnull
    # and stderr to the talker log. We restore originals in finally so we
    # don't poison subsequent tests.
    saved_stdout, saved_stderr = sys.stdout, sys.stderr
    devnull = open(os.devnull, "w")
    stderr_sink = open(log_file, "a")
    sys.stdout = devnull
    sys.stderr = stderr_sink
    try:
        setup_logging(
            level="INFO",
            log_file=str(log_file),
            suppress_stdout=True,
        )
        log = get_logger("alfred.telegram.bot")
        log.info("talker.bot.outbound", chat_id=1234, length=42, ok=True)

        for h in logging.getLogger().handlers:
            h.flush()
    finally:
        sys.stdout = saved_stdout
        sys.stderr = saved_stderr
        devnull.close()
        stderr_sink.close()

    contents = log_file.read_text(encoding="utf-8")
    assert "talker.bot.outbound" in contents, (
        "Even after _silence_stdio swallowed stdout, structured events must "
        "still reach the file via the stdlib FileHandler. If this fails, "
        "the talker daemon will go silent in production exactly when the "
        "orchestrator's suppression is active. Contents:\n"
        f"{contents}"
    )


def test_module_level_logger_picks_up_factory_after_setup(tmp_path: Path):
    """The talker imports ``log = get_logger(__name__)`` at module load time
    in ``bot.py`` / ``session.py`` / ``capture_batch.py`` / etc. — BEFORE
    ``setup_logging`` runs. With ``cache_logger_on_first_use=True``,
    structlog returns a lazy proxy that delays binding until the first
    method call, so the factory configured by ``setup_logging`` should
    still apply.

    This test pins that contract: a logger fetched before ``setup_logging``
    must produce events that land in the configured file once we start
    logging through it.
    """
    # Fetch the logger first — simulates ``bot.py`` module load.
    log = get_logger("alfred.telegram.bot")

    # Now configure logging — simulates ``daemon.py::run`` calling
    # ``setup_logging`` after imports.
    log_file = tmp_path / "talker.log"
    setup_logging(level="INFO", log_file=str(log_file), suppress_stdout=True)

    # First .info call after configuration — must bind to the just-configured
    # factory, not a default that drops to stdout.
    log.info("talker.capture.silent_turn", chat_id=42, turn_index=1)

    for h in logging.getLogger().handlers:
        h.flush()

    contents = log_file.read_text(encoding="utf-8")
    assert "talker.capture.silent_turn" in contents, (
        "A module-level logger fetched before setup_logging should still "
        "land in the file after the lazy proxy binds on first use. If this "
        "fails, the cache_logger_on_first_use contract is broken and the "
        f"talker will silently drop early events. Contents:\n{contents}"
    )
