"""weather → feed, driven by the REAL captured TAF payload.

This is the producer D7 was built for: a forecast is not a moment. Every
assertion here runs against `tests/fixtures/weather/taf_cyhz_cyzx_cyaw_
20260811.json` — the verbatim aviationweather.gov response captured on-box
2026-08-11 — rather than a hand-written dict, because the whole reason weather
was blocked was that hand-written shapes prove nothing about the live API.

Two calls this file exists to pin:
  * a PROB block's window is a POSSIBLE interval and must NEVER become the
    item's asserted extent (it is preserved in evidence, flagged possible);
  * `timeBec` is a transition-completion moment INSIDE the block window, not a
    second spelling of its start.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from alfred.brief.feed_producer import _epoch_iso, weather_feed_items
from alfred.brief.weather import parse_taf

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures" / "weather" / "taf_cyhz_cyzx_cyaw_20260811.json"
)


@pytest.fixture(scope="module")
def raw_tafs() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def tafs(raw_tafs: list[dict]) -> list[dict]:
    """Through the production ingest boundary, exactly as the daemon does."""
    return [parse_taf(t) for t in raw_tafs]


class _Station:
    def __init__(self, sid: str, name: str) -> None:
        self.id = sid
        self.name = name


STATIONS = [_Station("CYHZ", "Halifax"), _Station("CYZX", "Greenwood")]


def _items(tafs):
    return {
        it.evidence["station"]: it
        for it in weather_feed_items(tafs, STATIONS, instance="salem")
    }


# --- the fixture is what we think it is -------------------------------------


def test_fixture_carries_the_shapes_these_tests_rely_on(tafs: list[dict]) -> None:
    """Guard the GUARD. If a re-capture drops the PROB or BECMG records, the
    pins below would keep passing while testing nothing — so assert the fixture
    still contains the cases before trusting any result derived from it."""
    by_station = {t["icaoId"]: t for t in tafs}
    assert set(by_station) == {"CYHZ", "CYZX", "CYAW"}
    probs = [b for b in by_station["CYHZ"]["fcsts"] if b.get("probability") is not None]
    assert probs, "fixture lost its PROB blocks — the possible-interval pin is vacuous"
    becmg = [b for b in by_station["CYZX"]["fcsts"] if b.get("timeBec") is not None]
    assert len(becmg) == 1, "fixture lost its BECMG block"
    b = becmg[0]
    assert b["timeFrom"] < b["timeBec"] < b["timeTo"], (
        "timeBec must sit INSIDE the block window — that is the whole reason it "
        "is not the block's start"
    )


# --- identity + the asserted extent -----------------------------------------


def test_one_item_per_station_keyed_by_icao(tafs: list[dict]) -> None:
    items = weather_feed_items(tafs, STATIONS, instance="salem")
    assert {it.id for it in items} == {
        "weather:CYHZ", "weather:CYZX", "weather:CYAW",
    }
    assert all(it.kind == "weather" and it.mode == "fyi" for it in items)


def test_item_extent_is_the_forecast_validity_window(tafs: list[dict]) -> None:
    """The interval the forecast asserts outright — a real span, from the real
    payload, not a point and not a rendered string."""
    cyhz = _items(tafs)["CYHZ"]
    assert cyhz.starts_at == "2026-08-11T18:00:00+00:00"
    assert cyhz.ends_at == "2026-08-12T18:00:00+00:00"
    # It is genuinely an INTERVAL, and a long one.
    span = datetime.fromisoformat(cyhz.ends_at) - datetime.fromisoformat(cyhz.starts_at)
    assert span.total_seconds() == 24 * 3600


def test_a_shorter_forecast_keeps_its_own_shorter_window(tafs: list[dict]) -> None:
    """Positive control against a hardcoded 24h: CYAW's window is 12h, so the
    extent is read from the record rather than assumed."""
    cyaw = _items(tafs)["CYAW"]
    span = datetime.fromisoformat(cyaw.ends_at) - datetime.fromisoformat(cyaw.starts_at)
    assert span.total_seconds() == 12 * 3600


# --- THE call: a possible interval is not an asserted one -------------------


def test_prob_blocks_never_become_the_items_extent(tafs: list[dict]) -> None:
    """A PROB30 block says there is a 30% chance of thunderstorms in a window,
    NOT that thunderstorms occupy it. Promoting that to the item's extent would
    state as fact what the data explicitly qualifies.

    The pin: no PROB block's window equals the item's extent, AND the item's
    extent equals the validity window instead.
    """
    cyhz_raw = next(t for t in tafs if t["icaoId"] == "CYHZ")
    item = _items(tafs)["CYHZ"]
    prob_windows = {
        (_epoch_iso(b["timeFrom"]), _epoch_iso(b["timeTo"]))
        for b in cyhz_raw["fcsts"] if b.get("probability") is not None
    }
    assert prob_windows, "no PROB blocks — this pin would be vacuous"
    assert (item.starts_at, item.ends_at) not in prob_windows
    assert (item.starts_at, item.ends_at) == (
        _epoch_iso(cyhz_raw["validTimeFrom"]), _epoch_iso(cyhz_raw["validTimeTo"]),
    )


def test_prob_blocks_are_preserved_and_flagged_possible(tafs: list[dict]) -> None:
    """Nothing is LOST by refusing to assert them — the maybe-window survives
    in evidence with its own interval and its probability, so a renderer can
    draw it in its own register.

    Positive control: a non-PROB block in the same record is NOT flagged
    possible, so the flag carries information.
    """
    periods = _items(tafs)["CYHZ"].evidence["periods"]
    possible = [p for p in periods if p["possible"]]
    certain = [p for p in periods if not p["possible"]]
    assert possible and certain, "need both registers present to prove the flag"
    for p in possible:
        assert p["probability"] == 30
        assert p["change"] == "PROB"
        assert p["starts_at"] and p["ends_at"]  # the window is kept, not dropped
    assert all(p["probability"] is None for p in certain)


# --- timeBec is not a second spelling of the start --------------------------


def test_becoming_at_is_kept_distinct_from_the_period_start(tafs: list[dict]) -> None:
    """On a BECMG block ``timeBec`` marks when the transition COMPLETES, and it
    sits inside the block's window. Conflating it with ``starts_at`` would
    report a change as finished before the forecast says it is."""
    periods = _items(tafs)["CYZX"].evidence["periods"]
    becmg = [p for p in periods if p["becoming_at"] is not None]
    assert len(becmg) == 1
    p = becmg[0]
    assert p["change"] == "BECMG"
    assert p["starts_at"] < p["becoming_at"] < p["ends_at"]
    assert p["becoming_at"] != p["starts_at"]
    # Positive control: every other period carries no becoming_at at all, so
    # the field means something rather than being universally populated.
    assert sum(1 for q in periods if q["becoming_at"] is None) == len(periods) - 1


def test_every_period_carries_its_own_interval(tafs: list[dict]) -> None:
    """Phase B renders fog-as-its-hours off these, so every block must arrive
    with a usable window."""
    for item in weather_feed_items(tafs, STATIONS, instance="salem"):
        periods = item.evidence["periods"]
        assert periods
        assert item.evidence["period_count"] == len(periods)
        for p in periods:
            assert p["starts_at"] is not None and p["ends_at"] is not None
            assert p["starts_at"] < p["ends_at"]


# --- degradation ------------------------------------------------------------


def test_failed_forecast_leg_is_none_not_empty(tafs: list[dict]) -> None:
    """Failure ≠ emptiness. A fetch blip must not mass-``acted`` every
    station's forecast.

    Positive control: a real list (incl. the genuinely-empty one) reconciles.
    """
    assert weather_feed_items(None, STATIONS, instance="salem") is None
    assert weather_feed_items([], STATIONS, instance="salem") == []
    assert len(weather_feed_items(tafs, STATIONS, instance="salem")) == 3


def test_a_record_without_a_station_id_is_skipped_not_keyed_by_ordinal(
    tafs: list[dict],
) -> None:
    """The identity rule: no stable key → no item. Inventing an ordinal key is
    exactly what the model forbids.

    Positive control: the well-formed siblings in the same call still produce
    items, so this cannot pass by the producer dropping everything.
    """
    damaged = [dict(tafs[0], icaoId=""), tafs[1]]
    items = weather_feed_items(damaged, STATIONS, instance="salem")
    assert [it.id for it in items] == ["weather:CYZX"]


def test_unusable_times_degrade_to_no_extent_not_a_lost_item() -> None:
    """A malformed time costs the EXTENT, never the station's item."""
    items = weather_feed_items(
        [{"icaoId": "CYQX", "validTimeFrom": "not-an-epoch", "validTimeTo": None,
          "fcsts": []}],
        STATIONS, instance="salem",
    )
    assert len(items) == 1
    assert items[0].starts_at is None and items[0].ends_at is None


