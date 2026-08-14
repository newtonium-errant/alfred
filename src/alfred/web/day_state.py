"""Day-state aggregation — what the contact-surface router evaluates against (C4).

Assembles the spec's state model from the three places the facts actually live:

* the notification tray  (:mod:`alfred.web.notify_state`)   — rule 2's input
* the talker session log (:mod:`alfred.telegram.state`)     — rule 3's input
* the contact log        (:mod:`alfred.web.contact_state`)  — rule 3 + rule 4

and the two LEVERS from the operator's own preference record, so a Hypatia-side
edit to ``preference/Algernon — contact-surface routing.md`` changes the
router's behaviour with no deploy. Nothing client-side carries a lever default:
the PWA evaluates using the numbers this module serves it, and this module is
the only place the spec's defaults are written down.

FIELDS THAT CANNOT BE COMPUTED ARE ABSENT, NOT FALSE. Rule 1
(``open_capture_pending`` / ``pending_capture_id``) has no state source in the
PWA yet. Serving ``false`` would be a fabricated value that reads exactly like a
real one; instead the fields are omitted and :data:`~alfred.web.contact_state.
UNARMED_RULE_REASONS` says why, beside the armed-rule list. A degraded start
that declares itself, per the ratified C4 scope.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contact_state import (
    ARMED_RULES,
    DEFAULT_BRIEF_READ_DECAY_HOURS,
    DEFAULT_GAP_HOURS_NEW_DAY,
    DEFAULT_PATTERN_ENABLED,
    DEFAULT_PATTERN_MIN_OBSERVATIONS,
    DEFAULT_PATTERN_SAME_CONTEXT_REQUIRED,
    DEFAULT_PATTERN_THRESHOLD_RATIO,
    DEFAULT_PATTERN_WINDOW_DAYS,
    RULE_ORDER,
    UNARMED_RULE_REASONS,
    WebContactStore,
    parse_ts,
)
from .utils import get_logger

log = get_logger(__name__)

# The matcher rule id the preference record carries. Matched against
# ``matcher.rule``; the record's ``matcher.domain`` is ``algernon``.
CONTACT_ROUTER_RULE = "contact_surface_open"
CONTACT_ROUTER_DOMAIN = "algernon"

# Where the levers came from — served so the operator can tell a record that is
# being read from one that is being silently ignored.
LEVERS_FROM_RECORD = "preference_record"
LEVERS_FROM_DEFAULTS = "defaults"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _float_or(value: Any, default: float) -> float:
    """Coerce to a POSITIVE float, else ``default``.

    A non-positive lever is rejected rather than honoured: ``gap_hours_new_day:
    0`` would make every contact a new day and ``brief_read_decay_hours: 0``
    would make the brief never count as read. Both are almost certainly typos,
    and both fail toward nagging the operator forever.
    """
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def _int_or(value: Any, default: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return out if out > 0 else default


def read_router_preference(vault_path: Path | str | None) -> tuple[dict[str, Any], str]:
    """Read the router's ``matcher.args`` from the vault, or fall back.

    Returns ``(args, source)`` where ``source`` is :data:`LEVERS_FROM_RECORD` or
    :data:`LEVERS_FROM_DEFAULTS`.

    Deliberately does NOT call :func:`alfred.preferences.matchers.evaluate`:
    that is a skip/keep GATE over a candidate, and ``contact_surface_open`` is
    not one of its ``KNOWN_RULES``. This consumer wants the record's ``args`` as
    configuration, which the loader passes through verbatim.

    Every failure path lands on the defaults and SAYS SO — an unreadable vault,
    a missing record, a malformed matcher. The router must keep working when the
    record is being edited; falling back silently is what would make a broken
    record undiscoverable.
    """
    if not vault_path:
        log.info(
            "web.day_state.levers_default",
            reason="no vault path wired — cannot read the preference record",
        )
        return {}, LEVERS_FROM_DEFAULTS
    try:
        from alfred.preferences import load_active_preferences

        prefs = load_active_preferences(vault_path)
    except Exception as exc:  # noqa: BLE001 — a vault read must never 500 the router
        log.warning(
            "web.day_state.levers_read_failed",
            error=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return {}, LEVERS_FROM_DEFAULTS

    for pref in prefs:
        matcher = pref.matcher or {}
        if matcher.get("rule") != CONTACT_ROUTER_RULE:
            continue
        # ``matcher.domain`` is optional in v1 of the preference schema — a
        # missing domain matches every consumer (the house idiom, curator/
        # pipeline.py and brief/upcoming_events.py both spell it this way).
        if matcher.get("domain") not in (None, CONTACT_ROUTER_DOMAIN):
            continue
        args = matcher.get("args")
        if not isinstance(args, dict):
            log.warning(
                "web.day_state.levers_malformed",
                slug=pref.slug,
                detail="matcher.args is not a mapping — using spec defaults",
            )
            return {}, LEVERS_FROM_DEFAULTS
        log.info("web.day_state.levers_from_record", slug=pref.slug)
        return args, LEVERS_FROM_RECORD

    # ILB: "no record" is a real, recoverable state — the router runs on the
    # spec defaults and the operator can see that it is doing so.
    log.info(
        "web.day_state.levers_default",
        reason=(
            f"no active preference record with matcher.rule="
            f"{CONTACT_ROUTER_RULE} — using spec defaults"
        ),
        prefs_scanned=len(prefs),
    )
    return {}, LEVERS_FROM_DEFAULTS


def levers_from_args(args: dict[str, Any]) -> dict[str, float]:
    """The two tuning knobs, record-first with spec fallbacks."""
    raw = args.get("levers") if isinstance(args.get("levers"), dict) else {}
    return {
        "gap_hours_new_day": _float_or(
            raw.get("gap_hours_new_day"), DEFAULT_GAP_HOURS_NEW_DAY
        ),
        "brief_read_decay_hours": _float_or(
            raw.get("brief_read_decay_hours"), DEFAULT_BRIEF_READ_DECAY_HOURS
        ),
    }


def pattern_config_from_args(args: dict[str, Any]) -> dict[str, Any]:
    """Pattern-surfacing levers, record-first with spec fallbacks."""
    raw = (
        args.get("pattern_surfacing")
        if isinstance(args.get("pattern_surfacing"), dict)
        else {}
    )
    enabled = raw.get("enabled")
    same_context = raw.get("same_context_required")
    return {
        "enabled": (
            DEFAULT_PATTERN_ENABLED if enabled is None else bool(enabled)
        ),
        "min_observations": _int_or(
            raw.get("min_observations"), DEFAULT_PATTERN_MIN_OBSERVATIONS
        ),
        "window_days": _int_or(
            raw.get("window_days"), DEFAULT_PATTERN_WINDOW_DAYS
        ),
        "same_context_required": (
            DEFAULT_PATTERN_SAME_CONTEXT_REQUIRED
            if same_context is None
            else bool(same_context)
        ),
        "threshold_ratio": _float_or(
            raw.get("threshold_ratio"), DEFAULT_PATTERN_THRESHOLD_RATIO
        ),
    }


def _last_chat_activity(state_mgr: Any, chat_id: str) -> datetime | None:
    """Most recent talker activity for ``chat_id``, or ``None``.

    Reads BOTH halves of the session log because either can be the newest fact:
    an OPEN session's ``last_message_at`` (the operator is mid-conversation) and
    the newest matching ``closed_sessions[].ended_at``. Taking only one would
    read a live session as no contact at all, or a just-closed one as ancient.
    """
    if state_mgr is None:
        return None
    stamps: list[datetime] = []
    try:
        active = state_mgr.get_active(chat_id)
        if isinstance(active, dict):
            ts = parse_ts(active.get("last_message_at")) or parse_ts(
                active.get("started_at")
            )
            if ts is not None:
                stamps.append(ts)
        closed = (getattr(state_mgr, "state", {}) or {}).get("closed_sessions", [])
        if isinstance(closed, list):
            for entry in closed:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("chat_id", "")) != str(chat_id):
                    continue
                ts = parse_ts(entry.get("ended_at"))
                if ts is not None:
                    stamps.append(ts)
    except Exception as exc:  # noqa: BLE001 — session state is an input, not the payload
        log.warning(
            "web.day_state.session_read_failed",
            error=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return None
    return max(stamps) if stamps else None


def compute_day_state(
    *,
    user_key: str,
    contact_store: WebContactStore | None,
    notify_store: Any,
    state_mgr: Any,
    vault_path: Path | str | None,
    current_brief_date: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the day-state payload the PWA evaluates the rule set against.

    Never raises on a missing input: every store is optional and its absence
    reads as "nothing recorded", which the rules already handle (no
    notifications → rule 2 does not fire; no session or contact history → rule 3
    fires, which is the correct answer for a first-ever contact).
    """
    at = now or _now()
    args, levers_source = read_router_preference(vault_path)
    levers = levers_from_args(args)
    pattern_cfg = pattern_config_from_args(args)

    # --- rule 2 input: the unresolved (unread, undismissed) tray -------------
    unresolved = 0
    first_unresolved: str | None = None
    if notify_store is not None:
        try:
            unresolved = int(notify_store.unread_count(user_key))
            if unresolved:
                # ``list_for`` is NEWEST-first and already excludes dismissed
                # entries (#86). "First unresolved" is the one that has been
                # waiting LONGEST, so take the oldest unread — the last match
                # walking newest-first.
                for item in notify_store.list_for(user_key):
                    if not item.get("read"):
                        first_unresolved = str(item.get("id") or "") or None
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "web.day_state.notify_read_failed",
                error=type(exc).__name__,
                detail=str(exc)[:200],
            )
            unresolved, first_unresolved = 0, None

    # --- rule 3 + 4 inputs: last contact, brief read, last surface -----------
    chat_activity = _last_chat_activity(state_mgr, user_key)
    last_contact = contact_store.last_contact_ts(user_key) if contact_store else None
    # "Last session" is the last time the operator was HERE, by either door: a
    # talker turn or an app-open. Using only chat activity would make every
    # brief-only morning look like a fresh gap and re-fire rule 3 all day.
    candidates = [ts for ts in (chat_activity, last_contact) if ts is not None]
    last_session_ended = max(candidates) if candidates else None
    hours_since = (
        (at - last_session_ended).total_seconds() / 3600.0
        if last_session_ended is not None
        else None
    )

    # BRIEF_READ_TODAY IS TWO QUESTIONS, AND THE FIRST ONE USED TO BE MISSING.
    #
    # The original derivation asked only "how long ago did they land on the
    # brief", which a 28-second glance at YESTERDAY'S brief answers exactly like
    # a real read of today's — so a 04:48 look at the previous day's artifact
    # suppressed rule 3 for the one that landed at 06:00. Observed in the
    # operator's own contact log; the lever was never wrong, it was the only
    # question being asked.
    #
    # So: WHICH, then WHEN.
    #   WHICH — identity, not arithmetic. The artifact date recorded on the
    #     contact (read server-side when it happened) must equal the artifact
    #     date current now. Comparing a timestamp against a date string would
    #     need a local-midnight boundary this module has no business inventing,
    #     and the operator's case sits exactly where that guess would decide.
    #   WHEN — the decay lever, unchanged and still doing its own job: after
    #     `brief_read_decay_hours` a genuine morning read stops suppressing the
    #     evening offer.
    #
    # An unrecorded date (a contact predating this field, or an instance with no
    # spool) is NOT a match. The failure direction is being offered the brief
    # again, never having it silently withheld.
    brief_ts, brief_read_date = (
        contact_store.last_brief_read(user_key) if contact_store else (None, "")
    )
    decay_hours = levers["brief_read_decay_hours"]
    same_artifact = bool(
        brief_read_date and current_brief_date and brief_read_date == current_brief_date
    )
    brief_read_today = bool(
        brief_ts is not None
        and same_artifact
        and (at - brief_ts).total_seconds() / 3600.0 < decay_hours
    )

    last_surface = contact_store.last_landed_surface(user_key) if contact_store else ""
    adopted = contact_store.adopted_for(user_key) if contact_store else {}

    payload: dict[str, Any] = {
        # The spec's state model. ``open_capture_pending`` /
        # ``pending_capture_id`` are ABSENT, not false — see the module
        # docstring; ``unarmed_rules`` carries the reason.
        "last_session_ended": (
            last_session_ended.isoformat() if last_session_ended else None
        ),
        "time_since_last_session_hours": (
            round(hours_since, 4) if hours_since is not None else None
        ),
        "brief_read_today": brief_read_today,
        # Served so the client (and a human reading the payload) can see WHICH
        # artifact the answer is about. A bare boolean is what let the previous
        # derivation be wrong without looking wrong.
        "current_brief_date": current_brief_date,
        "brief_read_date": brief_read_date,
        "unresolved_flagged_notifications": unresolved,
        "first_unresolved_notification_id": first_unresolved,
        "last_active_surface": last_surface,
        # Which rungs are live, and why the rest are not.
        "rule_order": list(RULE_ORDER),
        "armed_rules": list(ARMED_RULES),
        "unarmed_rules": dict(UNARMED_RULE_REASONS),
        # Operator-approved per-rule surface overrides (pattern card ``adopt``).
        "adopted_defaults": adopted,
        "levers": levers,
        "levers_source": levers_source,
        "pattern_surfacing": pattern_cfg,
        # False when no state path is anchored: the PWA then does not route at
        # all (fail-safe — stay where you are), rather than routing on a day
        # state it cannot record against.
        "configured": contact_store is not None,
    }

    # ILB: every read logs, including the all-quiet one. This endpoint is the
    # router's only input, so "the router did nothing today" must be
    # distinguishable from "the endpoint stopped answering".
    log.info(
        "web.day_state.computed",
        user_key=user_key,
        configured=payload["configured"],
        unresolved=unresolved,
        brief_read_today=brief_read_today,
        current_brief_date=current_brief_date or "(none)",
        brief_read_date=brief_read_date or "(none)",
        hours_since_last_session=(
            round(hours_since, 2) if hours_since is not None else None
        ),
        last_active_surface=last_surface or "(none)",
        levers_source=levers_source,
        armed=len(ARMED_RULES),
    )
    return payload
