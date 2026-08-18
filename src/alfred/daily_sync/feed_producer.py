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

import structlog

from alfred.feed import FeedItem, FeedStore, try_feed_reconcile
from alfred.feed.model import ATTENTION_NEEDS_YOU, MODE_DECIDE

from .tier_override import TierOverrides

log = structlog.get_logger(__name__)

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


#: Which registered section provider feeds which feed family.
#:
#: The two vocabularies are NOT the same words — four of the seven differ
#: (``email_calibration``→``email_tier``, ``attribution_audit``→``attribution``,
#: ``canonical_proposals``→``proposal``, ``pending_items``→``pending``) — so the
#: correspondence has to be written down somewhere. Written down means it can
#: drift, so it is pinned — but NOT symmetrically, and the asymmetry is the
#: point rather than an omission:
#:
#:   * ``test_provider_family_map_covers_every_family`` — this map's keys and
#:     ``_FAMILIES`` are the SAME SET. The KeyError end: ``fire_once`` does a
#:     bare ``PROVIDER_FOR_FAMILY[kind]`` per family, so a family added there
#:     and not here raises mid-fire.
#:   * ``test_every_mapped_provider_is_actually_registered`` — every VALUE here
#:     is a name some section module really registers. The SILENT end, and the
#:     one with teeth: rename a provider in its own module and this lookup
#:     merely stops matching ``failed_sections()``. Nothing raises, every test
#:     stays green, and the could-not-read signal goes dark — which is exactly
#:     the failure this map was added to prevent.
#:
#: The converse — "every registered provider maps to a family" — is FALSE, and
#: an earlier version of this comment claimed the pin held it. Seven of the
#: fourteen registered providers feed no family at all (they render a section to
#: read and emit no cards); ``test_seven_registered_providers_deliberately_feed
#: _no_family`` asserts that list, so a provider that SHOULD have had a family
#: arrives as a diff instead of joining the unmapped majority unnoticed.
PROVIDER_FOR_FAMILY: dict[str, str] = {
    "email_tier": "email_calibration",
    "attribution": "attribution_audit",
    "proposal": "canonical_proposals",
    "pending": "pending_items",
    "routine_match": "routine_match",
    "radar": "radar",
    "friction": "friction",
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
# #72 item (c)/2, BUILT — this is the function the approved demotion override
# plugs into, and the two decisions recorded when the seam was cut still hold.
#
# WHERE IT GOES. An approved demotion returns attribution cards to needs-you
# WHOLESALE, not per item. So it belongs above the ``contested`` check as an
# early return, and KIND_DEFAULTS stays the code default — the override is a
# persisted operator decision layered over it, never an edit to the dict.
# Re-derive it on every emit, exactly as ``contested`` is re-derived: reconcile
# re-upserts every open item each fire, so a tier written once into the store
# would be flattened back to FYI by the next sync.
#
# It arrives as an ARGUMENT rather than being read from disk in here. Same
# reason ``contested`` is read off the item: this function is called once per
# item, and a file read per item would turn one daily read into a per-card one.
# :func:`build_feed_items` resolves the kind's override once and hands it down.
#
# REVERSIBILITY, v1. A manual escape hatch is sufficient — ``alfred
# tier-override clear <kind>`` returns the kind to its KIND_DEFAULTS tier. A second
# propose-flow for re-demotion is NOT built: it is speculative until an operator
# actually asks to undo one, and the demotion direction is the safe one (more
# review, not less), so a stuck override over-asks rather than silently
# under-asks. The state is schema-tolerant and instance-scoped per the CLAUDE.md
# state rules, because the escape hatch reads it too.
def _attribution_tier(
    d: dict[str, Any], override: tuple[str, str] | None,
) -> tuple[str, str] | None:
    if override is not None:
        return override
    if d.get("contested"):
        return (MODE_DECIDE, ATTENTION_NEEDS_YOU)
    return None


_TIER_OVERRIDES: dict[
    str, Callable[[dict, tuple[str, str] | None], tuple[str, str] | None]
] = {
    "attribution": _attribution_tier,
}


def build_feed_items(
    kind: str,
    raw_items: list[Any] | None,
    instance: str,
    *,
    tier_overrides: TierOverrides | None = None,
) -> list[FeedItem]:
    """Translate one batch family's raw items into FeedItems (evidence verbatim).

    ``tier_overrides`` (#72) is the operator's persisted standing decision about
    which tier a KIND sits at, layered over ``KIND_DEFAULTS``. Resolved once per
    kind here and handed to the per-item hook, so the file is read once per fire
    rather than once per card.
    """
    key_fn, title_fn = _FAMILIES[kind]
    override_fn = _TIER_OVERRIDES.get(kind)
    kind_override = tier_overrides.tier_for(kind) if tier_overrides else None
    out: list[FeedItem] = []
    for item in raw_items or []:
        d = _as_dict(item)
        stable = key_fn(d)
        if not stable:
            continue  # can't stably key it — skip rather than mint an unstable id
        # A kind with no per-item hook still honours a stored kind-level
        # override. Falling back to ``None`` here instead would make the escape
        # hatch a silent no-op on every kind but attribution — an operator
        # setting one, seeing no error, and getting no change.
        override = override_fn(d, kind_override) if override_fn else kind_override
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
    tier_overrides: TierOverrides | None = None,
) -> None:
    """Reconcile every daily-sync family into the feed store. Belt-guarded per
    family.

    FAILURE IS NOT EMPTINESS — the contract this producer adopted from the brief
    producer, which has enforced it since it shipped:

      * a family passed as ``None`` means COULD NOT READ. Its reconcile is
        SKIPPED entirely, leaving that kind's feed state untouched. A blip must
        never be able to terminalize an operator's open cards.
      * a family passed as a LIST — including ``[]`` — means the caller read its
        source and this is what was there. It reconciles, and a genuine 100%
        clear retires normally, because that is the lifecycle working.

    Passing a list is therefore a DECLARATION, and it is what earns
    ``empty_is_authoritative=True`` on the call below. The caller must not pass
    ``[]`` for a section it failed to read — see ``fire_once``, which asks
    ``assembler.failed_sections()`` rather than guessing.

    ``tier_overrides`` (#72) carries the operator's approved per-kind tier
    decisions. Threaded from the daemon's ONE production call site in the same
    commit that added it: a default-``None`` gate parameter that only tests pass
    is a live write side with a dead read side, and every pin stays green.
    """
    by_family = {
        "email_tier": email_items,
        "attribution": attribution_items,
        "proposal": proposal_items,
        "pending": pending_items,
        "routine_match": routine_match_items,
        "radar": radar_items,
        "friction": friction_items,
    }
    # ITEM 4 — retirement must not be a dark stratum. Accumulated across the
    # whole fire and reported once below; see that log line for why the three
    # non-retiring outcomes are counted alongside it.
    retired_by_kind: dict[str, int] = {}
    refused_by_kind: dict[str, int] = {}
    unreadable: list[str] = []
    belt_failed: list[str] = []
    for kind in _FAMILIES:
        raw = by_family[kind]
        if raw is None:
            # ILB: a skipped kind must announce itself. "The feed did nothing
            # for this family" and "this family had nothing" are the two states
            # this whole contract exists to separate, so they cannot share a
            # silence.
            unreadable.append(kind)
            log.warning(
                "feed.producer.family_unreadable",
                kind=kind,
                detail=(
                    "family reported COULD-NOT-READ — reconcile skipped, this "
                    "kind's feed state left untouched. Open cards stay open; a "
                    "healthy next fire reconciles them normally."
                ),
            )
            continue
        feed_items = build_feed_items(
            kind, raw, instance, tier_overrides=tier_overrides,
        )
        counts = try_feed_reconcile(
            store, kind, feed_items, empty_is_authoritative=True,
        )
        if counts is None:
            # The belt swallowed a store fault. It logged its own line; recorded
            # here too because from the SWEEP's point of view this is a fourth
            # reason a kind retired nothing, and the summary below is only
            # honest if it can name all four.
            belt_failed.append(kind)
            continue
        if counts.get("retired"):
            retired_by_kind[kind] = counts["retired"]
        if counts.get("refused"):
            refused_by_kind[kind] = counts["refused"]

    # THE COUNT LINE (item 4, the ratified minimum). One line per fire, ALWAYS
    # emitted — including the all-zero fire, which is the common case and the
    # whole reason this exists. A retirement is the one state transition no
    # operator ever asked for, so it is the one that most needs a place it can
    # be read; before this, a card that vanished overnight left nothing at this
    # altitude to read at all.
    #
    # NO CHEAPER SURFACE EXISTED. The transport serves ``/feed/items`` and
    # ``/feed/act`` only — no counts endpoint to extend — and the belt's
    # per-kind ``feed.reconcile`` line is per-RECONCILE, so reading retirements
    # off it means correlating seven lines and knowing which fire they came
    # from. This aggregates what already crossed the wire rather than
    # recomputing anything.
    #
    # ALL FOUR OUTCOMES, because a zero here is ambiguous in exactly the way
    # the counts were split to prevent. "Nothing retired" can mean: nothing left
    # any open set (quiet, healthy); the breaker REFUSED a wholesale wipe (a
    # producer is faulting and its cards are being held open); a family could
    # not be read at all (skipped upstream); or the belt swallowed a store
    # fault. Reporting only the first would make the three failures look like
    # the healthy case — the identical-looking-zeros problem, one level up.
    total_retired = sum(retired_by_kind.values())
    log.info(
        "feed.producer.retirement_summary",
        retired=total_retired,
        by_kind=dict(sorted(retired_by_kind.items())),
        refused=sum(refused_by_kind.values()),
        refused_by_kind=dict(sorted(refused_by_kind.items())),
        families_unreadable=sorted(unreadable),
        families_belt_failed=sorted(belt_failed),
        detail=(
            "no cards retired this fire"
            if not total_retired
            else f"{total_retired} card(s) left their producer's open set this fire"
        ),
    )
