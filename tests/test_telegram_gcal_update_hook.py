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
captured in the GCal init block. What's here:

  1. A small ``_promotion_branch_under_test`` reproduces the closure's
     per-event branching (PROMOTE / NO-OP / RE-SCOPE / PATCH) so we can
     unit-test the routing decision without spinning up the daemon. The
     reproduction mirrors the production logic; the daemon source-pins
     below ensure the production closure stays in sync.

  2. Per-branch tests verify the right ``sync_event_*_to_gcal`` function
     is invoked with the right args.

  3. Daemon source-text pins confirm the production closure keeps the
     promotion branch, calls ``_reconcile_left_collapse_group`` (the
     pre-edit-fm fix — reconcile the group a member LEFT), carries the
     RE-SCOPE branch, and no longer references the superseded NOTE-F
     ``_warn_if_collapse_key_removed`` warn.

The ``_reconcile_left_collapse_group`` helper (the pre-edit-fm
structural fix that replaced the NOTE-F deferred-reconcile warn) is
covered here for its breadcrumb + no-op gate; its richer group-reconcile
behavior is pinned in ``test_integrations_gcal_collapse.py``.
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
    from alfred.integrations.gcal_sync import (
        resolve_collapse_key, resolve_gcal_title,
    )

    def _on_event_updated(vault_path_, rel_path, fm, fields_changed, pre_fm):
        was_unkeyed = (
            bool(resolve_collapse_key(pre_fm))
            and not resolve_collapse_key(fm)
        )
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
            resolved_title, title_source = resolve_gcal_title(fm)
            sync_event_create_to_gcal(
                client=bound_client,
                config=bound_config,
                intended_on=bound_intended_on,
                file_path=Path(vault_path_) / rel_path,
                title=resolved_title,
                description=str(fm.get("summary") or ""),
                start_dt=start_dt,
                end_dt=end_dt,
                correlation_id=str(fm.get("correlation_id") or ""),
                title_source=title_source,
            )
            return

        # NO-OP
        if not gcal_event_id:
            return

        # RE-SCOPE — ex-primary un-keyed to standalone (had key, none now,
        # still holds the umbrella id): PATCH to its OWN name+times.
        if was_unkeyed:
            resolved_title, title_source = resolve_gcal_title(fm)
            rescope_start = rescope_end = None
            if start_raw:
                try:
                    rescope_start = _dt.fromisoformat(str(start_raw))
                except Exception:
                    pass
            if end_raw:
                try:
                    rescope_end = _dt.fromisoformat(str(end_raw))
                except Exception:
                    pass
            sync_event_update_to_gcal(
                client=bound_client,
                config=bound_config,
                intended_on=bound_intended_on,
                gcal_event_id=gcal_event_id,
                title=resolved_title,
                description=str(fm.get("summary") or ""),
                start_dt=rescope_start,
                end_dt=rescope_end,
                correlation_id=str(fm.get("correlation_id") or ""),
                title_source=title_source,
            )
            return

        # PATCH
        title_changed = (
            "gcal_title" in fields_changed
            or "title" in fields_changed
            or "name" in fields_changed
        )
        if title_changed:
            resolved_title, title_source = resolve_gcal_title(fm)
            title = resolved_title
        else:
            title = None
            title_source = None
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
            title_source=title_source,
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
    closure(tmp_path, "event/Predates Phase A+.md", fm, ["start", "end"], {})

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
    closure(tmp_path, "event/Bad Times.md", fm, ["start", "end"], {})
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
    closure(tmp_path, "event/Synced.md", fm, ["title"], {})

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
    closure(tmp_path, "event/Tag Only.md", fm, ["tags"], {})

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
    closure(tmp_path, "event/Ineligible.md", fm, ["date"], {})
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
    closure(tmp_path, "event/Half-eligible.md", fm, ["start"], {})
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


# ---------------------------------------------------------------------------
# pre-edit-fm fix: left-group reconcile breadcrumb (replaces the NOTE-F warn).
#
# ``_reconcile_left_collapse_group`` is module-level so its
# ``gcal.collapse_group_changed`` breadcrumb is unit-testable on the production
# code path (per ``feedback_log_emission_test_pattern.md``). The richer
# behavioral pins (old group reconciles / survivors stay projected / re-key /
# date-change) live in ``test_integrations_gcal_collapse.py`` with the seed +
# fake-GCal harness; here we pin the breadcrumb + the no-op gate cheaply.
# ---------------------------------------------------------------------------


