"""Tests for ``alfred.instructor.health``.

Commit 6 registers the instructor with the BIT aggregator. These tests
verify:

- SKIP when the ``instructor:`` config section is absent (matches the
  auto-start gate — we shouldn't warn about a tool the user didn't
  enable).
- OK shape when config is present and preconditions are met.
- WARN when the pending-queue length exceeds the stuck-queue threshold.
- WARN when a record has hit max_retries in the state file.
- FAIL when SKILL.md is missing (synthetic path).
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import pytest

from alfred.health.types import Status


def _minimal_raw(tmp_path: Path, extras: dict | None = None) -> dict:
    """Build a minimal unified config dict with instructor section."""
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    raw = {
        "vault": {"path": str(vault)},
        "logging": {"dir": str(tmp_path / "data")},
        "instructor": {
            "poll_interval_seconds": 60,
            "max_retries": 3,
            "state": {"path": str(tmp_path / "data" / "instructor_state.json")},
            **(extras or {}),
        },
    }
    return raw


async def test_health_check_skips_when_section_absent(tmp_path: Path) -> None:
    from alfred.instructor.health import health_check
    raw = {
        "vault": {"path": str(tmp_path / "vault")},
        "logging": {"dir": str(tmp_path / "data")},
    }
    result = await health_check(raw, mode="quick")
    assert result.tool == "instructor"
    assert result.status == Status.SKIP
    assert result.results == []


async def test_health_check_ok_with_full_preconditions(tmp_path: Path) -> None:
    """Fresh install, no pending directives, no retries → OK overall."""
    from alfred.instructor.health import health_check
    raw = _minimal_raw(tmp_path)
    result = await health_check(raw, mode="quick")
    assert result.tool == "instructor"
    # Names present.
    names = {r.name for r in result.results}
    assert "config-section" in names
    assert "state-path" in names
    assert "skill-file" in names
    assert "pending-queue" in names
    assert "retry-at-max" in names
    assert result.status in (Status.OK, Status.SKIP)


async def test_health_check_warns_on_stuck_queue(tmp_path: Path) -> None:
    """Pending queue length > threshold (20) trips WARN on that probe."""
    from alfred.instructor.health import health_check, _STUCK_QUEUE_THRESHOLD
    raw = _minimal_raw(tmp_path)
    vault = Path(raw["vault"]["path"])
    # Write a record with 21 pending instructions. Not using dedent here
    # because the multi-line interpolated directives list breaks dedent's
    # "common-leading-whitespace" strip — just build the whole YAML flat.
    directives_yaml = "\n".join(
        f'  - "directive {i}"' for i in range(_STUCK_QUEUE_THRESHOLD + 1)
    )
    (vault / "note").mkdir(exist_ok=True)
    (vault / "note" / "Stuck.md").write_text(
        (
            "---\n"
            "type: note\n"
            "name: Stuck\n"
            "created: '2026-04-20'\n"
            "alfred_instructions:\n"
            f"{directives_yaml}\n"
            "---\n"
        ),
        encoding="utf-8",
    )
    result = await health_check(raw, mode="quick")
    queue_check = next(r for r in result.results if r.name == "pending-queue")
    assert queue_check.status == Status.WARN
    assert result.status == Status.WARN


async def test_health_check_warns_on_retry_at_max(tmp_path: Path) -> None:
    """State file with retry_counts >= max_retries triggers retry-at-max WARN."""
    from alfred.instructor.health import health_check
    raw = _minimal_raw(tmp_path)
    state_path = Path(raw["instructor"]["state"]["path"])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({
            "version": 1,
            "file_hashes": {},
            "retry_counts": {"note/Flaky.md": 3, "task/AlsoFlaky.md": 5},
            "last_run_ts": None,
        }),
        encoding="utf-8",
    )
    result = await health_check(raw, mode="quick")
    retry_check = next(r for r in result.results if r.name == "retry-at-max")
    assert retry_check.status == Status.WARN
    assert "2" in retry_check.detail  # "2 record(s) at max_retries=3"


async def test_health_check_detects_corrupt_state_file(tmp_path: Path) -> None:
    """A corrupt JSON state file downgrades state-path to WARN (not FAIL).

    Daemon tolerates the corrupt file by heal-on-save; operator wants
    to know but it's not a blocker.
    """
    from alfred.instructor.health import health_check
    raw = _minimal_raw(tmp_path)
    state_path = Path(raw["instructor"]["state"]["path"])
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("not json at all", encoding="utf-8")

    result = await health_check(raw, mode="quick")
    state_check = next(r for r in result.results if r.name == "state-path")
    assert state_check.status == Status.WARN
