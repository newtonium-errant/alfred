"""Instructor health check — registered with the BIT aggregator.

Four probe tiers:

* **Static** — config section present, state file path writable
  (parent dir exists or can be created).
* **Local** — skills_dir contains ``vault-instructor/SKILL.md``.
* **Functional** — pending queue length < 20 (stuck-queue
  heuristic), no records have hit ``max_retries``.
* **Liveness** — last-successful-poll probe (added 2026-05-14):
  WARN if ``state.last_run_ts`` is > 1h ago, FAIL if > 4h ago, SKIP
  on fresh install (no state file). Surfaces the cross-daemon
  silent-failure class catalogued in
  ``project_cross_daemon_swallow_audit.md`` — the daemon's outer
  ``except Exception:`` at daemon.py:331 swallows poll-loop
  failures, and without this probe the operator only notices when
  pending directives stop being processed. Closes the same
  diagnostic gap brief / janitor / distiller / daily_sync closed
  earlier today.

Like every other tool's health module, registration fires at import
time so the aggregator picks it up automatically once
``KNOWN_TOOL_MODULES`` is updated.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alfred.health.aggregator import register_check
from alfred.health.types import CheckResult, Status, ToolHealth


# Heuristic threshold for "stuck queue" — more than this many pending
# instructions across the whole vault and something is wrong (the
# daemon is down, the directives are malformed, or the model is
# failing every call). Operator needs to look.
_STUCK_QUEUE_THRESHOLD = 20

# Liveness probe thresholds for ``last-successful-poll``. Instructor's
# default poll_interval is 60s (instructor/config.py:163), so:
#   * 1h = 60 polls behind — already abnormal but could be transient
#     (operator paused the daemon for a config edit). WARN.
#   * 4h = 240 polls behind — multi-hour silent failure, the same
#     daemon-loop swallow class catalogued in
#     ``project_cross_daemon_swallow_audit.md``. FAIL.
# These are conservative compared to brief (1d/2d) because instructor
# polls at minute-cadence, not daily — a healthy steady-state
# last_run_ts is ALWAYS within the last few minutes.
_POLL_WARN_SECONDS = 60 * 60       # 1 hour
_POLL_FAIL_SECONDS = 4 * 60 * 60   # 4 hours


def _check_config_present(raw: dict[str, Any]) -> CheckResult:
    """The instructor auto-starts only when ``instructor:`` is in config.

    If the section is absent, the whole probe returns SKIP at the
    health_check entry point. This helper is only reached when the
    section exists; we use it to surface the fact.
    """
    return CheckResult(
        name="config-section",
        status=Status.OK,
        detail="instructor section present",
    )


def _check_state_path_writable(raw: dict[str, Any]) -> CheckResult:
    """Confirm the state file (or its parent dir) is writable."""
    state_raw = (raw.get("instructor", {}) or {}).get("state", {}) or {}
    state_path = Path(state_raw.get("path", "./data/instructor_state.json"))
    parent = state_path.parent

    # Parent must exist OR be creatable.
    if not parent.exists():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return CheckResult(
                name="state-path",
                status=Status.FAIL,
                detail=f"cannot create {parent}: {exc}",
            )

    if not os.access(parent, os.W_OK):
        return CheckResult(
            name="state-path",
            status=Status.FAIL,
            detail=f"parent not writable: {parent}",
            data={"path": str(state_path)},
        )

    # If the file exists but isn't valid JSON, WARN — the daemon's
    # load() heals it on next save, but the operator probably wants
    # to know.
    if state_path.exists():
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            return CheckResult(
                name="state-path",
                status=Status.WARN,
                detail=f"state file unreadable: {exc.__class__.__name__}",
                data={"path": str(state_path)},
            )

    return CheckResult(
        name="state-path",
        status=Status.OK,
        detail=str(state_path),
        data={"path": str(state_path)},
    )


def _check_skill_file() -> CheckResult:
    """The executor raises FileNotFoundError if SKILL.md is missing.

    Probe this statically so operators see it in ``alfred check``
    without waiting for the first directive to fire.
    """
    try:
        from alfred._data import get_skills_dir
    except ImportError as exc:
        return CheckResult(
            name="skill-file",
            status=Status.FAIL,
            detail=f"cannot import alfred._data: {exc}",
        )
    skill_path = get_skills_dir() / "vault-instructor" / "SKILL.md"
    if not skill_path.is_file():
        return CheckResult(
            name="skill-file",
            status=Status.FAIL,
            detail=f"missing: {skill_path}",
            data={"path": str(skill_path)},
        )
    return CheckResult(
        name="skill-file",
        status=Status.OK,
        detail=str(skill_path),
        data={"path": str(skill_path)},
    )


def _check_queue_health(raw: dict[str, Any]) -> list[CheckResult]:
    """Two functional probes: pending-queue length + stuck-retry detection.

    Walks the vault's ``*.md`` files parsing frontmatter, counts
    entries in ``alfred_instructions``, and flags when:

    - total pending entries > ``_STUCK_QUEUE_THRESHOLD`` (WARN)
    - any record's retry count is at or above ``max_retries`` (WARN
      — the executor would have surfaced to ``alfred_instructions_error``
      by now, but a stale state file might still carry the counter;
      it's an operator signal)
    """
    results: list[CheckResult] = []

    vault_path_str = (raw.get("vault", {}) or {}).get("path", "") or ""
    if not vault_path_str or not Path(vault_path_str).exists():
        # The state-path probe above will catch vault issues; don't
        # double-FAIL here.
        results.append(CheckResult(
            name="pending-queue",
            status=Status.SKIP,
            detail="vault path not available",
        ))
        return results

    vault_path = Path(vault_path_str)
    import frontmatter  # base dep

    total_pending = 0
    for md in vault_path.rglob("*.md"):
        try:
            post = frontmatter.load(str(md))
        except Exception:  # noqa: BLE001 — tolerate any parse error
            continue
        pending = post.metadata.get("alfred_instructions") or []
        if isinstance(pending, str):
            pending = [pending]
        if isinstance(pending, list):
            total_pending += len(pending)

    if total_pending > _STUCK_QUEUE_THRESHOLD:
        status = Status.WARN
        detail = (
            f"pending queue length = {total_pending} "
            f"(threshold {_STUCK_QUEUE_THRESHOLD}) — the daemon may "
            f"be stuck or down"
        )
    else:
        status = Status.OK
        detail = f"pending queue length = {total_pending}"
    results.append(CheckResult(
        name="pending-queue",
        status=status,
        detail=detail,
        data={"pending": total_pending, "threshold": _STUCK_QUEUE_THRESHOLD},
    ))

    # Retry-at-max heuristic via the state file.
    instructor_raw = raw.get("instructor", {}) or {}
    max_retries = int(instructor_raw.get("max_retries", 3))
    state_raw = instructor_raw.get("state", {}) or {}
    state_path = Path(state_raw.get("path", "./data/instructor_state.json"))
    stuck: list[str] = []
    if state_path.exists():
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state_data = json.load(f)
            for rel, count in (state_data.get("retry_counts", {}) or {}).items():
                if int(count) >= max_retries:
                    stuck.append(rel)
        except (OSError, json.JSONDecodeError):
            pass

    if stuck:
        results.append(CheckResult(
            name="retry-at-max",
            status=Status.WARN,
            detail=f"{len(stuck)} record(s) at max_retries={max_retries}",
            data={"paths": sorted(stuck)[:10]},
        ))
    else:
        results.append(CheckResult(
            name="retry-at-max",
            status=Status.OK,
            detail=f"no records at max_retries={max_retries}",
        ))

    return results


def _resolve_state_path(raw: dict[str, Any]) -> Path:
    """Mirror :class:`alfred.instructor.config.InstructorConfig`'s
    state-path resolution so the probe consults the same file the
    daemon writes.

    Resolution order (matches the loader):
      1. ``instructor.state.path`` if explicitly set
      2. dataclass default ``./data/instructor_state.json``
    """
    state_raw = (raw.get("instructor", {}) or {}).get("state", {}) or {}
    explicit = state_raw.get("path", "")
    if explicit:
        return Path(explicit)
    return Path("./data/instructor_state.json")


def _read_last_run_ts(state_path: Path) -> str | None:
    """Read the instructor state file and return its top-level
    ``last_run_ts`` ISO timestamp string. Returns None on missing
    file / missing field / unparseable JSON / non-string value.

    Inline dict-walk rather than constructing
    :class:`InstructorState` — the probe is consume-only, and a
    malformed state file should produce a graceful SKIP rather than
    crash the BIT run mid-sweep. Mirrors the precedent established
    by ``brief.health._most_recent_successful_brief_date`` and
    ``daily_sync.health._read_last_fired_date``.
    """
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    last_run = data.get("last_run_ts", "")
    if isinstance(last_run, str) and last_run:
        return last_run
    return None


def _read_last_error(state_path: Path) -> dict | None:
    """Read the instructor state file and return the ``last_error``
    payload (shape: ``{"ts": iso_string, "message": str}``) or None
    when absent / unreadable / corrupted-shape.

    Same defensive-read posture as ``_read_last_run_ts`` — a corrupt
    state file degrades silently to None so the probe still runs the
    timestamp-based threshold check rather than crashing. Mirrors
    ``brief.health._read_last_error`` (2026-05-14).
    """
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("last_error")
    if not isinstance(err, dict):
        return None
    msg = err.get("message")
    if not isinstance(msg, str) or not msg:
        return None
    return err


def _check_last_successful_poll(raw: dict[str, Any]) -> CheckResult:
    """Validate that the instructor daemon's poll loop completed
    recently.

    Status mapping:
      * SKIP if state file missing (fresh install — daemon hasn't
        polled yet) or ``last_run_ts`` absent / unparseable
      * OK   if last_run_ts is within the last hour (steady-state)
      * WARN if last_run_ts is 1h-4h old (one missed cycle window —
        operator may have paused the daemon for an edit, or a single
        transient hiccup)
      * FAIL if last_run_ts is > 4h old (multi-hour silent failure —
        the swallow-class catalogued in
        ``project_cross_daemon_swallow_audit.md``)

    When ``last_error`` is populated, the WARN/FAIL detail string
    gets a ``; last error: <msg>`` suffix (150-char cap) so the BIT
    line carries the failure cause without the operator grepping
    ``data/instructor.log``. OK detail stays clean since
    ``stamp_run`` wipes ``last_error`` on every successful tick.

    Per ``feedback_intentionally_left_blank.md``: this is the
    operator-visible signal that surfaces a daemon-level silent
    failure. Silence (instructor.daemon.poll_error logged once 4h
    ago, no pending-queue progress, operator notices stale
    directives) is ambiguous between idle-healthy and broken; the
    probe disambiguates.
    """
    state_path = _resolve_state_path(raw)
    last_run = _read_last_run_ts(state_path)

    if last_run is None:
        if not state_path.is_file():
            return CheckResult(
                name="last-successful-poll",
                status=Status.SKIP,
                detail=f"no state file (fresh install): {state_path}",
                data={"state_path": str(state_path), "exists": False},
            )
        return CheckResult(
            name="last-successful-poll",
            status=Status.SKIP,
            detail="no last_run_ts recorded yet",
            data={"state_path": str(state_path), "exists": True},
        )

    try:
        last_run_dt = datetime.fromisoformat(last_run)
    except ValueError:
        return CheckResult(
            name="last-successful-poll",
            status=Status.SKIP,
            detail=f"unparseable last_run_ts in state: {last_run!r}",
            data={"state_path": str(state_path)},
        )

    # Normalise naive timestamps to UTC so the delta below is well-defined.
    if last_run_dt.tzinfo is None:
        last_run_dt = last_run_dt.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    age_seconds = (now - last_run_dt).total_seconds()
    payload: dict[str, Any] = {
        "state_path": str(state_path),
        "last_run_ts": last_run,
        "age_seconds": int(age_seconds),
    }

    # Build the WARN/FAIL error suffix. Capped at 150 chars so the BIT
    # line stays a single readable row. Full structured error always
    # rides in ``result.data["last_error"]`` for JSON consumers
    # regardless of the cap. OK status path skips the suffix because
    # stamp_run clears last_error — a stale entry surviving past a
    # successful poll would only happen if an operator hand-edited
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

    age_human = _humanise_age(age_seconds)

    if age_seconds < _POLL_WARN_SECONDS:
        return CheckResult(
            name="last-successful-poll",
            status=Status.OK,
            detail=f"last poll: {last_run} ({age_human} ago)",
            data=payload,
        )
    if age_seconds < _POLL_FAIL_SECONDS:
        return CheckResult(
            name="last-successful-poll",
            status=Status.WARN,
            detail=(
                f"last poll: {last_run} ({age_human} ago — one missed "
                f"cycle window){error_suffix}"
            ),
            data=payload,
        )
    return CheckResult(
        name="last-successful-poll",
        status=Status.FAIL,
        detail=(
            f"last poll: {last_run} ({age_human} ago — daemon may be "
            f"silently failing){error_suffix}"
        ),
        data=payload,
    )


def _humanise_age(seconds: float) -> str:
    """Compact human-friendly age string for BIT detail lines.

    Buckets: seconds, minutes, hours, days. Single-unit only —
    we're emitting one operator-glance string, not a full duration
    breakdown.
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


async def health_check(raw: dict[str, Any], mode: str = "quick") -> ToolHealth:
    """Run instructor health checks.

    Returns SKIP if the ``instructor:`` config section is absent —
    the daemon auto-start is also gated on that section, so the
    probe's behaviour is consistent with the orchestrator.
    """
    if raw.get("instructor") is None:
        return ToolHealth(
            tool="instructor",
            status=Status.SKIP,
            detail="no instructor section in config",
        )

    results: list[CheckResult] = [
        _check_config_present(raw),
        _check_state_path_writable(raw),
        _check_skill_file(),
    ]
    results.extend(_check_queue_health(raw))
    results.append(_check_last_successful_poll(raw))

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="instructor", status=status, results=results)


register_check("instructor", health_check)
