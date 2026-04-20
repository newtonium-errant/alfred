"""Tests for ``alfred transport`` subcommands + orchestrator env injection.

CLI dispatch is exercised via the pure command handlers (no argparse
round-trip). Covers:

- ``status`` prints the expected shape.
- ``queue`` lists pending scheduled sends.
- ``dead-letter list/drop/retry``.
- ``rotate`` replaces the token in .env with a backup.
- Orchestrator's ``_inject_transport_env_vars`` resolves values from
  the substituted config and sets env vars subprocesses will
  inherit.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

import pytest

from alfred.transport import cli as transport_cli


DUMMY_TRANSPORT_TEST_TOKEN = "DUMMY_TRANSPORT_CLI_TEST_TOKEN_PLACEHOLDER_01234567890"


def _make_raw(tmp_path: Path) -> dict:
    return {
        "transport": {
            "server": {"host": "127.0.0.1", "port": 8891},
            "state": {"path": str(tmp_path / "transport_state.json")},
            "auth": {
                "tokens": {
                    "local": {
                        "token": DUMMY_TRANSPORT_TEST_TOKEN,
                        "allowed_clients": ["scheduler", "brief"],
                    },
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _extract_json(output: str) -> dict:
    """Pull the first JSON object out of mixed log+JSON stdout.

    Structlog's ConsoleRenderer may dump a line or two to stdout
    before the command handler prints its JSON payload. The CLI is
    robust to this in real use (the caller pipes to jq with `| tail`
    or similar), but tests need a narrow extractor.
    """
    start = output.find("{")
    if start == -1:
        raise ValueError(f"no JSON in output: {output!r}")
    # Walk to matching brace.
    depth = 0
    for i, ch in enumerate(output[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(output[start : i + 1])
    raise ValueError(f"unbalanced JSON in output: {output!r}")


def test_cmd_status_json_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    monkeypatch.setenv("ALFRED_TRANSPORT_TOKEN", DUMMY_TRANSPORT_TEST_TOKEN)
    rc = transport_cli.cmd_status(_make_raw(tmp_path), wants_json=True)
    assert rc == 0
    out = capsys.readouterr().out
    payload = _extract_json(out)
    assert "pending_queue" in payload
    assert "dead_letter" in payload
    assert "health_status" in payload
    assert isinstance(payload["checks"], list)


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------


def test_cmd_queue_empty_json(tmp_path: Path, capsys) -> None:
    rc = transport_cli.cmd_queue(_make_raw(tmp_path), wants_json=True)
    assert rc == 0
    out = capsys.readouterr().out
    # Extract trailing JSON array — may be preceded by structlog output.
    start = out.rfind("[")
    assert start >= 0
    assert json.loads(out[start:]) == []


def test_cmd_queue_lists_pending(tmp_path: Path, capsys) -> None:
    from alfred.transport.state import TransportState

    raw = _make_raw(tmp_path)
    # Pre-populate the state file.
    state = TransportState.create(tmp_path / "transport_state.json")
    state.enqueue({
        "id": "pending-1",
        "user_id": 42,
        "text": "Future reminder",
        "scheduled_at": "2099-01-01T00:00:00+00:00",
    })
    state.save()

    rc = transport_cli.cmd_queue(raw, wants_json=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "pending-1" in out
    assert "Future reminder" in out


# ---------------------------------------------------------------------------
# dead-letter
# ---------------------------------------------------------------------------


def test_cmd_dead_letter_list(tmp_path: Path, capsys) -> None:
    from alfred.transport.state import TransportState

    raw = _make_raw(tmp_path)
    state = TransportState.create(tmp_path / "transport_state.json")
    state.append_dead_letter(
        {"id": "dl-1", "user_id": 42, "text": "Stale"},
        reason="stale_reminder_window_exceeded",
    )
    state.save()

    rc = transport_cli.cmd_dead_letter(raw, action="list")
    assert rc == 0
    out = capsys.readouterr().out
    assert "dl-1" in out
    assert "stale_reminder_window_exceeded" in out


def test_cmd_dead_letter_drop(tmp_path: Path) -> None:
    from alfred.transport.state import TransportState

    raw = _make_raw(tmp_path)
    state = TransportState.create(tmp_path / "transport_state.json")
    state.append_dead_letter({"id": "dl-drop"}, reason="whatever")
    state.save()

    rc = transport_cli.cmd_dead_letter(raw, action="drop", entry_id="dl-drop")
    assert rc == 0

    reloaded = TransportState.create(tmp_path / "transport_state.json")
    reloaded.load()
    assert reloaded.dead_letter == []


def test_cmd_dead_letter_retry_reenqueues(tmp_path: Path) -> None:
    from alfred.transport.state import TransportState

    raw = _make_raw(tmp_path)
    state = TransportState.create(tmp_path / "transport_state.json")
    state.append_dead_letter(
        {
            "id": "dl-retry",
            "user_id": 42,
            "text": "Retry me",
            "scheduled_at": "2026-01-01T00:00:00+00:00",
        },
        reason="stale",
    )
    state.save()

    rc = transport_cli.cmd_dead_letter(
        raw, action="retry", entry_id="dl-retry",
    )
    assert rc == 0

    reloaded = TransportState.create(tmp_path / "transport_state.json")
    reloaded.load()
    assert reloaded.dead_letter == []  # removed
    # Re-enqueued with scheduled_at cleared so it fires next tick.
    assert len(reloaded.pending_queue) == 1
    entry = reloaded.pending_queue[0]
    assert entry["id"] == "dl-retry"
    assert "scheduled_at" not in entry


def test_cmd_dead_letter_requires_id_for_drop(tmp_path: Path) -> None:
    rc = transport_cli.cmd_dead_letter(_make_raw(tmp_path), action="drop")
    assert rc == 1


# ---------------------------------------------------------------------------
# rotate
# ---------------------------------------------------------------------------


def test_cmd_rotate_creates_env_file(tmp_path: Path, capsys) -> None:
    env_path = tmp_path / ".env"
    # cmd_rotate uses default env_path=.env but we can pass a
    # different path via the kwarg — mirrors the real behaviour.
    rc = transport_cli.cmd_rotate({}, env_path=str(env_path))
    assert rc == 0
    content = env_path.read_text(encoding="utf-8")
    assert "ALFRED_TRANSPORT_TOKEN=" in content
    # 64 chars of hex.
    token_line = next(
        line for line in content.splitlines()
        if line.startswith("ALFRED_TRANSPORT_TOKEN=")
    )
    token = token_line.split("=", 1)[1]
    assert len(token) == 64
    int(token, 16)  # valid hex


def test_cmd_rotate_replaces_existing_token_and_backs_up(
    tmp_path: Path,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# Other vars\n"
        "MAIL_WEBHOOK_TOKEN=other-token\n"
        "ALFRED_TRANSPORT_TOKEN=old-token\n"
        "OTHER_VAR=preserved\n",
        encoding="utf-8",
    )

    rc = transport_cli.cmd_rotate({}, env_path=str(env_path))
    assert rc == 0

    # Backup captures the old content.
    backup = env_path.with_suffix(env_path.suffix + ".bak")
    assert backup.exists()
    assert "old-token" in backup.read_text(encoding="utf-8")

    # Live file has the new token; other vars preserved.
    new_content = env_path.read_text(encoding="utf-8")
    assert "old-token" not in new_content
    assert "MAIL_WEBHOOK_TOKEN=other-token" in new_content
    assert "OTHER_VAR=preserved" in new_content
    # Exactly one ALFRED_TRANSPORT_TOKEN line.
    assert new_content.count("ALFRED_TRANSPORT_TOKEN=") == 1


# ---------------------------------------------------------------------------
# Orchestrator env injection
# ---------------------------------------------------------------------------


def test_inject_transport_env_vars_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alfred.orchestrator import _inject_transport_env_vars

    # Clean slate — forget any values the test runner inherited.
    monkeypatch.delenv("ALFRED_TRANSPORT_HOST", raising=False)
    monkeypatch.delenv("ALFRED_TRANSPORT_PORT", raising=False)
    monkeypatch.delenv("ALFRED_TRANSPORT_TOKEN", raising=False)

    raw = {
        "transport": {
            "server": {"host": "192.168.1.1", "port": 9999},
            "auth": {
                "tokens": {
                    "local": {
                        "token": DUMMY_TRANSPORT_TEST_TOKEN,
                        "allowed_clients": ["scheduler"],
                    },
                },
            },
        },
    }
    _inject_transport_env_vars(raw)

    assert os.environ["ALFRED_TRANSPORT_HOST"] == "192.168.1.1"
    assert os.environ["ALFRED_TRANSPORT_PORT"] == "9999"
    assert os.environ["ALFRED_TRANSPORT_TOKEN"] == DUMMY_TRANSPORT_TEST_TOKEN


def test_inject_transport_env_vars_skips_unresolved_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unresolved ${VAR} placeholder must NOT be propagated to env.

    Leaking the literal ``${NOT_RESOLVED}`` string would cause the
    client to raise TransportAuthMissing on first call — worse, the
    BIT probe would show the placeholder in the token-configured
    check's FAIL detail. The guard keeps env clean.
    """
    from alfred.orchestrator import _inject_transport_env_vars

    monkeypatch.delenv("ALFRED_TRANSPORT_TOKEN", raising=False)
    raw = {
        "transport": {
            "auth": {
                "tokens": {
                    "local": {
                        "token": "${NOT_RESOLVED}",
                        "allowed_clients": [],
                    },
                },
            },
        },
    }
    _inject_transport_env_vars(raw)
    assert "ALFRED_TRANSPORT_TOKEN" not in os.environ


