"""Web notification store — KAL-LE ticket → web-PWA notify sink (parity #22).

The POLL / READ-ON-REQUEST slice: KAL-LE's ticket intake sends a
``kind=notice`` peer message tagged ``web_notify`` when (and only when) a
ticket ack is ``created``; the principal's transport fans that notice into
this store BESIDE the unchanged Telegram relay, and the PWA polls it back
via ``GET /chat/notifications`` (:mod:`alfred.web.routes_notify`). NO push
channel — that is deferred.

Store contract (mirrors :mod:`alfred.web.state`'s nonce store):

* JSON-backed, atomic writes (``.tmp`` → ``os.replace``), file lives under
  the daemon's ``data_dir`` (``web_notify_state.json``).
* **Bounded per-user**: at most :data:`NOTIFY_CAP` entries per user;
  enqueueing past the cap evicts the OLDEST entries first.
* Schema-tolerant load: a corrupt / partial file is logged and tolerated
  (heals on next save); malformed entries are dropped, unknown entry keys
  survive round-trips untouched.
* Keyed by the web identity's ``synthetic_chat_id`` (per-instance
  single-user v1 — the sink enqueues to the FIRST configured web user,
  the routes read the resolving identity's own key).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .identity import synthetic_chat_id
from .utils import get_logger

log = get_logger(__name__)

# Per-user retention cap. Oldest-evicted beyond this — the store is a
# notification tray, not an archive (the vault record + GitHub issue are
# the durable artifacts).
NOTIFY_CAP = 200


@dataclass
class WebNotifyStore:
    """In-memory mirror of the web-notification state file.

    ``notifications`` maps ``str(user_key) -> [entry, ...]`` ordered
    OLDEST-FIRST (append on enqueue, pop-front on eviction). Each entry::

        {"id": str, "text": str, "precedence": str, "source": str,
         "ticket_uid": str, "issue_url": str, "ts": iso8601, "read": bool}
    """

    state_path: Path
    version: int = 1
    notifications: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    # --- load/save ---------------------------------------------------------

    @classmethod
    def create(cls, state_path: str | Path) -> "WebNotifyStore":
        return cls(state_path=Path(state_path))

    def load(self) -> None:
        """Load state from disk if present; tolerate missing / corrupt."""
        if not self.state_path.exists():
            log.info(
                "web.notify_state.no_existing_state", path=str(self.state_path)
            )
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as exc:
            log.warning(
                "web.notify_state.load_failed",
                path=str(self.state_path),
                error=str(exc),
            )
            return
        if not isinstance(raw, dict):
            log.warning(
                "web.notify_state.load_failed",
                path=str(self.state_path),
                error="top-level JSON is not an object",
            )
            return
        self.version = int(raw.get("version", 1) or 1)
        per_user_raw = raw.get("notifications", {}) or {}
        loaded: dict[str, list[dict[str, Any]]] = {}
        if isinstance(per_user_raw, dict):
            for key, entries in per_user_raw.items():
                if not isinstance(entries, list):
                    continue
                # Keep only well-shaped entries (dict with a non-empty id) —
                # schema-tolerant against drift; extra keys ride along.
                kept = [
                    dict(e)
                    for e in entries
                    if isinstance(e, dict) and str(e.get("id", "") or "")
                ]
                if kept:
                    loaded[str(key)] = kept[-NOTIFY_CAP:]
        self.notifications = loaded
        log.info(
            "web.notify_state.loaded",
            users=len(self.notifications),
            entries=sum(len(v) for v in self.notifications.values()),
        )

    def save(self) -> None:
        """Atomic save: write to ``.tmp`` then ``os.replace``."""
        data = {"version": self.version, "notifications": self.notifications}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, self.state_path)

    # --- notification lifecycle -------------------------------------------

    def enqueue(
        self,
        user_key: int | str,
        *,
        text: str,
        precedence: str = "R",
        source: str = "",
        ticket_uid: str = "",
        issue_url: str = "",
    ) -> dict[str, Any]:
        """Append one unread notification for ``user_key``; saves.

        Evicts oldest entries beyond :data:`NOTIFY_CAP`. Returns the
        minted entry (id + ts stamped here).
        """
        entry = {
            "id": uuid.uuid4().hex[:16],
            "text": str(text),
            "precedence": str(precedence or "R"),
            "source": str(source or ""),
            "ticket_uid": str(ticket_uid or ""),
            "issue_url": str(issue_url or ""),
            "ts": datetime.now(timezone.utc).isoformat(),
            "read": False,
        }
        bucket = self.notifications.setdefault(str(user_key), [])
        bucket.append(entry)
        if len(bucket) > NOTIFY_CAP:
            del bucket[: len(bucket) - NOTIFY_CAP]
        self.save()
        return entry

    def list_for(self, user_key: int | str) -> list[dict[str, Any]]:
        """Return ``user_key``'s notifications NEWEST-FIRST (copies)."""
        bucket = self.notifications.get(str(user_key), [])
        return [dict(e) for e in reversed(bucket)]

    def unread_count(self, user_key: int | str) -> int:
        bucket = self.notifications.get(str(user_key), [])
        return sum(1 for e in bucket if not e.get("read"))

    def ack(self, user_key: int | str, ids: list[str]) -> int:
        """Mark the given ids read for ``user_key``; saves; returns count.

        Unknown / already-read ids are silently skipped (idempotent — a
        retried ack acks 0 the second time, never errors).
        """
        wanted = {str(i) for i in ids}
        if not wanted:
            return 0
        changed = 0
        for entry in self.notifications.get(str(user_key), []):
            if entry.get("id") in wanted and not entry.get("read"):
                entry["read"] = True
                changed += 1
        if changed:
            self.save()
        return changed


