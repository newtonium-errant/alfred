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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .identity import synthetic_chat_id
from .utils import get_logger

log = get_logger(__name__)

# Per-user retention cap. Oldest-evicted beyond this — the store is a
# notification tray, not an archive (the vault record + GitHub issue are
# the durable artifacts).
NOTIFY_CAP = 200

# #76 — cap on the ticket body an entry carries. Owned HERE, by the store that
# enforces it: this is the last point every producer passes through, so a
# producer cannot route around it and no second literal can drift from it. The
# producer deliberately does NOT truncate — it sends what it has and the store
# decides, which keeps the bound in one place. 4000 mirrors the feed's evidence
# body cap so the two read-surfaces agree on "too long to inline".
NOTIFY_BODY_MAX_CHARS = 4000

# #86 — how a notification LEAVES the tray.
#
# The operator's report: a ticket notice they had already read sat on screen for
# four days with no way to clear it. ``read`` restyles an entry forever; nothing
# ever removed one. So ``dismissed`` is added as a SECOND, terminal state:
#
#   unread    → bold, counted in the pill, offers "Mark read"
#   read      → calm, uncounted, collapsed out of the glance list
#   dismissed → not rendered at all, still IN the store (audit trail)
#
# Dismissal is reached two ways, and both land on the same field so there is
# exactly one rendering rule to reason about:
#   * the operator dismisses it        → reason ``operator``
#   * it ages out of the retention window → reason ``aged_out``
DISMISS_REASON_OPERATOR = "operator"
DISMISS_REASON_AGED_OUT = "aged_out"


def _safe_http_url(value: Any) -> str:
    """Return ``value`` only if it is an ``http(s)`` URL, else ``""`` (#22 XSS guard).

    A peer-supplied ``issue_url`` is rendered into an ``<a href>`` in the
    OPERATOR's PWA session, so a ``javascript:`` / ``data:`` scheme would be a
    stored-XSS sink (React only *warns* on a javascript: href — it does not
    block it). The backend is the authority: drop any value whose scheme isn't
    http(s), keeping the field SHAPE (empty string) rather than the dangerous
    value. Case-insensitive; surrounding whitespace stripped. The FE zod schema
    mirrors this as defense-in-depth.
    """
    s = str(value or "").strip()
    if s.lower().startswith(("http://", "https://")):
        return s
    return ""


