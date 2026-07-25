"""Tests for the #32 claude-cli-auth probe — the ``claude -p`` LOGIN surface.

The probe (``claude auth status``, zero tokens) closes the blind spot that made
the 2026-07 structuring outage silent: the SDK ``anthropic_auth`` probe checks
API-KEY auth, but the pipeline runs ``claude -p`` (Claude Code login), so with
``ANTHROPIC_API_KEY`` set the SDK probe stayed green while ``claude -p`` was
"not logged in". These pins run the real probe against a mocked subprocess, and
verify the wiring into curator/janitor/distiller (all gated on
``backend == "claude"``). Tests run unconditionally.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from alfred.curator import health as curator_health
from alfred.distiller import health as distiller_health
from alfred.health.claude_cli_auth import (
    _parse_logged_in,
    _safe_fields,
    check_claude_cli_auth,
    resolve_claude_command,
)
from alfred.health.types import Status
from alfred.janitor import health as janitor_health


# ---------------------------------------------------------------------------
# Mocked subprocess
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, hang=False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang

    async def communicate(self, input=None):
        if self._hang:
            await asyncio.sleep(10)
        return (self._stdout, self._stderr)


def _install(monkeypatch, *, stdout=b"", stderr=b"", returncode=0, raises=None, hang=False):
    async def _fake(*args, **kwargs):
        if raises is not None:
            raise raises
        return _FakeProc(stdout=stdout, stderr=stderr, returncode=returncode, hang=hang)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)


_LOGGED_IN_JSON = (
    b'{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty", '
    b'"email": "andrewnewton965@gmail.com", "orgId": "abc-123", '
    b'"orgName": "org", "subscriptionType": "max"}'
)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


async def test_logged_in_is_ok(monkeypatch) -> None:
    _install(monkeypatch, stdout=_LOGGED_IN_JSON, returncode=0)
    r = await check_claude_cli_auth()
    assert r.status is Status.OK
    assert r.name == "claude-cli-auth"


async def test_logged_in_data_excludes_pii(monkeypatch) -> None:
    _install(monkeypatch, stdout=_LOGGED_IN_JSON, returncode=0)
    r = await check_claude_cli_auth()
    # Diagnostic fields kept; account identity NOT echoed into the vault record.
    assert r.data == {"authMethod": "claude.ai", "subscriptionType": "max"}
    assert "email" not in r.data and "orgId" not in r.data


async def test_logged_out_json_is_fail(monkeypatch) -> None:
    _install(monkeypatch, stdout=b'{"loggedIn": false}', returncode=0)
    r = await check_claude_cli_auth()
    assert r.status is Status.FAIL
    assert "not logged in" in r.detail.lower()


async def test_nonzero_exit_is_fail(monkeypatch) -> None:
    _install(monkeypatch, stderr=b"Not logged in", returncode=1)
    r = await check_claude_cli_auth()
    assert r.status is Status.FAIL


async def test_not_logged_in_text_without_json_is_fail(monkeypatch) -> None:
    # Exit 0 but a plain "Not logged in" body → still FAIL (belt).
    _install(monkeypatch, stdout=b"Not logged in\n", returncode=0)
    r = await check_claude_cli_auth()
    assert r.status is Status.FAIL


async def test_command_not_found_is_fail(monkeypatch) -> None:
    _install(monkeypatch, raises=FileNotFoundError())
    r = await check_claude_cli_auth(command="nope")
    assert r.status is Status.FAIL
    assert "not found" in r.detail.lower()


async def test_timeout_is_warn_not_fail(monkeypatch) -> None:
    # Transient CLI variance must degrade to WARN, never a false FAIL.
    _install(monkeypatch, hang=True)
    r = await check_claude_cli_auth(timeout=0.01)
    assert r.status is Status.WARN


async def test_unparseable_exit0_is_warn(monkeypatch) -> None:
    _install(monkeypatch, stdout=b"totally not json", returncode=0)
    r = await check_claude_cli_auth()
    assert r.status is Status.WARN


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_resolve_claude_command_default() -> None:
    assert resolve_claude_command({}) == "claude"
    assert resolve_claude_command({"agent": {"claude": {}}}) == "claude"


def test_resolve_claude_command_from_config() -> None:
    assert resolve_claude_command({"agent": {"claude": {"command": "/opt/claude"}}}) == "/opt/claude"


def test_parse_logged_in() -> None:
    assert _parse_logged_in('{"loggedIn": true}') is True
    assert _parse_logged_in('{"loggedIn": false}') is False
    assert _parse_logged_in("garbage") is None
    assert _parse_logged_in('{"other": 1}') is None


def test_safe_fields_excludes_identity() -> None:
    fields = _safe_fields(_LOGGED_IN_JSON.decode())
    assert fields == {"authMethod": "claude.ai", "subscriptionType": "max"}


# ---------------------------------------------------------------------------
# Wiring into the three claude-backend tools (gated on backend == "claude")
# The probe itself is stubbed to OK by the suite conftest; these pin that the
# health_check CALLS it (present) / does not (when backend != claude).
# ---------------------------------------------------------------------------


def _raw(vault: Path, tool: str, backend: str) -> dict:
    (vault / "inbox").mkdir(parents=True, exist_ok=True)
    return {"vault": {"path": str(vault)}, tool: {}, "agent": {"backend": backend}}


async def test_curator_wires_claude_cli_auth(tmp_path: Path) -> None:
    th = await curator_health.health_check(_raw(tmp_path / "v", "curator", "claude"))
    assert any(r.name == "claude-cli-auth" for r in th.results)


async def test_janitor_wires_claude_cli_auth(tmp_path: Path) -> None:
    th = await janitor_health.health_check(_raw(tmp_path / "v", "janitor", "claude"))
    assert any(r.name == "claude-cli-auth" for r in th.results)


async def test_distiller_wires_claude_cli_auth(tmp_path: Path) -> None:
    th = await distiller_health.health_check(_raw(tmp_path / "v", "distiller", "claude"))
    assert any(r.name == "claude-cli-auth" for r in th.results)


async def test_non_claude_backend_omits_claude_cli_auth(tmp_path: Path) -> None:
    th = await curator_health.health_check(_raw(tmp_path / "v", "curator", "zo"))
    assert not any(r.name == "claude-cli-auth" for r in th.results)
