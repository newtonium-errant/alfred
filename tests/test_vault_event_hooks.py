"""Tests for the vault-ops event hook registry (Phase A+).

Phase A+ commit 2 — vault_create / vault_edit / vault_delete on
``event/`` records fire registered hooks so external syncers (GCal v1,
future webhook subscribers) get notified without each call site
duplicating the integration logic.

Coverage:
  * Registration is idempotent (same callable twice → one entry)
  * vault_create on event fires the create hook with vault_path,
    rel_path, and frontmatter dict
  * vault_create on non-event records does NOT fire the hook
  * vault_edit on event with gcal_event_id fires update hook with
    fields_changed list
  * vault_edit on event WITHOUT gcal_event_id is no-op (gate)
  * vault_edit on non-event is no-op (record_type gate)
  * vault_delete on event fires delete hook with pre-delete fm
    (specifically gcal_event_id is captured before file removal)
  * vault_delete on event WITHOUT gcal_event_id still fires the
    hook (delete is unconditional — the hook itself decides what to
    do based on the absent ID)
  * Wait — re-reading the spec: delete hook fires unconditionally
    on event records. The "gcal_event_id present" gate is in the
    GCal sync function (returns noop when id absent), not in the
    hook fire. This matches the create hook pattern (always fires
    for event records) and keeps the registry-fire side simple
  * Hook exceptions are caught + logged, never break vault_create /
    edit / delete (vault is canonical, hook failures are projection
    failures)
  * Multiple hooks fire in registration order
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import frontmatter
import pytest


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_hooks():
    """Clear hook registries before AND after each test.

    The registries are process-global — without this fixture, one
    test's registered hook would still fire in subsequent tests.
    """
    from alfred.vault.ops import clear_event_hooks
    clear_event_hooks()
    yield
    clear_event_hooks()


@pytest.fixture
def tmp_vault(tmp_path: Path) -> Path:
    """Minimal vault layout for these tests — just the directories
    vault_create needs to write into."""
    for sub in ("event", "person", "task", "note"):
        (tmp_path / sub).mkdir()
    return tmp_path


def _seed_event_record(
    vault: Path,
    *,
    name: str,
    fields: dict | None = None,
) -> str:
    """Write a minimal event/<name>.md and return its rel_path."""
    fm = {"type": "event", "name": name, "title": name}
    if fields:
        fm.update(fields)
    rel_path = f"event/{name}.md"
    file_path = vault / rel_path
    post = frontmatter.Post("body\n", **fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return rel_path


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_create_hook_is_idempotent():
    from alfred.vault.ops import (
        _EVENT_CREATE_HOOKS, register_event_create_hook,
    )

    def hook(vault_path, rel_path, fm):
        pass

    register_event_create_hook(hook)
    register_event_create_hook(hook)  # same callable
    assert _EVENT_CREATE_HOOKS.count(hook) == 1


def test_register_update_hook_is_idempotent():
    from alfred.vault.ops import (
        _EVENT_UPDATE_HOOKS, register_event_update_hook,
    )

    def hook(vault_path, rel_path, fm, fields_changed):
        pass

    register_event_update_hook(hook)
    register_event_update_hook(hook)
    assert _EVENT_UPDATE_HOOKS.count(hook) == 1


def test_register_delete_hook_is_idempotent():
    from alfred.vault.ops import (
        _EVENT_DELETE_HOOKS, register_event_delete_hook,
    )

    def hook(vault_path, rel_path, pre_delete_fm):
        pass

    register_event_delete_hook(hook)
    register_event_delete_hook(hook)
    assert _EVENT_DELETE_HOOKS.count(hook) == 1


def test_clear_hooks_wipes_all_three():
    from alfred.vault.ops import (
        _EVENT_CREATE_HOOKS, _EVENT_UPDATE_HOOKS, _EVENT_DELETE_HOOKS,
        clear_event_hooks,
        register_event_create_hook,
        register_event_delete_hook,
        register_event_update_hook,
    )

    register_event_create_hook(lambda *a: None)
    register_event_update_hook(lambda *a: None)
    register_event_delete_hook(lambda *a: None)
    assert len(_EVENT_CREATE_HOOKS) == 1
    assert len(_EVENT_UPDATE_HOOKS) == 1
    assert len(_EVENT_DELETE_HOOKS) == 1
    clear_event_hooks()
    assert _EVENT_CREATE_HOOKS == []
    assert _EVENT_UPDATE_HOOKS == []
    assert _EVENT_DELETE_HOOKS == []


# ---------------------------------------------------------------------------
# vault_create + create hook
# ---------------------------------------------------------------------------


def test_vault_create_event_fires_create_hook(tmp_vault):
    from alfred.vault.ops import register_event_create_hook, vault_create

    fired = []

    def hook(vault_path, rel_path, fm):
        fired.append({
            "vault_path": vault_path,
            "rel_path": rel_path,
            "fm": dict(fm),
        })

    register_event_create_hook(hook)
    result = vault_create(
        tmp_vault, "event", "Test Event",
        set_fields={
            "start": "2026-06-01T14:00:00-03:00",
            "end": "2026-06-01T15:00:00-03:00",
            "title": "Test Event",
        },
    )
    assert result["path"] == "event/Test Event.md"
    assert len(fired) == 1
    assert fired[0]["vault_path"] == tmp_vault
    assert fired[0]["rel_path"] == "event/Test Event.md"
    assert fired[0]["fm"]["type"] == "event"
    assert fired[0]["fm"]["title"] == "Test Event"


def test_vault_create_non_event_does_not_fire_event_hook(tmp_vault):
    from alfred.vault.ops import register_event_create_hook, vault_create

    fired = []

    def hook(vault_path, rel_path, fm):
        fired.append(rel_path)

    register_event_create_hook(hook)
    vault_create(tmp_vault, "person", "Andrew Newton")
    assert fired == []


def test_create_hook_exception_is_swallowed(tmp_vault):
    """Hook explosion must not break vault_create."""
    from alfred.vault.ops import register_event_create_hook, vault_create

    def boom(vault_path, rel_path, fm):
        raise RuntimeError("hook explode")

    register_event_create_hook(boom)
    # Must not raise; vault_create returns normally.
    result = vault_create(
        tmp_vault, "event", "Boom Event",
        set_fields={"start": "2026-06-01T14:00:00-03:00"},
    )
    assert result["path"] == "event/Boom Event.md"
    # File still on disk.
    assert (tmp_vault / "event" / "Boom Event.md").exists()


def test_multiple_create_hooks_fire_in_order(tmp_vault):
    from alfred.vault.ops import register_event_create_hook, vault_create

    order = []
    register_event_create_hook(lambda v, r, f: order.append("first"))
    register_event_create_hook(lambda v, r, f: order.append("second"))
    register_event_create_hook(lambda v, r, f: order.append("third"))

    vault_create(
        tmp_vault, "event", "Multi Hook",
        set_fields={"start": "2026-06-01T14:00:00-03:00"},
    )
    assert order == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# vault_edit + update hook
# ---------------------------------------------------------------------------


def test_vault_edit_event_with_gcal_id_fires_update_hook(tmp_vault):
    from alfred.vault.ops import register_event_update_hook, vault_edit

    rel_path = _seed_event_record(
        tmp_vault, name="Existing",
        fields={
            "start": "2026-06-01T14:00:00-03:00",
            "gcal_event_id": "abc-123",
        },
    )
    fired = []

    def hook(vault_path, rel_path, fm, fields_changed):
        fired.append({
            "rel_path": rel_path,
            "fm_gcal_id": fm.get("gcal_event_id"),
            "fields_changed": list(fields_changed),
        })

    register_event_update_hook(hook)

    vault_edit(
        tmp_vault, rel_path,
        set_fields={"start": "2026-06-01T15:00:00-03:00"},
    )
    assert len(fired) == 1
    assert fired[0]["rel_path"] == rel_path
    assert fired[0]["fm_gcal_id"] == "abc-123"
    assert "start" in fired[0]["fields_changed"]


def test_vault_edit_event_without_gcal_id_still_fires_hook(tmp_vault):
    """Event has no gcal_event_id → hook STILL fires (decision authority
    moved into the closure post-promotion fix).

    Pre-promotion this asserted ``fired == []`` (registry-level gate).
    The gate blocked the "vault_edit adds start+end to a previously-no-
    time event" promotion path: the hook never fired so the GCal create
    never happened. Now the hook fires unconditionally on event-edits
    and the closure decides what to do based on post-edit state.
    """
    from alfred.vault.ops import register_event_update_hook, vault_edit

    rel_path = _seed_event_record(
        tmp_vault, name="Unsynced",
        fields={"start": "2026-06-01T14:00:00-03:00"},
        # No gcal_event_id
    )
    fired = []

    def hook(vault_path, rel_path_arg, fm, fields_changed):
        fired.append({
            "rel_path": rel_path_arg,
            "fm_gcal_id": fm.get("gcal_event_id"),
            "fm_start": fm.get("start"),
            "fields_changed": list(fields_changed),
        })

    register_event_update_hook(hook)
    vault_edit(
        tmp_vault, rel_path,
        set_fields={"start": "2026-06-01T15:00:00-03:00"},
    )
    # Hook fires; closure sees no gcal_event_id and decides what to do
    # (the production closure promotes if start+end present).
    assert len(fired) == 1
    assert fired[0]["fm_gcal_id"] is None
    assert "start" in fired[0]["fields_changed"]


def test_vault_edit_event_with_promotion_eligible_state_fires_hook(tmp_vault):
    """Specifically test the gap that surfaced live: an event is created
    without start/end (e.g., predates Phase A+ or was a date-only stub),
    a subsequent vault_edit adds start+end → hook MUST fire so a
    closure can promote it to a GCal create.
    """
    from alfred.vault.ops import register_event_update_hook, vault_edit

    rel_path = _seed_event_record(
        tmp_vault, name="Predates Phase A+",
        fields={"date": "2026-06-27"},  # date-only, no start/end, no gcal_event_id
    )
    fired = []
    register_event_update_hook(
        lambda v, r, fm, fc: fired.append({
            "gcal_event_id": fm.get("gcal_event_id"),
            "start": fm.get("start"),
            "end": fm.get("end"),
            "fields_changed": list(fc),
        })
    )
    vault_edit(
        tmp_vault, rel_path,
        set_fields={
            "start": "2026-06-27T19:00:00-03:00",
            "end": "2026-06-27T22:00:00-03:00",
        },
    )
    assert len(fired) == 1
    # Promotion-eligible state: no ID, but both start + end now present.
    assert fired[0]["gcal_event_id"] is None
    assert fired[0]["start"] == "2026-06-27T19:00:00-03:00"
    assert fired[0]["end"] == "2026-06-27T22:00:00-03:00"
    assert "start" in fired[0]["fields_changed"]
    assert "end" in fired[0]["fields_changed"]


def test_vault_edit_non_event_does_not_fire_update_hook(tmp_vault):
    from alfred.vault.ops import register_event_update_hook, vault_create, vault_edit

    vault_create(tmp_vault, "person", "Andrew Newton")
    fired = []
    register_event_update_hook(lambda *a, **k: fired.append(a))
    vault_edit(
        tmp_vault, "person/Andrew Newton.md",
        set_fields={"timezone": "America/Halifax"},
    )
    assert fired == []


def test_update_hook_exception_is_swallowed(tmp_vault):
    from alfred.vault.ops import register_event_update_hook, vault_edit

    rel_path = _seed_event_record(
        tmp_vault, name="X",
        fields={"start": "2026-06-01T14:00:00-03:00", "gcal_event_id": "id-1"},
    )
    register_event_update_hook(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    # vault_edit should still complete cleanly.
    result = vault_edit(
        tmp_vault, rel_path,
        set_fields={"start": "2026-06-01T15:00:00-03:00"},
    )
    assert result["path"] == rel_path
    # File reflects the edit.
    fm = frontmatter.load(str(tmp_vault / rel_path))
    assert fm["start"] == "2026-06-01T15:00:00-03:00"


# ---------------------------------------------------------------------------
# vault_delete + delete hook
# ---------------------------------------------------------------------------


def test_vault_delete_event_fires_delete_hook_with_pre_delete_fm(tmp_vault):
    """The hook receives the frontmatter as it was BEFORE the delete,
    so it can read ``gcal_event_id`` for the GCal-side cleanup."""
    from alfred.vault.ops import register_event_delete_hook, vault_delete

    rel_path = _seed_event_record(
        tmp_vault, name="ToDelete",
        fields={
            "start": "2026-06-01T14:00:00-03:00",
            "gcal_event_id": "delete-me-id",
        },
    )
    fired = []

    def hook(vault_path, rel_path, pre_delete_fm):
        fired.append({
            "rel_path": rel_path,
            "gcal_event_id": pre_delete_fm.get("gcal_event_id"),
        })

    register_event_delete_hook(hook)
    vault_delete(tmp_vault, rel_path)
    assert len(fired) == 1
    assert fired[0]["rel_path"] == rel_path
    assert fired[0]["gcal_event_id"] == "delete-me-id"
    # File is gone.
    assert not (tmp_vault / rel_path).exists()


def test_vault_delete_event_without_gcal_id_still_fires_hook(tmp_vault):
    """Hook fires unconditionally for event records — the hook itself
    decides what to do (the GCal sync function will return noop when
    gcal_event_id is absent)."""
    from alfred.vault.ops import register_event_delete_hook, vault_delete

    rel_path = _seed_event_record(
        tmp_vault, name="UnsyncedToDelete",
        fields={"start": "2026-06-01T14:00:00-03:00"},
    )
    fired = []
    register_event_delete_hook(
        lambda v, r, fm: fired.append(fm.get("gcal_event_id"))
    )
    vault_delete(tmp_vault, rel_path)
    assert fired == [None]  # hook fired, but ID was absent


def test_vault_delete_non_event_does_not_fire_event_hook(tmp_vault):
    from alfred.vault.ops import (
        register_event_delete_hook, vault_create, vault_delete,
    )

    vault_create(tmp_vault, "person", "Andrew Newton")
    fired = []
    register_event_delete_hook(lambda *a: fired.append(a))
    vault_delete(tmp_vault, "person/Andrew Newton.md")
    assert fired == []


def test_delete_hook_exception_is_swallowed(tmp_vault):
    from alfred.vault.ops import register_event_delete_hook, vault_delete

    rel_path = _seed_event_record(
        tmp_vault, name="HookExplode",
        fields={"start": "2026-06-01T14:00:00-03:00", "gcal_event_id": "x"},
    )
    register_event_delete_hook(
        lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    # Delete must still succeed.
    result = vault_delete(tmp_vault, rel_path)
    assert result["deleted"] is True
    assert not (tmp_vault / rel_path).exists()
