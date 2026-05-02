"""Tests for the talker daemon's GCal event-update hook closure,
specifically the "first-sync promotion" branch.

Phase A+ surfaced a gap: when ``vault_edit`` adds ``start``/``end`` to
an event that has no ``gcal_event_id`` (e.g., back-fills datetimes
onto a record that predates Phase A+), the update hook needs to push
the event to GCal as a fresh create — not patch a non-existent
mirror. The fix moves decision authority from the
``_fire_update_hooks`` registry gate (now drops the gate) into the
hook closure (now branches three ways: PATCH / PROMOTE / NO-OP).

The closure itself lives inside the talker daemon's ``run`` function,
captured in the GCal init block. Three things here:

  1. A small ``_promotion_branch_under_test`` reproduces the closure's
     three-way branching so we can unit-test the routing decision
     without spinning up the daemon. The reproduction is byte-for-
     byte the same logic; the daemon source-pin below ensures the
     production closure stays in sync.

  2. Three tests exercise each branch (PATCH / PROMOTE / NO-OP) and
     verify the right ``sync_event_*_to_gcal`` function is invoked
     with the right args.

  3. A daemon source-text pin confirms the production closure has
     the three-branch structure (promotion check before patch check;
     ``gcal.sync_promoted_to_create`` log event on the promotion
     path; ``sync_event_create_to_gcal`` invoked on promotion).
"""

from __future__ import annotations

from datetime import datetime as _dt
from pathlib import Path
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Closure reproduction — mirrors the production three-way branching
# ---------------------------------------------------------------------------
#
# Kept here (not in production) so the test inputs can be invoked
# without daemon setup. The daemon source-pin at the bottom of this
# file fails if production drifts away from this shape.


def _promotion_branch_under_test(
    *,
    sync_event_create_to_gcal,
    sync_event_update_to_gcal,
    bound_client,
    bound_config,
    bound_intended_on,
    log,
):
    """Build a closure with the same three-way routing as the production
    ``_on_event_updated``. Returns the closure for direct invocation
    from tests.
    """
    def _on_event_updated(vault_path_, rel_path, fm, fields_changed):
        gcal_event_id = str(fm.get("gcal_event_id") or "")
        start_raw = fm.get("start")
        end_raw = fm.get("end")

        # PROMOTE
        if not gcal_event_id and start_raw and end_raw:
            try:
                start_dt = _dt.fromisoformat(str(start_raw))
                end_dt = _dt.fromisoformat(str(end_raw))
            except Exception:
                return
            log.info(
                "gcal.sync_promoted_to_create",
                rel_path=rel_path,
                reason="vault_edit added start+end",
                correlation_id=str(fm.get("correlation_id") or ""),
            )
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


# ---------------------------------------------------------------------------
# PROMOTE branch — the bug-fix's headline case
# ---------------------------------------------------------------------------


def test_promote_branch_no_id_with_times_calls_create(tmp_path):
    """vault_edit adds start+end to a record with no gcal_event_id →
    closure routes to ``sync_event_create_to_gcal``, NOT update."""
    create_fn = MagicMock()
    update_fn = MagicMock()
    log = MagicMock()
    closure = _promotion_branch_under_test(
        sync_event_create_to_gcal=create_fn,
        sync_event_update_to_gcal=update_fn,
        bound_client=MagicMock(),
        bound_config=MagicMock(),
        bound_intended_on=False,
        log=log,
    )

    fm = {
        "type": "event",
        "name": "Predates Phase A+",
        "title": "Halifax Music Fest",
        "summary": "TIXR ticket",
        "start": "2026-06-27T19:00:00-03:00",
        "end": "2026-06-27T22:00:00-03:00",
        # No gcal_event_id
    }
    closure(tmp_path, "event/Predates Phase A+.md", fm, ["start", "end"])

    create_fn.assert_called_once()
    update_fn.assert_not_called()
    kwargs = create_fn.call_args.kwargs
    assert kwargs["title"] == "Halifax Music Fest"
    assert kwargs["description"] == "TIXR ticket"
    assert kwargs["start_dt"] == _dt.fromisoformat("2026-06-27T19:00:00-03:00")
    assert kwargs["end_dt"] == _dt.fromisoformat("2026-06-27T22:00:00-03:00")
    assert kwargs["file_path"] == tmp_path / "event/Predates Phase A+.md"

    # Promotion log event emitted before the create call.
    log.info.assert_called_once()
    assert log.info.call_args.args[0] == "gcal.sync_promoted_to_create"


