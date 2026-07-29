"""Janitor backend failure classification (2026-07-29 incident).

Pins that the janitor CLI backend uses the shared classify + summary helper —
kind on ``BackendResult`` + ``claude.nonzero_exit`` log. The janitor daemon's
``sweep.agent_failed`` log passes ``agent_result.kind`` straight through, so
the kind value is pinned here at its source. Drives the real backend path per
``feedback_log_emission_test_pattern.md``.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

import asyncio

import pytest
import structlog

from alfred.janitor.backends import BackendResult
from alfred.janitor.backends.cli import ClaudeBackend
from alfred.janitor.config import ClaudeBackendConfig

INCIDENT_STDOUT = "You've hit your weekly limit · resets 4am (UTC)"


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self, input: bytes | None = None):  # noqa: A002
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_janitor_backend_classifies_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_exec(*args, **kwargs):
        return _FakeProc(1, INCIDENT_STDOUT.encode("utf-8"), b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    backend = ClaudeBackend(ClaudeBackendConfig())

    with structlog.testing.capture_logs() as captured:
        result: BackendResult = await backend.process(
            skill_text="s",
            issue_report="r",
            affected_records="a",
            vault_path="/tmp/v",
        )

    assert result.success is False
    assert result.kind == "quota_limited"
    assert "You've hit your weekly limit" in result.summary
    nonzero = [c for c in captured if c.get("event") == "claude.nonzero_exit"]
    assert len(nonzero) == 1 and nonzero[0]["kind"] == "quota_limited"


@pytest.mark.asyncio
async def test_janitor_backend_auth_classifies_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_exec(*args, **kwargs):
        return _FakeProc(1, b"", b"Not logged in. Please run /login")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    backend = ClaudeBackend(ClaudeBackendConfig())
    result = await backend.process(
        skill_text="s", issue_report="r", affected_records="a", vault_path="/tmp/v"
    )
    assert result.kind == "auth"
    assert "not logged in" in result.summary.lower()
