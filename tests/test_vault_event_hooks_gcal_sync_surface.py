"""Tests for the ``gcal_sync`` tool_result surface (shipped 2026-05-13).

Background (the silent-fail bug this layer of tests pins against):

Pre-2026-05-13, ``_fire_create_hooks`` / ``_fire_update_hooks`` /
``_fire_delete_hooks`` ignored hook return values. The GCal sync hook
called ``sync_event_*_to_gcal`` and got back a ``{"error": {"code":
"auth_failed", "detail": "<msg>"}}`` on expired OAuth tokens — and
silently dropped it. The talker's tool_result for ``vault_edit`` /
``vault_create`` on an event then carried no signal that the parallel
GCal sync had failed, so the LLM (Salem) narrated "GCal updated" /
"May 19 should appear shortly" to Andrew over two consecutive
auth-failure incidents (May 12 18:45 ADT, May 13 03:32 ADT). The
warning landed in the daemon log (``gcal.sync_update_failed``) but
the operator-facing reply said the opposite.

The fix routes hook return values back into ``vault_create`` /
``vault_edit`` / ``vault_delete`` so the tool_result the LLM sees
includes a ``gcal_sync`` field:

  * ``gcal_sync: {"status": "ok"}`` — sync succeeded
  * ``gcal_sync: {"status": "failed", "error_code": "<code>", "error": "<msg>"}``
    — vault landed, GCal did not
  * ``gcal_sync`` absent — no GCal action was attempted (disabled,
    no datetimes, no gcal_event_id-no-times no-op branch)

Three test groups in this file:

  1. ``_extract_gcal_sync_status`` unit tests — every shape the sync
     layer can return is translated correctly.
  2. ``_fire_*_hooks`` bubble-up tests — hook return values are
     collected into the list passed back to callers.
  3. ``vault_create`` / ``vault_edit`` / ``vault_delete`` end-to-end
     tests — registered mock hook returns a sync-failure dict, the
     return dict from the vault op carries ``gcal_sync`` correctly.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_hooks():
    """Clear hook registries before AND after each test (process-global state)."""
    from alfred.vault.ops import clear_event_hooks
    clear_event_hooks()
    yield
    clear_event_hooks()


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    for sub in ("event", "person", "task", "note"):
        (tmp_path / sub).mkdir()
    return tmp_path


def _seed_event(vault: Path, *, name: str, fields: dict | None = None) -> str:
    fm = {"type": "event", "name": name, "title": name}
    if fields:
        fm.update(fields)
    rel_path = f"event/{name}.md"
    file_path = vault / rel_path
    post = frontmatter.Post("body\n", **fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return rel_path


# ---------------------------------------------------------------------------
# Group 1: _extract_gcal_sync_status unit tests
# ---------------------------------------------------------------------------
#
# Every shape documented at the top of alfred.integrations.gcal_sync
# maps to a specific surface for the LLM. These pins guard against
# silent drift if the sync layer adds new shapes.


def test_extract_returns_none_for_empty_hook_results():
    """No hooks registered → no GCal action attempted → omit key."""
    from alfred.vault.ops import _extract_gcal_sync_status
    assert _extract_gcal_sync_status([]) is None


def test_extract_returns_none_for_disabled_sync_empty_dict():
    """``{}`` from sync layer = gcal disabled. Don't surface the key."""
    from alfred.vault.ops import _extract_gcal_sync_status
    assert _extract_gcal_sync_status([{}]) is None


def test_extract_returns_none_for_noop_hook_result():
    """``{"noop": "no_gcal_event_id"}`` = nothing to sync. Don't surface."""
    from alfred.vault.ops import _extract_gcal_sync_status
    assert _extract_gcal_sync_status([{"noop": "no_gcal_event_id"}]) is None


def test_extract_returns_ok_for_create_success():
    """Create success → ``{event_id, calendar_label}`` → status: ok."""
    from alfred.vault.ops import _extract_gcal_sync_status
    assert _extract_gcal_sync_status([
        {"event_id": "ev123", "calendar_label": "alfred"}
    ]) == {"status": "ok"}


