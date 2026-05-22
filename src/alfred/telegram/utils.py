"""Logging setup and utility helpers."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog

from alfred.common.logging_handler import (
    build_rotating_file_handler,
    emit_rotation_policy_log,
    resolve_rotation_policy,
)


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None,
    suppress_stdout: bool = False,
    *,
    max_bytes: int | None = None,
    backup_count: int | None = None,
) -> None:
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

    ``max_bytes`` / ``backup_count`` control size-based rotation of the
    log file via ``RotatingFileHandler``. ``None`` (the default) uses
    the bundled policy in ``alfred.common.logging_handler``. The
    orchestrator pulls these from ``raw["logging"]["rotation"]`` and
    threads them through every daemon's runner.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    resolved_max_bytes, resolved_backup_count = resolve_rotation_policy(
        max_bytes, backup_count
    )

    handlers: list[logging.Handler] = []
    if not suppress_stdout:
        handlers.append(logging.StreamHandler(sys.stdout))
    if log_file:
        handlers.append(
            build_rotating_file_handler(
                log_file,
                max_bytes=resolved_max_bytes,
                backup_count=resolved_backup_count,
            )
        )

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

    # Emit the resolved-policy receipt so operators can grep for
    # ``logging.rotation.policy_applied`` and confirm config was honored.
    # No-op when ``log_file`` is None. Per
    # ``feedback_intentionally_left_blank.md``.
    emit_rotation_policy_log(log_file, resolved_max_bytes, resolved_backup_count)


def get_logger(name: str = __name__) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
