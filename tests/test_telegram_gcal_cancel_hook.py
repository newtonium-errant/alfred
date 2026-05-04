"""Tests for the talker daemon's GCal event-update hook closure —
specifically the new CANCEL branch added to wire ``vault_edit
status: cancelled`` to GCal mirror removal/status-patch.

Pairs with ``test_telegram_gcal_update_hook.py`` which covers the
PROMOTE / PATCH / NO-OP branches. This file:

  1. A small ``_cancel_aware_branch_under_test`` reproduces the
     closure's four-way branching (CANCEL / PROMOTE / NO-OP / PATCH)
     so we can unit-test the cancel routing without spinning up the
     daemon. Mirrors the production closure shape; the daemon source-
     pin at the bottom keeps production in sync.

  2. Tests exercise each cancel sub-case: default delete path
     (no keep flag), keep_on_cancel path, vault-only cancellation
     (no gcal_event_id → no GCal call), cancel without
     status-in-fields-changed (already cancelled — don't re-fire).

  3. A daemon source-pin confirms the production closure has the
     CANCEL branch ahead of the PATCH branch.
"""

from __future__ import annotations

from datetime import datetime as _dt
from pathlib import Path
from unittest.mock import MagicMock


def _cancel_aware_branch_under_test(
    *,
    sync_event_cancellation_to_gcal,
    sync_event_create_to_gcal,
    sync_event_update_to_gcal,
    bound_client,
    bound_config,
    bound_intended_on,
    log,
):
    """Build a closure with the same four-way routing as the production
    ``_on_event_updated``. Returns the closure for direct invocation
    from tests.

    Order matters: CANCEL first (most specific — a cancel edit on a
    synced event must NOT fall into PATCH which would patch the title /
    times of an event we've decided to delete).
    """
    def _on_event_updated(vault_path_, rel_path, fm, fields_changed):
        gcal_event_id = str(fm.get("gcal_event_id") or "")
        start_raw = fm.get("start")
        end_raw = fm.get("end")

        # CANCEL
        status_now = str(fm.get("status") or "").strip().lower()
        if (
            "status" in fields_changed
            and status_now == "cancelled"
            and gcal_event_id
        ):
            keep_on_cancel = bool(fm.get("gcal_keep_on_cancel"))
            sync_event_cancellation_to_gcal(
                client=bound_client,
                config=bound_config,
                intended_on=bound_intended_on,
                file_path=Path(vault_path_) / rel_path,
                gcal_event_id=gcal_event_id,
                keep_on_cancel=keep_on_cancel,
                correlation_id=str(fm.get("correlation_id") or ""),
            )
            return

        # PROMOTE
        if not gcal_event_id and start_raw and end_raw:
            try:
                start_dt = _dt.fromisoformat(str(start_raw))
                end_dt = _dt.fromisoformat(str(end_raw))
            except Exception:
                return
            sync_event_create_to_gcal(
                client=bound_client,
                config=bound_config,
                intended_on=bound_intended_on,
                file_path=Path(vault_path_) / rel_path,
                title=str(fm.get("title") or fm.get("name") or ""),
                description=str(fm.get("summary") or ""),
                start_dt=start_dt,
                end_dt=end_dt,
                correlation_id=str(fm.get("correlation_id") or ""),
            )
            return

        # NO-OP
        if not gcal_event_id:
            return

        # PATCH
        title = (
            str(fm.get("title") or fm.get("name") or "")
            if "title" in fields_changed or "name" in fields_changed
            else None
        )
        description = (
            str(fm.get("summary") or "")
            if "summary" in fields_changed
            else None
        )
        start_dt = None
        end_dt = None
        if "start" in fields_changed and fm.get("start"):
            try:
                start_dt = _dt.fromisoformat(str(fm["start"]))
            except Exception:
                pass
        if "end" in fields_changed and fm.get("end"):
            try:
                end_dt = _dt.fromisoformat(str(fm["end"]))
            except Exception:
                pass
        sync_event_update_to_gcal(
            client=bound_client,
            config=bound_config,
            intended_on=bound_intended_on,
            gcal_event_id=gcal_event_id,
            title=title,
            description=description,
            start_dt=start_dt,
            end_dt=end_dt,
            correlation_id=str(fm.get("correlation_id") or ""),
        )

    return _on_event_updated


