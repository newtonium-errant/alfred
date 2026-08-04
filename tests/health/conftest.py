"""Shared fixtures for the BIT health test suite.

A tool ``health_check`` runs EVERY probe for that tool, so any test that drives
one — directly or via the aggregator — reaches the two probes that leave the
machine. Both are stubbed here, autouse, so the health suite is hermetic by
default rather than per-file:

* ``claude-cli-auth`` (#32) shells out to ``claude auth status`` — environment
  dependent (login state, CLI presence) and slow.
* ``anthropic-auth`` builds a real ``anthropic.Anthropic`` and calls
  ``count_tokens`` — a live HTTPS request to ``api.anthropic.com``.

**Why the anthropic stub moved here (#16).** It already existed as a local
autouse fixture in ``test_per_tool_core.py``, so every OTHER file in this
package was unprotected. ``test_claude_cli_auth.py``'s three wiring tests drove
``health_check`` with ``backend="claude"`` and dialled Anthropic for real —
measured, `api.anthropic.com:443`. Only three of its four wiring tests leaked
because the fourth uses ``backend="zo"`` and skips the whole block.

**That leak was invisible without credentials**, which is the reason this is a
conftest stub and not a per-test one: ``check_anthropic_auth`` returns SKIP on a
falsy key and ``resolve_api_key`` reads ``ANTHROPIC_API_KEY`` from the ambient
environment, so on a key-less machine the tests pass while proving nothing. A
fixture that only holds when the developer happens to lack a credential is not
a fixture. Stubbing ``resolve_api_key`` too makes the behaviour identical in
both environments rather than merely quiet in one.

Tests wanting a specific probe outcome monkeypatch over these stubs explicitly;
each probe's own logic is tested against fakes in ``test_claude_cli_auth.py`` /
``test_anthropic_auth.py``, which patch the SOURCE module and are unaffected by
these consumer-module patches.
"""
from __future__ import annotations

import pytest

from alfred.curator import health as curator_health
from alfred.distiller import health as distiller_health
from alfred.health.types import CheckResult, Status
from alfred.janitor import health as janitor_health
from alfred.telegram import health as talker_health

# Every module that imports ``check_anthropic_auth`` from the shared probe.
# Patching the CONSUMER modules (not the source) is deliberate: it leaves
# ``test_anthropic_auth.py``'s direct tests of the real probe untouched.
# If a new tool starts importing the probe, add it here — otherwise its health
# tests silently regain the ability to dial out.
_ANTHROPIC_CONSUMERS = (curator_health, janitor_health, distiller_health, talker_health)

# The subset that also imports ``resolve_api_key`` (talker resolves its key from
# its own config section instead).
_API_KEY_RESOLVERS = (curator_health, janitor_health, distiller_health)


@pytest.fixture(autouse=True)
def _stub_claude_cli_auth(monkeypatch):
    async def _ok(command="claude", timeout=10.0):  # noqa: ANN001
        return CheckResult(name="claude-cli-auth", status=Status.OK, detail="stubbed")

    for mod in (curator_health, janitor_health, distiller_health):
        monkeypatch.setattr(mod, "check_claude_cli_auth", _ok)


@pytest.fixture(autouse=True)
def _stub_anthropic_auth(monkeypatch):
    """Stub the Anthropic SDK auth probe across every consumer module.

    ``resolve_api_key`` is stubbed alongside it so the ``backend == "claude"``
    branch is exercised identically with and without ``ANTHROPIC_API_KEY`` in
    the environment — the probe returns a deterministic OK either way instead of
    SKIP-when-key-less / live-call-when-keyed.
    """
    async def _ok(api_key, model="claude-haiku-4-5"):  # noqa: ANN001
        return CheckResult(name="anthropic-auth", status=Status.OK, detail="stubbed")

    for mod in _ANTHROPIC_CONSUMERS:
        monkeypatch.setattr(mod, "check_anthropic_auth", _ok)
    for mod in _API_KEY_RESOLVERS:
        monkeypatch.setattr(mod, "resolve_api_key", lambda raw: "stub-key")
