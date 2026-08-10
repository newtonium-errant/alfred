"""Attribution markers for agent-inferred vault content.

Closes the audit gap surfaced 2026-04-22: when an agent (Salem, distiller,
KAL-LE, etc.) writes inferred content into a user-authoritative document,
nothing today distinguishes agent-inferred prose from Andrew-typed prose.
This module provides the marker primitives the write paths use to tag
inferred sections so a future Daily Sync confirmation flow can surface them
to Andrew for explicit confirmation or rejection.

This is the *foundation* (Phase 1, c1) — pure primitives and tests, no
write-path wiring. ``c2`` wires the talker's vault_create/vault_edit
callsites; ``c4`` wires curator/distiller/janitor/surveyor/instructor;
``c3`` does a retroactive sweep over existing vault prose; Phase 2 builds
the Daily Sync confirm/reject flow on top of these primitives.

## Marker schema

Two layers, intentionally — one human-readable in body, one machine-queryable
in frontmatter:

1. **HTML BEGIN/END pair in body** — visible to a human reading the markdown
   in Obsidian, and structurally precise enough that confirm/reject flows can
   identify the exact span to flip or strip:

   ```markdown
   <!-- BEGIN_INFERRED marker_id="inf-20260423-salem-a1b2c3" -->
   ## Sender-Specific Overrides
   ...inferred content...
   <!-- END_INFERRED marker_id="inf-20260423-salem-a1b2c3" -->
   ```

2. **``attribution_audit`` list in frontmatter** — structured entries the
   Daily Sync surfacer can iterate over without parsing markdown:

   ```yaml
   attribution_audit:
     - marker_id: inf-20260423-salem-a1b2c3
       agent: salem
       date: 2026-04-23T17:42:00Z
       section_title: Sender-Specific Overrides
       reason: conversation turn
       confirmed_by_andrew: false
       confirmed_at: null
   ```

## Marker ID shape

``inf-{YYYYMMDD}-{agent}-{6-char-hash}`` where the hash is taken from
``agent + date + content[:200]``. Deterministic on content, so re-running
the wrapper on the same input produces the same ID — supports idempotency
without a separate "already wrapped?" lookup.

## Idempotency stance

If ``with_inferred_marker`` is called on body content that already starts
with a ``BEGIN_INFERRED`` marker, the function returns the body unchanged
plus the *existing* audit entry. We do NOT update the date — the marker is
"this content was inferred at this time", not "this content was last
touched at this time". A separate edit that materially changes the wrapped
content will produce a different content hash, hence a different marker_id,
hence a new audit entry — which is the right outcome.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import structlog

log = structlog.get_logger(__name__)


# Regex for parsing existing markers out of body content. Tolerates either
# single or double quotes around the ID (YAML-stable round-trips can rewrite
# quoting); the marker_id pattern itself is the deterministic shape we
# emit — letters, digits, dashes only.
_BEGIN_RE = re.compile(
    r"<!--\s*BEGIN_INFERRED\s+marker_id=[\"']([\w-]+)[\"']\s*-->"
)
_END_RE_TEMPLATE = (
    r"<!--\s*END_INFERRED\s+marker_id=[\"']{marker_id}[\"']\s*-->"
)


# --- how an entry came to be confirmed (#63a) -------------------------------
# Three values, deliberately NOT merged. The operator ruling is explicit that
# collapsing them is irreversible:
#
#   operator    — Andrew explicitly confirmed it (deck, reply grammar, or after
#                 resolving his own contest). The strongest signal in the trail.
#   timeout_24h — it had its 24 hours under the current rules and nobody
#                 objected. A real, if weak, endorsement.
#   backfill    — it was swept in at deploy, having existed BEFORE the
#                 auto-confirm policy did. It was never offered under these
#                 rules, so it endorses nothing.
#
# The distinction is load-bearing for weighing contests later: counting backfill
# as timeout would inflate the apparent attribution-quality signal by exactly
# the size of whatever historical backlog happened to be sitting there at
# deploy time.
CONFIRMED_VIA_OPERATOR = "operator"
CONFIRMED_VIA_TIMEOUT = "timeout_24h"
CONFIRMED_VIA_BACKFILL = "backfill"
CONFIRMED_VIA_VALUES = frozenset({
    CONFIRMED_VIA_OPERATOR,
    CONFIRMED_VIA_TIMEOUT,
    CONFIRMED_VIA_BACKFILL,
})


@dataclass
class AuditEntry:
    """One entry in a record's ``attribution_audit`` frontmatter list.

    Frontmatter dict round-trips via ``asdict`` / ``from_dict`` — kept simple
    so YAML serialisation is just ``yaml.safe_dump([asdict(e), ...])`` with
    no custom representer needed.
    """

    marker_id: str
    agent: str
    date: str  # ISO 8601 UTC, e.g. "2026-04-23T17:42:00+00:00"
    section_title: str
    reason: str
    confirmed_by_andrew: bool = False
    confirmed_at: str | None = None
    # #63a. ``confirmed_via`` names WHICH path confirmed this entry — one of
    # CONFIRMED_VIA_*. ``None`` on an unconfirmed entry, and also on entries
    # confirmed before #63a existed (we cannot retroactively know how those
    # were confirmed, and guessing would be a fabricated audit trail).
    confirmed_via: str | None = None
    # ``contested`` is the operator saying the inference is wrong. It suppresses
    # auto-confirm permanently — the 24h clock never runs on a contested entry —
    # and re-tiers the card back to needs-you until he resolves it.
    contested: bool = False
    contested_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "AuditEntry":
        """Build an entry from a frontmatter dict.

        Tolerates extra keys (forward-compat) but requires the core five.
        Raises ``ValueError`` on missing required fields — callers (e.g.
        ``parse_audit_entries``) catch and skip with a log line.

        Schema tolerance runs BOTH directions, which is what lets #63a's new
        fields land on a live vault: records written before this build carry no
        ``confirmed_via`` / ``contested`` keys and load with the defaults, and
        records written by a NEWER build carrying fields we don't know about
        load fine too (unknown keys are ignored rather than splatted in).
        """
        required = ("marker_id", "agent", "date", "section_title", "reason")
        missing = [k for k in required if k not in raw]
        if missing:
            raise ValueError(f"AuditEntry missing required keys: {missing}")
        via = raw.get("confirmed_via")
        return cls(
            marker_id=str(raw["marker_id"]),
            agent=str(raw["agent"]),
            date=str(raw["date"]),
            section_title=str(raw["section_title"]),
            reason=str(raw["reason"]),
            confirmed_by_andrew=bool(raw.get("confirmed_by_andrew", False)),
            confirmed_at=raw.get("confirmed_at"),
            confirmed_via=str(via) if via else None,
            contested=bool(raw.get("contested", False)),
            contested_at=raw.get("contested_at"),
        )


def _now_utc() -> datetime:
    """Return aware UTC ``datetime``. Wrapped so tests can monkeypatch."""
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    """Serialise ``dt`` to ISO-8601 UTC.

    If ``dt`` is naive, assume UTC. Always emits a trailing offset so the
    round-trip parser doesn't have to guess.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def make_marker_id(
    agent: str,
    content: str,
    date: datetime | None = None,
) -> str:
    """Build a deterministic marker ID from agent + date + content.

    Shape: ``inf-{YYYYMMDD}-{agent}-{6-char-hash}``.

    The hash covers ``agent + iso_date + content[:200]`` — first 200 chars
    of content is enough to differentiate distinct sections without making
    the ID sensitive to trailing whitespace tweaks. Re-running on the same
    content yields the same ID, which is the basis for idempotency in
    ``with_inferred_marker``.
    """
    when = date or _now_utc()
    iso_date = _iso(when)
    raw = f"{agent}|{iso_date}|{content[:200]}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]
    return f"inf-{when.strftime('%Y%m%d')}-{agent}-{digest}"


