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
    routine_match confirm → ok · reject → reject
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
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date as _date
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from alfred.daily_sync import reply_dispatch as _rd
from alfred.daily_sync.assembler import ReplyCorrection
from alfred.daily_sync.corpus import append_correction
from alfred.daily_sync.feed_producer import _FAMILIES, _as_dict
from alfred.feed.model import MODE_FYI, STATE_ACKED, STATE_ACTED, STATE_OPEN, make_id

log = structlog.get_logger(__name__)

# The router presents the located batch item to the resolver as item #1 of a
# single-item map. The resolvers only use ``item_number`` to key an email
# ``items_by_num`` lookup and to prefix their human error strings — there is no
# numbered batch on the deck path, so a fixed synthetic 1 is correct.
_SYNTHETIC_ITEM_NUMBER = 1

ACK_ACTION = "ack"

# ActResult.status vocabulary.
STATUS_ACTED = "acted"
STATUS_ACKED = "acked"
STATUS_ALREADY_ACTED = "already_acted"
STATUS_STALE_ITEM = "stale_item"
STATUS_INVALID_ACTION = "invalid_action"
STATUS_ERROR = "error"
# Phase C slice 1 (board DONE path) — the slot-completion vocabulary.
STATUS_UNDONE = "undone"  # undo_done succeeded → item back to open
STATUS_UNSUPPORTED_ITEM = "unsupported_item"  # lane has no completion writer (task/unknown)

# slot_suggestion completion actions (handled by the dedicated dispatcher, NOT
# the ReplyCorrection/last_batch path — the writers are called directly).
DONE_ACTION = "done"
UNDO_DONE_ACTION = "undo_done"
SLOT_KIND = "slot_suggestion"

# (kind, action_id) → the kwargs that synthesize the owning resolver's
# ``ReplyCorrection``. THIS MAP IS THE CAPABILITY CEILING — a pair absent here
# can never reach a resolver. A fresh ReplyCorrection is built per call (never a
# shared mutable instance) from these kwargs.
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
    "attribution": {
        "confirm": {"ok": True},
        "reject": {"reject": True},
    },
    "proposal": {
        "confirm": {"ok": True},
        "reject": {"reject": True},
    },
    "routine_match": {
        "confirm": {"ok": True},
        "reject": {"reject": True},
    },
    "pending": {
        "noted": {"ok": True, "consumed_token": "noted"},
        "show": {"ok": True, "consumed_token": "show"},
    },
    # slot_suggestion (board DONE path) — the capability-ceiling declaration.
    # Unlike the five families above, the kwargs here are UNUSED: a slot
    # completion does NOT synthesize a ReplyCorrection or read last_batch. It is
    # intercepted by :func:`_dispatch_slot_completion` (right after this ceiling
    # check) which calls the per-lane completion writer directly against the
    # feed item's OWN stamped evidence (origin/routine_record/tier/item_text).
    "slot_suggestion": {
        "done": {},
        "undo_done": {},
    },
}

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
            "id": self.feed_item_id,
            "action_id": self.action_id,
        }


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

    if kind == "routine_match":
        corpus_path = _rd._routine_match_corpus_path(config)
        if corpus_path is None:
            return "routine-match corpus not configured on this instance", ""
        err, did_write = _rd._resolve_routine_match_correction(
            correction, item, corpus_path,
        )
        if err is not None:
            return err, ""
        verb = "confirmed" if correction.ok else "rejected"
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


