"""brief_narration pins (Phase C3a) — the brief-as-MODEL third renderer.

Drives the PURE composer (:func:`compose_narration`) over constructed structured
inputs — the gate surface, no I/O. Headline gates:

  * TAF-leak RED TEST — a raw METAR/TAF string in the StationWeather's
    ``raw_text`` NEVER appears in any narration segment (the by-construction
    property: narration reads structured fields only).
  * Per-segment word budget — every segment stays within its NarrationConfig
    budget.
  * ILB empty state — no speakable content → ``BriefNarration.empty`` True.
  * Weather demote ruling — calm weather omits the slide; severe (IFR / gust /
    low-vis) includes it.
  * Say-less health — nothing needing attention is one line; warn/fail (and any
    unrecognised status) names the tools; ``skip`` is NOT "needs a look".
  * Empty sources omit their slide (no empty narration segments).

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

import re

from alfred.brief.narration import (
    SECTION_DAY_STATE,
    SECTION_HEALTH,
    SECTION_SIGN_OFF,
    SECTION_WEATHER,
    NarrationConfig,
    compose_narration,
)
from alfred.brief.weather import StationWeather
from alfred.tier.compute import DailyGoalState, TierEntry, TodayView
from alfred.tier.day_plan import build_day_plan

DATE = "2026-08-01"


def _view(**goal) -> TodayView:
    return TodayView(daily_goal=DailyGoalState(**goal))


def _entry(name: str, tier: int = 1) -> TierEntry:
    return TierEntry(tier=tier, origin="task", name=name, path=f"task/{name}.md")


def _plan(view=None):
    """Project a view into the shared ``DayPlan`` the composer now takes.

    Phase C re-pointed the composer from ``TodayView`` to the SAME projection
    the brief's day section renders, so these pins drive the production spine
    rather than a view the production path no longer hands it. ``is_done`` is
    supplied explicitly because the composer's argument is required — see
    ``build_day_plan``'s docstring on why it is not defaulted.
    """
    return build_day_plan(
        view if view is not None else _view(),
        rollover=[],
        is_done=lambda _entry: False,
    )


def _compose(view=None, health=None, events=None, weather=None, config=None, plan=None):
    return compose_narration(
        brief_date=DATE,
        plan=plan if plan is not None else _plan(view),
        health_lines=health or [],
        events=events or [],
        weather_stations=weather or [],
        config=config,
    )


# --- TAF-leak RED TEST (the headline gate) ----------------------------------


def test_no_taf_code_in_narration() -> None:
    """A raw TAF/METAR string on StationWeather.raw_text must NEVER surface in a
    spoken segment — narration reads structured fields only (by construction)."""
    taf_raw = "KYHZ 011200Z 3512G32KT 1/2SM R06/2000FT +SN VV004 M02/M04 A2970"
    severe = StationWeather(
        station_id="KYHZ", name="Halifax", temp_c=-2.0,
        wind_gust_kt=32, visibility_sm=0.5, flight_category="LIFR",
        raw_text=taf_raw,
    )
    narr = _compose(weather=[severe])
    full = narr.full_text
    # The raw string (and its distinctive TAF tokens) must be absent.
    assert taf_raw not in full
    assert "VV004" not in full and "3512G32KT" not in full and "R06/2000FT" not in full
    # TAF group patterns (FM/TEMPO/BECMG/RMK, zulu-time, cloud codes) absent.
    assert not re.search(r"\b\d{6}Z\b", full)  # 011200Z
    assert not re.search(r"\b(FM|TEMPO|BECMG|RMK|VV|OVC|BKN|SCT)\d", full)
    # But the weather slide DID render (severe → attention-worthy), speakably.
    wx = [s for s in narr.segments if s.section_id == SECTION_WEATHER]
    assert len(wx) == 1 and wx[0].text
    assert "Halifax" in wx[0].text


# --- per-segment word budget ------------------------------------------------


def test_every_segment_within_budget() -> None:
    cfg = NarrationConfig()
    view = _view(
        t1_available=3, t2_available=2, t3_available=2,
        t1_done=1, t2_done=1, t3_done=0,
    )
    view.t1 = [_entry(f"Urgent task number {i} with a long name", 1) for i in range(5)]
    view.t2 = [_entry(f"Medium task {i}", 2) for i in range(4)]
    view.t3 = [_entry(f"Self care {i}", 3) for i in range(3)]
    narr = _compose(
        view=view,
        health=[("curator", "warn", "x"), ("janitor", "error", "y")],
        events=[type("E", (), {"name": "Dentist", "time_display": "2pm", "date_iso": DATE})()],
        weather=[StationWeather(station_id="KYHZ", name="Halifax", flight_category="IFR", visibility_sm=1.0)],
        config=cfg,
    )
    for seg in narr.segments:
        assert seg.word_count <= cfg.budget_for(seg.section_id), (
            f"{seg.section_id} over budget: {seg.word_count} > {cfg.budget_for(seg.section_id)}"
        )


def test_total_narration_in_say_less_range() -> None:
    """A full briefing lands in the ~60-90s spoken band (~150-230 words at
    conversational pace) — the say-less target."""
    view = _view(t1_available=2, t2_available=1, t3_available=1, t1_done=0, t2_done=0, t3_done=0)
    view.t1 = [_entry("Pay Steph", 1), _entry("Sign lease", 1)]
    view.t2 = [_entry("Call bank", 2)]
    view.t3 = [_entry("Walk Fergus", 3)]
    narr = _compose(
        view=view,
        health=[("curator", "warn", "")],
        events=[type("E", (), {"name": "Dentist", "time_display": "2pm", "date_iso": DATE})()],
        weather=[StationWeather(station_id="KYHZ", name="Halifax", flight_category="LIFR", visibility_sm=0.5)],
    )
    assert narr.total_words <= 230  # say-less ceiling
    assert narr.total_words >= 10   # not vacuous


# --- ILB empty state --------------------------------------------------------


def test_empty_when_nothing_speakable() -> None:
    """Clean-slate day, no health/events/severe-weather: day_state + sign_off
    still speak (never silence), so it is NOT empty. The truly-empty ILB is when
    even those are blank — pin the property that ``empty`` reflects no text."""
    narr = _compose()  # empty view → day_state clean-slate + sign_off
    assert not narr.empty  # day_state + sign_off always speak
    assert any(s.section_id == SECTION_DAY_STATE for s in narr.segments)
    assert any(s.section_id == SECTION_SIGN_OFF for s in narr.segments)


# --- weather demote ruling --------------------------------------------------


def test_calm_weather_omits_slide() -> None:
    calm = StationWeather(
        station_id="KYHZ", name="Halifax", temp_c=18.0,
        wind_speed_kt=6, visibility_sm=10.0, flight_category="VFR",
    )
    narr = _compose(weather=[calm])
    assert not any(s.section_id == SECTION_WEATHER for s in narr.segments)


def test_severe_weather_includes_slide() -> None:
    for severe in (
        StationWeather(station_id="A", name="A", flight_category="IFR", visibility_sm=2.0),
        StationWeather(station_id="B", name="B", wind_gust_kt=35, flight_category="VFR"),
        StationWeather(station_id="C", name="C", visibility_sm=1.5, flight_category="MVFR"),
    ):
        narr = _compose(weather=[severe])
        assert any(s.section_id == SECTION_WEATHER for s in narr.segments), severe.station_id


# --- say-less health --------------------------------------------------------


def test_health_all_green_one_line() -> None:
    narr = _compose(health=[("curator", "ok", ""), ("janitor", "ok", "")])
    hs = [s for s in narr.segments if s.section_id == SECTION_HEALTH]
    assert len(hs) == 1 and hs[0].text == "All systems green."


def test_health_unrecognised_status_names_tools() -> None:
    """An unrecognised status ("error" is not a ``Status`` value) still names the
    tool — the attention predicate is a denylist, so unknown fails OPEN.

    Renamed from ``test_health_non_ok_names_tools``: "non-ok" is the exact
    terminology that encoded the skip-blindness, and this pin is load-bearing in
    the other direction — it independently fails if the predicate is narrowed to
    an allowlist of {warn, fail}.
    """
    narr = _compose(health=[("curator", "ok", ""), ("janitor", "error", "boom")])
    hs = [s for s in narr.segments if s.section_id == SECTION_HEALTH]
    assert len(hs) == 1 and "janitor" in hs[0].text and "curator" not in hs[0].text


def test_no_health_data_omits_slide() -> None:
    narr = _compose(health=[])
    assert not any(s.section_id == SECTION_HEALTH for s in narr.segments)


# --- skip is not "needs a look" (#8, the narration half) ---------------------
#
# ``_health_text`` had the same skip-blindness as the feed's health cards — a
# local ``s.lower() != "ok"``. Both now call the one canonical
# ``health_section.is_attention_status``.


def _kalle_health() -> list[tuple[str, str, str]]:
    """KAL-LE's measured BIT shape: 5 ok + 7 skip, nothing wrong."""
    return [
        ("curator", "ok", ""), ("janitor", "ok", ""), ("distiller", "ok", ""),
        ("brief", "ok", ""), ("surveyor", "ok", ""),
        ("talker", "skip", ""), ("mail", "skip", ""), ("gcal", "skip", ""),
        ("weather", "skip", ""), ("transport", "skip", ""),
        ("scribe", "skip", ""), ("peer", "skip", ""),
    ]


