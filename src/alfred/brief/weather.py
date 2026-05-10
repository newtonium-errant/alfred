"""Weather section — fetch METAR/TAF from aviationweather.gov and format as markdown."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .config import StationConfig, WeatherConfig
from .utils import get_logger

log = get_logger(__name__)


@dataclass
class StationWeather:
    station_id: str
    name: str
    temp_c: float | None = None
    dewpoint_c: float | None = None
    wind_dir: int | None = None
    wind_speed_kt: int | None = None
    wind_gust_kt: int | None = None
    visibility_sm: float | None = None
    ceiling_ft: int | None = None
    cloud_cover: str = ""
    clouds: list[dict] = field(default_factory=list)
    flight_category: str = ""
    raw_text: str = ""
    observed_at: str = ""


def _parse_visibility(raw_visib: object, *, station_id: str = "") -> float | None:
    """Coerce aviationweather.gov's mixed-type ``visib`` field into a float.

    Boundary validator per the validate-at-system-boundary discipline —
    the API IS the boundary here, so the parse happens once at
    construction time and the rest of the module trusts that
    ``StationWeather.visibility_sm`` is ``float | None`` (per the
    dataclass field type).

    Live API surface (verified 2026-05-10):
    - numeric (int / float / numeric string): treated as miles, returned as float
    - ``"10+"`` / ``"6+"`` / ``"<N>+"``: aviationweather convention for "at least N
      statute miles." Returns the bare ``N`` — downstream
      ``_format_visibility`` re-adds the ``>10SM`` rendering when value >= 10
    - ``""`` / ``None`` / unparseable shape: returns ``None``; logs
      ``weather.visibility_unparseable`` so the operator has a trace
      rather than a silent drop

    Note: the bare ``N`` returned for ``"<N>+"`` is a small fidelity loss
    (we discard the "+") but matches the rendering contract of
    ``_format_visibility``: any value >= 10 renders as ``>10SM`` regardless,
    and ``"<N>+"`` for N < 10 is exceedingly rare in METAR data (the FAA
    convention is to emit a numeric value once below the >10SM threshold).

    Per the 2026-05-10 P0 fix (operator brief): ``brief.daemon`` was
    silently swallowing ``TypeError: '>=' not supported between instances
    of 'str' and 'int'`` for ~10 days because the dataclass field was
    typed ``float | None`` but populated with the API's mixed type
    untransformed. This parser is the contract enforcer.
    """
    if raw_visib is None:
        return None
    if isinstance(raw_visib, bool):
        # ``isinstance(True, int)`` is True in Python — handle bool
        # explicitly so a stray API JSON boolean doesn't smuggle 1.0
        # through the int branch below.
        log.warning(
            "weather.visibility_unparseable",
            raw=raw_visib,
            station=station_id,
            reason="bool",
        )
        return None
    if isinstance(raw_visib, (int, float)):
        return float(raw_visib)
    if isinstance(raw_visib, str):
        s = raw_visib.strip()
        if not s:
            return None
        # FAA "<N>+" — at-least-N. Strip the trailing "+" and parse the
        # numeric prefix.
        if s.endswith("+"):
            s = s[:-1].strip()
        try:
            return float(s)
        except ValueError:
            log.warning(
                "weather.visibility_unparseable",
                raw=raw_visib,
                station=station_id,
                reason="non_numeric_string",
            )
            return None
    log.warning(
        "weather.visibility_unparseable",
        raw=raw_visib,
        station=station_id,
        reason=f"type_{type(raw_visib).__name__}",
    )
    return None


async def fetch_metars(config: WeatherConfig) -> list[dict]:
    """Fetch METAR data for configured stations."""
    ids = ",".join(s.id for s in config.stations)
    url = f"{config.api_base}/metar?ids={ids}&format=json"
    log.info("weather.fetching_metars", url=url)
    async with httpx.AsyncClient(timeout=config.timeout) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


def parse_metar(raw: dict, station_configs: list[StationConfig]) -> StationWeather:
    """Parse a single METAR API response into StationWeather."""
    station_id = raw.get("icaoId", "")
    name_map = {s.id: s.name for s in station_configs}

    # Find lowest ceiling (BKN or OVC layer)
    ceiling_ft = None
    for cloud in raw.get("clouds", []):
        cover = cloud.get("cover", "")
        if cover in ("BKN", "OVC", "VV"):
            base = cloud.get("base")
            if base is not None and (ceiling_ft is None or base < ceiling_ft):
                ceiling_ft = base

    return StationWeather(
        station_id=station_id,
        name=name_map.get(station_id, raw.get("name", station_id)),
        temp_c=raw.get("temp"),
        dewpoint_c=raw.get("dewp"),
        wind_dir=raw.get("wdir"),
        wind_speed_kt=raw.get("wspd"),
        wind_gust_kt=raw.get("wgst"),
        # API returns ``visib`` as int / float / numeric-string / ``"<N>+"``
        # depending on conditions. ``_parse_visibility`` is the boundary
        # validator that coerces all shapes into the dataclass's typed
        # ``float | None``. See the helper's docstring for the full
        # surface + the 2026-05-10 P0 incident the boundary fix addresses.
        visibility_sm=_parse_visibility(raw.get("visib"), station_id=station_id),
        ceiling_ft=ceiling_ft,
        cloud_cover=raw.get("cover", ""),
        clouds=raw.get("clouds", []),
        flight_category=raw.get("fltCat", ""),
        raw_text=raw.get("rawOb", ""),
        observed_at=raw.get("reportTime", ""),
    )


def _format_wind(w: StationWeather) -> str:
    if w.wind_speed_kt is None:
        return "Calm"
    if w.wind_speed_kt == 0:
        return "Calm"
    wdir = str(w.wind_dir).zfill(3) if w.wind_dir is not None else "VRB"
    if w.wind_gust_kt:
        return f"{wdir}/{w.wind_speed_kt}G{w.wind_gust_kt}kt"
    return f"{wdir}/{w.wind_speed_kt}kt"


def _format_visibility(w: StationWeather) -> str:
    if w.visibility_sm is None:
        return "N/A"
    if w.visibility_sm >= 10:
        return ">10SM"
    return f"{w.visibility_sm}SM"


def _format_ceiling(w: StationWeather) -> str:
    if not w.clouds:
        return "CLR"
    parts = []
    for c in w.clouds[:3]:
        cover = c.get("cover", "")
        base = c.get("base")
        if base is not None:
            parts.append(f"{cover}{base // 100:03d}")
        else:
            parts.append(cover)
    return " ".join(parts) if parts else "CLR"


def _flight_cat_warning(w: StationWeather) -> str | None:
    """Return an alert string if conditions are IFR or LIFR."""
    if w.flight_category in ("IFR", "LIFR"):
        details = []
        if w.visibility_sm is not None and w.visibility_sm < 3:
            details.append(f"visibility {w.visibility_sm}SM")
        if w.ceiling_ft is not None and w.ceiling_ft < 1000:
            details.append(f"ceiling {w.ceiling_ft}ft")
        detail_str = " — " + ", ".join(details) if details else ""
        return f"{w.station_id}: {w.flight_category} conditions{detail_str}"
    return None


def format_weather_section(
    metars: list[StationWeather],
    station_configs: list[StationConfig],
) -> str:
    """Render the Weather section as markdown."""
    if not metars:
        return "*Weather data unavailable.*"

    # Observation time from first METAR
    obs_time = ""
    if metars[0].observed_at:
        try:
            dt = datetime.fromisoformat(metars[0].observed_at.replace("Z", "+00:00"))
            obs_time = dt.strftime("%H%MZ %d %b %Y")
        except (ValueError, AttributeError):
            obs_time = metars[0].observed_at

    primary_ids = {s.id for s in station_configs if s.primary}

    # Order metars to match config order
    id_order = {s.id: i for i, s in enumerate(station_configs)}
    metars_sorted = sorted(metars, key=lambda m: id_order.get(m.station_id, 99))

    lines = []
    if obs_time:
        lines.append(f"*Conditions as of {obs_time}*")
        lines.append("")

    # Table
    lines.append("| Station | Temp | Wind | Vis | Ceiling | Category |")
    lines.append("|---------|------|------|-----|---------|----------|")

    for m in metars_sorted:
        label = f"{m.station_id} {m.name}"
        if m.station_id in primary_ids:
            label = f"**{label}**"
        temp = f"{m.temp_c}°C" if m.temp_c is not None else "N/A"
        lines.append(
            f"| {label} | {temp} | {_format_wind(m)} | "
            f"{_format_visibility(m)} | {_format_ceiling(m)} | {m.flight_category} |"
        )

    # Summary
    lines.append("")
    summary = _build_summary(metars_sorted, primary_ids)
    if summary:
        lines.append(f"**Summary:** {summary}")

    # Alerts
    alerts = []
    for m in metars_sorted:
        alert = _flight_cat_warning(m)
        if alert:
            alerts.append(alert)
    if alerts:
        lines.append("")
        lines.append("### Alerts")
        for a in alerts:
            lines.append(f"- {a}")

    return "\n".join(lines)


def _build_summary(metars: list[StationWeather], primary_ids: set[str]) -> str:
    """Generate a 1-2 sentence human-readable summary."""
    parts = []
    for m in metars:
        if m.station_id in primary_ids:
            if m.flight_category == "VFR":
                parts.append(f"VFR at {m.name}")
            elif m.flight_category == "MVFR":
                detail = ""
                if m.ceiling_ft:
                    detail = f" with ceiling at {m.ceiling_ft}ft"
                parts.append(f"Marginal VFR at {m.name}{detail}")
            elif m.flight_category in ("IFR", "LIFR"):
                parts.append(f"{m.flight_category} conditions at {m.name}")
            else:
                parts.append(f"{m.name}: {m.flight_category or 'unknown'}")

    ifr_stations = [m for m in metars if m.flight_category in ("IFR", "LIFR") and m.station_id not in primary_ids]
    if ifr_stations:
        names = ", ".join(m.name for m in ifr_stations)
        parts.append(f"IFR at {names}")

    vfr_others = [m for m in metars if m.flight_category == "VFR" and m.station_id not in primary_ids]
    if vfr_others and not ifr_stations:
        parts.append("all other stations VFR")

    return ". ".join(parts) + "." if parts else ""


async def fetch_tafs(config: WeatherConfig) -> list[dict]:
    """Fetch TAF data for configured stations."""
    ids = ",".join(s.id for s in config.stations)
    url = f"{config.api_base}/taf?ids={ids}&format=json"
    log.info("weather.fetching_tafs", url=url)
    async with httpx.AsyncClient(timeout=config.timeout) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


def _format_taf_period(period: dict, *, station_id: str = "") -> str:
    """Format a single TAF forecast period as a compact string.

    The aviationweather.gov TAF JSON returns ``visib`` with the same
    mixed-type surface as the METAR endpoint (int / float /
    numeric-string / ``"<N>+"``). The METAR boundary fix
    (``_parse_visibility``, see commit d2228d4 / project_kalle…
    follow-up dispatch) is reused here so the TAF formatter renders
    consistently regardless of API shape — previously the literal
    ``"6+"`` was special-cased but ``"10+"`` (or any other
    ``"<N>+"`` for N != 6) would have rendered as ``"10+SM"``.

    ``station_id`` is plumbed through for the ``weather.visibility_unparseable``
    operator-trace log; default empty string for external callers /
    tests that don't carry station context.
    """
    change = period.get("fcstChange") or "BASE"
    parts = [change]

    wspd = period.get("wspd")
    wgst = period.get("wgst")
    wdir = period.get("wdir")
    if wspd is not None:
        d = str(wdir).zfill(3) if wdir is not None else "VRB"
        wind = f"{d}/{wspd}G{wgst}kt" if wgst else f"{d}/{wspd}kt"
        parts.append(wind)

    raw_vis = period.get("visib")
    vis = _parse_visibility(raw_vis, station_id=station_id)
    if vis is not None:
        if vis >= 10:
            parts.append(">10SM")
        elif vis >= 6 and isinstance(raw_vis, str) and raw_vis.strip().endswith("+"):
            # Preserve the FAA "at-least" semantic for the rare ``"6+"``
            # case (the original code's only special-case). At >= 10
            # we already render ``>10SM`` regardless; in the 6..10
            # band the ``+`` suffix on the raw value is the operator-
            # meaningful signal so we surface ``>NSM`` rather than the
            # numeric value.
            parts.append(f">{int(vis)}SM")
        else:
            parts.append(f"{vis}SM")

    wx = period.get("wxString")
    if wx:
        parts.append(wx)

    clouds = period.get("clouds", [])
    for c in clouds[:2]:
        cover = c.get("cover", "")
        base = c.get("base")
        if base is not None:
            parts.append(f"{cover}{base // 100:03d}")

    return " ".join(parts)


def format_taf_section(tafs: list[dict], station_configs: list[StationConfig]) -> str:
    """Render TAF forecast section as markdown."""
    if not tafs:
        return ""

    name_map = {s.id: s.name for s in station_configs}
    id_order = {s.id: i for i, s in enumerate(station_configs)}
    tafs_sorted = sorted(tafs, key=lambda t: id_order.get(t.get("icaoId", ""), 99))

    lines = ["### Forecast (TAF)", ""]
    for taf in tafs_sorted:
        station_id = taf.get("icaoId", "")
        name = name_map.get(station_id, station_id)
        raw = taf.get("rawTAF", "")

        lines.append(f"**{station_id} {name}**")
        lines.append(f"```")
        lines.append(raw)
        lines.append(f"```")

        # Compact summary of key periods
        periods = taf.get("fcsts", [])
        sig_periods = []
        for p in periods:
            change = p.get("fcstChange") or "BASE"
            wx = p.get("wxString") or ""
            # Reuse the boundary parser so the gate sees a real
            # ``float | None`` regardless of API shape — replaces the
            # legacy ``isinstance(vis, (int, float)) and vis < 6``
            # defensive check that handled the no-crash case but
            # silently dropped string-shaped vis from the gate. The
            # contract: low-vis (< 6SM) is sig; "<N>+" for any N
            # never registers as low-vis (since N >= 6 by API
            # convention).
            parsed_vis = _parse_visibility(
                p.get("visib"), station_id=station_id,
            )
            # Highlight significant weather, low vis, or tempo/FM changes
            is_sig = (
                bool(wx)
                or (parsed_vis is not None and parsed_vis < 6)
                or change in ("FM", "BECMG")
            )
            if is_sig:
                sig_periods.append(_format_taf_period(p, station_id=station_id))

        if sig_periods:
            lines.append(f"Key changes: {' → '.join(sig_periods[:5])}")
        lines.append("")

    return "\n".join(lines)


async def fetch_and_format(config: WeatherConfig) -> str:
    """Top-level: fetch weather data and return formatted markdown section."""
    parts = []

    # METAR (current conditions)
    try:
        raw_metars = await fetch_metars(config)
        metars = [parse_metar(m, config.stations) for m in raw_metars]
        parts.append(format_weather_section(metars, config.stations))
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        log.error("weather.metar_fetch_failed", error=str(e))
        parts.append("*Current conditions unavailable — METAR request failed.*")

    # TAF (forecast)
    try:
        raw_tafs = await fetch_tafs(config)
        taf_section = format_taf_section(raw_tafs, config.stations)
        if taf_section:
            parts.append(taf_section)
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        log.error("weather.taf_fetch_failed", error=str(e))
        parts.append("*Forecast unavailable — TAF request failed.*")

    return "\n\n".join(parts)