def _wrap(body: str, marker_id: str) -> str:
    """Surround ``body`` with BEGIN/END comment lines for ``marker_id``."""
    body = body.strip("\n")
    return (
        f"<!-- BEGIN_INFERRED marker_id=\"{marker_id}\" -->\n"
        f"{body}\n"
        f"<!-- END_INFERRED marker_id=\"{marker_id}\" -->"
    )


def _existing_marker_id(body: str) -> str | None:
    """If ``body`` opens with a BEGIN_INFERRED marker, return its ID."""
    head = body.lstrip()
    m = _BEGIN_RE.match(head)
    return m.group(1) if m else None


def with_inferred_marker(
    body: str,
    section_title: str,
    agent: str,
    reason: str,
    date: datetime | None = None,
) -> tuple[str, AuditEntry]:
    """Wrap ``body`` in BEGIN_INFERRED/END_INFERRED markers and return the
    wrapped body plus the matching ``AuditEntry`` to add to frontmatter.

    Idempotent: if ``body`` already opens with a BEGIN_INFERRED marker, the
    function leaves the body untouched and returns the existing marker_id
    plus a fresh ``AuditEntry`` carrying that ID. The caller can pass that
    entry to ``append_audit_entry`` which is itself idempotent on marker_id
    (replace, not duplicate).
    """
    when = date or _now_utc()

    existing_id = _existing_marker_id(body)
    if existing_id is not None:
        # Use the existing marker_id but build a *fresh* entry from current
        # call args (agent/reason/date/section_title may have shifted across
        # callers). append_audit_entry will replace any prior entry with
        # the same marker_id.
        entry = AuditEntry(
            marker_id=existing_id,
            agent=agent,
            date=_iso(when),
            section_title=section_title,
            reason=reason,
        )
        return body, entry

    marker_id = make_marker_id(agent, body, date=when)
    wrapped = _wrap(body, marker_id)
    entry = AuditEntry(
        marker_id=marker_id,
        agent=agent,
        date=_iso(when),
        section_title=section_title,
        reason=reason,
    )
    return wrapped, entry


