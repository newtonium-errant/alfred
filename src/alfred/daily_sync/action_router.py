"""Feed action router — the deck/feed decision path onto the SAME resolvers.

A deck card (or feed FYI-ack button) acts through :func:`act`, which maps a
``(kind, action_id)`` pair onto the SAME resolver functions the Daily Sync
reply grammar uses (``reply_dispatch._resolve_*``). There is NO parallel write
path: the router synthesizes the ``ReplyCorrection`` the resolver consumes and
calls the resolver directly, so a deck confirm and a typed "1 confirm" land the
identical corpus/vault/queue mutation.

The ``(kind, action_id)`` map (:data:`FEED_ACTIONS`) IS the capability ceiling:
a pair not present here can NEVER reach a resolver or any vault op. Only the
FIVE routed families carry resolver-backed actions:

    email_tier   high/medium/low/spam → tier · up/down → modifier · confirm → ok
    attribution  confirm → ok · reject → reject
    proposal     confirm → ok · reject → reject
    routine_match confirm → ok · reject → reject · correct → reject + the
                 operator's chosen item (#13) · one_off → reject + "means nothing"
    pending      noted → ok+noted · show → ok+show

Plus a universal ``ack`` for FYI items (``mode == "fyi"``) which sets the feed
item ``acked`` DIRECTLY — no resolver, no vault op.

Item source (load-bearing): the resolver needs the authoritative batch item.
The router loads it from the SAME persisted ``last_batch`` payload the
dispatcher reads (``daily_sync_state.json``), located by RE-DERIVING each batch
item's stable key with the SAME key functions the feed producer used
(``feed_producer._FAMILIES``) and matching the full feed id — NEVER by render
ordinal, NEVER from the feed item's display ``evidence``. Aged out of last_batch
→ ``stale_item``.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import date as _date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import frontmatter
import structlog

from alfred.daily_sync import reply_dispatch as _rd
from alfred.daily_sync.assembler import ReplyCorrection
from alfred.daily_sync.attribution_corpus import AttributionCorpusEntry
from alfred.daily_sync.attribution_corpus import append_entry as append_attribution_entry
from alfred.daily_sync.corpus import append_correction
from alfred.daily_sync.feed_producer import _FAMILIES, _as_dict
from alfred.feed.model import (
    ATTENTION_NEEDS_YOU,
    KIND_CALIBRATION,
    KIND_REMINDER_RETURNED,
    KIND_MOC_SUGGESTION,
    KIND_SORT_SUGGESTION,
    MODE_DECIDE,
    MODE_FYI,
    STATE_ACKED,
    STATE_ACTED,
    STATE_OPEN,
    STATE_RETIRED,
    make_id,
)
from alfred.telegram.capture_sections import is_known_section
from alfred.tier import slots
from alfred.vault.attribution import contest_marker
from alfred.vault.paths import VaultContainmentError, resolve_in_vault

log = structlog.get_logger(__name__)

# The router presents the located batch item to the resolver as item #1 of a
# single-item map. The resolvers only use ``item_number`` to key an email
# ``items_by_num`` lookup and to prefix their human error strings — there is no
# numbered batch on the deck path, so a fixed synthetic 1 is correct.
_SYNTHETIC_ITEM_NUMBER = 1

ACK_ACTION = "ack"

# routine_match #13 — the two enriched-reject actions. ``correct`` is the ONLY
# action that consumes the act's ``correction_target``; naming it here keeps the
# injection site and the ceiling entry from drifting apart.
ROUTINE_MATCH_KIND = "routine_match"
CORRECT_ACTION = "correct"
ONE_OFF_ACTION = "one_off"

# ActResult.status vocabulary.
STATUS_ACTED = "acted"
STATUS_ACKED = "acked"
STATUS_ALREADY_ACTED = "already_acted"
STATUS_STALE_ITEM = "stale_item"
STATUS_INVALID_ACTION = "invalid_action"
STATUS_ERROR = "error"
# Phase C slice 1 (board DONE path) — the slot-completion vocabulary.
STATUS_UNDONE = "undone"  # undo_done succeeded → item back to open
# A sort applied. ITS OWN STATUS rather than ``acted``, because the feed item's
# state is deliberately unchanged: the operator told the system where an item
# belongs, which is not the same act as deciding the card. Reusing ``acted``
# would make every coarse "was this decided?" reader — including the deck's own
# gate — answer yes about a card still sitting on the board awaiting a ✓.
STATUS_SORTED = "sorted"
STATUS_UNSUPPORTED_ITEM = "unsupported_item"  # lane has no completion writer (task/unknown)
# #63a — a contested attribution item goes BACK to needs-you and stays OPEN.
# It needs its own status because every other successful act ends the item's
# life (acted/acked); this one reopens the question instead.
STATUS_CONTESTED = "contested"
# PY-C item 2 — the operator acted on a card its PRODUCER withdrew.
#
# It needs its own status for the reason the state does: ``already_acted`` is a
# sentence ABOUT HIM ("you already dealt with this"), and for a retirement it is
# false — nobody dealt with it, the section that was offering it stopped. Until
# ``retired`` was its own state this gate could not tell the two apart, because
# a retirement was stored as ``acted``; now it can, so the answer stops lying.
#
# NOT ok. ``ok=True`` is documented on :class:`ActResult` as the applied paths
# plus the idempotent noop — an act that could not apply and left the operator's
# intent unrecorded is neither. The transport maps it to 409 (see
# ``routes_feed._HTTP_STATUS_BY_STATUS``), the same code as ``stale_item``,
# which is the same class of answer: the item moved on without him.
STATUS_RETIRED = "retired"

# #63a attribution contest. The door out of the FYI tier: "don't decide this one
# for me." Attribution's verb alone — see the ceiling entry below.
ATTRIBUTION_KIND = "attribution"
CONTEST_ACTION = "contest"

# slot_suggestion completion actions (handled by the dedicated dispatcher, NOT
# the ReplyCorrection/last_batch path — the writers are called directly).
DONE_ACTION = "done"
UNDO_DONE_ACTION = "undo_done"
# Backdated completion rungs (operator ruling 2026-08-20: "have yesterday be
# the default 'previously done' option, with ability to backdate further as
# needed") — verb id → days BACK from the act's today. A STATIC CLOSED SET per
# the sort-verbs precedent directly below: the ceiling cannot express
# per-request data, and three rungs cover the daily/weekly shapes the
# ±half-cycle bound admits (see ``routine.recurrence.backdate_credit_window``).
# Longer cycles (monthly, ~15d half-cycle) wait on the LEDGERED pick-a-date
# wire extension; the ``backdate_limit_days`` producer stamp is the seam it
# plugs into.
BACKDATE_DONE_ACTIONS: dict[str, int] = {
    "done_1d": 1,
    "done_2d": 2,
    "done_3d": 3,
}
# The completion FAMILY — the plain verb plus its dated rungs. Every gate that
# asks "is this act a completion?" or "was this item completed from the
# board?" consults THIS, never ``DONE_ACTION`` alone: a rung IS a completion
# (same writer, same lane, a different date), and a family member missed by a
# ``== DONE_ACTION`` comparison is a gate silently gone dark for backdates —
# the accepted-item exception and the snooze-on-done refusal both route
# through it below.
DONE_FAMILY: tuple[str, ...] = (DONE_ACTION, *BACKDATE_DONE_ACTIONS)
# slot_suggestion ACCEPT action (Phase C slice 2 — board day-planning). Commits
# an auto-surfaced candidate onto today's tier list via the tier_confirm writer.
# Gated: accept ONLY when ``evidence.candidate is True`` (a committed item →
# invalid_action, the provenance guard).
ACCEPT_ACTION = "accept"
SLOT_KIND = "slot_suggestion"

# slot_suggestion SORT actions (2026-08-19 — the operator's "no way of being
# sorted" report). Three verbs because the slots are three, and CO-EQUAL because
# the taxonomy says so: ``tier/slots.py`` and the board's voice doctrine both
# state that Duty / Rhythm / Fuel are a permission system rather than a priority
# stack. So there is no "yes" among them and none of them takes a gesture — see
# the ACTION_META entry below, where that decision is made visible.
#
# DISTINCT FROM ``accept``, and the distinction is the same one DEFER_ACTIONS
# draws against ``snooze_*``: ``accept`` commits an item to today's tier list
# (``confirm_slot_candidate`` writes ``tier_curation`` and assigns NO slot);
# a sort writes the SLOT axis onto the item's backing record and touches the day
# plan not at all. Tier and slot are orthogonal — one verb meaning both would be
# the same-concept-divergent-constants trap wearing sorting clothes.
#
# The verb id carries the slot so the ceiling stays a static map: a single
# ``sort`` verb would need the target slot as per-request data, which the
# ceiling cannot express (the ``correct``/``correction_target`` exception exists
# precisely because that shape is awkward, and it is not worth repeating for a
# closed set of three).
SORT_ACTION_BY_SLOT: dict[str, str] = {
    "sort_duty": slots.SLOT_DUTY,
    "sort_rhythm": slots.SLOT_RHYTHM,
    "sort_fuel": slots.SLOT_FUEL,
}
SORT_ACTIONS: tuple[str, ...] = tuple(SORT_ACTION_BY_SLOT)

# R3 board snooze. Imported from the tier layer so the duration ladder has ONE
# definition — the ceiling above, this dispatch, and the store all read it.
from alfred.tier.snooze import (  # noqa: E402
    SNOOZE_DURATIONS as SNOOZE_ACTIONS,
)
from alfred.tier.snooze import (  # noqa: E402
    UNSNOOZE_ACTION,
)

SNOOZE_ACTED_VERB = "snooze"
STATUS_ALREADY_DONE = "already_done"

# email_urgent (#27 slice 1) — the classify-time high-priority interrupt kind
# (curator emits it per-item; distinct from the sync-time email_tier card). It is
# MODE_DECIDE (deals into the deck + rings/needs-you) but its ONLY action is
# ``ack`` → acted. Unlike a FYI ack (which sets ``acked``), acking an urgent item
# = deciding it (the operator has seen the interrupt); there is no resolver and no
# last_batch (it was never part of a sync batch).
URGENT_KIND = "email_urgent"

# reminder_returned (T2-1) — a snoozed/waiting task's reminder came due and the
# transport scheduler dealt a card for it. Same SHAPE as email_urgent and for
# the same reasons: MODE_DECIDE (it deals into the deck and rings the phone),
# emitted per-item by something that is not a sync producer (so there is no
# last_batch to re-derive from), and its only verb is ``ack`` → acted.
#
# It carries the generic defer verbs and — unlike email_urgent — that is a
# promise it can keep: ``transport.returns_feed.sweep_return_cards`` reconciles
# this kind on every scheduler tick, and a reconcile is the ONE thing that
# returns a lapsed defer (``_revival_suppressed`` is called from nowhere else).
# The name is the feed's own (imported at the top of this module), so the kind
# has one spelling across the three packages that have to say it.
RETURN_KIND = KIND_REMINDER_RETURNED

# sort_suggestion (the deck rotation, 2026-08-19) — the card that PROPOSES a
# slot for an item the classifier refused to guess at, dealt by the brief's
# rotation (``brief.daemon._emit_sort_rotation``). Same one-spelling discipline
# as RETURN_KIND: the feed model owns the string.
#
# ITS VERBS ARE THE SAME THREE SORT VERBS the board's slot_suggestion carries —
# same ids, same writer (``tier.sort_writer.assign_slot``), because both
# surfaces record the same ruling on the same record field. What differs is the
# LIFECYCLE: a board sort is orthogonal to its card (the item stays open, still
# completable), while a rotation card's WHOLE question is "where does this
# belong?" — answering it decides the card, so the rotation dispatcher sets
# ``acted`` where the board one deliberately touches nothing.
SORT_SUGGESTION_KIND = KIND_SORT_SUGGESTION

# The MOC reader (2026-08-20). ONE verb, and the target rides as PER-REQUEST
# DATA — the shape this module's own sort comment named and declined:
#
#   "a single ``sort`` verb would need the target slot as per-request data,
#    which the ceiling cannot express (the ``correct``/``correction_target``
#    exception exists precisely because that shape is awkward, and it is not
#    worth repeating FOR A CLOSED SET OF THREE)."
#
# MOC targets are an OPEN set — every non-inventory MOC in the vault, and the
# vault grows — which is the condition that comment carved out. A verb per MOC
# is not expressible in a static ceiling at all, so this kind takes the
# correction_target shape rather than the sort shape. The ceiling stays honest:
# ONE verb id, ONE capability, and the open set lives in data where it belongs.
MOC_SUGGESTION_KIND = KIND_MOC_SUGGESTION
MOC_APPLY_ACTION = "moc_apply"

# Voice calibration (R4, 2026-08-21). The operator's ruling was "a feed-family
# card with plain confirm/reject", and the LABELS are exactly that — but the
# VERB IDS are kind-specific, following ``moc_apply`` rather than borrowing
# ``attribution``'s / ``proposal``'s ``confirm``/``reject``.
#
# WHY DISTINCT IDS. Those two kinds' ``confirm``/``reject`` fall through to the
# ReplyCorrection synthesis at the bottom of ``_act_locked``; calibration's do
# not — they are intercepted and routed straight into the store's approve/reject.
# Sharing an id would leave exactly one edit (a mis-ordered interception) between
# a calibration confirm and a code path that reads ``last_batch`` and synthesizes
# a correction for a different subsystem. A distinct id makes that unreachable
# through the ceiling itself rather than through the ordering of if-statements.
#
# DELIBERATELY NOT ``CONTEST_ACTION``, which is shaped like a reject and means
# something else: a contest carries ``contested_section`` bound to
# ``CONTEST_SECTIONS`` (drift-pinned to ``telegram.capture_sections
# .SUMMARY_SECTIONS``) and says "this inference is wrong, and THIS capture
# section produced it". A wording proposal has no capture section, so borrowing
# it would drag a meaningless picker onto the card. Shape is not meaning.
CALIBRATION_KIND = KIND_CALIBRATION
#: NOT the same thing as ``vault.scope.CALIBRATION_APPLY_SCOPE`` (scope.py),
#: despite the shared words. This is a FEED VERB ID — a member of the
#: ``FEED_ACTIONS`` capability ceiling, matched against an inbound act's
#: ``action_id``. That is a VAULT SCOPE NAME, matched against ``SCOPE_RULES``
#: when the write finally happens. Two registries, two match sites, no code path
#: compares them, and neither would do the other's job if swapped. They are
#: parallel names for the two ends of one feature — the tap and the write — and
#: the parallelism is deliberate; an equivalence between them is not.
CALIBRATION_APPLY_ACTION = "calibration_apply"
CALIBRATION_DISCARD_ACTION = "calibration_discard"

#: The operator identity recorded when a ruling arrives from the FEED CARD
#: rather than from the CLI's ``--operator``.
#:
#: A literal rather than a blank, and rather than a fabricated person name. The
#: store REFUSES a blank operator by design — that guard is what keeps anonymous
#: calibration writes impossible — so the card has to supply something, and what
#: it supplies should say honestly which door the decision came through. A card
#: act is already authenticated (it arrives on a web session token), so this is
#: provenance, not a bypass: nobody typed a name, and the row should not claim
#: they did.
FEED_CARD_OPERATOR = "feed_card"

# (kind, action_id) → the kwargs that synthesize the owning resolver's
# ``ReplyCorrection``. THIS MAP IS THE CAPABILITY CEILING — a pair absent here
# can never reach a resolver. A fresh ReplyCorrection is built per call (never a
# shared mutable instance) from these kwargs.
# --- the generic defer verbs (#102 1b-ii) ------------------------------------
# The vertical gesture's capability, for every decide kind that is NOT a board
# slot. DISTINCT IDS FROM ``snooze_*`` ON PURPOSE, and the distinction is
# load-bearing rather than cosmetic: a slot ``snooze_*`` writes the tier.snooze
# SIDECAR and marks the feed item ``acted``; a ``defer_*`` writes the FEED STORE
# and marks it ``deferred`` with a window. Different store, different state,
# different return mechanism. One action_id meaning either depending on kind
# would be the same-concept-divergent-constants trap wearing verb clothing.
#
# THERE IS NO ``defer_until_i_say``, and its absence is a DESIGN CONSTRAINT of
# the store rather than an omission. ``FeedStore.defer`` takes exactly two
# shapes — a window, or none (next render) — because the model deliberately
# separates a defer from a sticky ack: "a deferred item ALWAYS has a return
# date, and defer_window_open is the single place that decides it has arrived"
# (feed/store.py). An indefinite rung would need a sentinel far-future date,
# which is a lie the return-date invariant exists to prevent. The board keeps
# its own indefinite rung because its sidecar was built to hold one.
DEFER_NEXT_RENDER = "defer"
#: verb id -> days held. The dated rungs mirror the board ladder's shape so the
#: two surfaces read alike, without sharing its ids or its store.
DEFER_DURATIONS: dict[str, int] = {
    "defer_1d": 1,
    "defer_3d": 3,
    "defer_7d": 7,
}
#: Every generic defer verb, in menu order (quick defer first).
DEFER_ACTIONS: tuple[str, ...] = (DEFER_NEXT_RENDER, *DEFER_DURATIONS)

#: Kinds that get the generic defer capability. Every decide kind EXCEPT
#: ``slot_suggestion``, which keeps its board snooze semantics untouched —
#: giving it both would put two defer mechanisms on one card.
#:
#: ``pattern_surfaced`` (C4) is excluded for the SAME reason with a different
#: store: its ``ignore`` verb is a windowed set-aside written to the contact
#: router's own suppression map (the window is the operator's own
#: ``pattern_surfacing.window_days``, not this ladder's 1/3/7), and it is what
#: stops the pattern being re-detected and re-dealt. A generic defer beside it
#: would be a second set-aside that the detector does not consult — the card
#: would return on the next override regardless, which is a promise broken
#: rather than a rung offered.
#: ``email_urgent`` WAS excluded here, for a third reason the other two make
#: obvious in hindsight: nothing reconciled it, so a deferred urgent card was
#: terminal — off every open query with nothing left to bring it back. That was
#: a fail-safe close, explicitly not the fix. The fix now exists
#: (``alfred.curator.urgent_feed``, swept on the curator tick that hosts the
#: classifier), so the verbs return and the kind moves to the OTHER branch of
#: the partition below. The withdrawal and the re-admission are the same
#: argument read in two directions: a kind may carry a defer exactly when
#: something can bring it back.
DEFER_EXCLUDED_KINDS: frozenset[str] = frozenset(
    {"slot_suggestion", "pattern_surfaced"}
)

#: WHERE A DEFERRED CARD OF THIS KIND COMES BACK FROM — the other half of the
#: partition, and the reason a future producer cannot repeat #102's mistake by
#: accident.
#:
#: A defer is a PROMISE: the item is not judged, only moved, so it MUST return.
#: The only mechanism that returns one is a ``reconcile`` pass over the kind
#: (``_revival_suppressed`` holds the item while ``defer_window_open`` is true
#: and the upsert returns it when the window lapses). A kind that carries the
#: defer verbs with no reconciler makes a promise the system cannot keep — which
#: is exactly what shipped for ``email_urgent`` and is now excluded above.
#:
#: So every kind carrying the verbs must name its return path here, and
#: ``tests/feed/test_defer_return_path.py`` VERIFIES each claim against the
#: named source rather than trusting it: the ``feed_producer`` entries against
#: the live ``_FAMILIES`` registry, the ``brief`` entries against the
#: ``feed_kind=`` literals in that module, ``transport.returns_feed`` against the
#: ``try_feed_reconcile`` call it really makes. The partition itself — every
#: defer-carrying kind is either declared here or excluded above, never both and
#: never neither — is asserted by the same test, so a kind added to
#: ``FEED_ACTIONS`` (which gains the verbs by the auto-fold below) fails until
#: its author has answered the question.
DEFER_RETURN_PATH: dict[str, str] = {
    "email_urgent": "alfred.curator.urgent_feed",
    "email_tier": "alfred.daily_sync.feed_producer",
    "attribution": "alfred.daily_sync.feed_producer",
    "proposal": "alfred.daily_sync.feed_producer",
    "pending": "alfred.daily_sync.feed_producer",
    "routine_match": "alfred.daily_sync.feed_producer",
    "reminder_returned": "alfred.transport.returns_feed",
    # The deck rotation (2026-08-19). Its defer IS the operator's "not now"
    # (the ruling maps the reject swipe onto the quick defer), so the return
    # promise is load-bearing here, not incidental: ``_emit_sort_rotation``
    # reconciles the kind every brief fire, carries every still-deferred card
    # in its emit OUTSIDE the cap (the selection's held band — omitting one
    # would let reconcile retire it and destroy its window), and a lapsed
    # window revives through the same upsert every other kind uses.
    KIND_SORT_SUGGESTION: "alfred.brief.daemon",
    # Same emitter, same reconcile-carries-held-cards discipline as the
    # rotation above — ``moc_suggestion_feed_items`` carries every deferred
    # row OUTSIDE its cap, so a parked card keeps its window.
    KIND_MOC_SUGGESTION: "alfred.brief.daemon",
    # calibration (R4) — the daily-sync producer re-emits the PENDING store
    # every fire, so a deferred card returns when its window lapses exactly as
    # the other daily-sync families do. The promise is keepable because the
    # store is durable: a proposal is removed from the open set only by an
    # operator decision, never by time.
    KIND_CALIBRATION: "alfred.daily_sync.feed_producer",
}


FEED_ACTIONS: dict[str, dict[str, dict[str, Any]]] = {
    "email_tier": {
        "high": {"new_tier": "high"},
        "medium": {"new_tier": "medium"},
        "low": {"new_tier": "low"},
        "spam": {"new_tier": "spam"},
        "up": {"modifier": "up"},
        "down": {"modifier": "down"},
        "confirm": {"ok": True},
    },
    # attribution — confirm/reject synthesize a ReplyCorrection as ever. #63a's
    # ``contest`` does NOT: like the slot verbs, its kwargs are UNUSED and it is
    # intercepted by :func:`_dispatch_attribution_contest` below. It is listed
    # here because this map is the capability ceiling, and an action absent from
    # it can never reach any handler.
    "attribution": {
        "confirm": {"ok": True},
        "reject": {"reject": True},
        CONTEST_ACTION: {},
    },
    "proposal": {
        "confirm": {"ok": True},
        "reject": {"reject": True},
    },
    # routine_match — #13 reject-with-correction. ``correct`` and ``one_off`` are
    # both REJECTS that carry more than "not that one": ``correct`` needs the
    # operator's chosen item, supplied out-of-band as the act's
    # ``correction_target`` and injected below (the kwargs here can't hold it —
    # the target is per-request, the ceiling is static); ``one_off`` needs
    # nothing and declares the phrase meaningless. The target is injected for
    # THIS pair only, so no other (kind, action) can smuggle one in.
    "routine_match": {
        "confirm": {"ok": True},
        "reject": {"reject": True},
        "correct": {"reject": True},
        "one_off": {"reject": True, "one_off": True},
    },
    "pending": {
        "noted": {"ok": True, "consumed_token": "noted"},
        "show": {"ok": True, "consumed_token": "show"},
    },
    # slot_suggestion (board DONE + ACCEPT paths) — the capability-ceiling
    # declaration. Unlike the five families above, the kwargs here are UNUSED: a
    # slot completion/accept does NOT synthesize a ReplyCorrection or read
    # last_batch. It is intercepted by :func:`_dispatch_slot_completion`
    # (the done family — done + the dated done_Nd rungs — and undo_done) or
    # :func:`_dispatch_slot_confirm` (accept) right after
    # this ceiling check, which call the per-lane writer directly against the
    # feed item's OWN stamped evidence (origin/routine_record/tier/item_text).
    # snooze_* / unsnooze (R3): kwargs UNUSED, same as done/accept. Intercepted
    # by :func:`_dispatch_slot_snooze`, which is a SEPARATE dispatcher from
    # `_dispatch_slot_completion` on purpose — no completion writer is
    # reachable from a snooze under any input, so "snooze never fakes a
    # completion" is enforced structurally rather than by convention.
    # ``snooze_until_i_say`` is #14's fourth rung — the old board Park, now a
    # duration choice on the one defer verb rather than a second verb with its
    # own semantics. Same dispatcher, same store, no end date.
    # sort_duty / sort_rhythm / sort_fuel: kwargs UNUSED like every other slot
    # verb, intercepted by :func:`_dispatch_slot_sort` — a THIRD dispatcher
    # beside completion and snooze, for the same structural reason those two are
    # separate: no completion, accept or snooze writer is reachable from a sort
    # under any input, so "sorting never fakes a commitment" holds by
    # construction rather than by convention.
    "slot_suggestion": {
        "done": {},
        # The backdated rungs, in menu order right after the plain verb — the
        # when-family the hold-selector renders (done = Today is the suggested
        # member). kwargs UNUSED like every slot verb; intercepted by
        # :func:`_dispatch_slot_completion`, which maps the verb to its offset,
        # enforces the credit-window bound, and calls the SAME per-lane writer
        # with the chosen date. Routine lane only in v1 — the bound derives
        # from the recurrence grammar, which the task/tier lanes don't carry.
        **{_verb: {} for _verb in BACKDATE_DONE_ACTIONS},
        "undo_done": {},
        "accept": {},
        "snooze_1d": {},
        "snooze_3d": {},
        "snooze_7d": {},
        "snooze_until_i_say": {},
        "unsnooze": {},
        **{_verb: {} for _verb in SORT_ACTIONS},
    },
    # email_urgent (#27 slice 1) — ack-only capability ceiling. Like slot's, the
    # kwargs are UNUSED: an ``email_urgent`` ack does NOT synthesize a
    # ReplyCorrection or read last_batch. It is intercepted by :func:`_act_locked`
    # (see the URGENT_KIND block) which sets the item ``acted`` directly. Any
    # action other than ``ack`` is invalid — the ceiling stays closed.
    "email_urgent": {
        "ack": {},
    },
    # pattern_surfaced (C4) — the contact-surface router's propose-only card.
    # Like slot's and email_urgent's, the kwargs are UNUSED: it synthesizes no
    # ReplyCorrection and reads no last_batch. It is intercepted by
    # :func:`_dispatch_contact_pattern`, which writes the contact router's own
    # store and nothing else.
    #
    # TWO verbs, and the ceiling is closed at two on purpose. The spec's card
    # offers four adjustments; "Dismiss" and "Ignore for N days" are the same
    # act once a suppression window backs both, and "Add inferred condition"
    # edits the operator's rule set rather than the router's behaviour — that
    # is a change to the policy, not a use of it, and it is declared unbuilt on
    # the card itself rather than stubbed here.
    "pattern_surfaced": {
        "adopt": {},
        "ignore": {},
    },
    # reminder_returned (T2-1) — ack-only, same shape as email_urgent's entry
    # above: kwargs UNUSED, no ReplyCorrection, no last_batch, intercepted in
    # :func:`_act_locked` (see the RETURN_KIND block). Acking = "seen, dealt
    # with" → acted, not the FYI ``acked`` state.
    #
    # ONE verb, deliberately. The operator's real moves on a returned reminder
    # are moves on the TASK — finish it, push it again — and both retire the
    # card by themselves through the sweep. A card verb that also wrote the
    # record would be a second writer for a state the record already owns.
    RETURN_KIND: {
        "ack": {},
    },
    # sort_suggestion (the deck rotation) — the SAME three sort verbs as the
    # board's slot_suggestion, intercepted by :func:`_dispatch_sort_ruling`
    # (kwargs UNUSED, no ReplyCorrection, no last_batch — the brief's rotation
    # emits per-item). The auto-fold below adds the generic defer family: this
    # kind is deliberately NOT in DEFER_EXCLUDED_KINDS, because the operator's
    # reject-swipe on a rotation card MEANS "not now", and the defer store is
    # the mechanism that keeps that promise (return path declared in
    # DEFER_RETURN_PATH, verified by the partition test).
    SORT_SUGGESTION_KIND: {
        **{_verb: {} for _verb in SORT_ACTIONS},
    },
    # moc_suggestion (the MOC reader) — ONE verb, intercepted by
    # :func:`_dispatch_moc_apply` (kwargs UNUSED, no ReplyCorrection, no
    # last_batch — the brief emits per-item like the rotation). The chosen
    # MOC arrives as ``correction_target``; see the scoping block in
    # :func:`_act_locked`, which admits the payload for exactly this pair.
    # Deliberately NOT in DEFER_EXCLUDED_KINDS: a reject-swipe on a
    # suggestion card MEANS "not now", and the defer store keeps that
    # promise (return path declared in DEFER_RETURN_PATH).
    MOC_SUGGESTION_KIND: {
        MOC_APPLY_ACTION: {},
    },
    # calibration (R4) — TWO verbs, the operator's plain confirm/reject. kwargs
    # UNUSED like the four kinds above: intercepted by
    # :func:`_dispatch_calibration_ruling`, which synthesizes no ReplyCorrection
    # and reads no last_batch. The apply verb routes into the SAME
    # ``calibration_store.approve_proposal`` the CLI verb uses — one store, two
    # front doors, and in particular the same no-blank-operator and
    # no-calibration-block refusals. There is no third verb: a calibration card
    # has no "which part was wrong" question to ask.
    CALIBRATION_KIND: {
        CALIBRATION_APPLY_ACTION: {},
        CALIBRATION_DISCARD_ACTION: {},
    },
}

# The generic defer capability, folded into the ceiling for every eligible kind.
# WIDENED HERE rather than typed into each kind's literal so the eligibility rule
# has one author: a kind added to FEED_ACTIONS above gets defer automatically
# unless it is excluded, and the exclusion is a named set rather than an omission
# someone has to notice.
for _kind in FEED_ACTIONS:
    if _kind in DEFER_EXCLUDED_KINDS:
        continue
    for _verb in DEFER_ACTIONS:
        FEED_ACTIONS[_kind][_verb] = {}
del _kind, _verb

# --- advertised verbs (#102 1b) ----------------------------------------------
# What a CLIENT is told it may do with an item, derived from the ceiling above.
#
# WHY THIS EXISTS. The web deck used to carry its own hand-written per-kind verb
# map (`web/lib/algernon/feedConstants.DECK_VERBS`) whose own comment conceded
# the coupling: "these action_ids MUST be members of the B1 transport
# FEED_ACTIONS map for the kind". A mirror maintained by vigilance drifts the day
# someone edits one side, and the drift is silent in both directions — a verb the
# client offers that the ceiling refuses 400s in the operator's hand, and a verb
# the ceiling gained that the client never shows is a capability nobody can
# reach. Serving the verbs from the ceiling itself is F3 "fixed by construction".
#
# THAT MAP IS NOW DELETED (1b-ii): the deck derives its verbs from the served
# `actions[]` via `feedConstants.verbsFromActions`, so there is no second table
# left to drift. Consequences for anyone editing below — a verb added here
# reaches the deck with no client change, and a verb REMOVED here disappears from
# the deck the same way. The client can no longer paper over a gap in this table,
# which is the point; it also means this table is now load-bearing for the UI, so
# an accidental deletion here is an accidental capability removal there.
#
# THE CEILING IS THE SET; THIS TABLE IS THE PRESENTATION. `FEED_ACTIONS` decides
# WHICH pairs exist and is the only authority on that. This adds what a ceiling
# entry cannot know: the operator-facing label, the gesture weight, the gesture
# direction, and (for a heavy verb) the consequence sentence. An action present
# in the ceiling with no entry here is still advertised — with its raw id as the
# label and no gesture — because dropping it would make this table a second,
# quieter ceiling, which is the failure being removed.
#
# GESTURE IS AN ADDITION TO THE §4 CONTRACT, and a necessary one. §4 specifies
# ``{verb, label, weight, effect_ref}`` — no direction. A two-way swipe surface
# cannot be generated from that: nothing in the payload says `spam` is the LEFT
# swipe and `confirm` the right. Adding the direction here rather than inferring
# it client-side (by verb NAME, the obvious shortcut) is what keeps the deck from
# re-growing a private opinion about verbs it was just relieved of.
_GESTURE_AFFIRM = "affirm"
_GESTURE_REJECT = "reject"
# The vertical family. Its own direction, never folded into reject: "later" and
# "no" are opposite answers and the #14 ruling forbids one control meaning both.
_GESTURE_DEFER = "defer"

# weight: "light" commits on the gesture, inside the undo window. "heavy" ARMS,
# showing `note`, and commits on a second tap — a mutation-bearing verb never
# fires on a single motion (step-2 §3). The weights here are the catalog's,
# verb-for-verb, and they are the SAME judgements the web deck now renders.
ACTION_META: dict[str, dict[str, dict[str, Any]]] = {
    "email_tier": {
        "confirm": {"label": "Confirm", "weight": "light", "gesture": _GESTURE_AFFIRM},
        "spam": {"label": "Spam", "weight": "light", "gesture": _GESTURE_REJECT},
    },
    "attribution": {
        "confirm": {"label": "Confirm", "weight": "light", "gesture": _GESTURE_AFFIRM},
        # HEAVY, and the one this table was worth building for: the reject
        # strips the marked section out of the record body and drops its audit
        # entry (:func:`alfred.vault.attribution.reject_marker`). It shipped as
        # a light verb and committed on a single left swipe.
        "reject": {
            "label": "Reject",
            "weight": "heavy",
            "gesture": _GESTURE_REJECT,
            "note": (
                "Removes the marked section from the record body and drops its "
                "audit entry."
            ),
        },
    },
    "proposal": {
        "confirm": {
            "label": "Confirm",
            "weight": "heavy",
            "gesture": _GESTURE_AFFIRM,
            "note": "Creates or merges the vault records described above.",
        },
        "reject": {"label": "Reject", "weight": "light", "gesture": _GESTURE_REJECT},
    },
    "routine_match": {
        "confirm": {"label": "That's it", "weight": "light", "gesture": _GESTURE_AFFIRM},
        "reject": {"label": "No", "weight": "light", "gesture": _GESTURE_REJECT},
    },
    "pending": {
        "noted": {"label": "Noted", "weight": "light", "gesture": _GESTURE_AFFIRM},
    },
    "slot_suggestion": {
        "accept": {"label": "Take it", "weight": "light", "gesture": _GESTURE_AFFIRM},
        # The completion WHEN-family (backdated completion, 2026-08-20). Group
        # marks the co-equal choice set the board's ✓-hold selector renders:
        # Today is the plain verb's answer (the suggested member — quick tap
        # semantics unchanged), the dated rungs are the "previously done"
        # alternatives the operator ruled for.
        #
        # NO GESTURE ON ANY MEMBER, and that absence is load-bearing twice
        # over: (1) ``verbsFromActions`` promotes the FIRST gesture-matching
        # served verb to the deck's swipe — ``done`` precedes ``accept`` in
        # ceiling order, so a gesture here would silently hijack the slot
        # card's affirm from Take-it to Done; (2) the family's anchor on the
        # board is a BUTTON, not a swipe direction — the client derives the
        # selector by VERB (``holdChoicesForVerb('done')``), the
        # verb-anchored evolution of the train-18 hold primitive. LIGHT: a
        # co-equal choice commits on pick (the selector IS the deliberate
        # step), and undo_done reverses it.
        "done": {"label": "Today", "weight": "light", "group": "when"},
        "done_1d": {"label": "Yesterday", "weight": "light", "group": "when"},
        "done_2d": {"label": "2 days ago", "weight": "light", "group": "when"},
        "done_3d": {"label": "3 days ago", "weight": "light", "group": "when"},
        # The sort verbs. LABELLED so they do not ship under raw ids, and
        # deliberately GESTURE-FREE — which on this surface means "menu verb,
        # not swipe verb" (`verbsFromActions`). Two reasons, and the first is
        # the operator's own ruling rather than a UI convenience:
        #
        #   1. The three slots are CO-EQUAL. A swipe surface has an affirm and a
        #      reject, so putting one slot on a gesture would make it the "yes"
        #      and the others the alternatives — the priority stack the taxonomy
        #      explicitly is not. There is no honest two-way reading of a
        #      three-way co-equal choice.
        #   2. `slot_suggestion` already ships EVERY verb but `accept`
        #      gesture-free (they belong to the board and the hold menu, not to
        #      a swipe). These are the same case, for a different reason.
        #      Deliberately not stated as a count: the first draft of this line
        #      said "seven of its eight", which the three verbs directly below
        #      it made false in the same commit.
        #
        # LIGHT, not heavy: a sort is reversible by re-sorting, writes one
        # frontmatter field, and destroys nothing — an arm stage would spend a
        # tap protecting a decision the operator can simply make again.
        "sort_duty": {"label": "Duty", "weight": "light"},
        "sort_rhythm": {"label": "Rhythm", "weight": "light"},
        "sort_fuel": {"label": "Fuel", "weight": "light"},
    },
    "email_urgent": {
        "ack": {"label": "Got it", "weight": "light", "gesture": _GESTURE_AFFIRM},
    },
    # reminder_returned (T2-1). LIGHT and affirm-gestured: the ack writes
    # nothing outside the feed store — the task record is untouched — so there
    # is nothing for an arm stage to show. Without this entry the card would
    # still be advertised (the ceiling is the authority), but with a raw-id
    # label and NO gesture, which the deck reads as "not a swipe verb": the
    # card would be dealt with no way to clear it.
    RETURN_KIND: {
        "ack": {"label": "Got it", "weight": "light", "gesture": _GESTURE_AFFIRM},
    },
    "pattern_surfaced": {
        # HEAVY: this is the only tap in the system that changes what the router
        # opens to. Everything else in C4 observes; this decides. A verb that
        # alters future behaviour must not commit on one motion.
        "adopt": {
            "label": "Adopt",
            "weight": "heavy",
            "gesture": _GESTURE_AFFIRM,
            "note": (
                "Opens the surface you keep choosing whenever this rule fires, "
                "from your next contact on. Nothing in your preference record "
                "changes."
            ),
        },
        "ignore": {
            "label": "Ignore",
            "weight": "light",
            "gesture": _GESTURE_REJECT,
        },
    },
    # sort_suggestion (the deck rotation) — the affirm-with-hold-modifier
    # pattern's first consumer (operator ratification, 2026-08-19: "affirm as
    # suggested, or swipe hold to change the suggestion while affirming").
    #
    # NO GESTURE HERE, and that absence is the taxonomy holding: the three
    # slots are CO-EQUAL, so no verb may be the kind-level "yes". The affirm
    # gesture is stamped PER ITEM by :func:`actions_for_item`, onto whichever
    # verb matches that card's own ``evidence.proposed_slot`` — the "yes" is
    # THE PROPOSAL, never a slot. LIGHT for the same reasons as the board's
    # copy of these verbs: reversible by re-sorting, one frontmatter field,
    # destroys nothing.
    #
    # ``group`` is the §4 amendment this pattern adds (mirroring how the deck's
    # two-way surface needed ``gesture``): it marks a CO-EQUAL CHOICE GROUP.
    # The client derives the hold-selector's option list as "every served verb
    # sharing the affirm verb's group" — so a future suggestion-bearing kind
    # gets the selector by serving a group, with no client change and no
    # per-kind map (the 1b-ii lesson, kept).
    SORT_SUGGESTION_KIND: {
        "sort_duty": {"label": "Duty", "weight": "light", "group": "slot"},
        "sort_rhythm": {"label": "Rhythm", "weight": "light", "group": "slot"},
        "sort_fuel": {"label": "Fuel", "weight": "light", "group": "slot"},
    },
    # moc_suggestion (the MOC reader) — the affirm-with-hold pattern's SECOND
    # consumer, and its first OPEN-SET one.
    #
    # THE GESTURE IS ON THE VERB, unlike the rotation. There, three co-equal
    # slots meant no verb could be the kind-level "yes", so the affirm was
    # stamped per item onto whichever verb matched the proposal. Here there is
    # exactly ONE verb and the co-equal family lives in per-item DATA, so the
    # affirm is a property of the kind and can be declared statically. The
    # pattern's grammar is unchanged — affirm accepts the suggestion, hold
    # changes it, choosing IS the affirm — only the family's carrier moved.
    #
    # ``group`` still marks the co-equal choice group, and the client still
    # derives "every served verb sharing the affirm verb's group". A family of
    # one is no family, so on THIS kind that static derivation yields nothing
    # and the client falls through to the dynamic arm (``evidence.moc_choices``).
    # The static path is untouched for every existing consumer.
    #
    # LIGHT, and that is the pattern's contract rather than a risk assessment:
    # a co-equal choice is light because the SELECTOR is the deliberate step
    # (``useDeck.affirmWith`` has no arm stage by construction). Making the
    # plain affirm heavy would give one control two different safety levels
    # depending on which route reached it. What protects a multi-record write
    # instead is the face — the card states N before the swipe — and the
    # deferred act: the POST fires 3.5s after the gesture and UNDO cancels it
    # outright, so a mistaken apply inside the window writes nothing at all.
    MOC_SUGGESTION_KIND: {
        MOC_APPLY_ACTION: {
            "label": "Add",
            "weight": "light",
            "gesture": _GESTURE_AFFIRM,
            "group": "moc",
        },
    },
    # calibration (R4). The operator's words are the LABELS; the ids beneath
    # them are kind-specific (see the constants block).
    #
    # NO ``group`` ON EITHER VERB, and that absence is the load-bearing part of
    # this entry rather than an omission. ``hasSuggestedChoice`` is
    # ``holdChoicesFor(item,'affirm') !== null``, and ``isDeckCandidate`` is
    # ``mode === 'decide' || hasSuggestedChoice`` — so a suggested choice group
    # would make this card DECK-ELIGIBLE, and the deck is a needs-you surface.
    # This card's home is the feed board's FYI column via a per-kind affordance.
    #
    # It takes a group shared by BOTH verbs to do it: ``holdChoicesForVerb``
    # requires ``family.length >= 2``, so one grouped verb is inert. Measured by
    # ``web/tests/calibrationCardQuiet.test.ts``, which pins both directions —
    # the grouped PAIR becomes deck-eligible, the lone grouped verb does not.
    #
    # HEAVY on the apply, light on the discard, matching ``attribution``'s
    # asymmetry for the same reason read in the other direction: the apply
    # WRITES the operator's own person record (a body edit plus an audit entry),
    # while the discard writes one JSONL row and destroys nothing recoverable —
    # the observation is already in the analyzer's reach if it recurs.
    CALIBRATION_KIND: {
        CALIBRATION_APPLY_ACTION: {
            "label": "Confirm",
            "weight": "heavy",
            "gesture": _GESTURE_AFFIRM,
            "note": (
                "Adds this line to the calibration block on your person record "
                "and stamps an attribution-audit entry."
            ),
        },
        CALIBRATION_DISCARD_ACTION: {
            "label": "Reject",
            "weight": "light",
            "gesture": _GESTURE_REJECT,
        },
    },
}

# Defer presentation, applied to every kind the ceiling widened. Light in every
# case: a defer is reversible by construction — the card comes back — so arming
# it would spend a tap protecting nothing. Only the QUICK defer carries the
# gesture; the dated rungs live behind the hold menu, which is a choice the
# operator has already opened rather than a direction they can swipe by accident.
_DEFER_LABELS: dict[str, str] = {
    DEFER_NEXT_RENDER: "Later",
    "defer_1d": "1 day",
    "defer_3d": "3 days",
    "defer_7d": "7 days",
}
for _kind in FEED_ACTIONS:
    if _kind in DEFER_EXCLUDED_KINDS:
        continue
    _slot = ACTION_META.setdefault(_kind, {})
    for _verb in DEFER_ACTIONS:
        _entry: dict[str, Any] = {"label": _DEFER_LABELS[_verb], "weight": "light"}
        if _verb == DEFER_NEXT_RENDER:
            _entry["gesture"] = _GESTURE_DEFER
        _slot[_verb] = _entry
del _kind, _verb, _slot, _entry

# The rotation card's REJECT is its quick defer (operator ruling, 2026-08-19:
# "reject swipe = not now"). Overridden AFTER the fold on purpose — the fold is
# the family's one author and this is a per-kind presentation of a verb the
# fold already granted, not a second grant. The #14 one-control-one-meaning
# rule holds: on this kind the left swipe means ONLY "not now" (there is no
# decline — an unsorted item stays unsorted either way), so the reject gesture
# carries the defer verb rather than sharing a card with it. The dated rungs
# keep their fold presentation (menu verbs, no gesture).
ACTION_META[SORT_SUGGESTION_KIND][DEFER_NEXT_RENDER] = {
    "label": "Not now",
    "weight": "light",
    "gesture": _GESTURE_REJECT,
}

# Same ruling, same shape, same reason on the MOC card: its left swipe means
# ONLY "not now" (there is no decline — an unapplied membership is unapplied
# either way), so the reject gesture carries the defer verb. Overridden AFTER
# the fold for the same one-author reason as the rotation's copy.
ACTION_META[MOC_SUGGESTION_KIND][DEFER_NEXT_RENDER] = {
    "label": "Not now",
    "weight": "light",
    "gesture": _GESTURE_REJECT,
}


def actions_for(kind: str) -> list[dict[str, Any]]:
    """The advertised verbs for ``kind`` — every ceiling pair, presented.

    Derived from :data:`FEED_ACTIONS` so the set can never exceed what the
    router will actually admit, and never lag a verb the ceiling gains. Order
    follows the ceiling's own declaration order, which is stable per kind.

    Returns ``[]`` for a kind with no ceiling entry — a real answer (that kind
    has no actions), not a missing one.
    """
    ceiling = FEED_ACTIONS.get(kind)
    if not ceiling:
        return []
    meta = ACTION_META.get(kind, {})
    out: list[dict[str, Any]] = []
    for action_id in ceiling:
        entry = meta.get(action_id, {})
        advertised: dict[str, Any] = {
            "verb": action_id,
            # An unlabelled ceiling action still ships, under its raw id. It is
            # visibly unfinished rather than invisibly absent.
            "label": entry.get("label") or action_id,
            "weight": entry.get("weight") or "light",
        }
        gesture = entry.get("gesture")
        if gesture:
            advertised["gesture"] = gesture
        note = entry.get("note")
        if note:
            advertised["note"] = note
        # A co-equal choice group (the affirm-with-hold-modifier pattern). Same
        # passthrough shape as gesture/note: presentation the ceiling entry
        # cannot know, carried only when declared.
        group = entry.get("group")
        if group:
            advertised["group"] = group
        out.append(advertised)
    return out


def actions_for_item(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The advertised verbs for ONE item — its kind's ceiling, minus the verbs
    THIS item's own state puts out of reach.

    :func:`actions_for` answers the per-KIND question, which is all the ceiling
    can express. Some refusals are per-ITEM: the router looks at the item's
    stamped evidence and declines. Advertising such a verb would put a control
    in the operator's hand that 400s when pressed — the exact failure the served
    verb list exists to remove — so the filtering belongs on the SERVE side,
    next to the ceiling it derives from, and NOT in the transport layer (a
    per-item rule written at the stamp site would drift from the gate it
    mirrors, which is how the client's private verb map went wrong).

    Takes the SERIALISED item mapping — the shape the read path already holds.

    WHAT IS FILTERED, and only this: ``slot_suggestion``'s ``accept``, which
    :func:`_dispatch_slot_confirm` refuses unless ``evidence.candidate is True``
    (a committed slot is already on today's plan — there is nothing to accept).

    WHAT IS DELIBERATELY NOT FILTERED:

    * ``done`` / ``undo_done`` — their per-item conditions (the completion-lane
      matrix, and undo's accepted-vs-done distinction) are a BOARDED follow-up.
      Advertising them unconditionally is the pre-existing behaviour, kept.
    * ``ack`` on ``email_urgent`` — and this one is a TRAP, not an oversight.
      The universal FYI gate in :func:`_act_locked` refuses ``ack`` on any
      non-FYI item, and ``email_urgent`` is MODE_DECIDE; a generalised "strip
      the verbs the gates would refuse" pass that consulted that gate would
      remove the ONLY verb from every urgent interrupt card. The urgent ack is
      intercepted BEFORE that gate on purpose (see the URGENT_KIND block), so
      the gate does not apply to it. Do not fold the FYI gate in here.

    The act-time gates STAY exactly as they are. This narrows what is OFFERED;
    it is not a substitute for refusing at the door. Defence in depth: a stale
    client, a hand-crafted POST, or an item whose state changed between the read
    and the tap all still meet the real gate.
    """
    kind = str(item.get("kind") or "")
    verbs = actions_for(kind)
    if kind == SLOT_KIND:
        evidence = item.get("evidence") or {}
        if not isinstance(evidence, Mapping) or evidence.get("candidate") is not True:
            verbs = [v for v in verbs if v.get("verb") != ACCEPT_ACTION]
        # Backdated rungs are served only where they are HONEST: the producer
        # stamps ``backdate_limit_days`` — how many days back this item's
        # credit window reaches (``routine.recurrence.backdate_credit_window``,
        # computed against the item's own due_pattern + completion_log at emit
        # time) — and a rung deeper than the stamp would be a control that
        # refuses when pressed. Task/tier lanes and unstamped/old payloads
        # carry 0, so they serve no rungs and the ✓ has no hold family (a
        # selector of one is forbidden by the pattern). Matched by TYPE like
        # the candidate flag above: a non-int stamp is no stamp. The act-time
        # bound in ``_dispatch_slot_completion`` stays the real gate — this
        # narrows what is OFFERED, defence stays at the door.
        raw_limit = evidence.get("backdate_limit_days") if isinstance(evidence, Mapping) else 0
        limit = raw_limit if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) else 0
        verbs = [
            v for v in verbs
            if v.get("verb") not in BACKDATE_DONE_ACTIONS
            or BACKDATE_DONE_ACTIONS[str(v.get("verb"))] <= limit
        ]
    if kind == SORT_SUGGESTION_KIND:
        # THE PROPOSAL BECOMES THE GESTURE (2026-08-19 ruling). The kind-level
        # table serves the three sort verbs gesture-free — co-equal, no static
        # "yes" — and each ITEM's affirm gesture is stamped here onto exactly
        # the verb matching its own ``evidence.proposed_slot``. So the swipe's
        # meaning is "accept the proposal", which is a fact about the card,
        # never a ranking among the slots.
        #
        # A card with a missing or unrecognised proposal gets NO affirm gesture
        # — it still serves its verbs (menu-reachable, honest) but the deck
        # will not deal a swipe whose meaning it cannot state. The producer
        # stamps a proposal on every card (the table is total), so this arm is
        # the degraded-payload belt, not a live path.
        evidence = item.get("evidence") or {}
        proposed = (
            evidence.get("proposed_slot") if isinstance(evidence, Mapping) else None
        )
        verb_for_slot = {v: k for k, v in SORT_ACTION_BY_SLOT.items()}
        proposed_verb = verb_for_slot.get(str(proposed or ""))
        if proposed_verb is not None:
            for advertised in verbs:
                if advertised.get("verb") == proposed_verb:
                    advertised["gesture"] = _GESTURE_AFFIRM
    return verbs


