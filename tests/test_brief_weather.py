"""Tests for ``alfred.brief.weather`` — the boundary parser + formatters.

P0 regression pin (2026-05-10): aviationweather.gov returns ``visib``
as a mixed type (int / float / numeric string / ``"<N>+"`` like ``"10+"``).
The dataclass field ``StationWeather.visibility_sm`` is typed
``float | None``, but for ~10 days the parser populated it untransformed
straight from ``raw.get("visib")``. ``_format_visibility``'s
``w.visibility_sm >= 10`` then raised
``TypeError: '>=' not supported between instances of 'str' and 'int'``
which the brief daemon's ``except Exception:`` swallowed as a logged
warning while continuing to sleep — operationally invisible at the
log level, only surfacing as ``vault/run/`` going empty for 10 days.

These pins lock the contract: ``_parse_visibility`` ALWAYS returns
``float | None`` regardless of API shape, and ``_format_visibility``
works correctly against every observed API output.

Tests run unconditionally (no ``pytest.importorskip``) per
``feedback_regression_pin_unconditional.md`` — boundary validators
on production-breaking bugs MUST pin behavior whether or not optional
deps load.
"""

from __future__ import annotations

import structlog

from alfred.brief.weather import (
    StationWeather,
    _format_visibility,
    _parse_visibility,
)


# ---------------------------------------------------------------------------
# _parse_visibility — every observed API shape, plus invariants
# ---------------------------------------------------------------------------


class TestParseVisibilityNumericInputs:
    def test_int_returned_as_float(self) -> None:
        assert _parse_visibility(12) == 12.0
        assert isinstance(_parse_visibility(12), float)

    def test_float_passthrough(self) -> None:
        assert _parse_visibility(7.5) == 7.5

    def test_zero_is_valid_value(self) -> None:
        # Zero visibility is a real condition (heavy fog) — must not
        # be confused with None.
        assert _parse_visibility(0) == 0.0
        assert _parse_visibility(0.0) == 0.0


class TestParseVisibilityStringInputs:
    def test_at_least_n_plus_strips_suffix(self) -> None:
        # The bug-of-record. ``"10+"`` was raising TypeError downstream.
        assert _parse_visibility("10+") == 10.0

    def test_six_plus_at_least_n(self) -> None:
        # The TAF path's ``"6+"`` convention also covered.
        assert _parse_visibility("6+") == 6.0

    def test_at_least_n_with_decimal(self) -> None:
        # FAA encoding ``"7.5+"`` is rare but legal under the +-suffix
        # convention. Numeric prefix parses as float.
        assert _parse_visibility("7.5+") == 7.5

    def test_plain_numeric_string(self) -> None:
        # Some observations come through as ``"6"`` (no suffix).
        assert _parse_visibility("6") == 6.0
        assert _parse_visibility("12") == 12.0

    def test_decimal_string(self) -> None:
        assert _parse_visibility("3.5") == 3.5

    def test_string_with_whitespace_stripped(self) -> None:
        # Defensive: a stray space in the API response shouldn't crash.
        assert _parse_visibility(" 10 ") == 10.0
        assert _parse_visibility("10+ ") == 10.0


class TestParseVisibilityNoneAndEmpty:
    def test_none_returns_none(self) -> None:
        assert _parse_visibility(None) is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_visibility("") is None

    def test_whitespace_only_string_returns_none(self) -> None:
        assert _parse_visibility("   ") is None


