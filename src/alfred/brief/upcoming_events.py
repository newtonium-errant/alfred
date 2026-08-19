"""Upcoming Events section — forward-looking calendar slice for the Morning Brief.

Phase 1 (intentionally rule-free): scan all ``event`` and ``task`` records,
bucket by date relative to today (Halifax), drop anything more than 30 days
out. Filter rules grow inline as real-data patterns reveal what's noise.

Sources:
- ``event`` records via frontmatter ``start`` (preferred — full ISO datetime
  with timezone offset, written by Salem since SKILL update ``a923c1b``).
  Falls back to ``date`` for legacy records pre-Phase-A+.
- ``task`` records via frontmatter ``due`` (optional; tasks without ``due``
  are excluded entirely).

``remind_at`` is intentionally NOT a source here — it already drives the
outbound transport scheduler and would create duplicate user-visible noise.

Buckets depend on the caller-supplied ``scope`` kwarg on
``render_upcoming_events_section``:

``scope="brief"`` (default, used by the morning brief daemon):
- **Today** — ``date == today``
- **This Week** — ``today < date <= today + 7d``
- **Later** — ``today + 7d < date <= today + max_days_ahead``

``scope="today_tomorrow"`` (the glance-view slice — built 2026-05-30
for the ``/today`` Telegram slash command, which died with the
Telegram retirement in T5 2026-08-19; the mode is kept as a tested
narrow-view of this shared renderer with no production caller today):
- **Today** — ``date == today``
- **Tomorrow** — ``date == today + 1d``

In ``today_tomorrow`` mode the effective ``max_days_ahead`` is clamped
to ``1`` regardless of what the config carries — the operator's narrow-
view choice wins over any per-instance config widening.

Empty buckets are omitted. If all visible buckets are empty, the
renderer emits a literal "No upcoming events." marker so operators
know the section ran rather than crashing silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import frontmatter

from alfred.preferences.loader import Preference, load_active_preferences
from alfred.preferences.matchers import evaluate

from .config import UpcomingEventsConfig
from .utils import get_logger

log = get_logger(__name__)


# Canonical header text for the brief's Upcoming Events section.
# Hoisted to a module-level constant 2026-05-28 (Tier Phase 2A
# code-reviewer NOTE) so the ``/today`` slash command composer + any
# other downstream surface that mirrors the brief's section headers
# reads from one source of truth. A future refactor that renames the
# header (e.g. "Upcoming Calendar") updates this constant and every
# consumer follows.
SECTION_HEADER = "Upcoming Events"


# Operator-preference V1 (project_operator_preferences_v1).
# Per-type rule dispatch for the Upcoming Events section: each
# candidate type checks the corresponding ``skip_brief_*`` rule.
# Mirrors the curator pipeline's ``_CURATOR_RULE_BY_TYPE`` dispatch
# shape — extending V1 to a new type (e.g. ``project``) means
# adding the type→rule mapping here + registering the rule in
# ``alfred.preferences.matchers.KNOWN_RULES``.
_BRIEF_RULE_BY_TYPE: dict[str, str] = {
    "event": "skip_brief_event_if",
    "task": "skip_brief_task_if",
}


# Directories never worth scanning. Mirrors the conservative defaults the
# Operations section's vault counter uses.
_IGNORE_DIRS: frozenset[str] = frozenset(
    {"_templates", "_bases", "_docs", ".obsidian", "view", "session", "inbox"}
)


@dataclass(frozen=True)
class _UpcomingItem:
    """One row in the rendered section. Sortable by (date_iso, name).

    ``rec_type`` (event | task) + ``time_display`` added 2026-06-15 for
    the compact event-line render. Events render as a single compact
    line in EVERY list view (operator directive 2026-06-15: "scope is
    all events when listed, unless I ask for details") — the redundant
    trailing date is stripped from the name, the time is inlined when
    known, and location/description are dropped (they live in the
    record + the conversational "tell me about this event" path).
    Tasks keep their clean one-liner unchanged. ``location`` /
    ``description`` are still collected (cheap, and a future detail
    surface may want them) but no list renderer emits them now.
    """

    date_iso: str
    name: str
    location: str | None
    description: str | None
    rec_type: str = "event"
    time_display: str | None = None
    # Raw interval extent off the record's ``start`` / ``end`` frontmatter
    # (D7, 2026-08-11). Collected but NOT rendered — every list view still
    # emits ``time_display`` exactly as before, so the brief is byte-identical.
    # These exist for the feed projection, which needs the real interval rather
    # than a lossy ``"%H:%M"`` display string. ``end`` is written by gcal_sync
    # alongside ``start`` but is absent on hand-authored events, so ``ends_at``
    # is independently optional: an event can legitimately have a start and no
    # known end, and that must not be confused with a zero-length one.
    starts_at: str | None = None
    ends_at: str | None = None


# Closed-state denyset — records in any of these statuses are excluded
# from Upcoming Events even when their date/due is in the future. Applies
# to BOTH event and task records. ``cancelled`` and ``done`` are the
# obvious cases; ``superseded`` is included because it's the standard
# schema status for replaced records and would be equally noisy.
#
# Stored lowercase; the gate (`_is_closed_status`) normalises the input
# via ``.casefold()`` so capitalised variants ("Cancelled", "CANCELLED")
# also filter. Empty string and missing field are treated as OPEN — the
# brief defaults to surfacing records and the gate is an explicit denyset
# rather than a generic predicate.
_CLOSED_STATUSES: frozenset[str] = frozenset(
    {"cancelled", "done", "superseded"}
)


def _is_closed_status(value: Any) -> bool:
    """Return True iff ``value`` represents a closed/triaged status.

    Handles three shapes defensively:
    - ``None`` / missing field → False (open by default)
    - Empty string → False (open by default)
    - Non-string scalar → False (don't crash on malformed frontmatter)
    - String → casefolded compare against ``_CLOSED_STATUSES``
    """
    if not isinstance(value, str):
        return False
    normalised = value.strip().casefold()
    if not normalised:
        return False
    return normalised in _CLOSED_STATUSES


def _coerce_date(value: Any) -> date | None:
    """Best-effort coerce a frontmatter ``date``/``due`` value to a ``date``.

    ``python-frontmatter`` returns either a ``datetime.date`` (when YAML
    parsed it as a date scalar) or a string (when the value was quoted).
    Anything else (None, list, malformed) -> None and the record is skipped.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _event_date(fm: dict) -> date | None:
    """Resolve an event record's display date — prefer ``start``, fall back
    to ``date``.

    Per Salem SKILL update ``a923c1b`` (and the cross-instance event-propose
    handler), every new event ships with both ``start`` (full ISO datetime
    with tz offset) and ``date`` (the same local-tz date, derived via
    ``start_dt.astimezone().date()``). The two agree by construction. This
    helper prefers ``start`` so:

      - Future-only-``start`` records (e.g. backfill paths that didn't
        write a redundant ``date``) still surface in the brief.
      - Legacy-only-``date`` records (pre-Phase-A+) keep working.

    For string ``start`` values the ``[:10]`` slice extracts the date
    portion AT THE ENCODED OFFSET — which is the Halifax-local date when
    Salem wrote the value (the GCal sync code uses the local tz offset
    directly). For ``datetime`` values we use ``.date()`` which yields the
    date in the encoded zone for the same reason. Naive datetimes fall
    through to ``.date()`` defensively rather than guessing at a tz.
    """
    return _coerce_date(fm.get("start")) or _coerce_date(fm.get("date"))


# Matches a trailing ISO date baked into an event ``name`` — e.g.
# "Chiropractic Appointment 2026-06-16" or "… Berwick Chiropractic
# 2026-06-16". Event names historically appended the date (the
# event-creation/GCal-sync naming convention); the date already lives
# in ``date``/``start``, so the compact list render strips it to avoid
# showing the date twice. Anchored to end-of-string with a leading
# space so it only fires on a genuine trailing date, never a date
# mid-name.
_TRAILING_DATE_RE = re.compile(r"\s+\d{4}-\d{2}-\d{2}$")


def _strip_trailing_date(name: str) -> str:
    """Strip a trailing ` YYYY-MM-DD` from an event display name.

    Idempotent + safe on names without a trailing date (returns the
    name unchanged). Only the LAST trailing date is removed (the regex
    is end-anchored), so a name that legitimately contains a date
    mid-string keeps it.
    """
    return _TRAILING_DATE_RE.sub("", name).rstrip()


def _event_time_display(fm: dict) -> str | None:
    """Resolve an event's display time for the compact list line.

    Prefers the explicit ``time`` frontmatter field VERBATIM (operator-
    confirmed 2026-06-15) — live records carry it display-ready, e.g.
    ``time: 11:40 AM``. Falls back to deriving ``HH:MM`` from the
    ``start`` ISO datetime when ``time`` is absent. Returns ``None``
    when neither yields a usable value (all-day events → no time shown).
    """
    raw_time = fm.get("time")
    if isinstance(raw_time, str) and raw_time.strip():
        return raw_time.strip()
    # Fallback: derive HH:MM from a full ISO ``start`` datetime.
    start = fm.get("start")
    start_dt: datetime | None = None
    if isinstance(start, datetime):
        start_dt = start
    elif isinstance(start, str) and start.strip():
        try:
            start_dt = datetime.fromisoformat(start.strip())
        except ValueError:
            start_dt = None
    if start_dt is not None:
        return start_dt.strftime("%H:%M")
    return None


def _iso_extent(value: Any) -> str | None:
    """One end of an interval extent, as an ISO-8601 string (D7, 2026-08-11).

    Accepts what the frontmatter loader actually hands us for ``start`` /
    ``end``: a ``datetime`` (PyYAML resolves an unquoted ISO timestamp to one),
    a ``date``, or a string. Anything unparseable — a bare time, prose, a
    number, ``None`` — returns ``None`` rather than guessing, because a WRONG
    interval is worse than an absent one: an absent extent renders as "no known
    end", a wrong one silently moves an appointment.

    Strings are validated by round-tripping through ``fromisoformat`` and are
    re-emitted from the PARSED value, so the stored extent is canonical ISO
    regardless of the spacing/format the record used. Offsets are preserved
    (never coerced to UTC, never stripped) — dropping a tz would shift the
    operator's clock, which is exactly the lossiness this field exists to end.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        # An all-day event: a real date with no clock time. ``date.isoformat()``
        # yields ``YYYY-MM-DD``, which is a valid ISO-8601 extent.
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        # DATE-ONLY IS TRIED FIRST, and the order is load-bearing. On 3.11+
        # ``datetime.fromisoformat("2026-08-11")`` SUCCEEDS and silently invents
        # midnight, so a datetime-first helper turns an all-day event into a
        # zero-length one at 00:00 — a clock time the record never claimed, and
        # exactly the kind of confident wrong answer this function refuses to
        # give. ``date.fromisoformat`` is the precise discriminator: it raises on
        # anything carrying a time component (verified on 3.12), so a date-only
        # string can only reach it and a timestamp can only fall through.
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(raw).isoformat()
        except ValueError:
            return None
    return None


def _iter_records(vault_path: Path) -> list[tuple[Path, dict]]:
    """Walk the vault and return (path, frontmatter_dict) for every .md file
    that isn't in an ignored directory. Inline frontmatter read because
    Phase 1 needs full metadata access and ``vault_list`` only returns
    name/path/type/status."""
    out: list[tuple[Path, dict]] = []
    if not vault_path.exists():
        return out
    for md_file in vault_path.rglob("*.md"):
        try:
            rel = md_file.relative_to(vault_path)
        except ValueError:
            continue
        if any(part in _IGNORE_DIRS for part in rel.parts):
            continue
        try:
            post = frontmatter.load(str(md_file))
        except Exception:
            continue
        out.append((md_file, dict(post.metadata)))
    return out


def _matches_skip_brief_preference(
    fm: dict,
    rec_type: str,
    prefs: list[Preference],
) -> tuple[bool, str, str]:
    """Check the candidate against active brief-domain Shape A preferences.

    Returns ``(skip, preference_slug, reason)``. ``skip=False`` means no
    preference fired — the slug + reason are empty strings. ``skip=True``
    means at least one preference matched; the slug names which one and
    the reason carries the matcher's grep-able motivation.

    Per project_operator_preferences_v1.md Hard Contract #1+#2 — V1
    consumers in brief: events + tasks via ``skip_brief_event_if`` and
    ``skip_brief_task_if``. The shared dispatch table
    (``_BRIEF_RULE_BY_TYPE``) keeps both call sites symmetric;
    extending to a new type means adding one mapping + one
    KNOWN_RULES entry.
    """
    rule = _BRIEF_RULE_BY_TYPE.get(rec_type)
    if rule is None:
        return False, "", ""
    candidate = {
        "name": fm.get("name", ""),
        "title": fm.get("title", "") or fm.get("name", ""),
    }
    for pref in prefs:
        matcher = pref.matcher or {}
        if matcher.get("rule") != rule:
            continue
        if matcher.get("domain") not in (None, "brief"):
            continue
        result = evaluate(rule, matcher.get("args", {}), candidate)
        if result.skip:
            return True, pref.slug, result.reason
    return False, "", ""


def _collect_items(
    vault_path: Path,
    today: date,
    max_days_ahead: int,
    prefs: list[Preference] | None = None,
) -> tuple[list[_UpcomingItem], int]:
    """Pull events + tasks whose date/due falls in [today, today+max_days_ahead].

    Returns ``(items, preference_filtered_count)``. The count is the
    number of candidates dropped by the operator-preference gate (NOT
    by date / status / missing-due filters — those are date/schema
    semantics, not operator policy). The renderer appends a
    ``"_N items filtered by operator preferences._"`` footer when
    ``preference_filtered_count > 0`` so the operator sees the gate
    fired without having to grep the daemon log.
    """
    cutoff = today.toordinal() + max_days_ahead
    items: list[_UpcomingItem] = []
    prefs = prefs or []
    preference_filtered = 0
    for path, fm in _iter_records(vault_path):
        rec_type = fm.get("type")
        if rec_type == "event":
            d = _event_date(fm)
        elif rec_type == "task":
            d = _coerce_date(fm.get("due"))
        else:
            continue
        # Closed-state filter — applies to BOTH event and task records.
        # Pre-fix the gate only fired on the task branch, leaving
        # cancelled/done events visible in the brief; 2026-05-21
        # operator marked an open-house event ``status: cancelled`` and
        # 2026-05-22's brief still surfaced it. Per the dispatch
        # (project_brief_cancelled_event_filter): events grew a status
        # field once GCal sync went live (the cancel hook PATCHes GCal
        # when ``status: cancelled``), so the schema reality now
        # matches tasks. Use the shared ``_is_closed_status`` helper
        # so the empty-string / missing-field / case-variant behaviour
        # is identical across both code paths.
        if _is_closed_status(fm.get("status")):
            log.info(
                "upcoming_events.closed_status_excluded",
                path=str(path),
                rec_type=rec_type,
                status=fm.get("status"),
            )
            continue
        # Operator-preference action filter (V1, project_operator_
        # preferences_v1). Runs AFTER closed-status (avoid double-
        # logging records that would have been excluded anyway).
        # Per-drop log carries the preference slug + reason for
        # operator grep; per ``feedback_intentionally_left_blank.md``
        # the per-sweep summary log fires later with the count.
        skip, pref_slug, pref_reason = _matches_skip_brief_preference(
            fm, rec_type, prefs,
        )
        if skip:
            log.info(
                "upcoming_events.preference_filtered",
                path=str(path),
                rec_type=rec_type,
                preference_slug=pref_slug,
                reason=pref_reason,
            )
            preference_filtered += 1
            continue
        if d is None:
            # Per ``feedback_intentionally_left_blank.md``: events
            # missing both ``start`` and ``date`` are a real signal,
            # not noise — the record is malformed and silently
            # disappearing from the brief is exactly the failure
            # mode the principle exists to prevent. Log so an operator
            # can grep ``upcoming_events.event_missing_date`` to spot
            # the gap.
            if rec_type == "event":
                log.info(
                    "upcoming_events.event_missing_date",
                    path=str(path),
                    detail="event record has neither 'start' nor 'date'",
                )
            continue
        if d.toordinal() < today.toordinal():
            continue
        if d.toordinal() > cutoff:
            continue
        name = (
            fm.get("name")
            or fm.get("subject")
            or path.stem
        )
        location = fm.get("location")
        description = fm.get("description")
        # Time only applies to events; tasks have no clock time in the
        # list view (they render off ``due`` date only).
        time_display = _event_time_display(fm) if rec_type == "event" else None
        # Interval extent (D7) — events only. A task's ``due`` is a deadline, not
        # a span the task occupies, so stamping it as a start would assert an
        # interval the record does not claim.
        starts_at = _iso_extent(fm.get("start")) if rec_type == "event" else None
        ends_at = _iso_extent(fm.get("end")) if rec_type == "event" else None
        if rec_type == "event":
            # ILB: a PRESENT-but-unparseable start/end loses the extent, and
            # silence here is indistinguishable from "this event has no time".
            # Only fires when the field exists and we refused it — an event with
            # no ``start`` at all is the ordinary all-day case, not a defect.
            for field_name, parsed in (("start", starts_at), ("end", ends_at)):
                if fm.get(field_name) is not None and parsed is None:
                    log.info(
                        "upcoming_events.extent_unparseable",
                        path=str(path),
                        field=field_name,
                        raw=str(fm.get(field_name))[:80],
                        detail=(
                            "event still rendered and still fed; only the "
                            "interval extent was dropped"
                        ),
                    )
        items.append(
            _UpcomingItem(
                date_iso=d.isoformat(),
                name=str(name),
                location=str(location) if location else None,
                description=str(description) if description else None,
                rec_type=str(rec_type),
                time_display=time_display,
                starts_at=starts_at,
                ends_at=ends_at,
            )
        )
    return items, preference_filtered


def _bucket(
    items: list[_UpcomingItem],
    today: date,
    *,
    scope: Literal["brief", "today_tomorrow"] = "brief",
) -> dict[str, list[_UpcomingItem]]:
    """Split items into per-scope buckets.

    ``scope="brief"`` (default) — three buckets:
      * **Today** — ``date == today``
      * **This Week** — ``today < date <= today + 7d``
      * **Later** — ``today + 7d < date``

    ``scope="today_tomorrow"`` — two buckets:
      * **Today** — ``date == today``
      * **Tomorrow** — ``date == today + 1d``

    In ``today_tomorrow`` mode items dated beyond ``today + 1d`` will not
    appear in any bucket — but caller is expected to have clamped
    ``max_days_ahead`` to ``1`` before ``_collect_items`` ran, so such
    items shouldn't reach this function in the first place. The
    defensive drop here is a belt-and-braces guard so a future caller
    that bypasses the clamp doesn't silently leak items into a
    non-existent "Later" bucket.
    """
    today_ord = today.toordinal()
    if scope == "today_tomorrow":
        buckets: dict[str, list[_UpcomingItem]] = {
            "Today": [],
            "Tomorrow": [],
        }
        tomorrow_ord = today_ord + 1
        for item in items:
            item_ord = date.fromisoformat(item.date_iso).toordinal()
            if item_ord == today_ord:
                buckets["Today"].append(item)
            elif item_ord == tomorrow_ord:
                buckets["Tomorrow"].append(item)
            # else: defensively dropped (see docstring).
        for key in buckets:
            buckets[key].sort(key=lambda x: (x.date_iso, x.name))
        return buckets

    buckets = {
        "Today": [],
        "This Week": [],
        "Later": [],
    }
    week_ord = today_ord + 7
    for item in items:
        item_ord = date.fromisoformat(item.date_iso).toordinal()
        if item_ord == today_ord:
            buckets["Today"].append(item)
        elif item_ord <= week_ord:
            buckets["This Week"].append(item)
        else:
            buckets["Later"].append(item)
    for key in buckets:
        buckets[key].sort(key=lambda x: (x.date_iso, x.name))
    return buckets


def _render_item(item: _UpcomingItem) -> str:
    """Render one item as a single compact markdown line.

    EVENTS (every list view, operator directive 2026-06-15 "scope is
    all events when listed, unless I ask for details"):
        ``- {date} {time} — {name-sans-trailing-date}``
    The redundant trailing date baked into the event name is stripped,
    the time is inlined when known (omitted gracefully for all-day
    events → ``- {date} — {name}``), and location + description are
    DROPPED. The full detail lives in the event record + the
    conversational "tell me about this event" path (talker reads the
    record) — it is intentionally no longer in ANY list view.

    TASKS: clean one-liner unchanged — ``- {date} — {name}`` (tasks
    never carried the location/description clutter and have no
    trailing-date-in-name problem).
    """
    if item.rec_type == "event":
        display_name = _strip_trailing_date(item.name)
        if item.time_display:
            return f"- {item.date_iso} {item.time_display} — {display_name}"
        return f"- {item.date_iso} — {display_name}"
    # Task (or any non-event) — clean one-liner, no location/description.
    return f"- {item.date_iso} — {item.name}"


def render_upcoming_events_section(
    config: UpcomingEventsConfig,
    vault_path: str | Path,
    today: date,
    *,
    scope: Literal["brief", "today_tomorrow"] = "brief",
) -> str:
    """Render the Upcoming Events section body markdown.

    Returns an empty string if the section is disabled in config — the
    daemon uses that as a signal to omit the section entirely. A
    populated string (including the "No upcoming events." sentinel) means
    the section header should be emitted.

    ``scope`` (default ``"brief"``) selects the bucket shape:

      * ``"brief"`` — three buckets (Today / This Week / Later) over
        ``config.max_days_ahead`` days. Used by the morning brief
        daemon — unchanged behavior since Phase 1.
      * ``"today_tomorrow"`` — two buckets (Today / Tomorrow) over a
        clamped window of 1 day ahead, regardless of
        ``config.max_days_ahead``. Used by the ``/today`` Telegram
        slash command since 2026-05-30 — operator wanted only the
        immediate calendar slice in the glance-view. The clamp is
        applied at render time so the caller doesn't need to know
        the brief's default window.

    Operator-preference V1 (project_operator_preferences_v1): loads
    Shape A action preferences and applies them via the brief-domain
    rules (``skip_brief_event_if`` / ``skip_brief_task_if``). When any
    preference fires, appends a footer line *"_N items filtered by
    operator preferences._"* so the operator sees the gate effect
    without grepping the daemon log. Empty / disabled section paths
    don't fire the footer. Preference filtering applies symmetrically
    in both scopes — the preference layer is scope-agnostic.
    """
    if not config.enabled:
        return ""

    # Scope-specific window clamp. The brief scope keeps the operator's
    # configured ``max_days_ahead`` (default 30, common operator value
    # 30 for the morning brief); the today_tomorrow scope hard-clamps
    # to 1 so the operator's narrow-view choice wins over any
    # per-instance config widening. Clamping at collect time (rather
    # than only at bucket time) means the inner loop doesn't pull in
    # records the renderer would only throw away — keeps the
    # _UpcomingItem build cheap.
    if scope == "today_tomorrow":
        effective_max_days = 1
        bucket_order: tuple[str, ...] = ("Today", "Tomorrow")
    else:
        effective_max_days = config.max_days_ahead
        bucket_order = ("Today", "This Week", "Later")

    vault = Path(vault_path)
    # Load preferences once per render call. Failure to load is
    # non-fatal: we log + continue with an empty preference list so
    # the brief keeps shipping (degraded but visible) rather than
    # blanking out.
    try:
        prefs = load_active_preferences(vault, shape="action")
    except Exception as exc:
        log.warning(
            "upcoming_events.preference_load_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            detail="continuing without preference filter",
        )
        prefs = []
    items, preference_filtered = _collect_items(
        vault, today, effective_max_days, prefs=prefs,
    )
    buckets = _bucket(items, today, scope=scope)

    section_parts: list[str] = []
    for bucket_name in bucket_order:
        bucket_items = buckets[bucket_name]
        if not bucket_items:
            continue
        section_parts.append(f"### {bucket_name}")
        for item in bucket_items:
            section_parts.append(_render_item(item))
        section_parts.append("")

    if not section_parts:
        # Empty section sentinel — operator sees "ran, nothing to do"
        # rather than a silent omission. Per
        # ``feedback_intentionally_left_blank.md``. Footer-line for
        # preference-filtered count is added BELOW the sentinel when
        # preferences actually dropped something, so the empty render
        # explains itself ("nothing scheduled, AND N items were
        # filtered out by preferences").
        body = "No upcoming events."
        if preference_filtered > 0:
            body += f"\n\n_{preference_filtered} item"
            body += "s" if preference_filtered != 1 else ""
            body += " filtered by operator preferences._"
        return body

    # Drop trailing blank line for cleanliness.
    while section_parts and section_parts[-1] == "":
        section_parts.pop()
    if preference_filtered > 0:
        section_parts.append("")
        section_parts.append(
            f"_{preference_filtered} item"
            + ("s" if preference_filtered != 1 else "")
            + " filtered by operator preferences._"
        )
    return "\n".join(section_parts)