# kind → the ``reply_dispatch`` loader name for that family's last_batch list.
# Resolved via ``getattr(_rd, name)`` at call time (not pre-bound) so the
# lookup always reflects the live module.
_BATCH_LOADERS: dict[str, str] = {
    "email_tier": "_last_batch_items",
    "attribution": "_last_batch_attribution_items",
    "proposal": "_last_batch_proposal_items",
    "pending": "_last_batch_pending_items",
    "routine_match": "_last_batch_routine_match_items",
}


# --- per-item serialization (TOCTOU close) -----------------------------------
# The act path is load → folded-state check → resolver dispatch → set_state, and
# the transport runs it in a thread executor. Without serialization, two
# near-simultaneous acts on the SAME open item (a deck double-tap) both pass the
# ``open`` check and both dispatch the resolver — and the email resolver's
# ``append_correction`` is an UNCONDITIONAL append, so a duplicate corpus row
# lands. We serialize per feed_item_id with an IN-PROCESS keyed mutex held across
# the WHOLE critical section.
#
# NOT the store's ``file_rmw_lock``: a slow pending peer-dispatch act would then
# block producers' reconciles for seconds. In-process is sufficient under the
# SINGLE-ISSUER assumption — only THIS process's transport serves ``/feed/act``.
# If a second act-issuer (another process) is ever added, this MUST move to a
# cross-process lock on the store; the store internals already use file_rmw_lock.
#
# Reference-counted so the registry stays bounded to ids with an in-flight act:
# the refcount is bumped under the registry lock BEFORE the (blocking) acquire,
# so a concurrent releaser can't delete an entry another waiter still needs.
_registry_lock = threading.Lock()
_item_locks: dict[str, list] = {}  # feed_item_id -> [threading.Lock, refcount]


