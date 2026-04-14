"""Morning Brief configuration — typed dataclasses loaded from config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class StationConfig:
    id: str
    name: str
    primary: bool = False


@dataclass
class WeatherConfig:
    stations: list[StationConfig] = field(default_factory=list)
    api_base: str = "https://aviationweather.gov/api/data"
    timeout: int = 30


@dataclass
class ScheduleConfig:
    time: str = "06:00"
    timezone: str = "America/Halifax"


@dataclass
class OutputConfig:
    directory: str = "run"
    name_template: str = "Morning Brief {date}"


@dataclass
class StateConfig:
    path: str = "./data/brief_state.json"


@dataclass
class BriefConfig:
    vault_path: str = ""
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    state: StateConfig = field(default_factory=StateConfig)
    log_file: str = "./data/brief.log"


def load_from_unified(raw: dict[str, Any]) -> BriefConfig:
    """Build BriefConfig from the unified config dict."""
    section = raw.get("brief", {})
    vault_path = raw.get("vault", {}).get("path", "./vault")
    log_dir = raw.get("logging", {}).get("dir", "./data")

    # Parse stations
    stations = []
    for s in section.get("weather", {}).get("stations", []):
        stations.append(StationConfig(
            id=s.get("id", ""),
            name=s.get("name", ""),
            primary=s.get("primary", False),
        ))

    weather_raw = section.get("weather", {})
    weather = WeatherConfig(
        stations=stations,
        api_base=weather_raw.get("api_base", "https://aviationweather.gov/api/data"),
        timeout=weather_raw.get("timeout", 30),
    )

    schedule_raw = section.get("schedule", {})
    schedule = ScheduleConfig(
        time=schedule_raw.get("time", "06:00"),
        timezone=schedule_raw.get("timezone", "America/Halifax"),
    )

    output_raw = section.get("output", {})
    output = OutputConfig(
        directory=output_raw.get("directory", "run"),
        name_template=output_raw.get("name_template", "Morning Brief {date}"),
    )

    state_raw = section.get("state", {})
    state = StateConfig(
        path=state_raw.get("path", f"{log_dir}/brief_state.json"),
    )

    return BriefConfig(
        vault_path=vault_path,
        schedule=schedule,
        weather=weather,
        output=output,
        state=state,
        log_file=f"{log_dir}/brief.log",
    )
