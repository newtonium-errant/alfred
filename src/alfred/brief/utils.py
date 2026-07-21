"""Logging setup and utility helpers.

The brief's ``setup_logging`` is shared with several no-bespoke-logger
daemons in the orchestrator (BIT, daily_sync, brief_digest_push, digest,
radar_day, friction_analyzer, pending_items_pusher, cloudflared
supervisor). Any signature change here ripples through 8 runner sites
in ``orchestrator.py`` — see the grep for ``from alfred.brief.utils
import setup_logging`` before touching the kwargs.
"""

from __future__ import annotations

import enum
import logging
import sys
from pathlib import Path
from typing import NamedTuple

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


class SectionReadStatus(enum.Enum):
    """Outcome of :func:`safe_read_section_file`."""

    OK = "ok"
    NOT_FOUND = "not_found"
    OS_ERROR = "os_error"
    DECODE_ERROR = "decode_error"


class SectionRead(NamedTuple):
    """Result of a defensive section-file read.

    ``text`` is the file content when ``status is OK``, else ``None``.
    ``detail`` / ``error_type`` carry ``str(exc)`` and the exception class
    name on failure (both ``""`` on success) so callers can preserve their
    own log fields and degrade messages.
    """

    status: SectionReadStatus
    text: str | None
    detail: str
    error_type: str


def safe_read_section_file(path: Path) -> SectionRead:
    """Read a brief section file, catching every read failure uniformly.

    Brief section renderers are called BARE by the daemon — there is no
    per-section boundary guard, because each renderer is trusted to be
    internally total (it returns its own degrade line rather than raising).
    So an unhandled read exception escapes the renderer and kills the ENTIRE
    brief for that run.

    ``Path.read_text(encoding="utf-8")`` can raise three disjoint things:

    * ``FileNotFoundError`` — the file is missing (a subclass of ``OSError``);
    * other ``OSError`` — permission denied, is-a-directory, an I/O error;
    * ``UnicodeDecodeError`` — a corrupted / non-UTF-8 file. This one
      subclasses ``ValueError`` (via ``UnicodeError``), **not** ``OSError`` —
      so an ``except OSError`` misses it and it escapes. That exact gap
      recurred three times in this package (``watches``, ``tier_section``,
      ``stayc_relay``) before this helper centralized the catch.

    Returns a :class:`SectionRead` discriminating the outcome so each caller
    keeps its own degrade rendering (a not-found spool and a corrupted spool
    warrant different operator messages). A fourth section reader inherits
    the complete catch for free by calling this instead of ``read_text``.
    """
    try:
        return SectionRead(
            SectionReadStatus.OK, path.read_text(encoding="utf-8"), "", "",
        )
    except FileNotFoundError as exc:
        return SectionRead(
            SectionReadStatus.NOT_FOUND, None, str(exc), exc.__class__.__name__,
        )
    except OSError as exc:
        return SectionRead(
            SectionReadStatus.OS_ERROR, None, str(exc), exc.__class__.__name__,
        )
    except UnicodeDecodeError as exc:
        return SectionRead(
            SectionReadStatus.DECODE_ERROR, None, str(exc),
            exc.__class__.__name__,
        )