def append_audit_entry(frontmatter: dict, entry: AuditEntry) -> dict:
    """Add ``entry`` to ``frontmatter['attribution_audit']``.

    Creates the list if absent. Idempotent on ``marker_id``: if an entry
    with the same ID is already present, it is *replaced* by the new one
    (so rerunning a write doesn't duplicate, and a confirm-then-write-again
    cycle keeps the most recent entry shape).

    Returns the same frontmatter dict (mutated in place) for chaining
    convenience.
    """
    existing = frontmatter.get("attribution_audit")
    if not isinstance(existing, list):
        existing = []
    new_list: list[dict] = []
    replaced = False
    for raw in existing:
        if isinstance(raw, dict) and raw.get("marker_id") == entry.marker_id:
            new_list.append(entry.to_dict())
            replaced = True
        else:
            new_list.append(raw)
    if not replaced:
        new_list.append(entry.to_dict())
    frontmatter["attribution_audit"] = new_list
    return frontmatter


def parse_audit_entries(frontmatter: dict) -> list[AuditEntry]:
    """Parse ``frontmatter['attribution_audit']`` into a list of ``AuditEntry``.

    Tolerant of malformed entries: missing required keys or wrong types are
    logged and skipped, not raised. An empty / absent list returns ``[]``.
    """
    raw = frontmatter.get("attribution_audit")
    if not isinstance(raw, list):
        return []
    out: list[AuditEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            log.info(
                "attribution.audit_entry_malformed",
                reason="not_a_dict",
                value_type=type(item).__name__,
            )
            continue
        try:
            out.append(AuditEntry.from_dict(item))
        except ValueError as exc:
            log.info(
                "attribution.audit_entry_malformed",
                reason="missing_keys",
                error=str(exc),
            )
            continue
    return out


def find_marker_bounds(body: str, marker_id: str) -> tuple[int, int] | None:
    """Return ``(begin_line, end_line)`` (0-indexed, inclusive) of the
    BEGIN/END marker pair for ``marker_id`` in ``body``, or ``None`` if not
    found.

    Used by Phase 2 confirm/reject flows to identify the span to operate on.
    Lines are returned inclusive on both ends — the caller wanting to strip
    the marker but keep the content reads ``lines[begin+1:end]``.
    """
    end_re = re.compile(_END_RE_TEMPLATE.format(marker_id=re.escape(marker_id)))
    begin_idx: int | None = None
    for i, line in enumerate(body.splitlines()):
        if begin_idx is None:
            m = _BEGIN_RE.search(line)
            if m and m.group(1) == marker_id:
                begin_idx = i
        else:
            if end_re.search(line):
                return (begin_idx, i)
    return None


def confirm_marker(
    frontmatter: dict,
    marker_id: str,
    by: str = "andrew",
    at: datetime | None = None,
    via: str = CONFIRMED_VIA_OPERATOR,
) -> dict:
    """Flip the audit entry for ``marker_id`` to confirmed.

    Sets ``confirmed_by_andrew=True``, ``confirmed_at`` to the timestamp, and
    ``confirmed_via`` to ``via``. Returns frontmatter (mutated in place). If no
    matching entry is found, returns frontmatter unchanged and logs a warning.

    ``via`` defaults to :data:`CONFIRMED_VIA_OPERATOR` because every call site
    that predates #63a is an explicit operator confirm — the default keeps those
    honest without touching them. The sweep passes ``timeout_24h`` /
    ``backfill`` explicitly.

    ``confirmed_by_andrew`` stays True on the machine paths even though Andrew
    didn't act, because it is the field the whole read path filters on
    ("unconfirmed" == needs review) and splitting it would fork that predicate
    across every consumer. ``confirmed_via`` is what carries the truth about who
    or what did it, which is exactly why the ruling insists the three values
    stay distinct.

    ``by`` is currently informational — the entry only stores ``confirmed_at``
    and a boolean — but accepting it now means Phase 2's Daily Sync flow
    doesn't need a signature change when we add per-confirmer attribution
    (e.g. KAL-LE confirming on Andrew's behalf for coding rules).
    """
    when = at or _now_utc()
    entries = frontmatter.get("attribution_audit")
    if not isinstance(entries, list):
        log.info("attribution.confirm.no_audit_list", marker_id=marker_id)
        return frontmatter
    found = False
    for raw in entries:
        if isinstance(raw, dict) and raw.get("marker_id") == marker_id:
            raw["confirmed_by_andrew"] = True
            raw["confirmed_at"] = _iso(when)
            raw["confirmed_via"] = via
            found = True
            break
    if not found:
        log.warning(
            "attribution.confirm.marker_not_found",
            marker_id=marker_id,
            confirmer=by,
        )
    return frontmatter


def contest_marker(
    frontmatter: dict,
    marker_id: str,
    at: datetime | None = None,
) -> bool:
    """Mark the audit entry for ``marker_id`` as contested.

    Returns ``True`` when the entry was found and changed, ``False`` otherwise
    (already contested, or no such entry) — the caller reports the WRITE, not
    the intent, so a no-op can't be shown to the operator as a recorded contest.

    Contesting is NOT rejecting: the marked section stays in the body and the
    audit entry stays in frontmatter. It says "don't decide this for me" — it
    stops the 24h auto-confirm clock permanently and sends the card back to the
    needs-you tier so the operator can confirm or reject it deliberately.
    """
    when = at or _now_utc()
    entries = frontmatter.get("attribution_audit")
    if not isinstance(entries, list):
        log.info("attribution.contest.no_audit_list", marker_id=marker_id)
        return False
    for raw in entries:
        if isinstance(raw, dict) and raw.get("marker_id") == marker_id:
            if raw.get("contested"):
                log.info(
                    "attribution.contest.idempotent_noop", marker_id=marker_id,
                )
                return False
            raw["contested"] = True
            raw["contested_at"] = _iso(when)
            return True
    log.warning("attribution.contest.marker_not_found", marker_id=marker_id)
    return False


def reject_marker(
    body: str,
    frontmatter: dict,
    marker_id: str,
) -> tuple[str, dict]:
    """Strip the marked section from ``body`` AND remove the audit entry.

    Returns ``(new_body, new_frontmatter)``. If the marker isn't found in
    body or frontmatter, returns the inputs as-is and logs a warning — the
    Daily Sync flow shouldn't crash on a stale reject.
    """
    bounds = find_marker_bounds(body, marker_id)
    new_body = body
    if bounds is not None:
        begin, end = bounds
        lines = body.splitlines(keepends=True)
        # Drop begin..end inclusive. Preserve the trailing newline character
        # of the line after `end` if there was one (we just stitch the
        # surrounding text back together).
        new_lines = lines[:begin] + lines[end + 1:]
        new_body = "".join(new_lines)
    else:
        log.warning("attribution.reject.marker_not_in_body", marker_id=marker_id)

    entries = frontmatter.get("attribution_audit")
    if isinstance(entries, list):
        before = len(entries)
        frontmatter["attribution_audit"] = [
            e for e in entries
            if not (isinstance(e, dict) and e.get("marker_id") == marker_id)
        ]
        if len(frontmatter["attribution_audit"]) == before:
            log.warning(
                "attribution.reject.entry_not_found",
                marker_id=marker_id,
            )
    else:
        log.warning(
            "attribution.reject.no_audit_list",
            marker_id=marker_id,
        )

    return new_body, frontmatter


__all__ = [
    "CONFIRMED_VIA_BACKFILL",
    "CONFIRMED_VIA_OPERATOR",
    "CONFIRMED_VIA_TIMEOUT",
    "CONFIRMED_VIA_VALUES",
    "AuditEntry",
    "make_marker_id",
    "with_inferred_marker",
    "append_audit_entry",
    "parse_audit_entries",
    "find_marker_bounds",
    "confirm_marker",
    "contest_marker",
    "reject_marker",
]
