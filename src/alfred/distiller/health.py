"""Distiller health check — registered with the BIT aggregator.

Probes:
  * vault path writable
  * distiller state file readable (if present)
  * candidate_threshold is in the expected (0.0 .. 1.0) range — a
    misconfigured threshold is a silent footgun; the daemon won't
    refuse to start but it will produce nonsense.
  * backend known
  * anthropic auth (when backend == claude)
  * last-successful-extraction — daemon liveness validation per the
    universal "intentionally left blank" / observability discipline
    (added 2026-05-10 as part of the cross-daemon BIT-probe arc).
    Distiller's expected interval is hourly (default
    ``extraction.interval_seconds = 3600``); thresholds reflect that.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alfred.health.aggregator import register_check
from alfred.health.anthropic_auth import check_anthropic_auth, resolve_api_key
from alfred.health.types import CheckResult, Status, ToolHealth


_KNOWN_BACKENDS = ("claude", "zo", "openclaw")


# Stale-threshold calibrations for the last-successful-extraction probe.
#
# Recalibrated 2026-05-10 after smoke-test FAIL on healthy daemon: the
# previous 90min / 240min values were premised on the false assumption
# that distiller extracts hourly. It does NOT — the hourly
# ``extraction.interval_seconds = 3600`` is just the loop tick that
# re-checks whether the deep-extraction window is due. The only path
# that writes to ``state.runs[*]`` / ``state.last_deep_extraction`` is
# the daily deep extraction at 03:30 ADT (06:30 UTC) per
# ``extraction.deep_extraction_schedule``. Hourly light scans don't
# touch state.
#
# Recalibrated thresholds mirror janitor's daily-sweep shape (30h OK /
# 48h FAIL) since both daemons fire-and-record once per day. One full
# grace cycle before WARN; multi-day silence before FAIL.
#
# Out of scope here (deferred follow-ups): a separate
# ``last-successful-light-scan`` probe would catch a loop-died-mid-day
# failure mode the current probe wouldn't surface for 30+ hours;
# requires the hourly tick to write to state, which it currently
# doesn't.
_DISTILLER_STALE_OK_HOURS = 30
_DISTILLER_STALE_FAIL_HOURS = 48


def _check_vault(raw: dict[str, Any]) -> CheckResult:
    vault_path_str = (raw.get("vault", {}) or {}).get("path", "") or ""
    if not vault_path_str:
        return CheckResult(
            name="vault-path",
            status=Status.FAIL,
            detail="vault.path is empty",
        )
    vault_path = Path(vault_path_str)
    if not vault_path.exists():
        return CheckResult(
            name="vault-path",
            status=Status.FAIL,
            detail=f"vault path does not exist: {vault_path}",
        )
    if not os.access(vault_path, os.W_OK):
        return CheckResult(
            name="vault-path",
            status=Status.FAIL,
            detail=f"vault path not writable: {vault_path}",
        )
    return CheckResult(
        name="vault-path",
        status=Status.OK,
        detail=str(vault_path),
        data={"path": str(vault_path)},
    )


def _check_state_file(raw: dict[str, Any]) -> CheckResult:
    state_raw = (raw.get("distiller", {}) or {}).get("state", {}) or {}
    state_path = Path(state_raw.get("path", "./data/distiller_state.json"))
    if not state_path.exists():
        return CheckResult(
            name="state-file",
            status=Status.OK,
            detail="no state file yet (fresh install)",
            data={"path": str(state_path)},
        )
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult(
            name="state-file",
            status=Status.WARN,
            detail=f"state file unreadable: {exc.__class__.__name__}",
            data={"path": str(state_path)},
        )
    return CheckResult(
        name="state-file",
        status=Status.OK,
        detail=str(state_path),
        data={"path": str(state_path)},
    )


def _check_threshold(raw: dict[str, Any]) -> CheckResult:
    """Static sanity check on the candidate_threshold value.

    A threshold outside [0, 1] is almost certainly a typo — the
    candidate scorer produces values in that range. We warn rather
    than fail so a user experimenting with aggressive filtering can
    override the check if they really mean it.
    """
    ext_raw = (raw.get("distiller", {}) or {}).get("extraction", {}) or {}
    threshold = ext_raw.get("candidate_threshold", 0.3)
    try:
        value = float(threshold)
    except (TypeError, ValueError):
        return CheckResult(
            name="candidate-threshold",
            status=Status.FAIL,
            detail=f"candidate_threshold is not a number: {threshold!r}",
        )
    if not 0.0 <= value <= 1.0:
        return CheckResult(
            name="candidate-threshold",
            status=Status.WARN,
            detail=f"candidate_threshold out of [0, 1]: {value}",
            data={"value": value},
        )
    return CheckResult(
        name="candidate-threshold",
        status=Status.OK,
        detail=f"{value}",
        data={"value": value},
    )


def _check_backend(raw: dict[str, Any]) -> CheckResult:
    backend = (raw.get("agent", {}) or {}).get("backend", "claude")
    if backend in _KNOWN_BACKENDS:
        return CheckResult(
            name="backend",
            status=Status.OK,
            detail=f"backend={backend}",
            data={"backend": backend},
        )
    return CheckResult(
        name="backend",
        status=Status.WARN,
        detail=f"unknown backend '{backend}'",
        data={"backend": backend},
    )


def _resolve_distiller_state_path(raw: dict[str, Any]) -> Path:
    """Resolve distiller's state-file path the same way
    ``alfred.distiller.config.load_from_unified`` does — explicit
    path wins, otherwise dataclass default
    ``./data/distiller_state.json``.
    """
    state_section = (raw.get("distiller", {}) or {}).get("state", {}) or {}
    explicit = state_section.get("path", "")
    if explicit:
        return Path(explicit)
    return Path("./data/distiller_state.json")


def _read_distiller_most_recent_run(state_path: Path) -> str | None:
    """Read distiller state and return the max ``runs[*].timestamp``
    plus the top-level ``last_deep_extraction`` (whichever is newer).

    Returns None on missing / unparseable / no signals. Inline dict-
    walk per the precedent — a corrupt state file degrades to SKIP
    rather than crashing the BIT run.

    Same shape as janitor's ``_read_janitor_most_recent_sweep`` —
    runs and deep-extraction marker are informationally distinct;
    both contribute to the "any successful activity" daemon-liveness
    signal.
    """
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    candidates: list[str] = []

    runs = data.get("runs", {})
    if isinstance(runs, dict):
        for run in runs.values():
            if not isinstance(run, dict):
                continue
            ts = run.get("timestamp", "")
            if isinstance(ts, str) and ts:
                candidates.append(ts)

    last_deep = data.get("last_deep_extraction")
    if isinstance(last_deep, str) and last_deep:
        candidates.append(last_deep)

    if not candidates:
        return None
    return max(candidates)


def _check_last_successful_extraction(raw: dict[str, Any]) -> CheckResult:
    """Validate that the distiller daemon has run an extraction recently.

    Distiller's deep extraction (the only path that writes to
    ``state.runs[*]`` / ``state.last_deep_extraction``) fires once per
    day per ``extraction.deep_extraction_schedule`` (default 03:30 ADT
    / 06:30 UTC). The hourly ``extraction.interval_seconds = 3600`` is
    just the loop tick that re-checks whether the deep-extraction
    window is due — light scans don't touch state. Thresholds reflect
    the daily cadence:

      * SKIP if state file missing (fresh install) / no runs recorded /
        unparseable
      * OK   if most-recent-run <= 30h ago
      * WARN if most-recent-run 30h..48h ago (one missed daily run)
      * FAIL if most-recent-run > 48h ago (multi-day silent failure)

    Mirrors janitor's daily-sweep thresholds — both daemons fire-and-
    record once per day.

    "Now" is computed in UTC because distiller's RunResult.timestamp
    is written in UTC. No timezone config to consult.

    Per ``feedback_intentionally_left_blank.md``: silence (distiller
    daemon idle, no extraction log activity) is ambiguous between
    healthy-quiet and broken; the probe disambiguates.
    """
    state_path = _resolve_distiller_state_path(raw)
    most_recent_iso = _read_distiller_most_recent_run(state_path)

    if most_recent_iso is None:
        if not state_path.is_file():
            return CheckResult(
                name="last-successful-extraction",
                status=Status.SKIP,
                detail=f"no state file (fresh install): {state_path}",
                data={"state_path": str(state_path), "exists": False},
            )
        return CheckResult(
            name="last-successful-extraction",
            status=Status.SKIP,
            detail="no extraction runs recorded yet",
            data={"state_path": str(state_path), "exists": True},
        )

    try:
        normalized = (
            most_recent_iso.replace("Z", "+00:00")
            if most_recent_iso.endswith("Z") else most_recent_iso
        )
        most_recent = datetime.fromisoformat(normalized)
        if most_recent.tzinfo is None:
            most_recent = most_recent.replace(tzinfo=timezone.utc)
    except ValueError:
        return CheckResult(
            name="last-successful-extraction",
            status=Status.SKIP,
            detail=f"unparseable timestamp in state: {most_recent_iso!r}",
            data={"state_path": str(state_path)},
        )

    now = datetime.now(timezone.utc)
    elapsed = now - most_recent
    elapsed_hours = elapsed.total_seconds() / 3600.0
    payload: dict[str, Any] = {
        "state_path": str(state_path),
        "last_extraction": most_recent_iso,
        "elapsed_hours": round(elapsed_hours, 2),
    }

    if elapsed < timedelta(hours=_DISTILLER_STALE_OK_HOURS):
        return CheckResult(
            name="last-successful-extraction",
            status=Status.OK,
            detail=f"last extraction {round(elapsed_hours, 1)}h ago",
            data=payload,
        )
    if elapsed < timedelta(hours=_DISTILLER_STALE_FAIL_HOURS):
        return CheckResult(
            name="last-successful-extraction",
            status=Status.WARN,
            detail=f"last extraction {round(elapsed_hours, 1)}h ago (one missed run)",
            data=payload,
        )
    return CheckResult(
        name="last-successful-extraction",
        status=Status.FAIL,
        detail=f"last extraction {round(elapsed_hours, 1)}h ago (daemon may be silently failing)",
        data=payload,
    )


async def health_check(raw: dict[str, Any], mode: str = "quick") -> ToolHealth:
    """Run distiller health checks."""
    results: list[CheckResult] = [
        _check_vault(raw),
        _check_state_file(raw),
        _check_threshold(raw),
        _check_backend(raw),
    ]

    backend = (raw.get("agent", {}) or {}).get("backend", "claude")
    if backend == "claude":
        api_key = resolve_api_key(raw)
        results.append(await check_anthropic_auth(api_key))

    results.append(_check_last_successful_extraction(raw))

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="distiller", status=status, results=results)


register_check("distiller", health_check)