def test_inject_transport_env_vars_preserves_existing_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If env var is already set (e.g. from .env), don't clobber."""
    from alfred.orchestrator import _inject_transport_env_vars

    monkeypatch.setenv("ALFRED_TRANSPORT_TOKEN", "manual-override")
    raw = {
        "transport": {
            "auth": {
                "tokens": {
                    "local": {
                        "token": DUMMY_TRANSPORT_TEST_TOKEN,
                        "allowed_clients": ["scheduler"],
                    },
                },
            },
        },
    }
    _inject_transport_env_vars(raw)
    assert os.environ["ALFRED_TRANSPORT_TOKEN"] == "manual-override"


# ---------------------------------------------------------------------------
# Top-level parser registration
# ---------------------------------------------------------------------------


def test_main_parser_accepts_transport_subcommands() -> None:
    """``alfred transport ...`` argparse routing works end-to-end.

    No subprocess — just exercise ``build_parser`` directly to
    confirm every subcommand is registered.
    """
    from alfred.cli import build_parser

    parser = build_parser()
    # Each subcommand should parse cleanly.
    for argv in (
        ["transport", "status"],
        ["transport", "status", "--json"],
        ["transport", "queue"],
        ["transport", "queue", "--json"],
        ["transport", "dead-letter", "list"],
        ["transport", "dead-letter", "drop", "xyz"],
        ["transport", "dead-letter", "retry", "xyz"],
        ["transport", "rotate"],
        ["transport", "send-test", "42", "hello"],
    ):
        ns = parser.parse_args(argv)
        assert ns.command == "transport"
        assert ns.transport_cmd == argv[1]
