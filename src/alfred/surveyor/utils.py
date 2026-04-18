"""Logging, hashing, and retry helpers."""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

import structlog


def compute_md5(file_path: Path) -> str:
    """Compute MD5 hex digest of a file's contents."""
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_md5_bytes(data: bytes) -> str:
    """Compute MD5 hex digest of raw bytes."""
    return hashlib.md5(data).hexdigest()


def setup_logging(level: str = "INFO", log_file: str | None = None, suppress_stdout: bool = False) -> None:
    """Configure structlog + stdlib logging.

    The structlog config routes records through the stdlib ``logging`` module
    so they hit the ``FileHandler`` configured below. The previous
    ``PrintLoggerFactory`` setup wrote events directly to stdout, which the
    orchestrator redirects to ``/dev/null`` in daemon mode — every
    ``writer.tags_*`` / ``daemon.*`` event was silently dropped, leaving
    ``data/surveyor.log`` populated only by ``httpx`` chatter (httpx uses
    stdlib logging, so its records leaked through the FileHandler). Mirrors
    the curator/janitor/distiller helpers — the four daemons must agree on
    their logging contract or audits diverge per-tool.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Stdlib root logger
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
