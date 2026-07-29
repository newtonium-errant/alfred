"""Curator backend + daemon wiring for the 2026-07-29 weekly-limit incident.

Two production code paths are driven directly (not helper contracts in
isolation), per ``feedback_log_emission_test_pattern.md``:

  1. ``ClaudeBackend.process`` on a nonzero exit — a mocked subprocess emits
     the exact quota banner on stdout, exit 1. Pins that the returned
     ``BackendResult`` carries ``kind == quota_limited`` + a non-empty
     summary AND that ``claude.nonzero_exit`` logs the ``kind`` field.

  2. The curator daemon's ``result.success is False`` branch via
     ``_process_file`` with a stub backend. Pins that ``last_agent_failure``
     is persisted into state (kind + message) and that ``daemon.agent_failed``
     logs the ``kind`` field.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import structlog

from alfred.curator.backends import BackendResult
from alfred.curator.backends.cli import ClaudeBackend
from alfred.curator.config import ClaudeBackendConfig, load_from_unified
from alfred.curator.state import StateManager

INCIDENT_STDOUT = "You've hit your weekly limit · resets 4am (UTC)"


# ---------------------------------------------------------------------------
# 1 — ClaudeBackend.process nonzero-exit classification (real backend path)
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self, input: bytes | None = None):  # noqa: A002
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_backend_classifies_quota_and_logs_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_exec(*args, **kwargs):
        return _FakeProc(1, INCIDENT_STDOUT.encode("utf-8"), b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    backend = ClaudeBackend(ClaudeBackendConfig())

    with structlog.testing.capture_logs() as captured:
        result: BackendResult = await backend.process(
            inbox_content="raw",
            skill_text="skill",
            vault_context="ctx",
            inbox_filename="email-x.md",
            vault_path="/tmp/vault",
        )

    assert result.success is False
    assert result.kind == "quota_limited"
    assert "You've hit your weekly limit" in result.summary
    assert result.summary != "Exit code 1: "

    nonzero = [c for c in captured if c.get("event") == "claude.nonzero_exit"]
    assert len(nonzero) == 1
    assert nonzero[0]["kind"] == "quota_limited"
    assert "You've hit your weekly limit" in nonzero[0]["summary"]


@pytest.mark.asyncio
async def test_backend_stderr_only_error_classifies_other(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_exec(*args, **kwargs):
        return _FakeProc(1, b"", b"connection reset by peer")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    backend = ClaudeBackend(ClaudeBackendConfig())
    result = await backend.process(
        inbox_content="raw",
        skill_text="s",
        vault_context="c",
        inbox_filename="e.md",
        vault_path="/tmp/v",
    )
    assert result.kind == "other"
    assert "connection reset by peer" in result.summary


# ---------------------------------------------------------------------------
# 2 — daemon failure branch persists last_agent_failure + logs kind
# ---------------------------------------------------------------------------


class _StubBackend:
    """Minimal backend that always fails with a preset classified result."""

    def __init__(self, result: BackendResult) -> None:
        self._result = result
        self.env_overrides: dict[str, str] = {}

    async def process(self, **kwargs) -> BackendResult:
        return self._result


def _mk_config(vault: Path):
    return load_from_unified(
        {
            "vault": {"path": str(vault)},
            "curator": {
                "on_failure": {"action": "retry", "max_retries": 3},
                "state": {"path": str(vault / "data" / "curator_state.json")},
                "idle_tick": {"enabled": False},
            },
        }
    )


@pytest.mark.asyncio
async def test_daemon_failure_persists_last_agent_failure(tmp_path: Path) -> None:
    from alfred.curator.daemon import _process_file

    vault = tmp_path / "v"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    # No From header → sender_context path is skipped, keeping the drive minimal.
    inbox_file = inbox / "email-x.md"
    inbox_file.write_text("---\ntype: note\nname: X\n---\nbody\n", encoding="utf-8")

    config = _mk_config(vault)
    state_mgr = StateManager(config.state.path)
    state_mgr.load()

    failing = BackendResult(
        success=False,
        summary="Exit code 1: stdout: You've hit your weekly limit · resets 4am (UTC)",
        kind="quota_limited",
    )
    backend = _StubBackend(failing)

    with structlog.testing.capture_logs() as captured:
        await _process_file(inbox_file, backend, "skill", config, state_mgr)

    # State stamped with the failure (kind + message), last_run untouched.
    assert state_mgr.state.last_agent_failure is not None
    assert state_mgr.state.last_agent_failure["kind"] == "quota_limited"
    assert "weekly limit" in state_mgr.state.last_agent_failure["summary_tail"]

    # Persisted to disk (the daemon saves via _handle_processing_failure).
    reloaded = StateManager(config.state.path).load()
    assert reloaded.last_agent_failure is not None
    assert reloaded.last_agent_failure["kind"] == "quota_limited"

    # daemon.agent_failed logged with the kind field.
    failed = [c for c in captured if c.get("event") == "daemon.agent_failed"]
    assert len(failed) == 1
    assert failed[0]["kind"] == "quota_limited"

    # #34 contract preserved: file NOT marked processed, left in inbox for retry.
    assert inbox_file.exists()
    assert not state_mgr.state.is_processed(inbox_file.name)
