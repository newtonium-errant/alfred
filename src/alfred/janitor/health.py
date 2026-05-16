"""Janitor health check — registered with the BIT aggregator.

Janitor probes are deliberately cheap: vault writability, configured
sweep directories, state file readability, backend known, Anthropic
auth, and last-successful-sweep daemon-liveness. The runtime cost of
a full janitor sweep is irrelevant to this health check — we're only
checking that the preconditions to start a sweep are satisfied AND
that the daemon's loop has actually produced a sweep recently.

The ``last-successful-sweep`` probe was added 2026-05-10 as part of
the cross-daemon BIT-probe arc; per
``feedback_intentionally_left_blank.md`` silence is ambiguous between
healthy-quiet and broken — the probe disambiguates by consulting
the daemon's existing state file.
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


# Stale-threshold calibrations for the last-successful-sweep probe.
# Janitor's default cadence is daily-ish (deep sweep schedule plus
# event-driven shallow passes). 30h OK / 30-48h WARN / >48h FAIL
# mirrors the dispatch's per-daemon calibration table.
_JANITOR_STALE_OK_HOURS = 30
_JANITOR_STALE_FAIL_HOURS = 48


def _check_vault(raw: dict[str, Any]) -> CheckResult:
    """Verify vault path exists and is writable."""
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
    """Verify the janitor state file is readable JSON (if it exists).

    A missing state file is OK — it just means the janitor hasn't
    swept yet.  A corrupt one is WARN, not FAIL, because the janitor
    auto-resets on load errors.
    """
    state_raw = (raw.get("janitor", {}) or {}).get("state", {}) or {}
    state_path = Path(state_raw.get("path", "./data/janitor_state.json"))
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


def _check_backend(raw: dict[str, Any]) -> CheckResult:
    """Static backend check."""
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


def _resolve_janitor_state_path(raw: dict[str, Any]) -> Path:
    """Resolve the janitor's state-file path the same way
    ``alfred.janitor.config.load_from_unified`` does — explicit path
    wins, otherwise the dataclass default ``./data/janitor_state.json``.
    """
    state_section = (raw.get("janitor", {}) or {}).get("state", {}) or {}
    explicit = state_section.get("path", "")
    if explicit:
        return Path(explicit)
    return Path("./data/janitor_state.json")


def _read_janitor_most_recent_sweep(state_path: Path) -> str | None:
    """Read janitor state and return the max ``sweeps[*].timestamp``
    plus the top-level ``last_deep_sweep`` (whichever is newer).

    Returns None if the file is missing, unparseable, or the sweep
    history + last_deep_sweep are all empty/missing. Inline dict-walk
    rather than constructing ``alfred.janitor.state.JanitorState`` —
    matches the precedent set by yesterday's brief health probe.

    The "any sweep" signal is what we want for daemon-liveness: a
    shallow sweep that ran 6h ago means the daemon's loop is
    cycling. The deep-sweep signal alone would FAIL between deep
    sweeps even on a perfectly healthy daemon.
    """
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    candidates: list[str] = []

    sweeps = data.get("sweeps", {})
    if isinstance(sweeps, dict):
        for sw in sweeps.values():
            if not isinstance(sw, dict):
                continue
            ts = sw.get("timestamp", "")
            if isinstance(ts, str) and ts:
                candidates.append(ts)

    last_deep = data.get("last_deep_sweep")
    if isinstance(last_deep, str) and last_deep:
        candidates.append(last_deep)

    if not candidates:
        return None
    return max(candidates)


def _read_last_error(state_path: Path) -> dict | None:
    """Read janitor state and return the ``last_error`` payload
    (shape: ``{"ts": iso_string, "message": str}``) or None when
    absent / unreadable / corrupted-shape.

    Same defensive-read posture as ``_read_janitor_most_recent_sweep``
    — a corrupt state file degrades silently to None so the probe
    still runs the timestamp threshold check rather than crashing.
    Mirrors ``brief.health._read_last_error`` (2026-05-14).
    """
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    err = data.get("last_error")
    if not isinstance(err, dict):
        return None
    msg = err.get("message")
    if not isinstance(msg, str) or not msg:
        return None
    return err


def _check_last_successful_sweep(raw: dict[str, Any]) -> CheckResult:
    """Validate that the janitor daemon's loop has produced a sweep
    recently.

    Status mapping:
      * SKIP if state file missing (fresh install) / no sweeps
        recorded / unparseable timestamp
      * OK   if most-recent-sweep <= 30h ago
      * WARN if most-recent-sweep 30h..48h ago (one missed run —
        could be a transient API blip)
      * FAIL if most-recent-sweep > 48h ago (multi-day silent failure)

    "Now" is computed in UTC because janitor's sweep timestamps are
    written in UTC (see ``SweepResult`` and ``state.update_file``
    both use ``datetime.now(timezone.utc)``). No timezone config to
    consult.

    Per ``feedback_intentionally_left_blank.md``: silence (janitor
    daemon idle, no scan/fix log activity) is ambiguous between
    healthy-quiet and broken; the probe disambiguates.

    WARN/FAIL detail gets a ``; last error: <msg>`` suffix when
    ``state.last_error`` is populated (capped 150 chars) so the BIT
    line carries WHY the sweep stalled. OK detail stays clean —
    last_error is wiped on success. Added 2026-05-14 (per
    ``project_cross_daemon_swallow_audit.md``).
    """
    state_path = _resolve_janitor_state_path(raw)
    most_recent_iso = _read_janitor_most_recent_sweep(state_path)

    if most_recent_iso is None:
        if not state_path.is_file():
            return CheckResult(
                name="last-successful-sweep",
                status=Status.SKIP,
                detail=f"no state file (fresh install): {state_path}",
                data={"state_path": str(state_path), "exists": False},
            )
        return CheckResult(
            name="last-successful-sweep",
            status=Status.SKIP,
            detail="no sweeps recorded yet",
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
            name="last-successful-sweep",
            status=Status.SKIP,
            detail=f"unparseable timestamp in state: {most_recent_iso!r}",
            data={"state_path": str(state_path)},
        )

    now = datetime.now(timezone.utc)
    elapsed = now - most_recent
    elapsed_hours = elapsed.total_seconds() / 3600.0
    payload: dict[str, Any] = {
        "state_path": str(state_path),
        "last_sweep": most_recent_iso,
        "elapsed_hours": round(elapsed_hours, 2),
    }

    # Build the WARN/FAIL error suffix. Capped at 150 chars so the BIT
    # line stays a single readable row. Full structured error always
    # rides in ``result.data["last_error"]`` for JSON consumers
    # regardless of the cap. OK status path skips the suffix because
    # last_error is wiped on success — a stale entry surviving past a
    # successful sweep would only happen if an operator hand-edited
    # the state file, which the defensive _read_last_error has
    # already filtered for.
    last_error = _read_last_error(state_path)
    if last_error is not None:
        message = last_error.get("message", "")
        if isinstance(message, str) and len(message) > 150:
            message = message[:147] + "..."
        payload["last_error"] = last_error
        error_suffix = f"; last error: {message}" if message else ""
    else:
        error_suffix = ""

    if elapsed < timedelta(hours=_JANITOR_STALE_OK_HOURS):
        return CheckResult(
            name="last-successful-sweep",
            status=Status.OK,
            detail=f"last sweep {round(elapsed_hours, 1)}h ago",
            data=payload,
        )
    if elapsed < timedelta(hours=_JANITOR_STALE_FAIL_HOURS):
        return CheckResult(
            name="last-successful-sweep",
            status=Status.WARN,
            detail=f"last sweep {round(elapsed_hours, 1)}h ago (one missed run){error_suffix}",
            data=payload,
        )
    return CheckResult(
        name="last-successful-sweep",
        status=Status.FAIL,
        detail=f"last sweep {round(elapsed_hours, 1)}h ago (daemon may be silently failing){error_suffix}",
        data=payload,
    )


async def health_check(raw: dict[str, Any], mode: str = "quick") -> ToolHealth:
    """Run janitor health checks.

    Returns SKIP at the tool level when the ``janitor:`` config section
    is absent — the orchestrator gates janitor daemon startup on
    ``"janitor" in raw`` (see ``orchestrator.py``'s tool-add loop), so
    the probe mirrors that pattern. Without this gate, instances that
    don't run janitor (e.g. KAL-LE) surface a stale
    ``last-successful-sweep`` FAIL because the probe consults an
    absent state file and sees an ageing dataclass default path. Per
    ``feedback_intentionally_left_blank.md``: SKIP-with-detail
    distinguishes "not configured for this instance" from "configured
    but broken."
    """
    if raw.get("janitor") is None:
        return ToolHealth(
            tool="janitor",
            status=Status.SKIP,
            detail="no janitor section in config",
        )

    results: list[CheckResult] = [
        _check_vault(raw),
        _check_state_file(raw),
        _check_backend(raw),
    ]

    backend = (raw.get("agent", {}) or {}).get("backend", "claude")
    if backend == "claude":
        api_key = resolve_api_key(raw)
        results.append(await check_anthropic_auth(api_key))

    results.append(_check_last_successful_sweep(raw))

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="janitor", status=status, results=results)


register_check("janitor", health_check)