def test_epoch_conversion_refuses_non_epochs() -> None:
    """Positive control paired with each refusal so the helper cannot pass by
    returning None for everything."""
    assert _epoch_iso(1786471200) == "2026-08-11T18:00:00+00:00"
    for junk in (None, "1786471200", True, False, [], {}):
        assert _epoch_iso(junk) is None, junk


def test_stations_without_config_names_still_produce_items(tafs: list[dict]) -> None:
    """Station display names are optional context; a missing config must not
    cost the operator the forecast. Falls back to the ICAO id."""
    items = weather_feed_items(tafs, [], instance="salem")
    assert len(items) == 3
    cyhz = next(it for it in items if it.evidence["station"] == "CYHZ")
    assert cyhz.evidence["name"] == "CYHZ"
    assert cyhz.title == "Forecast: CYHZ"


def test_utc_offset_is_preserved_not_stripped(tafs: list[dict]) -> None:
    """The API's times are UTC instants; the stored extent must stay
    offset-aware or a renderer will localize a naive string and move the
    operator's weather."""
    cyhz = _items(tafs)["CYHZ"]
    assert datetime.fromisoformat(cyhz.starts_at).tzinfo is not None
    assert datetime.fromisoformat(cyhz.starts_at).utcoffset() == timezone.utc.utcoffset(None)
