"""Instructor health check — registered with the BIT aggregator.

Three probe tiers per the commit 6 plan:

* **Static** — config section present, state file path writable
  (parent dir exists or can be created).
* **Local** — skills_dir contains ``vault-instructor/SKILL.md``.
* **Functional** — pending queue length < 20 (stuck-queue
  heuristic), no records have hit ``max_retries``.

Like every other tool's health module, registration fires at import
time so the aggregator picks it up automatically once
``KNOWN_TOOL_MODULES`` is updated.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from alfred.health.aggregator import register_check
from alfred.health.types import CheckResult, Status, ToolHealth


# Heuristic threshold for "stuck queue" — more than this many pending
# instructions across the whole vault and something is wrong (the
# daemon is down, the directives are malformed, or the model is
# failing every call). Operator needs to look.
_STUCK_QUEUE_THRESHOLD = 20


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

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="instructor", status=status, results=results)


register_check("instructor", health_check)