@contextlib.contextmanager
def _per_item_lock(feed_item_id: str) -> Iterator[None]:
    with _registry_lock:
        entry = _item_locks.get(feed_item_id)
        if entry is None:
            entry = [threading.Lock(), 0]
            _item_locks[feed_item_id] = entry
        entry[1] += 1
        lock = entry[0]
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _registry_lock:
            entry = _item_locks.get(feed_item_id)
            if entry is not None:
                entry[1] -= 1
                if entry[1] <= 0:
                    del _item_locks[feed_item_id]


def _dispatch_barrier(feed_item_id: str) -> None:
    """Test seam — a no-op in production. Exists ONLY so the concurrency pin can
    force two acts to overlap inside the critical section (proving the per-item
    lock serializes them). Never do work here."""
    return None


@dataclass
class ActResult:
    """Outcome of one feed act.

    ``ok`` is True for the applied paths (acted/acked) AND the idempotent noop
    (already_acted). ``detail`` is the human line — the resolver's OWN message
    verbatim where it has one (e.g. "record no longer exists"), else a short
    router line. ``status`` is the machine code the transport maps to HTTP.
    """

    ok: bool
    status: str
    detail: str = ""
    feed_item_id: str = ""
    action_id: str = ""
    # Optional committed-render payload for the FE's optimistic flip (Phase C
    # slice 2 accept — the C1 optimistic-green pattern). Carries the fields the
    # deck needs to render the candidate card transitioning to committed
    # (tier / name / committed) without a re-fetch. ``None`` for every other
    # action (additive, backward-compatible: absent from ``to_dict`` when None).
    render: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
            "id": self.feed_item_id,
            "action_id": self.action_id,
        }
        if self.render is not None:
            out["render"] = self.render
        return out


def _load_batch_item(kind: str, feed_item_id: str, config: Any) -> dict[str, Any] | None:
    """Locate the authoritative last_batch item whose stable key re-derives to
    ``feed_item_id`` — NOT by ordinal, NOT from feed evidence.

    Re-derives every last_batch item's feed id via the SAME key function the
    producer used (``_FAMILIES[kind][0]``) and matches the full id, so a key
    containing a colon is handled correctly. Returns ``None`` when the item has
    aged out of the batch (the caller maps that to ``stale_item``).
    """
    loader_name = _BATCH_LOADERS.get(kind)
    family = _FAMILIES.get(kind)
    if loader_name is None or family is None:
        return None
    key_fn = family[0]
    items = getattr(_rd, loader_name)(config)
    for raw in items:
        d = _as_dict(raw)
        key = key_fn(d)
        if key and make_id(kind, key) == feed_item_id:
            return d
    return None


def _dispatch(
    kind: str,
    correction: ReplyCorrection,
    item: dict[str, Any],
    *,
    config: Any,
    vault_path: Path | None,
    instance_name: str,
    instance_scope: str,
    raw_config: dict[str, Any] | None,
) -> tuple[str | None, str]:
    """Route the synthesized correction to the OWNING resolver and write.

    Returns ``(error_or_None, detail)``. The resolver's own error string is
    surfaced verbatim; ``detail`` is a short success line. Path helpers that can
    return ``None`` (feature-not-wired) and a missing ``vault_path`` are guarded
    HERE — the reference dispatcher buckets those as execution errors rather
    than passing ``None`` into a resolver.
    """
    if kind == "email_tier":
        entries, err = _rd._resolve_correction(
            correction, {_SYNTHETIC_ITEM_NUMBER: item},
        )
        if err is not None:
            return err, ""
        rows = 0
        for entry in entries or []:
            try:
                append_correction(config.corpus.path, entry)
                rows += 1
            except Exception as exc:  # noqa: BLE001 — one bad row shouldn't drop the rest
                log.warning(
                    "feed.act.corpus_write_failed",
                    record_path=getattr(entry, "record_path", ""),
                    error=str(exc),
                )
        if rows == 0:
            return "email calibration write failed — nothing recorded", ""
        tier = correction.new_tier or (entries[0].andrew_priority if entries else "")
        return None, f"email tier calibrated → {tier}" if tier else "email calibration recorded"

    if kind == "attribution":
        if vault_path is None:
            return "vault not configured — can't apply attribution", ""
        err, did_write = _rd._resolve_attribution_correction(
            correction, item, vault_path, _rd._attribution_corpus_path(config),
        )
        if err is not None:
            return err, ""
        verb = "confirmed" if correction.ok else "rejected"
        return None, f"attribution {verb}" if did_write else f"attribution already {verb} (no change)"

    if kind == "proposal":
        if vault_path is None:
            return "vault not configured — can't apply proposal", ""
        queue_path = _rd._canonical_proposals_queue_path(config)
        if queue_path is None:
            return "canonical proposals not configured on this instance", ""
        err, did_write = _rd._resolve_proposal_correction(
            correction, item, vault_path, queue_path, instance_scope=instance_scope,
        )
        if err is not None:
            return err, ""
        verb = "confirmed" if correction.ok else "rejected"
        return None, f"proposal {verb}" if did_write else f"proposal already {verb} (no change)"

    if kind == ROUTINE_MATCH_KIND:
        corpus_path = _rd._routine_match_corpus_path(config)
        if corpus_path is None:
            return "routine-match corpus not configured on this instance", ""
        # vault_path is what the resolver validates a #13 correction target
        # against. Passing it unconditionally (not only when a target is
        # present) keeps this call site honest about what the resolver needs —
        # a target that arrives with vault_path=None gets the resolver's own
        # refusal, not a silently unvalidated write.
        err, did_write = _rd._resolve_routine_match_correction(
            correction, item, corpus_path, vault_path=vault_path,
        )
        if err is not None:
            return err, ""
        # Report the WRITE, not the intent — same shape as attribution and
        # proposal above. The resolver has no idempotent no-op path today (it
        # returns did_write=True on every successful append), so this reads
        # identically in production right now; wiring it means a future no-op
        # branch can't silently show the operator a cheerful "rejected" over a
        # write that never landed. That failure mode is invisible from the
        # deck, which is exactly how a broken suppression loop survives weeks.
        # #13's richer verdicts sit UNDER the same gate for that reason.
        verb = "confirmed" if correction.ok else "rejected"
        if not did_write:
            return None, f"routine match already {verb} (no change)"
        if correction.correction_target:
            return None, f"noted — it means “{correction.correction_target}”"
        if correction.one_off:
            return None, "noted — a one-off, not a routine item"
        return None, f"routine match {verb}"

    if kind == "pending":
        # Pre-guard the fail-loud-on-empty-identity contract as a CLEAN error
        # rather than letting the resolver's ValueError crash the handler — the
        # refusal (never silently route as Salem) is preserved either way.
        if not (instance_name or "").strip():
            return "instance identity not configured — can't resolve pending item", ""
        err, _did_resolve, summary = _rd._resolve_pending_item_correction(
            correction, item, self_instance=instance_name, raw_config=raw_config,
        )
        if err is not None:
            return err, ""
        return None, summary or "pending item resolved"

    # Unreachable: FEED_ACTIONS only maps the five families above.
    return f"no resolver for kind '{kind}'", ""


