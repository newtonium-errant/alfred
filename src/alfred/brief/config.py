"""Morning Brief configuration — typed dataclasses loaded from config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from alfred.common.schedule import ScheduleConfig


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
class OutputConfig:
    directory: str = "run"
    name_template: str = "Morning Brief {date}"


@dataclass
class StateConfig:
    path: str = "./data/brief_state.json"


@dataclass
class UpcomingEventsConfig:
    """Phase 1 config for the Upcoming Events section.

    Intentionally minimal: enable/disable + a single forward window. Filter
    rules will grow inline in ``upcoming_events.py`` as real-data patterns
    reveal what counts as noise — do NOT add a rule registry here.
    """

    enabled: bool = True
    max_days_ahead: int = 30


@dataclass
class PeerDigestsConfig:
    """Config for the Peer Digests section (V.E.R.A. content arc receiver).

    The principal (Salem) renders one ``### {Peer} Update`` sub-section
    per expected peer. When a peer hasn't pushed today, the renderer
    falls back to the intentionally-left-blank line so the brief reader
    can tell at a glance which peers reported and which didn't.

    ``expected_peers`` is a list of short peer names (``["kal-le"]``).
    Operators add a peer here when they want its absence to surface in
    the brief; omitting a peer means it only appears when it actually
    pushes a digest.

    ``peer_canonical_names`` overrides the auto-generated upper-case
    section header (``"kal-le"`` → ``"KAL-LE"``). Useful for peers
    whose canonical name doesn't follow the simple convention (e.g.
    ``"stay-c"`` → ``"STAY-C"`` works, but ``"kalle"`` → ``"K.A.L.L.E."``
    needs the override).
    """

    enabled: bool = True
    expected_peers: list[str] = field(default_factory=list)
    peer_canonical_names: dict[str, str] = field(default_factory=dict)


@dataclass
class BriefConfig:
    vault_path: str = ""
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    state: StateConfig = field(default_factory=StateConfig)
    upcoming_events: UpcomingEventsConfig = field(default_factory=UpcomingEventsConfig)
    peer_digests: PeerDigestsConfig = field(default_factory=PeerDigestsConfig)
    log_file: str = "./data/brief.log"

    # Telegram user_id the post-write brief push dispatches to. v1
    # single-user: first entry of ``telegram.allowed_users``. ``None``
    # when no telegram section is configured — the push is skipped
    # silently in that case.
    primary_telegram_user_id: int | None = None


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
        # Brief is daily-only; day_of_week stays None.
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

    # Upcoming Events — Phase 1: just enable/disable + window. Defaults
    # match the dataclass defaults so omitting the block "just works".
    ue_raw = section.get("upcoming_events", {}) or {}
    upcoming_events = UpcomingEventsConfig(
        enabled=ue_raw.get("enabled", True),
        max_days_ahead=int(ue_raw.get("max_days_ahead", 30)),
    )

    # Peer Digests — V.E.R.A. content-arc receiver. Defaults to enabled
    # with no expected peers, which means the section only renders when
    # a peer actually pushed a digest. Operators populate
    # ``expected_peers`` when they want a missing peer's absence to
    # surface as the intentionally-left-blank line.
    pd_raw = section.get("peer_digests", {}) or {}
    expected_peers_raw = pd_raw.get("expected_peers") or []
    expected_peers: list[str] = [
        str(p) for p in expected_peers_raw if isinstance(p, str)
    ]
    canonical_names_raw = pd_raw.get("peer_canonical_names") or {}
    peer_canonical_names: dict[str, str] = {
        str(k): str(v) for k, v in canonical_names_raw.items()
        if isinstance(k, str) and isinstance(v, str)
    }
    peer_digests = PeerDigestsConfig(
        enabled=bool(pd_raw.get("enabled", True)),
        expected_peers=expected_peers,
        peer_canonical_names=peer_canonical_names,
    )

    # Primary Telegram user for post-write brief push. Reads the
    # unified config's ``telegram.allowed_users[0]`` — single-user v1;
    # peer protocol in Stage 3.5 will widen.
    telegram_raw = raw.get("telegram", {}) or {}
    allowed = telegram_raw.get("allowed_users") or []
    primary_user: int | None = None
    if allowed:
        try:
            primary_user = int(allowed[0])
        except (TypeError, ValueError):
            primary_user = None

    return BriefConfig(
        vault_path=vault_path,
        schedule=schedule,
        weather=weather,
        output=output,
        state=state,
        upcoming_events=upcoming_events,
        peer_digests=peer_digests,
        log_file=f"{log_dir}/brief.log",
        primary_telegram_user_id=primary_user,
    )
