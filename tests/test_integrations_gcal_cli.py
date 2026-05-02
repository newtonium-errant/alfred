"""Tests for ``alfred gcal`` subcommand handlers.

Mocks the GCalClient so the tests never run a real OAuth flow or touch
Google. Focus is on:

  * Exit code semantics (0 OK, 1 generic error, 78 missing optional dep)
  * JSON vs human-readable output shapes
  * Disabled-config short-circuits without authorization attempts
  * Status command's calendar-ID redaction
  * test-write happy path + cleanup
  * test-write failure modes
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# `_redact_id`
# ---------------------------------------------------------------------------


def test_redact_id_short():
    from alfred.integrations.gcal_cli import _redact_id
    assert _redact_id("") == ""
    assert _redact_id("short") == "short"
    assert _redact_id("12345678") == "12345678"


def test_redact_id_long():
    from alfred.integrations.gcal_cli import _redact_id
    assert _redact_id("very-long-calendar-id-12345678") == "...12345678"


# ---------------------------------------------------------------------------
# `cmd_authorize`
# ---------------------------------------------------------------------------


def test_authorize_disabled_config_still_runs(capsys):
    """Disabled config prints a notice but still runs the OAuth flow."""
    from alfred.integrations import gcal_cli

    fake_client = MagicMock()
    fake_client.authorize_interactive.return_value = "andrew@example.com"

    with patch("alfred.integrations.gcal.GCalClient", return_value=fake_client):
        rc = gcal_cli.cmd_authorize({})  # empty config = disabled
    assert rc == 0
    out = capsys.readouterr().out
    assert "disabled in config" in out.lower()
    assert "andrew@example.com" in out


def test_authorize_returns_78_when_libs_missing(capsys):
    from alfred.integrations import gcal_cli
    from alfred.integrations.gcal import GCalNotInstalled

    fake_client = MagicMock()
    fake_client.authorize_interactive.side_effect = GCalNotInstalled(
        "missing google-auth",
    )
    with patch("alfred.integrations.gcal.GCalClient", return_value=fake_client):
        rc = gcal_cli.cmd_authorize({"gcal": {"enabled": True}})
    assert rc == 78
    assert "missing google-auth" in capsys.readouterr().out


def test_authorize_happy_path(capsys):
    from alfred.integrations import gcal_cli

    fake_client = MagicMock()
    fake_client.authorize_interactive.return_value = "operator@example.com"
    with patch("alfred.integrations.gcal.GCalClient", return_value=fake_client):
        rc = gcal_cli.cmd_authorize({"gcal": {"enabled": True}})
    assert rc == 0
    out = capsys.readouterr().out
    assert "Authorized as: operator@example.com" in out
    assert "Token saved to" in out
    assert "alfred gcal status" in out  # next-steps text included


# ---------------------------------------------------------------------------
# `cmd_status`
# ---------------------------------------------------------------------------


def test_status_disabled_config(capsys):
    from alfred.integrations import gcal_cli

    rc = gcal_cli.cmd_status({})  # gcal section absent → enabled=False
    assert rc == 1
    out = capsys.readouterr().out
    assert "enabled:" in out
    assert "False" in out
    assert "ERROR" in out


def test_status_disabled_config_json(capsys):
    from alfred.integrations import gcal_cli

    rc = gcal_cli.cmd_status({}, wants_json=True)
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["enabled"] is False
    assert payload["error"] is not None
    assert payload["authorized"] is False


def test_status_unauthorized(capsys):
    from alfred.integrations import gcal_cli

    fake_client = MagicMock()
    fake_client.is_authorized.return_value = False

    with patch("alfred.integrations.gcal.GCalClient", return_value=fake_client):
        rc = gcal_cli.cmd_status({"gcal": {"enabled": True}})
    assert rc == 1
    out = capsys.readouterr().out
    assert "authorized:" in out
    assert "False" in out


def test_status_happy_path_with_event_counts(capsys):
    from alfred.integrations import gcal_cli

    # Two events on alfred-cal, three on primary.
    fake_client = MagicMock()
    fake_client.is_authorized.return_value = True
    fake_client.list_events.side_effect = [
        ["e1", "e2"], ["e3", "e4", "e5"],
    ]
    with patch("alfred.integrations.gcal.GCalClient", return_value=fake_client):
        rc = gcal_cli.cmd_status({
            "gcal": {
                "enabled": True,
                "alfred_calendar_id": "alfred-cal-12345678",
                "primary_calendar_id": "primary-cal-87654321",
            }
        }, wants_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["authorized"] is True
    assert payload["alfred_events_next_24h"] == 2
    assert payload["primary_events_next_24h"] == 3
    # IDs redacted to last 8 chars.
    assert payload["alfred_calendar_id_redacted"] == "...12345678"
    assert payload["primary_calendar_id_redacted"] == "...87654321"


def test_status_handles_unconfigured_calendar_id(capsys):
    from alfred.integrations import gcal_cli

    fake_client = MagicMock()
    fake_client.is_authorized.return_value = True
    # Only one calendar configured.
    fake_client.list_events.return_value = []
    with patch("alfred.integrations.gcal.GCalClient", return_value=fake_client):
        rc = gcal_cli.cmd_status({
            "gcal": {
                "enabled": True,
                "alfred_calendar_id": "alfred-only",
                # primary_calendar_id intentionally absent
            }
        }, wants_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["primary_events_next_24h"] == "calendar_id not configured"


# ---------------------------------------------------------------------------
# `cmd_test_write`
# ---------------------------------------------------------------------------


def test_test_write_disabled_config(capsys):
    from alfred.integrations import gcal_cli

    rc = gcal_cli.cmd_test_write({}, wants_json=True)
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "disabled" in payload["error"].lower()


def test_test_write_no_alfred_calendar_id(capsys):
    from alfred.integrations import gcal_cli

    rc = gcal_cli.cmd_test_write({
        "gcal": {"enabled": True},  # no alfred_calendar_id
    }, wants_json=True)
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "alfred_calendar_id" in payload["error"]


def test_test_write_happy_path_with_cleanup(capsys):
    from alfred.integrations import gcal_cli

    fake_client = MagicMock()
    fake_client.create_event.return_value = "test-event-id-99"
    fake_client.delete_event.return_value = True

    with patch("alfred.integrations.gcal.GCalClient", return_value=fake_client):
        rc = gcal_cli.cmd_test_write({
            "gcal": {
                "enabled": True,
                "alfred_calendar_id": "alfred-cal-id",
            }
        }, cleanup=True, wants_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["event_id"] == "test-event-id-99"
    assert payload["cleaned_up"] is True
    fake_client.delete_event.assert_called_once()


def test_test_write_no_cleanup_leaves_event(capsys):
    from alfred.integrations import gcal_cli

    fake_client = MagicMock()
    fake_client.create_event.return_value = "leave-me-alone-42"

    with patch("alfred.integrations.gcal.GCalClient", return_value=fake_client):
        rc = gcal_cli.cmd_test_write({
            "gcal": {
                "enabled": True,
                "alfred_calendar_id": "alfred-cal-id",
            }
        }, cleanup=False, wants_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["cleaned_up"] is False
    fake_client.delete_event.assert_not_called()


def test_test_write_unauthorized_returns_1(capsys):
    from alfred.integrations import gcal_cli
    from alfred.integrations.gcal import GCalNotAuthorized

    fake_client = MagicMock()
    fake_client.create_event.side_effect = GCalNotAuthorized("no token")
    with patch("alfred.integrations.gcal.GCalClient", return_value=fake_client):
        rc = gcal_cli.cmd_test_write({
            "gcal": {
                "enabled": True,
                "alfred_calendar_id": "alfred-cal-id",
            }
        }, wants_json=True)
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


def test_test_write_libs_missing_returns_78(capsys):
    from alfred.integrations import gcal_cli
    from alfred.integrations.gcal import GCalNotInstalled

    fake_client = MagicMock()
    fake_client.create_event.side_effect = GCalNotInstalled("no google-auth")
    with patch("alfred.integrations.gcal.GCalClient", return_value=fake_client):
        rc = gcal_cli.cmd_test_write({
            "gcal": {
                "enabled": True,
                "alfred_calendar_id": "alfred-cal-id",
            }
        })
    assert rc == 78


# ---------------------------------------------------------------------------
# Top-level CLI parser registration smoke
# ---------------------------------------------------------------------------


def test_alfred_gcal_subcommands_registered():
    """The top-level ``alfred`` parser knows about gcal authorize/status/test-write."""
    from alfred.cli import build_parser

    parser = build_parser()
    # Parse each subcommand to confirm the parser accepts them.
    args = parser.parse_args(["gcal", "authorize"])
    assert args.command == "gcal"
    assert args.gcal_cmd == "authorize"

    args = parser.parse_args(["gcal", "status", "--json"])
    assert args.gcal_cmd == "status"
    assert args.json is True

    args = parser.parse_args(["gcal", "test-write", "--no-cleanup", "--json"])
    assert args.gcal_cmd == "test-write"
    assert args.no_cleanup is True
    assert args.json is True
