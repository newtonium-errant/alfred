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
# "later" (D2, 2026-08-11). A DEFERRED item is not open (it stays out of the
# deck and out of every ``state=open`` query) and is not decided either — the
# operator has not judged it, only moved it. It therefore MUST come back; a
# defer that never returns is a silent drop wearing a politer name.
#
# Two shapes, distinguished by ``FeedItem.deferred_until``:
#   * ``None`` — defer to NEXT RENDER. The next producer fire returns it.
#   * an ISO timestamp — defer to A TIME. Fires before that instant leave it
#     alone; the first fire at/after it returns the item.
# Both are consumed through ONE predicate, :func:`defer_window_open`, so no
# consumer re-derives "is this still deferred" and drifts.
STATE_DEFERRED = "deferred"

# RETIRED (operator ruling "Ratify A+C", 2026-08-15) — the item left its
# producer's open set and was terminalized for that reason ALONE. Nobody judged
# it.
#
# WHY IT IS A STATE AND NOT ONLY A VERB. PY-B gave retirements the
# ``acted_action='retired'`` verb, which made them auditable going forward but
# left them sharing ``state='acted'`` with the operator's own decisions. Every
# consumer that asks the coarse question — "was this decided?" — still got
# "yes" for something he never decided, and the deck's own gate answered an act
# on one with ``already_acted``, which is a sentence about him that is not true.
# The verb annotated the record; the state is what the consumers actually read.
#
# NO RETROACTIVE MIGRATION. Retirements already on disk keep ``state='acted'``
# and their verb. Rewriting history to make a past record legible is how the
# record stops being a record — the census annotates them separately.
STATE_RETIRED = "retired"

# NO ``TERMINAL_STATES`` SET LIVES HERE, and that is a decision rather than an
# omission — it was defined in this lane, found to have no consumer, and removed
# before it could acquire one.
#
# The idea was that consumers asking "is this finished with?" would read one
# frozenset instead of hand-comparing against ``acted``. Every site that looked
# like its caller turned out to be one of two things it must not be turned into:
#
#   * an ALLOWLIST whose allowlist-ness IS its safety property. The router's
#     folded-state gate admits ``open`` alone; ``deferred`` is non-open but NOT
#     terminal, so a terminal-denylist there would make an act sail past a card
#     the operator explicitly parked. The sweep and the reconcile admit
#     ``(open, deferred)``, and phrasing those as "not terminal" would flip an
#     unknown FUTURE state from excluded-by-construction to included — handed to
#     a reconcile that upserts at ``state=open``, i.e. revived every tick, which
#     is the groundhog bug arriving through the tidy-up meant to prevent it.
#   * a deliberate PROPER SUBSET that has to stay one. ``_revival_suppressed``
#     gates on (acted, acked) — DECIDED, deliberately excluding expired and
#     retired. ``_apply_event``'s verb branch gates on (acted, retired) —
#     terminal-WITH-VERB, deliberately excluding acked and expired. Both would
#     be wrong if widened to all four.
#
# So the explicit comparisons stay explicit: each encodes a different question,
# and the set that answered none of them was a claim about a job it was not
# doing — the same shape as the docstring in feed_producer that named a test
# nobody had written. Keeping it against a future consumer would have been that
# same bet placed twice.

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


# T2-1 — the returned-reminder ring. A NAMED constant where its siblings are
# bare literals, because two packages OUTSIDE ``alfred.feed`` have to spell this
# kind: the transport scheduler emits it and the daily-sync action router gates
# it. The sibling locals (``URGENT_KIND`` &c. in action_router) each have one
# owner and predate this; they are deliberately left alone rather than swept
# into a half-migration.
KIND_REMINDER_RETURNED = "reminder_returned"

# The sort rotation (2026-08-19). A NAMED constant for the same reason as the
# kind above: three packages outside ``alfred.feed`` have to spell it — the
# rotation selects (``tier.sort_rotation``), the brief emits
# (``brief.feed_producer``) and the router gates (``daily_sync.action_router``).
# The canonical spelling lives in ``tier.sort_rotation.SORT_KIND``; this is the
# feed package's import-free copy, and a test pins them equal.
KIND_SORT_SUGGESTION = "sort_suggestion"

# The MOC reader (2026-08-20). Named for the same reason as the kind above:
# spelled outside ``alfred.feed`` by the brief (emits), the action router
# (gates + dispatches) and the surveyor-side queue reader.
KIND_MOC_SUGGESTION = "moc_suggestion"

# The voice-calibration review card (R4, 2026-08-21). Named for the same reason
# as the kinds above: spelled outside ``alfred.feed`` by the daily-sync producer
# (emits), the action router (gates + dispatches) and the web client.
KIND_CALIBRATION = "calibration"