def build_web_notify_sink(
    store: WebNotifyStore, web_config: Any,
) -> Callable[..., None]:
    """Build the transport-level fan-out sink over ``store``.

    Registered on the transport app via
    :func:`alfred.transport.peer_handlers.register_web_notify_sink` so the
    ``/peer/send`` ``message|notice`` fan-out reaches this store WITHOUT
    the telegram daemon (or the transport layer) importing web modules at
    module scope. The callable is SYNC — the enqueue is one small atomic
    file write, same in-handler weight as the intake state saves.

    Recipient (v1 single-user ruling): the FIRST configured ``web.users``
    entry is the operator; the entry is keyed to their
    ``synthetic_chat_id`` — the same key the session-authed read routes
    resolve, so only the operator's own identity reads it back. No users
    configured → logged skip (ILB), never a crash.
    """
    users = getattr(web_config, "users", []) or []
    operator = users[0].name if users else ""

    def _sink(
        *,
        payload: dict[str, Any],
        from_peer: str = "",
        correlation_id: str = "",
    ) -> None:
        if not operator:
            # Intentionally-left-blank: an instance with no web.users has
            # no notification recipient — observable skip, not silence.
            log.info(
                "web.notify.sink_no_operator",
                from_peer=from_peer,
                correlation_id=correlation_id,
                reason="web.users empty — no recipient to key the "
                       "notification to",
            )
            return
        text = str(payload.get("text") or payload.get("body") or "")
        if not text:
            log.info(
                "web.notify.sink_empty_text",
                from_peer=from_peer,
                correlation_id=correlation_id,
            )
            return
        entry = store.enqueue(
            synthetic_chat_id(operator),
            text=text,
            precedence=str(payload.get("precedence") or "R"),
            source=str(payload.get("source") or from_peer or ""),
            ticket_uid=str(payload.get("ticket_uid") or ""),
            issue_url=str(payload.get("issue_url") or ""),
        )
        log.info(
            "web.notify.enqueued",
            id=entry["id"],
            from_peer=from_peer,
            ticket_uid=entry["ticket_uid"] or None,
            correlation_id=correlation_id,
        )

    return _sink
