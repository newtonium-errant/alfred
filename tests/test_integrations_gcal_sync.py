"""Unit tests for the extracted ``alfred.integrations.gcal_sync`` module.

The functions here are pure over their args — no aiohttp request, no
hidden globals — so they're trivially testable with a mock GCalClient
and a real on-disk frontmatter file.

Coverage:
  * ``classify_gcal_error`` returns stable codes for each exception type
  * ``sync_event_create_to_gcal``: happy path writes back gcal_event_id +
    gcal_calendar; failure preserves the file; gcal-disabled is a silent
    skip; sentinel-on warns; calendar_id_missing returns structured error
  * ``sync_event_update_to_gcal``: patches with only the changed fields;
    no-op when gcal_event_id is empty; stale-id (404) → structured error;
    propagates time_zone when configured
  * ``sync_event_delete_to_gcal``: deletes when gcal_event_id present;
    no-op when absent; gcal-disabled silent skip; treats 404 as success
  * ``GCalClient.update_event``: patch body shape; returns None on 404;
    requires tz-aware; rejects start>=end; no-op on all-None kwargs
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import frontmatter
import pytest


# ---------------------------------------------------------------------------
# classify_gcal_error
# ---------------------------------------------------------------------------


def test_classify_gcal_error_codes():
    from alfred.integrations.gcal import (
        GCalAPIError,
        GCalNotAuthorized,
        GCalNotInstalled,
    )
    from alfred.integrations.gcal_sync import classify_gcal_error

    assert classify_gcal_error(GCalNotAuthorized("x")) == "auth_failed"
    assert classify_gcal_error(GCalNotInstalled("x")) == "missing_dependency"
    assert classify_gcal_error(GCalAPIError("x")) == "api_error"
    assert classify_gcal_error(RuntimeError("x")) == "unknown"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_config(
    *,
    enabled: bool = True,
    alfred_id: str = "alfred-cal@group.calendar.google.com",
    label: str = "alfred",
    time_zone: str = "",
):
    from alfred.integrations.gcal_config import GCalConfig
    return GCalConfig(
        enabled=enabled,
        alfred_calendar_id=alfred_id,
        alfred_calendar_label=label,
        default_time_zone=time_zone,
    )


def _seed_event_file(tmp_path: Path, *, fm: dict, body: str = "body\n") -> Path:
    """Write a minimal event/<name>.md and return its absolute path."""
    event_dir = tmp_path / "event"
    event_dir.mkdir(exist_ok=True)
    file_path = event_dir / f"{fm.get('name', 'evt')}.md"
    post = frontmatter.Post(body, **fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return file_path


# ---------------------------------------------------------------------------
# sync_event_create_to_gcal
# ---------------------------------------------------------------------------


def test_sync_create_disabled_config_skips(tmp_path):
    from alfred.integrations.gcal_sync import sync_event_create_to_gcal

    file_path = _seed_event_file(tmp_path, fm={"type": "event", "name": "x"})
    client = MagicMock()
    config = _make_config(enabled=False)

    result = sync_event_create_to_gcal(
        client=client, config=config, intended_on=False,
        file_path=file_path,
        title="x", description="",
        start_dt=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
        end_dt=datetime(2026, 6, 1, 15, tzinfo=timezone.utc),
        correlation_id="t-disabled",
    )
    assert result == {}
    client.create_event.assert_not_called()


def test_sync_create_intended_on_warns(tmp_path, caplog):
    """Sentinel-aware skip — operator wanted it on but client is None."""
    import logging
    from alfred.integrations.gcal_sync import sync_event_create_to_gcal
    from structlog.testing import capture_logs

    file_path = _seed_event_file(tmp_path, fm={"type": "event", "name": "x"})
    config = _make_config(enabled=False)
    with capture_logs() as captured:
        result = sync_event_create_to_gcal(
            client=None, config=config, intended_on=True,
            file_path=file_path,
            title="x", description="",
            start_dt=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
            end_dt=datetime(2026, 6, 1, 15, tzinfo=timezone.utc),
            correlation_id="t-intended-on",
        )
    assert result == {}
    warnings = [
        c for c in captured
        if c.get("log_level") == "warning"
        and "skipped_but_intended_on" in c.get("event", "")
    ]
    assert len(warnings) == 1
    assert warnings[0]["op"] == "create"


def test_sync_create_calendar_id_missing(tmp_path):
    from alfred.integrations.gcal_sync import sync_event_create_to_gcal

    file_path = _seed_event_file(tmp_path, fm={"type": "event", "name": "x"})
    client = MagicMock()
    config = _make_config(alfred_id="")  # configured but no ID

    result = sync_event_create_to_gcal(
        client=client, config=config,
        file_path=file_path,
        title="x", description="",
        start_dt=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
        end_dt=datetime(2026, 6, 1, 15, tzinfo=timezone.utc),
        correlation_id="t-no-id",
    )
    assert "error" in result
    assert result["error"]["code"] == "calendar_id_missing"
    client.create_event.assert_not_called()


def test_sync_create_happy_path_writes_back(tmp_path):
    from alfred.integrations.gcal_sync import sync_event_create_to_gcal

    file_path = _seed_event_file(
        tmp_path,
        fm={"type": "event", "name": "Coaching"},
    )
    client = MagicMock()
    client.create_event.return_value = "gcal-event-id-42"
    config = _make_config(label="alfred")

    result = sync_event_create_to_gcal(
        client=client, config=config,
        file_path=file_path,
        title="Coaching", description="weekly check-in",
        start_dt=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
        end_dt=datetime(2026, 6, 1, 15, tzinfo=timezone.utc),
        correlation_id="t-happy",
    )
    assert result["event_id"] == "gcal-event-id-42"
    assert result["calendar_label"] == "alfred"

    # Vault frontmatter has the writeback.
    fm = frontmatter.load(str(file_path))
    assert fm["gcal_event_id"] == "gcal-event-id-42"
    assert fm["gcal_calendar"] == "alfred"

    # API call shape — title / description / tz-aware datetimes.
    client.create_event.assert_called_once()
    kwargs = client.create_event.call_args.kwargs
    assert kwargs["title"] == "Coaching"
    assert kwargs["description"] == "weekly check-in"
    # No time_zone since config.default_time_zone is empty.
    assert "time_zone" not in kwargs


def test_sync_create_propagates_time_zone(tmp_path):
    from alfred.integrations.gcal_sync import sync_event_create_to_gcal

    file_path = _seed_event_file(tmp_path, fm={"type": "event", "name": "x"})
    client = MagicMock()
    client.create_event.return_value = "id-1"
    config = _make_config(time_zone="America/Halifax")

    sync_event_create_to_gcal(
        client=client, config=config,
        file_path=file_path,
        title="x", description="",
        start_dt=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
        end_dt=datetime(2026, 6, 1, 15, tzinfo=timezone.utc),
        correlation_id="t-tz",
    )
    kwargs = client.create_event.call_args.kwargs
    assert kwargs["time_zone"] == "America/Halifax"


def test_sync_create_uses_custom_label(tmp_path):
    """V.E.R.A.-style label flows through to vault + result."""
    from alfred.integrations.gcal_sync import sync_event_create_to_gcal

    file_path = _seed_event_file(tmp_path, fm={"type": "event", "name": "x"})
    client = MagicMock()
    client.create_event.return_value = "rrts-id-1"
    config = _make_config(label="rrts")

    result = sync_event_create_to_gcal(
        client=client, config=config,
        file_path=file_path,
        title="x", description="",
        start_dt=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
        end_dt=datetime(2026, 6, 1, 15, tzinfo=timezone.utc),
        correlation_id="t-label",
    )
    assert result["calendar_label"] == "rrts"
    fm = frontmatter.load(str(file_path))
    assert fm["gcal_calendar"] == "rrts"


def test_sync_create_api_failure_classified(tmp_path):
    from alfred.integrations.gcal import GCalAPIError
    from alfred.integrations.gcal_sync import sync_event_create_to_gcal

    file_path = _seed_event_file(tmp_path, fm={"type": "event", "name": "x"})
    client = MagicMock()
    client.create_event.side_effect = GCalAPIError("quota exceeded")
    config = _make_config()

    result = sync_event_create_to_gcal(
        client=client, config=config,
        file_path=file_path,
        title="x", description="",
        start_dt=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
        end_dt=datetime(2026, 6, 1, 15, tzinfo=timezone.utc),
        correlation_id="t-quota",
    )
    assert result["error"]["code"] == "api_error"
    assert "quota" in result["error"]["detail"].lower()
    # Vault frontmatter unchanged — no gcal_event_id.
    fm = frontmatter.load(str(file_path))
    assert "gcal_event_id" not in fm.metadata


def test_sync_create_writeback_failure_is_soft(tmp_path, monkeypatch):
    """Frontmatter rewrite explosion → still returns success with event_id."""
    from alfred.integrations import gcal_sync as sync_mod
    from alfred.integrations.gcal_sync import sync_event_create_to_gcal

    file_path = _seed_event_file(tmp_path, fm={"type": "event", "name": "x"})
    client = MagicMock()
    client.create_event.return_value = "soft-id-1"
    config = _make_config()

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated dumps failure")

    monkeypatch.setattr(sync_mod.frontmatter, "dumps", _boom)

    result = sync_event_create_to_gcal(
        client=client, config=config,
        file_path=file_path,
        title="x", description="",
        start_dt=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
        end_dt=datetime(2026, 6, 1, 15, tzinfo=timezone.utc),
        correlation_id="t-soft-fail",
    )
    # Soft fail — sync succeeded, only writeback broke.
    assert result["event_id"] == "soft-id-1"
    assert result["calendar_label"] == "alfred"


# ---------------------------------------------------------------------------
# sync_event_update_to_gcal
# ---------------------------------------------------------------------------


def test_sync_update_no_id_is_noop():
    from alfred.integrations.gcal_sync import sync_event_update_to_gcal

    client = MagicMock()
    config = _make_config()
    result = sync_event_update_to_gcal(
        client=client, config=config,
        gcal_event_id="",
        title="new title",
        correlation_id="t-no-id",
    )
    assert result == {"noop": "no_gcal_event_id"}
    client.update_event.assert_not_called()


def test_sync_update_disabled_skips():
    from alfred.integrations.gcal_sync import sync_event_update_to_gcal

    client = MagicMock()
    config = _make_config(enabled=False)
    result = sync_event_update_to_gcal(
        client=client, config=config,
        gcal_event_id="abc", title="new",
    )
    assert result == {}
    client.update_event.assert_not_called()


def test_sync_update_happy_path():
    from alfred.integrations.gcal_sync import sync_event_update_to_gcal

    client = MagicMock()
    # update_event returns a GCalEvent on success; we just need
    # truthiness for the sync path's return-shape assertion.
    client.update_event.return_value = MagicMock(spec=["id"])
    config = _make_config()

    result = sync_event_update_to_gcal(
        client=client, config=config,
        gcal_event_id="gcal-id-7",
        title="Updated title",
        start_dt=datetime(2026, 6, 1, 15, tzinfo=timezone.utc),
        end_dt=datetime(2026, 6, 1, 16, tzinfo=timezone.utc),
    )
    assert result["event_id"] == "gcal-id-7"
    assert result["calendar_label"] == "alfred"
    client.update_event.assert_called_once()
    args = client.update_event.call_args.args
    kwargs = client.update_event.call_args.kwargs
    assert args == ("alfred-cal@group.calendar.google.com", "gcal-id-7")
    assert kwargs["title"] == "Updated title"
    assert kwargs["start"] == datetime(2026, 6, 1, 15, tzinfo=timezone.utc)
    assert kwargs["end"] == datetime(2026, 6, 1, 16, tzinfo=timezone.utc)
    # description not patched — None means leave-as-is.
    assert "description" not in kwargs


def test_sync_update_only_sends_changed_fields():
    from alfred.integrations.gcal_sync import sync_event_update_to_gcal

    client = MagicMock()
    client.update_event.return_value = MagicMock()
    config = _make_config()

    sync_event_update_to_gcal(
        client=client, config=config,
        gcal_event_id="id-1",
        # Only description changes; title/start/end stay None.
        description="new note",
    )
    kwargs = client.update_event.call_args.kwargs
    assert kwargs == {"description": "new note"}


def test_sync_update_stale_id_returns_structured_error():
    """update_event returning None (404) → structured stale_gcal_id error."""
    from alfred.integrations.gcal_sync import sync_event_update_to_gcal

    client = MagicMock()
    client.update_event.return_value = None  # signals 404
    config = _make_config()

    result = sync_event_update_to_gcal(
        client=client, config=config,
        gcal_event_id="ghost-id",
        title="late patch",
    )
    assert result["error"]["code"] == "stale_gcal_id"
    assert "ghost-id" in result["error"]["detail"]


def test_sync_update_propagates_time_zone():
    from alfred.integrations.gcal_sync import sync_event_update_to_gcal

    client = MagicMock()
    client.update_event.return_value = MagicMock()
    config = _make_config(time_zone="America/Halifax")

    sync_event_update_to_gcal(
        client=client, config=config,
        gcal_event_id="id-1",
        start_dt=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
    )
    kwargs = client.update_event.call_args.kwargs
    assert kwargs["time_zone"] == "America/Halifax"


def test_sync_update_api_failure_classified():
    from alfred.integrations.gcal import GCalAPIError
    from alfred.integrations.gcal_sync import sync_event_update_to_gcal

    client = MagicMock()
    client.update_event.side_effect = GCalAPIError("quota")
    config = _make_config()

    result = sync_event_update_to_gcal(
        client=client, config=config,
        gcal_event_id="id-1",
        title="x",
    )
    assert result["error"]["code"] == "api_error"


# ---------------------------------------------------------------------------
# sync_event_delete_to_gcal
# ---------------------------------------------------------------------------


def test_sync_delete_no_id_is_noop():
    from alfred.integrations.gcal_sync import sync_event_delete_to_gcal

    client = MagicMock()
    config = _make_config()
    result = sync_event_delete_to_gcal(
        client=client, config=config,
        gcal_event_id="",
    )
    assert result == {"noop": "no_gcal_event_id"}
    client.delete_event.assert_not_called()


def test_sync_delete_disabled_skips():
    from alfred.integrations.gcal_sync import sync_event_delete_to_gcal

    client = MagicMock()
    config = _make_config(enabled=False)
    result = sync_event_delete_to_gcal(
        client=client, config=config,
        gcal_event_id="abc",
    )
    assert result == {}
    client.delete_event.assert_not_called()


def test_sync_delete_happy_path():
    from alfred.integrations.gcal_sync import sync_event_delete_to_gcal

    client = MagicMock()
    client.delete_event.return_value = True
    config = _make_config()

    result = sync_event_delete_to_gcal(
        client=client, config=config,
        gcal_event_id="bye-id-1",
    )
    assert result == {"deleted": True, "event_id": "bye-id-1"}
    client.delete_event.assert_called_once_with(
        "alfred-cal@group.calendar.google.com", "bye-id-1",
    )


def test_sync_delete_already_gone_is_success():
    """delete_event returning False (404) → still treated as success."""
    from alfred.integrations.gcal_sync import sync_event_delete_to_gcal

    client = MagicMock()
    client.delete_event.return_value = False
    config = _make_config()

    result = sync_event_delete_to_gcal(
        client=client, config=config,
        gcal_event_id="ghost-id",
    )
    # Vault delete already happened; the GCal event being already-gone
    # is the same outcome as a successful delete.
    assert result == {"deleted": True, "event_id": "ghost-id"}


def test_sync_delete_api_failure_classified():
    from alfred.integrations.gcal import GCalAPIError
    from alfred.integrations.gcal_sync import sync_event_delete_to_gcal

    client = MagicMock()
    client.delete_event.side_effect = GCalAPIError("api boom")
    config = _make_config()

    result = sync_event_delete_to_gcal(
        client=client, config=config,
        gcal_event_id="id-1",
    )
    assert result["error"]["code"] == "api_error"


# ---------------------------------------------------------------------------
# GCalClient.update_event
# ---------------------------------------------------------------------------


def _client_with_mock_service(tmp_path: Path, service_mock: MagicMock):
    from alfred.integrations.gcal import GCalClient
    client = GCalClient(
        credentials_path=tmp_path / "creds.json",
        token_path=tmp_path / "token.json",
    )
    client._service = service_mock
    return client


def test_update_event_patch_call_shape(tmp_path):
    patch_call = MagicMock()
    patch_call.execute.return_value = {
        "id": "id-1",
        "summary": "patched title",
        "start": {"dateTime": "2026-06-01T14:00:00+00:00"},
        "end": {"dateTime": "2026-06-01T15:00:00+00:00"},
    }
    events_obj = MagicMock()
    events_obj.patch.return_value = patch_call
    service = MagicMock()
    service.events.return_value = events_obj
    client = _client_with_mock_service(tmp_path, service)

    result = client.update_event(
        "cal-id", "id-1",
        title="patched title",
        start=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
        end=datetime(2026, 6, 1, 15, tzinfo=timezone.utc),
        time_zone="America/Halifax",
    )
    assert result is not None
    assert result.id == "id-1"
    assert result.title == "patched title"
    events_obj.patch.assert_called_once()
    body = events_obj.patch.call_args.kwargs["body"]
    assert body["summary"] == "patched title"
    assert body["start"]["dateTime"] == "2026-06-01T14:00:00+00:00"
    assert body["start"]["timeZone"] == "America/Halifax"
    assert body["end"]["timeZone"] == "America/Halifax"
    # description not patched — None means don't include in body.
    assert "description" not in body


def test_update_event_only_sends_changed(tmp_path):
    """Pass only ``title`` → body has only ``summary``."""
    patch_call = MagicMock()
    patch_call.execute.return_value = {
        "id": "id-1",
        "summary": "new",
        "start": {"dateTime": "2026-06-01T14:00:00+00:00"},
        "end": {"dateTime": "2026-06-01T15:00:00+00:00"},
    }
    events_obj = MagicMock()
    events_obj.patch.return_value = patch_call
    service = MagicMock()
    service.events.return_value = events_obj
    client = _client_with_mock_service(tmp_path, service)

    client.update_event("cal", "id-1", title="new")
    body = events_obj.patch.call_args.kwargs["body"]
    assert body == {"summary": "new"}


def test_update_event_404_returns_none(tmp_path):
    patch_call = MagicMock()
    patch_call.execute.side_effect = Exception("HttpError 404 Not Found")
    events_obj = MagicMock()
    events_obj.patch.return_value = patch_call
    service = MagicMock()
    service.events.return_value = events_obj
    client = _client_with_mock_service(tmp_path, service)

    result = client.update_event("cal", "ghost", title="x")
    assert result is None


def test_update_event_requires_tz_aware(tmp_path):
    from alfred.integrations.gcal import GCalAPIError

    service = MagicMock()
    client = _client_with_mock_service(tmp_path, service)
    with pytest.raises(GCalAPIError, match="timezone-aware"):
        client.update_event(
            "cal", "id",
            start=datetime(2026, 6, 1, 14),  # naive
        )


def test_update_event_rejects_end_before_start(tmp_path):
    from alfred.integrations.gcal import GCalAPIError

    service = MagicMock()
    client = _client_with_mock_service(tmp_path, service)
    with pytest.raises(GCalAPIError, match="end must be"):
        client.update_event(
            "cal", "id",
            start=datetime(2026, 6, 1, 15, tzinfo=timezone.utc),
            end=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
        )


def test_update_event_all_none_is_noop_get(tmp_path):
    """All-None kwargs → no patch call; falls back to get_event."""
    patch_call = MagicMock()
    get_call = MagicMock()
    get_call.execute.return_value = {
        "id": "id-1",
        "summary": "unchanged",
        "start": {"dateTime": "2026-06-01T14:00:00+00:00"},
        "end": {"dateTime": "2026-06-01T15:00:00+00:00"},
    }
    events_obj = MagicMock()
    events_obj.patch.return_value = patch_call
    events_obj.get.return_value = get_call
    service = MagicMock()
    service.events.return_value = events_obj
    client = _client_with_mock_service(tmp_path, service)

    result = client.update_event("cal", "id-1")
    # No patch call attempted.
    events_obj.patch.assert_not_called()
    # Fell back to get for current state.
    events_obj.get.assert_called_once()
    assert result is not None
    assert result.title == "unchanged"
