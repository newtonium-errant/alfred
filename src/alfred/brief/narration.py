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
    from alfred.tier.compute import TodayView

log = get_logger(__name__)


# --- section ids (stable — the C3c primer + the FE deep-links key on these) ---
SECTION_DAY_STATE = "day_state"
SECTION_HEALTH = "health"
SECTION_DAY_PLAN = "day_plan"
SECTION_EVENTS = "events"
SECTION_WEATHER = "weather"
SECTION_SIGN_OFF = "sign_off"

# Segment order — the player renders slides in this order.
SEGMENT_ORDER = (
    SECTION_DAY_STATE,
    SECTION_HEALTH,
    SECTION_DAY_PLAN,
    SECTION_EVENTS,
    SECTION_WEATHER,
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
    budget_sign_off: int = 20

    def budget_for(self, section_id: str) -> int:
        return {
            SECTION_DAY_STATE: self.budget_day_state,
            SECTION_HEALTH: self.budget_health,
            SECTION_DAY_PLAN: self.budget_day_plan,
            SECTION_EVENTS: self.budget_events,
            SECTION_WEATHER: self.budget_weather,
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


def _day_state_text(view: "TodayView") -> str:
    """The rings/day-state segment from the C2 ``daily_goal`` stage data."""
    g = view.daily_goal
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


def _day_plan_text(view: "TodayView") -> str:
    """The day-plan segment — the T1/T2/T3 items, urgent first, names only
    (the visual slide carries the full list; narration names the top few)."""
    def _names(entries, limit):
        return [e.name for e in entries[:limit] if getattr(e, "name", "")]

    t1 = _names(view.t1, 3)
    t2 = _names(view.t2, 2)
    if not t1 and not t2 and not view.t3:
        return "No urgent or medium items — today's yours to shape."
    parts: list[str] = []
    if t1:
        parts.append("Urgent: " + ", ".join(t1) + ".")
    if t2:
        parts.append("Then: " + ", ".join(t2) + ".")
    if view.t3:
        parts.append(f"Plus {len(view.t3)} self-care intention{'s' if len(view.t3) != 1 else ''}.")
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


def _sign_off_text(view: "TodayView") -> str:
    """A short encouraging close keyed on the daily goal (self-correcting-friendly
    tone; the composer log captures which segments draw questions later)."""
    g = view.daily_goal
    if g.balanced_day:
        return "You're set up well. Go get it."
    if g.t1_available and g.t1_done < g.t1_available:
        return "One urgent thing first, then the rest follows. You've got this."
    return "That's the shape of your day. Take it one slot at a time."


# --- pure composer (the gate surface) ----------------------------------------


def compose_narration(
    *,
    brief_date: str,
    view: "TodayView",
    health_lines: list[tuple[str, str, str]],
    events: list[Any],
    weather_stations: list["StationWeather"],
    config: NarrationConfig | None = None,
) -> BriefNarration:
    """Compose the sectioned narration from already-gathered STRUCTURED inputs.

    Pure (no I/O). Each segment is generated from its structured source and
    clipped to its per-segment word budget. Segments whose text is empty
    (no health data / no events / non-severe weather) are DROPPED so the player
    renders only real slides — but the whole thing being empty is a valid ILB
    state the caller surfaces (``BriefNarration.empty``).
    """
    cfg = config or NarrationConfig()
    raw: list[tuple[str, str, str]] = [
        (SECTION_DAY_STATE, "State of your day", _day_state_text(view)),
        (SECTION_HEALTH, "Health", _health_text(health_lines)),
        (SECTION_DAY_PLAN, "Today's plan", _day_plan_text(view)),
        (SECTION_EVENTS, "Coming up", _events_text(events)),
        (SECTION_WEATHER, "Weather", _weather_text(weather_stations)),
        (SECTION_SIGN_OFF, "Sign-off", _sign_off_text(view)),
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

    vault_path = Path(config.vault_path)
    today_local = now.date()

    # 1. Tier view (rings + day-plan) — the C2 stage data.
    try:
        view = compute_today_view(vault_path, now, config.tier_defaults)
    except Exception as exc:  # noqa: BLE001 — a bad view degrades, never kills
        log.warning("brief.narration.view_failed", error=str(exc), error_type=exc.__class__.__name__)
        from alfred.tier.compute import TodayView
        view = TodayView()

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

    return compose_narration(
        brief_date=brief_date,
        view=view,
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
    "SECTION_SIGN_OFF",
    "SEGMENT_ORDER",
    "NarrationConfig",
    "NarrationSegment",
    "BriefNarration",
    "compose_narration",
    "build_narration",
]
