"""Brief → Feed extractors (Feed Phase A, producer #2).

Thin projections that re-derive the SAME structured data the section renderers
consume (reusing each section module's gather helpers) into FeedItems — the
renderers' markdown is untouched, so the brief render stays byte-identical.

Phase A converts the item-bearing sections with clean structured sources:
health, event, peer_digest here; slot_suggestion (the TodayView projection) is
deferred to its own slice. Evidence is a minimal projection of the section's own
record (not the whole markdown). All extractors are best-effort: any failure
returns ``[]`` — the belt at the call site also swallows, so a feed extractor
can never break the brief.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alfred.feed import FeedItem

_SOURCE_REF = {"producer": "brief"}


def health_feed_items(
    vault_path: str | Path,
    *,
    instance: str,
) -> list[FeedItem]:
    """One ``health`` item per NON-OK tool in the latest BIT record (stable key
    = tool name). All-ok / no record → ``[]`` so reconcile marks stale warns acted."""
    from .health_section import _find_latest_bit_record, _per_tool_lines

    vault = Path(vault_path)
    try:
        record = _find_latest_bit_record(vault)
        if record is None:
            return []
        body = record.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[FeedItem] = []
    for tool, status, detail in _per_tool_lines(body):
        if status == "ok":
            continue
        title = f"Health: {tool} {status.upper()}" + (f" — {detail}" if detail else "")
        out.append(FeedItem.create(
            kind="health",
            stable_key=tool,
            instance=instance,
            title=title,
            evidence={"tool": tool, "status": status, "detail": detail},
            source_ref=dict(_SOURCE_REF, record=str(record)),
        ))
    return out


def event_feed_items(
    upcoming_config: Any,
    vault_path: str | Path,
    today_local,
    *,
    instance: str,
) -> list[FeedItem]:
    """One ``event`` item per upcoming event/task in the window (stable key =
    date_iso + name). Phase A does NOT apply the operator inbox-preference filter
    to the feed projection — that is a render-time concern and the (unrendered)
    feed being a superset is harmless."""
    from .upcoming_events import _collect_items

    max_days = int(getattr(upcoming_config, "max_days_ahead", 30) or 30)
    try:
        items, _filtered = _collect_items(Path(vault_path), today_local, max_days)
    except Exception:  # noqa: BLE001 — extractor is best-effort; belt also swallows
        return []
    out: list[FeedItem] = []
    for ev in items:
        out.append(FeedItem.create(
            kind="event",
            stable_key=f"{ev.date_iso}|{ev.name}",
            instance=instance,
            title=f"{ev.date_iso}: {ev.name}",
            evidence={
                "date_iso": ev.date_iso,
                "name": ev.name,
                "rec_type": ev.rec_type,
                "time_display": ev.time_display,
            },
            source_ref=dict(_SOURCE_REF),
        ))
    return out


def peer_digest_feed_items(
    vault_path: str | Path,
    today_iso: str,
    *,
    instance: str,
) -> list[FeedItem]:
    """One ``peer_digest`` item per peer digest record today (stable key = peer +
    date; fyi)."""
    from .peer_digests import _scan_peer_digests

    try:
        records = _scan_peer_digests(Path(vault_path), today_iso)
    except Exception:  # noqa: BLE001 — best-effort; belt also swallows
        return []
    out: list[FeedItem] = []
    for rec in records:
        peer = (getattr(rec, "peer", "") or "").strip()
        if not peer:
            continue
        out.append(FeedItem.create(
            kind="peer_digest",
            stable_key=f"{peer}|{today_iso}",
            instance=instance,
            title=f"Peer digest: {peer}",
            evidence={"peer": peer, "date": today_iso},
            source_ref=dict(_SOURCE_REF),
        ))
    return out
