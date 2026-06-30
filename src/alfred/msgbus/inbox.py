"""Inbox drain — read-state is directory position.

A message is UNREAD while it sits as ``<inbox>/*.md``; DRAINED once moved
to ``<inbox>/read/<…>.md`` with a ``read_at`` stamp (mirrors curator's
inbox→processed move). "Unread count" = number of ``*.md`` files directly
in ``<inbox>/`` (the ``read/`` sub-dir is excluded by the non-recursive
glob). Both the brief section (pull) and the router ping (push) count via
:func:`count_unread` so they can never diverge.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import structlog

from .record import (
    MessageRecord,
    parse_message_file,
    write_message_file,
)

log = structlog.get_logger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _unread_files(inbox_path: str | Path) -> list[Path]:
    """The ``*.md`` files directly in the inbox (the ``read/`` sub-dir is
    excluded — glob is non-recursive). Sorted (filename leads with a
    compact timestamp → chronological)."""
    inbox = Path(inbox_path)
    if not inbox.exists():
        return []
    return sorted(p for p in inbox.glob("*.md") if p.is_file())


def count_unread(inbox_path: str | Path) -> int:
    """Number of unread messages in the inbox (cheap — no parsing)."""
    return len(_unread_files(inbox_path))


def list_inbox(inbox_path: str | Path) -> list[MessageRecord]:
    """Parse the unread messages (a bad file logs + is skipped, never
    crashes the listing)."""
    records: list[MessageRecord] = []
    for md_file in _unread_files(inbox_path):
        try:
            records.append(parse_message_file(md_file))
        except Exception as exc:  # noqa: BLE001 — one bad file never kills the list
            log.warning(
                "msgbus.inbox.parse_failed",
                path=str(md_file),
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
    return records


def read_message(inbox_path: str | Path, message_id: str) -> MessageRecord | None:
    """Find an unread message by id (does NOT mark it read)."""
    for md_file in _unread_files(inbox_path):
        try:
            record = parse_message_file(md_file)
        except Exception:  # noqa: BLE001 — skip unparseable
            continue
        if record.id == message_id:
            return record
    return None


def drain_inbox(
    inbox_path: str | Path,
    *,
    mark_read: bool = True,
) -> list[MessageRecord]:
    """Drain the inbox: return the unread messages and (when
    ``mark_read``) move each to ``read/`` with a ``read_at`` stamp.

    ``mark_read=False`` is a dry-run list (no moves). The move is
    write-to-read/ then unlink-original so a crash mid-drain leaves the
    message readable in exactly one place (re-drain re-processes it)."""
    inbox = Path(inbox_path)
    read_dir = inbox / "read"
    drained: list[MessageRecord] = []
    for md_file in _unread_files(inbox):
        try:
            record = parse_message_file(md_file)
        except Exception as exc:  # noqa: BLE001 — skip + log, keep draining
            log.warning(
                "msgbus.inbox.drain_parse_failed",
                path=str(md_file),
                error=str(exc),
                error_type=exc.__class__.__name__,
            )
            continue
        if mark_read:
            record.read_at = _now_iso()
            read_copy = read_dir / md_file.name
            try:
                write_message_file(read_copy, record)
            except OSError as exc:
                log.warning(
                    "msgbus.inbox.drain_write_failed",
                    path=str(md_file),
                    error_type=exc.__class__.__name__,
                )
                continue
            try:
                md_file.unlink()
            except OSError as exc:
                # The read/ copy was written but the inbox file couldn't be
                # removed — without rollback the message would live in BOTH
                # inbox/ and read/ and be re-drained (a duplicate). Roll back
                # the read/ copy so it stays ONLY in inbox/ (re-drainable),
                # and don't count it as drained.
                log.warning(
                    "msgbus.inbox.drain_unlink_failed",
                    path=str(md_file),
                    error_type=exc.__class__.__name__,
                )
                try:
                    read_copy.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
        drained.append(record)
    if drained:
        log.info(
            "msgbus.inbox.drained",
            inbox=str(inbox),
            count=len(drained),
            marked_read=mark_read,
        )
    else:
        # Intentionally-left-blank: an empty drain is an explicit signal.
        log.info("msgbus.inbox.drain_empty", inbox=str(inbox))
    return drained


__all__ = [
    "count_unread",
    "drain_inbox",
    "list_inbox",
    "read_message",
]
