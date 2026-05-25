"""Unit tests for curator / janitor / distiller health modules.

These tests exercise the three tool health modules that landed in BIT c2.
We build a minimal config dict (not the full ``config.yaml.example``) and
point the vault path at a pytest ``tmp_path`` so we can flip writability
and state-file corruption on and off without side effects.

Anthropic auth is short-circuited by not setting a backend of "claude"
where possible — when a test needs to see the auth probe, it patches
``check_anthropic_auth`` to a stub. The real anthropic_auth module has
its own test file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from alfred.curator import health as curator_health
from alfred.distiller import health as distiller_health
from alfred.health.types import CheckResult, Status
from alfred.janitor import health as janitor_health


def _base_config(vault_path: Path, backend: str = "claude") -> dict[str, Any]:
    """A minimal raw config dict pointing at ``vault_path``.

    Default backend is ``claude`` — the only surviving agent backend
    post backend-abstraction-collapse (2026-05-25). The
    :func:`_stub_anthropic_auth` autouse fixture short-circuits the
    anthropic probe so tests stay offline by default. Tests that need
    to verify the probe semantics override the stub explicitly.
    """
    return {
        "vault": {"path": str(vault_path)},
        "agent": {"backend": backend},
        "curator": {
            "inbox_dir": "inbox",
            "state": {"path": str(vault_path.parent / "curator_state.json")},
        },
        "janitor": {"state": {"path": str(vault_path.parent / "janitor_state.json")}},
        "distiller": {
            "extraction": {"candidate_threshold": 0.3},
            "state": {"path": str(vault_path.parent / "distiller_state.json")},
        },
    }


@pytest.fixture(autouse=True)
def _stub_anthropic_auth(monkeypatch):
    """Stub the anthropic auth probe across all three tool-health modules.

    With the backend-collapse contract (only ``claude`` is known),
    the default ``_base_config`` switches from ``backend="zo"`` to
    ``backend="claude"``, which triggers the anthropic probe in every
    health check. Without this autouse stub, every default-backend
    test would either hit the network or fail because the env lacks
    an API key. Tests that want to verify probe semantics monkeypatch
    over this stub explicitly.
    """
    async def _ok(api_key, model="claude-haiku-4-5"):  # noqa: ANN001
        return CheckResult(name="anthropic-auth", status=Status.OK, detail="stubbed")

    monkeypatch.setattr(curator_health, "check_anthropic_auth", _ok)
    monkeypatch.setattr(janitor_health, "check_anthropic_auth", _ok)
    monkeypatch.setattr(distiller_health, "check_anthropic_auth", _ok)
    monkeypatch.setattr(curator_health, "resolve_api_key", lambda raw: "stub-key")
    monkeypatch.setattr(janitor_health, "resolve_api_key", lambda raw: "stub-key")
    monkeypatch.setattr(distiller_health, "resolve_api_key", lambda raw: "stub-key")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir()
    (v / "inbox").mkdir()
    return v


async def _ok_auth(api_key, model="claude-haiku-4-5"):  # noqa: ANN001
    return CheckResult(name="anthropic-auth", status=Status.OK, detail="stubbed")


# ---------------------------------------------------------------------------
# Curator
# ---------------------------------------------------------------------------

class TestCuratorHealth:
    async def test_happy_path_ok(self, vault: Path) -> None:
        # Seed a recent last_run so last-successful-process returns OK
        # (and not SKIP, which would bubble up via Status.worst). Without
        # this, the test was implicitly relying on a curator_state.json
        # in the main repo's ``./data/`` dir, so it only passed from
        # main-repo CWD — a worktree or fresh tmpdir CWD failed.
        state_path = vault.parent / "curator_state.json"
        state_path.write_text(
            json.dumps({
                "version": 1,
                "last_run": datetime.now(timezone.utc).isoformat(),
                "processed": {},
            }),
            encoding="utf-8",
        )
        result = await curator_health.health_check(_base_config(vault))
        assert result.tool == "curator"
        assert result.status == Status.OK
        names = {r.name for r in result.results}
        assert "vault-path" in names
        assert "inbox-dir" in names
        assert "backend" in names

    async def test_missing_vault_path_fails(self, tmp_path: Path) -> None:
        raw = _base_config(tmp_path / "does-not-exist")
        result = await curator_health.health_check(raw)
        assert result.status == Status.FAIL
        vp = next(r for r in result.results if r.name == "vault-path")
        assert vp.status == Status.FAIL

    async def test_empty_vault_path_fails(self, tmp_path: Path) -> None:
        # NOTE: must include a ``curator:`` section so the tool-level
        # SKIP gate (added 2026-05-16 for the KAL-LE peer-digest fix)
        # doesn't short-circuit before _check_vault runs. The test's
        # intent is to verify that an empty vault.path produces FAIL
        # when curator IS configured for this instance — KAL-LE-shape
        # configs (curator section absent entirely) are covered
        # separately in test_curator_probes.py::TestHealthCheckIntegration.
        raw = {"vault": {"path": ""}, "agent": {"backend": "zo"}, "curator": {}}
        result = await curator_health.health_check(raw)
        assert result.status == Status.FAIL

    async def test_missing_inbox_is_warn(self, vault: Path) -> None:
        # Delete the inbox the fixture created
        (vault / "inbox").rmdir()
        result = await curator_health.health_check(_base_config(vault))
        inbox = next(r for r in result.results if r.name == "inbox-dir")
        assert inbox.status == Status.WARN

    async def test_unknown_backend_is_warn(self, vault: Path) -> None:
        result = await curator_health.health_check(_base_config(vault, backend="made-up"))
        backend = next(r for r in result.results if r.name == "backend")
        assert backend.status == Status.WARN

    async def test_anthropic_probed_when_backend_is_claude(
        self,
        monkeypatch,
        vault: Path,
    ) -> None:
        monkeypatch.setattr(curator_health, "check_anthropic_auth", _ok_auth)
        monkeypatch.setattr(curator_health, "resolve_api_key", lambda raw: "test-anthropic-key")
        result = await curator_health.health_check(_base_config(vault, backend="claude"))
        names = {r.name for r in result.results}
        assert "anthropic-auth" in names

    async def test_anthropic_not_probed_for_non_claude_backends(
        self,
        monkeypatch,
        vault: Path,
    ) -> None:
        # If we accidentally probed, our stub would record a call.
        # Non-claude backends (here ``zo``, which is now an "unknown"
        # backend per the 2026-05-25 backend-collapse contract — but
        # still semantically non-claude) must NOT trigger the
        # anthropic probe.
        called = {"n": 0}

        async def _counting(api_key, model="claude-haiku-4-5"):  # noqa: ANN001
            called["n"] += 1
            return CheckResult(name="anthropic-auth", status=Status.OK)

        monkeypatch.setattr(curator_health, "check_anthropic_auth", _counting)
        await curator_health.health_check(_base_config(vault, backend="zo"))
        assert called["n"] == 0


# ---------------------------------------------------------------------------
# Janitor
# ---------------------------------------------------------------------------

class TestJanitorHealth:
    async def test_happy_path_ok(self, vault: Path) -> None:
        # Seed a recent last_deep_sweep so last-successful-sweep returns
        # OK (else SKIP bubbles up via Status.worst). The override path
        # in _base_config points at a fresh tmpdir; without a state
        # file there, this test was failing from every CWD.
        state_path = vault.parent / "janitor_state.json"
        state_path.write_text(
            json.dumps({
                "version": 1,
                "files": {},
                "sweeps": {},
                "last_deep_sweep": datetime.now(timezone.utc).isoformat(),
            }),
            encoding="utf-8",
        )
        result = await janitor_health.health_check(_base_config(vault))
        assert result.tool == "janitor"
        assert result.status == Status.OK

    async def test_missing_vault_fails(self, tmp_path: Path) -> None:
        raw = _base_config(tmp_path / "nope")
        result = await janitor_health.health_check(raw)
        assert result.status == Status.FAIL

    async def test_state_file_absent_is_ok(self, vault: Path) -> None:
        result = await janitor_health.health_check(_base_config(vault))
        state = next(r for r in result.results if r.name == "state-file")
        assert state.status == Status.OK
        assert "fresh install" in state.detail

    async def test_corrupt_state_file_warns(self, vault: Path, tmp_path: Path) -> None:
        state_path = tmp_path / "janitor_state.json"
        state_path.write_text("not json", encoding="utf-8")
        raw = _base_config(vault)
        raw["janitor"]["state"]["path"] = str(state_path)
        result = await janitor_health.health_check(raw)
        state = next(r for r in result.results if r.name == "state-file")
        assert state.status == Status.WARN

    async def test_good_state_file_is_ok(self, vault: Path, tmp_path: Path) -> None:
        state_path = tmp_path / "janitor_state.json"
        state_path.write_text(json.dumps({"version": 1}), encoding="utf-8")
        raw = _base_config(vault)
        raw["janitor"]["state"]["path"] = str(state_path)
        result = await janitor_health.health_check(raw)
        state = next(r for r in result.results if r.name == "state-file")
        assert state.status == Status.OK


# ---------------------------------------------------------------------------
# Distiller
# ---------------------------------------------------------------------------

class TestDistillerHealth:
    async def test_happy_path_ok(self, vault: Path) -> None:
        # Seed a recent last_deep_extraction so last-successful-extraction
        # returns OK (else SKIP bubbles up via Status.worst). Same shape
        # as janitor — override path is a fresh tmpdir without a state
        # file, so the freshness probe needs explicit content.
        state_path = vault.parent / "distiller_state.json"
        state_path.write_text(
            json.dumps({
                "version": 1,
                "runs": {},
                "last_deep_extraction": datetime.now(timezone.utc).isoformat(),
            }),
            encoding="utf-8",
        )
        result = await distiller_health.health_check(_base_config(vault))
        assert result.tool == "distiller"
        assert result.status == Status.OK

    async def test_threshold_out_of_range_warns(self, vault: Path) -> None:
        raw = _base_config(vault)
        raw["distiller"]["extraction"]["candidate_threshold"] = 1.5
        result = await distiller_health.health_check(raw)
        thr = next(r for r in result.results if r.name == "candidate-threshold")
        assert thr.status == Status.WARN

    async def test_threshold_non_numeric_fails(self, vault: Path) -> None:
        raw = _base_config(vault)
        raw["distiller"]["extraction"]["candidate_threshold"] = "zero"
        result = await distiller_health.health_check(raw)
        thr = next(r for r in result.results if r.name == "candidate-threshold")
        assert thr.status == Status.FAIL
        assert result.status == Status.FAIL

    async def test_corrupt_state_file_warns(self, vault: Path, tmp_path: Path) -> None:
        state_path = tmp_path / "distiller_state.json"
        state_path.write_text("not json", encoding="utf-8")
        raw = _base_config(vault)
        raw["distiller"]["state"]["path"] = str(state_path)
        result = await distiller_health.health_check(raw)
        state = next(r for r in result.results if r.name == "state-file")
        assert state.status == Status.WARN
