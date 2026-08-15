"""Append-only event-JSONL feed store with fold-on-load (the audit-log idiom).

NOT a mutable snapshot: every mutation appends an event; the current state is the
LAST-WRITE-WINS fold of the event log. This gives Phase B a full history on day
one and makes concurrent producers (sync daemon + brief daemon both write) safe
without a shared DB.

Concurrency: writes go through ``alfred.common.file_lock.file_rmw_lock`` on a
``.lock`` sidecar (the #37 pattern). Appends alone don't lose data, but
COMPACTION is a read-fold-rewrite — an append racing a compaction's atomic
replace would be lost without the lock. So every writer (append AND compact)
holds the same sidecar lock; compaction rewrites via ``.tmp`` → ``os.replace``.

Forward-compat: an unknown ``ev`` value or an unparseable line is SKIPPED, never
crashed — an older reader tolerates a newer writer's events.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import structlog

from alfred.common.file_lock import file_rmw_lock

from .model import (
    ACTION_RETIRED,
    STATE_ACKED,
    STATE_ACTED,
    STATE_DEFERRED,
    STATE_OPEN,
    FeedItem,
    _now_iso,
    defer_window_open,
    snapshot_fingerprint,
)

log = structlog.get_logger(__name__)

DEFAULT_COMPACT_THRESHOLD_BYTES = 5 * 1024 * 1024  # 5 MiB


def _revival_suppressed(
    kind: str, incoming: FeedItem, stored: FeedItem | None,
    *, now: str | None = None,
) -> bool:
    """Should this reconcile upsert be SKIPPED to keep a decision sticky?

    Reconcile normally upserts every incoming item at ``state=open``, which
    REVIVES a stored item the operator already decided. For episode-shaped
    kinds that is correct and deliberate: a health check that goes ok → warn
    again is genuinely new news. For SNAPSHOT kinds it is the groundhog bug —
    the producer re-emits the same standing item every fire, so an acked
    appointment came back the next morning, and the next, forever.

    Suppress only when ALL of:
      * the item is stored and already decided (``acted`` / ``acked``) —
        an open item is not being revived, and ``expired`` keeps its existing
        behaviour rather than acquiring a new one here;
      * the kind is a snapshot kind (see ``SNAPSHOT_FINGERPRINT_FIELDS``);
      * both fingerprints are computable AND equal — content is unchanged, so
        there is nothing new to tell the operator.

    Any other shape returns False and the pre-existing revive happens. That is
    the read-belt: an item written before this policy (or with evidence we
    can't fingerprint) behaves exactly as it does today rather than being
    silently suppressed forever on the strength of a guess.

    **Defer (D2) rides HERE rather than in a second predicate.** This function
    is already "should this upsert be skipped to keep a prior decision sticky",
    and a defer is exactly that with an expiry. Two mechanisms would be two
    places to fix the groundhog bug, which this codebase has now had to fix
    twice. Note the asymmetry that keeps them honest: a sticky ACK suppresses
    indefinitely (until the content changes), while a defer suppresses only
    until its window passes — so a deferred item ALWAYS has a return date, and
    ``defer_window_open`` is the single place that decides it has arrived.
    """
    if stored is None:
        return False
    if stored.state == STATE_DEFERRED:
        return defer_window_open(stored.deferred_until, now)
    if stored.state not in (STATE_ACTED, STATE_ACKED):
        return False
    fp_incoming = snapshot_fingerprint(kind, incoming.evidence)
    if fp_incoming is None:
        return False
    fp_stored = snapshot_fingerprint(kind, stored.evidence)
    if fp_stored is None:
        return False
    return fp_incoming == fp_stored


class FeedStore:
    """Event-log feed store. One instance per feed file."""

    def __init__(
        self,
        path: str | Path,
        *,
        compact_threshold_bytes: int = DEFAULT_COMPACT_THRESHOLD_BYTES,
    ) -> None:
        self.path = Path(path)
        self.compact_threshold_bytes = compact_threshold_bytes

    # --- read (no lock — folding tolerates a torn tail line) -----------------

    def load(self) -> dict[str, FeedItem]:
        """Fold the event log into ``{id: FeedItem}`` (last-write-wins per id).

        A concurrent appender can only add a (possibly torn) line at the tail;
        an unparseable / unknown-ev / un-constructable event is skipped, so a
        lock-free read never crashes.
        """
        return self._fold_from_disk()

    def _fold_from_disk(self) -> dict[str, FeedItem]:
        if not self.path.is_file():
            return {}
        items: dict[str, FeedItem] = {}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("feed.store.read_failed", path=str(self.path), error=str(exc))
            return {}
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                # Torn tail line or corruption — skip, don't crash.
                continue
            if not isinstance(event, dict):
                continue
            self._apply_event(event, items)
        return items

    @staticmethod
    def _apply_event(event: dict[str, Any], items: dict[str, FeedItem]) -> None:
        ev = event.get("ev")
        if ev == "upsert":
            payload = event.get("item")
            if not isinstance(payload, dict):
                return
            try:
                item = FeedItem.from_dict(payload)
            except (TypeError, ValueError):
                return  # missing id/kind → un-constructable → skip
            if item.id:
                items[item.id] = item
        elif ev == "state":
            item_id = event.get("id")
            new_state = event.get("state")
            if not isinstance(item_id, str) or not isinstance(new_state, str):
                return
            existing = items.get(item_id)
            if existing is None:
                return  # state for an id we've never upserted — nothing to fold onto
            existing.state = new_state
            # The defer window lives and dies with the deferred state. Carrying
            # it out of that state would leave a returned item holding a stale
            # timestamp that a later reader could mistake for a live defer.
            existing.deferred_until = (
                event.get("deferred_until") if new_state == STATE_DEFERRED else None
            )
            if new_state == STATE_ACTED:
                # ``acted_action`` tracks the VERB of the LATEST acted event
                # (newest wins: an accept then a later done ends "done"). A
                # legacy / verbless acted event (e.g. reconcile-decided) → None.
                existing.acted_action = event.get("action")
                if not existing.acted_at:
                    existing.acted_at = event.get("ts") or _now_iso()
            else:
                # Non-acted transition (open via undo/revival, acked, expired) —
                # the item is no longer acted, so clear the verb.
                existing.acted_action = None
        # Unknown ev → deliberately skipped (forward-compat).

    # --- write primitives (assume the lock is HELD by the caller) ------------

    def _append_lines_locked(self, events: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            for event in events:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _maybe_compact_locked(self) -> bool:
        try:
            size = self.path.stat().st_size
        except OSError:
            return False
        if size <= self.compact_threshold_bytes:
            return False
        self._rewrite_locked(self._fold_from_disk())
        return True

    def _rewrite_locked(self, items: dict[str, FeedItem]) -> None:
        """Rewrite the log as fresh upserts of the folded state. Atomic
        (``.tmp`` → ``os.replace``) so a reader never sees a half-rewritten file
        and a concurrent (lock-blocked) appender lands cleanly after."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for item in items.values():
                fh.write(json.dumps({"ev": "upsert", "ts": _now_iso(), "item": item.to_dict()}, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    # --- created_at episode semantics ----------------------------------------

    @staticmethod
    def _episode_merged_payload(item: FeedItem, current: dict[str, FeedItem]) -> dict[str, Any]:
        """Build an upsert payload with EPISODE-correct ``created_at``.

        The feed tracks an item across a continuously-open EPISODE; ``created_at``
        is that episode's first-seen. So MERGE, don't wholesale-replace:

          * stored item exists AND is still ``open`` → preserve its stored
            ``created_at`` (the episode continues; producers pass a fresh
            timestamp each fire, which must NOT drift the first-seen);
          * stored item is ``deferred`` → ALSO preserve it. A defer pauses an
            episode, it does not end one: the operator said "later", not "done".
            Resetting first-seen on return would erase that he has been carrying
            this for a week and make a long-deferred item read as brand new —
            precisely the age signal an attention policy needs most;
          * stored item was ``acted`` / ``acked`` / ``expired`` and the same key
            reappears in the open set → a NEW episode: keep the incoming fresh
            ``created_at``, and the upsert (state=open) legitimately REVIVES it —
            the authoritative store re-opened it, so it IS open again;
          * no stored item → first sighting, keep the incoming ``created_at``.
        """
        payload = item.to_dict()
        stored = current.get(item.id)
        if stored is not None and stored.state in (STATE_OPEN, STATE_DEFERRED):
            payload["created_at"] = stored.created_at
        return payload

    # --- public write API (each acquires the lock once — never nested) -------

    def upsert(self, item: FeedItem) -> None:
        with file_rmw_lock(self.path):
            current = self._fold_from_disk()
            payload = self._episode_merged_payload(item, current)
            self._append_lines_locked([{"ev": "upsert", "ts": _now_iso(), "item": payload}])
            self._maybe_compact_locked()

    def set_state(self, item_id: str, state: str, *, action: str | None = None) -> None:
        """Append a state transition. ``action`` (optional) records the acting
        VERB on an ``acted`` transition (e.g. ``"accept"`` / ``"done"``) so the
        fold can distinguish accept-acted from done-acted; omitted → legacy None
        (backward-compatible with every existing caller)."""
        event: dict[str, Any] = {"ev": "state", "ts": _now_iso(), "id": item_id, "state": state}
        if action is not None:
            event["action"] = action
        with file_rmw_lock(self.path):
            self._append_lines_locked([event])
            self._maybe_compact_locked()

    def defer(self, item_id: str, *, until: str | None = None) -> None:
        """Move an item to ``deferred`` — "later", not "judged" (D2).

        ``until=None`` defers to the NEXT RENDER; an ISO instant defers to A
        TIME. Both shapes go through this one method so the deferral is logged
        in one place and the window can only be set alongside the state.

        ILB, outbound half: every deferral is announced. The return half is
        logged by :meth:`reconcile` when the window lapses, so a deferred item
        leaves a matched pair of log lines and "where did that card go" is a
        greppable question rather than a mystery.
        """
        event: dict[str, Any] = {
            "ev": "state", "ts": _now_iso(), "id": item_id, "state": STATE_DEFERRED,
        }
        if until is not None:
            event["deferred_until"] = until
        with file_rmw_lock(self.path):
            self._append_lines_locked([event])
            self._maybe_compact_locked()
        log.info(
            "feed.store.deferred",
            id=item_id,
            shape="until_time" if until else "next_render",
            deferred_until=until or "",
            detail="item is deferred, not decided — it returns when the window lapses",
        )

    def compact(self) -> None:
        """Force a compaction (rewrite folded state as fresh upserts)."""
        with file_rmw_lock(self.path):
            self._rewrite_locked(self._fold_from_disk())

    def reconcile(self, kind: str, open_items: list[FeedItem]) -> dict[str, int]:
        """The workhorse: make the store's OPEN set for ``kind`` exactly
        ``open_items``.

        Upserts every currently-open item (refreshing evidence/title), and marks
        any item of this kind that was open in the store but is ABSENT from
        ``open_items`` as ``acted`` — the authoritative store decided it
        elsewhere (decided-detection for free, no decided-store reads).

        Load + compute + append happen under ONE lock acquisition so the absent
        set is computed against the same state the writes extend. Idempotent in
        RESULT: re-running with the same open set re-appends upserts but folds to
        the identical state.
        """
        with file_rmw_lock(self.path):
            current = self._fold_from_disk()
            incoming_ids = {item.id for item in open_items}
            # DEFERRED counts as present for absent-detection. A deferred item
            # that vanishes from the producer's open set was resolved in the
            # owning store while the operator had it parked, and marking it
            # acted is the same decided-detection every open item gets. Omitting
            # it would strand the item as deferred forever with no producer left
            # to return it — an immortal ghost, the failure this whole amendment
            # exists to prevent, reintroduced through the back door.
            previously_present = {
                item_id
                for item_id, item in current.items()
                if item.kind == kind and item.state in (STATE_OPEN, STATE_DEFERRED)
            }
            absent = previously_present - incoming_ids

            ts = _now_iso()
            events: list[dict[str, Any]] = []
            suppressed = 0
            held: list[str] = []
            returned: list[str] = []
            for item in open_items:
                stored = current.get(item.id)
                was_deferred = stored is not None and stored.state == STATE_DEFERRED
                if _revival_suppressed(kind, item, stored, now=ts):
                    suppressed += 1
                    if was_deferred:
                        held.append(item.id)
                    continue
                if was_deferred:
                    # The window lapsed — this upsert (state=open) IS the return.
                    returned.append(item.id)
                events.append(
                    {"ev": "upsert", "ts": ts,
                     "item": self._episode_merged_payload(item, current)}
                )
            # THE WOULD-RETIRE-EVERYTHING TRIPWIRE. A producer that returns an
            # empty open set while the store holds open items retires ALL of
            # them, and today that happens in total silence — the same silence
            # whether the operator cleared the deck or the producer broke.
            # This announces it BEFORE the events are written, so the warning
            # survives even if the append below raises.
            #
            # OBSERVATION ONLY: the reconcile proceeds exactly as it always
            # has. The refusal half of the circuit breaker awaits the
            # operator's ratification; shipping the loud half first costs
            # nothing and means the next occurrence is greppable rather than
            # reconstructed. Deliberately inside the lock, unlike this
            # method's other log lines, because "before proceeding" is the
            # whole point of it.
            if not open_items and previously_present:
                log.warning(
                    "feed.store.reconcile_would_retire_all_open",
                    kind=kind,
                    count=len(previously_present),
                    ids=sorted(previously_present, key=str),
                    detail=(
                        "producer returned an EMPTY open set while the store "
                        "held open items — every one of them is being retired "
                        "as acted. Semantics unchanged; this is the signal "
                        "that a producer fault and an emptied deck look "
                        "identical from here."
                    ),
                )
            events.extend(
                # ``action`` marks these as RETIREMENTS — acted because the
                # item left the producer's open set, not because the operator
                # judged it. Without the verb both outcomes are byte-identical
                # verbless events and the distinction is unrecoverable from
                # the log. State is deliberately unchanged: ``acted`` stays
                # ``acted``, and every current reader compares the verb for
                # equality against accept/done/snooze, so a fourth value is
                # inert (see ``ACTION_RETIRED``). Auditability starts the day
                # this ships and does not reach backwards.
                {"ev": "state", "ts": ts, "id": item_id,
                 "state": STATE_ACTED, "action": ACTION_RETIRED}
                # ``key=str`` because the fold does not coerce types: one item
                # carrying a non-string id makes a bare sort's own comparison
                # raise TypeError, and the belt then refuses the whole
                # reconcile — every kind's retirement blocked by one malformed
                # line. The order is only the sequence of appended log events,
                # so rendering the key costs nothing semantic.
                for item_id in sorted(absent, key=str)
            )
            self._append_lines_locked(events)
            self._maybe_compact_locked()

        if returned:
            # ILB, inbound half — the matched pair to ``feed.store.deferred``.
            # A card silently reappearing is as confusing as one silently
            # vanishing; this is the line that explains it.
            log.info(
                "feed.store.defer_returned",
                kind=kind,
                count=len(returned),
                ids=sorted(returned),
                detail="defer window lapsed — item returned to attention",
            )
        if held:
            log.info(
                "feed.store.defer_held",
                kind=kind,
                count=len(held),
                ids=sorted(held),
                detail="still inside the defer window — suppressed this fire, will return",
            )

        if absent:
            # ILB, and the half the belt's ``acted`` COUNT cannot serve: WHICH
            # cards were retired. Same shape as the two defer logs above, for
            # the same reason — a card vanishing needs a line that explains it.
            #
            # Read the wording carefully, because it is doing real work.
            # Retirement here means only "the producer stopped emitting this";
            # the decided-elsewhere reading is an INFERENCE, correct for the
            # case this was designed for and wrong for at least one other. On
            # 2026-08-15 five email_tier cards were dealt against a stale
            # ``last_batch``, the operator's verdicts were all refused, and the
            # next reconcile would have retired them as ``acted`` — recording
            # as decided the very decisions the system had just declined to
            # accept. The act path is fixed; whether an operator-facing decide
            # kind should retire to a state that does not CLAIM a decision is a
            # design question left open deliberately, and boarded rather than
            # answered here. Until then these ids are how that case is found.
            log.info(
                "feed.store.retired_absent",
                kind=kind,
                count=len(absent),
                ids=sorted(absent, key=str),
                detail="absent from the producer's open set this fire — retired",
            )

        return {
            "open": len(open_items),
            "acted": len(absent),
            "suppressed": suppressed,
            "deferred_held": len(held),
            "defer_returned": len(returned),
        }