# --- slot_suggestion completion (board DONE path) ----------------------------


def _today_for(config: Any) -> _date:
    """Today's date in the instance's configured timezone (the SAME tz the brief
    / tier compute uses) so a board completion stamps the day the producer
    surfaced the item under. UTC fallback only when the config carries no
    timezone (production always populates ``schedule.timezone``)."""
    tz = getattr(getattr(config, "schedule", None), "timezone", "") or ""
    if tz:
        try:
            return datetime.now(ZoneInfo(tz)).date()
        except Exception:  # noqa: BLE001 — bad tz string → UTC fallback
            log.warning("feed.act.slot.bad_timezone", tz=tz)
    return datetime.now(timezone.utc).date()


def _slot_lane(evidence: dict[str, Any]) -> str:
    """Classify a slot item's completion LANE from its stamped evidence (the
    Phase C matrix). Returns ``"routine"`` / ``"tier"`` / ``"task"`` /
    ``"unsupported"``.

      * ``origin == "task"``                → task lane (status → done + a
        completion date, via ``tier.task_completion.mark_task_done``). C1b
        wired the writer; done-only (undo → unsupported, see :func:`_slot_undo`).
      * ``routine_record`` present          → routine lane (completion_log
        writer, shared with ``routine_done``).
      * ``tier == 3`` (free-text, no record) → tier lane (``done_at`` writer,
        shared with ``tier_done``).
      * anything else                       → unsupported (unknown origin).

    NO fuzzy, NO guessing — the discriminator is the producer's own stamped
    fields (trusted, same class as ``last_batch``).
    """
    origin = str(evidence.get("origin") or "")
    if origin == "task":
        return "task"
    if str(evidence.get("routine_record") or "").strip():
        return "routine"
    if evidence.get("tier") == 3:
        return "tier"
    return "unsupported"


def _snooze_store_path(config: Any) -> str | None:
    """Resolve the board snooze store from the unified config, or ``None``.

    Reads ``tier.snooze.path`` out of the instance's OWN config file, threaded
    via ``config.config_path`` — the same single-source discipline as
    :func:`reply_dispatch._routine_match_corpus_path`. The writer here and the
    reader in ``compute_today_view`` MUST resolve to the same file; deriving
    both from one config key is what stops them drifting.

    ``None`` = not wired on this instance, and every snooze verb then refuses
    honestly rather than accepting a tap it would silently drop.
    """
    from alfred.tier.snooze import resolve_snooze_path

    config_path = getattr(config, "config_path", None) or "config.yaml"
    try:
        import yaml as _yaml

        with open(config_path, "r", encoding="utf-8") as f:
            raw = _yaml.safe_load(f) or {}
    except Exception as exc:  # noqa: BLE001
        log.info("board.snooze.config_unavailable", error=str(exc))
        return None
    # Shared parse — the reader resolves the SAME key through the SAME helper.
    return resolve_snooze_path(raw)


def _defer_until_iso(action_id: str, *, now: Any = None) -> str | None:
    """The window a defer verb asks for — or ``None`` for next-render.

    Pure, so the ladder's arithmetic is pinnable without a store. An unknown id
    yields ``None`` rather than raising: the ceiling has already refused
    anything unmapped, so reaching here with a stranger means a bug, and the
    fail-safe direction is the one the model chose everywhere else — the item
    comes back SOONER (next render), never later than asked.
    """
    days = DEFER_DURATIONS.get(action_id)
    if days is None:
        return None
    base = now or datetime.now(timezone.utc)
    return (base + timedelta(days=days)).isoformat()


def _dispatch_feed_defer(
    feed_item_id: str,
    action_id: str,
    *,
    feed_store: Any,
) -> ActResult:
    """Apply a generic ``defer`` / ``defer_Nd`` — the vertical gesture (D2).

    DELIBERATELY SEPARATE from :func:`_dispatch_slot_snooze`, and the separation
    is the same structural argument that one makes about completions: this
    function imports no board writer and touches no sidecar, so a defer can
    never fake a snooze (or a completion) no matter what evidence arrives. It
    writes ONE thing — the feed store's own deferred state.

    IT DOES NOT RE-IMPLEMENT THE WINDOW. ``FeedStore.defer`` owns the event and
    the log; ``defer_window_open`` owns the question of whether a window still
    holds. A second opinion about either — a comparison re-localized here —
    is precisely the drift the model's docstring rules out by naming itself the
    single place that decides.

    CRASH-ISOLATED FROM THE OTHER VERBS: a fault here returns an error for THIS
    act and nothing else. It cannot be reached from, and cannot fall through
    into, the resolver path that confirms and rejects — so a broken defer never
    costs the operator a confirm.
    """
    until = _defer_until_iso(action_id)
    try:
        feed_store.defer(feed_item_id, until=until)
    except Exception as exc:
        # The isolation, made real. A store fault is reported as a failed defer;
        # it does not raise into `act`, where it would abort a request that may
        # be carrying nothing else — and it never leaves the item half-judged,
        # because the only write this function makes is the one that failed.
        log.warning(
            "feed.act.defer_failed",
            id=feed_item_id, action=action_id, error=type(exc).__name__,
        )
        return ActResult(
            False, STATUS_ERROR,
            "couldn't set that aside just now — it stays where it is",
            feed_item_id, action_id,
        )
    log.info(
        "feed.act.deferred",
        id=feed_item_id, action=action_id,
        shape="until_time" if until else "next_render",
    )
    return ActResult(
        True, STATUS_ACTED,
        "set aside — it comes back when the window lapses" if until
        else "set aside — it comes back at the next sync",
        feed_item_id, action_id,
    )


#: The contact-surface router's kind + its two verbs, spelled once here so the
#: intercept below and the ceiling above cannot drift. The producer's own copy
#: lives in ``alfred.web.contact_patterns``; these are pinned equal by
#: ``tests/web/test_contact_pattern_card.py``.
PATTERN_KIND = "pattern_surfaced"
PATTERN_ADOPT = "adopt"
PATTERN_IGNORE = "ignore"


def _contact_store_path(config: Any) -> str | None:
    """Resolve the contact-router store from the instance's OWN config file.

    The exact shape of :func:`_snooze_store_path`, and for the exact reason:
    the ``/day/*`` routes write this file and this dispatcher writes it too, so
    both must resolve it through ONE parse. ``resolve_contact_state_path`` is
    that parse — the web config layer calls the same function at config load.

    ``None`` = not wired on this instance, and the verbs then refuse honestly
    rather than accepting a tap they would silently drop.
    """
    from alfred.web.contact_state import resolve_contact_state_path

    config_path = getattr(config, "config_path", None) or "config.yaml"
    try:
        import yaml as _yaml

        with open(config_path, "r", encoding="utf-8") as f:
            raw = _yaml.safe_load(f) or {}
    except Exception as exc:  # noqa: BLE001
        log.info("contact_router.config_unavailable", error=str(exc))
        return None
    return resolve_contact_state_path(raw)


