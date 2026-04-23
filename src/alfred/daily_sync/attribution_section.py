"""Attribution-audit section provider — Phase 2 of the calibration audit arc.

c1 (``src/alfred/vault/attribution.py``) shipped the marker primitives.
c2 (``src/alfred/telegram/conversation.py``) wired Salem's vault_create
+ vault_edit body_append callsites so every agent-inferred body lands
with a BEGIN_INFERRED/END_INFERRED wrap and an ``attribution_audit``
frontmatter entry. That closed the WRITE half of the audit gap.

This module closes the READ half: every Daily Sync, Salem surfaces up
to N unconfirmed audit entries as a numbered batch Andrew can confirm
("6 confirm" → flip ``confirmed_by_andrew`` true) or reject
("6 reject" → strip the marked section + drop the entry). Without this
read path the markers go in but nothing ever acts on them.

Sampling strategy::

    1. Walk ``vault/**/*.md`` (or just ``daily_sync.attribution.scan_paths``
       when configured) and parse each file's ``attribution_audit``
       frontmatter via ``parse_audit_entries``.
    2. Keep entries where ``confirmed_by_andrew is False`` AND
       ``confirmed_at is None`` — anything already-confirmed stays
       silent, which is the intentionally-left-blank steady state.
    3. Sort by ``date`` descending (most recent unconfirmed first) so
       Andrew sees fresh markers before stale ones.
    4. Cap at ``daily_sync.attribution.batch_size`` (default 5).

Item rendering (matches the spec in the c3 task):

    6. [salem 2026-04-23 18:44 — note/Marker Smoke Test]
       Section: "Marker Smoke Test"
       Content: "Testing the attribution audit marker. ..."
       Reason: talker conversation turn (session=78a7c5a2)

The leading number is GLOBAL across the Daily Sync — the assembler
passes ``start_index`` so attribution items pick up where email
calibration left off (5 email items → attribution starts at 6).

Empty state: emits ``"## Attribution audit\\n\\nNo attribution items
pending review.\\n"`` per the intentionally-left-blank principle from
``feedback_intentionally_left_blank.md`` — silence is a bug, an
explicit "nothing to do" is observability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

import frontmatter
import structlog

from alfred.vault.attribution import AuditEntry, parse_audit_entries

from .config import DailySyncConfig

log = structlog.get_logger(__name__)


# Default attribution config when the ``daily_sync.attribution`` block is
# absent — enabled, batch of 5, scan the whole vault. Mirrored in
# ``config.yaml.example`` so the default behaviour is documented.
_DEFAULT_BATCH_SIZE = 5


@dataclass
class AttributionItem:
    """One item in a Daily Sync attribution-audit batch.

    All fields are display-derived from the underlying audit entry +
    the wrapped body content. Persisted into the state file's
    ``last_batch.attribution_items`` list so the reply dispatcher can
    resolve "item 6" → ``(record_path, marker_id)`` without re-reading
    the underlying record.
    """

    item_number: int  # 1-indexed, GLOBAL across Daily Sync sections
    record_path: str  # vault-relative
    marker_id: str
    agent: str
    date: str  # ISO 8601 from the audit entry
    section_title: str
    reason: str
    content_preview: str  # first ~140 chars of the wrapped body

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_number": self.item_number,
            "record_path": self.record_path,
            "marker_id": self.marker_id,
            "agent": self.agent,
            "date": self.date,
            "section_title": self.section_title,
            "reason": self.reason,
            "content_preview": self.content_preview,
        }


@dataclass
class _Candidate:
    record_path: str  # vault-relative
    entry: AuditEntry
    content_preview: str
    parsed_date: datetime | None  # for sorting


def _attribution_settings(config: DailySyncConfig) -> tuple[bool, int, list[str]]:
    """Return ``(enabled, batch_size, scan_paths)`` for attribution.

    The c2 ``DailySyncConfig`` dataclass doesn't yet carry an
    ``attribution`` block — when it lands, this helper will pull from
    ``config.attribution.*``. Until then we read defensively from the
    raw ``getattr`` so a tests-only ``DailySyncConfig`` that doesn't
    set the block still works (default: enabled, batch 5, full vault).
    """
    block = getattr(config, "attribution", None)
    if block is None:
        return (True, _DEFAULT_BATCH_SIZE, [])
    enabled = bool(getattr(block, "enabled", True))
    batch_size = int(getattr(block, "batch_size", _DEFAULT_BATCH_SIZE))
    scan_paths = list(getattr(block, "scan_paths", []) or [])
    return (enabled, batch_size, scan_paths)


def _parse_iso(date_str: str) -> datetime | None:
    """Tolerant ISO-8601 parser. Returns ``None`` on failure (so the
    candidate sorts last in a stable way)."""
    if not date_str:
        return None
    try:
        # ``fromisoformat`` handles the offsets we emit (``+00:00``)
        # and naive forms.
        return datetime.fromisoformat(date_str)
    except ValueError:
        return None


def _content_preview(body: str, marker_id: str, *, limit: int = 140) -> str:
    """Extract the wrapped content for ``marker_id`` from ``body``.

    Returns the first ``limit`` chars of the content between BEGIN/END
    markers, with whitespace collapsed. Returns an empty string when
    the marker isn't found in body — defensive for cases where the
    audit entry is in frontmatter but the body has been edited to
    remove the marker (Andrew may have manually cleaned up).
    """
    from alfred.vault.attribution import find_marker_bounds

    if not body:
        return ""
    bounds = find_marker_bounds(body, marker_id)
    if bounds is None:
        return ""
    begin, end = bounds
    lines = body.splitlines()
    inner = "\n".join(lines[begin + 1: end])
    text = " ".join(inner.split())
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return text


def _short_record_label(record_path: str) -> str:
    """Trim ``note/Foo.md`` → ``note/Foo`` for the rendered header."""
    if record_path.endswith(".md"):
        return record_path[:-3]
    return record_path


def _short_date(iso_date: str) -> str:
    """Render the audit entry date as ``YYYY-MM-DD HH:MM`` (UTC).

    Falls back to the raw string when parsing fails so we don't drop
    the date entirely.
    """
    parsed = _parse_iso(iso_date)
    if parsed is None:
        return iso_date
    return parsed.strftime("%Y-%m-%d %H:%M")


def _walk_vault(vault_path: Path, scan_paths: list[str]) -> Iterable[Path]:
    """Yield every ``*.md`` file to scan.

    When ``scan_paths`` is empty, walks the whole vault. Otherwise
    walks each subpath (joined to vault_path). Ignores hidden dirs
    (``.obsidian`` etc.) and the conventional ``_templates`` /
    ``_bases`` scaffolding so the scan doesn't trip on records
    whose ``attribution_audit`` field is illustrative not real.
    """
    skip_dirs = {".obsidian", ".git", "_templates", "_bases", "_docs"}
    roots: list[Path]
    if scan_paths:
        roots = [vault_path / p for p in scan_paths]
    else:
        roots = [vault_path]
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        if root.is_file():
            if root.suffix == ".md":
                yield root
            continue
        for md in root.rglob("*.md"):
            # Skip if any ancestor is a skip_dir relative to the vault.
            try:
                rel_parts = md.relative_to(vault_path).parts
            except ValueError:
                rel_parts = md.parts
            if any(part in skip_dirs for part in rel_parts):
                continue
            if md in seen:
                continue
            seen.add(md)
            yield md


def _read_candidates(vault_path: Path, scan_paths: list[str]) -> list[_Candidate]:
    """Walk the vault, parse audit entries, return unconfirmed candidates."""
    candidates: list[_Candidate] = []
    for md_file in _walk_vault(vault_path, scan_paths):
        try:
            post = frontmatter.load(str(md_file))
        except Exception:
            # Malformed YAML or unreadable file — skip with a log.
            log.info(
                "daily_sync.attribution.read_failed",
                path=str(md_file),
            )
            continue
        fm = post.metadata or {}
        entries = parse_audit_entries(fm)
        if not entries:
            continue
        body = post.content or ""
        try:
            rel_path = str(md_file.relative_to(vault_path)).replace("\\", "/")
        except ValueError:
            rel_path = str(md_file)
        for entry in entries:
            if entry.confirmed_by_andrew or entry.confirmed_at is not None:
                continue
            preview = _content_preview(body, entry.marker_id)
            parsed = _parse_iso(entry.date)
            candidates.append(_Candidate(
                record_path=rel_path,
                entry=entry,
                content_preview=preview,
                parsed_date=parsed,
            ))
    return candidates


def _sort_key(candidate: _Candidate) -> tuple[int, datetime, str, str]:
    """Sort newest-first; missing dates sort last; record_path tiebreaks."""
    parsed = candidate.parsed_date
    if parsed is None:
        # Use a sentinel that sorts AFTER any real date — the negation
        # below (descending order) flips it back to "last".
        return (1, datetime.min, candidate.record_path, candidate.entry.marker_id)
    return (0, parsed, candidate.record_path, candidate.entry.marker_id)


def build_batch(
    vault_path: Path,
    config: DailySyncConfig,
    *,
    start_index: int = 1,
) -> list[AttributionItem]:
    """Sample a batch and return it as :class:`AttributionItem` rows.

    Public surface for the daemon and any future ``/attribution_audit``
    slash command. Returns ``[]`` when the vault has nothing
    unconfirmed (the steady state once Andrew is caught up).

    ``start_index`` (1-based, GLOBAL across Daily Sync sections) lets
    the assembler keep numbering continuous — when email calibration
    rendered 5 items, attribution starts at 6.
    """
    enabled, batch_size, scan_paths = _attribution_settings(config)
    if not enabled or batch_size <= 0:
        return []
    candidates = _read_candidates(vault_path, scan_paths)
    if not candidates:
        return []
    # Sort newest-first. ``_sort_key`` returns a tuple that sorts
    # oldest-first by ``(parsed,)``, so we reverse for newest-first.
    sorted_candidates = sorted(
        candidates,
        key=_sort_key,
        reverse=True,
    )
    # ``reverse=True`` flips the missing-date sentinel too — re-correct
    # by partitioning so dated items lead and undated trail.
    dated = [c for c in sorted_candidates if c.parsed_date is not None]
    undated = [c for c in sorted_candidates if c.parsed_date is None]
    # ``dated`` is currently newest-first; ``undated`` we keep stable
    # by record_path order (deterministic for the same vault state).
    undated.sort(key=lambda c: (c.record_path, c.entry.marker_id))
    ordered = dated + undated
    chosen = ordered[:batch_size]
    return [
        AttributionItem(
            item_number=start_index + i,
            record_path=c.record_path,
            marker_id=c.entry.marker_id,
            agent=c.entry.agent,
            date=c.entry.date,
            section_title=c.entry.section_title,
            reason=c.entry.reason,
            content_preview=c.content_preview,
        )
        for i, c in enumerate(chosen)
    ]


def render_batch(items: list[AttributionItem]) -> str:
    """Render the attribution batch as the section body.

    Format (per spec)::

        ## Attribution audit (5 items)

        6. [salem 2026-04-23 18:44 — note/Marker Smoke Test]
           Section: "Marker Smoke Test"
           Content: "Testing the attribution audit marker. ..."
           Reason: talker conversation turn (session=78a7c5a2)

    Reply hints at the bottom mirror the email-calibration section's
    style so Andrew has one consistent reply grammar.
    """
    if not items:
        # Empty state — intentionally-left-blank principle: a section
        # header that says "nothing to do" beats a missing section
        # because operator visibility is the load-bearing property.
        return (
            "## Attribution audit\n\n"
            "No attribution items pending review.\n"
        )
    plural = "s" if len(items) != 1 else ""
    lines = [f"## Attribution audit ({len(items)} item{plural})", ""]
    for item in items:
        record_label = _short_record_label(item.record_path)
        date_label = _short_date(item.date)
        lines.append(
            f"{item.item_number}. [{item.agent} {date_label} — {record_label}]"
        )
        lines.append(f'   Section: "{item.section_title}"')
        if item.content_preview:
            lines.append(f'   Content: "{item.content_preview}"')
        if item.reason:
            lines.append(f"   Reason: {item.reason}")
        lines.append("")
    lines.append(
        "Reply with `N confirm` to keep, `N reject` to strip the section."
    )
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Section provider entry point + registration
# ---------------------------------------------------------------------------


# Module-level vault-path holder, mirrors ``email_section`` for
# consistency. Daemon sets this once at startup; tests may set it
# directly before invoking the section provider.
_VAULT_PATH_HOLDER: dict[str, Path] = {}


def set_vault_path(vault_path: Path) -> None:
    """Configure the module-level vault path used by the section provider.

    Idempotent — daemon calls once at startup, tests may call repeatedly.
    """
    _VAULT_PATH_HOLDER["path"] = vault_path


def get_vault_path() -> Path | None:
    """Return the currently-configured vault path (None if unset)."""
    return _VAULT_PATH_HOLDER.get("path")


# Holder for the most recent batch so the daemon can persist the
# item ↔ marker mapping after assembly. Mirrors ``email_section`` —
# the assembler signature returns only a string, so per-section
# metadata flows through this side channel.
_LAST_BATCH_HOLDER: dict[str, list[AttributionItem]] = {"items": []}


def consume_last_batch() -> list[AttributionItem]:
    """Return and clear the most recently-built batch.

    Called by the daemon after :func:`assemble_message` so it can
    persist the item ↔ marker mapping into the Daily Sync state file
    under ``last_batch.attribution_items``.
    """
    items = _LAST_BATCH_HOLDER.get("items", [])
    _LAST_BATCH_HOLDER["items"] = []
    return items


def peek_last_batch_count() -> int:
    """Return the count of items in the most-recently-built batch.

    Non-destructive — used by the assembler's ``item_count_after`` hook
    to advance the global ``start_index`` after this provider runs,
    without consuming the batch (the daemon calls ``consume_last_batch``
    afterwards to actually persist the mapping).
    """
    return len(_LAST_BATCH_HOLDER.get("items", []))


def attribution_audit_section(
    config: DailySyncConfig,
    today: date,
    *,
    start_index: int = 1,
) -> str | None:
    """Section provider — builds and renders the attribution-audit batch.

    Registered with priority 25 (between friction's reserved slot 20
    and open-questions' reserved slot 30, AFTER email calibration at
    10). Returns the empty-state header when there's nothing pending,
    NOT ``None`` — the intentionally-left-blank principle is
    load-bearing here. Returns ``None`` only when attribution is
    disabled or the vault path isn't configured.
    """
    enabled, _batch_size, _scan_paths = _attribution_settings(config)
    if not enabled:
        return None
    vault_path = get_vault_path()
    if vault_path is None or not vault_path.is_dir():
        return None
    items = build_batch(vault_path, config, start_index=start_index)
    _LAST_BATCH_HOLDER["items"] = items
    return render_batch(items)


def register() -> None:
    """Idempotent provider registration. Safe to call multiple times.

    Registers at priority 25 — between the friction-queue slot (20,
    reserved) and open-questions slot (30, reserved). Email calibration
    at priority 10 renders first; attribution renders second.
    """
    from . import assembler
    if "attribution_audit" in assembler.registered_providers():
        return
    assembler.register_provider(
        "attribution_audit",
        priority=25,
        provider=attribution_audit_section,
        item_count_after=peek_last_batch_count,
    )


__all__ = [
    "AttributionItem",
    "attribution_audit_section",
    "build_batch",
    "consume_last_batch",
    "get_vault_path",
    "peek_last_batch_count",
    "register",
    "render_batch",
    "set_vault_path",
]
