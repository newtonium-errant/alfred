"""GCal health check — active-call probe (queue #8 rework, 2026-05-17).

Mirrors the brief / janitor / distiller / daily_sync / instructor
``last-successful-*`` daemon-liveness pattern but for the Google
Calendar OAuth token. Active-call rework: probe makes a lightweight
authenticated API call (``calendarList().list(maxResults=1)``) and
maps the outcome to status. Prior mtime-only logic produced
false-positive FAIL on idle days when token was still valid but
hadn't been refreshed because no API activity touched it.

**Pre-rework problem (mtime-only):** when Andrew has no event
activity for >24h (vacation, weekend, just no calendar changes),
the token file's mtime stays old even though the refresh token is
still valid. The probe couldn't distinguish "idle (no event
activity)" from "broken (auth failed)" — operator saw FAIL on a
healthy system. Per CLAUDE.md universal
``feedback_intentionally_left_blank.md``: silence-from-no-activity
must surface DIFFERENTLY from silence-from-failure.

**Post-rework probe:** active call verifies auth state regardless
of activity history.

Status mapping:

* **OK**   — API call succeeded. Token works. mtime info appended
  to the detail string as supporting context (e.g. "token last
  refreshed 0.3h ago") but isn't the gate.
* **WARN** — API call raised a network / transient error
  (connection refused, DNS failure, timeout, generic transport).
  Couldn't verify auth; flag for operator awareness. Last-error
  suffix from log scan still applied.
* **FAIL** — API call raised auth-specific failure: ``RefreshError``
  (refresh token revoked / expired / invalid) OR ``HttpError`` with
  status 401 (unauthorized) / 403 (forbidden). Operator must run
  ``alfred gcal authorize``.
* **SKIP** — ``gcal.enabled=false`` OR the section is absent OR the
  token file doesn't exist (fresh install — operator hasn't run the
  ``authorize`` flow yet, which is expected) OR the ``google-*``
  libraries aren't installed (degraded-but-functional install).

**Last-error suffix:** when the probe lands WARN/FAIL, the detail
string gets a ``; last error: <error_code>`` suffix if any
``gcal.sync_*_failed`` log line landed within the last 24h. Mirrors
the brief/instructor probes' last-error contract; source is
log-scanning since gcal has no per-daemon state file. Capped at
150 chars.

Per ``feedback_intentionally_left_blank.md`` for the probe itself:
every status path emits an explicit detail string so the operator
distinguishes "skipped because disabled" from "skipped because no
token yet" from "skipped because libs not installed" from "ok, API
call succeeded" from "fail, auth revoked" from "warn, transient
network".

Per CLAUDE.md "Subprocess Failure Logging" / equivalent: errors
captured for the detail string use ``str(exc)[:200]`` truncation so
a verbose Google API error doesn't blow up the BIT detail line. Full
exception info still surfaces in the result.data dict for diagnostic
deep-dive.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alfred.health.aggregator import register_check
from alfred.health.types import CheckResult, Status, ToolHealth


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

# Truncation cap for the active-call error message embedded in the
# detail string. Google API errors can run multiple kilobytes of
# JSON; 200 chars is enough to identify the failure class without
# blowing up the BIT detail line.
_EXC_DETAIL_CAP = 200

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


def _build_calendar_service(token_path: Path) -> Any:
    """Build a GCal v3 ``service`` object using the cached credentials.

    Mirrors ``GCalClient._service_obj`` shape (per
    ``feedback_sdk_quirk_centralization.md`` — keep import-time + build-
    time SDK quirks consistent across call sites) but lighter — no
    refresh-on-load round-trip. The probe wants to know whether the
    saved credentials work AS-IS at call time; if the access token is
    expired, the API call itself triggers a refresh via google-auth's
    auto-refresh middleware. If the refresh fails, the API call
    raises ``RefreshError`` — exactly the signal we want for the FAIL
    path.

    Raises ``ImportError`` (re-raised as ``GoogleLibsNotInstalled``)
    when the google-* libraries aren't available. Caller maps this to
    SKIP.
    """
    # Lazy import (matches the pattern in gcal.py) — keeps gcal_health
    # importable on installs that didn't pull in the optional gcal
    # extras.
    from google.oauth2.credentials import Credentials  # type: ignore
    from googleapiclient.discovery import build  # type: ignore

    # Same scopes the adapter uses. ``Credentials.from_authorized_user_file``
    # tolerates the scopes arg being None — we pass it for parity but
    # the loaded credentials carry the scopes that were minted at
    # authorize-time anyway.
    creds = Credentials.from_authorized_user_file(str(token_path), None)
    # cache_discovery=False suppresses the file-cache deprecation
    # warning. discovery fetched fresh per probe call — acceptable
    # latency cost for the diagnostic value.
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


class _GoogleLibsNotInstalled(Exception):
    """Internal sentinel: ``google-*`` libs aren't importable.

    Distinct from ``alfred.integrations.gcal.GCalNotInstalled`` to
    avoid cross-module import cycles at health-probe load time.
    """


def _classify_active_call_outcome(
    exc: BaseException,
) -> tuple[Status, str]:
    """Map an active-call exception to (status, error_class_tag).

    The ``error_class_tag`` is a short stable identifier embedded in
    the detail string so operators can grep for specific failure
    classes across BIT runs.

    Mapping rules:

      * ``RefreshError`` (google-auth) → FAIL / ``refresh_failed``
        The cached refresh token is no longer accepted. Operator must
        re-authorize.

      * ``HttpError`` with status 401 / 403 (googleapiclient) → FAIL /
        ``http_<code>``. Auth-rejected at the API layer.

      * ``HttpError`` with any other status (5xx, 429, etc.) → WARN /
        ``http_<code>``. Could be transient quota / Google
        outage / API change — operator should retry, not re-auth.

      * Anything else (network errors, SSL handshake, generic
        Exception) → WARN / ``transient``. Worth flagging but not
        worth burning the operator's OAuth flow on.

    Imports are lazy / defensive so the classifier still works when
    the google-* libs aren't present (in which case the outer caller
    short-circuits to SKIP before this is reached, but the defensive
    isinstance checks remain).
    """
    # RefreshError — terminal auth failure. The token's refresh_token
    # is revoked / expired / invalid. Operator must re-authorize.
    try:
        from google.auth.exceptions import RefreshError  # type: ignore
        if isinstance(exc, RefreshError):
            return Status.FAIL, "refresh_failed"
    except ImportError:
        # google-auth not importable — fall through. Probably handled
        # at the SKIP branch upstream; defensive here.
        pass

    # HttpError — distinguish auth-rejected (401/403) from transient
    # / quota (other status codes).
    try:
        from googleapiclient.errors import HttpError  # type: ignore
        if isinstance(exc, HttpError):
            # HttpError exposes the HTTP status code via ``resp.status``
            # on its underlying httplib2 response. Be defensive — the
            # attribute path has shifted across googleapiclient versions.
            status_code = 0
            resp = getattr(exc, "resp", None)
            if resp is not None:
                status_code = int(getattr(resp, "status", 0) or 0)
            if status_code in (401, 403):
                return Status.FAIL, f"http_{status_code}"
            # Any other HttpError status (5xx server error, 429 quota,
            # 4xx client error other than auth) is transient/retry-
            # worthy from the operator's perspective — don't push them
            # toward re-authorize.
            return Status.WARN, f"http_{status_code or 'unknown'}"
    except ImportError:
        pass

    # Anything else — generic transient: network failure, DNS, TLS
    # handshake, socket timeout, json decode error, etc. Flag the
    # operator but don't claim auth is broken.
    return Status.WARN, "transient"


def _check_last_successful_gcal_sync(raw: dict[str, Any]) -> CheckResult:
    """Active-call probe: verify GCal auth state by making a lightweight
    API call.

    See module docstring for the full status-mapping rationale.
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

    # Build mtime context for the detail string. The active-call result
    # is the gate; mtime is supporting context only.
    now_dt = datetime.now(timezone.utc)
    mtime_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)
    age_seconds = (now_dt - mtime_dt).total_seconds()
    mtime_iso = mtime_dt.isoformat().replace("+00:00", "Z")
    payload["last_refreshed"] = mtime_iso
    payload["age_seconds"] = int(age_seconds)
    age_h = _format_age_hours(age_seconds)

    # Try to build the service object — this can raise ImportError
    # when the google-* libs aren't installed. Map that to SKIP so a
    # base install (no gcal extras) doesn't surface a noisy FAIL.
    try:
        service = _build_calendar_service(token_path)
    except ImportError as exc:
        return CheckResult(
            name="last-successful-gcal-sync",
            status=Status.SKIP,
            detail=(
                f"google-api libraries not installed; "
                f"pip install alfred-vault[gcal] to enable "
                f"(import error: {str(exc)[:80]})"
            ),
            data={**payload, "libs_installed": False},
        )
    except Exception as exc:  # noqa: BLE001
        # Credentials.from_authorized_user_file can raise on a
        # corrupted token file (json decode error). Map to FAIL with a
        # clean operator-actionable message — the token file exists but
        # is unreadable, just like the adapter's GCalNotAuthorized path.
        msg = str(exc)[:_EXC_DETAIL_CAP]
        return CheckResult(
            name="last-successful-gcal-sync",
            status=Status.FAIL,
            detail=(
                f"token file unreadable (run alfred gcal authorize): "
                f"{msg}"
            ),
            data={
                **payload,
                "error_class": "token_load_failed",
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
            },
        )

    # Active call — lightweight: list one calendarList entry. Doesn't
    # depend on a specific calendar_id being configured; just verifies
    # the credentials can authenticate to GCal at all.
    call_error: BaseException | None = None
    try:
        service.calendarList().list(maxResults=1).execute()
    except BaseException as exc:  # noqa: BLE001
        # BaseException to also catch the rare SystemExit-shaped errors
        # some HTTP transports raise on TLS failures. The classifier
        # below maps to WARN by default for anything-not-RefreshError-
        # or-HttpError, so worst case is a noisy WARN — never a crash.
        call_error = exc

    # Last-error suffix lookup — only consulted when we're about to
    # emit WARN/FAIL. The OK path keeps the detail clean since a
    # successful active call is a strong-enough green signal that a
    # stale log error is noise.
    log_paths = _resolve_log_paths(raw)
    last_error = _scan_recent_failures(log_paths)

    if call_error is None:
        # OK — active call succeeded. Token works regardless of mtime.
        # Detail includes the mtime as supporting context per
        # "intentionally left blank" — operator sees "active probe OK"
        # AND "last refresh was N hours ago" so a quietly-stale token
        # is visible even when working.
        return CheckResult(
            name="last-successful-gcal-sync",
            status=Status.OK,
            detail=(
                f"active probe ok; token last refreshed {age_h} ago "
                f"({mtime_iso})"
            ),
            data={**payload, "active_probe": "ok"},
        )

    # Active call failed. Classify the exception.
    status, error_class = _classify_active_call_outcome(call_error)
    error_msg = str(call_error)[:_EXC_DETAIL_CAP]
    exception_type = type(call_error).__name__

    payload["active_probe"] = "failed"
    payload["error_class"] = error_class
    payload["exception_type"] = exception_type
    payload["exception_message"] = str(call_error)

    if last_error is not None:
        payload["last_error"] = last_error
        code = last_error.get("error_code", "") or ""
        if isinstance(code, str) and len(code) > _LAST_ERROR_CAP:
            code = code[: _LAST_ERROR_CAP - 3] + "..."
        error_suffix = f"; last error: {code}" if code else ""
    else:
        error_suffix = ""

    # FAIL detail — auth-broken framing, actionable next step.
    if status == Status.FAIL:
        return CheckResult(
            name="last-successful-gcal-sync",
            status=Status.FAIL,
            detail=(
                f"active probe failed ({error_class}; "
                f"run alfred gcal authorize): {error_msg}{error_suffix}"
            ),
            data=payload,
        )

    # WARN detail — couldn't-verify framing, operator awareness only.
    return CheckResult(
        name="last-successful-gcal-sync",
        status=Status.WARN,
        detail=(
            f"active probe could not verify ({error_class}; "
            f"token last refreshed {age_h} ago): {error_msg}{error_suffix}"
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
