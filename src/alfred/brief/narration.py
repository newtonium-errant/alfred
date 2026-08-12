"""brief_narration — the brief-as-MODEL third renderer (Phase C3a).

The interruptible briefing player speaks a SECTIONED script — one segment per
slide, not one blob. This module produces that script from the SAME structured
sources the brief's markdown + feed renderers consume (``compute_today_view``,
the BIT health record, ``upcoming_events``, and — critically — the weather
module's ``StationWeather`` dataclass), NEVER from the rendered markdown.

**TAF-safety by construction.** The weather segment reads ``StationWeather``
structured fields (condition / wind / visibility), so a raw METAR/TAF code can
NEVER reach a spoken segment — the property holds because narration never touches
the weather markdown, not because we scrub it. The ``no-TAF-in-narration`` gate
pins this against a future regression where someone "helpfully" adds a markdown
fallback.

**Say-less.** Each segment has a per-segment word budget (config, tuned tight);
the whole briefing targets ~60-90s (~150-230 words). Segments compose to a
sectioned script the player syncs to slides; the ``section_id`` is the stable
key the C3c context-primer references so a paused question resolves against the
on-screen slide.

The split: :func:`compose_narration` is a PURE function over already-gathered
structured inputs (the gate surface — no I/O), and :func:`build_narration` is the
async gatherer that reads the sources and calls it. Tests drive the pure composer
directly; the gatherer is thin wiring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .utils import get_logger

if TYPE_CHECKING:  # avoid heavy imports at brief import time
    from alfred.brief.weather import StationWeather
    from alfred.tier.compute import DailyGoalState
    from alfred.tier.day_plan import DayPlan

log = get_logger(__name__)


# --- section ids (stable — the C3c primer + the FE deep-links key on these) ---
SECTION_DAY_STATE = "day_state"
SECTION_HEALTH = "health"
SECTION_DAY_PLAN = "day_plan"
SECTION_EVENTS = "events"
SECTION_WEATHER = "weather"
SECTION_WAITING = "waiting"
SECTION_SIGN_OFF = "sign_off"

# Segment order — the player renders slides in this order. ``waiting`` sits
# LAST before the sign-off on purpose: it is the segment that hands the
# operator off to the deck, so it is the last thing said before "go".
SEGMENT_ORDER = (
    SECTION_DAY_STATE,
    SECTION_HEALTH,
    SECTION_DAY_PLAN,
    SECTION_EVENTS,
    SECTION_WEATHER,
    SECTION_WAITING,
    SECTION_SIGN_OFF,
)


@dataclass(frozen=True)
class NarrationConfig:
    """Per-segment word budgets (say-less knob). Defaults are tuned tight so the
    whole briefing lands ~60-90s spoken; an operator YAML can override per
    segment later without touching code. ``enabled`` gates the whole feature."""

    enabled: bool = True
    budget_day_state: int = 45
    budget_health: int = 30
    budget_day_plan: int = 55
    budget_events: int = 40
    budget_weather: int = 35
    # Tight ON PURPOSE: the waiting segment's whole job is to say HOW MANY and
    # send the operator to the deck. A budget large enough to list the items
    # would invite exactly the read-them-aloud behaviour it exists to prevent.
    budget_waiting: int = 25
    budget_sign_off: int = 20

    def budget_for(self, section_id: str) -> int:
        return {
            SECTION_DAY_STATE: self.budget_day_state,
            SECTION_HEALTH: self.budget_health,
            SECTION_DAY_PLAN: self.budget_day_plan,
            SECTION_EVENTS: self.budget_events,
            SECTION_WEATHER: self.budget_weather,
            SECTION_WAITING: self.budget_waiting,
            SECTION_SIGN_OFF: self.budget_sign_off,
        }.get(section_id, 40)


@dataclass
class NarrationSegment:
    """One speakable slide segment. ``section_id`` is the stable key; ``title``
    is the slide heading; ``text`` is the spoken prose (empty ⟹ omitted slide)."""

    section_id: str
    title: str
    text: str

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def to_dict(self) -> dict[str, Any]:
        return {
            "section_id": self.section_id,
            "title": self.title,
            "text": self.text,
            "word_count": self.word_count,
        }


@dataclass
class BriefNarration:
    """The full sectioned script for one brief date. ``segments`` are the
    non-empty slides in :data:`SEGMENT_ORDER`. ``empty`` is True when nothing
    was speakable (ILB: the player says so rather than playing silence)."""

    brief_date: str
    segments: list[NarrationSegment] = field(default_factory=list)

    @property
    def full_text(self) -> str:
        """The whole briefing as one string (for a single-shot synth)."""
        return "\n\n".join(s.text for s in self.segments if s.text)

    @property
    def total_words(self) -> int:
        return sum(s.word_count for s in self.segments)

    @property
    def empty(self) -> bool:
        return not any(s.text for s in self.segments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "brief_date": self.brief_date,
            "segments": [s.to_dict() for s in self.segments],
            "total_words": self.total_words,
            "empty": self.empty,
        }


def _clip_words(text: str, budget: int) -> str:
    """Safety net: trim ``text`` to ``budget`` words at a word boundary. The
    generators aim under budget by construction; this guards a runaway."""
    words = text.split()
    if len(words) <= budget:
        return text
    return " ".join(words[:budget]).rstrip(",;:") + "…"


# --- per-segment speakable generators (pure) ---------------------------------


def _day_state_text(g: "DailyGoalState") -> str:
    """The rings/day-state segment from the C2 ``daily_goal`` stage data.

    ``daily_goal`` is TIER-based and stays that way — ``balanced_day`` counts
    one-done in each of T1/T2/T3, and flipping that metric to the slot axis is
    a separate gated lane. The copy below therefore speaks tiers ("urgent",
    "medium", "self-care") even though the brief now ARRANGES the same rows by
    slot. Per ``tier/day_plan.py``: no copy anywhere may claim a slot-based
    goal while the metric is tier-based.
    """
    total = g.t1_available + g.t2_available + g.t3_available
    if total == 0:
        return "Your day is a clean slate — nothing tiered up yet."
    done = g.t1_done + g.t2_done + g.t3_done
    lead = f"You've got {total} thing{'s' if total != 1 else ''} on today's plan"
    if done:
        lead += f", {done} already done"
    lead += "."
    if g.balanced_day:
        return lead + " You've hit one in every tier — a balanced day already."
    lanes = []
    if g.t1_available:
        lanes.append(f"{g.t1_done} of {g.t1_available} urgent")
    if g.t2_available:
        lanes.append(f"{g.t2_done} of {g.t2_available} medium")
    if g.t3_available:
        lanes.append(f"{g.t3_done} of {g.t3_available} self-care")
    if lanes:
        return lead + " So far: " + ", ".join(lanes) + "."
    return lead


def _health_text(tool_lines: list[tuple[str, str, str]]) -> str:
    """The health-glance segment from ``_per_tool_lines`` (tool, status, detail).
    Speaks only the tools needing attention (say-less); nothing needing attention
    is one reassuring line.

    Attention is :func:`~.health_section.is_attention_status` — the SAME
    predicate the feed's health cards use, not a local ``!= "ok"``. A skipped
    check did not apply, so speaking it would tell the operator that an
    unconfigured tool "needs a look" every single morning: KAL-LE's measured BIT
    is ``{ok: 5, warn: 0, fail: 0, skip: 7}``, which the old comparison narrated
    as "7 tools need a look" daily, with nothing wrong.
    """
    from .health_section import is_attention_status

    attention = [(t, s, d) for (t, s, d) in tool_lines if is_attention_status(s)]
    if not tool_lines:
        return ""  # no health data → omit the slide (ILB handled by caller)
    if not attention:
        # Mirrors the ratified skip-posture ruling in
        # ``kalle_digest._render_skip_posture``: skips ALONGSIDE passing probes
        # are green, unqualified — but green on ZERO passing probes would claim
        # checks passed on the strength of no evidence. Suppressing skips is what
        # makes the all-skipped case reachable here (it used to fall into the
        # "needs a look" branch), so it has to be answered honestly.
        if not any(s.strip().lower() == "ok" for (_t, s, _d) in tool_lines):
            return "No health checks ran this morning."
        return "All systems green."
    names = ", ".join(t for (t, _s, _d) in attention)
    n = len(attention)
    return f"Heads up — {n} tool{'s' if n != 1 else ''} need{'s' if n == 1 else ''} a look: {names}."


def _day_plan_text(plan: "DayPlan") -> str:
    """The day-plan segment — names only (the visual slide carries the full
    list; narration names the top few).

    **Re-pointed (Phase C, C2+C3) at the shared ``DayPlan`` projection** — the
    SAME object the brief's day section renders, so the spoken plan and the
    read plan can no longer disagree about what is on today's plan. Before
    this, each render read ``TodayView`` independently.

    **STALE COPY, deliberately left in place.** The strings below still speak
    the TIER axis ("Urgent", "Then", "self-care intentions") while the brief
    now ARRANGES the same rows by slot (Duty / Rhythm / Fuel). That is not an
    oversight and it is not a bug to fix here: this lane re-points the
    structure and freezes the voice, and the prompt-tuner's voice pass
    re-voices these strings against the slot vocabulary afterwards. Changing
    them here would collide with that pass — and, worse, would be the first
    copy in the system to name a slot while ``daily_goal`` still measures
    tiers. ``rows_in_tier`` exists precisely so this stayed byte-identical
    across the re-point: it preserves the view's own lane order.
    """
    def _names(rows, limit):
        return [r.name for r in rows[:limit] if r.name]

    t3_rows = plan.rows_in_tier(3)
    t1 = _names(plan.rows_in_tier(1), 3)
    t2 = _names(plan.rows_in_tier(2), 2)
    if not t1 and not t2 and not t3_rows:
        return "No urgent or medium items — today's yours to shape."
    parts: list[str] = []
    if t1:
        parts.append("Urgent: " + ", ".join(t1) + ".")
    if t2:
        parts.append("Then: " + ", ".join(t2) + ".")
    if t3_rows:
        parts.append(f"Plus {len(t3_rows)} self-care intention{'s' if len(t3_rows) != 1 else ''}.")
    return " ".join(parts)


def _events_text(events: list[Any]) -> str:
    """The events segment from the ``upcoming_events`` structured items. Each
    item exposes ``name`` + ``date_iso`` (+ optional ``time_display``)."""
    if not events:
        return ""  # no events → omit the slide
    first = events[:2]
    said: list[str] = []
    for ev in first:
        name = str(getattr(ev, "name", "") or "").strip()
        if not name:
            continue
        when = str(getattr(ev, "time_display", "") or "").strip()
        said.append(f"{name}{f' at {when}' if when else ''}")
    if not said:
        return ""
    extra = len(events) - len(said)
    line = "Coming up: " + "; ".join(said) + "."
    if extra > 0:
        line += f" And {extra} more."
    return line


def _weather_attention_worthy(stations: list["StationWeather"]) -> bool:
    """The demote ruling: the weather slide is prominent ONLY when severe —
    any station IFR/LIFR, a strong gust, or low visibility. (VERA-drivers-out is
    a future signal; no driver-schedule source in the brief config today.)"""
    for w in stations:
        if (w.flight_category or "").upper() in ("IFR", "LIFR"):
            return True
        if w.wind_gust_kt is not None and w.wind_gust_kt >= 30:
            return True
        if w.visibility_sm is not None and w.visibility_sm < 3.0:
            return True
    return False


def _weather_text(stations: list["StationWeather"]) -> str:
    """The weather segment — SPEAKABLE prose from ``StationWeather`` structured
    fields ONLY. Never the raw_text / markdown (that's where TAF codes live) —
    the no-TAF gate holds by construction. Empty unless attention-worthy
    (demote ruling)."""
    if not stations or not _weather_attention_worthy(stations):
        return ""
    w = next(
        (s for s in stations if (s.flight_category or "").upper() in ("IFR", "LIFR")),
        stations[0],
    )
    bits: list[str] = []
    if w.temp_c is not None:
        bits.append(f"{round(w.temp_c)} degrees")
    cat = (w.flight_category or "").upper()
    if cat in ("IFR", "LIFR"):
        bits.append("low ceilings and reduced visibility")
    if w.wind_gust_kt is not None and w.wind_gust_kt >= 30:
        bits.append(f"gusting to {w.wind_gust_kt} knots")
    elif w.visibility_sm is not None and w.visibility_sm < 3.0:
        bits.append("poor visibility")
    where = (w.name or w.station_id or "").strip()
    head = f"Weather worth noting at {where}: " if where else "Weather worth noting: "
    return head + ", ".join(bits) + "." if bits else ""


def count_waiting_items(feed_store_path: str) -> int | None:
    """How many feed items are actually waiting on a decision.

    Waiting = ``mode == decide`` AND ``state == open``. Deliberately NOT
    ``attention == needs_you``: attention is a learned re-tiering signal that
    answers "how loudly", while mode answers "does this need an answer from
    him at all" — and the deck is the answering surface, so mode is the
    question this count is asking. A DEFERRED item is not waiting either; it
    was answered, with "later".

    ``None`` on a read failure, which omits the slide — speaking a number we
    could not read is worse than not speaking, and it is the one case where a
    confident "nothing waiting" would be actively misleading.

    **The unreadable case has to be detected HERE.** ``FeedStore.load`` is
    deliberately crash-proof for lock-free concurrent reads: a missing store, an
    ``OSError``, and a corrupt tail all fold to ``{}``. That is right for the
    store and wrong for this caller, because it makes "no feed" and "cannot read
    the feed" arrive as the same zero — so a "nothing waiting on you" would be
    spoken with full confidence over an unreadable store. Hence the explicit
    probe below rather than relying on an exception that never comes.

    A store that does not exist YET is a genuine zero, not a failure: an
    instance with no feed has nothing waiting, and that is worth saying.
    """
    try:
        from alfred.feed import MODE_DECIDE, STATE_OPEN, FeedStore

        path = Path(feed_store_path)
        if path.exists() and not path.is_file():
            log.warning(
                "brief.narration.waiting_count_failed",
                path=str(path),
                error_type="NotAFile",
                error="feed store path exists but is not a readable file",
                detail="deck-waiting slide omitted; a spoken count we could not "
                       "read would be worse than silence here.",
            )
            return None
        items = FeedStore(feed_store_path).load()
        return sum(
            1 for it in items.values()
            if it.mode == MODE_DECIDE and it.state == STATE_OPEN
        )
    except Exception as exc:  # noqa: BLE001 — degrade to an omitted slide
        log.warning(
            "brief.narration.waiting_count_failed",
            error=str(exc),
            error_type=exc.__class__.__name__,
            detail="deck-waiting slide omitted; a spoken count we could not "
                   "read would be worse than silence here.",
        )
        return None


def _waiting_text(waiting_count: int) -> str:
    """The interactive-items segment: say HOW MANY, then route to the deck.

    The C3 ruling in its own terms — "interactive items say 'N waiting' and
    route to the deck". A briefing is a one-way audio surface: an item that
    needs a yes/no cannot be answered by listening to it, so reading the items
    aloud spends the operator's attention on decisions they are structurally
    unable to make while driving. The count is the actionable part; the deck is
    where the answering happens.

    Zero is SPOKEN, not omitted (intentionally-left-blank). "Nothing waiting"
    is a real and welcome answer, and a segment that vanishes when the count is
    zero is indistinguishable from a segment whose count failed to load — which
    is the one case where the operator most needs to know not to trust it.
    ``_gather_waiting_count`` returns ``None`` for that failure, and the caller
    omits the slide only then.
    """
    if waiting_count <= 0:
        return "Nothing waiting on you in the deck."
    n = waiting_count
    return (
        f"{n} thing{'s' if n != 1 else ''} waiting on you in the deck. "
        "Open it when you're at a screen."
    )


def _sign_off_text(g: "DailyGoalState") -> str:
    """A short encouraging close keyed on the daily goal (self-correcting-friendly
    tone; the composer log captures which segments draw questions later).

    Tier-based like :func:`_day_state_text`, and stale in the same deliberate
    way — "one urgent thing first" names a tier, not a slot.
    """
    if g.balanced_day:
        return "You're set up well. Go get it."
    if g.t1_available and g.t1_done < g.t1_available:
        return "One urgent thing first, then the rest follows. You've got this."
    return "That's the shape of your day. Take it one slot at a time."


# --- pure composer (the gate surface) ----------------------------------------


def compose_narration(
    *,
    brief_date: str,
    plan: "DayPlan",
    health_lines: list[tuple[str, str, str]],
    events: list[Any],
    weather_stations: list["StationWeather"],
    waiting_count: int | None,
    config: NarrationConfig | None = None,
) -> BriefNarration:
    """Compose the sectioned narration from already-gathered STRUCTURED inputs.

    Pure (no I/O). Each segment is generated from its structured source and
    clipped to its per-segment word budget. Segments whose text is empty
    (no health data / no events / non-severe weather) are DROPPED so the player
    renders only real slides — but the whole thing being empty is a valid ILB
    state the caller surfaces (``BriefNarration.empty``).

    ``plan`` REPLACED ``view`` in Phase C (C2+C3) and is REQUIRED, not an added
    optional argument: an optional ``plan`` would have left the gatherer free
    to keep passing a raw view forever while every unit pin that passed a plan
    stayed green — the shared spine would be live in the tests and dead in the
    morning. The one production caller (:func:`build_narration`) threads it in
    the same commit.
    """
    cfg = config or NarrationConfig()
    from alfred.tier.compute import DailyGoalState

    goal = plan.daily_goal or DailyGoalState()
    raw: list[tuple[str, str, str]] = [
        (SECTION_DAY_STATE, "State of your day", _day_state_text(goal)),
        (SECTION_HEALTH, "Health", _health_text(health_lines)),
        (SECTION_DAY_PLAN, "Today's plan", _day_plan_text(plan)),
        (SECTION_EVENTS, "Coming up", _events_text(events)),
        (SECTION_WEATHER, "Weather", _weather_text(weather_stations)),
        # ``None`` means the count could not be read — omit the slide rather
        # than speak a number we do not have. Zero is a real answer and IS
        # spoken (see _waiting_text).
        (
            SECTION_WAITING,
            "Waiting on you",
            "" if waiting_count is None else _waiting_text(waiting_count),
        ),
        (SECTION_SIGN_OFF, "Sign-off", _sign_off_text(goal)),
    ]
    segments: list[NarrationSegment] = []
    for section_id, title, text in raw:
        text = (text or "").strip()
        if not text:
            continue  # omit the slide entirely (no empty narration segments)
        text = _clip_words(text, cfg.budget_for(section_id))
        segments.append(NarrationSegment(section_id=section_id, title=title, text=text))

    narration = BriefNarration(brief_date=brief_date, segments=segments)
    log.info(
        "brief.narration.composed",
        brief_date=brief_date,
        segment_count=len(segments),
        total_words=narration.total_words,
        empty=narration.empty,
        sections=[s.section_id for s in segments],
    )
    return narration


# --- async gatherer (thin I/O wiring) ----------------------------------------


async def build_narration(
    config: Any,
    now: datetime,
    *,
    brief_date: str,
    narration_config: NarrationConfig | None = None,
) -> BriefNarration:
    """Gather the structured sources for ``now`` and compose the narration.

    Reads the SAME structured seams the brief renderers use — never the rendered
    markdown. Each source is guarded so one failing source degrades to an omitted
    slide rather than killing the narration (the brief's section-containment
    idiom). ``config`` is a ``BriefConfig``."""
    from alfred.tier.compute import compute_today_view
    from alfred.tier.day_plan import build_day_plan_for_vault

    vault_path = Path(config.vault_path)
    today_local = now.date()

    # 1. Tier view (rings + day-plan) — the C2 stage data.
    try:
        view = compute_today_view(vault_path, now, config.tier_defaults)
    except Exception as exc:  # noqa: BLE001 — a bad view degrades, never kills
        log.warning("brief.narration.view_failed", error=str(exc), error_type=exc.__class__.__name__)
        from alfred.tier.compute import TodayView
        view = TodayView()

    # 1b. The SHARED slot projection (Phase C) — the same object the brief's
    # day section renders, so the spoken and read plans cannot diverge.
    #
    # ``rollover`` is deliberately NOT threaded here. Rollover needs yesterday's
    # curation block plus a scan of the whole task pool for current statuses,
    # which is a second full vault walk, and the spoken segment does not say
    # anything about carryover — the strings are frozen this lane. An empty
    # rollover means every row projects as non-carryover, which is exactly what
    # the current copy describes. When the voice pass gives the player something
    # to SAY about carryover, this is the line that grows the argument.
    try:
        plan = build_day_plan_for_vault(vault_path, view, today_local)
    except Exception as exc:  # noqa: BLE001 — a bad plan degrades, never kills
        log.warning(
            "brief.narration.plan_failed",
            error=str(exc), error_type=exc.__class__.__name__,
        )
        from alfred.tier.day_plan import DayPlan
        plan = DayPlan(daily_goal=view.daily_goal)

    # 2. Health — structured per-tool lines from the latest BIT record.
    health_lines: list[tuple[str, str, str]] = []
    try:
        from .health_section import _find_latest_bit_record, _per_tool_lines
        record = _find_latest_bit_record(vault_path)
        if record is not None:
            health_lines = _per_tool_lines(record.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("brief.narration.health_failed", error=str(exc), error_type=exc.__class__.__name__)

    # 3. Events — structured upcoming items (same pref-aligned collect the feed uses).
    events: list[Any] = []
    try:
        from alfred.preferences.loader import load_active_preferences
        from .upcoming_events import _collect_items
        max_days = int(getattr(config.upcoming_events, "max_days_ahead", 30) or 30)
        try:
            prefs = load_active_preferences(vault_path, shape="action")
        except Exception:  # noqa: BLE001 — mirror render: pref-load failure → no filter
            prefs = []
        events, _filtered = _collect_items(vault_path, today_local, max_days, prefs)
    except Exception as exc:  # noqa: BLE001
        log.warning("brief.narration.events_failed", error=str(exc), error_type=exc.__class__.__name__)

    # 4. Weather — STRUCTURED StationWeather (never the markdown → TAF-safe).
    weather_stations: list["StationWeather"] = []
    try:
        from .weather import fetch_metars, parse_metar
        raw_metars = await fetch_metars(config.weather)
        stations_cfg = list(getattr(config.weather, "stations", []) or [])
        weather_stations = [parse_metar(r, stations_cfg) for r in raw_metars]
    except Exception as exc:  # noqa: BLE001 — weather degrades to an omitted slide
        log.warning("brief.narration.weather_failed", error=str(exc), error_type=exc.__class__.__name__)

    # 5. Deck-waiting count (C3) — the interactive items say HOW MANY and route
    # to the deck rather than being read out. Threaded at the one production
    # entry point in the same commit that added the parameter.
    waiting_count = count_waiting_items(config.feed.store_path)

    return compose_narration(
        brief_date=brief_date,
        plan=plan,
        waiting_count=waiting_count,
        health_lines=health_lines,
        events=events,
        weather_stations=weather_stations,
        config=narration_config,
    )


__all__ = [
    "SECTION_DAY_STATE",
    "SECTION_HEALTH",
    "SECTION_DAY_PLAN",
    "SECTION_EVENTS",
    "SECTION_WEATHER",
    "SECTION_WAITING",
    "SECTION_SIGN_OFF",
    "SEGMENT_ORDER",
    "NarrationConfig",
    "NarrationSegment",
    "BriefNarration",
    "compose_narration",
    "build_narration",
    "count_waiting_items",
]
