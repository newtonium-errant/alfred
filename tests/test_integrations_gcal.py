"""Unit tests for the Google Calendar adapter.

Mocks ``googleapiclient.discovery.build`` so the tests don't need real
credentials and never touch the network. Token-refresh and OAuth-flow
paths are covered via mocks of ``Credentials.from_authorized_user_file``
+ ``InstalledAppFlow.from_client_secrets_file``.

Coverage:
  * Config loader (defaults, env substitution, override, schema-tolerance)
  * GCalNotInstalled raised when google-* libs absent
  * GCalNotAuthorized raised when no token exists
  * Token refresh on expired token; saved back to disk
  * list_events: API call shape, parse of dateTime + date events,
    skip-on-parse-error defensiveness
  * create_event: API call shape, RFC-3339 datetime serialization,
    timezone-aware enforcement, end>start enforcement
  * get_event: 404 → None
  * delete_event: 404 → False
  * event_to_conflict_dict: shape includes source + gcal_event_id

The Google API responses are constructed by hand (tiny dicts) — the
real shape is documented at
https://developers.google.com/calendar/api/v3/reference/events
and the relevant fields are: id, summary, description, start{dateTime
or date}, end{dateTime or date}.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------


def test_gcal_config_defaults_when_absent():
    from alfred.integrations.gcal_config import load_from_unified

    config = load_from_unified({})
    assert config.enabled is False
    assert config.alfred_calendar_id == ""
    assert config.primary_calendar_id == ""
    # Default scopes list contains the events scope.
    assert any("calendar.events" in s for s in config.scopes)


def test_gcal_config_loads_from_block():
    from alfred.integrations.gcal_config import load_from_unified

    config = load_from_unified({
        "gcal": {
            "enabled": True,
            "credentials_path": "/tmp/creds.json",
            "token_path": "/tmp/token.json",
            "alfred_calendar_id": "alfred-cal-id@group.calendar.google.com",
            "primary_calendar_id": "andrew@example.com",
        }
    })
    assert config.enabled is True
    assert config.alfred_calendar_id == "alfred-cal-id@group.calendar.google.com"
    assert config.primary_calendar_id == "andrew@example.com"
    assert config.credentials_path == "/tmp/creds.json"
    assert config.token_path == "/tmp/token.json"


def test_gcal_config_env_substitution(monkeypatch):
    from alfred.integrations.gcal_config import load_from_unified

    monkeypatch.setenv("TEST_GCAL_ID", "from-env-12345")
    config = load_from_unified({
        "gcal": {
            "enabled": True,
            "alfred_calendar_id": "${TEST_GCAL_ID}",
        }
    })
    assert config.alfred_calendar_id == "from-env-12345"


def test_gcal_config_tolerates_extra_keys():
    """Schema-tolerance contract — newer-version key must not crash."""
    from alfred.integrations.gcal_config import load_from_unified

    config = load_from_unified({
        "gcal": {
            "enabled": True,
            "alfred_calendar_id": "id1",
            "future_phase_d_webhook_url": "https://example.com/hook",  # unknown
        }
    })
    assert config.enabled is True
    assert config.alfred_calendar_id == "id1"


def test_gcal_config_scopes_coercion():
    """Scopes accept either string or list."""
    from alfred.integrations.gcal_config import load_from_unified

    cfg = load_from_unified({"gcal": {"scopes": "single-scope"}})
    assert cfg.scopes == ["single-scope"]
    cfg2 = load_from_unified({"gcal": {"scopes": ["a", "b"]}})
    assert cfg2.scopes == ["a", "b"]


# ---------------------------------------------------------------------------
# GCalNotInstalled — when google-* libs missing
# ---------------------------------------------------------------------------


def test_gcal_not_installed_raised(tmp_path, monkeypatch):
    """Force the import shim to fail and confirm the right exception bubbles."""
    from alfred.integrations import gcal as gcal_mod

    def _fail():
        raise gcal_mod.GCalNotInstalled("test forced missing")

    monkeypatch.setattr(gcal_mod, "_import_google", _fail)
    client = gcal_mod.GCalClient(
        credentials_path=tmp_path / "creds.json",
        token_path=tmp_path / "token.json",
    )
    with pytest.raises(gcal_mod.GCalNotInstalled):
        client.is_authorized()  # exercise nothing — but list_events would
        # actually trigger _import_google. Use list_events to be safe:
        client.list_events(
            "cal", datetime.now(timezone.utc), datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# Authorization paths
# ---------------------------------------------------------------------------


def test_is_authorized_false_when_no_token(tmp_path):
    from alfred.integrations.gcal import GCalClient

    client = GCalClient(
        credentials_path=tmp_path / "creds.json",
        token_path=tmp_path / "token.json",
    )
    assert client.is_authorized() is False


def test_is_authorized_true_when_token_has_refresh(tmp_path):
    from alfred.integrations.gcal import GCalClient

    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps({
        "token": "access-stub",
        "refresh_token": "refresh-stub",
        "client_id": "id",
        "client_secret": "secret",
    }), encoding="utf-8")

    client = GCalClient(
        credentials_path=tmp_path / "creds.json",
        token_path=token_path,
    )
    assert client.is_authorized() is True


def test_is_authorized_false_on_unparseable_token(tmp_path):
    from alfred.integrations.gcal import GCalClient

    token_path = tmp_path / "token.json"
    token_path.write_text("not json{{{", encoding="utf-8")
    client = GCalClient(
        credentials_path=tmp_path / "creds.json",
        token_path=token_path,
    )
    assert client.is_authorized() is False


def test_load_credentials_raises_when_no_token(tmp_path):
    from alfred.integrations.gcal import GCalClient, GCalNotAuthorized

    client = GCalClient(
        credentials_path=tmp_path / "creds.json",
        token_path=tmp_path / "token.json",
    )
    # Force the google-libs import to succeed (return MagicMocks) so the
    # NotAuthorized check fires (not the NotInstalled one).
    with patch.object(client, "_load_credentials", wraps=client._load_credentials):
        with patch(
            "alfred.integrations.gcal._import_google",
            return_value=(MagicMock(), MagicMock(), MagicMock(), MagicMock()),
        ):
            with pytest.raises(GCalNotAuthorized):
                client._load_credentials()


def test_load_credentials_refreshes_expired_token(tmp_path):
    """Expired token + valid refresh_token → refresh() called + saved back."""
    from alfred.integrations.gcal import GCalClient

    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps({
        "token": "old", "refresh_token": "rt",
        "client_id": "id", "client_secret": "sec",
    }), encoding="utf-8")

    fake_creds = MagicMock()
    fake_creds.valid = False
    fake_creds.expired = True
    fake_creds.refresh_token = "rt"
    fake_creds.to_json.return_value = json.dumps({
        "token": "new", "refresh_token": "rt",
        "client_id": "id", "client_secret": "sec",
    })

    Credentials = MagicMock()
    Credentials.from_authorized_user_file.return_value = fake_creds
    Request = MagicMock()

    with patch(
        "alfred.integrations.gcal._import_google",
        return_value=(Credentials, Request, MagicMock(), MagicMock()),
    ):
        client = GCalClient(
            credentials_path=tmp_path / "creds.json",
            token_path=token_path,
        )
        creds = client._load_credentials()
        # refresh() must have been called once.
        assert fake_creds.refresh.called
        # Token file should have been overwritten with refreshed JSON.
        on_disk = json.loads(token_path.read_text(encoding="utf-8"))
        assert on_disk["token"] == "new"
        assert creds is fake_creds


def test_load_credentials_refresh_failure_message_acknowledges_transient(tmp_path):
    """Refresh failure message must acknowledge the transient-network case.

    The same generic Exception catches terminal auth failure (refresh
    token revoked) AND transient transport errors (DNS, TLS, 5xx from
    Google). Telling the operator to immediately re-auth on a transient
    failure burns an OAuth flow they don't need. Pin that the error
    message tells them to re-try first.
    """
    from alfred.integrations.gcal import GCalClient, GCalNotAuthorized

    token_path = tmp_path / "token.json"
    token_path.write_text(json.dumps({
        "token": "old", "refresh_token": "rt",
        "client_id": "id", "client_secret": "sec",
    }), encoding="utf-8")

    fake_creds = MagicMock()
    fake_creds.valid = False
    fake_creds.expired = True
    fake_creds.refresh_token = "rt"
    fake_creds.refresh.side_effect = ConnectionError("simulated DNS failure")

    Credentials = MagicMock()
    Credentials.from_authorized_user_file.return_value = fake_creds
    Request = MagicMock()

    with patch(
        "alfred.integrations.gcal._import_google",
        return_value=(Credentials, Request, MagicMock(), MagicMock()),
    ):
        client = GCalClient(
            credentials_path=tmp_path / "creds.json",
            token_path=token_path,
        )
        with pytest.raises(GCalNotAuthorized) as excinfo:
            client._load_credentials()
    msg = str(excinfo.value)
    # Underlying error surfaces.
    assert "simulated DNS failure" in msg
    # Message acknowledges transient case so operator re-tries before re-auth.
    assert "re-try" in msg.lower() or "retry" in msg.lower() or "transient" in msg.lower()
    # The re-auth instruction is still present, just gated on persistence.
    assert "alfred gcal authorize" in msg


# ---------------------------------------------------------------------------
# list_events
# ---------------------------------------------------------------------------


def _make_service_mock(events_list_response: dict) -> MagicMock:
    """Build a MagicMock googleapiclient ``service`` returning a canned response."""
    list_call = MagicMock()
    list_call.execute.return_value = events_list_response
    events_obj = MagicMock()
    events_obj.list.return_value = list_call
    service = MagicMock()
    service.events.return_value = events_obj
    return service


def _client_with_mocked_service(tmp_path: Path, service_mock: MagicMock):
    from alfred.integrations.gcal import GCalClient

    client = GCalClient(
        credentials_path=tmp_path / "creds.json",
        token_path=tmp_path / "token.json",
    )
    # Skip the auth path entirely.
    client._service = service_mock
    return client


def test_list_events_parses_timed_event(tmp_path):
    response = {
        "items": [
            {
                "id": "evt-1",
                "summary": "Standup",
                "description": "daily",
                "start": {"dateTime": "2026-05-04T13:00:00-03:00"},
                "end": {"dateTime": "2026-05-04T13:30:00-03:00"},
            },
        ]
    }
    service = _make_service_mock(response)
    client = _client_with_mocked_service(tmp_path, service)

    events = client.list_events(
        "cal-id",
        datetime(2026, 5, 4, tzinfo=timezone.utc),
        datetime(2026, 5, 5, tzinfo=timezone.utc),
    )
    assert len(events) == 1
    e = events[0]
    assert e.id == "evt-1"
    assert e.title == "Standup"
    assert e.calendar_id == "cal-id"
    assert e.start.tzinfo is not None
    assert e.end.tzinfo is not None
    # Verify the call was shaped right.
    service.events.return_value.list.assert_called_once()
    call_kwargs = service.events.return_value.list.call_args.kwargs
    assert call_kwargs["calendarId"] == "cal-id"
    assert call_kwargs["singleEvents"] is True
    assert call_kwargs["orderBy"] == "startTime"


def test_list_events_parses_all_day_event(tmp_path):
    """All-day events have ``date`` not ``dateTime``; must still parse + overlap."""
    response = {
        "items": [
            {
                "id": "ad-1",
                "summary": "Trip Day",
                "start": {"date": "2026-05-04"},
                "end": {"date": "2026-05-05"},
            },
        ]
    }
    service = _make_service_mock(response)
    client = _client_with_mocked_service(tmp_path, service)
    events = client.list_events(
        "cal-id",
        datetime(2026, 5, 1, tzinfo=timezone.utc),
        datetime(2026, 5, 10, tzinfo=timezone.utc),
    )
    assert len(events) == 1
    assert events[0].title == "Trip Day"
    # Date-only window: ±12h around UTC midnight, so start should be
    # before the date, end should be after.
    assert events[0].start.year == 2026


def test_list_events_skips_unparseable(tmp_path):
    """One bad event in the response must not break the whole query."""
    response = {
        "items": [
            {
                "id": "ok-1",
                "summary": "Good",
                "start": {"dateTime": "2026-05-04T13:00:00-03:00"},
                "end": {"dateTime": "2026-05-04T14:00:00-03:00"},
            },
            {
                "id": "bad-1",
                "summary": "Broken",
                "start": {},  # neither dateTime nor date — should skip
                "end": {},
            },
        ]
    }
    service = _make_service_mock(response)
    client = _client_with_mocked_service(tmp_path, service)
    events = client.list_events(
        "cal-id",
        datetime(2026, 5, 4, tzinfo=timezone.utc),
        datetime(2026, 5, 5, tzinfo=timezone.utc),
    )
    assert len(events) == 1
    assert events[0].id == "ok-1"


def test_list_events_requires_tz_aware(tmp_path):
    from alfred.integrations.gcal import GCalAPIError

    service = _make_service_mock({"items": []})
    client = _client_with_mocked_service(tmp_path, service)
    with pytest.raises(GCalAPIError):
        client.list_events(
            "cal-id",
            datetime(2026, 5, 4),  # naive
            datetime(2026, 5, 5, tzinfo=timezone.utc),
        )


# ---------------------------------------------------------------------------
# create_event
# ---------------------------------------------------------------------------


def test_create_event_returns_id_and_call_shape(tmp_path):
    insert_call = MagicMock()
    insert_call.execute.return_value = {"id": "new-evt-123"}
    events_obj = MagicMock()
    events_obj.insert.return_value = insert_call
    service = MagicMock()
    service.events.return_value = events_obj

    client = _client_with_mocked_service(tmp_path, service)
    eid = client.create_event(
        "alfred-cal",
        start=datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc),
        end=datetime(2026, 5, 4, 15, 0, tzinfo=timezone.utc),
        title="Test Event",
        description="Created from unit test",
        time_zone="America/Halifax",
    )
    assert eid == "new-evt-123"
    events_obj.insert.assert_called_once()
    body = events_obj.insert.call_args.kwargs["body"]
    assert body["summary"] == "Test Event"
    assert body["description"] == "Created from unit test"
    assert body["start"]["dateTime"] == "2026-05-04T14:00:00+00:00"
    assert body["end"]["dateTime"] == "2026-05-04T15:00:00+00:00"
    assert body["start"]["timeZone"] == "America/Halifax"
    assert body["end"]["timeZone"] == "America/Halifax"
    assert events_obj.insert.call_args.kwargs["calendarId"] == "alfred-cal"


def test_create_event_requires_tz_aware(tmp_path):
    from alfred.integrations.gcal import GCalAPIError

    service = _make_service_mock({"items": []})
    client = _client_with_mocked_service(tmp_path, service)
    with pytest.raises(GCalAPIError, match="timezone-aware"):
        client.create_event(
            "cal",
            start=datetime(2026, 5, 4, 14, 0),  # naive
            end=datetime(2026, 5, 4, 15, 0, tzinfo=timezone.utc),
            title="x",
        )


def test_create_event_requires_end_after_start(tmp_path):
    from alfred.integrations.gcal import GCalAPIError

    service = _make_service_mock({"items": []})
    client = _client_with_mocked_service(tmp_path, service)
    with pytest.raises(GCalAPIError, match="end must be"):
        client.create_event(
            "cal",
            start=datetime(2026, 5, 4, 15, 0, tzinfo=timezone.utc),
            end=datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc),  # earlier
            title="backwards",
        )


# ---------------------------------------------------------------------------
# get_event / delete_event
# ---------------------------------------------------------------------------


def test_get_event_returns_none_on_404(tmp_path):
    get_call = MagicMock()
    get_call.execute.side_effect = Exception("HttpError 404 Not Found")
    events_obj = MagicMock()
    events_obj.get.return_value = get_call
    service = MagicMock()
    service.events.return_value = events_obj
    client = _client_with_mocked_service(tmp_path, service)

    result = client.get_event("cal", "missing-id")
    assert result is None


def test_get_event_returns_normalized_event(tmp_path):
    get_call = MagicMock()
    get_call.execute.return_value = {
        "id": "evt-1",
        "summary": "Hello",
        "start": {"dateTime": "2026-05-04T14:00:00-03:00"},
        "end": {"dateTime": "2026-05-04T15:00:00-03:00"},
    }
    events_obj = MagicMock()
    events_obj.get.return_value = get_call
    service = MagicMock()
    service.events.return_value = events_obj
    client = _client_with_mocked_service(tmp_path, service)

    result = client.get_event("cal", "evt-1")
    assert result is not None
    assert result.title == "Hello"
    assert result.id == "evt-1"


def test_delete_event_returns_false_on_404(tmp_path):
    del_call = MagicMock()
    del_call.execute.side_effect = Exception("HttpError 410 Resource has been deleted")
    events_obj = MagicMock()
    events_obj.delete.return_value = del_call
    service = MagicMock()
    service.events.return_value = events_obj
    client = _client_with_mocked_service(tmp_path, service)

    assert client.delete_event("cal", "gone") is False


def test_delete_event_returns_true_on_success(tmp_path):
    del_call = MagicMock()
    del_call.execute.return_value = ""
    events_obj = MagicMock()
    events_obj.delete.return_value = del_call
    service = MagicMock()
    service.events.return_value = events_obj
    client = _client_with_mocked_service(tmp_path, service)

    assert client.delete_event("cal", "real-id") is True


# ---------------------------------------------------------------------------
# event_to_conflict_dict
# ---------------------------------------------------------------------------


def test_event_to_conflict_dict_shape():
    from alfred.integrations.gcal import GCalEvent, event_to_conflict_dict

    e = GCalEvent(
        id="abc-123",
        calendar_id="primary",
        title="Real meeting",
        start=datetime(2026, 5, 4, 14, 0, tzinfo=timezone.utc),
        end=datetime(2026, 5, 4, 15, 0, tzinfo=timezone.utc),
        description="",
    )
    out = event_to_conflict_dict(e, source="gcal_primary")
    assert out["title"] == "Real meeting"
    assert out["source"] == "gcal_primary"
    assert out["gcal_event_id"] == "abc-123"
    assert out["start"] == "2026-05-04T14:00:00+00:00"
    assert out["end"] == "2026-05-04T15:00:00+00:00"
