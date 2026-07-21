"""Morning Brief configuration — typed dataclasses loaded from config.yaml."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

from alfred.common.schedule import ScheduleConfig

from .utils import get_logger

log = get_logger(__name__)


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
class StaycBugRelayConfig:
    """Config for the STAY-C Bug Relay section (task #21 — downstream of #4).

    Salem's brief reads the ``stayc_bug_watcher`` relay spool and renders one
    PHI-free count line. STAY-C uses NO Telegram, so only the count may cross
    into the (Telegram-transiting) brief — never bug bodies.

    ``enabled`` defaults OFF: only the Salem instance that lives alongside a
    STAY-C deployment has a spool to read; KAL-LE / Hypatia have none, and a
    section that reads a nonexistent spool would render a permanent "no data"
    line. Opt in per-instance.

    ``spool_path`` has NO baked-in default on purpose — it is a
    deployment-specific absolute path (the box's
    ``/data/algernon/alfred/data/stayc_bugs_relay.md`` differs from a dev
    checkout), so a shared-code default would be wrong on every other
    machine (per-instance-defaults rule). Enabled-but-unset renders an
    explicit "not configured" line rather than guessing.

    ``staleness_hours`` (default 25) — a ``generated_at`` older than this
    marks the relay stale (the watcher fires on any bug-dir change plus the
    operator's daily cadence; >25h with no write means it likely stopped).
    25h, not 24h, tolerates a slightly-late daily tick without false-stale.
    """

    enabled: bool = False
    spool_path: str = ""
    staleness_hours: float = 25.0


@dataclass
class StaycRetentionRelayConfig:
    """Config for the STAY-C Retention Review Relay section (task #13 §4 / C3).

    Salem's brief reads the retention sweep's PHI-free review spool and renders one line: the
    ``review_due`` count + the oldest over-window (OPAQUE) encounter_id. Same discipline as the bug
    relay — STAY-C uses NO Telegram, so only the PHI-free count/id may cross into the
    (Telegram-transiting) brief, never encounter labels/bodies.

    ``enabled`` defaults OFF (Salem-only, opt-in — only the Salem instance beside a STAY-C deployment
    has a spool). ``spool_path`` has NO baked-in default (a deployment-specific absolute path;
    enabled-but-unset renders 'not configured'). ``staleness_hours`` (default 25) — the sweep writes
    every ~30s on the box, but the box→Salem sync is the cadence bottleneck, so 25h tolerates a daily
    sync without false-stale (mirrors the bug relay)."""

    enabled: bool = False
    spool_path: str = ""
    staleness_hours: float = 25.0


@dataclass
class WatchItemConfig:
    """One ``brief.watches`` entry — a config-driven upstream check.

    The morning brief runs these live (the way the weather section
    does) and renders one line per item under ``## Watch Items``.
    GENERIC by design: what gets watched is operator config, never
    code. Two types:

    * ``github_pr`` — ``repo`` (``owner/name``) + ``number``. Reports
      open / merged / closed; a state CHANGE since the last brief is a
      flip (rendered loud with ``on_flip_note``).
    * ``github_release_mention`` — ``repo`` + ``pattern`` (regex,
      case-insensitive) + ``baseline_tag``. Fires when the first
      release strictly newer than the baseline / last-seen tag matches
      the pattern across tag + name + body.

    ``id`` keys the persisted last-seen state
    (``data/brief_watches_state.json``); keep it stable once set.
    ``on_flip_note`` is the operator's action text, rendered when the
    watch flips.
    """

    id: str = ""
    label: str = ""
    type: str = ""
    repo: str = ""
    number: int = 0
    pattern: str = ""
    baseline_tag: str = ""
    on_flip_note: str = ""
    # Multi-host support: per-item forge base URL (default GitHub) and an
    # OPTIONAL per-item auth token. A private Forgejo repo 403/404s
    # unauthenticated; public GitHub needs neither. ``token`` is the
    # resolved value (the loader reads ``token_env`` from the environment
    # when set, else falls back to a literal ``token``); ``token_env``
    # is retained only for diagnostics. repr=False on the resolved token —
    # it's a credential.
    api_base: str = "https://api.github.com"
    token: str = field(default="", repr=False)
    token_env: str = ""

    def state_key(self) -> str:
        """Stable key for the persisted watch state. Explicit ``id`` wins.

        The id-less fallback is TYPE-SPECIFIC (watch-module review nit
        a1, 2026-06-11): the original ``type:repo:number`` fallback
        omitted ``pattern``, so two id-less release watches on the SAME
        repo shared one state entry — the first match latched BOTH and
        the second pattern could never fire. Release fallbacks now
        include a short pattern hash; PR fallbacks keep the number
        (which already disambiguates them). Set an explicit ``id``
        anyway — the loader warns when one is missing.
        """
        if self.id:
            return self.id
        if self.type == "github_release_mention":
            pattern_hash = hashlib.sha1(
                self.pattern.encode("utf-8")
            ).hexdigest()[:8]
            return f"{self.type}:{self.repo}:{pattern_hash}"
        return f"{self.type}:{self.repo}:{self.number}"


@dataclass
class BriefConfig:
    vault_path: str = ""
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    weather: WeatherConfig = field(default_factory=WeatherConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    state: StateConfig = field(default_factory=StateConfig)
    upcoming_events: UpcomingEventsConfig = field(default_factory=UpcomingEventsConfig)
    peer_digests: PeerDigestsConfig = field(default_factory=PeerDigestsConfig)
    # STAY-C Bug Relay — optional; disabled by default (Salem-only, opt-in).
    # See StaycBugRelayConfig.
    stayc_bug_relay: StaycBugRelayConfig = field(default_factory=StaycBugRelayConfig)
    # STAY-C Retention Review Relay — optional; disabled by default (Salem-only, opt-in).
    # See StaycRetentionRelayConfig (§4 morning-review surface, C3).
    stayc_retention_relay: StaycRetentionRelayConfig = field(
        default_factory=StaycRetentionRelayConfig)
    # Watch Items — optional; empty list = feature off, section never
    # rendered. See WatchItemConfig.
    watches: list[WatchItemConfig] = field(default_factory=list)
    log_file: str = "./data/brief.log"

    # Telegram user_id the post-write brief push dispatches to. v1
    # single-user: first entry of ``telegram.allowed_users``. ``None``
    # when no telegram section is configured — the push is skipped
    # silently in that case.
    primary_telegram_user_id: int | None = None

    # c6 spam-quarantine surface wiring (2026-05-31 followup to 164839a).
    # The brief's operations section reads quarantined records from
    # ``<vault>/<quarantine_dir_name>/spam/<YYYY-MM>/``. Mirrors the
    # ``email_classifier.quarantine_dir_name`` field on the classifier
    # side; brief.load_from_unified pulls the value from the
    # email_classifier YAML block so a per-instance override on the
    # classifier side surfaces in the operator brief without manual
    # double-config. Default ``"quarantine"`` matches
    # ``EmailClassifierConfig.quarantine_dir_name`` default; instances
    # that don't run the email_classifier still get the same default
    # (the brief's operations.py emits "Spam quarantine: empty" when
    # the directory doesn't exist, which is the correct surface).
    quarantine_dir_name: str = "quarantine"

    # Q3 Option A (2026-06-26): global tier-window defaults. Sourced from
    # the ``routine.tier_defaults`` YAML block (NOT a brief-section key)
    # so the brief's 06:00 tier view applies the SAME defaults the
    # routine aggregator's 05:59 pass does — the two must agree or the
    # render disagrees with the persisted handoff. Stored as the routine
    # package's ``TierDefaultsConfig``; default all-None (no defaults →
    # opt-out semantics unchanged). Mirrors the cross-section read
    # precedent of ``quarantine_dir_name`` (from email_classifier).
    tier_defaults: Any = None


def load_from_unified(raw: dict[str, Any]) -> BriefConfig:
    """Build BriefConfig from the unified config dict."""
    section = raw.get("brief", {})
    vault_path = raw.get("vault", {}).get("path", "./vault")
    # Cross-section read: the tier-window defaults live under
    # ``routine.tier_defaults`` (routine-domain config) but the brief
    # render needs them too. Shared parse via the routine package's
    # ``TierDefaultsConfig.from_raw`` so both layers build them
    # identically. Lazy import keeps the brief→routine dependency at
    # call time (not module load).
    from alfred.routine.config import TierDefaultsConfig
    tier_defaults = TierDefaultsConfig.from_raw(
        (raw.get("routine") or {}).get("tier_defaults"),
    )
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

    # STAY-C Bug Relay — Salem-only, opt-in. Absent block = disabled (the
    # dataclass default), so the section never renders on instances without
    # a STAY-C deployment. ``spool_path`` has no baked-in default (it is a
    # deployment-specific absolute path); enabled-but-unset surfaces as an
    # explicit "not configured" line at render time.
    sbr_raw = section.get("stayc_bug_relay", {}) or {}
    try:
        staleness_hours = float(sbr_raw.get("staleness_hours", 25.0))
    except (TypeError, ValueError):
        staleness_hours = 25.0
    stayc_bug_relay = StaycBugRelayConfig(
        enabled=bool(sbr_raw.get("enabled", False)),
        spool_path=str(sbr_raw.get("spool_path", "") or ""),
        staleness_hours=staleness_hours,
    )
    # STAY-C Retention Review Relay (§4 / C3) — same shape/discipline as the bug relay.
    srr_raw = section.get("stayc_retention_relay", {}) or {}
    try:
        srr_staleness = float(srr_raw.get("staleness_hours", 25.0))
    except (TypeError, ValueError):
        srr_staleness = 25.0
    stayc_retention_relay = StaycRetentionRelayConfig(
        enabled=bool(srr_raw.get("enabled", False)),
        spool_path=str(srr_raw.get("spool_path", "") or ""),
        staleness_hours=srr_staleness,
    )

    # Watch Items — optional list; absent block = feature off. Lenient
    # build (str/int coercion, non-dict entries skipped): a structurally
    # malformed ITEM still constructs and is surfaced at check time as a
    # "watch unavailable (config error / api error)" line in the brief —
    # a config mistake the operator actually SEES, rather than a
    # load-time skip that silently shrinks the watch list.
    watches: list[WatchItemConfig] = []
    for w in section.get("watches", []) or []:
        if not isinstance(w, dict):
            # A YAML typo (stray string / list entry) must be VISIBLE —
            # silently shrinking the watch list hides the mistake until
            # the operator notices a watch never fired (review nit a2).
            log.warning(
                "brief.watch_entry_invalid",
                raw=str(w)[:80],
                reason=f"entry_type_{type(w).__name__}",
            )
            continue
        try:
            number = int(w.get("number", 0) or 0)
        except (TypeError, ValueError):
            number = 0
        # Resolve the optional per-item auth token: ``token_env`` (read
        # from the environment at load time) wins over a literal
        # ``token``. Left empty for public GitHub (the default). A private
        # Forgejo repo needs one or its fetches 403/404.
        token_env = str(w.get("token_env", "") or "")
        if token_env:
            resolved_token = os.environ.get(token_env, "") or ""
        else:
            resolved_token = str(w.get("token", "") or "")
        watches.append(WatchItemConfig(
            id=str(w.get("id", "") or ""),
            label=str(w.get("label", "") or ""),
            type=str(w.get("type", "") or ""),
            repo=str(w.get("repo", "") or ""),
            number=number,
            pattern=str(w.get("pattern", "") or ""),
            baseline_tag=str(w.get("baseline_tag", "") or ""),
            on_flip_note=str(w.get("on_flip_note", "") or ""),
            api_base=str(w.get("api_base", "") or "https://api.github.com"),
            token=resolved_token,
            token_env=token_env,
        ))

    # Resolved-key hygiene (review nit a1): an empty ``id`` means the
    # type-specific fallback key is in use (works, but brittle across
    # config edits — warn so the operator adds one); a DUPLICATE
    # resolved key means two watches would share one state entry (the
    # first match latches both — almost certainly a config mistake).
    seen_keys: set[str] = set()
    for item in watches:
        if not item.id:
            log.warning(
                "brief.watch_missing_id",
                label=item.label or "(unlabeled)",
                fallback_key=item.state_key(),
            )
        key = item.state_key()
        if key in seen_keys:
            log.warning(
                "brief.watch_duplicate_state_key",
                key=key,
                label=item.label or "(unlabeled)",
                detail="two watches share one state entry — give each a unique id",
            )
        seen_keys.add(key)

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

    # c6 spam-quarantine wiring (2026-05-31 followup to 164839a). Mirror
    # the classifier's ``email_classifier.quarantine_dir_name`` onto the
    # brief side so per-instance overrides flow through to the operator
    # surface. Reads ONLY the brief-relevant field — the brief doesn't
    # need anything else from the email_classifier block, so a full
    # ``email_classifier.config.load_from_unified`` would be over-coupling.
    # Default ``"quarantine"`` matches the classifier default so
    # instances that omit the email_classifier block load unchanged.
    ec_raw = raw.get("email_classifier", {}) or {}
    quarantine_dir_name = str(
        ec_raw.get("quarantine_dir_name", "quarantine")
    )

    return BriefConfig(
        vault_path=vault_path,
        schedule=schedule,
        weather=weather,
        output=output,
        state=state,
        upcoming_events=upcoming_events,
        peer_digests=peer_digests,
        stayc_bug_relay=stayc_bug_relay,
        stayc_retention_relay=stayc_retention_relay,
        watches=watches,
        log_file=f"{log_dir}/brief.log",
        primary_telegram_user_id=primary_user,
        quarantine_dir_name=quarantine_dir_name,
        tier_defaults=tier_defaults,
    )