def _dispatch_contact_pattern(
    feed_item_id: str,
    action_id: str,
    item: Any,
    *,
    feed_store: Any,
    config: Any,
) -> ActResult:
    """Apply ``adopt`` / ``ignore`` on a ``pattern_surfaced`` card (C4).

    The operator-approval end of the self-correcting loop. ``adopt`` is the ONLY
    write in the system that changes what the contact router opens to; ``ignore``
    silences this pattern for the operator's own ``window_days``.

    WRITES EXACTLY ONE FILE — the contact router's store — and reads the card's
    own ``source_ref``/``evidence`` for everything it needs. No vault op, no
    preference-record edit (the rule set and levers stay the operator's, edited
    where they live), no resolver, no ``last_batch``. Adopting a routing habit
    must not be able to touch a record.

    CRASH-ISOLATED like every other dispatcher: a store fault fails THIS act and
    leaves the card open, rather than raising into ``act`` or leaving the card
    judged with nothing written behind it.
    """
    evidence = getattr(item, "evidence", None) or {}
    source_ref = getattr(item, "source_ref", None) or {}
    rule = str(evidence.get("rule") or "")
    surface = str(evidence.get("observed_surface") or "")
    window_days = evidence.get("window_days")
    # The key the card was minted for — carried on the card rather than
    # re-derived here, so the act path holds no opinion about who the operator
    # is (see ``contact_patterns.build_pattern_item``).
    user_key = str(source_ref.get("user_key") or "")
    if not rule or not surface or not user_key:
        log.warning(
            "contact_router.act_incomplete_card",
            id=feed_item_id, action=action_id,
            has_rule=bool(rule), has_surface=bool(surface),
            has_user_key=bool(user_key),
            detail="the card is missing what the write needs — refusing rather "
                   "than guessing which rule or operator it meant",
        )
        return ActResult(
            False, STATUS_ERROR,
            "this card is missing the detail that tap needs — it stays open",
            feed_item_id, action_id,
        )

    store_path = _contact_store_path(config)
    if store_path is None:
        # Honest refusal, not a silent no-op: the operator tapped, and a tap
        # that writes nowhere must say so rather than flipping the card.
        log.info(
            "contact_router.act_not_wired",
            id=feed_item_id, action=action_id,
            detail="no contact-router state path on this instance — refusing "
                   "the tap rather than dropping it",
        )
        return ActResult(
            False, STATUS_ERROR,
            "the contact router isn't wired on this instance — nothing changed",
            feed_item_id, action_id,
        )

    try:
        from alfred.web.contact_state import WebContactStore

        store = WebContactStore.create(store_path)
        store.load()
        if action_id == PATTERN_ADOPT:
            store.adopt_default(user_key, rule=rule, surface=surface)
            detail = f"opening {surface} for this rule from now on"
        else:
            days = window_days if isinstance(window_days, int) else 14
            store.suppress_pattern(user_key, f"{rule}->{surface}", days=days)
            detail = f"won't raise this again for {days} days"
    except Exception as exc:  # noqa: BLE001 — a store fault fails one act only
        log.warning(
            "contact_router.act_failed",
            id=feed_item_id, action=action_id, error=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return ActResult(
            False, STATUS_ERROR,
            "couldn't save that just now — the card stays open",
            feed_item_id, action_id,
        )

    feed_store.set_state(feed_item_id, STATE_ACTED, action=action_id)
    log.info(
        "contact_router.acted",
        id=feed_item_id, action=action_id, rule=rule, surface=surface,
    )
    return ActResult(True, STATUS_ACTED, detail, feed_item_id, action_id)


def _dispatch_slot_snooze(
    feed_item_id: str,
    action_id: str,
    item: Any,
    *,
    feed_store: Any,
    config: Any,
) -> ActResult:
    """Apply a board ``snooze_*`` / ``unsnooze``.

    DELIBERATELY SEPARATE from :func:`_dispatch_slot_completion`: this function
    imports no completion writer and calls none, so a snooze cannot fake a
    completion no matter what evidence arrives. It writes ONLY the sidecar
    store — no vault mutation of any kind.
    """
    # Only these two. This function's imports are structurally pinned (the
    # no-completion-writer test reads its bytecode names), so an unused name
    # here reads as load-bearing to the next person — keep it exact.
    from alfred.tier.snooze import add_snooze, remove_snooze

    evidence = dict(getattr(item, "evidence", None) or {})
    store_path = _snooze_store_path(config)
    if store_path is None:
        log.info(
            "board.snooze.not_configured", id=feed_item_id, action=action_id,
        )
        return ActResult(
            False, STATUS_ERROR,
            "board snooze isn't configured on this instance",
            feed_item_id, action_id,
        )

    # The feed id IS "<kind>:<stable_key>" by construction, so the key is the
    # id's tail — no re-derivation from evidence, which could disagree.
    key = feed_item_id.partition(":")[2]
    if not key:
        return ActResult(
            False, STATUS_STALE_ITEM,
            "this item is missing its identity — it'll resurface at the next sync",
            feed_item_id, action_id,
        )

    if action_id == UNSNOOZE_ACTION:
        removed = remove_snooze(store_path, key)
        log.info(
            "board.unsnooze", id=feed_item_id, key=key, removed=removed,
        )
        if not removed:
            return ActResult(
                True, STATUS_ALREADY_ACTED, "wasn't snoozed (no change)",
                feed_item_id, action_id,
            )
        feed_store.set_state(feed_item_id, STATE_OPEN)
        return ActResult(True, STATUS_ACTED, "un-snoozed", feed_item_id, action_id)

    # Refuse a snooze on something already done — parking a finished item is
    # meaningless, and accepting the tap silently would be the exact
    # gesture-accepted-then-ignored failure this design rules out.
    # Family membership, not ``== DONE_ACTION``: an item completed via a
    # backdated rung is exactly as finished as one completed via the plain
    # verb, and parking it would be the same meaningless accept-and-ignore.
    if bool(evidence.get("done")) or getattr(item, "acted_action", None) in DONE_FAMILY:
        log.info(
            "board.snooze.refused_already_done", id=feed_item_id, action=action_id,
        )
        return ActResult(
            False, STATUS_ALREADY_DONE,
            "this item is already done — nothing to snooze",
            feed_item_id, action_id,
        )

    # ``None`` days = the indefinite rung (#14). The label is derived inside
    # add_snooze so the store's vocabulary has one author.
    days = SNOOZE_ACTIONS[action_id]
    lane = _slot_lane(evidence)
    entry = add_snooze(
        store_path, key,
        days=days,
        today=_today_for(config),
        lane=lane,
        due_iso=str(evidence.get("due_iso") or ""),
    )
    # Verb-stamped so "what did I snooze?" is answerable and the FE has a place
    # to hang an undo affordance (the staged snooze list reads this verb).
    feed_store.set_state(feed_item_id, STATE_ACTED, action=SNOOZE_ACTED_VERB)
    log.info(
        "board.snooze", id=feed_item_id, key=key, lane=lane, days=days,
        # "" for the indefinite rung — the log says the same thing the store
        # does (no end date), rather than inventing one for the sake of a
        # uniformly-shaped field.
        until=entry.snoozed_until, indefinite=days is None,
        overdue_at_snooze=entry.overdue_at_snooze,
    )
    # Say what actually happened. "snoozed until " with nothing after the
    # preposition is the trailing-colon failure in another costume — the
    # indefinite rung gets its own sentence rather than a blank where the
    # date should be.
    detail = (
        "snoozed until you say otherwise" if days is None
        else f"snoozed until {entry.snoozed_until}"
    )
    return ActResult(True, STATUS_ACTED, detail, feed_item_id, action_id)


def _dispatch_slot_completion(
    feed_item_id: str,
    action_id: str,
    item: Any,
    *,
    feed_store: Any,
    config: Any,
    vault_path: Path | None,
) -> ActResult:
    """Apply a slot_suggestion completion-family verb (``done`` / a dated
    ``done_Nd`` rung) or ``undo_done`` via the per-lane completion writer —
    the SAME functions the talker uses (single writer per lane). Acts on the feed item's OWN stamped ``evidence`` (never last_batch,
    never a re-derived key). Runs inside the caller's per-item mutex.
    """
    evidence = dict(getattr(item, "evidence", None) or {})
    lane = _slot_lane(evidence)

    if lane == "unsupported":
        log.info(
            "feed.act.slot.unsupported_item", id=feed_item_id, action=action_id,
            origin=evidence.get("origin", ""), tier=evidence.get("tier"),
        )
        return ActResult(
            False, STATUS_UNSUPPORTED_ITEM,
            "this item can't be completed from the board yet",
            feed_item_id, action_id,
        )

    if vault_path is None:
        return ActResult(
            False, STATUS_ERROR,
            "vault not configured — can't record completion",
            feed_item_id, action_id,
        )

    item_text = str(evidence.get("item_text") or evidence.get("name") or "").strip()
    if not item_text:
        log.info(
            "feed.act.slot.stale_item", id=feed_item_id, action=action_id,
            reason="no_item_text",
        )
        return ActResult(
            False, STATUS_STALE_ITEM,
            "this item is missing its completion key — it'll resurface at the next sync",
            feed_item_id, action_id,
        )

    today = _today_for(config)

    if action_id == UNDO_DONE_ACTION:
        # An item acted-by-ACCEPT (planned, not completed) has NO completion to
        # reverse and there is no un-accept writer in v1 — refuse honestly rather
        # than routing to the completion-undo writer (which would find nothing
        # logged and misleadingly flip the item back to open). "un-accept via
        # chat" is the honest fallback (task #21).
        if getattr(item, "acted_action", None) == ACCEPT_ACTION:
            log.info(
                "feed.act.slot.undo_on_accepted", id=feed_item_id, lane=lane,
                action=action_id,
            )
            return ActResult(
                False, STATUS_UNSUPPORTED_ITEM,
                "this was added to today's plan, not completed — there's nothing "
                "to undo (un-accept via chat)",
                feed_item_id, action_id,
            )
        # A SNOOZE is a delay, not a completion — but it is stamped
        # ``state=acted, action=snooze`` (see :func:`_slot_snooze`), so the
        # ``state == STATE_ACTED`` half of ``is_done`` below reads it as done
        # and would route it to the completion-undo writer. That writer would
        # find no completion logged and flip a merely-postponed item to open,
        # silently erasing the operator's snooze.
        #
        # This is the same verb-guard shape as the accept case above, and it
        # has to be its own refusal rather than falling through to "isn't
        # marked done": the item HAS a live snooze, and the honest answer names
        # it and points at the action that actually reverses it.
        if getattr(item, "acted_action", None) == SNOOZE_ACTED_VERB:
            log.info(
                "feed.act.slot.undo_on_snoozed", id=feed_item_id, lane=lane,
                action=action_id,
            )
            return ActResult(
                False, STATUS_INVALID_ACTION,
                "this item is snoozed, not done — there's nothing to undo "
                "(use unsnooze to bring it back today)",
                feed_item_id, action_id,
            )
        # Undo only when the item is CURRENTLY done — freshly board-acted
        # (state=acted) OR still-open-but-done-in-vault (evidence.done: completed
        # via the talker, or a reconcile revived a board-acted item back to open
        # with done=true). Nothing to undo otherwise.
        is_done = item.state == STATE_ACTED or bool(evidence.get("done"))
        if not is_done:
            log.info(
                "feed.act.slot.not_done", id=feed_item_id, action=action_id,
                state=item.state,
            )
            return ActResult(
                False, STATUS_INVALID_ACTION,
                "this item isn't marked done — nothing to undo",
                feed_item_id, action_id,
            )
        # A backdated completion logged a PAST date, and its acted verb says
        # which (``done_1d`` → today-1 at act time). Undo must aim at THAT
        # date, not today — ``undo_done`` after a ``done_1d`` with today's
        # date would find nothing logged, answer ok, and leave the record
        # still satisfied: an undo that silently undoes nothing. Deriving the
        # offset from the stamped verb against the UNDO day's today keeps the
        # same-day undo (the whole undo surface: the row button, the
        # optimistic flip) exact; a cross-midnight undo degrades to the same
        # honest not_logged no-op the plain verb has always had.
        undo_offset = BACKDATE_DONE_ACTIONS.get(
            getattr(item, "acted_action", "") or "", 0,
        )
        return _slot_undo(
            feed_item_id, action_id, lane=lane, evidence=evidence,
            feed_store=feed_store, vault_path=vault_path,
            item_text=item_text, today=today,
            completion_date=today - timedelta(days=undo_offset),
        )

    # action_id is in the DONE FAMILY (the ceiling admits done / done_Nd /
    # undo_done here). The rung decides the date; the plain verb is offset 0.
    backdate_days = BACKDATE_DONE_ACTIONS.get(action_id, 0)
    completion_date = today - timedelta(days=backdate_days)
    when_shape = ""
    if backdate_days:
        refusal, when_shape = _check_backdate_admissible(
            feed_item_id, action_id, lane=lane, evidence=evidence,
            vault_path=Path(vault_path), item_text=item_text,
            chosen=completion_date, today=today,
        )
        if refusal is not None:
            return refusal
    return _slot_done(
        feed_item_id, action_id, lane=lane, evidence=evidence,
        feed_store=feed_store, vault_path=vault_path,
        item_text=item_text, today=today, completion_date=completion_date,
        when_shape=when_shape,
    )


def _check_backdate_admissible(
    feed_item_id: str,
    action_id: str,
    *,
    lane: str,
    evidence: dict[str, Any],
    vault_path: Path,
    item_text: str,
    chosen: _date,
    today: _date,
) -> "tuple[ActResult | None, str]":
    """The act-time bound for a backdated completion.

    Returns ``(refusal, shape)``: ``(None, <recurrence type>)`` when the
    backdate is admissible — the shape rides back so the when-ruling capture
    can record its learnable discriminator without a second record read — and
    ``(<ActResult>, "")`` on a refusal.

    Enforces the ratified rule (2026-08-20): a 'previously done' date must lie
    inside the item's CURRENT credit window (±half-cycle around its effective
    due, strictly before today — ``routine.recurrence.backdate_credit_window``,
    the single owner of that arithmetic). The serve side already filters the
    rungs by the producer's ``backdate_limit_days`` stamp; this is the door
    itself, for stale clients, stale stamps, and hand-crafted POSTs.

    ROUTINE LANE ONLY in v1: the bound derives from the recurrence grammar,
    which the task and free-text T3 lanes don't carry — an unbounded backdate
    there would be a claim with no window to check it against, so those lanes
    refuse honestly rather than guess. Every refusal logs a named event with a
    ``reason`` field (the refusal-pin contract: WHY, not just that).

    TOCTOU note: this reads the record outside the writer's ``file_rmw_lock``;
    the per-item act mutex serializes board taps, and a concurrent CLI
    completion landing between check and write costs at worst one extra logged
    date — undoable, never a corruption.
    """
    if lane != "routine":
        log.info(
            "feed.act.slot.backdate_refused",
            id=feed_item_id, action=action_id, lane=lane,
            reason="unsupported_lane",
        )
        return ActResult(
            False, STATUS_UNSUPPORTED_ITEM,
            "backdating is only available for routine items so far — "
            "the plain ✓ still records it as done today",
            feed_item_id, action_id,
        ), ""

    rel = str(evidence.get("path") or "").strip()
    if not rel:
        return ActResult(
            False, STATUS_STALE_ITEM,
            "routine item is missing its record path — it'll resurface at the next sync",
            feed_item_id, action_id,
        ), ""
    record_path, refusal = _contained_record_path(
        vault_path, rel, feed_item_id=feed_item_id,
        action_id=action_id, lane=lane, writer="feed.act.slot.backdate.routine",
    )
    if refusal is not None:
        return refusal, ""

    try:
        fm = dict(frontmatter.load(str(record_path)).metadata or {})
    except Exception:  # noqa: BLE001 — unreadable record → honest error
        log.info(
            "feed.act.slot.backdate_refused",
            id=feed_item_id, action=action_id, lane=lane,
            reason="record_unreadable", path=str(record_path),
        )
        return ActResult(
            False, STATUS_ERROR,
            "the routine record could not be read",
            feed_item_id, action_id,
        ), ""

    due_pattern = None
    raw_items = fm.get("items") or []
    if isinstance(raw_items, list):
        for raw_item in raw_items:
            if isinstance(raw_item, dict) and str(raw_item.get("text") or "").strip() == item_text:
                due_pattern = raw_item.get("due_pattern")
                break
    if not isinstance(due_pattern, dict):
        # No recurrence grammar on the item (cadence/self-care shapes, or the
        # item moved) → no window to credit → no honest backdate. The plain
        # verb still works; a cadence-item backdate bound is a ledgered
        # follow-up ruling, not a silent admit.
        log.info(
            "feed.act.slot.backdate_refused",
            id=feed_item_id, action=action_id, lane=lane,
            reason="no_due_pattern", item=item_text,
        )
        return ActResult(
            False, STATUS_INVALID_ACTION,
            f"'{item_text}' has no recurring deadline to credit — "
            "the plain ✓ records it as done today",
            feed_item_id, action_id,
        ), ""

    from alfred.routine.recurrence import backdate_credit_window

    completion_log = fm.get("completion_log")
    window = backdate_credit_window(
        due_pattern,
        completion_log if isinstance(completion_log, dict) else {},
        item_text, today,
    )
    if window is None or not (window[0] <= chosen <= window[1]):
        log.info(
            "feed.act.slot.backdate_refused",
            id=feed_item_id, action=action_id, lane=lane,
            reason="beyond_window", item=item_text,
            chosen=chosen.isoformat(),
            window_start=window[0].isoformat() if window else "",
            window_end=window[1].isoformat() if window else "",
        )
        return ActResult(
            False, STATUS_INVALID_ACTION,
            f"{chosen.isoformat()} is outside the window this completion "
            f"could credit for '{item_text}' — nothing was written",
            feed_item_id, action_id,
        ), ""
    return None, str(due_pattern.get("type") or "")


def _contained_record_path(
    vault_path: Path,
    rel: str,
    *,
    feed_item_id: str,
    action_id: str,
    lane: str,
    writer: str,
) -> "tuple[Path | None, ActResult | None]":
    """Resolve an evidence-stamped ``rel`` inside the vault, or build the refusal.

    Arc #18. ``evidence["path"]`` is producer-stamped, which the board writers
    have historically treated as trusted. It is NOT safe to compose blind: for a
    CURATED slot the producer builds that field by interpolating a string read
    back out of the daily file (``tier.compute._curated_to_tier_entry`` does
    ``path=f"routine/{record}.md"``), and that string is persisted from an
    unsanitised LLM tool argument by ``tier.tier_confirm.confirm_slot_candidate``.
    So a hostile ``routine_record`` reaches this composition through the vault,
    without ever crossing the HTTP boundary (``/feed/act`` takes only
    ``{id, action_id}``).

    Returns ``(resolved_path, None)`` on success and ``(None, refusal)`` on an
    escape — the caller returns the refusal verbatim. Fail-closed: NO write, the
    feed item stays ``open``, and one named log fires so the refusal is grep-able
    (the containment helper logs the path detail; this adds the board context).

    Note the TASK lane does not use this: it hands ``(vault_path, rel)`` to
    ``tier.task_completion.mark_task_done``, which composes internally and
    therefore gates internally. One gate per composition site, never two.
    """
    try:
        return resolve_in_vault(vault_path, rel, writer=writer), None
    except VaultContainmentError:
        log.warning(
            "feed.act.slot.path_escape_denied",
            id=feed_item_id, action=action_id, lane=lane, rel_path=rel,
        )
        return None, ActResult(
            False, STATUS_ERROR,
            "this item's record path is not inside the vault — refusing to write",
            feed_item_id, action_id,
        )


def _slot_done(
    feed_item_id: str,
    action_id: str,
    *,
    lane: str,
    evidence: dict[str, Any],
    feed_store: Any,
    vault_path: Path,
    item_text: str,
    today: _date,
    completion_date: _date,
    when_shape: str = "",
) -> ActResult:
    """Route a slot ``done`` (or a dated ``done_Nd`` rung) to the lane's
    completion writer; on the non-error end-state set the feed item ``acted``
    (optimistic-green). Idempotent: a re-done maps to the same ``acted`` (the
    writer's own idempotent_noop).

    ``completion_date`` is the date the completion CLAIMS — ``today`` for the
    plain verb, ``today - N`` for a rung (already bound-checked by the
    dispatcher; only the routine lane can receive a rung). ``when_shape`` is
    the recurrence type the bound check read, carried for the when-ruling
    capture."""
    if lane == "routine":
        from alfred.routine import completion as _completion

        rel = str(evidence.get("path") or "").strip()
        if not rel:
            return ActResult(
                False, STATUS_STALE_ITEM,
                "routine item is missing its record path — it'll resurface at the next sync",
                feed_item_id, action_id,
            )
        record_path, refusal = _contained_record_path(
            Path(vault_path), rel, feed_item_id=feed_item_id,
            action_id=action_id, lane=lane, writer="feed.act.slot.done.routine",
        )
        if refusal is not None:
            return refusal
        result = _completion.mark_routine_item_done(
            record_path, item_text, completion_date.isoformat(),
            vault_path=Path(vault_path),
        )
        if result.ok:
            # Stamp the TRUE verb (``done_1d``, not a collapsed ``done``): the
            # undo path derives the date to remove from it, and the stage map
            # on the web reads the family (rings.ts ACTED_VERB_STAGE). The
            # plain verb is byte-unchanged — action_id IS ``done`` there.
            feed_store.set_state(feed_item_id, STATE_ACTED, action=action_id)
            backdated = action_id in BACKDATE_DONE_ACTIONS
            if backdated and result.changed:
                # Self-correcting capture: the operator answered the WHEN
                # question with a non-default. BELTED — the completion must
                # land even when the learning store cannot.
                try:
                    from alfred.routine.when_corrections import (
                        record_when_ruling,
                        when_corrections_path_for,
                    )

                    record_when_ruling(
                        when_corrections_path_for(feed_store.path),
                        shape=when_shape,
                        item=item_text,
                        record=str(evidence.get("routine_record") or ""),
                        proposed=today.isoformat(),
                        chosen=completion_date.isoformat(),
                        feed_item_id=feed_item_id,
                    )
                except Exception as exc:  # noqa: BLE001 — capture is best-effort
                    log.warning(
                        "routine.completion.when_ruling_capture_failed",
                        id=feed_item_id, error=type(exc).__name__,
                    )
            log.info(
                "feed.act.slot.done", id=feed_item_id, lane=lane,
                kind=result.kind, item=item_text,
                date=completion_date.isoformat(),
            )
            # ILB: a backdated confirmation NAMES the chosen day — "marked
            # done" alone would read as today, the exact claim the operator
            # chose this rung to avoid making.
            detail = f"marked done: {item_text}"
            if backdated:
                detail += f" — {_backdate_phrase(BACKDATE_DONE_ACTIONS[action_id])}"
            return ActResult(True, STATUS_ACTED, detail, feed_item_id, action_id)
        log.info(
            "feed.act.slot.writer_error", id=feed_item_id, lane=lane,
            kind=result.kind, item=item_text,
        )
        return ActResult(
            False, STATUS_ERROR, _routine_writer_detail(result.kind, item_text),
            feed_item_id, action_id,
        )

    if lane == "task":
        from alfred.tier.task_completion import (
            TASK_DONE_KIND_IDEMPOTENT_NOOP,
            TASK_DONE_KIND_SUCCESS,
            mark_task_done,
        )

        rel = str(evidence.get("path") or "").strip()
        if not rel:
            return ActResult(
                False, STATUS_STALE_ITEM,
                "task is missing its record path — it'll resurface at the next sync",
                feed_item_id, action_id,
            )
        result = mark_task_done(Path(vault_path), rel, today.isoformat())
        if result.kind in (TASK_DONE_KIND_SUCCESS, TASK_DONE_KIND_IDEMPOTENT_NOOP):
            feed_store.set_state(feed_item_id, STATE_ACTED, action=DONE_ACTION)
            log.info(
                "feed.act.slot.done", id=feed_item_id, lane=lane,
                kind=result.kind, item=result.name or item_text,
            )
            return ActResult(True, STATUS_ACTED, f"marked done: {result.name or item_text}", feed_item_id, action_id)
        log.info(
            "feed.act.slot.writer_error", id=feed_item_id, lane=lane,
            kind=result.kind, item=result.name or item_text,
        )
        return ActResult(
            False, STATUS_ERROR, _task_writer_detail(result.kind, result.name or item_text),
            feed_item_id, action_id,
        )

    # tier lane — free-text T3.
    from alfred.tier.daily_curation import (
        TIER_DONE_KIND_IDEMPOTENT_NOOP,
        TIER_DONE_KIND_SUCCESS,
        mark_t3_done,
    )

    result = mark_t3_done(Path(vault_path), item_text, completed_at=today, today=today)
    if result.kind in (TIER_DONE_KIND_SUCCESS, TIER_DONE_KIND_IDEMPOTENT_NOOP):
        feed_store.set_state(feed_item_id, STATE_ACTED, action=DONE_ACTION)
        log.info(
            "feed.act.slot.done", id=feed_item_id, lane=lane,
            kind=result.kind, item=item_text,
        )
        return ActResult(True, STATUS_ACTED, f"marked done: {result.item or item_text}", feed_item_id, action_id)
    log.info(
        "feed.act.slot.writer_error", id=feed_item_id, lane=lane,
        kind=result.kind, item=item_text,
    )
    return ActResult(
        False, STATUS_ERROR, _tier_writer_detail(result.kind, item_text),
        feed_item_id, action_id,
    )


def _backdate_phrase(days_back: int) -> str:
    """The operator-facing name of a backdate offset ("yesterday" / "N days
    ago") — the confirmation surface's vocabulary, one owner."""
    return "yesterday" if days_back == 1 else f"{days_back} days ago"


def _slot_undo(
    feed_item_id: str,
    action_id: str,
    *,
    lane: str,
    evidence: dict[str, Any],
    feed_store: Any,
    vault_path: Path,
    item_text: str,
    today: _date,
    completion_date: _date,
) -> ActResult:
    """Route a slot ``undo_done`` to the lane's inverse writer; on the non-error
    end-state set the feed item back to ``open`` (the completion is reversed).

    ``completion_date`` is the date the undo aims at — today for a plain done,
    the acted rung's derived date for a backdated one (see the dispatcher's
    ``undo_offset`` note)."""
    if lane == "task":
        # v1 task lane is DONE-ONLY — the prior open status isn't cleanly
        # restorable (a done task could have been todo/active/blocked; restoring
        # a fixed one would guess a lifecycle), so undo isn't wired. Honest
        # dead-end; the FE surfaces "undo via chat for now".
        log.info("feed.act.slot.undo_unsupported", id=feed_item_id, lane=lane)
        return ActResult(
            False, STATUS_UNSUPPORTED_ITEM,
            "undo isn't available for tasks from the board yet — undo via chat",
            feed_item_id, action_id,
        )

    if lane == "routine":
        from alfred.routine import completion as _completion

        rel = str(evidence.get("path") or "").strip()
        if not rel:
            return ActResult(
                False, STATUS_STALE_ITEM,
                "routine item is missing its record path — it'll resurface at the next sync",
                feed_item_id, action_id,
            )
        record_path, refusal = _contained_record_path(
            Path(vault_path), rel, feed_item_id=feed_item_id,
            action_id=action_id, lane=lane, writer="feed.act.slot.undone.routine",
        )
        if refusal is not None:
            return refusal
        result = _completion.mark_routine_item_undone(
            record_path, item_text, completion_date.isoformat(),
            vault_path=Path(vault_path),
        )
        if result.ok:
            feed_store.set_state(feed_item_id, STATE_OPEN)
            log.info(
                "feed.act.slot.undone", id=feed_item_id, lane=lane,
                kind=result.kind, item=item_text,
                date=completion_date.isoformat(),
            )
            # ILB: undoing a BACKDATED completion names the date it removed —
            # "undone" alone would read as today's, which is not what was
            # logged and not what just changed.
            detail = f"undone: {item_text}"
            if completion_date != today:
                detail += f" ({completion_date.isoformat()})"
            return ActResult(True, STATUS_UNDONE, detail, feed_item_id, action_id)
        log.info(
            "feed.act.slot.writer_error", id=feed_item_id, lane=lane,
            kind=result.kind, item=item_text,
        )
        return ActResult(
            False, STATUS_ERROR, _routine_writer_detail(result.kind, item_text),
            feed_item_id, action_id,
        )

    # tier lane — free-text T3.
    from alfred.tier.daily_curation import (
        TIER_UNDONE_KIND_NOT_MARKED,
        TIER_UNDONE_KIND_UNMARKED,
        mark_t3_undone,
    )

    result = mark_t3_undone(Path(vault_path), item_text, on_date=today)
    if result.kind in (TIER_UNDONE_KIND_UNMARKED, TIER_UNDONE_KIND_NOT_MARKED):
        feed_store.set_state(feed_item_id, STATE_OPEN)
        log.info(
            "feed.act.slot.undone", id=feed_item_id, lane=lane,
            kind=result.kind, item=item_text,
        )
        return ActResult(True, STATUS_UNDONE, f"undone: {result.item or item_text}", feed_item_id, action_id)
    log.info(
        "feed.act.slot.writer_error", id=feed_item_id, lane=lane,
        kind=result.kind, item=item_text,
    )
    return ActResult(
        False, STATUS_ERROR, _tier_writer_detail(result.kind, item_text),
        feed_item_id, action_id,
    )


def _routine_writer_detail(kind: str, item_text: str) -> str:
    """Human detail for a routine completion writer refusal."""
    if kind == "unknown_item":
        return f"'{item_text}' is no longer on that routine record"
    if kind == "unknown_record":
        return "the routine record could not be read"
    return f"could not record completion for '{item_text}'"


def _task_writer_detail(kind: str, name: str) -> str:
    """Human detail for a task completion writer refusal."""
    if kind == "unknown_record":
        return f"'{name}' is no longer a task at that path"
    if kind == "invalid_status":
        return f"'{name}' has an unrecognized status — refusing to guess the lifecycle"
    return f"could not complete '{name}'"


def _tier_writer_detail(kind: str, item_text: str) -> str:
    """Human detail for a tier (free-text T3) writer refusal."""
    if kind == "unknown_item":
        return f"'{item_text}' isn't on today's T3 list"
    if kind == "ambiguous_item":
        return f"'{item_text}' matches more than one T3 item — be more specific"
    if kind == "future_date_rejected":
        return "can't complete a future-dated item"
    return f"could not record completion for '{item_text}'"


# --- slot_suggestion sort (the "Not sorted yet" path) ------------------------


def _dispatch_slot_sort(
    feed_item_id: str,
    action_id: str,
    item: Any,
    *,
    feed_store: Any,
    vault_path: Path | None,
) -> ActResult:
    """Apply a slot_suggestion sort — record the operator's slot ruling on the
    item's backing record via :func:`alfred.tier.sort_writer.assign_slot`.

    The operator's 2026-08-19 report is the whole brief: an item the classifier
    honestly refused to place had DONE as its only affordance, so "not sorted"
    was a permanent state rather than a question. This dispatcher is the answer
    to the question.

    **IT DOES NOT TOUCH FEED STATE, and that is the design rather than an
    omission.** A sort is orthogonal to the card's lifecycle: the item is still
    on today's board, still completable, still acceptable if it was a candidate.
    Marking it ``acted`` would clear it off the board as though the operator had
    decided it — turning "I told you where this belongs" into "I dealt with
    this", which is the same lying-affordance class the lane is closing. What
    changes instead is the RECORD, and the next projection re-emits the item
    with its new ``evidence.slot``; the board then draws it in its slot because
    that is the only thing the board ever read.

    Acts on the feed item's OWN stamped evidence (the trusted-producer class the
    sibling slot dispatchers read), never ``last_batch``. Runs inside the
    caller's per-item mutex.
    """
    target_slot = SORT_ACTION_BY_SLOT.get(action_id)
    if target_slot is None:
        # Unreachable through the ceiling, which already refused any unmapped
        # verb. Kept because this function is one refactor away from a caller
        # that forgets, and guessing a slot is the one thing it must never do.
        log.info(
            "feed.act.slot.sort_unknown_verb", id=feed_item_id, action=action_id,
        )
        return ActResult(
            False, STATUS_INVALID_ACTION,
            f"'{action_id}' is not a sort action", feed_item_id, action_id,
        )

    if vault_path is None:
        return ActResult(
            False, STATUS_ERROR,
            "vault not configured — can't sort this item",
            feed_item_id, action_id,
        )

    from alfred.tier.sort_writer import assign_slot

    evidence = dict(getattr(item, "evidence", None) or {})
    result = assign_slot(
        Path(vault_path),
        origin=str(evidence.get("origin") or ""),
        slot=target_slot,
        path=str(evidence.get("path") or ""),
        routine_record=evidence.get("routine_record"),
        item_text=evidence.get("item_text"),
    )

    if result.ok:
        log.info(
            "feed.act.slot.sorted", id=feed_item_id, action=action_id,
            slot=result.slot, origin=result.origin, target=result.target,
            changed=result.changed,
        )
        return ActResult(
            True, STATUS_SORTED,
            f"sorted into {result.slot}", feed_item_id, action_id,
            # The FE's optimistic flip: the board moves the row into this stack
            # without waiting for the next producer emit. ``idempotent_noop``
            # renders identically on purpose — the operator asked for this slot
            # and this slot is where it is, whether or not a byte changed.
            render={"slot": result.slot, "sorted": True},
        )

    log.info(
        "feed.act.slot.sort_failed", id=feed_item_id, action=action_id,
        slot=target_slot, kind=result.kind, origin=result.origin,
    )
    return ActResult(
        False,
        # A record that isn't there / an item text that no longer matches is the
        # card having moved on, not a bad request — the same reading
        # ``unsupported_item`` carries for a lane with no writer.
        STATUS_UNSUPPORTED_ITEM,
        _sort_writer_detail(result.kind),
        feed_item_id, action_id,
    )


def _sort_writer_detail(kind: str) -> str:
    """Human detail for a sort refusal. Each branch names WHAT could not be
    done, never what the operator did wrong."""
    from alfred.tier.sort_writer import (
        SORT_KIND_THIN_EVIDENCE,
        SORT_KIND_UNKNOWN_ITEM,
        SORT_KIND_UNKNOWN_RECORD,
    )

    if kind == SORT_KIND_UNKNOWN_RECORD:
        return "the record behind this one isn't there anymore — nothing to sort"
    if kind == SORT_KIND_UNKNOWN_ITEM:
        return "this item isn't on its record under that name anymore — nothing to sort"
    if kind == SORT_KIND_THIN_EVIDENCE:
        return (
            "this one is a free-text note with no record behind it, so there's "
            "nowhere to keep a slot for it"
        )
    return "could not sort this item"


def _dispatch_calibration_ruling(
    feed_item_id: str,
    action_id: str,
    item: Any,
    *,
    feed_store: Any,
    vault_path: Path | None,
    config: Any,
    raw_config: dict[str, Any] | None,
) -> ActResult:
    """Rule on a voice-calibration proposal from the FEED CARD — the operator's
    second front door onto the store the CLI verb already opens.

    ONE STORE, TWO FRONT DOORS, and the point of this function is that it adds
    no third policy. Confirm calls ``calibration_store.approve_proposal`` and
    reject calls ``reject_proposal`` — the SAME functions
    ``alfred voice-calibration`` calls, so both surfaces inherit the same
    guards: the named-operator requirement, the no-default-target refusal, the
    already-decided refusal, and the no-calibration-block precondition. A second
    implementation here would be a second place for those to drift, and the
    guarantee this feature rests on is that there is exactly one writer.

    THE OPERATOR IDENTITY IS THE TAP. A card act reaches this function only
    through the authenticated web session, so the tap IS the thumb-crossing the
    self-correcting guardrail requires; it is recorded as ``feed_card`` so the
    decision row says which door it came through rather than implying someone
    typed a name. This is deliberately NOT anonymous — ``approve_proposal``
    would refuse a blank, and passing a literal keeps that refusal live for the
    CLI while giving the card an honest provenance.

    ITS WRITER SET, one level deep: ``approve_proposal`` / ``reject_proposal``
    plus ``feed_store.set_state`` to decide the card. No completion, accept,
    snooze, sort or MOC writer is referenced from this function's code object.
    """
    from alfred.audit import instance_name_from_raw
    from alfred.telegram import calibration_store

    evidence = dict(getattr(item, "evidence", None) or {})
    proposal_id_ = str(evidence.get("proposal_id") or "")
    if not proposal_id_:
        # The producer stamps this on every card it emits, so an absent id means
        # a hand-crafted or corrupted item. Refuse rather than guess.
        log.info(
            "feed.act.calibration.invalid_action", id=feed_item_id,
            action=action_id, reason="no_proposal_id",
        )
        return ActResult(
            False, STATUS_INVALID_ACTION,
            "this calibration card carries no proposal id",
            feed_item_id, action_id,
        )

    cal = getattr(config, "calibration_review", None)
    if cal is None:
        return ActResult(
            False, STATUS_ERROR,
            "calibration review is not configured on this instance",
            feed_item_id, action_id,
        )

    if action_id == CALIBRATION_DISCARD_ACTION:
        result = calibration_store.reject_proposal(
            cal, proposal_id_, operator=FEED_CARD_OPERATOR,
        )
    else:
        if vault_path is None:
            return ActResult(
                False, STATUS_ERROR,
                "no vault path wired — cannot apply calibration",
                feed_item_id, action_id,
            )
        telegram_raw = (raw_config or {}).get("telegram")
        primary = (
            telegram_raw.get("primary_users") if isinstance(telegram_raw, dict) else None
        ) or []
        result = calibration_store.approve_proposal(
            Path(vault_path), cal, proposal_id_,
            operator=FEED_CARD_OPERATOR,
            user_rel_path=str(primary[0]) if primary else "",
            agent_slug=(instance_name_from_raw(raw_config) or "salem").lower(),
        )

    if "error" in result:
        # The card stays OPEN. A refused ruling must remain re-tappable once the
        # operator fixes what the message named — retiring it here would hide a
        # pending proposal behind a card he can no longer reach.
        log.info(
            "feed.act.calibration.refused", id=feed_item_id, action=action_id,
            proposal_id=proposal_id_, reason=result["error"],
        )
        return ActResult(
            False, STATUS_ERROR, result["error"], feed_item_id, action_id,
        )

    feed_store.set_state(feed_item_id, STATE_ACTED, action=action_id)
    log.info(
        "feed.act.calibration.ruled", id=feed_item_id, action=action_id,
        proposal_id=proposal_id_,
    )
    detail = (
        "calibration line added to your profile"
        if action_id == CALIBRATION_APPLY_ACTION
        else "calibration proposal discarded"
    )
    return ActResult(True, STATUS_OK, detail, feed_item_id, action_id)


def _dispatch_sort_ruling(
    feed_item_id: str,
    action_id: str,
    item: Any,
    *,
    feed_store: Any,
    vault_path: Path | None,
) -> ActResult:
    """Apply a sort verb on a ROTATION card (``sort_suggestion``) — record the
    ruling via the SAME writer the board uses (:func:`tier.sort_writer
    .assign_slot`), score it against the card's own proposal, and DECIDE the
    card.

    Three deliberate differences from :func:`_dispatch_slot_sort`, its board
    sibling, and one likeness worth stating:

    * **It sets feed state.** The rotation card's whole question is "where does
      this belong?" — a successful sort answers it, so the card goes ``acted``
      (stamped with the sort verb) and leaves the deck. The board card stays
      untouched there because sorting is orthogonal to ITS lifecycle; here it
      is the lifecycle.
    * **It captures the correction signal** (the self-correcting standard's
      part 1): the operator's chosen slot against the card's stamped
      ``proposed_slot``, keyed by the ``proposal_shape`` stamped at deal time.
      BELT-SWALLOWED — a learning-store failure must never cost the operator
      the sort itself, so the write lands first and the capture failure is a
      logged, named degradation. A reject/defer records nothing (the operator
      declined to judge), which the defer dispatcher enforces by never coming
      here.
    * **Its writer set is the same, minus nothing:** no completion, accept or
      snooze writer is referenced from this function's own code object — the
      structural pin mirrors the board sibling's, with ``set_state`` allowed
      here (and asserted THERE) because deciding the card is this dispatcher's
      contract. As with the sibling pin, the check is one level deep (direct
      references), not a transitive reachability proof.

    Runs inside the caller's per-item mutex, on the item's OWN stamped
    evidence (trusted-producer class).
    """
    target_slot = SORT_ACTION_BY_SLOT.get(action_id)
    if target_slot is None:
        # Unreachable through the ceiling; kept for the same one-refactor
        # reason as the sibling — guessing a slot is the one forbidden thing.
        log.info(
            "feed.act.sort.unknown_verb", id=feed_item_id, action=action_id,
        )
        return ActResult(
            False, STATUS_INVALID_ACTION,
            f"'{action_id}' is not a sort action", feed_item_id, action_id,
        )

    if vault_path is None:
        return ActResult(
            False, STATUS_ERROR,
            "vault not configured — can't sort this item",
            feed_item_id, action_id,
        )

    from alfred.tier.sort_writer import assign_slot

    evidence = dict(getattr(item, "evidence", None) or {})
    result = assign_slot(
        Path(vault_path),
        origin=str(evidence.get("origin") or ""),
        slot=target_slot,
        path=str(evidence.get("path") or ""),
        routine_record=evidence.get("routine_record"),
        item_text=evidence.get("item_text"),
    )

    if not result.ok:
        log.info(
            "feed.act.sort.failed", id=feed_item_id, action=action_id,
            slot=target_slot, kind=result.kind, origin=result.origin,
        )
        return ActResult(
            False, STATUS_UNSUPPORTED_ITEM, _sort_writer_detail(result.kind),
            feed_item_id, action_id,
        )

    # THE CORRECTION SIGNAL (self-correcting standard, part 1). Scored against
    # the proposal stamped at DEAL time — quick affirm lands here with the
    # proposed verb (confirmation), a hold-selector pick lands with a different
    # one (correction). Belt: the ruling write may fail without failing the
    # sort, and the skip is NAMED so a rotation that silently stopped learning
    # stays diagnosable (ILB).
    proposed = str(evidence.get("proposed_slot") or "")
    shape = str(evidence.get("proposal_shape") or "")
    if proposed in slots.CANONICAL_SLOTS and shape:
        try:
            from alfred.tier.sort_proposal import corrections_path_for, record_ruling

            record_ruling(
                corrections_path_for(feed_store.path),
                shape=shape,
                proposed=proposed,
                chosen=target_slot,
                proposed_rule=str(evidence.get("proposed_rule") or ""),
                feed_item_id=feed_item_id,
            )
        except Exception as exc:  # noqa: BLE001 — learning must never cost the sort
            log.warning(
                "feed.act.sort.capture_failed",
                id=feed_item_id, error=str(exc), error_type=type(exc).__name__,
                consequence="ruling applied but NOT recorded — the proposer "
                            "learns nothing from this one",
            )
    else:
        log.info(
            "feed.act.sort.capture_skipped",
            id=feed_item_id, proposed=proposed or "(none)", shape=shape or "(none)",
            reason="card carries no scoreable proposal (degraded/legacy payload)",
        )

    # The card is DECIDED — its question is answered. Stamped with the sort
    # verb so the fold (and the audit trail) can tell a sorted card from every
    # other acted shape.
    feed_store.set_state(feed_item_id, STATE_ACTED, action=action_id)
    log.info(
        "feed.act.sort.ruled", id=feed_item_id, action=action_id,
        slot=result.slot, origin=result.origin, target=result.target,
        changed=result.changed, proposed=proposed or "(none)",
        confirmed=(proposed == target_slot) if proposed else None,
    )
    return ActResult(
        True, STATUS_SORTED,
        f"sorted into {result.slot}", feed_item_id, action_id,
        # The same server-confirmed render contract as the board sort — the
        # FE's optimistic flip fires only when this is present.
        render={"slot": result.slot, "sorted": True},
    )


# --- moc_suggestion apply (the MOC reader) -----------------------------------


def _dispatch_moc_apply(
    feed_item_id: str,
    action_id: str,
    item: Any,
    *,
    feed_store: Any,
    vault_path: Path | None,
    config: Any,
    chosen_target: str | None,
) -> ActResult:
    """Apply a MOC-membership suggestion — the operator's affirm, with the
    chosen target arriving as per-request data.

    THE TARGET GATE IS FAIL-CLOSED, TWICE, and both refusals are named:

      * **Missing target.** Refused rather than defaulted to the card's own
        proposal. Defaulting would be the silent-downgrade the ``correct``
        gate already forbids one shape over: it would tell the operator their
        choice was taken while writing something they may not have picked.
      * **Unknown target.** The chosen MOC must be one of the card's OWN
        served ``moc_choices``. A hand-crafted POST naming any other path —
        or a stale sheet naming a MOC that has since left the vault — is
        refused. This is the open-set analogue of the ceiling's job: the
        ceiling bounds the VERBS, and for a verb whose target is data,
        something has to bound the TARGETS. The card's served choices are
        that bound, and they are producer-stamped.

    Runs inside the caller's per-item mutex, on the item's OWN stamped
    evidence (trusted-producer class), like its rotation sibling.

    ITS WRITER SET, one level deep: :func:`apply_membership` and the queue's
    :func:`update_status`, plus ``feed_store.set_state`` to decide the card.
    No completion, accept, snooze or sort writer is referenced from this
    function's code object. As with the sibling pins, that is a direct-
    reference check rather than a transitive reachability proof.
    """
    from alfred.surveyor.moc_apply import apply_membership

    evidence = dict(getattr(item, "evidence", None) or {})
    proposed = str(evidence.get("proposed_target") or "")
    choices = evidence.get("moc_choices") or []
    valid_targets = {
        str(c.get("target")) for c in choices
        if isinstance(c, Mapping) and c.get("target")
    }

    chosen = (chosen_target or "").strip()
    if not chosen:
        log.info(
            "feed.act.moc.invalid_action", id=feed_item_id, action=action_id,
            reason="apply_without_target",
        )
        return ActResult(
            False, STATUS_INVALID_ACTION,
            "an apply needs the MOC you meant", feed_item_id, action_id,
        )
    if chosen not in valid_targets:
        log.info(
            "feed.act.moc.invalid_action", id=feed_item_id, action=action_id,
            reason="target_not_offered", chosen=chosen[:120],
            offered=len(valid_targets),
        )
        return ActResult(
            False, STATUS_INVALID_ACTION,
            "that MOC isn't one of this card's choices", feed_item_id, action_id,
        )

    if vault_path is None:
        return ActResult(
            False, STATUS_ERROR,
            "vault not configured — can't apply this membership",
            feed_item_id, action_id,
        )

    members = [
        str(m) for m in (evidence.get("members") or []) if str(m).strip()
    ]
    if not members:
        # ILB: a card with nothing left to apply is a real answer, not an
        # error. It happens when the operator applied the members by hand
        # between the deal and the tap.
        log.info(
            "feed.act.moc.nothing_to_apply", id=feed_item_id, target=chosen,
            reason="card carries no remaining members",
        )
        feed_store.set_state(feed_item_id, STATE_ACTED, action=action_id)
        return ActResult(
            True, STATUS_ACTED, "nothing left to add", feed_item_id, action_id,
        )

    # RESOLVED BEFORE THE VAULT WRITE, and the ordering is the invariant: a
    # VAULT WRITE MUST NOT OUTLIVE ITS LEDGER UPDATE. These two lines used to
    # sit six lines BELOW ``apply_membership``, so an affirm on a config whose
    # queue would not resolve wrote every member's ``mocs:`` and then silently
    # failed to flip the row to ``applied`` — the record and its ledger
    # diverging, with the divergence announced only by an INFO line whose
    # wording ("no suggestion id on the card, or no queue path in config")
    # reads as a benign skip. A missing ledger row is worse than a wrong one
    # because absence is what nobody notices: the row stays ``pending`` and the
    # card is re-proposed for work already done.
    #
    # Resolving first turns an unrecoverable half-write into a refusal that
    # costs nothing. It is scoped to the case where a ledger update is actually
    # REQUIRED — a card carrying a ``suggestion_id`` has a row to flip, so an
    # unresolvable queue is fatal to it; a card without one has no ledger to
    # diverge from and proceeds exactly as before. What remains belt-swallowed
    # below is the I/O failure (disk error mid-write), which is not predictable
    # from config and is already logged with its consequence named. This closes
    # the half that IS predictable.
    suggestion_id = str(evidence.get("suggestion_id") or "")
    queue_path = _moc_queue_path(config)
    if suggestion_id and queue_path is None:
        log.error(
            "feed.act.moc.queue_unresolved",
            id=feed_item_id, suggestion_id=suggestion_id, target=chosen,
            members=len(members),
            consequence="REFUSED BEFORE THE VAULT WRITE — nothing was written, "
                        "so the queue row and the records still agree",
            reason="this card carries a suggestion_id (a queue row exists to "
                   "flip) but no surveyor: block in this config resolves a "
                   "queue path — applying would strand the row at pending",
        )
        return ActResult(
            False, STATUS_ERROR,
            "can't reach the suggestion queue — nothing applied",
            feed_item_id, action_id,
        )

    result = apply_membership(
        Path(vault_path), member_rel_paths=members,
        target_moc_rel_path=chosen,
    )

    # THE CORRECTION SIGNAL (self-correcting standard, part 1), captured
    # BEFORE the queue bookkeeping so a queue failure cannot cost the
    # learning. Scored against the card's stamped proposal: a quick affirm
    # lands with proposed == chosen (a confirmation), a hold-selector pick
    # lands with a different one (a correction). Same ruling-row shape and
    # the SAME writer as the sort rotation's — one spelling of "the operator
    # was proposed X and chose Y".
    #
    # BELT-SWALLOWED: a learning-store failure must never cost the operator
    # the apply, and the skip is NAMED so a reader that silently stopped
    # learning stays diagnosable.
    if proposed:
        try:
            from alfred.tier.sort_proposal import corrections_path_for, record_ruling

            record_ruling(
                corrections_path_for(feed_store.path),
                shape=f"moc:{evidence.get('mapping_signal') or 'unknown'}",
                proposed=proposed,
                chosen=chosen,
                proposed_rule=str(evidence.get("mapping_signal") or ""),
                feed_item_id=feed_item_id,
            )
        except Exception as exc:  # noqa: BLE001 — learning never costs the apply
            log.warning(
                "feed.act.moc.capture_failed",
                id=feed_item_id, error=str(exc), error_type=type(exc).__name__,
                consequence="membership applied but NOT recorded — the "
                            "suggester learns nothing from this one",
            )
    else:
        log.info(
            "feed.act.moc.capture_skipped", id=feed_item_id,
            reason="card carries no scoreable proposal (degraded payload)",
        )

    # QUEUE BOOKKEEPING. The lifecycle is pending -> accepted -> applied, and
    # a PARTIAL re-flips accepted -> pending carrying ``applied_members`` so
    # the retry skips what landed. Belt-swallowed for the same reason as the
    # capture: the vault is the source of truth and the write already
    # happened, so a queue blip must not report failure to the operator.
    if suggestion_id and queue_path:
        try:
            from alfred.surveyor.moc_suggestion_queue import update_status

            update_status(queue_path, suggestion_id, "accepted")
            if result.ok:
                update_status(
                    queue_path, suggestion_id, "applied",
                    applied_members=result.touched,
                )
            else:
                update_status(
                    queue_path, suggestion_id, "pending",
                    last_apply_error=result.first_error[:200],
                    applied_members=result.touched,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "feed.act.moc.queue_update_failed",
                id=feed_item_id, suggestion_id=suggestion_id,
                error=str(exc), error_type=type(exc).__name__,
                consequence="membership applied but the queue row still reads "
                            "pending — it will be re-proposed",
            )
    else:
        # ONE cause reaches here now, and the reason says so. It used to read
        # "no suggestion id on the card, OR no queue path in config" — true
        # when written, false the moment the unresolved-queue case above began
        # refusing before the write. The second disjunct can no longer arrive:
        # a card with a suggestion_id and no queue path returns early, and one
        # with both takes the branch above. Leaving the old wording would point
        # the next reader at a config problem for a card that simply carries no
        # row to flip.
        log.info(
            "feed.act.moc.queue_update_skipped", id=feed_item_id,
            suggestion_id="(none)",
            queue_path=str(queue_path or "(no surveyor block)"),
            reason="the card carries no suggestion_id, so there is no queue "
                   "row to flip — the vault write is the whole of this act",
        )

    log.info(
        "feed.act.moc.applied", id=feed_item_id, target=chosen,
        proposed=proposed or "(none)",
        confirmed=(chosen == proposed) if proposed else None,
        applied=len(result.applied), already=len(result.already),
        ineligible=len(result.ineligible), failed=len(result.failed),
        ok=result.ok, partial=result.partial,
    )

    if not result.ok:
        # A PARTIAL DOES NOT DECIDE THE CARD (2026-08-20 ruling). The row
        # stays actionable with its failures visible, and the card is left
        # open so the operator can retry it — retiring it would strand the
        # members that did not land.
        return ActResult(
            False, STATUS_ERROR,
            f"added {len(result.applied)}, {len(result.failed)} failed — "
            f"the card stays for a retry",
            feed_item_id, action_id,
        )

    feed_store.set_state(feed_item_id, STATE_ACTED, action=action_id)
    moc_label = chosen.split("/")[-1].removesuffix(".md")
    return ActResult(
        True, STATUS_ACTED,
        f"added {len(result.applied)} to {moc_label}",
        feed_item_id, action_id,
    )


def _moc_queue_path(config: Any) -> Path | None:
    """The suggestion queue this instance's surveyor enqueues into.

    ONE derivation, shared with the producer's read side by construction —
    but by CONSTRUCTION now rather than by assertion. Both sides read a
    declared ``moc_queue_path`` field that their loaders stamp through the
    same ``surveyor.config.resolve_moc_queue_path`` call on the same raw
    dict, so the file this router flips a row in is the file the brief dealt
    the card from.

    IT DID NOT USED TO BE. This function walked ``getattr(config, "surveyor",
    None)`` against ``DailySyncConfig``, which has no ``surveyor`` field — the
    lookup could not raise, so it returned ``None`` for every config forever
    and the ledger write-back never ran on any instance. The docstring above
    it claimed the two sides shared one derivation; they shared one BUG, which
    is self-consistent and therefore invisible. Hence the declared field and a
    plain attribute read: a dropped or renamed field is an AttributeError at
    this line, not another decade of plausible silence.
    """
    path = config.moc_queue_path
    return Path(path) if path else None


# --- slot_suggestion accept (board day-planning path) ------------------------


def _dispatch_slot_confirm(
    feed_item_id: str,
    action_id: str,
    item: Any,
    *,
    feed_store: Any,
    config: Any,
    vault_path: Path | None,
) -> ActResult:
    """Apply a slot_suggestion ``accept`` — commit an auto-surfaced candidate
    onto today's tier list via ``tier.tier_confirm.confirm_slot_candidate`` (the
    deterministic writer; task #21 routes the talker's confirm grammar through
    the same one). Acts on the feed item's OWN stamped ``evidence`` (never
    last_batch). Runs inside the caller's per-item mutex.

    PROVENANCE GUARD (output-bound): accept is valid ONLY on a genuine candidate
    (``evidence.candidate is True``). A committed item — an operator-added or
    already-confirmed slot — is NOT accept-able → ``invalid_action``, and NO
    write happens (the guard is proven by asserting the vault is UNCHANGED, not
    by a call-count — per ``feedback_identity_pin_output_binding``).

    On the non-error end-state (success / idempotent_noop) the feed item is set
    ``acted`` (the candidate episode is decided) and the ActResult carries the
    committed-render payload for the FE's optimistic flip. The NEXT producer emit
    re-emits the item as committed (``candidate=False``) — a new open episode per
    the C1 reconcile semantics (verified against feed/store.py)."""
    evidence = dict(getattr(item, "evidence", None) or {})

    # Provenance guard — accept ONLY genuine candidates.
    if evidence.get("candidate") is not True:
        log.info(
            "feed.act.slot.not_a_candidate", id=feed_item_id, action=action_id,
            candidate=evidence.get("candidate"), source=evidence.get("source", ""),
        )
        return ActResult(
            False, STATUS_INVALID_ACTION,
            "this item is already on today's plan — nothing to accept",
            feed_item_id, action_id,
        )

    if vault_path is None:
        return ActResult(
            False, STATUS_ERROR,
            "vault not configured — can't accept this suggestion",
            feed_item_id, action_id,
        )

    from alfred.tier.tier_confirm import confirm_slot_candidate

    today = _today_for(config)
    result = confirm_slot_candidate(
        Path(vault_path),
        tier=evidence.get("tier"),
        origin=str(evidence.get("origin") or ""),
        name=str(evidence.get("name") or ""),
        path=str(evidence.get("path") or ""),
        routine_record=evidence.get("routine_record"),
        item_text=evidence.get("item_text"),
        source=str(evidence.get("source") or ""),
        date=today,
    )
    if result.ok:
        # Stamp the acting verb so the fold distinguishes accept-acted (PLANNED,
        # still completable) from done-acted (DONE). See _act_locked's verb-aware
        # gate + the FeedItem.acted_action docstring.
        feed_store.set_state(feed_item_id, STATE_ACTED, action=ACCEPT_ACTION)
        log.info(
            "feed.act.slot.accepted", id=feed_item_id, tier=result.tier,
            kind=result.kind, item=result.name,
        )
        return ActResult(
            True, STATUS_ACTED, f"added to today's T{result.tier}: {result.name}",
            feed_item_id, action_id,
            render={"tier": result.tier, "name": result.name, "committed": True},
        )
    log.info(
        "feed.act.slot.confirm_error", id=feed_item_id, tier=evidence.get("tier"),
        kind=result.kind,
    )
    return ActResult(
        False, STATUS_ERROR, _confirm_writer_detail(result.kind),
        feed_item_id, action_id,
    )


def _confirm_writer_detail(kind: str) -> str:
    """Human detail for a slot-accept writer refusal."""
    if kind == "invalid_tier":
        return "this suggestion has an unrecognized tier — refusing to guess"
    if kind == "thin_evidence":
        return "this suggestion is missing what's needed to add it — it'll resurface at the next sync"
    return "could not add this suggestion to today's plan"


def _normalise_contested_section(value: str | None) -> str:
    """Map an incoming tap to the controlled vocabulary, or to ``""``.

    The tap arrives over HTTP from the PWA, so it is caller-composed input and
    gets the same treatment as any other: recognised values pass through
    verbatim, everything else — empty, whitespace, a heading from a build that
    renamed one, an outright invention — becomes ``""`` and files under
    ``unknown`` downstream.

    Rejecting into ``unknown`` rather than refusing the whole act is the right
    trade: the contest itself is the operator's signal and must land. Losing
    which section it came from costs one dimension of a statistic; losing the
    contest costs the correction.
    """
    name = (value or "").strip()
    if not name:
        return ""
    if not is_known_section(name):
        log.info(
            "feed.act.attribution.section_unknown",
            section=name[:120],
            detail="tapped section is not a rendered summary heading — filing "
                   "the contest under unknown rather than minting a bucket",
        )
        return ""
    return name


def _dispatch_attribution_contest(
    feed_item_id: str,
    action_id: str,
    batch_item: dict[str, Any],
    *,
    feed_store: Any,
    config: Any,
    vault_path: Path | None,
    contested_section: str = "",
) -> ActResult:
    """#63a — the contest door: "don't decide this one for me."

    A dedicated dispatcher rather than a ReplyCorrection verb, because a contest
    is neither a confirm nor a reject. It leaves the marked section in the body
    and the entry in frontmatter; what it changes is WHO decides. Two effects,
    and the feature is broken if either is missing:

      1. It RECORDS the contest — in the vault entry (which stops the 24h clock
         permanently) and in the audit corpus (which is the correction signal
         the self-correcting standard requires be captured, not dropped).
      2. It REVERTS the item to needs-you and leaves it OPEN, so the question
         comes back to the operator instead of ending here.

    Ordering is load-bearing: the vault write happens first, and the feed item is
    only re-tiered once it lands. A re-tier over a failed write would show the
    operator a contested card whose next sweep confirms it anyway.

    ``contested_section`` (#72 item 4) is the summary heading the operator
    TAPPED — the section the bad inference came from. It rides onto the corpus
    row so per-section rates can be counted, and it is optional by design:
    contesting the card as a whole stays allowed and files under ``unknown``
    rather than dropping out of the denominator. Deliberately NOT
    ``section_title``, which is free text chosen by whichever producer wrote the
    marker; keying rates on that would give a long tail of one-off strings and
    never surface "this section stands out".
    """
    marker_id = str(batch_item.get("marker_id") or "")
    record_path = str(batch_item.get("record_path") or "")
    if vault_path is None:
        log.info(
            "feed.act.attribution.contest_no_vault", id=feed_item_id,
            marker_id=marker_id,
        )
        return ActResult(
            False, STATUS_ERROR, "vault not configured — can't record a contest",
            feed_item_id, action_id,
        )
    if not marker_id or not record_path:
        log.info(
            "feed.act.attribution.contest_metadata_missing", id=feed_item_id,
            marker_id=marker_id, record_path=record_path[:200],
        )
        return ActResult(
            False, STATUS_ERROR, "attribution metadata missing — can't record a contest",
            feed_item_id, action_id,
        )

    # Same containment gate the confirm/reject resolver applies: record_path
    # round-trips through a JSON state file that nothing re-validates on load,
    # so it is gated like any caller-composed path — and BEFORE any stat, so a
    # traversal target is never even probed.
    try:
        file_path = resolve_in_vault(
            vault_path, record_path,
            writer="daily_sync.attribution.contest",
        )
    except VaultContainmentError:
        log.warning(
            "feed.act.attribution.path_escape_denied",
            record_path=record_path[:200], marker_id=marker_id,
        )
        return ActResult(
            False, STATUS_ERROR,
            f"{record_path} is not a path inside the vault",
            feed_item_id, action_id,
        )
    if not file_path.exists():
        log.warning(
            "feed.act.attribution.contest_record_missing",
            record_path=record_path, marker_id=marker_id,
        )
        return ActResult(
            False, STATUS_ERROR, f"record {record_path} no longer exists",
            feed_item_id, action_id,
        )

    try:
        post = frontmatter.load(str(file_path))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "feed.act.attribution.contest_read_failed",
            record_path=record_path, error=str(exc),
        )
        return ActResult(
            False, STATUS_ERROR, f"couldn't read {record_path}",
            feed_item_id, action_id,
        )

    fm = post.metadata or {}
    changed = contest_marker(fm, marker_id)
    if not changed:
        # Already contested (or the entry is gone). Idempotent ok-noop — but we
        # still re-tier below, because the whole point is that the operator sees
        # it under needs-you, and a double-tap must not leave it stranded in FYI.
        log.info(
            "feed.act.attribution.contest_noop", id=feed_item_id,
            marker_id=marker_id,
        )
    else:
        post.metadata = fm
        try:
            file_path.write_text(
                frontmatter.dumps(post) + "\n", encoding="utf-8",
            )
        except OSError as exc:
            log.warning(
                "feed.act.attribution.contest_write_failed",
                record_path=record_path, error=str(exc),
            )
            return ActResult(
                False, STATUS_ERROR, "write failed — the contest wasn't recorded",
                feed_item_id, action_id,
            )
        corpus_path = _rd._attribution_corpus_path(config)
        if corpus_path:
            try:
                append_attribution_entry(corpus_path, AttributionCorpusEntry(
                    type="attribution_contest",
                    marker_id=marker_id,
                    record_path=record_path,
                    agent=str(batch_item.get("agent") or ""),
                    section_title=str(batch_item.get("section_title") or ""),
                    marker_date=str(batch_item.get("date") or ""),
                    andrew_action="contest",
                    action_at=datetime.now(timezone.utc).isoformat(),
                    # Normalised at the boundary: an unrecognised value is
                    # filed as unknown rather than minting a new bucket, so a
                    # stale client cannot invent sections in the stats.
                    section=_normalise_contested_section(contested_section),
                ))
            except OSError as exc:
                # The vault write already landed and is the source of truth for
                # suppression; a missing corpus row costs signal, not correctness.
                log.warning(
                    "feed.act.attribution.contest_corpus_write_failed",
                    record_path=record_path, marker_id=marker_id, error=str(exc),
                )

    # Re-tier: back to needs-you, still OPEN. `contested` also goes into the
    # stored evidence so the tier survives until the next sync re-derives it
    # from the vault (feed_producer._attribution_tier).
    stored = feed_store.load().get(feed_item_id)
    if stored is not None:
        evidence = dict(stored.evidence or {})
        evidence["contested"] = True
        feed_store.upsert(replace(
            stored,
            mode=MODE_DECIDE,
            attention=ATTENTION_NEEDS_YOU,
            evidence=evidence,
        ))
    log.info(
        "feed.act.attribution.contested", id=feed_item_id,
        marker_id=marker_id, recorded=changed,
    )
    return ActResult(
        True, STATUS_CONTESTED,
        "contested — it's back under things that need you",
        feed_item_id, action_id,
    )


