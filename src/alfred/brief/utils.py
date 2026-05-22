"""Logging setup and utility helpers.

The brief's ``setup_logging`` is shared with several no-bespoke-logger
daemons in the orchestrator (BIT, daily_sync, brief_digest_push, digest,
radar_day, friction_analyzer, pending_items_pusher, cloudflared
supervisor). Any signature change here ripples through 8 runner sites
in ``orchestrator.py`` — see the grep for ``from alfred.brief.utils
import setup_logging`` before touching the kwargs.
"""

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