def _parse_ts(value: Any) -> "datetime | None":
    """Parse an entry's ``ts`` to an aware datetime, or ``None`` if unusable.

    ``None`` on anything unparseable is load-bearing for :meth:`retire_aged`:
    an entry whose age cannot be established must NOT be treated as ancient.
    Failing the other way would auto-clear notices on a guess about their age.

    A naive timestamp (no offset) is read as UTC — every writer here stamps
    ``datetime.now(timezone.utc).isoformat()``, so a naive value can only come
    from a hand-edited file, and UTC is the only defensible reading of it.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class WebNotifyStore:
    """In-memory mirror of the web-notification state file.

    ``notifications`` maps ``str(user_key) -> [entry, ...]`` ordered
    OLDEST-FIRST (append on enqueue, pop-front on eviction). Each entry::

        {"id": str, "text": str, "precedence": str, "source": str,
         "ticket_uid": str, "issue_url": str, "ts": iso8601, "read": bool,
         "dismissed": bool,                       # #86, terminal for rendering
         "dismissed_at": iso8601,                 # present once dismissed
         "dismissed_reason": "operator"|"aged_out"}

    ``dismissed`` and its two companions are ADDITIVE: pre-#86 entries simply
    lack them and every check is a falsy ``.get``, so no migration is needed
    and a rollback leaves the older build reading the file unchanged.
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
        ticket_body: str = "",
        issue_number: Any = 0,
    ) -> dict[str, Any]:
        """Append one unread notification for ``user_key``; saves.

        Evicts oldest entries beyond :data:`NOTIFY_CAP`. Returns the
        minted entry (id + ts stamped here).

        NOTE (Nit B, accepted-as-documented): there is no ``ticket_uid`` dedup
        here — a repeated enqueue of the same ticket appends a duplicate entry.
        Safe while the producer is single-shot best-effort (the KAL-LE ticket
        poll fires once per ticket); add a ``ticket_uid`` dedup guard if a
        RETRYING producer is ever introduced.
        """
        raw_body = str(ticket_body or "")
        truncated = len(raw_body) > NOTIFY_BODY_MAX_CHARS
        body_text = raw_body[:NOTIFY_BODY_MAX_CHARS] if truncated else raw_body
        entry = {
            "id": uuid.uuid4().hex[:16],
            "text": str(text),
            "precedence": str(precedence or "R"),
            "source": str(source or ""),
            "ticket_uid": str(ticket_uid or ""),
            # #22 XSS guard — a peer-supplied issue_url renders into an <a href>
            # in the operator's session; store it only if it's an http(s) URL.
            "issue_url": _safe_http_url(issue_url),
            # #76 — the ticket's content, so the PWA card can expand and be READ
            # rather than merely linked to. Always PRESENT, even when empty:
            # that is what lets the renderer tell "this notice carries no
            # content" from "this notice predates the field", and only the first
            # of those deserves the honest "content unavailable" line.
            #
            # Bounded HERE, at the store, because this is the last point every
            # producer passes through — a cap applied only at the notify site
            # would miss any future producer.
            "ticket_body": body_text,
            "ticket_body_truncated": truncated,
            "issue_number": issue_number if isinstance(issue_number, int) else 0,
            "ts": datetime.now(timezone.utc).isoformat(),
            "read": False,
            # #86 — always PRESENT on a new entry, for the same reason
            # ``ticket_body`` is: a reader can then tell "not dismissed" from
            # "predates the field" without guessing. Entries written before
            # #86 simply lack it, and every check is a falsy ``.get``, so the
            # rollout needs no migration.
            "dismissed": False,
        }
        bucket = self.notifications.setdefault(str(user_key), [])
        bucket.append(entry)
        if len(bucket) > NOTIFY_CAP:
            del bucket[: len(bucket) - NOTIFY_CAP]
        self.save()
        return entry

    def list_for(
        self,
        user_key: int | str,
        *,
        include_dismissed: bool = False,
    ) -> list[dict[str, Any]]:
        """Return ``user_key``'s notifications NEWEST-FIRST (copies).

        Dismissed entries are EXCLUDED by default (#86). That default is the
        one that matters, because this method has two callers in two different
        daemons: the PWA tray (``routes_notify``) and the daily-sync brief
        (``daily_sync.ticket_notify_section``). If dismissal only hid an entry
        in the tray, a notice the operator had explicitly cleared would
        reappear in tomorrow's brief — the revive-after-ack failure the health
        cards already taught us (CLAUDE.md: acked cards reviving because one
        surface forgot the other's terminal state). One filter here fixes both
        surfaces at once.

        ``include_dismissed=True`` is the audit escape: the entries never leave
        the store, so anything that needs the full history can still ask for it.

        READ-ONLY, and deliberately so — see :meth:`retire_aged` for why this
        method must never write.
        """
        bucket = self.notifications.get(str(user_key), [])
        out = reversed(bucket) if include_dismissed else (
            e for e in reversed(bucket) if not e.get("dismissed")
        )
        return [dict(e) for e in out]

    def unread_count(self, user_key: int | str) -> int:
        """Unread AND not dismissed.

        ``dismiss`` already marks an entry read, so the second clause is
        unreachable through the API — it is here for a store file that was
        hand-edited or written by an older build. A badge counting something
        the tray does not render is the exact inversion of the
        intentionally-left-blank rule: it reports work that cannot be found.
        """
        bucket = self.notifications.get(str(user_key), [])
        return sum(
            1 for e in bucket if not e.get("read") and not e.get("dismissed")
        )

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

    def dismiss(self, user_key: int | str, ids: list[str]) -> int:
        """Dismiss the given ids for ``user_key``; saves; returns count (#86).

        Dismissal is TERMINAL for rendering and NON-DESTRUCTIVE for storage:
        the entry stops appearing in the tray and the brief, and stays in the
        file. The store is a tray, not an archive — but it is also the only
        record that a notice was ever delivered, so clearing the screen must
        not erase the evidence.

        Dismissing also marks the entry READ. Not a convenience: it makes
        "dismissed but unread" unreachable, so there is no state in which the
        unread pill counts something the operator cannot see or reach. One
        invariant (dismissed ⇒ read) beats a compound condition every reader
        of this store would otherwise have to remember.

        Idempotent, like :meth:`ack` — unknown or already-dismissed ids are
        skipped, so a retried dismiss returns 0 and never errors.
        """
        wanted = {str(i) for i in ids}
        if not wanted:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        changed = 0
        for entry in self.notifications.get(str(user_key), []):
            if entry.get("id") in wanted and not entry.get("dismissed"):
                entry["dismissed"] = True
                entry["read"] = True
                entry["dismissed_at"] = now
                entry["dismissed_reason"] = DISMISS_REASON_OPERATOR
                changed += 1
        if changed:
            self.save()
        return changed

    def retire_aged(
        self,
        user_key: int | str,
        *,
        max_age_days: int,
        now: datetime | None = None,
    ) -> int:
        """Auto-dismiss READ entries older than ``max_age_days``; returns count.

        The third way a tray empties, and the only one needing no operator
        action: a notice read three weeks ago is history, not a task.

        WHY THIS IS A SEPARATE METHOD AND NOT A HOOK INSIDE ``list_for``.
        ``list_for`` has two callers in two different DAEMONS — the talker
        (which owns this file and is its only writer) and the daily-sync brief
        (documented read-only). Retiring inside the read path would turn the
        brief daemon into a second writer of an unlocked JSON file, and two
        read-modify-write processes with no lock is a lost-update generator:
        the brief's save would clobber whatever the talker enqueued or acked in
        between. So retirement lives in an explicitly-named method that only
        the owning process calls (``routes_notify``'s list handler), and the
        read path stays pure.

        UNREAD ENTRIES ARE NEVER RETIRED, at any age. An unread notice that
        aged out would be work the operator never saw, deleted for being old —
        which is the failure this whole feature exists to prevent, pointed the
        other way. Age is only permission to clear something already seen.

        ``max_age_days <= 0`` disables retirement (an operator choice: keep the
        tray until it is cleared by hand), and says so rather than silently
        retiring everything on a zero.
        """
        key = str(user_key)
        bucket = self.notifications.get(key, [])
        if max_age_days <= 0:
            # ILB: "retention off" is a configured state, not a broken sweep.
            log.info(
                "web.notify.retire_disabled",
                user_key=key,
                entries=len(bucket),
                detail="web.notifications.retain_read_days <= 0 — read "
                       "entries are kept until dismissed by hand",
            )
            return 0

        cutoff = (now or datetime.now(timezone.utc)) - timedelta(
            days=max_age_days,
        )
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        retired = 0
        for entry in bucket:
            if entry.get("dismissed") or not entry.get("read"):
                continue
            ts = _parse_ts(entry.get("ts"))
            if ts is None or ts > cutoff:
                # An unparseable timestamp is NOT treated as ancient. Failing
                # open here would auto-clear entries whose age we cannot
                # actually establish — deleting on a guess.
                continue
            entry["dismissed"] = True
            entry["dismissed_at"] = stamp
            entry["dismissed_reason"] = DISMISS_REASON_AGED_OUT
            retired += 1
        if retired:
            self.save()

        # ILB: fires on EVERY sweep including zero. A retirement mechanism that
        # only speaks when it acts is indistinguishable from one that stopped
        # running, and this one runs on a read path where nobody would notice.
        log.info(
            "web.notify.retired_aged",
            user_key=key,
            retired=retired,
            entries=len(bucket),
            max_age_days=max_age_days,
            detail=(
                "ran, nothing to retire — no read entry is older than the "
                "retention window" if not retired else
                "read entries past the retention window were auto-dismissed"
            ),
        )
        return retired


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
            # #76 — the ticket content the intake sent with the notice. Threaded
            # HERE as well as accepted by enqueue: a parameter the store takes
            # but no producer passes is a feature that is green in every unit
            # test and dead in the field.
            ticket_body=str(payload.get("ticket_body") or ""),
            issue_number=payload.get("issue_number") or 0,
        )
        log.info(
            "web.notify.enqueued",
            id=entry["id"],
            from_peer=from_peer,
            ticket_uid=entry["ticket_uid"] or None,
            correlation_id=correlation_id,
        )

    return _sink
