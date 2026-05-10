"""Curator health check — registered with the BIT aggregator.

Probes:
  * vault path exists + writable
  * inbox_dir exists (where the curator watches for new files)
  * anthropic auth (via the shared probe in alfred.health.anthropic_auth)
  * backend type is known (static check — warns on misconfigured backends)
  * last-successful-process — daemon liveness validation per the
    universal "intentionally left blank" / observability discipline
    (added 2026-05-10 as part of the cross-daemon BIT probe arc;
    mirrors brief's ``last-successful-brief`` precedent). Curator is
    inotify-driven so a quiet inbox legitimately means no activity —
    the probe uses a stale-with-non-empty-inbox heuristic to
    distinguish "daemon dead" from "no work to do."

This module is imported by ``alfred.health.aggregator._load_tool_checks``;
the import side-effect is registering the ``health_check`` callable.
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


# Stale-threshold calibrations for the last-successful-process probe.
# Curator is inotify-driven so the cadence is "as work arrives." These
# are escalation thresholds applied ONLY when the inbox has unprocessed
# files — a quiet inbox means no work, which is healthy regardless of
# last_run age. Surfaced as module constants so threshold-tuning is a
# 1-line change.
_CURATOR_STALE_WARN_HOURS = 24
_CURATOR_STALE_FAIL_HOURS = 48


_KNOWN_BACKENDS = ("claude", "zo", "openclaw", "hermes")


def _check_vault(raw: dict[str, Any]) -> list[CheckResult]:
    """Verify vault path + curator-specific inbox directory."""
    results: list[CheckResult] = []

    vault_path_str = (raw.get("vault", {}) or {}).get("path", "") or ""
    if not vault_path_str:
        results.append(CheckResult(
            name="vault-path",
            status=Status.FAIL,
            detail="vault.path is empty in config",
        ))
        return results

    vault_path = Path(vault_path_str)
    if not vault_path.exists():
        results.append(CheckResult(
            name="vault-path",
            status=Status.FAIL,
            detail=f"vault path does not exist: {vault_path}",
        ))
        return results
    if not os.access(vault_path, os.W_OK):
        results.append(CheckResult(
            name="vault-path",
            status=Status.FAIL,
            detail=f"vault path not writable: {vault_path}",
        ))
    else:
        results.append(CheckResult(
            name="vault-path",
            status=Status.OK,
            detail=str(vault_path),
            data={"path": str(vault_path)},
        ))

    inbox_rel = (raw.get("curator", {}) or {}).get("inbox_dir", "inbox")
    inbox_path = vault_path / inbox_rel
    if inbox_path.exists():
        results.append(CheckResult(
            name="inbox-dir",
            status=Status.OK,
            detail=str(inbox_path),
            data={"path": str(inbox_path)},
        ))
    else:
        # Missing inbox_dir is not fatal — it's auto-created when the
        # curator ingests the first email. Surface as WARN so operators
        # notice on fresh installs.
        results.append(CheckResult(
            name="inbox-dir",
            status=Status.WARN,
            detail=f"inbox dir missing (will be created on first use): {inbox_path}",
        ))

    return results


def _check_backend(raw: dict[str, Any]) -> CheckResult:
    """Static check of the configured agent backend."""
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
        detail=f"unknown backend '{backend}' (known: {', '.join(_KNOWN_BACKENDS)})",
        data={"backend": backend},
    )


def _resolve_curator_state_path(raw: dict[str, Any]) -> Path:
    """Resolve the curator's state-file path the same way
    ``alfred.curator.config.load_from_unified`` does — explicit path
    wins, otherwise the dataclass default ``./data/curator_state.json``.

    Probes consult this rather than the runtime ``StateManager`` so a
    malformed JSON degrades gracefully (returns SKIP) without crashing
    the BIT run mid-sweep.
    """
    state_section = (raw.get("curator", {}) or {}).get("state", {}) or {}
    explicit = state_section.get("path", "")
    if explicit:
        return Path(explicit)
    return Path("./data/curator_state.json")


def _read_curator_last_run(state_path: Path) -> str | None:
    """Read curator state file's top-level ``last_run`` ISO timestamp.

    Returns None if missing / unparseable / empty. Inlined dict-walk
    rather than constructing ``alfred.curator.state.StateManager`` —
    matches the precedent set by ``brief.health._most_recent_successful_brief_date``.
    """
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    last_run = data.get("last_run", "")
    if isinstance(last_run, str) and last_run:
        return last_run
    return None


def _inbox_has_pending_files(raw: dict[str, Any]) -> bool:
    """True iff curator's inbox dir has any non-``.gitkeep`` file.

    Used by the last-successful-process probe to distinguish "daemon
    dead" from "no work to do" — curator is inotify-driven, so a
    quiet inbox is the legitimate idle state. Returns False (treat as
    "no work") if the inbox is missing or unreadable; we don't want
    a missing inbox to cascade into a FAIL on the wrong probe. The
    ``inbox-dir`` probe surfaces missing-inbox separately.
    """
    vault_path_str = (raw.get("vault", {}) or {}).get("path", "") or ""
    if not vault_path_str:
        return False
    vault_path = Path(vault_path_str)
    inbox_rel = (raw.get("curator", {}) or {}).get("inbox_dir", "inbox")
    inbox_path = vault_path / inbox_rel
    if not inbox_path.is_dir():
        return False
    try:
        for entry in inbox_path.iterdir():
            if entry.name == ".gitkeep":
                continue
            if entry.is_file():
                return True
            # Subdirs other than ``processed/`` (curator's audit trail)
            # are treated as "potential work" — defensive default.
            if entry.is_dir() and entry.name != "processed":
                # Recurse into top-level subdirs since curator processes
                # nested files too (mail accounts land in inbox/<mailbox>/).
                try:
                    for sub_entry in entry.iterdir():
                        if sub_entry.is_file() and sub_entry.name != ".gitkeep":
                            return True
                except OSError:
                    continue
    except OSError:
        return False
    return False


def _check_last_successful_process(raw: dict[str, Any]) -> CheckResult:
    """Validate that the curator daemon has processed something recently.

    Curator is inotify-driven (no fixed schedule), so "stale ``last_run``"
    is only a problem when there's pending work in the inbox. The probe
    combines both signals:

    * SKIP if state file missing (fresh install) OR ``last_run`` empty
      (curator has never run) OR ``last_run`` unparseable
    * OK   if inbox is empty (regardless of ``last_run`` age — quiet
      inbox legitimately means nothing to do)
    * OK   if inbox non-empty AND ``last_run`` <= 24h ago (working
      through the queue)
    * WARN if inbox non-empty AND ``last_run`` 24h..48h ago (stale —
      could be a single ingest hiccup)
    * FAIL if inbox non-empty AND ``last_run`` > 48h ago (silent
      failure pattern — daemon is up but not making progress)

    "Now" is computed in UTC because curator's ``last_run`` is written
    in UTC (see ``state.mark_processed`` line 62: ``datetime.now(
    timezone.utc).isoformat()``). No timezone config to consult.

    Per ``feedback_intentionally_left_blank.md``: silence (curator
    daemon idle, inbox piling up, no log signal) is ambiguous between
    healthy-quiet and broken; the probe disambiguates.
    """
    state_path = _resolve_curator_state_path(raw)
    last_run_iso = _read_curator_last_run(state_path)

    if last_run_iso is None:
        if not state_path.is_file():
            return CheckResult(
                name="last-successful-process",
                status=Status.SKIP,
                detail=f"no state file (fresh install): {state_path}",
                data={"state_path": str(state_path), "exists": False},
            )
        return CheckResult(
            name="last-successful-process",
            status=Status.SKIP,
            detail="no last_run recorded yet",
            data={"state_path": str(state_path), "exists": True},
        )

    try:
        # Tolerate trailing-Z form alongside the explicit-offset form
        # ``state.mark_processed`` writes today.
        normalized = last_run_iso.replace("Z", "+00:00") if last_run_iso.endswith("Z") else last_run_iso
        last_run = datetime.fromisoformat(normalized)
        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=timezone.utc)
    except ValueError:
        return CheckResult(
            name="last-successful-process",
            status=Status.SKIP,
            detail=f"unparseable last_run in state: {last_run_iso!r}",
            data={"state_path": str(state_path)},
        )

    now = datetime.now(timezone.utc)
    elapsed = now - last_run
    elapsed_hours = elapsed.total_seconds() / 3600.0
    pending = _inbox_has_pending_files(raw)
    payload: dict[str, Any] = {
        "state_path": str(state_path),
        "last_run": last_run_iso,
        "elapsed_hours": round(elapsed_hours, 2),
        "inbox_has_pending": pending,
    }

    if not pending:
        # Quiet inbox — curator's idle state is legitimate. Record the
        # age in detail so the operator can still see drift, but don't
        # escalate.
        return CheckResult(
            name="last-successful-process",
            status=Status.OK,
            detail=f"inbox empty; last process {round(elapsed_hours, 1)}h ago",
            data=payload,
        )
    if elapsed < timedelta(hours=_CURATOR_STALE_WARN_HOURS):
        return CheckResult(
            name="last-successful-process",
            status=Status.OK,
            detail=f"inbox has pending files; last process {round(elapsed_hours, 1)}h ago",
            data=payload,
        )
    if elapsed < timedelta(hours=_CURATOR_STALE_FAIL_HOURS):
        return CheckResult(
            name="last-successful-process",
            status=Status.WARN,
            detail=(
                f"inbox has pending files; last process "
                f"{round(elapsed_hours, 1)}h ago (stale — possible ingest hiccup)"
            ),
            data=payload,
        )
    return CheckResult(
        name="last-successful-process",
        status=Status.FAIL,
        detail=(
            f"inbox has pending files; last process "
            f"{round(elapsed_hours, 1)}h ago (daemon may be silently failing)"
        ),
        data=payload,
    )


async def health_check(raw: dict[str, Any], mode: str = "quick") -> ToolHealth:
    """Run curator health checks.

    The ``mode`` argument is accepted for interface uniformity; curator
    checks are all cheap so quick and full do the same work today.
    """
    results: list[CheckResult] = []
    results.extend(_check_vault(raw))
    results.append(_check_backend(raw))

    # Anthropic auth — only probe if the configured backend is one that
    # uses the Anthropic SDK / CLI. Other backends (zo, openclaw via local
    # models) don't need Anthropic credentials.
    backend = (raw.get("agent", {}) or {}).get("backend", "claude")
    if backend == "claude":
        api_key = resolve_api_key(raw)
        results.append(await check_anthropic_auth(api_key))

    results.append(_check_last_successful_process(raw))

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="curator", status=status, results=results)


# Registration side-effect at import time — the aggregator imports this
# module in ``_load_tool_checks`` to populate its registry.
register_check("curator", health_check)