class TestParseVisibilityUnparseable:
    def test_alpha_string_returns_none_with_warn_log(self) -> None:
        # Pin both the value AND the operator-trace log per the
        # subprocess-failure-logging discipline. Silent drop without
        # operator-trace would mean a future API surface change
        # (e.g. ``"VRB"``-shaped vis) goes undetected for days.
        with structlog.testing.capture_logs() as captured:
            assert _parse_visibility("not-a-number", station_id="CYZX") is None
        warns = [c for c in captured if c.get("event") == "weather.visibility_unparseable"]
        assert len(warns) == 1
        w = warns[0]
        assert w["raw"] == "not-a-number"
        assert w["station"] == "CYZX"
        assert w["reason"] == "non_numeric_string"

    def test_alpha_string_with_plus_returns_none(self) -> None:
        # ``"abc+"`` strips to ``"abc"`` then fails float() — must be
        # treated as unparseable, not silently coerced.
        with structlog.testing.capture_logs() as captured:
            assert _parse_visibility("abc+") is None
        warns = [c for c in captured if c.get("event") == "weather.visibility_unparseable"]
        assert len(warns) == 1

    def test_unexpected_type_returns_none_with_warn(self) -> None:
        # API surface change to a list / dict / object would otherwise
        # leak through and crash downstream. Pin the type-rejection.
        with structlog.testing.capture_logs() as captured:
            assert _parse_visibility([10], station_id="CYHZ") is None
        warns = [c for c in captured if c.get("event") == "weather.visibility_unparseable"]
        assert len(warns) == 1
        assert warns[0]["station"] == "CYHZ"
        assert warns[0]["reason"] == "type_list"

    def test_bool_rejected_explicitly(self) -> None:
        # isinstance(True, int) is True in Python — without the
        # explicit bool branch, ``_parse_visibility(True)`` would
        # smuggle 1.0 through the int branch. Defensive pin: a bool
        # in the API JSON returns None + warn rather than 1.0.
        with structlog.testing.capture_logs() as captured:
            assert _parse_visibility(True) is None
        warns = [c for c in captured if c.get("event") == "weather.visibility_unparseable"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "bool"


# ---------------------------------------------------------------------------
# _format_visibility — works correctly against every parsed shape
# ---------------------------------------------------------------------------


class TestFormatVisibility:
    def _w(self, vis: float | None) -> StationWeather:
        return StationWeather(station_id="CYZX", name="Greenwood", visibility_sm=vis)

    def test_none_renders_na(self) -> None:
        assert _format_visibility(self._w(None)) == "N/A"

    def test_at_or_above_ten_renders_gt10sm(self) -> None:
        # The bug-of-record path: parser produced 10.0 from ``"10+"``,
        # the ``>= 10`` branch fires, output is ``">10SM"``.
        assert _format_visibility(self._w(10.0)) == ">10SM"
        assert _format_visibility(self._w(12.0)) == ">10SM"
        assert _format_visibility(self._w(15.0)) == ">10SM"

    def test_below_ten_renders_with_value(self) -> None:
        assert _format_visibility(self._w(6.0)) == "6.0SM"
        assert _format_visibility(self._w(3.5)) == "3.5SM"

    def test_zero_renders_as_zero(self) -> None:
        # Zero visibility (heavy fog) is real. Must render, not crash.
        assert _format_visibility(self._w(0.0)) == "0.0SM"


# ---------------------------------------------------------------------------
# End-to-end regression — parse_metar + _format_visibility on the
# bug-of-record path. This is the test that would have caught the P0
# at introduction time.
# ---------------------------------------------------------------------------


class TestParseMetarToFormatPipeline:
    def test_string_visib_does_not_crash_format(self) -> None:
        # The exact shape from the 2026-05-10 production trace:
        # API returns ``"visib": "10+"`` for clear conditions, parser
        # built StationWeather with the string untransformed, and
        # _format_visibility raised TypeError on ``>= 10``.
        # End-to-end pin: parse_metar produces a StationWeather with
        # a numeric visibility_sm, _format_visibility consumes it
        # without crashing.
        from alfred.brief.weather import parse_metar
        from alfred.brief.config import StationConfig

        raw = {
            "icaoId": "CYZX",
            "visib": "10+",  # ← the bug-of-record shape
            "temp": 15.0,
            "wdir": 270,
            "wspd": 8,
            "fltCat": "VFR",
        }
        station_configs = [StationConfig(id="CYZX", name="Greenwood", primary=True)]
        sw = parse_metar(raw, station_configs)

        assert sw.visibility_sm == 10.0
        assert isinstance(sw.visibility_sm, float)
        # Must not raise.
        assert _format_visibility(sw) == ">10SM"

    def test_int_visib_passthrough(self) -> None:
        from alfred.brief.weather import parse_metar
        from alfred.brief.config import StationConfig

        raw = {"icaoId": "CYHZ", "visib": 12, "fltCat": "VFR"}
        station_configs = [StationConfig(id="CYHZ", name="Halifax")]
        sw = parse_metar(raw, station_configs)
        assert sw.visibility_sm == 12.0
        assert _format_visibility(sw) == ">10SM"

    def test_missing_visib_yields_none(self) -> None:
        from alfred.brief.weather import parse_metar
        from alfred.brief.config import StationConfig

        raw = {"icaoId": "CYAW", "fltCat": "VFR"}
        station_configs = [StationConfig(id="CYAW", name="Shearwater")]
        sw = parse_metar(raw, station_configs)
        assert sw.visibility_sm is None
        assert _format_visibility(sw) == "N/A"

    def test_unparseable_visib_yields_none_with_log(self) -> None:
        from alfred.brief.weather import parse_metar
        from alfred.brief.config import StationConfig

        raw = {"icaoId": "CYQI", "visib": "garbage", "fltCat": "VFR"}
        station_configs = [StationConfig(id="CYQI", name="Yarmouth")]
        with structlog.testing.capture_logs() as captured:
            sw = parse_metar(raw, station_configs)
        assert sw.visibility_sm is None
        # Operator-visible trace is the contract — silent drop is
        # actively wrong here per the subprocess-failure-logging
        # discipline (don't lose data without operator notification).
        warns = [c for c in captured if c.get("event") == "weather.visibility_unparseable"]
        assert len(warns) == 1
        assert warns[0]["station"] == "CYQI"


# ---------------------------------------------------------------------------
# TAF path — same root cause as METAR. ``_format_taf_period`` previously
# special-cased ``"6+"`` only; ``"<N>+"`` for N != 6 would render literally
# as ``"10+SM"``. ``format_taf_section``'s is_sig gate had a defensive
# isinstance check that avoided crashes but silently dropped string-shaped
# vis from the low-vis significance signal. Both paths now consume
# ``_parse_visibility`` for consistent behavior across API shapes.
# ---------------------------------------------------------------------------


class TestFormatTafPeriodVisibility:
    def test_at_least_10_renders_gt10sm(self) -> None:
        # The bug-of-record shape on the TAF side. API returns
        # ``"visib": "10+"`` for clear forecast; previous code would
        # render literally as ``"10+SM"``. Boundary parser → 10.0 →
        # ``>10SM``.
        from alfred.brief.weather import _format_taf_period
        out = _format_taf_period({"fcstChange": "FM", "visib": "10+"})
        assert ">10SM" in out
        assert "10+SM" not in out

    def test_six_plus_preserves_at_least_semantic(self) -> None:
        # The legacy special-case still works. ``"6+"`` parsed → 6.0,
        # raw value ends with ``+`` so we render ``>6SM`` not the bare
        # numeric ``6.0SM`` — preserving operator-meaningful semantics
        # in the 6..10 band where ``+`` suffix is the FAA at-least
        # signal but the value is below the universal >10 threshold.
        from alfred.brief.weather import _format_taf_period
        out = _format_taf_period({"fcstChange": "BASE", "visib": "6+"})
        assert ">6SM" in out

    def test_int_visib_renders_normally(self) -> None:
        from alfred.brief.weather import _format_taf_period
        out = _format_taf_period({"fcstChange": "BASE", "visib": 4})
        assert "4.0SM" in out

    def test_below_6_low_vis_renders_with_value(self) -> None:
        # Low-vis numeric — no ``+`` semantic, render the raw float.
        from alfred.brief.weather import _format_taf_period
        out = _format_taf_period({"fcstChange": "TEMPO", "visib": 2.5})
        assert "2.5SM" in out

    def test_unparseable_visib_omitted_with_warn_log(self) -> None:
        # The boundary parser drops + warns. The TAF formatter then
        # has nothing to append for vis (vis is None). Pin the log
        # emission to catch silent-drop regressions per the
        # log-emission test discipline.
        from alfred.brief.weather import _format_taf_period
        with structlog.testing.capture_logs() as captured:
            out = _format_taf_period(
                {"fcstChange": "FM", "visib": "garbage"},
                station_id="CYHZ",
            )
        # No vis token in output — neither the garbage nor an SM render.
        assert "garbage" not in out
        warns = [c for c in captured if c.get("event") == "weather.visibility_unparseable"]
        assert len(warns) == 1
        assert warns[0]["station"] == "CYHZ"

    def test_missing_visib_omitted_cleanly(self) -> None:
        # Period with no ``visib`` key — vis is None, no token emitted,
        # no log fires (None is the well-formed empty case).
        from alfred.brief.weather import _format_taf_period
        with structlog.testing.capture_logs() as captured:
            out = _format_taf_period({"fcstChange": "BASE"})
        assert "SM" not in out
        warns = [c for c in captured if c.get("event") == "weather.visibility_unparseable"]
        assert len(warns) == 0


class TestFormatTafSectionSignificanceGate:
    """The ``format_taf_section`` is_sig gate filters TAF periods to
    "key changes" — significant weather, low visibility, or BECMG/FM
    transitions. The legacy gate used ``isinstance(vis, (int, float)) and
    vis < 6`` which silently dropped string-shaped vis from the low-vis
    signal. Boundary parser fixes that contract.
    """

    def _taf(self, periods: list[dict]) -> dict:
        return {
            "icaoId": "CYZX",
            "rawTAF": "TAF CYZX 101200Z 1012/1112 27010KT P6SM",
            "fcsts": periods,
        }

    def test_string_low_vis_now_registered_as_significant(self) -> None:
        # The legacy gate's isinstance check would silently drop a
        # string-shaped low-vis value (``"3"``) — gate would say
        # "not sig" and the period would be omitted from "Key
        # changes". Boundary parser → 3.0 → gate registers as sig.
        from alfred.brief.weather import format_taf_section
        from alfred.brief.config import StationConfig

        taf = self._taf([
            {"fcstChange": "TEMPO", "visib": "3"},
        ])
        out = format_taf_section([taf], [StationConfig(id="CYZX", name="Greenwood")])
        # The rendered low-vis period must show up under "Key changes".
        assert "Key changes" in out
        assert "3.0SM" in out

    def test_at_least_10_not_significant_unless_other_signal(self) -> None:
        # ``"10+"`` (parsed → 10.0) is not low-vis. Without other
        # signals (no wxString, no FM/BECMG change), the period is NOT
        # significant → no Key changes line.
        from alfred.brief.weather import format_taf_section
        from alfred.brief.config import StationConfig

        taf = self._taf([
            {"fcstChange": "BASE", "visib": "10+"},
        ])
        out = format_taf_section([taf], [StationConfig(id="CYZX", name="Greenwood")])
        # Header always present; the per-period "Key changes" line is
        # the conditional one.
        assert "Key changes" not in out

    def test_six_plus_not_significant_at_or_above_threshold(self) -> None:
        # ``"6+"`` (parsed → 6.0) is at the gate boundary (gate uses
        # ``< 6``). At 6 exactly, NOT significant.
        from alfred.brief.weather import format_taf_section
        from alfred.brief.config import StationConfig

        taf = self._taf([
            {"fcstChange": "BASE", "visib": "6+"},
        ])
        out = format_taf_section([taf], [StationConfig(id="CYZX", name="Greenwood")])
        assert "Key changes" not in out

    def test_int_low_vis_still_significant(self) -> None:
        # Numeric path stays unchanged — int low-vis is still
        # significant. Pin to catch regressions on the easy path.
        from alfred.brief.weather import format_taf_section
        from alfred.brief.config import StationConfig

        taf = self._taf([
            {"fcstChange": "TEMPO", "visib": 2},
        ])
        out = format_taf_section([taf], [StationConfig(id="CYZX", name="Greenwood")])
        assert "Key changes" in out
        assert "2.0SM" in out

    def test_fm_change_significant_regardless_of_vis(self) -> None:
        # FM transition is ALWAYS significant, even with no other
        # signals. Pin to ensure the gate's ``or`` semantics are
        # preserved across the boundary-parser refactor.
        from alfred.brief.weather import format_taf_section
        from alfred.brief.config import StationConfig

        taf = self._taf([
            {"fcstChange": "FM", "visib": "10+"},
        ])
        out = format_taf_section([taf], [StationConfig(id="CYZX", name="Greenwood")])
        assert "Key changes" in out
