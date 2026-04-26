"""Logging setup and utility helpers."""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path

import structlog


def setup_logging(level: str = "INFO", log_file: str | None = None, suppress_stdout: bool = False) -> None:
    """Configure structlog + stdlib logging."""
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


def file_hash(path: Path) -> str:
    """Return SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_md5(path: Path) -> str:
    """Return MD5 hex digest of a file."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# YAML frontmatter delimiter — see ``compute_body_hash``.
_FRONTMATTER_DELIM = "---"


def compute_body_hash(text: str) -> str:
    """Return SHA-256 hex digest of the body only (frontmatter stripped).

    The skip-distill gate uses this instead of the full-file md5 because
    janitor deep_sweep_fix and surveyor alfred_tags both write cosmetic
    frontmatter changes that don't shift the source's claim wording. A
    body-only hash skips those rewrites; body bytes only change when
    the actual content shifts (LINK001 wikilink repair, STUB001
    enrichment), which legitimately should re-trigger extraction.

    Body extraction follows the standard frontmatter convention: a
    document opening with ``---\\n…\\n---\\n`` has its frontmatter block
    stripped; everything after the closing ``---`` is the body. Documents
    without frontmatter are hashed in full. Trailing whitespace is
    normalized via ``.rstrip()`` so a trailing newline change alone
    won't cause a re-extract.
    """
    body = _extract_body(text)
    return hashlib.sha256(body.rstrip().encode("utf-8")).hexdigest()


def _extract_body(text: str) -> str:
    """Strip the ``---``-delimited frontmatter block, returning the body."""
    if not text.startswith(_FRONTMATTER_DELIM):
        return text
    # Find the closing delimiter on its own line. Splitting on the
    # newline-anchored marker prevents stray ``---`` inside frontmatter
    # values (rare but legal in YAML strings) from terminating early.
    rest = text[len(_FRONTMATTER_DELIM):]
    # ``---`` must be followed by newline to count as the opening fence.
    if not rest.startswith(("\n", "\r\n")):
        return text
    # Search for the closing ``\n---`` (followed by newline or EOF).
    idx = 0
    while True:
        nxt = rest.find("\n" + _FRONTMATTER_DELIM, idx)
        if nxt == -1:
            return text  # no closing fence — treat whole text as body
        after = nxt + 1 + len(_FRONTMATTER_DELIM)
        # Closing fence must be at line end (newline or EOF), not e.g. ``----``.
        if after == len(rest) or rest[after] in ("\n", "\r"):
            body = rest[after:]
            # Drop leading newline that follows the closing fence.
            if body.startswith("\r\n"):
                return body[2:]
            if body.startswith("\n"):
                return body[1:]
            return body
        idx = nxt + 1


def get_logger(name: str = __name__) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
