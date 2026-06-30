"""Message-record — the on-disk envelope carried by the bus.

Plain markdown = YAML frontmatter + markdown body. The body is the
handover/request/fyi/reply content; the frontmatter is the routing
envelope. NOT a vault record (no schema.py / scope.py coupling).

Read-state is DIRECTORY POSITION (no separate drain-side state file): a
message is UNREAD while it sits as ``<inbox>/*.md`` and DRAINED once moved
to ``<inbox>/read/<…>.md`` (mirrors curator's inbox→processed move).
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import frontmatter

from alfred.transport.peers import normalize_precedence

if TYPE_CHECKING:
    from .registry import ProjectRegistry


# The v1 message-kind enum. Layer-2 contract kinds (propose/counter/…)
# are a SEPARATE downstream arc and are NOT in this set.
MESSAGE_KINDS: frozenset[str] = frozenset(
    {"handover", "request", "fyi", "reply"},
)


@dataclass
class MessageRecord:
    """One bus message. ``from``/``to`` are project slugs (mapped to the
    Python-safe ``from_project``/``to_project`` since ``from`` is a
    keyword). Router stamps ``routed_at``/``routed_by`` at placement; drain
    stamps ``read_at``."""

    id: str = ""
    from_project: str = ""
    to_project: str = ""
    kind: str = ""
    correlation_id: str = ""
    created: str = ""
    subject: str = ""
    reply_to: str = ""
    precedence: str = "R"
    body: str = ""
    routed_at: str = ""
    routed_by: str = ""
    read_at: str = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _frontmatter_dict(record: MessageRecord) -> dict[str, str]:
    """The frontmatter mapping (only non-empty optional stamps emitted)."""
    fm: dict[str, str] = {
        "id": record.id,
        "from": record.from_project,
        "to": record.to_project,
        "kind": record.kind,
        "correlation_id": record.correlation_id,
        "created": record.created,
        "subject": record.subject,
        "precedence": record.precedence,
    }
    for key, value in (
        ("reply_to", record.reply_to),
        ("routed_at", record.routed_at),
        ("routed_by", record.routed_by),
        ("read_at", record.read_at),
    ):
        if value:
            fm[key] = value
    return fm


def parse_message_file(path: str | Path) -> MessageRecord:
    """Parse a message file into a :class:`MessageRecord`.

    Raises whatever ``frontmatter.load`` raises on a malformed file — the
    caller (``scan_spool``) wraps it in try/except and quarantines.
    ``precedence`` is coerced via the canonical
    :func:`peers.normalize_precedence` (unknown → ``R``).
    """
    post = frontmatter.load(str(path))
    fm = dict(post.metadata or {})
    precedence, _ = normalize_precedence(fm.get("precedence"))
    return MessageRecord(
        id=str(fm.get("id", "") or ""),
        from_project=str(fm.get("from", "") or ""),
        to_project=str(fm.get("to", "") or ""),
        kind=str(fm.get("kind", "") or ""),
        correlation_id=str(fm.get("correlation_id", "") or ""),
        created=str(fm.get("created", "") or ""),
        subject=str(fm.get("subject", "") or ""),
        reply_to=str(fm.get("reply_to", "") or ""),
        precedence=precedence,
        body=post.content or "",
        routed_at=str(fm.get("routed_at", "") or ""),
        routed_by=str(fm.get("routed_by", "") or ""),
        read_at=str(fm.get("read_at", "") or ""),
    )


def write_message_file(path: str | Path, record: MessageRecord) -> None:
    """Atomically write a message file (``.tmp`` → ``os.replace``).

    Atomic so a torn write never leaves a half-parsed file in the spool or
    an inbox (the router re-places idempotently on the next tick)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(record.body or "", **_frontmatter_dict(record))
    rendered = frontmatter.dumps(post)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(rendered)
    os.replace(tmp, p)


def validate_record(
    record: MessageRecord,
    registry: "ProjectRegistry | None" = None,
    *,
    valid_kinds: "frozenset[str] | None" = None,
) -> list[str]:
    """Return a list of validation errors (empty == valid).

    Structural checks (every required field present + a known ``kind``).
    ``valid_kinds`` defaults to :data:`MESSAGE_KINDS`; the router widens it
    to ``MESSAGE_KINDS | CONTRACT_KINDS`` so a Layer-2 contract message
    (kind=propose/counter/…) is not malform-quarantined. When ``registry``
    is given, ALSO checks the destination is registered (the ``unknown
    destination`` error, classified as *undeliverable* rather than
    *malformed* by :func:`router.scan_spool`)."""
    accepted = valid_kinds if valid_kinds is not None else MESSAGE_KINDS
    errors: list[str] = []
    if not record.id:
        errors.append("missing id")
    if not record.from_project:
        errors.append("missing from")
    if not record.to_project:
        errors.append("missing to")
    if not record.kind:
        errors.append("missing kind")
    elif record.kind not in accepted:
        errors.append(f"invalid kind: {record.kind}")
    if not record.correlation_id:
        errors.append("missing correlation_id")
    if not record.created:
        errors.append("missing created")
    if not record.subject:
        errors.append("missing subject")
    if (
        registry is not None
        and record.to_project
        and record.to_project not in registry.names()
    ):
        errors.append(f"unknown destination project: {record.to_project}")
    return errors


def _compact_ts(created: str) -> str:
    """A sortable compact timestamp for the filename (``YYYYmmddTHHMMSSZ``).
    Falls back to an all-zero stamp when ``created`` doesn't parse (the file
    still routes — the id carries the real identity)."""
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "00000000T000000Z"
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _slug(value: str) -> str:
    """Filesystem-safe slug for the ``from`` segment of the filename."""
    return "".join(
        ch if (ch.isalnum() or ch in "-_") else "-" for ch in str(value)
    ) or "unknown"


def message_filename(record: MessageRecord) -> str:
    """Sortable, id-keyed filename: ``<compact-ts>-<from>-<id>.md``.

    The ``id`` segment makes the destination filename DETERMINISTIC per
    message, which is what makes placement idempotent — a torn re-place
    overwrites the same target."""
    return f"{_compact_ts(record.created)}-{_slug(record.from_project)}-{record.id}.md"


def to_summary_dict(record: MessageRecord) -> dict[str, str]:
    """A small dict for CLI/JSON surfaces (no body)."""
    d = asdict(record)
    d.pop("body", None)
    return d


__all__ = [
    "MESSAGE_KINDS",
    "MessageRecord",
    "message_filename",
    "parse_message_file",
    "to_summary_dict",
    "validate_record",
    "write_message_file",
]
