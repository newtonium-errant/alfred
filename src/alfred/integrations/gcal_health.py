"""GCal health check — registered with the BIT aggregator.

Mirrors the brief / janitor / distiller / daily_sync / instructor
``last-successful-*`` daemon-liveness pattern but for the Google
Calendar OAuth token rather than a daemon state file.

**Source-of-truth signal:** ``data/secrets/gcal_token.json``'s mtime.
The Google OAuth library re-writes this file via tmp+rename every time
the cached ``access_token`` is refreshed (see
``GCalClient._save_credentials`` in ``gcal.py``). A healthy install
sees the file touched roughly every ~hour (access tokens expire at
that cadence). When the ``refresh_token`` is revoked / expired /
rotated, no refresh happens, no rewrite happens, and the mtime ages
unbounded.

Status mapping:

* **OK**   — token mtime < 24h ago. Active refreshes are happening
  (or have happened recently); auth is alive.
* **WARN** — 24h ≤ mtime age < 72h. Could be a quiet calendar day
  (no sync attempts → no forced refresh) OR an early auth issue.
* **FAIL** — mtime age ≥ 72h. Three days without a single refresh on
  a normally-active calendar means the refresh_token almost certainly
  needs re-authorisation. Run ``alfred gcal authorize``.
* **SKIP** — ``gcal.enabled=false`` OR the section is absent OR the
  token file doesn't exist (fresh install — operator hasn't run the
  ``authorize`` flow yet, which is expected).

**Last-error suffix:** when the timestamp threshold flags WARN/FAIL,
the detail string gets a ``; last error: <error_code>`` suffix if any
``gcal.sync_*_failed`` log line landed within the last 24h. Mirrors
the brief/instructor probes' last-error contract, but the source is
log-scanning rather than a state-file field — gcal has no per-daemon
state file. Capped at 150 chars (same convention).

Per ``feedback_intentionally_left_blank.md``: the failure case here is
EXACTLY the 2026-05-07 → 2026-05-15 silent-auth incident — token
expired May 7, Andrew didn't notice for 9 days because the only
operator-visible signal was Salem's narration after a calendar-edit
attempt, which only fires when Andrew makes a calendar change. This
probe disambiguates "no sync happening because no calendar activity"
from "no sync happening because auth is broken" at day 3 instead of
day 9.

Per ``feedback_intentionally_left_blank.md`` for the probe itself:
every status path emits an explicit detail string so the operator
distinguishes "skipped because disabled" from "skipped because no
token yet" from "ok, X hours since last refresh."
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alfred.health.aggregator import register_check
from alfred.health.types import CheckResult, Status, ToolHealth


# Thresholds for the mtime-based status mapping. Documented in module
# docstring above; isolated here as constants so a future tuning pass
# is a one-line edit.
#
# Why 24h WARN / 72h FAIL: the Google OAuth library refreshes the
# access_token every ~hour while in use. On an idle-but-healthy
# calendar (Andrew didn't make a change today) the file still gets
# touched whenever Salem's transport reads the calendar for the daily
# brief (06:00 ADT). So a healthy mtime is always within 24h. A 24-72h
# gap is ambiguous (legitimately quiet weekend, or early auth failure
# — operator should look). > 72h is almost certainly auth-broken on
# any normally-active install.
_GCAL_WARN_SECONDS = 24 * 60 * 60       # 24 hours
_GCAL_FAIL_SECONDS = 72 * 60 * 60       # 72 hours

# Window for the last-error log scan. Anything older than 24h is
# unlikely to be the cause of "auth is currently broken" — could be a
# transient that's already resolved.
_LAST_ERROR_WINDOW_SECONDS = 24 * 60 * 60

# Number of log-file tail bytes to scan for recent ``gcal.sync_*_failed``
# events. Capped so a large rotated log doesn't slow the probe. 256KB
# at ~150 bytes/line ≈ 1700 lines — comfortably more than 24h of
# normal-cadence gcal events.
_LOG_TAIL_BYTES = 256 * 1024

# Same 150-char cap as brief/instructor for the last-error suffix.
_LAST_ERROR_CAP = 150

# ANSI escape-sequence stripper. The dev/console structlog renderer
# writes ANSI-coloured logs to file (e.g. ``\x1b[2m...\x1b[0m``), so
# regex-matching needs the colours stripped first. Pulled out so the
# pattern is unit-testable in isolation.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Regex for a single gcal failure log line. Captures:
#   group 1: ISO timestamp (e.g. 2026-05-15T14:22:11.123456Z)
#   group 2: full event name (e.g. gcal.sync_create_failed)
#   group 3: optional error_code value (single quoted, double quoted,
#            or bare word). Empty if absent.
#
# Matches the structlog ConsoleRenderer output shape after ANSI strip:
#   2026-05-15T14:22:11.123456Z [warning ] gcal.sync_create_failed
#       correlation_id= error=... error_code=auth_failed
_GCAL_FAILURE_LINE_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)"
    r".*?(?P<event>gcal\.sync_\w*failed\w*)"
    r"(?:.*?error_code=(?P<code>'[^']*'|\"[^\"]*\"|\S+))?"
)


def _resolve_token_path(raw: dict[str, Any]) -> Path:
    """Mirror ``GCalConfig.token_path`` resolution so the probe consults
    the same file the adapter writes.

    Resolution order (matches ``load_from_unified``):
      1. ``gcal.token_path`` if explicitly set
      2. dataclass default ``~/alfred/data/secrets/gcal_token.json``

    The ``~`` is expanded so a tilde-prefixed path resolves to the
    operator's home directory; ``GCalClient`` does the same expansion
    internally, so the probe's file inspection lines up with whatever
    the live adapter would actually open.
    """
    gcal_raw = (raw.get("gcal", {}) or {})
    explicit = gcal_raw.get("token_path", "")
    if not explicit:
        explicit = "~/alfred/data/secrets/gcal_token.json"
    return Path(os.path.expanduser(str(explicit)))


def _resolve_log_paths(raw: dict[str, Any]) -> list[Path]:
    """Return the candidate log files to scan for recent gcal failures.

    GCal sync activity is logged from two daemons:
      * ``data/talker.log`` — talker daemon (gcal_sync invocations
        triggered by user vault edits / hook fan-out)
      * ``data/alfred.log`` — the umbrella log written by the
        orchestrator + several daemons; gcal events fan out here too
        (the orchestrator pipes the curator/janitor/distiller stdout
        into this file).

    Both candidates are checked; missing files are silently skipped.
    Returns paths in priority order — caller picks the most recent
    failure across all.
    """
    log_dir = (raw.get("logging", {}) or {}).get("dir", "./data")
    log_dir_path = Path(log_dir)
    return [
        log_dir_path / "talker.log",
        log_dir_path / "alfred.log",
    ]


def _read_token_mtime(token_path: Path) -> float | None:
    """Return the token file's mtime as a Unix timestamp, or None if
    the file doesn't exist or can't be stat'd.

    Defensive: any OSError (permission denied, race on rotation,
    fs-level glitch) degrades to None so the probe surfaces SKIP
    rather than crashing the whole BIT run on a bad stat.
    """
    if not token_path.is_file():
        return None
    try:
        return token_path.stat().st_mtime
    except OSError:
        return None


def _scan_recent_failures(
    log_paths: list[Path],
    window_seconds: int = _LAST_ERROR_WINDOW_SECONDS,
    now_ts: float | None = None,
    tail_bytes: int = _LOG_TAIL_BYTES,
) -> dict[str, Any] | None:
    """Scan the tail of each log file for ``gcal.sync_*_failed`` events
    within the last ``window_seconds``. Return the most recent match's
    ``{"ts": iso_string, "event": event_name, "error_code": str}`` or
    None when no qualifying line is found.

    Implementation notes:
      * Reads only the last ``tail_bytes`` of each file (defaults to
        256KB) so a multi-megabyte log doesn't add probe latency.
      * ANSI escape sequences are stripped before regex-matching since
        the dev/console structlog renderer writes coloured output.
      * Multiple matches across files are sorted by timestamp; the
        most-recent wins. Ties (same timestamp) take the first match
        order — irrelevant for the operator-facing detail line.

    The ``now_ts`` / ``tail_bytes`` parameters are injection points
    for tests — production callers leave both as defaults.
    """
    if now_ts is None:
        now_ts = datetime.now(timezone.utc).timestamp()
    cutoff_ts = now_ts - window_seconds

    matches: list[tuple[float, str, str, str]] = []  # (ts_epoch, iso, event, code)
    for log_path in log_paths:
        if not log_path.is_file():
            continue
        try:
            size = log_path.stat().st_size
            with open(log_path, "rb") as f:
                if size > tail_bytes:
                    f.seek(size - tail_bytes)
                    # Discard a partial line at the start of the tail —
                    # we may have landed mid-line. Reading and ignoring
                    # one line aligns the rest of the scan to record
                    # boundaries.
                    f.readline()
                body_bytes = f.read()
        except OSError:
            continue

        # Decode tolerantly — log files are utf-8 by convention but
        # truncation at a multi-byte boundary shouldn't crash the probe.
        body = body_bytes.decode("utf-8", errors="replace")
        body = _ANSI_ESCAPE_RE.sub("", body)

        for line in body.splitlines():
            m = _GCAL_FAILURE_LINE_RE.search(line)
            if not m:
                continue
            ts_str = m.group("ts")
            event = m.group("event")
            raw_code = m.group("code") or ""
            # Strip surrounding quotes if the matcher captured them.
            if raw_code.startswith(("'", '"')) and raw_code.endswith(raw_code[0]):
                raw_code = raw_code[1:-1]
            # Parse timestamp; Z-suffix needs to map to +00:00 for fromisoformat.
            try:
                ts_norm = ts_str.replace("Z", "+00:00") if ts_str.endswith("Z") else ts_str
                ts_dt = datetime.fromisoformat(ts_norm)
            except ValueError:
                continue
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.replace(tzinfo=timezone.utc)
            ts_epoch = ts_dt.timestamp()
            if ts_epoch < cutoff_ts:
                continue
            matches.append((ts_epoch, ts_str, event, raw_code))

    if not matches:
        return None

    matches.sort(key=lambda t: t[0], reverse=True)
    _ts_epoch, iso, event, code = matches[0]
    return {"ts": iso, "event": event, "error_code": code}


def _humanise_age(seconds: float) -> str:
    """Compact human-friendly age string. Single-unit, matches the
    instructor probe's helper. Buckets: s / m / h / d.
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _format_age_hours(seconds: float) -> str:
    """One-decimal hours string for detail lines (e.g. ``"36.5h"``).

    Distinct from ``_humanise_age`` because the GCal mtime detail line
    benefits from sub-day precision: "last gcal sync 0.3h ago" carries
    more info than "0h ago." Buckets <1m get the bare-seconds
    treatment so the operator sees fresh-installs clearly.
    """
    seconds = max(0.0, float(seconds))
    if seconds < 60.0:
        return f"{int(seconds)}s"
    hours = seconds / 3600.0
    return f"{hours:.1f}h"