def act(
    feed_item_id: str,
    action_id: str,
    *,
    feed_store: Any,
    config: Any,
    vault_path: Path | None,
    instance_name: str,
    instance_scope: str,
    raw_config: dict[str, Any] | None = None,
    correction_target: str | None = None,
    contested_section: str | None = None,
) -> ActResult:
    """Apply one deck/feed action through the owning resolver.

    Flow (folded-state check FIRST):
      1. Load the feed item from the store; not present → ``stale_item``.
      2. Not ``open`` → ``already_acted`` (ok-noop; resolvers are idempotent,
         but we don't re-run them — the store already reflects a decision).
         EXCEPT ``retired`` → ``retired`` (NOT ok): the store reflects the
         producer withdrawing the card, which is not a decision at all.
      3. Kind comes from ``item.kind`` (defense-in-depth), with an id-prefix
         consistency guard — a mismatch is data corruption → ``error``.
      4. Universal ``ack`` for FYI items → set ``acked`` directly, no resolver.
      5. ``(kind, action_id)`` not in :data:`FEED_ACTIONS` → ``invalid_action``.
      6. Load the authoritative last_batch item by stable-key re-derivation;
         absent → ``stale_item``.
      7. Synthesize the ReplyCorrection + dispatch to the resolver; resolver
         error → ``error`` carrying the resolver's own message.
      8. Success → set ``acted`` → ``acted`` ok.

    ``correction_target`` (#13) is the routine item the operator says a rejected
    completion ACTUALLY meant. It is consumed by exactly one (kind, action) pair
    — ``(routine_match, correct)`` — and ignored everywhere else, so it cannot
    add capability to any other family. It is never trusted as text: the
    resolver validates it against the vault's live routine items and refuses
    anything else.

    The whole load→check→dispatch→set_state span runs under the per-item mutex
    (:func:`_per_item_lock`) so two concurrent acts on the same open item can't
    both pass the folded-state gate and double-dispatch. See that helper for the
    single-issuer assumption.
    """
    feed_item_id = (feed_item_id or "").strip()
    action_id = (action_id or "").strip()
    if not feed_item_id or not action_id:
        return ActResult(
            False, STATUS_INVALID_ACTION, "missing id or action_id",
            feed_item_id, action_id,
        )

    with _per_item_lock(feed_item_id):
        return _act_locked(
            feed_item_id, action_id,
            feed_store=feed_store, config=config, vault_path=vault_path,
            instance_name=instance_name, instance_scope=instance_scope,
            raw_config=raw_config, correction_target=correction_target,
            contested_section=contested_section,
        )


