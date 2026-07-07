"""Logging helpers for the web module.

Same shape as every other tool's ``utils.py``. Like the transport server,
the web surface does not run as a standalone daemon — it is hosted inside
the talker's event loop — so its log output is multiplexed onto the
talker's log file. ``setup_logging`` exists for parity / standalone test
exercise; inside the talker the daemon's own ``setup_logging`` has already
wired structlog before the web routes are registered.
"""

from __future__ import annotations

import logging
import sys

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
    """Configure structlog + stdlib logging for the web helpers."""
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

    emit_rotation_policy_log(log_file, resolved_max_bytes, resolved_backup_count)


def get_logger(name: str = __name__) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def pcm_rms(data: bytes) -> float:
    """RMS energy of s16le mono PCM (0 .. ~32767). Pure stdlib — no numpy /
    audioop (audioop is removed in 3.13). Used ONLY for input-energy
    observability (avg/peak, quiet detection); it never touches transcript
    content. An odd trailing byte (half a sample) is dropped."""
    import array
    import math

    if not data:
        return 0.0
    if len(data) % 2:
        data = data[:-1]
    samples = array.array("h")
    samples.frombytes(data)
    if not samples:
        return 0.0
    total = 0
    for s in samples:
        total += s * s
    return math.sqrt(total / len(samples))
