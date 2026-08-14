"""Contact-surface router state — the store behind ``/day/*`` (C4).

The consumer of ``preference/Algernon — contact-surface routing.md``, whose own
text said "no Algernon-side daemon consumes this preference yet". This module
holds the two things the spec's rule set needs and the vault record cannot
supply: the **day-state** the rules evaluate against, and the **contact log**
that makes the router self-correcting.

WHY A STORE AT ALL — the design doc called the day-state endpoint a "read-only
aggregation of state that already exists". Two of its four fields do not exist:

* ``unresolved_flagged_notifications`` — EXISTS (:mod:`alfred.web.notify_state`).
* ``last_session_ended``               — EXISTS (:mod:`alfred.telegram.state`).
* ``brief_read_today``                 — nothing records that the brief was read.
* ``last_active_surface``              — nothing records which surface was used.

So the router writes what it reads. That is not a workaround: the spec already
requires a write path ("Overrides are logged, with the triggering state"), and
once contacts are logged, ``brief_read_today`` and ``last_active_surface`` fall
out of the same log for free — one store rather than three.

SINGLE WRITER. Like :mod:`alfred.web.notify_state`, this file is written only by
the talker daemon (the ``/day/*`` handlers and the feed-act dispatcher both run
there). No sweep loop, no second daemon, no lock — single-writer by construction.

PATH RESOLUTION IS SHARED, NOT COPIED. :func:`resolve_contact_state_path` is THE
parse of the key. The web route reaches it through ``WebConfig`` (resolved at
config load, where the unified dict is in hand) and the feed-act dispatcher
reaches it by re-reading the instance's own config file — the same two-consumers-
one-helper discipline as ``tier.snooze.resolve_snooze_path``, and for the same
reason: a writer and a reader that derive a path separately eventually derive
different paths, and the failure is silent (writes land where nothing reads).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alfred.common.instance_paths import configured_logging_dir

from .utils import get_logger

log = get_logger(__name__)

# The state file, beside ``web_notify_state.json`` in the instance's own data
# dir. Named here so the two resolvers below cannot spell it differently.
CONTACT_STATE_FILENAME = "web_contact_state.json"

# Per-user contact retention cap. The pattern window is 14 days by default and
# contacts are a handful a day, so this holds several windows — oldest-evicted
# beyond it. A tray-sized bound, matching NOTIFY_CAP's reasoning: this is
# working state for a rolling window, not an archive.
CONTACT_CAP = 400

# ---------------------------------------------------------------------------
# The shared vocabulary. BOTH SIDES OF THE WIRE SPEAK THESE STRINGS — the PWA
# sends a rule id + surface on every contact, and this module refuses anything
# outside the vocabulary rather than storing a client-supplied string. The
# TypeScript spelling lives in ``web/lib/algernon/contactRouter.ts`` and the two
# are pinned equal by ``tests/web/test_contact_router_vocabulary_parity.py``
# (the SEGMENT_ORDER parity pattern) — TypeScript cannot import a Python tuple,
# so the second spelling is deliberate and the pin is what makes it safe.
# ---------------------------------------------------------------------------

# Rule ids, in the spec's priority order.
RULE_RESUME_PENDING_CAPTURE = "resume_pending_capture"   # 1 — NOT armed in v1
RULE_UNRESOLVED_NOTIFICATION = "unresolved_notification"  # 2
RULE_FIRST_CONTACT_AFTER_GAP = "first_contact_after_gap"  # 3
RULE_DEFAULT = "default"                                  # 4

# Priority order, whole spec. Includes the unarmed rule 1 on purpose: the
# ordering is the spec's, and dropping the unarmed rung from the ORDER would
# make the order a different object from the one the record describes.
RULE_ORDER: tuple[str, ...] = (
    RULE_RESUME_PENDING_CAPTURE,
    RULE_UNRESOLVED_NOTIFICATION,
    RULE_FIRST_CONTACT_AFTER_GAP,
    RULE_DEFAULT,
)

# What v1 actually evaluates. Rule 1 needs ``open_capture_pending`` — capture
# session state that does not exist yet — so it is DECLARED unarmed rather than
# silently absent (the ratified degraded start; the endpoint serves this list so
# the operator can see which rungs are live).
ARMED_RULES: tuple[str, ...] = (
    RULE_UNRESOLVED_NOTIFICATION,
    RULE_FIRST_CONTACT_AFTER_GAP,
    RULE_DEFAULT,
)

# Why each unarmed rule is unarmed — served beside ARMED_RULES so "rule 1 is
# missing" is answerable from the payload, not from this source file.
UNARMED_RULE_REASONS: dict[str, str] = {
    RULE_RESUME_PENDING_CAPTURE: (
        "needs open_capture_pending — the PWA does not track capture-session "
        "state yet, so this rung cannot be evaluated and is declared off"
    ),
}

# Surfaces the router may open, and that an override may name. These are PWA
# surfaces that exist today. ``capture`` is deliberately ABSENT: it is rule 1's
# surface, and rule 1 is unarmed — a vocabulary entry for a surface nothing can
# route to would be the same silent-absence the ARMED_RULES list exists to stop.
SURFACE_HOME = "home"
SURFACE_CHAT = "chat"
SURFACE_FEED = "feed"
SURFACE_BRIEF = "brief"
SURFACE_DECK = "deck"
SURFACE_PLAYER = "player"
SURFACE_INGEST = "ingest"
SURFACE_BATCH = "batch"

SURFACES: tuple[str, ...] = (
    SURFACE_HOME,
    SURFACE_CHAT,
    SURFACE_FEED,
    SURFACE_BRIEF,
    SURFACE_DECK,
    SURFACE_PLAYER,
    SURFACE_INGEST,
    SURFACE_BATCH,
)

# Spec defaults for the two levers, used when the preference record is missing,
# unreadable, or omits them. Defined HERE, server-side, and served to the client
# — nothing in the PWA carries a lever default, so a Hypatia-side edit to the
# record is the only place either number is set.
DEFAULT_GAP_HOURS_NEW_DAY = 6.0
DEFAULT_BRIEF_READ_DECAY_HOURS = 12.0

# Spec defaults for pattern-surfacing.
DEFAULT_PATTERN_ENABLED = True
DEFAULT_PATTERN_MIN_OBSERVATIONS = 3
DEFAULT_PATTERN_WINDOW_DAYS = 14
DEFAULT_PATTERN_SAME_CONTEXT_REQUIRED = True
DEFAULT_PATTERN_THRESHOLD_RATIO = 0.6


def resolve_contact_state_path(raw: Any) -> str | None:
    """``web.contact_router.state_path`` out of a unified config dict, or ``None``.

    THE single parse of this key. The ``/day/*`` writer (through ``WebConfig``)
    and the feed-act dispatcher (through the instance's own config file) both
    come through here, so they cannot resolve to different files.

    Explicit config always wins. Otherwise the path is derived from the
    instance's OWN data dir via :func:`configured_logging_dir` — deliberately
    NOT ``instance_data_dir``, whose ``./data`` fallback is cwd-relative and is
    exactly how KAL-LE's writer landed in Salem's store (#74). When nothing
    anchors the path this returns ``None``, and the router is then honestly
    unwired: the endpoint says so and the PWA stays where it is. A router that
    guessed the cwd would route on one instance's habits using another's log.
    """
    if not isinstance(raw, dict):
        return None
    web_section = raw.get("web")
    if isinstance(web_section, dict):
        router_section = web_section.get("contact_router")
        if isinstance(router_section, dict):
            explicit = str(router_section.get("state_path") or "")
            if explicit:
                return explicit
    data_dir = configured_logging_dir(raw)
    if not data_dir:
        return None
    # String join (not pathlib) so a ``./data`` anchor keeps its exact string —
    # the same byte-identity reason ``feed.config._default_store_path`` gives.
    return f"{data_dir.rstrip('/')}/{CONTACT_STATE_FILENAME}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: Any) -> "datetime | None":
    """Parse an ISO timestamp to an aware datetime, or ``None`` if unusable.

    ``None`` rather than a guess is load-bearing everywhere this is used: an
    unparseable contact timestamp must not be read as "just now" (which would
    suppress rule 3 forever) NOR as "long ago" (which would fire it forever).
    Callers skip what they cannot date.
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
class WebContactStore:
    """In-memory mirror of the contact-router state file.

    ``contacts`` maps ``str(user_key) -> [contact, ...]`` ordered OLDEST-FIRST
    (append on record, pop-front on eviction). Each contact::

        {"id": str, "ts": iso8601,
         "rule": str,                  # the rule that fired (RULE_ORDER member)
         "surface": str,               # what the router OPENED
         "landed": str,                # where the operator ended up
         "overridden": bool,
         "overridden_at": iso8601,     # present once overridden
         "state": {...}}               # the triggering state, per the spec

    ``landed`` starts equal to ``surface`` and moves on override, so a reader
    never has to compute "where did they actually end up" from two fields.

    ``adopted`` maps ``user_key -> {rule: surface}`` — operator-APPROVED
    per-rule surface overrides (the pattern card's ``adopt`` verb). This is the
    only thing in the system that changes what the router does, and it can only
    be written by an explicit operator tap.

    ``suppressed`` maps ``user_key -> {pattern_key: iso8601-until}`` — patterns
    the operator dismissed or deferred, and when they may be surfaced again.
    Without it a dismissed card revives the moment the pattern is re-detected,
    which is the acked-cards-revive failure CLAUDE.md already records twice.
    """

    state_path: Path
    version: int = 1
    contacts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    adopted: dict[str, dict[str, str]] = field(default_factory=dict)
    suppressed: dict[str, dict[str, str]] = field(default_factory=dict)

    # --- load/save ---------------------------------------------------------

    @classmethod
    def create(cls, state_path: str | Path) -> "WebContactStore":
        return cls(state_path=Path(state_path))

    def load(self) -> None:
        """Load state from disk if present; tolerate missing / corrupt."""
        if not self.state_path.exists():
            log.info(
                "web.contact_state.no_existing_state", path=str(self.state_path)
            )
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError) as exc:
            log.warning(
                "web.contact_state.load_failed",
                path=str(self.state_path),
                error=str(exc),
            )
            return
        if not isinstance(raw, dict):
            log.warning(
                "web.contact_state.load_failed",
                path=str(self.state_path),
                error="top-level JSON is not an object",
            )
            return
        self.version = int(raw.get("version", 1) or 1)
        self.contacts = _load_contacts(raw.get("contacts"))
        self.adopted = _load_str_map(raw.get("adopted"))
        self.suppressed = _load_str_map(raw.get("suppressed"))
        log.info(
            "web.contact_state.loaded",
            users=len(self.contacts),
            contacts=sum(len(v) for v in self.contacts.values()),
            adopted=sum(len(v) for v in self.adopted.values()),
            suppressed=sum(len(v) for v in self.suppressed.values()),
        )

    def save(self) -> None:
        """Atomic save: write to ``.tmp`` then ``os.replace``."""
        data = {
            "version": self.version,
            "contacts": self.contacts,
            "adopted": self.adopted,
            "suppressed": self.suppressed,
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp_path, self.state_path)

    # --- contact log -------------------------------------------------------

    def record_contact(
        self,
        user_key: int | str,
        *,
        rule: str,
        surface: str,
        state: dict[str, Any] | None = None,
        brief_date: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Append one contact for ``user_key``; saves; returns the entry.

        ``rule`` and ``surface`` are assumed already validated against the
        vocabulary by the caller (the route validates, so an invalid value is a
        4xx rather than a stored string nothing can interpret).

        ``brief_date`` is the date of the brief artifact that was CURRENT when
        this contact happened, read server-side by the route. It is what makes
        "has the operator read today's brief" answerable: without it the only
        available question is "how long ago did they land on the brief", and a
        28-second glance at YESTERDAY'S brief answers that identically to a real
        read of today's. Empty string means unrecorded (a contact written before
        this field existed, or an instance with no spool) — and every reader
        treats unrecorded as NOT-read, so the failure direction is being offered
        the brief again rather than having it silently suppressed.
        """
        stamp = (now or _now()).isoformat()
        entry = {
            "id": uuid.uuid4().hex[:16],
            "ts": stamp,
            "rule": rule,
            "surface": surface,
            # Starts equal to ``surface``; moves on override. One field answers
            # "where did they end up" so no reader recomputes it.
            "landed": surface,
            "overridden": False,
            # The triggering state, verbatim — the spec requires overrides to be
            # logged WITH it, and an override is only discovered later, so the
            # state must already be on the contact when the tap arrives.
            "state": dict(state or {}),
            # The brief that was current at this moment (see the docstring).
            "brief_date": str(brief_date or ""),
        }
        bucket = self.contacts.setdefault(str(user_key), [])
        bucket.append(entry)
        if len(bucket) > CONTACT_CAP:
            del bucket[: len(bucket) - CONTACT_CAP]
        self.save()
        return entry

    def record_override(
        self,
        user_key: int | str,
        contact_id: str,
        *,
        surface: str,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Mark ``contact_id`` overridden to ``surface``; saves; returns the entry.

        Returns ``None`` for an unknown id — the caller answers 404 rather than
        minting a contact, because an override with no contact to attach to has
        no triggering state, and a pattern observation without its context is
        exactly what ``same_context_required`` exists to exclude.

        Idempotent in the way that matters: re-overriding to the SAME surface
        rewrites the same values. A second override to a DIFFERENT surface moves
        ``landed`` again — the operator changing their mind twice is one contact
        that ended somewhere, not two observations.
        """
        bucket = self.contacts.get(str(user_key), [])
        for entry in bucket:
            if entry.get("id") != contact_id:
                continue
            entry["overridden"] = True
            entry["landed"] = surface
            entry["overridden_at"] = (now or _now()).isoformat()
            self.save()
            return dict(entry)
        return None

    def contacts_for(self, user_key: int | str) -> list[dict[str, Any]]:
        """``user_key``'s contacts OLDEST-FIRST (copies)."""
        return [dict(e) for e in self.contacts.get(str(user_key), [])]

    def last_contact_ts(self, user_key: int | str) -> datetime | None:
        """The most recent datable contact timestamp, or ``None``."""
        stamps = [
            ts
            for ts in (
                parse_ts(e.get("ts")) for e in self.contacts.get(str(user_key), [])
            )
            if ts is not None
        ]
        return max(stamps) if stamps else None

    def last_landed_surface(self, user_key: int | str) -> str:
        """The surface the operator ended up on last, or ``""``.

        Walks backwards to the newest contact carrying a KNOWN surface. An
        unknown value (a store written by a build with a wider vocabulary) is
        skipped rather than served: rule 4 routes to this, and routing to a
        surface this build cannot map is a dead navigation.
        """
        for entry in reversed(self.contacts.get(str(user_key), [])):
            landed = str(entry.get("landed") or "")
            if landed in SURFACES:
                return landed
        return ""

    def last_brief_read(
        self, user_key: int | str
    ) -> "tuple[datetime | None, str]":
        """The operator's most recent brief LANDING — ``(when, which)``.

        ``which`` is the ``brief_date`` recorded on that contact: the artifact
        that was current when they landed. ``("", None)`` shapes mean nothing to
        compare, and every caller must read that as NOT-read rather than as a
        pass.

        ``landed``, not ``surface``: a contact the router sent to the brief and
        the operator immediately overrode away from is not a brief read. That
        distinction is the whole reason ``landed`` is a separate field.
        """
        best_ts: datetime | None = None
        best_date = ""
        for entry in self.contacts.get(str(user_key), []):
            if str(entry.get("landed") or "") != SURFACE_BRIEF:
                continue
            ts = parse_ts(entry.get("ts"))
            if ts is None:
                continue
            if best_ts is None or ts > best_ts:
                best_ts = ts
                best_date = str(entry.get("brief_date") or "")
        return best_ts, best_date

    def last_brief_contact_ts(self, user_key: int | str) -> datetime | None:
        """When the operator last LANDED on the brief, or ``None``.

        Kept as the timestamp-only view; :meth:`last_brief_read` is the one that
        can answer WHICH brief, and is what the day-state derivation uses.
        """
        stamps = [
            ts
            for ts in (
                parse_ts(e.get("ts"))
                for e in self.contacts.get(str(user_key), [])
                if str(e.get("landed") or "") == SURFACE_BRIEF
            )
            if ts is not None
        ]
        return max(stamps) if stamps else None

    # --- operator-approved adjustments -------------------------------------

    def adopt_default(
        self, user_key: int | str, *, rule: str, surface: str
    ) -> None:
        """Record an operator-APPROVED per-rule surface override; saves.

        The one path by which the router's behaviour changes, and it is reached
        only by an explicit tap on a pattern card. Nothing here mutates the
        preference record: the rule set and levers stay Hypatia-owned, and this
        is the operator's local answer to "you keep going somewhere else".
        """
        self.adopted.setdefault(str(user_key), {})[rule] = surface
        self.save()

    def adopted_for(self, user_key: int | str) -> dict[str, str]:
        """``{rule: surface}`` the operator has approved (copy)."""
        return dict(self.adopted.get(str(user_key), {}))

    def suppress_pattern(
        self,
        user_key: int | str,
        pattern_key: str,
        *,
        days: int,
        now: datetime | None = None,
    ) -> str:
        """Silence ``pattern_key`` for ``days``; saves; returns the ISO until.

        ``days <= 0`` is treated as one day rather than as "forever" or as
        "already expired": a zero reaching here is a caller bug, and both of the
        other readings are worse than a short silence.
        """
        until = (now or _now()) + timedelta(days=max(1, days))
        stamp = until.isoformat()
        self.suppressed.setdefault(str(user_key), {})[pattern_key] = stamp
        self.save()
        return stamp

    def is_pattern_suppressed(
        self,
        user_key: int | str,
        pattern_key: str,
        *,
        now: datetime | None = None,
    ) -> bool:
        """True while ``pattern_key`` is inside its suppression window.

        An unparseable ``until`` is read as NOT suppressed — failing the other
        way would silence a pattern permanently on a corrupt timestamp, and a
        pattern the operator can no longer be told about is worse than one they
        are told about twice.
        """
        raw_until = self.suppressed.get(str(user_key), {}).get(pattern_key)
        until = parse_ts(raw_until)
        if until is None:
            return False
        return (now or _now()) < until


def _load_contacts(raw: Any) -> dict[str, list[dict[str, Any]]]:
    """Schema-tolerant contact load — keeps well-shaped entries, drops the rest."""
    out: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(raw, dict):
        return out
    for key, entries in raw.items():
        if not isinstance(entries, list):
            continue
        kept = [
            dict(e)
            for e in entries
            if isinstance(e, dict) and str(e.get("id", "") or "")
        ]
        if kept:
            out[str(key)] = kept[-CONTACT_CAP:]
    return out


def _load_str_map(raw: Any) -> dict[str, dict[str, str]]:
    """Schema-tolerant ``user -> {str: str}`` load (adopted / suppressed)."""
    out: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return out
    for key, mapping in raw.items():
        if not isinstance(mapping, dict):
            continue
        kept = {
            str(k): str(v)
            for k, v in mapping.items()
            if isinstance(k, str) and isinstance(v, str) and k and v
        }
        if kept:
            out[str(key)] = kept
    return out