def _dispatch_slot_completion(
    feed_item_id: str,
    action_id: str,
    item: Any,
    *,
    feed_store: Any,
    config: Any,
    vault_path: Path | None,
) -> ActResult:
    """Apply a slot_suggestion ``done`` / ``undo_done`` via the per-lane
    completion writer — the SAME functions the talker uses (single writer per
    lane). Acts on the feed item's OWN stamped ``evidence`` (never last_batch,
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
        return _slot_undo(
            feed_item_id, action_id, lane=lane, evidence=evidence,
            feed_store=feed_store, vault_path=vault_path,
            item_text=item_text, today=today,
        )

    # action_id == DONE_ACTION (the ceiling admits only done/undo_done).
    return _slot_done(
        feed_item_id, action_id, lane=lane, evidence=evidence,
        feed_store=feed_store, vault_path=vault_path,
        item_text=item_text, today=today,
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
) -> ActResult:
    """Route a slot ``done`` to the lane's completion writer; on the non-error
    end-state set the feed item ``acted`` (optimistic-green). Idempotent: a
    re-done maps to the same ``acted`` (the writer's own idempotent_noop)."""
    if lane == "routine":
        from alfred.routine import completion as _completion

        rel = str(evidence.get("path") or "").strip()
        if not rel:
            return ActResult(
                False, STATUS_STALE_ITEM,
                "routine item is missing its record path — it'll resurface at the next sync",
                feed_item_id, action_id,
            )
        result = _completion.mark_routine_item_done(
            Path(vault_path) / rel, item_text, today.isoformat(),
        )
        if result.ok:
            feed_store.set_state(feed_item_id, STATE_ACTED)
            log.info(
                "feed.act.slot.done", id=feed_item_id, lane=lane,
                kind=result.kind, item=item_text,
            )
            return ActResult(True, STATUS_ACTED, f"marked done: {item_text}", feed_item_id, action_id)
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
            feed_store.set_state(feed_item_id, STATE_ACTED)
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
        feed_store.set_state(feed_item_id, STATE_ACTED)
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
) -> ActResult:
    """Route a slot ``undo_done`` to the lane's inverse writer; on the non-error
    end-state set the feed item back to ``open`` (the completion is reversed)."""
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
        result = _completion.mark_routine_item_undone(
            Path(vault_path) / rel, item_text, today.isoformat(),
        )
        if result.ok:
            feed_store.set_state(feed_item_id, STATE_OPEN)
            log.info(
                "feed.act.slot.undone", id=feed_item_id, lane=lane,
                kind=result.kind, item=item_text,
            )
            return ActResult(True, STATUS_UNDONE, f"undone: {item_text}", feed_item_id, action_id)
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
) -> ActResult:
    """Apply one deck/feed action through the owning resolver.

    Flow (folded-state check FIRST):
      1. Load the feed item from the store; not present → ``stale_item``.
      2. Not ``open`` → ``already_acted`` (ok-noop; resolvers are idempotent,
         but we don't re-run them — the store already reflects a decision).
      3. Kind comes from ``item.kind`` (defense-in-depth), with an id-prefix
         consistency guard — a mismatch is data corruption → ``error``.
      4. Universal ``ack`` for FYI items → set ``acked`` directly, no resolver.
      5. ``(kind, action_id)`` not in :data:`FEED_ACTIONS` → ``invalid_action``.
      6. Load the authoritative last_batch item by stable-key re-derivation;
         absent → ``stale_item``.
      7. Synthesize the ReplyCorrection + dispatch to the resolver; resolver
         error → ``error`` carrying the resolver's own message.
      8. Success → set ``acted`` → ``acted`` ok.

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
            raw_config=raw_config,
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
    # reconcile); we never re-drive a resolver for it.
    #
    # THE ONE EXCEPTION: ``undo_done`` on a slot_suggestion acts on an item that
    # is DONE — i.e. already ``acted`` (the board's done set it). It must reach
    # the slot dispatcher below to reverse the completion, so it is exempt from
    # the acted-is-final gate. (An undo on an OPEN item — done via a non-board
    # path, still emitted open with evidence.done=true — passes this gate
    # naturally.) Every other (kind, action) on a non-open item is the noop.
    _is_slot_undo = item.kind == SLOT_KIND and action_id == UNDO_DONE_ACTION
    if item.state != STATE_OPEN and not _is_slot_undo:
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

    # slot_suggestion (board DONE path) — a DEDICATED dispatcher, NOT the
    # ReplyCorrection/last_batch path. It acts on the feed item's OWN stamped
    # evidence (the producer's trusted origin/routine_record/tier/item_text,
    # same trust class as last_batch) and calls the per-lane completion writer
    # — the SAME functions the talker uses (single writer per lane). Intercepts
    # BEFORE _load_batch_item because slot items have no last_batch entry.
    if kind == SLOT_KIND:
        return _dispatch_slot_completion(
            feed_item_id, action_id, item,
            feed_store=feed_store, config=config, vault_path=vault_path,
        )

    # Authoritative item from last_batch — re-derived, never ordinal/evidence.
    batch_item = _load_batch_item(kind, feed_item_id, config)
    if batch_item is None:
        log.info(
            "feed.act.stale_item", id=feed_item_id, action=action_id,
            reason="aged_out_of_last_batch",
        )
        return ActResult(
            False, STATUS_STALE_ITEM,
            "this batch has moved on — it'll resurface at the next sync",
            feed_item_id, action_id,
        )

    correction = ReplyCorrection(
        item_number=_SYNTHETIC_ITEM_NUMBER, **kind_actions[action_id],
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

    feed_store.set_state(feed_item_id, STATE_ACTED)
    log.info("feed.act.acted", id=feed_item_id, kind=kind, action=action_id)
    return ActResult(True, STATUS_ACTED, detail, feed_item_id, action_id)