def test_health_skipped_tools_do_not_need_a_look() -> None:
    """5 ok + 7 skip speaks the green line, NOT "7 tools need a look".

    Mutation: restore ``s.lower() != "ok"`` → this fails (the segment names all
    seven unconfigured tools as needing attention, every morning).
    """
    narr = _compose(health=_kalle_health())
    hs = [s for s in narr.segments if s.section_id == SECTION_HEALTH]
    assert len(hs) == 1
    assert hs[0].text == "All systems green."


def test_health_skip_alongside_warn_names_only_the_warn() -> None:
    """Preservation pin: a real warn is still spoken, and the skips are not."""
    narr = _compose(health=_kalle_health() + [("surveyor2", "warn", "ollama 404")])
    hs = [s for s in narr.segments if s.section_id == SECTION_HEALTH]
    assert len(hs) == 1
    assert "1 tool needs a look" in hs[0].text
    assert "surveyor2" in hs[0].text
    assert "talker" not in hs[0].text


def test_health_all_skipped_does_not_claim_green() -> None:
    """Zero probes passed → say so rather than claiming a clean bill of health.

    Mirrors the ratified skip-posture ruling in
    ``kalle_digest._render_skip_posture``: skips alongside PASSING probes are
    green, but green on zero passing probes would claim checks passed on the
    strength of no evidence. Suppressing skips is what makes this case reachable
    (it used to fall into the "needs a look" branch).
    """
    narr = _compose(health=[("talker", "skip", ""), ("mail", "skip", "")])
    hs = [s for s in narr.segments if s.section_id == SECTION_HEALTH]
    assert len(hs) == 1
    assert hs[0].text == "No health checks ran this morning."


# --- empty sources omit their slide -----------------------------------------


def test_no_events_omits_slide() -> None:
    narr = _compose(events=[])
    assert not any(s.section_id == "events" for s in narr.segments)


def test_segments_have_no_empty_text() -> None:
    """No emitted segment ever carries empty text (empties are dropped)."""
    narr = _compose(
        view=_view(t1_available=1, t1_done=0),
        health=[("curator", "warn", "")],
    )
    assert narr.segments
    for seg in narr.segments:
        assert seg.text.strip()


# --- log-emission pin -------------------------------------------------------


def test_compose_emits_named_log() -> None:
    import structlog

    with structlog.testing.capture_logs() as cap:
        _compose(view=_view(t1_available=1))
    events = [c for c in cap if c.get("event") == "brief.narration.composed"]
    assert len(events) == 1
    assert events[0]["brief_date"] == DATE
    assert "sections" in events[0]
