"""FeedItem — the one attention-tiered model brief + sync both project into.

Phase A is foundation-invisible: this model + its store accumulate truth while
the operator sees zero change. Phase B/C render it (Awareness feed, Decide deck,
briefing player); Phase D adds the attention *learning* loop. Here the per-kind
mode/attention are STATIC defaults only.

Identity rule (load-bearing): ``id = f"{kind}:{stable_key}"`` where the stable
key is the OWNING STORE's durable key for the thing (record_path, correlation_id,
trp- proposal id, uuid, a (query, record) tuple, …) — **NEVER a per-fire render
ordinal**. The feed is a projection + action router over authoritative stores;
a render ordinal would make the same underlying decision a different feed item on
every fire, breaking dedup, decided-detection, and the attention policy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

# --- closed vocabularies -----------------------------------------------------
MODE_DECIDE = "decide"
MODE_FYI = "fyi"

ATTENTION_NEEDS_YOU = "needs_you"
ATTENTION_FYI = "fyi"

STATE_OPEN = "open"
STATE_ACTED = "acted"
STATE_ACKED = "acked"
STATE_EXPIRED = "expired"

# Every kind the feed can carry (step-2 contract + brief peer_digest). Kept as a
# frozenset so producers/tests can assert membership without importing the dict.
KINDS: frozenset[str] = frozenset({
    "email_tier", "attribution", "proposal", "pending", "routine_match",
    "recurrence", "contract", "slot_suggestion", "routing", "health", "weather",
    "event", "ops_notable", "ticket_notice", "radar", "friction",
    "notegen_readout", "peer_digest",
})

# Static per-kind (mode, attention) defaults, seeded from the step-2 severity map
# (job table Mode column: D=Decide → decide/needs_you, A=Awareness → fyi/fyi).
# ONE flat dict, trivially re-tuned in Phase D when the learning loop arrives —
# do NOT scatter per-kind branches through the producers.
KIND_DEFAULTS: dict[str, tuple[str, str]] = {
    # Decision kinds — the operator must judge these.
    "email_tier": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    "attribution": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    "proposal": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    "pending": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    "routine_match": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    "recurrence": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    "contract": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    "slot_suggestion": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    "routing": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    # Awareness kinds — surfaced for glance, no decision demanded.
    "health": (MODE_FYI, ATTENTION_FYI),
    "weather": (MODE_FYI, ATTENTION_FYI),
    "event": (MODE_FYI, ATTENTION_FYI),
    "ops_notable": (MODE_FYI, ATTENTION_FYI),
    "ticket_notice": (MODE_FYI, ATTENTION_FYI),
    "radar": (MODE_FYI, ATTENTION_FYI),
    "friction": (MODE_FYI, ATTENTION_FYI),
    "notegen_readout": (MODE_FYI, ATTENTION_FYI),
    "peer_digest": (MODE_FYI, ATTENTION_FYI),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_id(kind: str, stable_key: str) -> str:
    """The feed id for a thing: ``<kind>:<stable_key>``. Never a render ordinal."""
    return f"{kind}:{stable_key}"


@dataclass
class FeedItem:
    """One projected decision/awareness item.

    ``id`` + ``kind`` are the load-bearing identity; every other field carries a
    schema-tolerant default so an older/newer store event round-trips cleanly
    (the house ``from_dict`` known-fields contract, BOTH directions).
    """

    id: str
    kind: str
    instance: str = ""
    title: str = ""
    mode: str = MODE_FYI
    attention: str = ATTENTION_FYI
    evidence: dict[str, Any] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    state: str = STATE_OPEN
    created_at: str = ""
    acted_at: str | None = None
    # The VERB that last drove this item to ``acted`` (Phase C slice 2). ``None``
    # for legacy acted events (pre-amendment) + reconcile-decided items + every
    # non-acted state. Slot completions stamp ``"done"``; a slot accept stamps
    # ``"accept"`` — the ONLY signal that distinguishes an accepted-but-not-yet-
    # completed item (state=acted, evidence.candidate still true) from a completed
    # one, so the FE can render PLANNED reload-stably AND the router can let a
    # ``done`` through on an accepted item. Newest acted event wins the fold.
    acted_action: str | None = None
    expires_at: str | None = None
    source_ref: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        kind: str,
        stable_key: str,
        instance: str,
        title: str,
        evidence: dict[str, Any] | None = None,
        actions: list[dict[str, Any]] | None = None,
        mode: str | None = None,
        attention: str | None = None,
        state: str = STATE_OPEN,
        created_at: str | None = None,
        expires_at: str | None = None,
        source_ref: dict[str, Any] | None = None,
    ) -> "FeedItem":
        """Build a FeedItem, applying KIND_DEFAULTS for mode/attention unless
        explicitly overridden. Producers use this; the stable_key becomes the id."""
        default_mode, default_attention = KIND_DEFAULTS.get(kind, (MODE_FYI, ATTENTION_FYI))
        return cls(
            id=make_id(kind, stable_key),
            kind=kind,
            instance=instance,
            title=title,
            mode=mode or default_mode,
            attention=attention or default_attention,
            evidence=dict(evidence or {}),
            actions=list(actions or []),
            state=state,
            created_at=created_at or _now_iso(),
            expires_at=expires_at,
            source_ref=dict(source_ref or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FeedItem":
        """Schema-tolerant load: filter to known fields so an event written by a
        different tool version (extra fields either direction) round-trips rather
        than crashing the loader. ``id`` + ``kind`` must be present; a corrupt
        event missing them raises (the store's fold skips those)."""
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)
