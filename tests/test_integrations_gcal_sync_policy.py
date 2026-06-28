"""Per-event GCal sync-policy pins — event↔GCal decouple (consolidation Step 4).

The event's IDENTITY is the vault record; GCal is ONE optional output channel.
``gcal_sync`` (per-event) declares whether the event projects to Google
Calendar: ``"sync"`` (default-absent — today's behaviour) or ``"none"``
(remind-only, e.g. birthdays). Coverage:

  * ``resolve_sync_policy`` — absent→sync, none, case, garbage→sync (fail-safe).
  * the gate INSIDE all four sync funcs — ``none`` → ``{"noop":
    "sync_policy_none"}`` with NO GCal client call; ``sync`` (default +
    explicit) proceeds.
  * BEHAVIOR-PRESERVATION (corpus: 138 events, 57-58 synced, 0 birthdays) —
    an existing synced event (``gcal_event_id`` set, NO ``gcal_sync`` field)
    still PATCHes on update; an existing unsynced event (no field) still
    syncs on create. Default-sync preserves all 138.
  * all THREE entry points honour the policy: the sync funcs (hooks route
    through these), the backfill CLI, the peer propose-create shim.
  * reminders are GCal-independent — a ``gcal_sync: none`` event (no
    ``gcal_event_id``) still appears in the brief's upcoming-events.
  * schema: ``gcal_sync`` is in ``EVENT_GCAL_FIELDS``.

Unconditional pins (no importorskip — backends are MagicMock'd).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import frontmatter

from alfred.integrations.gcal_sync import (
    resolve_sync_policy,
    sync_event_cancellation_to_gcal,
    sync_event_create_to_gcal,
    sync_event_delete_to_gcal,
    sync_event_update_to_gcal,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(*, enabled: bool = True, alfred_id: str = "cal@g.com"):
    from alfred.integrations.gcal_config import GCalConfig
    return GCalConfig(
        enabled=enabled, alfred_calendar_id=alfred_id,
        alfred_calendar_label="alfred",
    )


def _seed_event(tmp_path: Path, *, fm: dict) -> Path:
    event_dir = tmp_path / "event"
    event_dir.mkdir(exist_ok=True)
    file_path = event_dir / f"{fm.get('name', 'evt')}.md"
    file_path.write_text(
        frontmatter.dumps(frontmatter.Post("body\n", **fm)) + "\n",
        encoding="utf-8",
    )
    return file_path


_START = datetime(2099, 6, 1, 14, 0, tzinfo=timezone.utc)
_END = datetime(2099, 6, 1, 15, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# resolve_sync_policy
# ---------------------------------------------------------------------------


def test_resolve_sync_policy_absent_defaults_to_sync():
    # The load-bearing default: an event with no gcal_sync field syncs.
    assert resolve_sync_policy({}) == "sync"
    assert resolve_sync_policy({"name": "x"}) == "sync"


def test_resolve_sync_policy_none():
    assert resolve_sync_policy({"gcal_sync": "none"}) == "none"


def test_resolve_sync_policy_none_case_and_whitespace():
    assert resolve_sync_policy({"gcal_sync": "None"}) == "none"
    assert resolve_sync_policy({"gcal_sync": "NONE"}) == "none"
    assert resolve_sync_policy({"gcal_sync": "  none  "}) == "none"


def test_resolve_sync_policy_explicit_sync():
    assert resolve_sync_policy({"gcal_sync": "sync"}) == "sync"


def test_resolve_sync_policy_garbage_fails_safe_to_sync():
    # Fail-safe toward sync — never silently DROP a projection on a typo.
    assert resolve_sync_policy({"gcal_sync": "non"}) == "sync"
    assert resolve_sync_policy({"gcal_sync": ""}) == "sync"
    assert resolve_sync_policy({"gcal_sync": 123}) == "sync"
    assert resolve_sync_policy({"gcal_sync": None}) == "sync"


def test_gcal_sync_in_event_gcal_fields():
    from alfred.vault.schema import EVENT_GCAL_FIELDS
    assert "gcal_sync" in EVENT_GCAL_FIELDS


def test_disabled_precedence_over_policy_none(tmp_path):
    """§2 review coverage pin: gcal globally disabled + policy none →
    returns ``{}`` (disabled-precedence skip), NOT the ``sync_policy_none``
    noop. The global gate runs before the policy gate, so a disabled
    instance behaves byte-identically regardless of the per-event policy."""
    client = MagicMock()
    file_path = _seed_event(
        tmp_path, fm={"type": "event", "name": "B", "gcal_sync": "none"},
    )
    out = sync_event_create_to_gcal(
        client=client, config=_make_config(enabled=False), intended_on=False,
        file_path=file_path, title="B", description="",
        start_dt=_START, end_dt=_END, sync_policy="none",
    )
    assert out == {}  # disabled precedence, NOT {"noop": "sync_policy_none"}
    client.create_event.assert_not_called()


# ---------------------------------------------------------------------------
# The gate — none → noop, NO client call (all four funcs)
# ---------------------------------------------------------------------------


def test_create_policy_none_noop_no_client_call(tmp_path):
    client = MagicMock()
    file_path = _seed_event(tmp_path, fm={"type": "event", "name": "Bday"})
    out = sync_event_create_to_gcal(
        client=client, config=_make_config(), intended_on=True,
        file_path=file_path, title="Bday", description="",
        start_dt=_START, end_dt=_END, sync_policy="none",
    )
    assert out == {"noop": "sync_policy_none"}
    client.create_event.assert_not_called()
    # No writeback either — the file stays free of gcal_event_id.
    assert "gcal_event_id" not in frontmatter.load(str(file_path)).metadata


def test_update_policy_none_noop_no_client_call():
    client = MagicMock()
    out = sync_event_update_to_gcal(
        client=client, config=_make_config(), intended_on=True,
        gcal_event_id="evt-1", title="x", sync_policy="none",
    )
    assert out == {"noop": "sync_policy_none"}
    client.update_event.assert_not_called()


def test_delete_policy_none_noop_no_client_call():
    client = MagicMock()
    out = sync_event_delete_to_gcal(
        client=client, config=_make_config(), intended_on=True,
        gcal_event_id="evt-1", sync_policy="none",
    )
    assert out == {"noop": "sync_policy_none"}
    client.delete_event.assert_not_called()


def test_cancellation_policy_none_noop_no_client_call(tmp_path):
    client = MagicMock()
    file_path = _seed_event(
        tmp_path, fm={"type": "event", "name": "B", "gcal_event_id": "evt-1"},
    )
    out = sync_event_cancellation_to_gcal(
        client=client, config=_make_config(), intended_on=True,
        file_path=file_path, gcal_event_id="evt-1", sync_policy="none",
    )
    assert out == {"noop": "sync_policy_none"}
    client.delete_event.assert_not_called()
    client.update_event.assert_not_called()


# ---------------------------------------------------------------------------
# The gate — sync (default + explicit) proceeds
# ---------------------------------------------------------------------------


def test_create_default_sync_proceeds(tmp_path):
    """No sync_policy arg → defaults to 'sync' → proceeds (the historical
    behaviour; an un-updated caller preserves today's path)."""
    client = MagicMock()
    client.create_event.return_value = "evt-new"
    file_path = _seed_event(tmp_path, fm={"type": "event", "name": "Meeting"})
    out = sync_event_create_to_gcal(
        client=client, config=_make_config(), intended_on=True,
        file_path=file_path, title="Meeting", description="",
        start_dt=_START, end_dt=_END,
    )
    assert out["event_id"] == "evt-new"
    client.create_event.assert_called_once()
    # Writeback happened.
    assert frontmatter.load(str(file_path)).metadata["gcal_event_id"] == "evt-new"


def test_update_explicit_sync_proceeds():
    client = MagicMock()
    client.update_event.return_value = {"id": "evt-1"}
    out = sync_event_update_to_gcal(
        client=client, config=_make_config(), intended_on=True,
        gcal_event_id="evt-1", title="x", sync_policy="sync",
    )
    assert out["event_id"] == "evt-1"
    client.update_event.assert_called_once()


# ---------------------------------------------------------------------------
# BEHAVIOR-PRESERVATION — existing events have NO gcal_sync field
# ---------------------------------------------------------------------------


def test_existing_synced_event_no_policy_field_still_patches():
    """The load-bearing migration pin: an existing synced event carries
    gcal_event_id but NO gcal_sync field → resolve_sync_policy → 'sync' →
    update still PATCHes. Preserves all 57-58 synced events in the corpus."""
    fm = {"type": "event", "name": "Synced", "gcal_event_id": "evt-9"}
    assert resolve_sync_policy(fm) == "sync"
    client = MagicMock()
    client.update_event.return_value = {"id": "evt-9"}
    out = sync_event_update_to_gcal(
        client=client, config=_make_config(), intended_on=True,
        gcal_event_id="evt-9", title="Renamed",
        sync_policy=resolve_sync_policy(fm),
    )
    assert out["event_id"] == "evt-9"
    client.update_event.assert_called_once()


def test_existing_unsynced_event_no_policy_field_still_syncs(tmp_path):
    """An existing unsynced event (no gcal_event_id, no gcal_sync field) →
    'sync' → create proceeds (the 81 unsynced events backfill as before)."""
    fm = {"type": "event", "name": "Unsynced"}
    assert resolve_sync_policy(fm) == "sync"
    client = MagicMock()
    client.create_event.return_value = "evt-x"
    file_path = _seed_event(tmp_path, fm=fm)
    out = sync_event_create_to_gcal(
        client=client, config=_make_config(), intended_on=True,
        file_path=file_path, title="Unsynced", description="",
        start_dt=_START, end_dt=_END, sync_policy=resolve_sync_policy(fm),
    )
    assert out["event_id"] == "evt-x"
    client.create_event.assert_called_once()


# ---------------------------------------------------------------------------
# Birthday — gcal_sync: none → zero GCal calls across create/update/delete
# ---------------------------------------------------------------------------


def test_birthday_never_syncs_across_all_ops(tmp_path):
    fm = {"type": "event", "name": "Mom Birthday", "gcal_sync": "none"}
    policy = resolve_sync_policy(fm)
    assert policy == "none"
    client = MagicMock()
    file_path = _seed_event(tmp_path, fm=fm)
    cfg = _make_config()

    assert sync_event_create_to_gcal(
        client=client, config=cfg, intended_on=True, file_path=file_path,
        title="Mom Birthday", description="", start_dt=_START, end_dt=_END,
        sync_policy=policy,
    ) == {"noop": "sync_policy_none"}
    assert sync_event_delete_to_gcal(
        client=client, config=cfg, intended_on=True,
        gcal_event_id="", sync_policy=policy,
    ) == {"noop": "sync_policy_none"}
    client.create_event.assert_not_called()
    client.delete_event.assert_not_called()


# ---------------------------------------------------------------------------
# Reminders are GCal-independent — a gcal_sync:none event still surfaces
# ---------------------------------------------------------------------------


def test_birthday_still_appears_in_upcoming_events(tmp_path):
    from alfred.brief.config import UpcomingEventsConfig
    from alfred.brief.upcoming_events import render_upcoming_events_section

    vault = tmp_path / "vault"
    (vault / "event").mkdir(parents=True)
    # A remind-only birthday: gcal_sync none, no gcal_event_id, dated soon.
    (vault / "event" / "Mom Birthday.md").write_text(
        "---\n"
        "type: event\n"
        "name: Mom Birthday\n"
        "date: 2026-05-03\n"
        "gcal_sync: none\n"
        "created: 2026-04-01\n"
        "tags: []\n"
        "---\n\n# Mom Birthday\n",
        encoding="utf-8",
    )
    out = render_upcoming_events_section(
        UpcomingEventsConfig(enabled=True, max_days_ahead=30),
        vault, date(2026, 5, 1),
    )
    assert "Mom Birthday" in out, (
        "a gcal_sync:none event must still surface in reminders "
        "(reminders read the record, not GCal)"
    )


# ---------------------------------------------------------------------------
# Entry point 2 — backfill CLI skips none-policy events
# ---------------------------------------------------------------------------


def test_backfill_skips_sync_policy_none(tmp_path, capsys):
    from alfred.integrations import gcal_cli

    event_dir = tmp_path / "event"
    event_dir.mkdir()
    (event_dir / "Birthday.md").write_text(
        frontmatter.dumps(frontmatter.Post(
            "body\n",
            type="event", name="Birthday", title="Birthday",
            start="2099-06-01T14:00:00-03:00",
            end="2099-06-01T15:00:00-03:00",
            gcal_sync="none",
        )) + "\n",
        encoding="utf-8",
    )
    raw = {
        "vault": {"path": str(tmp_path)},
        "gcal": {
            "enabled": True,
            "alfred_calendar_id": "cal@g.com",
            "alfred_calendar_label": "alfred",
        },
    }
    rc = gcal_cli.cmd_backfill(raw, dry_run=True, wants_json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["synced_count"] == 0
    assert payload["skipped_sync_policy_none"] == 1
    assert "event/Birthday.md" in payload["skipped_sync_policy_none_paths"]


# ---------------------------------------------------------------------------
# Entry point 3 — peer propose-create shim resolves policy from the file
# ---------------------------------------------------------------------------


def test_peer_sync_shim_honours_none_policy(tmp_path):
    from alfred.transport import peer_handlers
    from alfred.transport.peer_handlers import (
        _KEY_GCAL_CLIENT,
        _KEY_GCAL_CONFIG,
        _KEY_GCAL_INTENDED_ON,
    )

    client = MagicMock()
    # The peer handler writes the file before calling the shim; seed one
    # carrying gcal_sync: none.
    file_path = _seed_event(
        tmp_path, fm={"type": "event", "name": "Peer Bday", "gcal_sync": "none"},
    )
    request = MagicMock()
    request.app = {
        _KEY_GCAL_CLIENT: client,
        _KEY_GCAL_CONFIG: _make_config(),
        _KEY_GCAL_INTENDED_ON: True,
    }
    out = peer_handlers._sync_event_to_gcal(
        request, file_path=file_path, title="Peer Bday", description="",
        start_dt=_START, end_dt=_END, correlation_id="c1",
    )
    assert out == {"noop": "sync_policy_none"}
    client.create_event.assert_not_called()
