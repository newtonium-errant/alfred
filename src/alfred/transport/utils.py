"""Logging helpers for the transport module.

Same shape as every other tool's ``utils.py`` so the orchestrator's
per-tool logging wiring treats transport uniformly. The transport does
not run as a standalone daemon — it is hosted inside the talker's event
loop — so its log output is multiplexed onto the talker's log file by
default, but the helpers here let callers route to a different sink if
needed.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None,
    suppress_stdout: bool = False,
) -> None:
    """Configure structlog + stdlib logging for the transport helpers.

    Called from places where the transport is exercised outside the
    talker daemon (CLI smoke commands, BIT probes, standalone tests).
    Inside the talker, the daemon's own ``setup_logging`` has already
    wired structlog before the transport server starts.
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


def chunk_for_telegram(text: str, max_chars: int = 3800) -> list[str]:
    """Split ``text`` into Telegram-safe chunks.

    Telegram's single-message limit is 4096 characters, but we chunk at
    3800 to leave headroom for quoting, footers, and multi-byte edge
    cases. Algorithm (in preference order):

    1. Empty body → ``[""]`` (callers decide whether to send or skip).
    2. Whole text fits → single-chunk list.
    3. Split on paragraph breaks (``\\n\\n``). Pack paragraphs greedily
       into chunks up to ``max_chars``.
    4. If a single paragraph exceeds ``max_chars``, split it on sentence
       boundaries (``. `` / ``? `` / ``! ``) into sentences, then pack.
    5. If a single sentence still exceeds ``max_chars``, hard-wrap at
       ``max_chars`` as a last resort.

    Paragraphs within a chunk are rejoined with ``\\n\\n`` so the
    structure survives the split. Called by the brief daemon before
    dispatching to ``send_outbound_batch``.
    """
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]

    # Split on blank-line separators. The ``\n\n`` split does not
    # preserve separators; we rejoin with ``\n\n`` inside the chunk.
    paragraphs = text.split("\n\n")

    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0

    def _flush() -> None:
        nonlocal buf, buf_len
        if buf:
            chunks.append("\n\n".join(buf))
            buf = []
            buf_len = 0

    for para in paragraphs:
        # +2 for the ``\n\n`` that will rejoin this paragraph to the
        # previous one (no-op cost when buf is empty).
        join_cost = 2 if buf else 0
        if len(para) + join_cost + buf_len <= max_chars:
            buf.append(para)
            buf_len += len(para) + join_cost
            continue

        # Current buffer plus this paragraph overflows. Flush the buffer
        # first so the current content lands in its own chunk.
        _flush()

        if len(para) <= max_chars:
            buf.append(para)
            buf_len = len(para)
            continue

        # Single paragraph is larger than the limit — split it on
        # sentence boundaries, then hard-wrap any sentence that still
        # overflows. We rebuild the sentences with their terminator
        # intact so the text remains readable.
        pieces = _split_long_paragraph(para, max_chars)
        for piece in pieces:
            if buf_len + len(piece) + (1 if buf else 0) <= max_chars:
                buf.append(piece)
                buf_len += len(piece) + (1 if buf_len else 0)
            else:
                _flush()
                buf.append(piece)
                buf_len = len(piece)

    _flush()
    return chunks


def _split_long_paragraph(paragraph: str, max_chars: int) -> list[str]:
    """Split a paragraph that exceeds ``max_chars`` into safe pieces.

    First attempts sentence-boundary splits (``. ``, ``? ``, ``! ``).
    Any resulting piece still over ``max_chars`` gets hard-wrapped.
    """
    # Sentence split: keep the terminator with the preceding sentence.
    import re
    parts = re.split(r"(?<=[.!?])\s+", paragraph)
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        if len(part) <= max_chars:
            out.append(part)
            continue
        # Hard-wrap fallback — split into fixed-width slices.
        for i in range(0, len(part), max_chars):
            out.append(part[i : i + max_chars])
    return out
