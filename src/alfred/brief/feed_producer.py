"""Brief → Feed extractors (Feed Phase A, producer #2).

Thin projections that re-derive the SAME structured data the section renderers
consume (reusing each section module's gather helpers) into FeedItems — the
renderers' markdown is untouched, so the brief render stays byte-identical.

Phase A converts the item-bearing sections with clean structured sources:
health, slot_suggestion, event, peer_digest. Evidence is a minimal projection of
the section's own record (not the whole markdown).

**Failure ≠ emptiness.** Each extractor returns ``list[FeedItem] | None``:
  * a LIST (possibly ``[]``) is a genuine read — ``[]`` means "the section has
    nothing open right now", and the caller reconciles it (so cleared items go
    ``acted``);
  * ``None`` is a FAILURE (the source raised / couldn't be read) — the caller
    must NOT reconcile, or an extractor blip would spuriously mass-``acted`` a
    whole kind. The belt logs the failure either way; on ``None`` the kind's feed
    state is left untouched this run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alfred.feed import FeedItem

_SOURCE_REF = {"producer": "brief"}

# The ``source`` enum values a TierEntry carries when it is AUTO-surfaced (not
# operator-committed) — mirrors the auto/self-care sources assigned in
# ``tier.compute.compute_today_view``. A slot item is an accept CANDIDATE (Phase
# C slice 2) iff its source is one of these AND it is not yet confirmed: a
# confirmed T1 preserves its original auto source but flips ``confirmed=True``,
# and an operator-committed T2/T3 carries a non-auto source ("operator"). Pinned
# by ``tests/feed/test_brief_feed_slot_suggestion.py`` against the compute
# assignments — if compute adds/renames an auto source, update BOTH in lockstep.
_AUTO_CANDIDATE_SOURCES = frozenset({
    "auto-due",
    "auto-escalate",
    "auto-due-routine",
    "auto-surface-routine",
    "auto-cadence-routine",
    "self-care",
})


def _slot_is_candidate(source: str, confirmed: Any) -> bool:
    """Whether a slot item is an accept CANDIDATE (auto-surfaced, not yet
    committed to today's plan). Candidate = the entry's ``source`` is an
    auto/self-care source AND it is not confirmed (``confirmed is not True`` —
    covers T1's explicit ``False`` and T2/T3's ``None``). A committed item
    (operator-added, or a confirmed auto-T1) is NOT a candidate → the router
    refuses ``accept`` on it (the provenance guard)."""
    return source in _AUTO_CANDIDATE_SOURCES and confirmed is not True


def health_feed_items(
    vault_path: str | Path,
    *,
    instance: str,
) -> list[FeedItem] | None:
    """One ``health`` item per NON-OK tool in the latest BIT record (stable key =
    tool name). All-ok (record present, every tool ok) → ``[]`` so reconcile marks
    stale warns acted. NO record / read error → ``None`` (can't confirm health;
    don't mass-``acted`` on missing data)."""
    from .health_section import _find_latest_bit_record, _per_tool_lines

    vault = Path(vault_path)
    try:
        record = _find_latest_bit_record(vault)
        if record is None:
            return None  # no BIT data at all → don't reconcile (would mass-act)
        body = record.read_text(encoding="utf-8")
    except OSError:
        return None
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
) -> list[FeedItem] | None:
    """One ``event`` item per upcoming event/task in the window (stable key =
    date_iso + name). Applies the SAME operator action-preference filter the
    render applies (``load_active_preferences`` → ``_collect_items``), so the feed
    projection matches what the operator sees — no superset. Read failure →
    ``None`` (don't reconcile)."""
    from alfred.preferences.loader import load_active_preferences

    from .upcoming_events import _collect_items

    max_days = int(getattr(upcoming_config, "max_days_ahead", 30) or 30)
    vault = Path(vault_path)
    try:
        try:
            prefs = load_active_preferences(vault, shape="action")
        except Exception:  # noqa: BLE001 — mirror render: pref-load failure → no filter
            prefs = []
        items, _filtered = _collect_items(vault, today_local, max_days, prefs)
    except Exception:  # noqa: BLE001 — source failure → don't reconcile (belt also logs)
        return None
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


def _slot_stable_key(entry: Any) -> str:
    """Durable identity for a tier-lane entry, per the step-2 identity table:
    task → path (wikilink target); routine item → (record, text); free-text
    T3 → item text. Origin-prefixed so the three spaces can't collide."""
    origin = getattr(entry, "origin", "")
    if origin == "routine_item" and entry.routine_record and entry.item_text:
        return f"routine:{entry.routine_record}::{entry.item_text}"
    if origin == "task" and entry.path:
        return f"task:{entry.path}"
    name = getattr(entry, "name", "") or ""
    return f"text:{name}" if name else ""


def slot_suggestion_feed_items(
    vault_path: str | Path,
    now,
    tier_defaults: Any,
    *,
    instance: str,
) -> list[FeedItem] | None:
    """One ``slot_suggestion`` item per T1/T2/T3 tier-lane entry in the TodayView
    projection (auto-T1 task + routine candidates, T2 auto-surfaced, T3
    suggestions). The feed reads the SAME ``compute_today_view`` projection the
    brief's tier section renders, so it never diverges from what the operator
    sees. Empty day → ``[]``; projection failure → ``None`` (don't reconcile).

    Each item carries ``evidence.done`` — the completion state the ring reads to
    show a green/struck segment. It is computed by the tier layer's OWN
    ``entry_is_done`` predicate over ``build_done_caches`` (identity-shared with
    the daily-goal count), NEVER a re-derived doneness — a done/undone
    disagreement between the ring and the brief is the exact gaslighting Phase C
    slice 1 closes. ``compute_today_view`` still SKIPS auto-surfaced routine
    items once completed-this-cycle; those don't vanish from the board because
    the feed store retains them as ``acted`` after a board completion — so the
    ring's greenness is ``state == acted OR evidence.done`` (see the board
    action router)."""
    from alfred.tier.compute import (
        build_done_caches,
        compute_today_view,
        entry_is_done,
    )

    try:
        view = compute_today_view(Path(vault_path), now, tier_defaults)
        task_fm_by_name, completion_by_record = build_done_caches(Path(vault_path))
    except Exception:  # noqa: BLE001 — source failure → don't reconcile (belt also logs)
        return None
    today = now.date()
    out: list[FeedItem] = []
    for entry in (*view.t1, *view.t2, *view.t3):
        key = _slot_stable_key(entry)
        if not key:
            continue
        reason = getattr(entry, "surface_reason", None) or getattr(entry, "source", "") or ""
        title = f"T{entry.tier}: {entry.name}" + (f" — {reason}" if reason else "")
        done = entry_is_done(
            entry,
            task_fm_by_name=task_fm_by_name,
            completion_by_record=completion_by_record,
            today=today,
        )
        source = getattr(entry, "source", "") or ""
        confirmed = getattr(entry, "confirmed", None)
        out.append(FeedItem.create(
            kind="slot_suggestion",
            stable_key=key,
            instance=instance,
            title=title,
            evidence={
                "tier": entry.tier,
                "origin": getattr(entry, "origin", ""),
                "name": entry.name,
                "path": getattr(entry, "path", ""),
                "due_iso": getattr(entry, "due_iso", None),
                "surface_reason": getattr(entry, "surface_reason", None),
                "source": source,
                # Phase C slice 2 (board ACCEPT path): ``confirmed`` verbatim
                # (T1-only; None for T2/T3) + the derived ``candidate`` flag the
                # router's accept provenance-guard reads. Evidence-only — the
                # brief render never reads evidence, so byte-parity holds (the
                # C1 ``done`` precedent).
                "confirmed": confirmed,
                "candidate": _slot_is_candidate(source, confirmed),
                "routine_record": getattr(entry, "routine_record", None),
                "item_text": getattr(entry, "item_text", None),
                "done": done,
            },
            source_ref=dict(_SOURCE_REF),
        ))
    return out


# The peer-digest card renders ``evidence.body`` as readable prose, so the
# extractor must EMIT the digest body (the record already holds it — this is
# emission, not new gathering). Bounded because the feed store is append-only
# JSONL folded on every load: an unbounded multi-KB digest would bloat every
# fold. Verbatim under the cap; over it, hard-truncate + an honest marker + a
# machine-readable ``truncated`` flag so the card can say "there's more."
_PEER_DIGEST_BODY_MAX = 4000
_PEER_DIGEST_TRUNCATION_MARKER = "\n\n…[truncated]"


def _bounded_digest_body(body: str) -> tuple[str, bool]:
    """Return ``(emitted_body, truncated)`` — the body verbatim when within
    :data:`_PEER_DIGEST_BODY_MAX`, else its first ``_PEER_DIGEST_BODY_MAX``
    chars plus :data:`_PEER_DIGEST_TRUNCATION_MARKER`."""
    if len(body) <= _PEER_DIGEST_BODY_MAX:
        return body, False
    return body[:_PEER_DIGEST_BODY_MAX] + _PEER_DIGEST_TRUNCATION_MARKER, True


def peer_digest_feed_items(
    vault_path: str | Path,
    today_iso: str,
    *,
    instance: str,
) -> list[FeedItem] | None:
    """One ``peer_digest`` item per peer digest record today (stable key = peer +
    date; fyi). Evidence carries the digest ``body`` (bounded — see
    :func:`_bounded_digest_body`) plus a ``truncated`` flag, so the card renders
    readable content instead of asking for an ack on {peer, date} alone. Scan
    failure → ``None`` (don't reconcile). The stable key is UNCHANGED (peer +
    date) — adding body must never shift an id, so an ack'd digest stays acked."""
    from .peer_digests import _scan_peer_digests

    try:
        records = _scan_peer_digests(Path(vault_path), today_iso)
    except Exception:  # noqa: BLE001 — source failure → don't reconcile (belt also logs)
        return None
    out: list[FeedItem] = []
    for rec in records:
        peer = (getattr(rec, "peer", "") or "").strip()
        if not peer:
            continue
        body, truncated = _bounded_digest_body(getattr(rec, "body", "") or "")
        out.append(FeedItem.create(
            kind="peer_digest",
            stable_key=f"{peer}|{today_iso}",
            instance=instance,
            title=f"Peer digest: {peer}",
            evidence={
                "peer": peer,
                "date": today_iso,
                "body": body,
                "truncated": truncated,
            },
            source_ref=dict(_SOURCE_REF),
        ))
    return out