def _gcfg():
    from alfred.integrations.gcal_config import GCalConfig

    return GCalConfig(
        enabled=True, alfred_calendar_id="cal@g.com",
        alfred_calendar_label="alfred",
    )


def test_left_group_reconcile_emits_breadcrumb_on_unkey(tmp_path):
    """Key removed this edit (pre had a key, post has none) → the left-group
    reconcile fires + emits the ``gcal.collapse_group_changed`` breadcrumb. No
    event dir → the reconcile itself is a clean noop, but the breadcrumb (the
    ILB signal that the group identity changed) still fires."""
    import structlog

    from alfred.telegram.daemon import _reconcile_left_collapse_group

    pre = {"type": "event", "gcal_collapse_key": "rTMS", "date": "2026-07-06"}
    post = {"type": "event", "date": "2026-07-06"}  # key removed
    with structlog.testing.capture_logs() as cap:
        changed = _reconcile_left_collapse_group(
            client=MagicMock(), config=_gcfg(), vault_path=tmp_path,
            rel_path="event/rTMS Slot 1.md", pre_fm=pre, post_fm=post,
            intended_on=True, correlation_id="cid-1",
        )
    assert changed is True
    crumbs = [c for c in cap if c.get("event") == "gcal.collapse_group_changed"]
    assert len(crumbs) == 1
    assert crumbs[0]["old_key"] == "rTMS"
    assert crumbs[0]["new_key"] == ""
    assert crumbs[0]["old_date"] == "2026-07-06"


def test_left_group_reconcile_breadcrumb_on_rekey(tmp_path):
    """Key CHANGED (rTMS → physio) → breadcrumb fires with both keys."""
    import structlog

    from alfred.telegram.daemon import _reconcile_left_collapse_group

    pre = {"type": "event", "gcal_collapse_key": "rTMS", "date": "2026-07-06"}
    post = {"type": "event", "gcal_collapse_key": "physio", "date": "2026-07-06"}
    with structlog.testing.capture_logs() as cap:
        changed = _reconcile_left_collapse_group(
            client=MagicMock(), config=_gcfg(), vault_path=tmp_path,
            rel_path="event/x.md", pre_fm=pre, post_fm=post,
            intended_on=True,
        )
    assert changed is True
    crumbs = [c for c in cap if c.get("event") == "gcal.collapse_group_changed"]
    assert len(crumbs) == 1
    assert crumbs[0]["old_key"] == "rTMS" and crumbs[0]["new_key"] == "physio"


def test_left_group_reconcile_breadcrumb_on_date_change(tmp_path):
    """Same key, DATE moved → the event left its (key, old-date) group →
    breadcrumb fires (date-change is the same gap class as key-change)."""
    import structlog

    from alfred.telegram.daemon import _reconcile_left_collapse_group

    pre = {"type": "event", "gcal_collapse_key": "rTMS", "date": "2026-07-06"}
    post = {"type": "event", "gcal_collapse_key": "rTMS", "date": "2026-07-07"}
    with structlog.testing.capture_logs() as cap:
        changed = _reconcile_left_collapse_group(
            client=MagicMock(), config=_gcfg(), vault_path=tmp_path,
            rel_path="event/x.md", pre_fm=pre, post_fm=post, intended_on=True,
        )
    assert changed is True
    crumbs = [c for c in cap if c.get("event") == "gcal.collapse_group_changed"]
    assert len(crumbs) == 1
    assert crumbs[0]["old_date"] == "2026-07-06"
    assert crumbs[0]["new_date"] == "2026-07-07"