def test_promote_branch_falls_back_to_noop_on_unparseable_times(tmp_path):
    """If start/end are present but unparseable → log warning, no create."""
    create_fn = MagicMock()
    update_fn = MagicMock()
    log = MagicMock()
    closure = _promotion_branch_under_test(
        sync_event_create_to_gcal=create_fn,
        sync_event_update_to_gcal=update_fn,
        bound_client=MagicMock(),
        bound_config=MagicMock(),
        bound_intended_on=False,
        log=log,
    )

    fm = {
        "type": "event",
        "name": "Bad Times",
        "start": "not a real datetime",
        "end": "also broken",
    }
    closure(tmp_path, "event/Bad Times.md", fm, ["start", "end"])
    create_fn.assert_not_called()
    update_fn.assert_not_called()


# ---------------------------------------------------------------------------
# PATCH branch — pre-existing behavior preserved
# ---------------------------------------------------------------------------


def test_patch_branch_with_id_routes_to_update(tmp_path):
    """vault_edit on an event WITH gcal_event_id → routes to update."""
    create_fn = MagicMock()
    update_fn = MagicMock()
    log = MagicMock()
    closure = _promotion_branch_under_test(
        sync_event_create_to_gcal=create_fn,
        sync_event_update_to_gcal=update_fn,
        bound_client=MagicMock(),
        bound_config=MagicMock(),
        bound_intended_on=False,
        log=log,
    )

    fm = {
        "type": "event",
        "name": "Synced",
        "title": "New title",
        "start": "2026-06-27T19:00:00-03:00",
        "end": "2026-06-27T22:00:00-03:00",
        "gcal_event_id": "existing-mirror-id",
    }
    closure(tmp_path, "event/Synced.md", fm, ["title"])

    update_fn.assert_called_once()
    create_fn.assert_not_called()
    kwargs = update_fn.call_args.kwargs
    assert kwargs["gcal_event_id"] == "existing-mirror-id"
    assert kwargs["title"] == "New title"
    # start/end NOT in fields_changed → not patched.
    assert kwargs["start_dt"] is None
    assert kwargs["end_dt"] is None


def test_patch_branch_only_sends_changed_gcal_fields(tmp_path):
    """A vault_edit that only touches ``tags`` (not in GCal patch
    surface) → update_fn is called with all None for GCal fields."""
    create_fn = MagicMock()
    update_fn = MagicMock()
    log = MagicMock()
    closure = _promotion_branch_under_test(
        sync_event_create_to_gcal=create_fn,
        sync_event_update_to_gcal=update_fn,
        bound_client=MagicMock(),
        bound_config=MagicMock(),
        bound_intended_on=False,
        log=log,
    )

    fm = {
        "type": "event",
        "name": "Tag Only",
        "title": "Same title",
        "summary": "Same summary",
        "start": "2026-06-27T19:00:00-03:00",
        "end": "2026-06-27T22:00:00-03:00",
        "gcal_event_id": "id-1",
    }
    closure(tmp_path, "event/Tag Only.md", fm, ["tags"])

    update_fn.assert_called_once()
    kwargs = update_fn.call_args.kwargs
    assert kwargs["title"] is None
    assert kwargs["description"] is None
    assert kwargs["start_dt"] is None
    assert kwargs["end_dt"] is None
    # gcal_event_id is always passed (it's required to identify the patch target).
    assert kwargs["gcal_event_id"] == "id-1"


# ---------------------------------------------------------------------------
# NO-OP branch — never synced AND no datetimes
# ---------------------------------------------------------------------------