def test_extract_returns_ok_for_delete_success():
    """Delete success → ``{deleted: True, event_id}`` → status: ok."""
    from alfred.vault.ops import _extract_gcal_sync_status
    assert _extract_gcal_sync_status([
        {"deleted": True, "event_id": "ev123"}
    ]) == {"status": "ok"}


def test_extract_returns_failed_for_auth_failed_error():
    """The actual bug shape — auth_failed must surface to the LLM."""
    from alfred.vault.ops import _extract_gcal_sync_status
    out = _extract_gcal_sync_status([
        {
            "error": {
                "code": "auth_failed",
                "detail": (
                    "GCal token refresh failed: ('invalid_grant: Token "
                    "has been expired or revoked.', '{...}')"
                ),
            }
        }
    ])
    assert out is not None
    assert out["status"] == "failed"
    assert out["error_code"] == "auth_failed"
    # Detail starts with the human-readable phrase the LLM can paraphrase.
    assert "GCal token refresh failed" in out["error"]


def test_extract_returns_failed_for_stale_gcal_id():
    """stale_gcal_id surfaces with its own code so SKILL can branch on it."""
    from alfred.vault.ops import _extract_gcal_sync_status
    out = _extract_gcal_sync_status([
        {"error": {"code": "stale_gcal_id", "detail": "Event evXYZ not found"}}
    ])
    assert out == {
        "status": "failed",
        "error_code": "stale_gcal_id",
        "error": "Event evXYZ not found",
    }


def test_extract_truncates_long_error_detail():
    """Long error messages get truncated to spare the LLM context.

    The full detail still lands in the warning log; the tool_result
    only needs enough to phrase the failure.
    """
    from alfred.vault.ops import _extract_gcal_sync_status
    long_detail = "x" * 500
    out = _extract_gcal_sync_status([
        {"error": {"code": "api_error", "detail": long_detail}}
    ])
    assert out is not None
    # Truncated to under 200 chars (well below original 500).
    assert len(out["error"]) <= 200
    assert out["error"].endswith("…")


def test_extract_returns_failed_unknown_for_unrecognized_shape():
    """Future drift in sync return shape doesn't silently look like success."""
    from alfred.vault.ops import _extract_gcal_sync_status
    out = _extract_gcal_sync_status([{"some_new_key": "value"}])
    assert out == {
        "status": "failed",
        "error_code": "unknown",
        "error": "unrecognized gcal_sync hook return shape",
    }


def test_extract_honors_first_dict_result_only():
    """v1 pragmatism: multiple hooks all returning dicts → first wins.

    Documented in ``_fire_create_hooks`` docstring. Test pins the
    behavior so a later change to multiplex surfaces is intentional.
    """
    from alfred.vault.ops import _extract_gcal_sync_status
    out = _extract_gcal_sync_status([
        {"event_id": "first"},
        {"error": {"code": "auth_failed", "detail": "ignored"}},
    ])
    assert out == {"status": "ok"}


# ---------------------------------------------------------------------------
# Group 2: _fire_*_hooks bubble-up tests
# ---------------------------------------------------------------------------


def test_fire_create_hooks_collects_dict_returns(tmp_vault):
    from alfred.vault.ops import (
        _fire_create_hooks, register_event_create_hook,
    )

    def hook(vault_path, rel_path, fm):
        return {"event_id": "ev1", "calendar_label": "alfred"}

    register_event_create_hook(hook)
    results = _fire_create_hooks(tmp_vault, "event/X.md", {})
    assert results == [{"event_id": "ev1", "calendar_label": "alfred"}]


def test_fire_create_hooks_skips_non_dict_returns(tmp_vault):
    """None / int / list returns are filtered. Future hooks that don't
    follow the dict convention don't pollute the results list."""
    from alfred.vault.ops import (
        _fire_create_hooks, register_event_create_hook,
    )

    def none_hook(vault_path, rel_path, fm):
        return None

    def int_hook(vault_path, rel_path, fm):
        return 42

    def list_hook(vault_path, rel_path, fm):
        return ["not", "a", "dict"]

    register_event_create_hook(none_hook)
    register_event_create_hook(int_hook)
    register_event_create_hook(list_hook)
    results = _fire_create_hooks(tmp_vault, "event/X.md", {})
    assert results == []


