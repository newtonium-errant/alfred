"""Distiller backend + pipeline failure classification (2026-07-29 incident).

Pins that the distiller CLI backend uses the shared classify + summary helper
(kind on ``BackendResult`` + ``claude.nonzero_exit`` log) and that
``pipeline._call_llm`` surfaces the ``kind`` on its ``pipeline.llm_failed``
log — driving the real production paths per
``feedback_log_emission_test_pattern.md``.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import structlog

from alfred.distiller.backends import BackendResult
from alfred.distiller.backends.cli import ClaudeBackend
from alfred.distiller.config import ClaudeBackendConfig, load_from_unified
from alfred.distiller.pipeline import _call_llm
from alfred.health.agent_failure import AgentCallOutcomes
from alfred.vault.mutation_log import cleanup_session_file, create_session_file

INCIDENT_STDOUT = "You've hit your weekly limit · resets 4am (UTC)"


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self, input: bytes | None = None):  # noqa: A002
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_distiller_backend_classifies_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_exec(*args, **kwargs):
        return _FakeProc(1, INCIDENT_STDOUT.encode("utf-8"), b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    backend = ClaudeBackend(ClaudeBackendConfig())

    with structlog.testing.capture_logs() as captured:
        result: BackendResult = await backend.process(prompt="p", vault_path="/tmp/v")

    assert result.success is False
    assert result.kind == "quota_limited"
    assert "You've hit your weekly limit" in result.summary
    # Raw streams still carried for pipeline.py diagnostics.
    assert result.stdout == INCIDENT_STDOUT
    nonzero = [c for c in captured if c.get("event") == "claude.nonzero_exit"]
    assert len(nonzero) == 1 and nonzero[0]["kind"] == "quota_limited"


@pytest.mark.asyncio
async def test_pipeline_llm_failed_logs_kind(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def _fake_exec(*args, **kwargs):
        return _FakeProc(1, INCIDENT_STDOUT.encode("utf-8"), b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    config = load_from_unified({"vault": {"path": str(tmp_path)}})
    session_path = create_session_file()
    outcomes = AgentCallOutcomes()
    try:
        with structlog.testing.capture_logs() as captured:
            stdout = await _call_llm(
                "prompt", config, session_path, "s3-test", outcomes=outcomes
            )
    finally:
        cleanup_session_file(session_path)

    # Returns raw stdout (the banner), not the error-summary string.
    assert stdout == INCIDENT_STDOUT
    failed = [c for c in captured if c.get("event") == "pipeline.llm_failed"]
    assert len(failed) == 1
    assert failed[0]["kind"] == "quota_limited"
    assert "You've hit your weekly limit" in failed[0]["summary"]
    # The log line was never the deliverable — the SINK is what reaches state
    # and therefore the probe. Before 2026-08-22 the kind stopped at the log.
    assert outcomes.events == [(False, "quota_limited", failed[0]["summary"])]
