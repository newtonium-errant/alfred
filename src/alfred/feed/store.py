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

from .model import STATE_ACTED, STATE_OPEN, FeedItem, _now_iso

log = structlog.get_logger(__name__)

DEFAULT_COMPACT_THRESHOLD_BYTES = 5 * 1024 * 1024  # 5 MiB


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
          * stored item was ``acted`` / ``acked`` / ``expired`` and the same key
            reappears in the open set → a NEW episode: keep the incoming fresh
            ``created_at``, and the upsert (state=open) legitimately REVIVES it —
            the authoritative store re-opened it, so it IS open again;
          * no stored item → first sighting, keep the incoming ``created_at``.
        """
        payload = item.to_dict()
        stored = current.get(item.id)
        if stored is not None and stored.state == STATE_OPEN:
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
            previously_open = {
                item_id
                for item_id, item in current.items()
                if item.kind == kind and item.state == STATE_OPEN
            }
            absent = previously_open - incoming_ids

            ts = _now_iso()
            events: list[dict[str, Any]] = [
                {"ev": "upsert", "ts": ts, "item": self._episode_merged_payload(item, current)}
                for item in open_items
            ]
            events.extend(
                {"ev": "state", "ts": ts, "id": item_id, "state": STATE_ACTED}
                for item_id in sorted(absent)
            )
            self._append_lines_locked(events)
            self._maybe_compact_locked()

        return {"open": len(open_items), "acted": len(absent)}
