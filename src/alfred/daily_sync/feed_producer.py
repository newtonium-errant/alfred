"""Daily-sync → Feed translation (Feed Phase A, producer #1).

Called by ``daemon.fire_once`` AFTER the ``last_batch`` payload is persisted, so
it can never affect the assembled body or the batch. Each family reconcile goes
through the belt (``try_feed_reconcile``), so a feed failure can never break the
fire. Evidence is the item's existing ``to_dict()`` VERBATIM (Phase A does not
reshape). Reconcile semantics give decided-detection for free: an item open in
the previous fire's feed state but absent from this fire's open set is marked
``acted`` — no decided-store reads.

Every family is reconciled EVERY fire, even when empty: an empty family this fire
means the queue was cleared, so its previously-open items become ``acted``.
"""

from __future__ import annotations

from typing import Any, Callable

from alfred.feed import FeedItem, FeedStore, try_feed_reconcile
from alfred.feed.model import ATTENTION_NEEDS_YOU, MODE_DECIDE

_SOURCE_REF = {"producer": "daily_sync"}


def _s(d: dict[str, Any], *keys: str) -> str:
    """First non-empty string value among ``keys`` (stable-key extraction)."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


# --- per-family stable-key + title (evidence is the raw to_dict) -------------


def _email_key(d: dict[str, Any]) -> str:
    # Cluster head is the stable identity for an email-tier item (the batch
    # groups near-identical records under a head path).
    cluster = d.get("cluster_record_paths")
    if isinstance(cluster, list) and cluster and isinstance(cluster[0], str) and cluster[0].strip():
        return cluster[0].strip()
    return _s(d, "record_path")


# email_section injects the literal "(unknown)"/"unknown" placeholder when an
# email has no resolvable sender (see email_section.py:181, its own no-sender
# sentinel set); an empty evidence sender is the same signal. Kept local rather
# than imported so the title builder carries no cross-module dependency — the
# sentinel is a stable, human-facing placeholder.
_SENDER_ABSENT = frozenset({"(unknown)", "unknown"})


def _email_title(d: dict[str, Any]) -> str:
    # Drop the sender segment entirely when the sender is absent — otherwise the
    # card title read "Email tier: (unknown) — subject" (#28). Subject-only when
    # there's no real sender to name.
    subject = _s(d, "subject") or "(no subject)"
    sender = _s(d, "sender")
    if not sender or sender.lower() in _SENDER_ABSENT:
        return f"Email tier: {subject}"
    return f"Email tier: {sender} — {subject}"


def _attr_key(d: dict[str, Any]) -> str:
    rp, mid = _s(d, "record_path"), _s(d, "marker_id")
    return f"{rp}|{mid}" if rp and mid else ""


def _attr_title(d: dict[str, Any]) -> str:
    return f"Attribution: {_s(d, 'record_path') or 'record'}"


def _proposal_title(d: dict[str, Any]) -> str:
    label = f"{_s(d, 'record_type') or 'record'} {_s(d, 'name')}".strip()
    return f"Proposal: {label}"


def _pending_title(d: dict[str, Any]) -> str:
    return f"Pending: {_s(d, 'category') or _s(d, 'context') or 'item'}"


def _routine_match_key(d: dict[str, Any]) -> str:
    q, r = _s(d, "query"), _s(d, "record")
    return f"{q}|{r}" if q and r else ""


def _routine_match_title(d: dict[str, Any]) -> str:
    return f"Routine match: {_s(d, 'query')} → {_s(d, 'matched_to') or '?'}"


def _radar_title(d: dict[str, Any]) -> str:
    return f"Radar: {_s(d, 'record_type') or _s(d, 'record_path') or 'item'}"


def _friction_title(d: dict[str, Any]) -> str:
    return f"Friction: {_s(d, 'event_id') or 'event'}"


# kind → (stable_key_fn, title_fn). The batch-list argument name maps 1:1.
_FAMILIES: dict[str, tuple[Callable[[dict], str], Callable[[dict], str]]] = {
    "email_tier": (_email_key, _email_title),
    "attribution": (_attr_key, _attr_title),
    "proposal": (lambda d: _s(d, "correlation_id"), _proposal_title),
    "pending": (lambda d: _s(d, "id"), _pending_title),
    "routine_match": (_routine_match_key, _routine_match_title),
    "radar": (lambda d: _s(d, "record_path", "event_id"), _radar_title),
    "friction": (lambda d: _s(d, "event_id", "record_path"), _friction_title),
}


def _as_dict(item: Any) -> dict[str, Any]:
    if hasattr(item, "to_dict"):
        return item.to_dict()
    return dict(item) if isinstance(item, dict) else {}


# --- per-ITEM tier overrides (#63a) ------------------------------------------
# KIND_DEFAULTS answers "what tier is this kind"; this answers "…except for this
# particular item". Only attribution needs one today: the kind is FYI, but an
# entry the operator CONTESTED is a live question and belongs back in needs-you.
#
# Deriving the promotion from the item's own evidence on every emit — rather
# than writing the tier once when the contest lands — is what makes the revert
# durable: reconcile re-upserts every open item each fire, so a one-off store
# write would be flattened back to FYI by the next sync and the contested card
# would quietly rejoin the glance pile.
#
# #72 item (c)/2, NOT YET BUILT — this is the function the approved demotion
# override plugs into, and two decisions are recorded here.
#
# WHERE IT GOES. An approved demotion returns attribution cards to needs-you
# WHOLESALE, not per item. So it belongs above the ``contested`` check as an
# early return, and KIND_DEFAULTS stays the code default — the override is a
# persisted operator decision layered over it, never an edit to the dict.
# Re-derive it on every emit, exactly as ``contested`` is re-derived: reconcile
# re-upserts every open item each fire, so a tier written once into the store
# would be flattened back to FYI by the next sync.
#
# REVERSIBILITY, v1. A manual escape hatch is sufficient — a CLI subcommand (or
# editing the one persisted value) to clear the override and return the kind to
# its KIND_DEFAULTS tier. A second propose-flow for re-demotion is NOT built:
# it is speculative until an operator actually asks to undo one, and the
# demotion direction is the safe one (more review, not less), so a stuck
# override over-asks rather than silently under-asks. The state must still be
# schema-tolerant and instance-scoped per the CLAUDE.md state rules, because
# the escape hatch reads it too.
def _attribution_tier(d: dict[str, Any]) -> tuple[str, str] | None:
    if d.get("contested"):
        return (MODE_DECIDE, ATTENTION_NEEDS_YOU)
    return None


_TIER_OVERRIDES: dict[str, Callable[[dict], tuple[str, str] | None]] = {
    "attribution": _attribution_tier,
}


def build_feed_items(kind: str, raw_items: list[Any] | None, instance: str) -> list[FeedItem]:
    """Translate one batch family's raw items into FeedItems (evidence verbatim)."""
    key_fn, title_fn = _FAMILIES[kind]
    override_fn = _TIER_OVERRIDES.get(kind)
    out: list[FeedItem] = []
    for item in raw_items or []:
        d = _as_dict(item)
        stable = key_fn(d)
        if not stable:
            continue  # can't stably key it — skip rather than mint an unstable id
        override = override_fn(d) if override_fn else None
        mode, attention = override if override else (None, None)
        out.append(FeedItem.create(
            kind=kind,
            stable_key=stable,
            instance=instance,
            title=title_fn(d),
            evidence=d,
            mode=mode,
            attention=attention,
            source_ref=dict(_SOURCE_REF),
        ))
    return out


def emit_sync_feed(
    store: FeedStore,
    instance: str,
    *,
    email_items: list[Any] | None = None,
    attribution_items: list[Any] | None = None,
    proposal_items: list[Any] | None = None,
    pending_items: list[Any] | None = None,
    routine_match_items: list[Any] | None = None,
    radar_items: list[Any] | None = None,
    friction_items: list[Any] | None = None,
) -> None:
    """Reconcile every daily-sync family into the feed store. Belt-guarded per
    family; reconciled every fire (empty family → prior open items go acted)."""
    by_family = {
        "email_tier": email_items,
        "attribution": attribution_items,
        "proposal": proposal_items,
        "pending": pending_items,
        "routine_match": routine_match_items,
        "radar": radar_items,
        "friction": friction_items,
    }
    for kind in _FAMILIES:
        feed_items = build_feed_items(kind, by_family[kind], instance)
        try_feed_reconcile(store, kind, feed_items)