def test_left_group_reconcile_noop_when_group_identity_unchanged(tmp_path):
    """Same (key, day) — e.g. a time-only edit within the day, or a non-key
    field edit — is NOT a group change → no reconcile, no breadcrumb. Returns
    False and never calls the GCal client."""
    import structlog

    from alfred.telegram.daemon import _reconcile_left_collapse_group

    client = MagicMock()
    # Same key, same DAY (only the time-of-day differs) → unchanged group id.
    pre = {"type": "event", "gcal_collapse_key": "rTMS",
           "start": "2026-07-06T08:30:00-03:00"}
    post = {"type": "event", "gcal_collapse_key": "rTMS",
            "start": "2026-07-06T09:00:00-03:00"}
    with structlog.testing.capture_logs() as cap:
        changed = _reconcile_left_collapse_group(
            client=client, config=_gcfg(), vault_path=tmp_path,
            rel_path="event/x.md", pre_fm=pre, post_fm=post, intended_on=True,
        )
    assert changed is False
    assert not [c for c in cap if c.get("event") == "gcal.collapse_group_changed"]
    client.delete_event.assert_not_called()
    client.update_event.assert_not_called()
    client.create_event.assert_not_called()


def test_left_group_reconcile_noop_when_no_old_key(tmp_path):
    """Pure key-ADD (no old key) → not a 'left a group' event → no reconcile,
    no breadcrumb (the caller's new-group branch handles the add)."""
    import structlog

    from alfred.telegram.daemon import _reconcile_left_collapse_group

    pre = {"type": "event", "date": "2026-07-06"}  # no key before
    post = {"type": "event", "gcal_collapse_key": "rTMS", "date": "2026-07-06"}
    with structlog.testing.capture_logs() as cap:
        changed = _reconcile_left_collapse_group(
            client=MagicMock(), config=_gcfg(), vault_path=tmp_path,
            rel_path="event/x.md", pre_fm=pre, post_fm=post, intended_on=True,
        )
    assert changed is False
    assert not [c for c in cap if c.get("event") == "gcal.collapse_group_changed"]


def test_talker_daemon_update_hook_calls_left_group_reconcile():
    """Source-pin: the production ``_on_event_updated`` closure must call the
    left-group reconcile helper (FIRST, before the new-state logic) + carry the
    RE-SCOPE branch; the stale NOTE-F warn helper must be GONE."""
    here = Path(__file__).resolve().parent
    source = (
        here.parent / "src" / "alfred" / "telegram" / "daemon.py"
    ).read_text(encoding="utf-8")
    # The closure must invoke the left-group reconcile helper.
    assert "_reconcile_left_collapse_group(" in source, (
        "the update closure must call _reconcile_left_collapse_group so the "
        "group the member LEFT reconciles immediately (no transient gap)."
    )
    # ORDERING (load-bearing, reviewer WARN fold-in): inside _on_event_updated
    # the reconcile CALL must run BEFORE the keyed-return resolve
    # (``collapse_key = resolve_collapse_key(fm)``) — else a re-key / date-change
    # returns from the NEW-group branch before the OLD group is reconciled and
    # the transient gap silently regresses with no other test catching it.
    # Anchor the comparison WITHIN the closure body: a bare ``source.index()``
    # would resolve ``_reconcile_left_collapse_group(`` to the module-level DEF
    # and ``collapse_key = resolve_collapse_key(fm)`` to the copy in
    # _on_event_created — unrelated occurrences that never move (false-green).
    hook_body = source[source.index("def _on_event_updated("):]
    assert (
        hook_body.index("_reconcile_left_collapse_group(")
        < hook_body.index("collapse_key = resolve_collapse_key(fm)")
    ), (
        "the left-group reconcile CALL must precede the keyed-return "
        "`collapse_key = resolve_collapse_key(fm)` inside _on_event_updated — "
        "moving it below regresses the re-key/date-change transient gap."
    )
    # The breadcrumb is the ILB signal for the group-identity change.
    assert '"gcal.collapse_group_changed"' in source, (
        "the left-group reconcile must emit gcal.collapse_group_changed."
    )
    # The RE-SCOPE branch (ex-primary un-keyed to standalone) must exist.
    assert "was_unkeyed" in source and '"gcal.collapse_unkey_rescope"' in source, (
        "the closure must carry the RE-SCOPE branch (un-keyed ex-primary "
        "sheds the umbrella identity)."
    )
    # The stale NOTE-F deferred-reconcile warn must be removed.
    assert "_warn_if_collapse_key_removed" not in source, (
        "the NOTE-F deferred-reconcile warn is superseded by the immediate "
        "left-group reconcile — it must be removed, not left stale."
    )
    assert '"gcal.collapse_key_removed"' not in source, (
        "the stale collapse_key_removed warn event must be gone."
    )


