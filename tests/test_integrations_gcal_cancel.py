"""Unit tests for ``sync_event_cancellation_to_gcal`` — the soft-cancel
GCal mirror that fires when ``vault_edit`` sets ``status: cancelled``
on an event record.

Distinct from ``sync_event_delete_to_gcal`` (the vault_delete /
hard-delete hook): cancel keeps the vault record alive, only the
GCal mirror is removed (or status-patched if
``gcal_keep_on_cancel: true``). Cancel is also responsible for
clearing ``gcal_event_id`` from the vault record's frontmatter on the
delete path so subsequent re-confirms start fresh.

Coverage:
  * No gcal_event_id → noop, no GCal call (vault-only cancellation)
  * Disabled config → silent skip
  * Default delete path: client.delete_event called, gcal_event_id +
    gcal_calendar cleared from vault frontmatter, log emitted
  * keep_on_cancel=True path: client.update_event called with
    status="cancelled", gcal_event_id retained, log emitted
  * Delete returns False (404) → still treated as success
  * Delete API error (500) → vault preserved, gcal_event_id retained,
    structured error code, no partial mutation
  * keep path stale_id (404 on patch) → structured error
  * keep path API error → classified
  * Bonus regression: the operator-caught Ben Tuesday case (event
    with gcal_event_id, vault_edit sets status=cancelled, default
    behavior expected → delete + clear)

Log assertions use ``structlog.testing.capture_logs`` per
``feedback_structlog_assertion_patterns.md`` — sync code wrapped
through the structlog test capture works reliably across modules.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import frontmatter
import pytest
import structlog


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


def _seed_event_file(
    tmp_path: Path,
    *,
    fm: dict,
    body: str = "body\n",
) -> Path:
    """Write a minimal event/<name>.md and return its absolute path."""
    event_dir = tmp_path / "event"
    event_dir.mkdir(exist_ok=True)
    file_path = event_dir / f"{fm.get('name', 'evt')}.md"
    post = frontmatter.Post(body, **fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return file_path


# ---------------------------------------------------------------------------
# Noop + disabled paths
# ---------------------------------------------------------------------------


def test_sync_cancel_no_gcal_event_id_is_noop(tmp_path):
    """vault_edit cancel + NO gcal_event_id → no GCal call, no error.

    This is the vault-only cancellation case — record was never
    synced, so there's nothing to mirror. Common for events that
    Andrew creates and immediately cancels before the daemon syncs.
    """
    from alfred.integrations.gcal_sync import sync_event_cancellation_to_gcal

    file_path = _seed_event_file(
        tmp_path,
        fm={"type": "event", "name": "Never Synced", "status": "cancelled"},
    )
    client = MagicMock()
    config = _make_config()
    result = sync_event_cancellation_to_gcal(
        client=client, config=config,
        file_path=file_path,
        gcal_event_id="",
    )
    assert result == {"noop": "no_gcal_event_id"}
    client.delete_event.assert_not_called()
    client.update_event.assert_not_called()


def test_sync_cancel_disabled_config_skips(tmp_path):
    """gcal.enabled=False → silent skip ({}). No client calls."""
    from alfred.integrations.gcal_sync import sync_event_cancellation_to_gcal

    file_path = _seed_event_file(
        tmp_path,
        fm={"type": "event", "name": "x", "status": "cancelled"},
    )
    client = MagicMock()
    config = _make_config(enabled=False)
    result = sync_event_cancellation_to_gcal(
        client=client, config=config,
        file_path=file_path,
        gcal_event_id="abc",
    )
    assert result == {}
    client.delete_event.assert_not_called()
    client.update_event.assert_not_called()


# ---------------------------------------------------------------------------
# Default delete path — GCal event removed, vault gcal_event_id cleared
# ---------------------------------------------------------------------------


def test_sync_cancel_default_path_deletes_and_clears_id(tmp_path):
    """vault_edit cancel + has gcal_event_id + no keep flag → DELETE
    fires, gcal_event_id + gcal_calendar cleared from vault."""
    from alfred.integrations.gcal_sync import sync_event_cancellation_to_gcal

    file_path = _seed_event_file(
        tmp_path,
        fm={
            "type": "event",
            "name": "Call with Ben",
            "status": "cancelled",
            "gcal_event_id": "ben-tuesday-id-1",
            "gcal_calendar": "alfred",
        },
    )
    client = MagicMock()
    client.delete_event.return_value = True
    config = _make_config()

    with structlog.testing.capture_logs() as captured:
        result = sync_event_cancellation_to_gcal(
            client=client, config=config,
            file_path=file_path,
            gcal_event_id="ben-tuesday-id-1",
            keep_on_cancel=False,
        )

    # Result shape.
    assert result == {
        "cancelled": True,
        "event_id": "ben-tuesday-id-1",
        "path": "delete",
    }

    # GCal delete invoked with the right calendar.
    client.delete_event.assert_called_once_with(
        "alfred-cal@group.calendar.google.com", "ben-tuesday-id-1",
    )
    # No update call (we're on the delete path, not the keep path).
    client.update_event.assert_not_called()

    # Vault frontmatter cleared — gcal_event_id + gcal_calendar gone.
    post = frontmatter.load(str(file_path))
    assert "gcal_event_id" not in post
    assert "gcal_calendar" not in post
    # status: cancelled is preserved (the original vault_edit set it,
    # we never touch it).
    assert post["status"] == "cancelled"
    # Other fields preserved.
    assert post["type"] == "event"
    assert post["name"] == "Call with Ben"

    # Log emitted with structured fields the operator can grep.
    log_events = [e["event"] for e in captured]
    assert "gcal.sync_cancelled_via_delete" in log_events
    cancel_log = next(
        e for e in captured if e["event"] == "gcal.sync_cancelled_via_delete"
    )
    assert cancel_log["event_id"] == "ben-tuesday-id-1"
    assert cancel_log["calendar_id"] == "alfred-cal@group.calendar.google.com"


def test_sync_cancel_delete_404_treated_as_success(tmp_path):
    """delete_event returns False (404 — already gone) → still success.
    Same outcome from the operator's perspective: the GCal event is
    not on the calendar. ``gcal_event_id`` is still cleared so the
    vault record reflects the absence."""
    from alfred.integrations.gcal_sync import sync_event_cancellation_to_gcal

    file_path = _seed_event_file(
        tmp_path,
        fm={
            "type": "event",
            "name": "Ghost",
            "status": "cancelled",
            "gcal_event_id": "ghost-id",
        },
    )
    client = MagicMock()
    client.delete_event.return_value = False  # 404
    config = _make_config()

    result = sync_event_cancellation_to_gcal(
        client=client, config=config,
        file_path=file_path,
        gcal_event_id="ghost-id",
        keep_on_cancel=False,
    )
    assert result == {
        "cancelled": True,
        "event_id": "ghost-id",
        "path": "delete",
    }
    # Frontmatter still cleared even on 404.
    post = frontmatter.load(str(file_path))
    assert "gcal_event_id" not in post


def test_sync_cancel_delete_api_error_preserves_vault_state(tmp_path):
    """GCal delete throws GCalAPIError (e.g. 500) → structured error
    code returned, vault state UNCHANGED, gcal_event_id RETAINED.

    Critical: a partial mutation here (vault cleared but GCal still
    has the event) would leave the operator unable to retry — the
    vault would have lost the anchor needed to find the GCal event.
    """
    from alfred.integrations.gcal import GCalAPIError
    from alfred.integrations.gcal_sync import sync_event_cancellation_to_gcal

    file_path = _seed_event_file(
        tmp_path,
        fm={
            "type": "event",
            "name": "Server Down",
            "status": "cancelled",
            "gcal_event_id": "still-there-id",
            "gcal_calendar": "alfred",
        },
    )
    client = MagicMock()
    client.delete_event.side_effect = GCalAPIError("500 server error")
    config = _make_config()

    with structlog.testing.capture_logs() as captured:
        result = sync_event_cancellation_to_gcal(
            client=client, config=config,
            file_path=file_path,
            gcal_event_id="still-there-id",
            keep_on_cancel=False,
        )

    # Structured error returned.
    assert result["error"]["code"] == "api_error"
    assert "500 server error" in result["error"]["detail"]

    # Vault frontmatter UNCHANGED — gcal_event_id + gcal_calendar
    # preserved so a future retry still has the GCal anchor.
    post = frontmatter.load(str(file_path))
    assert post["gcal_event_id"] == "still-there-id"
    assert post["gcal_calendar"] == "alfred"

    # Failure log emitted with the right shape.
    failure_events = [
        e for e in captured
        if e["event"] == "gcal.sync_cancelled_via_delete_failed"
    ]
    assert len(failure_events) == 1
    assert failure_events[0]["error_code"] == "api_error"
    assert failure_events[0]["gcal_event_id"] == "still-there-id"

    # No success log.
    success_events = [
        e for e in captured if e["event"] == "gcal.sync_cancelled_via_delete"
    ]
    assert success_events == []


# ---------------------------------------------------------------------------
# keep_on_cancel=True path — patch with status=cancelled, retain ID
# ---------------------------------------------------------------------------


def test_sync_cancel_keep_on_cancel_patches_status_and_retains_id(tmp_path):
    """keep_on_cancel=True → update_event called with status='cancelled',
    gcal_event_id RETAINED on the vault record."""
    from alfred.integrations.gcal_sync import sync_event_cancellation_to_gcal

    file_path = _seed_event_file(
        tmp_path,
        fm={
            "type": "event",
            "name": "Visible Cancelled",
            "status": "cancelled",
            "gcal_event_id": "keep-me-id",
            "gcal_calendar": "alfred",
            "gcal_keep_on_cancel": True,
        },
    )
    client = MagicMock()
    # update_event returns a non-None GCalEvent on success — we mock
    # it as a MagicMock since we only care it was called and was not None.
    client.update_event.return_value = MagicMock()
    config = _make_config()

    with structlog.testing.capture_logs() as captured:
        result = sync_event_cancellation_to_gcal(
            client=client, config=config,
            file_path=file_path,
            gcal_event_id="keep-me-id",
            keep_on_cancel=True,
        )

    assert result == {
        "cancelled": True,
        "event_id": "keep-me-id",
        "path": "status_cancelled",
    }

    # update_event called with status=cancelled — no other fields.
    client.update_event.assert_called_once_with(
        "alfred-cal@group.calendar.google.com",
        "keep-me-id",
        status="cancelled",
    )
    # delete_event NOT called.
    client.delete_event.assert_not_called()

    # Vault frontmatter UNCHANGED — gcal_event_id retained.
    post = frontmatter.load(str(file_path))
    assert post["gcal_event_id"] == "keep-me-id"
    assert post["gcal_calendar"] == "alfred"
    assert post["gcal_keep_on_cancel"] is True

    # Structured log on the keep path.
    keep_events = [
        e for e in captured if e["event"] == "gcal.sync_cancelled_via_status"
    ]
    assert len(keep_events) == 1
    assert keep_events[0]["event_id"] == "keep-me-id"


def test_sync_cancel_keep_path_stale_id_returns_structured_error(tmp_path):
    """keep_on_cancel=True + GCal returns None on patch (404/410) →
    structured stale_gcal_id error. Vault keeps the stale ID for a
    future janitor sweep."""
    from alfred.integrations.gcal_sync import sync_event_cancellation_to_gcal

    file_path = _seed_event_file(
        tmp_path,
        fm={
            "type": "event",
            "name": "Patch Ghost",
            "status": "cancelled",
            "gcal_event_id": "ghost-keep-id",
            "gcal_keep_on_cancel": True,
        },
    )
    client = MagicMock()
    client.update_event.return_value = None  # 404/410
    config = _make_config()

    result = sync_event_cancellation_to_gcal(
        client=client, config=config,
        file_path=file_path,
        gcal_event_id="ghost-keep-id",
        keep_on_cancel=True,
    )
    assert result["error"]["code"] == "stale_gcal_id"
    # Vault stale ID retained.
    post = frontmatter.load(str(file_path))
    assert post["gcal_event_id"] == "ghost-keep-id"


def test_sync_cancel_keep_path_api_error_classified(tmp_path):
    """keep_on_cancel=True + update_event raises → classified error,
    vault preserved."""
    from alfred.integrations.gcal import GCalAPIError
    from alfred.integrations.gcal_sync import sync_event_cancellation_to_gcal

    file_path = _seed_event_file(
        tmp_path,
        fm={
            "type": "event",
            "name": "Patch Fail",
            "status": "cancelled",
            "gcal_event_id": "patch-fail-id",
            "gcal_keep_on_cancel": True,
        },
    )
    client = MagicMock()
    client.update_event.side_effect = GCalAPIError("api boom")
    config = _make_config()

    result = sync_event_cancellation_to_gcal(
        client=client, config=config,
        file_path=file_path,
        gcal_event_id="patch-fail-id",
        keep_on_cancel=True,
    )
    assert result["error"]["code"] == "api_error"
    # Vault preserved.
    post = frontmatter.load(str(file_path))
    assert post["gcal_event_id"] == "patch-fail-id"


# ---------------------------------------------------------------------------
# Bonus regression — the operator-caught Ben Tuesday case
# ---------------------------------------------------------------------------


def test_ben_tuesday_regression_delete_fires(tmp_path):
    """The original Salem QA case (operator-caught from screenshot):

      Andrew: 'Delete the call with Ben Tuesday'
      Salem (vault_edit): set status: cancelled on
        event/Call with Ben — scheduling discussion 2026-05-05.md

    Pre-fix: Salem's reply confessed 'sync hook handles creates and
    updates but not deletes'; the GCal event stayed on Andrew's
    calendar (visible to Jamie).

    Post-fix: vault_edit setting status=cancelled on a record with
    type:event and gcal_event_id present → delete fires automatically.
    No keep flag → default delete behavior (calendar reflects the
    cancellation by removing the event).
    """
    from alfred.integrations.gcal_sync import sync_event_cancellation_to_gcal

    file_path = _seed_event_file(
        tmp_path,
        fm={
            "type": "event",
            "name": "Call with Ben — scheduling discussion 2026-05-05",
            "status": "cancelled",
            "gcal_event_id": "ben-mcmillan-tuesday-event-id",
            "gcal_calendar": "alfred",
            "start": "2026-05-05T14:00:00-03:00",
            "end": "2026-05-05T14:30:00-03:00",
        },
    )
    client = MagicMock()
    client.delete_event.return_value = True
    config = _make_config()

    result = sync_event_cancellation_to_gcal(
        client=client, config=config,
        file_path=file_path,
        gcal_event_id="ben-mcmillan-tuesday-event-id",
        # No keep flag → default delete.
        keep_on_cancel=False,
    )

    assert result["cancelled"] is True
    assert result["path"] == "delete"
    client.delete_event.assert_called_once()

    # Vault: status=cancelled preserved, gcal_event_id cleared.
    post = frontmatter.load(str(file_path))
    assert post["status"] == "cancelled"
    assert "gcal_event_id" not in post


# ---------------------------------------------------------------------------
# update_event status= validation (gcal.py addition)
# ---------------------------------------------------------------------------


def test_update_event_rejects_invalid_status(tmp_path):
    """GCalClient.update_event rejects status values outside the
    GCal-permitted set (confirmed/tentative/cancelled). Defense in
    depth — the sync function only ever passes 'cancelled' but the
    validation guards against future callers."""
    from alfred.integrations.gcal import GCalAPIError, GCalClient

    client = GCalClient(
        credentials_path=tmp_path / "creds.json",
        token_path=tmp_path / "token.json",
    )
    # Stub the service so we don't hit the network path; the validation
    # error fires before any service call.
    client._service = MagicMock()

    with pytest.raises(GCalAPIError, match="status must be one of"):
        client.update_event(
            "cal@group.calendar.google.com",
            "evt-1",
            status="bogus_value",
        )


def test_update_event_status_cancelled_in_patch_body(tmp_path):
    """When status='cancelled' is passed, the patch body sent to GCal
    includes ``status: cancelled``. Other fields stay untouched."""
    from alfred.integrations.gcal import GCalClient

    service_mock = MagicMock()
    patch_chain = (
        service_mock.events.return_value.patch.return_value.execute
    )
    patch_chain.return_value = {
        "id": "evt-1",
        "status": "cancelled",
        "summary": "Was confirmed",
        "start": {"dateTime": "2026-05-05T14:00:00-03:00"},
        "end": {"dateTime": "2026-05-05T14:30:00-03:00"},
    }

    client = GCalClient(
        credentials_path=tmp_path / "creds.json",
        token_path=tmp_path / "token.json",
    )
    client._service = service_mock

    client.update_event(
        "cal@group.calendar.google.com",
        "evt-1",
        status="cancelled",
    )
    # Inspect the patch body — only the status field should be in it.
    call_kwargs = service_mock.events.return_value.patch.call_args.kwargs
    assert call_kwargs["calendarId"] == "cal@group.calendar.google.com"
    assert call_kwargs["eventId"] == "evt-1"
    assert call_kwargs["body"] == {"status": "cancelled"}