def _build_closure_mocks():
    """Return (cancel, create, update, log) MagicMocks plus a closure
    pre-built with sensible bound args."""
    cancel_fn = MagicMock()
    create_fn = MagicMock()
    update_fn = MagicMock()
    log = MagicMock()
    closure = _cancel_aware_branch_under_test(
        sync_event_cancellation_to_gcal=cancel_fn,
        sync_event_create_to_gcal=create_fn,
        sync_event_update_to_gcal=update_fn,
        bound_client=MagicMock(),
        bound_config=MagicMock(),
        bound_intended_on=False,
        log=log,
    )
    return cancel_fn, create_fn, update_fn, log, closure


# ---------------------------------------------------------------------------
# CANCEL branch — default delete (no keep flag)
# ---------------------------------------------------------------------------


def test_cancel_default_delete_path_routes_to_cancel_fn(tmp_path):
    """vault_edit cancel + has gcal_event_id + no keep flag →
    cancel_fn called with keep_on_cancel=False, NOT update_fn."""
    cancel_fn, create_fn, update_fn, _log, closure = _build_closure_mocks()

    fm = {
        "type": "event",
        "name": "Call with Ben",
        "status": "cancelled",
        "gcal_event_id": "ben-tuesday-id",
        "gcal_calendar": "alfred",
        "start": "2026-05-05T14:00:00-03:00",
        "end": "2026-05-05T14:30:00-03:00",
    }
    closure(tmp_path, "event/Call with Ben.md", fm, ["status"])

    cancel_fn.assert_called_once()
    create_fn.assert_not_called()
    update_fn.assert_not_called()
    kwargs = cancel_fn.call_args.kwargs
    assert kwargs["gcal_event_id"] == "ben-tuesday-id"
    assert kwargs["keep_on_cancel"] is False
    assert kwargs["file_path"] == tmp_path / "event/Call with Ben.md"


def test_cancel_with_keep_flag_routes_to_cancel_fn_keep_true(tmp_path):
    """vault_edit cancel + gcal_keep_on_cancel: true → cancel_fn called
    with keep_on_cancel=True (status-patch path)."""
    cancel_fn, _create, update_fn, _log, closure = _build_closure_mocks()

    fm = {
        "type": "event",
        "name": "Visible Cancelled",
        "status": "cancelled",
        "gcal_event_id": "keep-me-id",
        "gcal_keep_on_cancel": True,
    }
    closure(tmp_path, "event/Visible Cancelled.md", fm, ["status"])

    cancel_fn.assert_called_once()
    update_fn.assert_not_called()
    assert cancel_fn.call_args.kwargs["keep_on_cancel"] is True


# ---------------------------------------------------------------------------
# CANCEL branch — vault-only / no-gcal-event-id case
# ---------------------------------------------------------------------------


def test_cancel_without_gcal_event_id_does_not_call_cancel_fn(tmp_path):
    """vault_edit cancel + NO gcal_event_id → no GCal call AT ALL.
    Vault-only event (was never synced); falls through to the no-op
    branch downstream."""
    cancel_fn, create_fn, update_fn, _log, closure = _build_closure_mocks()

    fm = {
        "type": "event",
        "name": "Vault Only",
        "status": "cancelled",
        # No gcal_event_id, no start/end either → falls into NO-OP.
    }
    closure(tmp_path, "event/Vault Only.md", fm, ["status"])
    cancel_fn.assert_not_called()
    create_fn.assert_not_called()
    update_fn.assert_not_called()


# ---------------------------------------------------------------------------
# CANCEL gate — fields_changed must include 'status'
# ---------------------------------------------------------------------------


def test_cancel_already_cancelled_does_not_refire(tmp_path):
    """Record was already status=cancelled from a prior edit; THIS edit
    only touched (e.g.) the body. fields_changed does NOT include
    'status' → cancel_fn must NOT fire (we'd be re-deleting an already-
    deleted GCal event on every body edit, generating spurious 404s)."""
    cancel_fn, _create, update_fn, _log, closure = _build_closure_mocks()

    fm = {
        "type": "event",
        "name": "Old Cancel",
        "status": "cancelled",
        "gcal_event_id": "still-there-id",  # operator hasn't run repair
        "start": "2026-04-30T10:00:00-03:00",
        "end": "2026-04-30T11:00:00-03:00",
    }
    # Edit only touched body — status was set in a prior edit.
    closure(tmp_path, "event/Old Cancel.md", fm, ["body"])

    cancel_fn.assert_not_called()
    # Falls through to PATCH (gcal_event_id present, body edit
    # doesn't touch any GCal-relevant field, so update_fn gets called
    # with all-Nones — that's a no-op patch on the GCal side, which
    # is fine).
    update_fn.assert_called_once()
    kwargs = update_fn.call_args.kwargs
    assert kwargs["title"] is None
    assert kwargs["description"] is None
    assert kwargs["start_dt"] is None
    assert kwargs["end_dt"] is None