def _act_locked(
    feed_item_id: str,
    action_id: str,
    *,
    feed_store: Any,
    config: Any,
    vault_path: Path | None,
    instance_name: str,
    instance_scope: str,
    raw_config: dict[str, Any] | None,
    correction_target: str | None = None,
    contested_section: str | None = None,
) -> ActResult:
    """The critical section — runs holding this item's mutex. See :func:`act`."""
    item = feed_store.load().get(feed_item_id)
    if item is None:
        log.info(
            "feed.act.stale_item", id=feed_item_id, action=action_id,
            reason="not_in_store",
        )
        return ActResult(
            False, STATUS_STALE_ITEM,
            "this item is no longer in the feed — it'll resurface at the next sync",
            feed_item_id, action_id,
        )

    # Folded-state check FIRST — only OPEN items are actionable. An acted/acked/
    # expired item is an idempotent ok-noop (the deck may double-tap or race a
    # reconcile); we never re-drive a resolver for it. A ``retired`` one is the
    # exception that is NOT a noop — see the branch below.
    #
    # EXCEPTION 1: ``undo_done`` on a slot_suggestion acts on an item that is
    # DONE — i.e. already ``acted`` (the board's done set it). It must reach the
    # slot dispatcher below to reverse the completion, so it is exempt from the
    # acted-is-final gate. (An undo on an OPEN item — done via a non-board path,
    # still emitted open with evidence.done=true — passes this gate naturally.)
    #
    # EXCEPTION 2 (Phase C slice 2): ``done`` on a slot item acted-by-ACCEPT.
    # An accepted item is ``state=acted`` with ``acted_action="accept"`` — it's
    # PLANNED, not completed. Completing it must NOT be swallowed as
    # already_acted (that re-opened the operator's #1 friction: he couldn't ✓ an
    # item he accepted this morning until the next producer emit revived it,
    # hours away). So a done on an accept-acted slot item is exempt — it reaches
    # the completion writer, which re-stamps ``acted_action="done"``. A done on a
    # done-acted item stays the idempotent already_acted noop.
    #
    # Every other (kind, action) on a non-open item is the noop.
    #
    # EXCEPTION 3 (2026-08-19): a SORT verb on a slot item. Sorting is
    # orthogonal to the card's decision lifecycle — it writes the slot axis on
    # the backing record and changes nothing about whether the item is planned
    # or finished. An accepted item is ``state=acted`` and is still ON THE BOARD
    # (``ringItemVisibleToday`` keeps today's acted items visible), so it can sit
    # in the "Not sorted yet" stack exactly like an open one, and refusing to
    # sort it would rebuild the very dead end this lane exists to remove.
    #
    # RETIRED IS DELIBERATELY NOT EXEMPT. A retired card left its producer's open
    # set, which for this kind means the entry is no longer in today's projection
    # at all — its backing record may be closed, renamed or gone. Writing a slot
    # onto that is a guess about something the system has stopped tracking, and
    # the honest refusal below is the better answer.
    _is_slot_undo = item.kind == SLOT_KIND and action_id == UNDO_DONE_ACTION
    # The WHOLE done family, not DONE_ACTION alone: the operator's
    # accept-then-backdate flow ("took it this morning, then remembered he did
    # it yesterday") is exactly a ``done_1d`` on an accept-acted item — a
    # ``== DONE_ACTION`` comparison here would swallow it as already_acted,
    # which is the friction this exception was carved for, back again wearing
    # a date.
    _is_slot_done_on_accepted = (
        item.kind == SLOT_KIND
        and action_id in DONE_FAMILY
        and getattr(item, "acted_action", None) == ACCEPT_ACTION
    )
    _is_slot_sort = (
        item.kind == SLOT_KIND
        and action_id in SORT_ACTION_BY_SLOT
        and item.state != STATE_RETIRED
    )
    if item.state != STATE_OPEN and not (
        _is_slot_undo or _is_slot_done_on_accepted or _is_slot_sort
    ):
        # RETIRED IS NOT ALREADY-ACTED, and the difference is the operator's.
        # A retired card left its producer's open set — the section stopped
        # offering it — which is a fact about the producer and not a decision by
        # him. Answering ``already_acted`` here tells him he dealt with
        # something he never saw the end of; it is the sentence PY-C exists to
        # stop saying. Checked BEFORE the generic branch so the honest answer
        # wins whenever the state is known.
        #
        # The wire split, as established: ``reason`` on the log line is the
        # machine fact (a grep for producer-withdrawn acts), ``detail`` is the
        # sentence the operator reads.
        #
        # The resurface half of that sentence is a CHECKED claim, not a
        # comforting one: reconcile upserts re-offered items at ``state=open``,
        # ``_apply_event`` replaces the folded item wholesale, and
        # ``_revival_suppressed`` gates on ``acted``/``acked`` only — so a
        # retired card genuinely returns to open when its producer offers it
        # again.
        if item.state == STATE_RETIRED:
            log.info(
                "feed.act.retired", id=feed_item_id, action=action_id,
                state=item.state, reason="producer_withdrew_item",
            )
            return ActResult(
                False, STATUS_RETIRED,
                "this one was withdrawn — whatever was offering it stopped, so "
                "there's nothing here to act on. It'll come back on its own if "
                "it turns up again.",
                feed_item_id, action_id,
            )
        log.info(
            "feed.act.already_acted", id=feed_item_id, action=action_id,
            state=item.state,
        )
        return ActResult(
            True, STATUS_ALREADY_ACTED, f"already {item.state}",
            feed_item_id, action_id,
        )

    # Kind is the STORED item's kind (authoritative), not the client-supplied id
    # string — defense-in-depth. The id is ``<kind>:<stable_key>`` by
    # construction, so a prefix mismatch means a corrupt / hand-crafted store
    # entry: fail loud, never silently trust one side.
    kind = item.kind
    id_prefix = feed_item_id.partition(":")[0]
    if kind != id_prefix:
        log.warning(
            "feed.act.kind_mismatch", id=feed_item_id, action=action_id,
            item_kind=kind, id_prefix=id_prefix,
        )
        return ActResult(
            False, STATUS_ERROR,
            "feed item kind does not match its id — refusing",
            feed_item_id, action_id,
        )

    # email_urgent (#27 slice 1) — DEDICATED ack intercept, placed BEFORE the
    # universal FYI-ack gate below ON PURPOSE. email_urgent is MODE_DECIDE, and
    # that gate rejects ``ack`` on any non-FYI item (``ack_on_non_fyi``); so
    # without this intercept an urgent ack would be refused. Acking an urgent
    # item = deciding it → ``acted`` (NOT ``acked``, which is the FYI-awareness
    # state). No resolver, no last_batch — the curator emits it per-item, so
    # there is no sync batch to re-derive from. FEED_ACTIONS[email_urgent] is the
    # capability ceiling: any action other than ``ack`` is invalid. This block
    # touches ONLY email_urgent — the universal FYI-ack gate is byte-unchanged,
    # so every other decide kind's ack still returns invalid_action there.
    #
    # GATED ON THE VERB as well as the kind, matching the ``RETURN_KIND`` block
    # below, and the difference is defence in depth rather than tidiness. The
    # membership test on the next line already rejects a defer now that
    # ``email_urgent`` is excluded — but that rejection is CONTINGENT ON THE
    # CEILING'S CONTENTS, and the whole point of the boarded reconciler lane is
    # to put those verbs back. On the day it does, a kind-only gate would
    # silently resume converting every defer into an ``acted``/``ack`` — the
    # operator saying "later" and the system recording "decided" — because this
    # block sits ABOVE the defer dispatcher and would answer first.
    #
    # Testing the verb makes the refusal a property of THIS block instead of a
    # side effect of what the ceiling happens to hold, so a defer falls through
    # to its own dispatcher whenever the ceiling admits one. Verified by running
    # both shapes, not by reading them.
    if kind == URGENT_KIND and action_id == ACK_ACTION:
        if action_id not in FEED_ACTIONS.get(URGENT_KIND, {}):
            log.info(
                "feed.act.invalid_action", id=feed_item_id, kind=kind,
                action=action_id, reason="urgent_non_ack",
            )
            return ActResult(
                False, STATUS_INVALID_ACTION,
                f"'{action_id}' is not a valid action for a {kind} item",
                feed_item_id, action_id,
            )
        feed_store.set_state(feed_item_id, STATE_ACTED, action=ACK_ACTION)
        log.info("feed.act.acted", id=feed_item_id, kind=kind, action=action_id)
        return ActResult(True, STATUS_ACTED, "acknowledged", feed_item_id, action_id)

    # reminder_returned (T2-1) — the same interception the urgent ack needs, for
    # the same reason: this kind is MODE_DECIDE, so the universal gate below
    # would refuse its ``ack``, and it has no resolver and no last_batch.
    #
    # GATED ON THE VERB, not on the kind alone, and that difference is
    # load-bearing. A kind-only intercept swallows EVERY verb the ceiling
    # admits — including the auto-folded defer family — and answers them all
    # with ``acted``, which turns "later" into "decided" at the one moment the
    # operator said the opposite. Testing ``action_id == ACK_ACTION`` lets a
    # defer fall through to its own dispatcher below, and anything else fall
    # through to the generic ceiling check (which returns invalid_action for
    # this kind's closed set of ack + defer).
    if kind == RETURN_KIND and action_id == ACK_ACTION:
        feed_store.set_state(feed_item_id, STATE_ACTED, action=ACK_ACTION)
        log.info("feed.act.acted", id=feed_item_id, kind=kind, action=action_id)
        return ActResult(True, STATUS_ACTED, "acknowledged", feed_item_id, action_id)

    # Universal ack — FYI items only, no resolver, no last_batch, no vault op.
    if action_id == ACK_ACTION:
        if item.mode != MODE_FYI:
            log.info(
                "feed.act.invalid_action", id=feed_item_id, kind=kind,
                action=action_id, reason="ack_on_non_fyi",
            )
            return ActResult(
                False, STATUS_INVALID_ACTION,
                f"'ack' is only valid for FYI items (this is a {item.mode} item)",
                feed_item_id, action_id,
            )
        feed_store.set_state(feed_item_id, STATE_ACKED)
        log.info("feed.act.acked", id=feed_item_id, kind=kind)
        return ActResult(True, STATUS_ACKED, "acknowledged", feed_item_id, action_id)

    # Generic defer (#102 1b-ii) — intercepted BEFORE the resolver path, like
    # the slot snooze and the urgent ack before it. A defer decides nothing, so
    # it must never reach a resolver: the whole point of the vertical gesture is
    # that the judgement has not been made yet. The ceiling above already
    # refused any defer verb on an excluded kind (slot_suggestion keeps its
    # board snooze), so arriving here means this kind really does have it.
    if action_id in DEFER_ACTIONS and action_id in FEED_ACTIONS.get(kind, {}):
        return _dispatch_feed_defer(feed_item_id, action_id, feed_store=feed_store)

    # Contact-surface router (C4) — intercepted here for the same reason the
    # urgent ack and the slot verbs are: this kind is emitted per-override by
    # the web surface, so there is no sync batch to re-derive it from and
    # ``_load_batch_item`` below would answer stale_item for every tap. The
    # ceiling check runs FIRST so an unmapped verb is still invalid_action.
    if kind == PATTERN_KIND:
        if action_id not in FEED_ACTIONS.get(PATTERN_KIND, {}):
            log.info(
                "feed.act.invalid_action", id=feed_item_id, kind=kind,
                action=action_id, reason="pattern_unmapped_verb",
            )
            return ActResult(
                False, STATUS_INVALID_ACTION,
                f"'{action_id}' is not a valid action for a {kind} item",
                feed_item_id, action_id,
            )
        return _dispatch_contact_pattern(
            feed_item_id, action_id, item, feed_store=feed_store, config=config,
        )

    # sort_suggestion (the deck rotation) — intercepted for the same reason as
    # pattern_surfaced directly above: emitted per-item by the brief's
    # rotation, so there is no sync batch and no resolver, and falling through
    # to ``_load_batch_item`` would end at "no resolver for kind". Ceiling
    # check FIRST so an unmapped verb is still ``invalid_action``. What can
    # actually arrive here is exactly the three sort verbs: the FYI ``ack``
    # and the generic defer family were both intercepted ABOVE this block, and
    # the dispatcher's own unknown-verb guard backstops the remainder.
    if kind == SORT_SUGGESTION_KIND:
        if action_id not in FEED_ACTIONS.get(SORT_SUGGESTION_KIND, {}):
            log.info(
                "feed.act.invalid_action", id=feed_item_id, kind=kind,
                action=action_id, reason="sort_unmapped_verb",
            )
            return ActResult(
                False, STATUS_INVALID_ACTION,
                f"'{action_id}' is not a valid action for a {kind} item",
                feed_item_id, action_id,
            )
        return _dispatch_sort_ruling(
            feed_item_id, action_id, item,
            feed_store=feed_store, vault_path=vault_path,
        )

    # moc_suggestion (the MOC reader) — same interception shape as the
    # rotation directly above: emitted per-item by the brief, no sync batch,
    # no resolver. Ceiling check FIRST so an unmapped verb is
    # ``invalid_action``. The FYI ``ack`` and the generic defer family were
    # both intercepted ABOVE this block, so what arrives here is ``moc_apply``.
    #
    # This is where ``correction_target`` joins the second (kind, action) pair
    # in the codebase — passed explicitly rather than folded into
    # ``action_kwargs``, because this dispatcher synthesizes no
    # ReplyCorrection at all.
    if kind == MOC_SUGGESTION_KIND:
        if action_id not in FEED_ACTIONS.get(MOC_SUGGESTION_KIND, {}):
            log.info(
                "feed.act.invalid_action", id=feed_item_id, kind=kind,
                action=action_id, reason="moc_unmapped_verb",
            )
            return ActResult(
                False, STATUS_INVALID_ACTION,
                f"'{action_id}' is not a valid action for a {kind} item",
                feed_item_id, action_id,
            )
        return _dispatch_moc_apply(
            feed_item_id, action_id, item,
            feed_store=feed_store, vault_path=vault_path,
            config=config, chosen_target=correction_target,
        )

    # calibration (R4) — same interception shape as the three kinds above:
    # ceiling check FIRST so an unmapped verb is ``invalid_action``, then route
    # to the store. The FYI ``ack`` and the generic defer family were both
    # intercepted ABOVE this block, so what arrives here is one of the two
    # calibration verbs.
    #
    # It is intercepted rather than allowed to fall through for the reason the
    # distinct verb ids exist: the tail of this function synthesizes a
    # ReplyCorrection and reads ``last_batch``, and a calibration ruling must do
    # neither — its authority is the pending store, keyed by the proposal id the
    # producer stamped on the card.
    if kind == CALIBRATION_KIND:
        if action_id not in FEED_ACTIONS.get(CALIBRATION_KIND, {}):
            log.info(
                "feed.act.invalid_action", id=feed_item_id, kind=kind,
                action=action_id, reason="calibration_unmapped_verb",
            )
            return ActResult(
                False, STATUS_INVALID_ACTION,
                f"'{action_id}' is not a valid action for a {kind} item",
                feed_item_id, action_id,
            )
        return _dispatch_calibration_ruling(
            feed_item_id, action_id, item,
            feed_store=feed_store, vault_path=vault_path,
            config=config, raw_config=raw_config,
        )

    # The (kind, action) map is the capability ceiling.
    kind_actions = FEED_ACTIONS.get(kind)
    if kind_actions is None or action_id not in kind_actions:
        log.info(
            "feed.act.invalid_action", id=feed_item_id, kind=kind, action=action_id,
        )
        return ActResult(
            False, STATUS_INVALID_ACTION,
            f"'{action_id}' is not a valid action for a {kind} item",
            feed_item_id, action_id,
        )

    # slot_suggestion (board DONE + ACCEPT paths) — DEDICATED dispatchers, NOT
    # the ReplyCorrection/last_batch path. They act on the feed item's OWN
    # stamped evidence (the producer's trusted origin/routine_record/tier/
    # item_text, same trust class as last_batch): done/undo_done → the per-lane
    # completion writer, accept → the tier_confirm writer — the SAME functions
    # the talker uses (single writer per lane). Intercepts BEFORE
    # _load_batch_item because slot items have no last_batch entry.
    if kind == SLOT_KIND:
        if action_id in SNOOZE_ACTIONS or action_id == UNSNOOZE_ACTION:
            # Intercepted BEFORE the completion dispatcher. A snooze can never
            # reach a completion writer — that is the structural guarantee, not
            # a convention (see FEED_ACTIONS' slot_suggestion note).
            return _dispatch_slot_snooze(
                feed_item_id, action_id, item,
                feed_store=feed_store, config=config,
            )
        if action_id in SORT_ACTION_BY_SLOT:
            # The SORT path. Intercepted alongside the snooze and before the
            # completion dispatcher for the same structural reason: no
            # completion, accept or snooze writer is reachable from a sort under
            # any input, so a sort can never fake a commitment or a done.
            return _dispatch_slot_sort(
                feed_item_id, action_id, item,
                feed_store=feed_store, vault_path=vault_path,
            )
        if action_id == ACCEPT_ACTION:
            # Board day-planning: commit an auto-surfaced candidate onto today's
            # tier list. A separate dispatcher (not the completion writers) —
            # it calls the tier_confirm writer and enforces the candidate
            # provenance guard. An already-acted candidate never reaches here
            # (folded-state gate above returns already_acted).
            return _dispatch_slot_confirm(
                feed_item_id, action_id, item,
                feed_store=feed_store, config=config, vault_path=vault_path,
            )
        return _dispatch_slot_completion(
            feed_item_id, action_id, item,
            feed_store=feed_store, config=config, vault_path=vault_path,
        )

    # Authoritative item from last_batch — re-derived by id, never by ordinal.
    batch_item = _load_batch_item(kind, feed_item_id, config)
    if batch_item is None:
        # FALL BACK TO THE CARD'S OWN STAMPED EVIDENCE rather than refuse.
        #
        # THE OPERATOR'S VERDICT IS VALID REGARDLESS OF BATCH AGE, and this
        # branch used to throw it away: on 2026-08-15 he swiped five email
        # cards and all five 409'd, two spam and three confirm verdicts gone.
        # Worse than a visible refusal, the next reconcile then records those
        # cards ``acted`` on the reasoning that absence means "decided
        # elsewhere" — so a decision he was asked for and REFUSED becomes a
        # decision on the record.
        #
        # This is sound because the evidence IS the batch item: the producer
        # stamps ``evidence=d`` verbatim from the same dict this resolver
        # consumes (``daily_sync.feed_producer.build_feed_items``), and
        # ``FeedStore.reconcile`` re-upserts every still-open card each fire,
        # refreshing evidence rather than freezing it at first sight. The
        # precedent is explicit: the slot dispatchers already act on the feed
        # item's own stamped evidence, ruled "same trust class as last_batch".
        # The wire cannot reach this — an act carries only an id and a verb.
        #
        # NOT a weaker gate. The ``NOT by ordinal`` half of the original rule
        # is untouched: identity still re-derives through the id. What is
        # dropped is only the requirement that the batch still be RESIDENT,
        # which was never what made the act correct.
        #
        # Idempotency is unaffected because it never lived here: the
        # folded-state check above admits only ``open`` items (an applied
        # verdict has already moved to ``acted``), and ``_per_item_lock``
        # serializes same-id acts across the executor's threads. Both are
        # pinned; neither reads ``last_batch``.
        evidence = dict(getattr(item, "evidence", None) or {})
        if not evidence:
            log.info(
                "feed.act.stale_item", id=feed_item_id, action=action_id,
                reason="aged_out_of_last_batch_and_no_evidence",
            )
            return ActResult(
                False, STATUS_STALE_ITEM,
                "this batch has moved on — it'll resurface at the next sync",
                feed_item_id, action_id,
            )
        batch_item = evidence
        # ILB: the act SUCCEEDED on a path the operator can't see. Without this
        # line, "resolved from the card" is indistinguishable in the log from
        # the resident-batch case, and the next quota outage would leave no
        # trace that the fallback is what carried the day's verdicts.
        log.info(
            "feed.act.resolved_from_evidence",
            id=feed_item_id, kind=kind, action=action_id,
            reason="aged_out_of_last_batch",
        )

    # #63a attribution contest — intercepted AFTER the authoritative batch item
    # loads (it needs the record_path/marker_id from last_batch, never from the
    # feed item's display evidence) and BEFORE the ReplyCorrection synthesis,
    # because a contest is not a correction: it records a disagreement and hands
    # the decision back, rather than resolving the entry either way.
    if kind == ATTRIBUTION_KIND and action_id == CONTEST_ACTION:
        return _dispatch_attribution_contest(
            feed_item_id, action_id, batch_item,
            feed_store=feed_store, config=config, vault_path=vault_path,
            # Scoped exactly like correction_target below: the payload reaches
            # ONE (kind, action) pair. A contested_section posted alongside any
            # other action never gets here.
            contested_section=contested_section or "",
        )

    # #13 — a per-request payload joining the static ceiling kwargs. Scoped to
    # (routine_match, correct) so no other action can carry a target: a
    # `correction_target` posted alongside `confirm`, or alongside any other
    # kind, is dropped here and never reaches a resolver. A `correct` with no
    # target is refused rather than degraded into a plain reject — silently
    # downgrading would tell the operator their answer was taken when the deck
    # learned nothing.
    #
    # NO LONGER THE ONLY SUCH PLACE (2026-08-20): (moc_suggestion, moc_apply)
    # takes the same payload, and is intercepted EARLIER — see the
    # MOC_SUGGESTION_KIND block above, which never reaches this line. The
    # discipline travels with the shape: that gate refuses a missing target
    # AND a target absent from the card's own served choices, both fail-closed
    # with a named reason, rather than defaulting to the proposal.
    action_kwargs = dict(kind_actions[action_id])
    if kind == ROUTINE_MATCH_KIND and action_id == CORRECT_ACTION:
        chosen = (correction_target or "").strip()
        if not chosen:
            log.info(
                "feed.act.invalid_action", id=feed_item_id, kind=kind,
                action=action_id, reason="correct_without_target",
            )
            return ActResult(
                False, STATUS_INVALID_ACTION,
                "a correction needs the item you meant",
                feed_item_id, action_id,
            )
        action_kwargs["correction_target"] = chosen
    correction = ReplyCorrection(
        item_number=_SYNTHETIC_ITEM_NUMBER, **action_kwargs,
    )
    _dispatch_barrier(feed_item_id)  # no-op in prod; concurrency-pin seam
    err, detail = _dispatch(
        kind, correction, batch_item,
        config=config, vault_path=vault_path,
        instance_name=instance_name, instance_scope=instance_scope,
        raw_config=raw_config,
    )
    if err is not None:
        log.info(
            "feed.act.resolver_error", id=feed_item_id, kind=kind,
            action=action_id, detail=err,
        )
        return ActResult(False, STATUS_ERROR, err, feed_item_id, action_id)

    # Stamp the VERB the operator actually used. Before this, a resolver
    # success appended a verbless ``acted`` event — byte-identical to the one
    # ``reconcile`` writes when an item merely falls out of a producer's open
    # set. So "the operator confirmed this" and "this was auto-retired" were
    # the same line in the log, and the day's history could not distinguish
    # them. ``set_state`` has carried the optional ``action`` since the
    # snooze verb; this is the general path finally passing it.
    #
    # FORWARD ONLY, and that is the point: events already on disk stay
    # verbless and stay ambiguous. The record becomes auditable from here.
    feed_store.set_state(feed_item_id, STATE_ACTED, action=action_id)
    log.info("feed.act.acted", id=feed_item_id, kind=kind, action=action_id)
    return ActResult(True, STATUS_ACTED, detail, feed_item_id, action_id)