# ---------------------------------------------------------------------------
# gcal_sync policy LEAK on the PROMOTION path (skill-qa catch).
#
# The promotion branch (date-only event later gains start/end → reaches a
# ``sync_event_create_to_gcal(...)`` call) was the lone create-like branch
# that did NOT thread ``sync_policy=`` — unthreaded since §2 (it lives at a
# deeper indent than the create-hook block, so §2's replace_all missed it).
# A ``gcal_sync: none`` remind-only event that later gained a time would LEAK
# onto GCal. Two pins: a SOURCE-pin that genuinely fails without the fix (the
# call site lacked the kwarg), and a behavioral semantics pin.
# ---------------------------------------------------------------------------


def test_promotion_path_threads_sync_policy_source_pin():
    """LOAD-BEARING: the production promotion-path ``sync_event_create_to_gcal``
    call MUST pass ``sync_policy=resolve_sync_policy(fm)``. Fails without the
    fix (the kwarg was absent on this call site → gcal_sync:none leaked)."""
    here = Path(__file__).resolve().parent
    source = (
        here.parent / "src" / "alfred" / "telegram" / "daemon.py"
    ).read_text(encoding="utf-8")

    # Anchor on the promotion gate, then the NEXT create call after it, then
    # its closing paren — assert sync_policy is threaded within that span.
    promo_idx = source.find(
        "if not gcal_event_id and start_raw and end_raw:"
    )
    assert promo_idx > 0, "promotion gate not found in _on_event_updated"
    create_idx = source.find("sync_event_create_to_gcal(", promo_idx)
    assert create_idx > promo_idx, "promotion create call not found"
    close_idx = source.find("\n                        )", create_idx)
    assert close_idx > create_idx, "promotion create close-paren not found"
    promo_create_span = source[create_idx:close_idx]
    assert "sync_policy=resolve_sync_policy(fm)" in promo_create_span, (
        "the PROMOTION-path sync_event_create_to_gcal must thread "
        "sync_policy=resolve_sync_policy(fm) — without it a gcal_sync:none "
        "event that gains a time LEAKS onto GCal (the §2 omission)."
    )


def test_promotion_shape_none_policy_does_not_create():
    """Behavioral semantics: a promotion-shaped event (no gcal_event_id, has
    start/end) carrying gcal_sync:none → resolve_sync_policy → 'none' → the
    create func returns the policy noop, NO client.create_event. Proves the
    threaded value actually suppresses the leak."""
    import tempfile
    from datetime import datetime, timezone

    import frontmatter

    from alfred.integrations.gcal_config import GCalConfig
    from alfred.integrations.gcal_sync import (
        resolve_sync_policy,
        sync_event_create_to_gcal,
    )

    fm = {
        "type": "event", "name": "Mom Birthday",
        "start": "2099-06-01T14:00:00-03:00",
        "end": "2099-06-01T15:00:00-03:00",
        "gcal_sync": "none",
    }
    with tempfile.TemporaryDirectory() as d:
        fp = Path(d) / "evt.md"
        fp.write_text(
            frontmatter.dumps(frontmatter.Post("body\n", **fm)) + "\n",
            encoding="utf-8",
        )
        client = MagicMock()
        out = sync_event_create_to_gcal(
            client=client,
            config=GCalConfig(
                enabled=True, alfred_calendar_id="cal@g.com",
                alfred_calendar_label="alfred",
            ),
            intended_on=True, file_path=fp, title="Mom Birthday",
            description="",
            start_dt=datetime(2099, 6, 1, 14, tzinfo=timezone.utc),
            end_dt=datetime(2099, 6, 1, 15, tzinfo=timezone.utc),
            sync_policy=resolve_sync_policy(fm),  # the value the fix threads
        )
    assert out == {"noop": "sync_policy_none"}
    client.create_event.assert_not_called()
