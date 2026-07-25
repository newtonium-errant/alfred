"""Shared fixtures for the BIT health test suite.

The #32 ``claude-cli-auth`` probe shells out to ``claude auth status``. Every
test that drives the curator/janitor/distiller health check (directly or via
the aggregator) would otherwise invoke the real ``claude`` CLI — environment-
dependent (login state, CLI presence) and slow. This autouse fixture stubs the
probe to a fast OK across the three claude-backend tool modules, so the health
suite stays hermetic. The probe's OWN logic is tested against a mocked
subprocess in ``test_claude_cli_auth.py``; tests wanting a specific probe
outcome monkeypatch over this stub explicitly.
"""
from __future__ import annotations

import pytest

from alfred.curator import health as curator_health
from alfred.distiller import health as distiller_health
from alfred.health.types import CheckResult, Status
from alfred.janitor import health as janitor_health


@pytest.fixture(autouse=True)
def _stub_claude_cli_auth(monkeypatch):
    async def _ok(command="claude", timeout=10.0):  # noqa: ANN001
        return CheckResult(name="claude-cli-auth", status=Status.OK, detail="stubbed")

    for mod in (curator_health, janitor_health, distiller_health):
        monkeypatch.setattr(mod, "check_claude_cli_auth", _ok)
