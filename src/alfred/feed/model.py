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

# --- snapshot kinds: per-kind revival policy ---------------------------------
#
# Most kinds are EPISODE-shaped: the producer emits an item when a condition
# starts and stops emitting it when the condition ends, so the same key
# reappearing genuinely means "this happened again" and reviving a decided item
# is correct (a tool that goes ok → warn → ok → warn should re-surface).
#
# SNAPSHOT kinds are different: the producer re-emits the same open set every
# fire because it is describing a standing state, not an event. ``event`` is
# the case that bit us — an appointment on the 10th keeps the id
# ``event:2026-08-10|Dentist`` every single morning until it passes, so the
# reconcile upsert revived it the day after each ack and the operator was asked
# to acknowledge the same appointment daily. Operator ruling (2026-08-03):
# events surface when FIRST ADDED and when their CONTENT CHANGES (a moved
# time); an ack sticks otherwise.
#
# The fingerprint is the content the operator would want re-surfaced. Fields
# already inside the stable key are included so the fingerprint stands alone if
# a key function ever changes. Adding a kind here is a DELIBERATE semantic
# change — it makes acks stick across re-emission — so it does not happen by
# default; see the module docstring note on peer_digest.
SNAPSHOT_FINGERPRINT_FIELDS: dict[str, tuple[str, ...]] = {
    "event": ("date_iso", "name", "rec_type", "time_display"),
    # ``peer_digest`` — same shape as ``event``, different trigger. Its stable
    # key is ``<peer>|<today_iso>`` (feed_producer.py:277), so it is stable
    # WITHIN a day: a brief retry or a manual re-fire re-emits the same id and
    # the reconcile upsert revives a digest the operator already acked.
    #
    # Fields chosen so a CHANGED digest still revives. ``body`` is the digest's
    # whole content and ``truncated`` distinguishes a 4000-cap clip from the
    # same prose in full — an UPDATED re-fire (peer sent more since the first
    # run) resurfaces, which is what the operator wants; only a byte-identical
    # re-fire stays suppressed. Deliberately NOT keyed on ``peer``/``date``
    # alone, which are already in the stable key and would suppress every
    # re-fire including a changed one.
    #
    # Exposure was low (the brief fires once daily), which is why this rode as
    # a smalls item rather than the #16 event fix — but the mechanism is
    # identical and the operator-facing failure is the same groundhog ack.
    "peer_digest": ("body", "truncated"),
}


def snapshot_fingerprint(kind: str, evidence: dict[str, Any] | None) -> str | None:
    """Content fingerprint for a snapshot kind, or ``None``.

    ``None`` means "no opinion — use episode semantics", and is returned for
    every non-snapshot kind AND for a snapshot item whose evidence carries none
    of the fingerprint fields. That second case is the read-belt for items
    written before this policy existed (and for malformed evidence): an item we
    cannot fingerprint falls back to the PRE-EXISTING behaviour (revive) rather
    than being silently suppressed forever on the strength of a guess.
    """
    fields = SNAPSHOT_FINGERPRINT_FIELDS.get(kind)
    if fields is None:
        return None
    if not isinstance(evidence, dict):
        return None
    parts = [str(evidence.get(f, "") or "") for f in fields]
    if not any(parts):
        return None
    # Unit separator — cannot appear in the field values, so ("a", "b|c") and
    # ("a|b", "c") can't collide into the same fingerprint.
    return "\x1f".join(parts)


# Every kind the feed can carry (step-2 contract + brief peer_digest). Kept as a
# frozenset so producers/tests can assert membership without importing the dict.
KINDS: frozenset[str] = frozenset({
    "email_tier", "email_urgent", "attribution", "proposal", "pending", "routine_match",
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
    # #27 — a classify-time high-priority email interrupt (curator emits at
    # classify time, distinct from the sync-time email_tier calibration card).
    # decide/needs_you: deals into the deck + counts in rings/needs-you. Single
    # verb is ``ack`` → acted (see FEED_ACTIONS in daily_sync/action_router).
    "email_urgent": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    "proposal": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    "pending": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    "routine_match": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    "recurrence": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    "contract": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    "slot_suggestion": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    "routing": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    # Awareness kinds — surfaced for glance, no decision demanded.
    # attribution (#63a) was a decide kind until the operator ruled that these
    # confirmations are consistently correct, so reviewing them in the deck cost
    # him time and returned no information. It is now a glance card that
    # auto-confirms after 24h. A CONTESTED entry is the exception and the
    # producer promotes it back to decide/needs_you per-item — the default here
    # is the uncontested case.
    "attribution": (MODE_FYI, ATTENTION_FYI),
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
    # --- interval extent (D7, 2026-08-11) ------------------------------------
    # WHEN THE THING ITSELF IS, as opposed to when the feed learned about it.
    # ``created_at`` is provenance; these two are content. Time-shaped items are
    # the norm rather than the exception here — a fog window runs 11:00–15:00, a
    # run is a moment at 09:30, a driver's corridor exposure is four hours, a
    # routine has a cadence — and the pre-amendment model could only carry a
    # POINT, so every renderer that wanted a duration had to re-parse it out of
    # a display string (``event`` evidence carried ``time_display``, a rendered
    # ``"%H:%M"``, which is lossy by construction: no end, no date, no tz).
    #
    # Both are OPTIONAL and both are independently optional: an instant carries
    # ``starts_at`` with ``ends_at=None`` (a moment, not a zero-length span), and
    # an item with no time dimension at all leaves both ``None``. Renderers MUST
    # treat ``ends_at=None`` as "no known end" and never as "ends immediately".
    #
    # ISO-8601 strings, not datetimes, because the whole model round-trips
    # through JSON in the store — the same reason ``created_at`` is a string.
    # Producers stamp whatever offset the source carries; normalization is a
    # render-layer concern, so nothing here silently shifts an operator's clock.
    starts_at: str | None = None
    ends_at: str | None = None
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
        starts_at: str | None = None,
        ends_at: str | None = None,
        source_ref: dict[str, Any] | None = None,
    ) -> "FeedItem":
        """Build a FeedItem, applying KIND_DEFAULTS for mode/attention unless
        explicitly overridden. Producers use this; the stable_key becomes the id.

        ``starts_at`` / ``ends_at`` are the item's own interval extent (D7) and
        both default to ``None`` — a producer with no time dimension passes
        neither, exactly as every producer did before the amendment."""
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
            starts_at=starts_at,
            ends_at=ends_at,
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