# The verb stamped on a retirement — an item marked ``acted`` by
# :meth:`FeedStore.reconcile` because it left the producer's open set, NOT
# because the operator judged it. Until this existed both outcomes appended a
# byte-identical verbless ``state=acted`` event, so "the operator decided this"
# and "the producer stopped emitting it" were indistinguishable in the log
# forever after — which is how an auto-retirement reads as a decision.
#
# A NAMED constant for the same reason as the kind above: it is written in
# ``feed.store`` and read by tests and future auditors outside this package.
#
# STATE IS UNCHANGED — ``acted`` stays ``acted``. This distinguishes the two
# only by VERB, which is forward-compatible with the pending retire-vs-decided
# ratification and inert until then: every current reader of ``acted_action``
# (Python and web) compares it for equality against a specific verb
# (``accept`` / ``done`` / ``snooze``), and NOTHING anywhere tests it for
# absence — so a fourth verb changes no branch. ``rings.ts`` is the case worth
# naming: ``acted_action === 'accept' ? 'planned' : 'done'`` sent verbless
# retirements to ``done`` before and sends ``retired`` to ``done`` now.
ACTION_RETIRED = "retired"

# Every kind the feed can carry (step-2 contract + brief peer_digest). Kept as a
# frozenset so producers/tests can assert membership without importing the dict.
KINDS: frozenset[str] = frozenset({
    "email_tier", "email_urgent", "attribution", "proposal", "pending", "routine_match",
    "recurrence", "contract", "slot_suggestion", "routing", "health", "weather",
    "event", "ops_notable", "ticket_notice", "radar", "friction",
    "notegen_readout", "peer_digest", "pattern_surfaced",
    KIND_REMINDER_RETURNED, KIND_SORT_SUGGESTION, KIND_MOC_SUGGESTION,
    KIND_CALIBRATION,
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
    # C4 — the contact-surface router noticed it keeps being overridden the same
    # way and is ASKING. decide/needs_you because the whole point is that it is
    # never applied silently: the operator's tap is the only thing that changes
    # the router's behaviour (the self-correcting standard's propose-then-
    # approve arrow, made visible on the deck).
    "pattern_surfaced": (MODE_DECIDE, ATTENTION_NEEDS_YOU),
    # T2-1 — a snoozed/waiting task's reminder came due. THIS LINE IS THE ONE
    # THAT RINGS THE PHONE, and it is worth saying where the ring actually comes
    # from, because nothing else in this file mentions it: the web push poller
    # fetches by ATTENTION (``feedNeedsYou.isNeedsYouItem``) with no kind
    # allowlist, and the default push policy admits every needs-you item. So
    # ``ATTENTION_NEEDS_YOU`` here is the whole wiring — downgrade this entry to
    # FYI and the doorbell goes silent with every Python test still green, which
    # is why the pin that guards it is a web test rather than a Python one.
    KIND_REMINDER_RETURNED: (MODE_DECIDE, ATTENTION_NEEDS_YOU),
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
    # The sort rotation. A sort card ASKS the operator something, so by the
    # seeding rule above it reads like a decide kind — and it is deliberately
    # NOT one, for a measured reason rather than a preference.
    #
    # ``feedNeedsYou.isNeedsYouItem`` is ``attention === 'needs_you' || mode ===
    # 'decide'`` (web), the push poller fetches by that predicate with NO kind
    # allowlist, and the default ``needs_you`` policy admits everything it is
    # handed. So MODE_DECIDE rings the phone REGARDLESS of attention: there is
    # no decide/fyi combination that stays quiet. The operator killed the
    # notification flood, and a backlog-grooming card is the last thing that
    # earns a doorbell — it is work he chose to defer, by definition.
    #
    # FYI costs the card nothing it needs, because it carries a co-equal
    # choice group and therefore clears ``isDeckCandidate``.
    #
    # CORRECTION (2026-08-20, R4), recorded AT the claim because the next
    # reader meets it here rather than in commit history: this comment used to
    # cite ``attribution`` as "the shipped precedent (FYI/FYI, three verbs,
    # deck-dealt)". That is a pre-``isDeckCandidate`` FOSSIL and it is exactly
    # backwards. ``deck.tsx`` filters by ``isDeckCandidate`` FIRST and only
    # then by ``isDeckDealt``, and ``isDeckCandidate`` is ``mode === 'decide'
    # || hasSuggestedChoice(item)`` — so an FYI card with gestured-but-
    # UNGROUPED verbs never reaches the deck at all. ``attribution`` is the
    # named COUNTER-example on both sides: ``feedConstants.ts`` cites it in
    # ``isDeckCandidate``'s own docstring as the reason the rule is not
    # ``isDeckDealt ∧ fyi``, and ``web/tests/holdPrimitive.test.ts`` pins
    # ``isDeckCandidate(fyiAttribution) === false``. A quiet card reaches the
    # deck through its suggestion GROUP, never through its verbs.
    # What FYI DOES add is the universal ack
    # (``action_router`` gates ack on ``mode == MODE_FYI``): acking a card that
    # is still unslotted revives it on the next fire, because this is an episode
    # kind. That is honest — it IS still unsorted — but the defer verbs are the
    # intended "not now", and they are the ones that hold.
    KIND_SORT_SUGGESTION: (MODE_FYI, ATTENTION_FYI),
    # The MOC reader (2026-08-20). QUIET FOR THE SAME MEASURED REASON as the
    # rotation above, and the reasoning is worth repeating rather than
    # cross-referencing because getting it wrong is silent: MODE_DECIDE rings
    # the phone REGARDLESS of attention (``isNeedsYouItem`` is
    # ``attention === 'needs_you' || mode === 'decide'``, and the push poller
    # fetches on that predicate with no kind allowlist). There is no
    # decide/fyi combination that stays silent. A suggestion the surveyor
    # generated on its own schedule is the last thing that earns a doorbell.
    #
    # FYI costs this card NOTHING it needs, because it carries a co-equal
    # choice group: ``isDeckCandidate`` admits a quiet card precisely when
    # ``hasSuggestedChoice`` holds. That coupling is not incidental here — it
    # is what makes the card exist at all, and the producer refuses to emit a
    # row that cannot offer two targets rather than deal a verb onto a
    # surface with no gesture.
    KIND_MOC_SUGGESTION: (MODE_FYI, ATTENTION_FYI),
    # The voice-calibration review card (R4, 2026-08-21). QUIET, and this pair is
    # the whole of why — so it is recorded here rather than in a commit message.
    #
    # THE SEEDING RULE POINTS THE OTHER WAY AND IS OVERRULED DELIBERATELY. A
    # calibration card ASKS the operator something ("should Alfred believe this
    # about you?"), which by the rule above reads like a decide kind. It is not
    # one, for a MEASURED reason:
    #
    #   ``feedNeedsYou.isNeedsYouItem`` is ``attention === 'needs_you' || mode
    #   === 'decide'``, and the push poller fetches on that predicate with NO
    #   kind allowlist. So MODE_DECIDE rings the phone REGARDLESS of attention —
    #   there is no decide/fyi combination that stays quiet. A wording
    #   observation drafted by an analyzer on its own schedule is the last thing
    #   that earns a doorbell.
    #
    # AND IT MUST NOT GAIN A SUGGESTED CHOICE GROUP, which is the non-obvious
    # half. ``isDeckCandidate`` is ``mode === 'decide' || hasSuggestedChoice``,
    # so a grouped affirm would make this card deck-eligible — and the deck is a
    # needs-you surface. The card reaches the operator through the FEED BOARD's
    # FYI column via a per-kind affordance (the attribution contest door's
    # shape), NOT through the deck.
    #
    # PRECISELY: it takes a group shared by BOTH verbs, because
    # ``holdChoicesForVerb`` requires ``family.length >= 2`` ("a family of one is
    # no family"). A group on one verb alone is inert. This sentence was
    # initially written as the stronger "give either verb a group and you re-arm
    # the doorbell", which is FALSE — the web pin's positive control measured it
    # and the claim is corrected here, where the next reader meets it, rather
    # than only in commit history.
    #
    # PINNED IN TWO PLACES, on purpose. ``web/tests/calibrationCardQuiet.test.ts``
    # drives the REAL web predicates against a card built from this table (a
    # Python test asserting "the tuple says fyi" would only restate the line it
    # guards); ``tests/feed/test_calibration_card_is_quiet.py`` holds the
    # Python-side facts the web cannot see — this seed's membership and the
    # absence of a ``group`` in ACTION_META.
    KIND_CALIBRATION: (MODE_FYI, ATTENTION_FYI),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 instant, or ``None`` when it isn't one.

    A NAIVE value is read as UTC rather than rejected: every producer in-tree
    stamps aware UTC, so a naive one is an older/hand-written value, and reading
    it as UTC is the interpretation that keeps a comparison possible instead of
    raising ``TypeError`` mid-reconcile.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def defer_window_open(deferred_until: str | None, now: str | None = None) -> bool:
    """Is this defer still holding? THE canonical defer predicate (D2).

    ``True`` means the item stays suppressed this fire; ``False`` means it
    RETURNS to attention. Every consumer asks this function — the health-status
    ``skip`` lesson applies exactly: a re-localized comparison drifts, so there
    is one predicate and no second spelling of it.

    The fail direction is deliberate and one-way: **when in doubt, the item
    returns.** A missing window (next-render defer), an unparseable one, and an
    unreadable clock all yield ``False``. Extra noise costs the operator a
    glance; a commitment that silently never comes back costs him the thing
    itself, and he cannot notice its absence to ask about it.
    """
    if not deferred_until:
        # Defer-to-next-render. Nothing to hold against: the item was hidden for
        # the render in which it was deferred (it is not ``open``), and the very
        # next fire is the return this shape promises.
        return False
    until = _parse_iso(deferred_until)
    if until is None:
        return False
    now_dt = _parse_iso(now) if now else datetime.now(timezone.utc)
    if now_dt is None:
        return False
    return now_dt < until


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
    # --- defer (D2, 2026-08-11) ----------------------------------------------
    # Meaningful only while ``state == STATE_DEFERRED``, and cleared by any
    # transition out of it so a returned item can never carry a stale window.
    # ``None`` while deferred = defer-to-next-render; an ISO instant =
    # defer-to-a-time. Read ONLY through :func:`defer_window_open`.
    deferred_until: str | None = None
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
