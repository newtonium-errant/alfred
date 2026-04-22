"""Logging setup and utility helpers."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog


def setup_logging(level: str = "INFO", log_file: str | None = None, suppress_stdout: bool = False) -> None:
    """Configure structlog + stdlib logging.

    The structlog config routes records through the stdlib ``logging`` module
    so they hit the ``FileHandler`` configured below. A previous incarnation
    of the surveyor's twin helper used ``PrintLoggerFactory``, which wrote
    events directly to stdout — under the orchestrator's daemon-mode
    ``_silence_stdio`` redirect that meant every structured event was
    silently dropped, leaving ``data/<tool>.log`` populated only by ``httpx``
    chatter (httpx uses stdlib logging, so its records still reached the
    FileHandler). The talker has the exact same exposure shape as the
    surveyor — same orchestrator wiring, same ``suppress_stdout=True`` in
    the foreground/internal path — so it must use the stdlib factory for
    the same reason. Mirrors the curator/janitor/distiller/surveyor helpers
    — the daemons must agree on their logging contract or audits diverge
    per-tool.

    On ``cache_logger_on_first_use=True``: structlog returns a lazy proxy
    from ``get_logger`` that delays factory binding until the first
    ``log.info(...)`` call. That means module-level
    ``log = get_logger(__name__)`` calls in ``bot.py``/``session.py``/etc.
    are safe to evaluate before this function runs — the binding picks up
    whatever factory is configured at the moment of first use.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    handlers: list[logging.Handler] = []
    if not suppress_stdout:
        handlers.append(logging.StreamHandler(sys.stdout))
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(str(log_path), encoding="utf-8"))

    logging.basicConfig(
        format="%(message)s",
        level=log_level,
        handlers=handlers,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = __name__) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
