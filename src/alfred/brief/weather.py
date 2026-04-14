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
        visibility_sm=raw.get("visib"),
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


def _format_taf_period(period: dict) -> str:
    """Format a single TAF forecast period as a compact string."""
    change = period.get("fcstChange") or "BASE"
    parts = [change]

    wspd = period.get("wspd")
    wgst = period.get("wgst")
    wdir = period.get("wdir")
    if wspd is not None:
        d = str(wdir).zfill(3) if wdir is not None else "VRB"
        wind = f"{d}/{wspd}G{wgst}kt" if wgst else f"{d}/{wspd}kt"
        parts.append(wind)

    vis = period.get("visib")
    if vis is not None:
        parts.append(f"{vis}SM" if vis != "6+" else ">6SM")

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
            vis = p.get("visib")
            # Highlight significant weather, low vis, or tempo/FM changes
            is_sig = (
                wx
                or (vis is not None and vis not in ("6+", None) and (isinstance(vis, (int, float)) and vis < 6))
                or change in ("FM", "BECMG")
            )
            if is_sig:
                sig_periods.append(_format_taf_period(p))

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