def test_cancel_status_changed_to_non_cancelled_does_not_fire_cancel(tmp_path):
    """vault_edit changes status, but to e.g. 'confirmed' (not cancelled)
    → no cancel fire. Falls through to PATCH (existing behavior)."""
    cancel_fn, _create, update_fn, _log, closure = _build_closure_mocks()

    fm = {
        "type": "event",
        "name": "Confirmed Now",
        "status": "confirmed",  # changed from something else
        "gcal_event_id": "evt-1",
    }
    closure(tmp_path, "event/Confirmed Now.md", fm, ["status"])

    cancel_fn.assert_not_called()
    update_fn.assert_called_once()


# ---------------------------------------------------------------------------
# CANCEL branch precedence — must beat PATCH branch
# ---------------------------------------------------------------------------


def test_cancel_takes_precedence_over_patch(tmp_path):
    """Cancellation on a synced event with other fields ALSO set in the
    same edit → CANCEL fires, NOT patch. Otherwise we'd patch the title
    of an event we've decided to delete (wasteful API call, confusing
    log trail)."""
    cancel_fn, _create, update_fn, _log, closure = _build_closure_mocks()

    fm = {
        "type": "event",
        "name": "Multi-edit",
        "title": "Updated title in same edit",
        "status": "cancelled",
        "gcal_event_id": "evt-1",
    }
    # Both status AND title in fields_changed.
    closure(tmp_path, "event/Multi-edit.md", fm, ["status", "title"])

    cancel_fn.assert_called_once()
    update_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Daemon source-pin — production closure stays in sync
# ---------------------------------------------------------------------------


def test_talker_daemon_update_hook_has_cancel_branch():
    """Source-pin: the production ``_on_event_updated`` closure must
    include the CANCEL branch ahead of the PATCH branch.

    Without this pin, a future refactor could drop the cancel routing
    silently — the in-process unit tests would still pass because they
    exercise the reproduction, not the production closure.
    """
    here = Path(__file__).resolve().parent
    daemon_path = here.parent / "src" / "alfred" / "telegram" / "daemon.py"
    source = daemon_path.read_text(encoding="utf-8")

    # Cancel branch must reference the new sync function.
    assert "sync_event_cancellation_to_gcal(" in source, (
        "talker daemon's _on_event_updated must invoke "
        "sync_event_cancellation_to_gcal on the cancel path. Without "
        "this routing, vault_edit setting status=cancelled fires no "
        "GCal call (the original Salem QA bug)."
    )

    # The cancel branch must come BEFORE the promote/patch branches
    # so it has priority. Locate the cancel-condition string and the
    # promote-condition string; cancel must appear first.
    cancel_check_idx = source.find('status_now == "cancelled"')
    promote_check_idx = source.find(
        "if not gcal_event_id and start_raw and end_raw:"
    )
    assert cancel_check_idx > 0, (
        "talker daemon's _on_event_updated must check "
        "'status_now == \"cancelled\"' as the cancel gate. Refactor "
        "that drops this check re-introduces the original bug."
    )
    assert promote_check_idx > 0, (
        "promote branch must still be present (regression check)"
    )
    assert cancel_check_idx < promote_check_idx, (
        "Cancel branch must appear BEFORE the promote branch in the "
        "closure — otherwise a cancel on a record that somehow lost "
        "its gcal_event_id would route through promote (creating a "
        "fresh GCal event for an event being cancelled)."
    )

    # Cancel branch must gate on "status" being in fields_changed —
    # without this, every body-only edit on an already-cancelled record
    # re-fires the cancel sync.
    assert '"status" in fields_changed' in source, (
        "_on_event_updated must gate the cancel branch on "
        "'status in fields_changed' — without this, a body-only edit "
        "on an already-cancelled record re-fires the cancel sync."
    )
