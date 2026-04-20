"""Tests for ``alfred.transport.health``.

Covers each BIT probe in isolation — config-section, token-configured,
port-reachable, queue-depth, dead-letter-depth — plus the top-level
``health_check`` return shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alfred.health.types import Status
from alfred.transport.health import health_check


DUMMY_TRANSPORT_TEST_TOKEN = "DUMMY_TRANSPORT_HEALTH_TEST_TOKEN_PLACEHOLDER_01234567890"


def _base_config(tmp_path: Path) -> dict:
    return {
        "transport": {
            "server": {"host": "127.0.0.1", "port": 8891},
            "state": {"path": str(tmp_path / "transport_state.json")},
            "auth": {
                "tokens": {
                    "local": {
                        "token": "placeholder",
                        "allowed_clients": ["scheduler"],
                    },
                },
            },
        },
    }


async def test_health_check_skips_when_no_transport_section() -> None:
    report = await health_check({})
    assert report.status == Status.SKIP
    assert "no transport section" in (report.detail or "")


async def test_health_check_flags_missing_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALFRED_TRANSPORT_TOKEN", raising=False)
    report = await health_check(_base_config(tmp_path))
    # One of the checks should be FAIL with name token-configured.
    token_check = next(r for r in report.results if r.name == "token-configured")
    assert token_check.status == Status.FAIL
    assert "not set" in token_check.detail


async def test_health_check_warns_on_short_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALFRED_TRANSPORT_TOKEN", "short-token")
    report = await health_check(_base_config(tmp_path))
    token_check = next(r for r in report.results if r.name == "token-configured")
    assert token_check.status == Status.WARN


async def test_health_check_ok_on_good_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALFRED_TRANSPORT_TOKEN", DUMMY_TRANSPORT_TEST_TOKEN)
    report = await health_check(_base_config(tmp_path))
    token_check = next(r for r in report.results if r.name == "token-configured")
    assert token_check.status == Status.OK
    # Data field contains length, never the token itself.
    assert token_check.data["length"] == len(DUMMY_TRANSPORT_TEST_TOKEN)
    assert DUMMY_TRANSPORT_TEST_TOKEN not in (token_check.detail or "")


async def test_health_check_flags_placeholder_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALFRED_TRANSPORT_TOKEN", "${NOT_RESOLVED}")
    report = await health_check(_base_config(tmp_path))
    token_check = next(r for r in report.results if r.name == "token-configured")
    assert token_check.status == Status.FAIL
    assert "placeholder" in token_check.detail


async def test_health_check_port_unreachable_yields_warn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server not running is WARN, not FAIL — transport is optional."""
    monkeypatch.setenv("ALFRED_TRANSPORT_TOKEN", DUMMY_TRANSPORT_TEST_TOKEN)
    # Use a port nothing's listening on.
    cfg = _base_config(tmp_path)
    cfg["transport"]["server"]["port"] = 1  # privileged/unused port
    report = await health_check(cfg)
    port_check = next(r for r in report.results if r.name == "port-reachable")
    assert port_check.status == Status.WARN


async def test_health_check_state_depth_probes_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALFRED_TRANSPORT_TOKEN", DUMMY_TRANSPORT_TEST_TOKEN)
    # Write a state file with counts below threshold.
    state_path = tmp_path / "transport_state.json"
    state_path.write_text(
        json.dumps({
            "version": 1,
            "pending_queue": [{"id": f"p{i}"} for i in range(5)],
            "send_log": [],
            "dead_letter": [{"id": f"d{i}"} for i in range(3)],
        }),
        encoding="utf-8",
    )
    cfg = _base_config(tmp_path)
    cfg["transport"]["state"]["path"] = str(state_path)

    report = await health_check(cfg)
    q = next(r for r in report.results if r.name == "queue-depth")
    dl = next(r for r in report.results if r.name == "dead-letter-depth")
    assert q.status == Status.OK
    assert q.data["pending"] == 5
    assert dl.status == Status.OK
    assert dl.data["dead_letter"] == 3


async def test_health_check_state_depth_warn_on_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALFRED_TRANSPORT_TOKEN", DUMMY_TRANSPORT_TEST_TOKEN)
    state_path = tmp_path / "transport_state.json"
    state_path.write_text(
        json.dumps({
            "version": 1,
            "pending_queue": [{"id": f"p{i}"} for i in range(200)],
            "send_log": [],
            "dead_letter": [{"id": f"d{i}"} for i in range(100)],
        }),
        encoding="utf-8",
    )
    cfg = _base_config(tmp_path)
    cfg["transport"]["state"]["path"] = str(state_path)
    report = await health_check(cfg)

    q = next(r for r in report.results if r.name == "queue-depth")
    dl = next(r for r in report.results if r.name == "dead-letter-depth")
    assert q.status == Status.WARN
    assert dl.status == Status.WARN