def test_fire_update_hooks_collects_dict_returns(tmp_vault):
    from alfred.vault.ops import (
        _fire_update_hooks, register_event_update_hook,
    )

    def hook(vault_path, rel_path, fm, fields_changed):
        return {"error": {"code": "auth_failed", "detail": "expired"}}

    register_event_update_hook(hook)
    results = _fire_update_hooks(
        tmp_vault, "event/X.md", {}, ["start"],
    )
    assert results == [{"error": {"code": "auth_failed", "detail": "expired"}}]


def test_fire_delete_hooks_collects_dict_returns(tmp_vault):
    from alfred.vault.ops import (
        _fire_delete_hooks, register_event_delete_hook,
    )

    def hook(vault_path, rel_path, pre_delete_fm):
        return {"deleted": True, "event_id": "ev1"}

    register_event_delete_hook(hook)
    results = _fire_delete_hooks(tmp_vault, "event/X.md", {})
    assert results == [{"deleted": True, "event_id": "ev1"}]


def test_fire_hooks_skip_exceptions_but_collect_others(tmp_vault):
    """A raising hook doesn't break collection of the non-raising one.

    The exception still gets logged and swallowed (existing contract);
    this test pins that the bubble-up doesn't break that contract.
    """
    from alfred.vault.ops import (
        _fire_update_hooks, register_event_update_hook,
    )

    def boom(vault_path, rel_path, fm, fields_changed):
        raise RuntimeError("boom")

    def good(vault_path, rel_path, fm, fields_changed):
        return {"event_id": "ev1"}

    register_event_update_hook(boom)
    register_event_update_hook(good)
    results = _fire_update_hooks(
        tmp_vault, "event/X.md", {}, ["start"],
    )
    # Good hook's result still made it through; boom's exception was
    # logged and swallowed.
    assert results == [{"event_id": "ev1"}]


# ---------------------------------------------------------------------------
# Group 3: End-to-end — vault op return dicts carry gcal_sync correctly
# ---------------------------------------------------------------------------
#
# These are the regression pins that map most directly onto the
# user-visible bug: a tool_result that previously said "edit
# succeeded" silently is now self-describing about the GCal sync
# state.


def test_vault_create_event_surfaces_gcal_sync_failed(tmp_vault):
    """The actual bug: registered hook returns auth_failed → tool_result
    carries gcal_sync: {status: failed, error_code: auth_failed, ...}.
    """
    from alfred.vault.ops import register_event_create_hook, vault_create

    def hook(vault_path, rel_path, fm):
        return {
            "error": {
                "code": "auth_failed",
                "detail": "GCal token refresh failed: invalid_grant",
            }
        }

    register_event_create_hook(hook)
    result = vault_create(
        tmp_vault, "event", "Dentist Cleaning",
        set_fields={
            "start": "2026-05-19T10:30:00-03:00",
            "end": "2026-05-19T11:00:00-03:00",
        },
    )
    assert result["path"] == "event/Dentist Cleaning.md"
    assert "gcal_sync" in result
    assert result["gcal_sync"]["status"] == "failed"
    assert result["gcal_sync"]["error_code"] == "auth_failed"
    assert "GCal token refresh failed" in result["gcal_sync"]["error"]


def test_vault_create_event_surfaces_gcal_sync_ok(tmp_vault):
    from alfred.vault.ops import register_event_create_hook, vault_create

    def hook(vault_path, rel_path, fm):
        return {"event_id": "ev_abc123", "calendar_label": "alfred"}

    register_event_create_hook(hook)
    result = vault_create(
        tmp_vault, "event", "Lunch with Marie",
        set_fields={
            "start": "2026-05-19T12:30:00-03:00",
            "end": "2026-05-19T13:30:00-03:00",
        },
    )
    assert result["gcal_sync"] == {"status": "ok"}