def _check_last_successful_gcal_sync(raw: dict[str, Any]) -> CheckResult:
    """Validate that the GCal OAuth token has been refreshed recently.

    See module docstring for full status-mapping rationale.
    """
    gcal_raw = raw.get("gcal", {}) or {}
    enabled = bool(gcal_raw.get("enabled", False))

    token_path = _resolve_token_path(raw)
    payload: dict[str, Any] = {
        "token_path": str(token_path),
        "enabled": enabled,
    }

    if not enabled:
        return CheckResult(
            name="last-successful-gcal-sync",
            status=Status.SKIP,
            detail="gcal disabled in config",
            data=payload,
        )

    mtime = _read_token_mtime(token_path)
    if mtime is None:
        # Fresh install / operator hasn't run ``alfred gcal authorize``
        # yet. Distinguished from "disabled" via the detail string so
        # the operator sees the right next step.
        return CheckResult(
            name="last-successful-gcal-sync",
            status=Status.SKIP,
            detail=f"no token file yet: {token_path}",
            data={**payload, "exists": False},
        )

    now_dt = datetime.now(timezone.utc)
    mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
    age_seconds = (now_dt - mtime_dt).total_seconds()

    # ISO 8601 with Z-suffix for parity with structlog TimeStamper(fmt="iso").
    mtime_iso = mtime_dt.isoformat().replace("+00:00", "Z")
    payload["last_refreshed"] = mtime_iso
    payload["age_seconds"] = int(age_seconds)

    # Last-error suffix lookup — only consulted when we're about to
    # emit WARN/FAIL. The OK path keeps the detail clean since "auth
    # was refreshed within the last hour" is a strong-enough green
    # signal that a stale error is just noise.
    log_paths = _resolve_log_paths(raw)
    last_error = _scan_recent_failures(log_paths)
    if last_error is not None:
        payload["last_error"] = last_error
        code = last_error.get("error_code", "") or ""
        if isinstance(code, str) and len(code) > _LAST_ERROR_CAP:
            code = code[: _LAST_ERROR_CAP - 3] + "..."
        error_suffix = f"; last error: {code}" if code else ""
    else:
        error_suffix = ""

    age_h = _format_age_hours(age_seconds)

    if age_seconds < _GCAL_WARN_SECONDS:
        return CheckResult(
            name="last-successful-gcal-sync",
            status=Status.OK,
            detail=f"last gcal sync {age_h} ago (token last refreshed {mtime_iso})",
            data=payload,
        )
    if age_seconds < _GCAL_FAIL_SECONDS:
        return CheckResult(
            name="last-successful-gcal-sync",
            status=Status.WARN,
            detail=(
                f"last gcal sync {age_h} ago (token may be stale){error_suffix}"
            ),
            data=payload,
        )
    return CheckResult(
        name="last-successful-gcal-sync",
        status=Status.FAIL,
        detail=(
            f"last gcal sync {age_h} ago (auth likely revoked — "
            f"run alfred gcal authorize){error_suffix}"
        ),
        data=payload,
    )


async def health_check(raw: dict[str, Any], mode: str = "quick") -> ToolHealth:
    """Run gcal health checks.

    Returns SKIP at the tool level when the ``gcal:`` section is absent
    so the BIT output cleanly distinguishes "not configured" from
    "disabled by flag." Both surface as SKIP at the tool level but the
    detail differs (and the result list differs — the disabled case
    runs the probe to emit the SKIP detail line).
    """
    if raw.get("gcal") is None:
        return ToolHealth(
            tool="gcal",
            status=Status.SKIP,
            detail="no gcal section in config",
        )

    result = _check_last_successful_gcal_sync(raw)
    return ToolHealth(tool="gcal", status=result.status, results=[result])


register_check("gcal", health_check)
