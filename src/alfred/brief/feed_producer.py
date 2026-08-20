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

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alfred.feed import FeedItem
from alfred.feed.model import ATTENTION_FYI, ATTENTION_NEEDS_YOU
from alfred.health.types import Status

from .utils import get_logger

log = get_logger(__name__)

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


def _health_attention(status: str) -> str:
    """Attention tier for a health card of per-tool BIT ``status``.

    ``fail`` → needs-you; everything else that reaches a card (``warn``, and any
    unrecognised status, which :func:`~.health_section.is_attention_status`
    already let through) → the kind's FYI default.

    WHY THE SPLIT LIVES HERE AND THE JUDGEMENT DOES NOT. This module does not
    decide severity — the docstring below says so, and the health checks own it.
    What this adds is the missing half of that contract: before 2026-08-15 the
    health layer could distinguish warn from fail perfectly well and it made no
    difference on the operator's surfaces, because every health card was born
    FYI. A sustained agent-backend outage (curator quota-limited for days, email
    intake stopped) rendered as an FYI glance card and the operator found it by
    noticing an empty deck. So ``fail`` is now addressed to someone; ``warn``
    keeps its glance semantics, which is what stops this from becoming a second,
    noisier version of the skip-card incident.

    **MODE IS DELIBERATELY NOT PROMOTED, and that is load-bearing.** Attention
    alone is enough to reach the operator: ``isNeedsYouItem`` (web) is
    ``attention === 'needs_you' || mode === 'decide'``, so the needs-you column
    and the push doorbell (``pushNotifier.fetchNeedsYouItems`` under the default
    ``needs_you`` policy) both pick this up. Promoting MODE to ``decide`` would
    additionally BREAK the card: ``health`` has no entry in
    ``daily_sync.action_router.FEED_ACTIONS``, so it advertises no verbs, and
    the only dismiss path it has is the universal ack — which is gated on
    ``item.mode != MODE_FYI`` and would start answering ``invalid_action``.
    The result would be an unclearable card that rings every morning: the
    2026-08-03 undismissable-health-card incident again, with a doorbell.
    """
    return ATTENTION_NEEDS_YOU if (status or "").strip().lower() == Status.FAIL.value else ATTENTION_FYI


def health_feed_items(
    vault_path: str | Path,
    *,
    instance: str,
) -> list[FeedItem] | None:
    """One ``health`` item per ATTENTION-status tool in the latest BIT record
    (stable key = tool name). Attention = warn/fail, and any unrecognised status
    — see :func:`~.health_section.is_attention_status`. Nothing needing attention
    (record present, every tool ok and/or skip) → ``[]`` so reconcile marks stale
    warns acted. NO record / read error → ``None`` (can't confirm health; don't
    mass-``acted`` on missing data).

    **``skip`` produces no card.** A skipped check is "this did not apply"
    (unconfigured tool, fresh install), which is a standing config fact rather
    than news, and on an instance with unconfigured tools it is a PERMANENT fact:
    KAL-LE's measured BIT is ``tool_counts {ok: 5, warn: 0, fail: 0, skip: 7}``,
    so the old ``status == "ok"`` filter carded all 7 every morning. Those cards
    could not be dismissed — ``health`` is an episode kind (absent from
    ``feed.model.SNAPSHOT_FINGERPRINT_FIELDS``), so ``_revival_suppressed``
    declines to suppress and reconcile REVIVES an acked card on the next fire.
    Suppressing them also restores two properties the old filter broke on any
    skip-bearing instance:

      * the genuine-empty ``[]`` became unreachable (``out`` always held the 7),
        so the all-clear path this docstring promises was dead code there;
      * a tool going warn → skip kept the SAME stable key in the open set, so it
        was never ABSENT and its warn card never cleared — it silently became a
        permanently-open "Health: <tool> SKIP" card.

    No information is lost: ``health_section._render_from_frontmatter`` renders
    every tool unfiltered, so skips stay visible in the brief's Health section.
    The feed is the attention surface; that section is the status surface. If a
    particular skip ever IS news, the fix belongs in the BIT check that assigns
    the status (make it warn/fail), not here — severity is the health layer's
    call, not the feed's.
    """
    from .health_section import (
        _find_latest_bit_record,
        _per_tool_lines,
        is_attention_status,
    )

    vault = Path(vault_path)
    try:
        record = _find_latest_bit_record(vault)
        if record is None:
            return None  # no BIT data at all → don't reconcile (would mass-act)
        body = record.read_text(encoding="utf-8")
    except OSError:
        return None
    out: list[FeedItem] = []
    quiet: list[str] = []
    for tool, status, detail in _per_tool_lines(body):
        if not is_attention_status(status):
            # Log the SUPPRESSED ones an operator might expect a card for. A
            # plain ``ok`` never raised that question, so it would be pure noise;
            # today that leaves exactly ``skip``, but the condition is written
            # against the quiet SET so it keeps reporting if that set ever grows.
            if status != Status.OK.value:
                quiet.append(tool)
            continue
        title = f"Health: {tool} {status.upper()}" + (f" — {detail}" if detail else "")
        out.append(FeedItem.create(
            kind="health",
            stable_key=tool,
            instance=instance,
            title=title,
            evidence={"tool": tool, "status": status, "detail": detail},
            # FAIL is needs-you; WARN stays the FYI glance the kind defaults to.
            # See :func:`_health_attention` — the severity decision is the health
            # layer's, and this only maps it onto the attention surface.
            attention=_health_attention(status),
            source_ref=dict(_SOURCE_REF, record=str(record)),
        ))
    if quiet:
        # ILB: "ran, chose not to card these". Without this line, an operator
        # asking "why is there no health card for my skipped tool?" has only
        # silence to read, which is indistinguishable from a broken producer.
        log.info(
            "brief.health_feed_quiet_tools",
            count=len(quiet),
            tools=sorted(quiet),
            instance=instance,
            reason="status is not attention-worthy (skip = check did not apply); "
                   "still rendered in the brief's Health section",
        )
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
            # Interval extent (D7, 2026-08-11) — the event's own span, off the
            # record's ``start``/``end`` frontmatter. Deliberately NOT folded
            # into ``evidence``: evidence is the card body (what the operator
            # reads), while the extent is structural (what a time-shaped render
            # LAYS OUT against). It also must not join the snapshot fingerprint
            # by the back door — ``SNAPSHOT_FINGERPRINT_FIELDS["event"]`` reads
            # evidence keys, and a moved appointment already re-surfaces through
            # ``time_display`` there. Adding a second, differently-formatted
            # spelling of the same fact to that tuple would let an all-day event
            # whose ``end`` normalization changed revive an ack for no
            # operator-visible reason.
            starts_at=ev.starts_at,
            ends_at=ev.ends_at,
            source_ref=dict(_SOURCE_REF),
        ))
    return out