def test_vault_create_event_omits_gcal_sync_when_hook_returns_disabled(tmp_vault):
    """Hook returns ``{}`` (gcal disabled) → no ``gcal_sync`` key.

    Distinct from a failure: failure means "we tried and it didn't
    work"; absent means "nothing tried to sync." LLM should not
    volunteer calendar status in this case.
    """
    from alfred.vault.ops import register_event_create_hook, vault_create

    def hook(vault_path, rel_path, fm):
        return {}  # gcal disabled

    register_event_create_hook(hook)
    result = vault_create(
        tmp_vault, "event", "No-GCal Event",
        set_fields={
            "start": "2026-05-19T10:00:00-03:00",
            "end": "2026-05-19T11:00:00-03:00",
        },
    )
    assert "gcal_sync" not in result


def test_vault_create_non_event_omits_gcal_sync(tmp_vault):
    """Non-event records don't fire the event hook → no gcal_sync at all."""
    from alfred.vault.ops import register_event_create_hook, vault_create

    fired = []

    def hook(vault_path, rel_path, fm):
        fired.append(rel_path)
        return {"event_id": "should_not_fire"}

    register_event_create_hook(hook)
    result = vault_create(tmp_vault, "person", "Andrew Newton")
    assert fired == []
    assert "gcal_sync" not in result


def test_vault_edit_event_surfaces_gcal_sync_failed(tmp_vault):
    """The actual user-visible incident shape: vault_edit succeeded,
    GCal returned auth_failed, tool_result must carry that.
    """
    from alfred.vault.ops import register_event_update_hook, vault_edit

    rel_path = _seed_event(
        tmp_vault,
        name="Dentist Cleaning",
        fields={
            "start": "2026-05-19T10:30:00-03:00",
            "end": "2026-05-19T11:00:00-03:00",
            "gcal_event_id": "ev55futp8gbsqk0dtc5276d24o",
        },
    )

    def hook(vault_path, rel_path_, fm, fields_changed):
        return {
            "error": {
                "code": "auth_failed",
                "detail": (
                    "GCal token refresh failed: ('invalid_grant: Token "
                    "has been expired or revoked.', '{...}')"
                ),
            }
        }

    register_event_update_hook(hook)
    result = vault_edit(
        tmp_vault, rel_path,
        set_fields={
            "start": "2026-05-26T10:30:00-03:00",
            "end": "2026-05-26T11:00:00-03:00",
        },
    )
    assert "start" in result["fields_changed"]
    assert "gcal_sync" in result
    assert result["gcal_sync"]["status"] == "failed"
    assert result["gcal_sync"]["error_code"] == "auth_failed"


def test_vault_edit_event_omits_gcal_sync_for_noop_branch(tmp_vault):
    """Hook returns ``{"noop": "no_gcal_event_id"}`` → omit key.

    Covers the "vault_edit on an event with no GCal mirror and no
    sync intent" case — the LLM shouldn't volunteer calendar status
    when no calendar action was attempted.
    """
    from alfred.vault.ops import register_event_update_hook, vault_edit

    rel_path = _seed_event(
        tmp_vault,
        name="Some Event",
        fields={"date": "2026-05-19"},  # no start/end, no gcal_event_id
    )

    def hook(vault_path, rel_path_, fm, fields_changed):
        return {"noop": "no_gcal_event_id"}

    register_event_update_hook(hook)
    result = vault_edit(
        tmp_vault, rel_path,
        set_fields={"location": "Bedford"},
    )
    assert "gcal_sync" not in result


def test_vault_delete_event_surfaces_gcal_sync_failed(tmp_vault):
    """Vault delete is unconditional; GCal failure surfaces via gcal_sync."""
    from alfred.vault.ops import register_event_delete_hook, vault_delete

    rel_path = _seed_event(
        tmp_vault,
        name="To Delete",
        fields={"gcal_event_id": "ev999"},
    )

    def hook(vault_path, rel_path_, pre_delete_fm):
        return {
            "error": {
                "code": "auth_failed",
                "detail": "GCal token refresh failed",
            }
        }

    register_event_delete_hook(hook)
    result = vault_delete(tmp_vault, rel_path)
    assert result["deleted"] is True
    assert result["gcal_sync"]["status"] == "failed"
    assert result["gcal_sync"]["error_code"] == "auth_failed"


