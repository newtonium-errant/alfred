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
from pathlib import Path
from typing import Any

import pytest

from alfred.curator import health as curator_health
from alfred.distiller import health as distiller_health
from alfred.health.types import CheckResult, Status
from alfred.janitor import health as janitor_health


def _base_config(vault_path: Path, backend: str = "zo") -> dict[str, Any]:
    """A minimal raw config dict pointing at ``vault_path``.

    Default backend is ``zo`` so the anthropic probe is skipped unless
    the test explicitly switches to claude — keeps these tests offline.
    """
    return {
        "vault": {"path": str(vault_path)},
        "agent": {"backend": backend},
        "curator": {"inbox_dir": "inbox"},
        "janitor": {"state": {"path": str(vault_path.parent / "janitor_state.json")}},
        "distiller": {
            "extraction": {"candidate_threshold": 0.3},
            "state": {"path": str(vault_path.parent / "distiller_state.json")},
        },
    }


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
        raw = {"vault": {"path": ""}, "agent": {"backend": "zo"}}
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
        monkeypatch.setattr(curator_health, "resolve_api_key", lambda raw: "sk-x")
        result = await curator_health.health_check(_base_config(vault, backend="claude"))
        names = {r.name for r in result.results}
        assert "anthropic-auth" in names

    async def test_anthropic_not_probed_for_non_claude_backends(
        self,
        monkeypatch,
        vault: Path,
    ) -> None:
        # If we accidentally probed, our stub would record a call.
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