def _epoch_iso(value: Any) -> str | None:
    """aviationweather.gov epoch SECONDS → ISO-8601 UTC, or ``None``.

    The API hands times as ints (verified against the 2026-08-11 capture:
    ``timeFrom: 1786471200`` → ``2026-08-11T18:00:00+00:00``). Anything else —
    missing, null, a string, a bool — yields ``None`` rather than a guess,
    because a wrong interval silently moves weather on the operator's timeline.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def weather_feed_items(
    tafs: list[dict] | None,
    station_configs: Any,
    *,
    instance: str,
) -> list[FeedItem] | None:
    """One ``weather`` item per station whose TAF we hold (stable key = ICAO id).

    **This is the interval case D7 exists for** — a forecast is not a moment.

    *Item extent = the TAF's VALIDITY window* (``validTimeFrom`` /
    ``validTimeTo``), which is the one interval the forecast asserts outright.

    *Probabilistic blocks do NOT set the item's extent* — the call the fixture
    capture asked for. A ``PROB30`` block covering 19:00–22:00 says there is a
    30% chance of thunderstorms in that window, NOT that thunderstorms occupy
    it. Promoting it to the item's extent would state as fact something the
    data explicitly qualifies, which is the same error as stamping a task's
    ``due`` as a span or promoting an all-day date to midnight. Nothing is
    lost: every block is carried in ``evidence["periods"]`` with its OWN
    interval plus ``probability`` and ``possible: True``, so a renderer can
    draw the maybe-window in its own register and the operator sees the
    forecast's real shape.

    *``timeBec`` is kept as ``becoming_at``, never merged into ``starts_at``.*
    On a BECMG block it marks when the transition COMPLETES, and it sits inside
    the block's window rather than replacing its start (confirmed on the
    capture's CYZX record: ``timeBec`` 19:40 inside a 19:00–21:00 block).
    Conflating them would report a change as finished before the forecast says
    it is.

    ``None`` (a failed/absent forecast leg) → don't reconcile. ``[]`` is a
    genuine empty (no stations forecast) and DOES reconcile, clearing stale
    items.
    """
    if tafs is None:
        return None
    name_map: dict[str, str] = {}
    try:
        name_map = {s.id: s.name for s in (station_configs or [])}
    except (AttributeError, TypeError):  # station configs are optional context
        name_map = {}

    out: list[FeedItem] = []
    for taf in tafs:
        if not isinstance(taf, dict):
            continue
        station = str(taf.get("icaoId", "") or "")
        if not station:
            # No stable key available — skip the record rather than invent one
            # (an ordinal key is the thing the identity rule forbids).
            log.warning("brief.weather_feed_no_station", detail="TAF record without icaoId")
            continue
        periods: list[dict[str, Any]] = []
        for block in taf.get("fcsts", []) or []:
            if not isinstance(block, dict):
                continue
            prob = block.get("probability")
            periods.append({
                "starts_at": _epoch_iso(block.get("timeFrom")),
                "ends_at": _epoch_iso(block.get("timeTo")),
                # None on the BASE block; "FM"/"TEMPO"/"PROB"/"BECMG" otherwise.
                "change": block.get("fcstChange") or "BASE",
                "probability": prob,
                # The renderer's cue to draw this as a MAYBE rather than a will.
                "possible": prob is not None,
                "becoming_at": _epoch_iso(block.get("timeBec")),
                "wx": block.get("wxString") or "",
                "visibility": block.get("visib"),
            })
        out.append(FeedItem.create(
            kind="weather",
            stable_key=station,
            instance=instance,
            title=f"Forecast: {name_map.get(station, station)}",
            evidence={
                "station": station,
                "name": name_map.get(station, station),
                "periods": periods,
                "period_count": len(periods),
            },
            # The asserted interval: the forecast's own validity window.
            starts_at=_epoch_iso(taf.get("validTimeFrom")),
            ends_at=_epoch_iso(taf.get("validTimeTo")),
            source_ref=dict(_SOURCE_REF, station=station),
        ))
    return out


def _slot_stable_key(entry: Any) -> str:
    """Durable identity for a tier-lane entry, per the step-2 identity table:
    task → path (wikilink target); routine item → (record, text); free-text
    T3 → item text. Origin-prefixed so the three spaces can't collide.

    DELEGATES to ``alfred.tier.snooze.slot_stable_key``, the canonical
    implementation. The board card's feed id and the snooze store's key MUST be
    the same string — two copies of this logic would drift, and a drifted key
    silently snoozes nothing (or the wrong row). Pinned by a test that drives
    both names over the same entries.
    """
    from alfred.tier.snooze import slot_stable_key

    return slot_stable_key(entry)


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
                # #18 slice 1 — the slot axis, alongside tier rather than
                # instead of it. Tier is NOT removed: the completion-semantics
                # matrix still keys on ``tier === 3`` for the free-text lane,
                # and escalation / surface windows / all_t1_done are untouched.
                #
                # Emitted from stage 1 so the FE can be built and the rings
                # swapped (stage 2) without a second producer change — a
                # contract extension that arrives with its consumer is a
                # contract nobody can forget to thread. ``slot_rule`` rides
                # along because "Duty because the operator said so" and "Duty
                # because it's a dated task" are different claims, and a card
                # that eventually explains itself needs the distinction.
                "slot": getattr(entry, "slot", "unslotted"),
                "slot_rule": getattr(entry, "slot_rule", "no_signal"),
                "origin": getattr(entry, "origin", ""),
                "name": entry.name,
                "path": getattr(entry, "path", ""),
                "due_iso": getattr(entry, "due_iso", None),
                "surface_reason": getattr(entry, "surface_reason", None),
                "source": source,
                # #14 — WHY this row is back before its snooze ran out
                # (``crossed_due`` / ``moved_earlier``), or "" when it was
                # never snoozed or simply expired. Evidence-only, like
                # ``confirmed`` above: the brief render never reads evidence,
                # so byte-parity holds. The card uses it to answer the question
                # an early return provokes instead of leaving it mysterious.
                "snooze_breakthrough": getattr(entry, "snooze_breakthrough", "") or "",
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
                # Backdated completion (2026-08-20): how many days back a
                # "previously done" completion may honestly reach — computed
                # by the tier layer from the item's own recurrence state
                # (AutoT1Candidate.backdate_limit_days), NEVER re-derived
                # here. ALWAYS PRESENT (0 = no admissible backdate: task/tier
                # lanes, cadence items, mid-window, paid cycle) per the
                # ILB data-shape rule — an absent field cannot be told apart
                # from a producer that predates the feature. The serve side
                # (``actions_for_item``) filters the ``done_Nd`` rungs by it;
                # evidence-only, the brief render never reads it, so
                # byte-parity holds (the ``done``/``confirmed`` precedent).
                "backdate_limit_days": getattr(entry, "backdate_limit_days", 0) or 0,
            },
            source_ref=dict(_SOURCE_REF),
        ))
    return out


def sort_suggestion_feed_items(
    vault_path: str | Path,
    now,
    tier_defaults: Any,
    *,
    instance: str,
    tracked: dict[str, str],
    cap: int,
    corrections_path: str | Path | None,
) -> list[FeedItem] | None:
    """The sort rotation — up to ``cap`` cards asking the operator to place an
    item the classifier honestly refused to guess at.

    The population is the SAME ``compute_today_view`` projection the board reads,
    filtered to entries the projection itself stamped ``unslotted`` — see
    :mod:`alfred.tier.sort_rotation` on why that is a read of the classifier's
    verdict rather than a second copy of its rules. Empty day, or a day where
    every item already has a slot → ``[]`` (a genuine empty; the rotation's ILB
    line still fires). Projection failure → ``None`` (don't reconcile).

    ``tracked`` maps this kind's feed item ids to their stored states. It is
    REQUIRED rather than defaulted because it is what keeps a deferred card
    alive: ``reconcile`` retires anything absent from the returned list, so the
    selection has to know what the store is already holding. A defaulted empty
    dict would make the read side silently dead on the one caller that matters —
    the standing optional-gate trap — and the symptom would be defer windows
    quietly evaporating overnight.

    ``cap`` is likewise required and comes from config; see
    :func:`~alfred.tier.sort_rotation.select_rotation` for why held items are
    carried OUTSIDE it.

    ``corrections_path`` is the proposer's learning store (REQUIRED for the same
    optional-gate reason as ``tracked`` — a defaulted ``None`` would ship the
    write side live and the read side dead, and the learned overrides would
    never reach a card). ``None`` is still a legal VALUE — an instance with no
    store yet proposes from the static table alone, which is what
    ``load_tally(None)`` degrades to.
    """
    from alfred.feed.model import make_id
    from alfred.tier.compute import compute_today_view
    from alfred.tier.sort_proposal import load_tally, propose_slot, shape_of
    from alfred.tier.sort_rotation import (
        SORT_KIND,
        log_rotation,
        select_rotation,
        unslotted_entries,
    )

    try:
        # A SECOND projection this fire — ``slot_suggestion_feed_items`` already
        # computed one. Deliberately not shared: the extractors are independent
        # by construction (each is a lambda the belt can fail in isolation), and
        # threading one view through them would couple every kind's failure to
        # every other's. The brief fires once a day, so the cost is one extra
        # vault read per morning.
        view = compute_today_view(Path(vault_path), now, tier_defaults)
    except Exception:  # noqa: BLE001 — source failure → don't reconcile (belt also logs)
        return None

    sortable, unwritable = unslotted_entries(view)
    selection = select_rotation(
        sortable, tracked, cap=cap,
        key_of=lambda e: make_id(SORT_KIND, _slot_stable_key(e)),
    )
    log_rotation(selection, unwritable=unwritable, cap=cap, instance=instance)

    # The proposer's learned layer, loaded ONCE per fire. Belt-swallowed inside
    # load_tally: a corrupt learning store degrades to the static table, never
    # to a failed fire.
    tally = load_tally(corrections_path)

    out: list[FeedItem] = []
    for entry in selection.emitted:
        key = _slot_stable_key(entry)
        if not key:
            continue
        # EVERY card carries a proposal — the table is total by construction,
        # and a card without one would deal a swipe surface with no affirm (the
        # accepted-then-ignored shape). The SHAPE is stamped at deal time so the
        # act path records the ruling against the exact key this proposal was
        # made under, rather than re-deriving one that could drift.
        proposal = propose_slot(entry, tally)
        out.append(FeedItem.create(
            kind=SORT_KIND,
            stable_key=key,
            instance=instance,
            title=f"Sort: {entry.name}",
            evidence={
                # The writer's addressing fields — the trusted-producer class
                # the sibling board writers already read. ``assign_slot`` takes
                # exactly these and composes no path of its own.
                "origin": getattr(entry, "origin", ""),
                "name": entry.name,
                "path": getattr(entry, "path", ""),
                "routine_record": getattr(entry, "routine_record", None),
                "item_text": getattr(entry, "item_text", None),
                # Context for the face. ``slot_rule`` is carried even though it
                # is always ``no_signal`` for this population: the card should be
                # able to say WHY nothing fired, and hardcoding the answer on the
                # face would make it a caption instead of a reading.
                "tier": entry.tier,
                "slot": getattr(entry, "slot", "unslotted"),
                "slot_rule": getattr(entry, "slot_rule", "no_signal"),
                "due_iso": getattr(entry, "due_iso", None),
                "surface_reason": getattr(entry, "surface_reason", None),
                # The PROPOSAL (2026-08-19 gesture-grammar ruling). The serve
                # side stamps the affirm gesture onto exactly this slot's verb
                # (``actions_for_item``), the face renders it as the suggestion,
                # and the act path scores the operator's choice against it.
                "proposed_slot": proposal.slot,
                "proposed_rule": proposal.rule,
                "proposal_shape": shape_of(entry),
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