def test_vault_delete_event_surfaces_gcal_sync_ok(tmp_vault):
    from alfred.vault.ops import register_event_delete_hook, vault_delete

    rel_path = _seed_event(
        tmp_vault,
        name="To Delete OK",
        fields={"gcal_event_id": "ev100"},
    )

    def hook(vault_path, rel_path_, pre_delete_fm):
        return {"deleted": True, "event_id": "ev100"}

    register_event_delete_hook(hook)
    result = vault_delete(tmp_vault, rel_path)
    assert result["gcal_sync"] == {"status": "ok"}


# ---------------------------------------------------------------------------
# Group 4: Talker hook closure pinning
# ---------------------------------------------------------------------------
#
# The hook closures in telegram/daemon.py are now expected to
# ``return`` the sync function's result so the bubble-up contract
# works end-to-end. These pins guard against a refactor that drops
# the return statement (which would re-introduce the silent-fail
# bug without breaking any of the dispatch-layer tests above).


def test_telegram_daemon_create_hook_returns_sync_result():
    """Source-text pin: ``_on_event_created`` must end with a ``return
    sync_event_create_to_gcal(...)`` (not just a bare call).
    """
    import inspect
    from alfred.telegram import daemon

    src = inspect.getsource(daemon)
    # The closure invokes sync_event_create_to_gcal twice (create-hook
    # body + update-hook promotion branch). Both should be ``return``
    # calls now — never a bare call discarding the result.
    bare_calls = src.count("    sync_event_create_to_gcal(\n")
    return_calls = src.count("return sync_event_create_to_gcal(\n")
    assert bare_calls == 0, (
        f"Found {bare_calls} bare ``sync_event_create_to_gcal(...)`` calls "
        "in telegram/daemon.py. These discard the sync result and "
        "re-introduce the 2026-05-13 silent-fail bug. Use "
        "``return sync_event_create_to_gcal(...)`` so _fire_*_hooks can "
        "bubble the result up to the tool_result."
    )
    assert return_calls >= 2, (
        f"Expected at least 2 ``return sync_event_create_to_gcal(...)`` "
        f"sites (create-hook + promote-branch), found {return_calls}."
    )


def test_telegram_daemon_update_hook_returns_sync_result():
    """Source-text pin: PATCH branch ``sync_event_update_to_gcal`` and
    CANCEL branch ``sync_event_cancellation_to_gcal`` must both return.
    """
    import inspect
    from alfred.telegram import daemon

    src = inspect.getsource(daemon)
    bare_update = src.count("    sync_event_update_to_gcal(\n")
    return_update = src.count("return sync_event_update_to_gcal(\n")
    bare_cancel = src.count("    sync_event_cancellation_to_gcal(\n")
    return_cancel = src.count("return sync_event_cancellation_to_gcal(\n")

    assert bare_update == 0, (
        "Bare ``sync_event_update_to_gcal(...)`` in telegram/daemon.py "
        "drops the GCal sync result. Use ``return ...``."
    )
    assert return_update >= 1, "PATCH branch must return the sync result."
    assert bare_cancel == 0, (
        "Bare ``sync_event_cancellation_to_gcal(...)`` in telegram/daemon.py "
        "drops the GCal sync result. Use ``return ...``."
    )
    assert return_cancel >= 1, "CANCEL branch must return the sync result."


def test_telegram_daemon_delete_hook_returns_sync_result():
    """Source-text pin: ``_on_event_deleted`` returns the sync result."""
    import inspect
    from alfred.telegram import daemon

    src = inspect.getsource(daemon)
    bare = src.count("    sync_event_delete_to_gcal(\n")
    returned = src.count("return sync_event_delete_to_gcal(\n")
    assert bare == 0, (
        "Bare ``sync_event_delete_to_gcal(...)`` in telegram/daemon.py "
        "drops the GCal sync result. Use ``return ...``."
    )
    assert returned >= 1