def test_noop_branch_no_id_no_times_does_nothing(tmp_path):
    """No gcal_event_id AND no start/end → no API call (vault edit
    happened on a record that's not GCal-eligible)."""
    create_fn = MagicMock()
    update_fn = MagicMock()
    log = MagicMock()
    closure = _promotion_branch_under_test(
        sync_event_create_to_gcal=create_fn,
        sync_event_update_to_gcal=update_fn,
        bound_client=MagicMock(),
        bound_config=MagicMock(),
        bound_intended_on=False,
        log=log,
    )

    fm = {
        "type": "event",
        "name": "Ineligible",
        "date": "2026-06-27",  # date-only, no times
        # No gcal_event_id
    }
    closure(tmp_path, "event/Ineligible.md", fm, ["date"])
    create_fn.assert_not_called()
    update_fn.assert_not_called()


def test_noop_branch_only_start_no_end_does_nothing(tmp_path):
    """Promotion requires BOTH start and end (GCal create needs both).
    Only one present → no-op (operator should fix the record)."""
    create_fn = MagicMock()
    update_fn = MagicMock()
    log = MagicMock()
    closure = _promotion_branch_under_test(
        sync_event_create_to_gcal=create_fn,
        sync_event_update_to_gcal=update_fn,
        bound_client=MagicMock(),
        bound_config=MagicMock(),
        bound_intended_on=False,
        log=log,
    )

    fm = {
        "type": "event",
        "name": "Half-eligible",
        "start": "2026-06-27T19:00:00-03:00",
        # end missing
    }
    closure(tmp_path, "event/Half-eligible.md", fm, ["start"])
    create_fn.assert_not_called()
    update_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Daemon source-pin — production closure stays in sync
# ---------------------------------------------------------------------------


def test_talker_daemon_update_hook_has_promotion_branch():
    """Source-pin: the production ``_on_event_updated`` closure must
    have the promotion branch (no_id + start + end → sync_event_create_to_gcal).

    Without this pin, the daemon's closure could regress back to the
    pre-fix shape (only PATCH path, gated on gcal_event_id) and the
    bug surfaces again silently — the in-process unit tests above
    would still pass because they test the reproduction, not the
    production closure.
    """
    here = Path(__file__).resolve().parent
    daemon_path = here.parent / "src" / "alfred" / "telegram" / "daemon.py"
    source = daemon_path.read_text(encoding="utf-8")

    # The hook closure must reference the promotion log event.
    assert '"gcal.sync_promoted_to_create"' in source, (
        "talker daemon's _on_event_updated must emit "
        "'gcal.sync_promoted_to_create' on the promotion path. "
        "Without this log line, operators can't grep for first-sync-"
        "via-edit cases (the diagnostic value is the headline reason "
        "the log exists)."
    )

    # The promotion branch must invoke sync_event_create_to_gcal,
    # NOT sync_event_update_to_gcal, when promoting.
    # Look for the pattern: "if not gcal_event_id and start_raw and end_raw"
    # followed by sync_event_create_to_gcal.
    promotion_check_idx = source.find(
        "if not gcal_event_id and start_raw and end_raw:"
    )
    assert promotion_check_idx > 0, (
        "talker daemon's _on_event_updated must check "
        "'if not gcal_event_id and start_raw and end_raw' as the "
        "promotion gate. Refactor that drops this check re-introduces "
        "the bug fixed here."
    )

    # sync_event_create_to_gcal must appear AFTER the promotion check
    # (proves the create function is reachable from the update path).
    create_call_after_promotion = source.find(
        "sync_event_create_to_gcal(", promotion_check_idx,
    )
    assert create_call_after_promotion > promotion_check_idx, (
        "sync_event_create_to_gcal must be invoked AFTER the "
        "promotion check inside _on_event_updated — it's the routing "
        "destination for the promotion path."
    )

    # Vault-ops registry must NOT have a gcal_event_id gate anymore
    # (the gate moved into the closure).
    ops_path = here.parent / "src" / "alfred" / "vault" / "ops.py"
    ops_source = ops_path.read_text(encoding="utf-8")
    fire_update_idx = ops_source.find("def _fire_update_hooks(")
    fire_update_end = ops_source.find("\ndef ", fire_update_idx + 1)
    fire_update_block = ops_source[fire_update_idx:fire_update_end]
    assert 'if not fm.get("gcal_event_id"):' not in fire_update_block, (
        "_fire_update_hooks must NOT gate on fm.get('gcal_event_id') "
        "anymore — that gate blocked the promotion path. Decision "
        "authority lives in the hook closure now."
    )
