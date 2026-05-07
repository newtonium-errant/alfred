"""Tests pinning the ``gcal_title`` decoupling contract.

Spec: ``vault/note/GCal Title Field — Decouple Event Record Name From
Calendar Title.md``.

The vault filename / ``name`` field needs date-suffixed disambiguators
for findability (``Novaket — May 13``, ``Fergus Bath 2026-05-12``);
GCal renders the date in its own UI so the suffix is redundant noise on
the calendar entry. ``gcal_title`` is an optional operator-set
override: when present the sync layer prefers it over ``name``;
otherwise the existing ``fm.title or fm.name`` fallback chain stays in
effect (regression-safe).

Coverage:
  * :func:`alfred.integrations.gcal_sync.resolve_gcal_title` — every
    branch of the precedence chain (gcal_title, title, name, empty)
    plus the whitespace + non-string defensive paths.
  * :func:`sync_event_create_to_gcal` — when called with
    ``title_source`` kwarg, ``gcal.sync_created`` log carries it.
  * :func:`sync_event_update_to_gcal` — when called with title +
    title_source, ``gcal.sync_updated`` log carries title_source;
    when title is None (untouched), title_source is None too.
  * Daemon hook reproduction — vault_edit adding ``gcal_title``
    triggers a PATCH with the override (PATCH branch's title_changed
    gate fires on ``gcal_title`` in fields_changed).
  * Backfill — records with ``gcal_title`` push the override to GCal,
    not the filename stem.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import frontmatter
from structlog.testing import capture_logs


# ---------------------------------------------------------------------------
# resolve_gcal_title — the canonical precedence helper
# ---------------------------------------------------------------------------


class TestResolveGcalTitle:
    def test_gcal_title_wins_when_set(self):
        from alfred.integrations.gcal_sync import resolve_gcal_title

        fm = {
            "gcal_title": "Novaket",
            "title": "Novaket — May 13",
            "name": "Novaket — May 13",
        }
        title, source = resolve_gcal_title(fm)
        assert title == "Novaket"
        assert source == "gcal_title"

    def test_falls_back_to_title_when_gcal_title_absent(self):
        from alfred.integrations.gcal_sync import resolve_gcal_title

        fm = {"title": "Halifax Music Fest", "name": "Halifax Music Fest"}
        title, source = resolve_gcal_title(fm)
        assert title == "Halifax Music Fest"
        assert source == "title"

    def test_falls_back_to_name_when_neither_gcal_title_nor_title(self):
        """Daemon-direct creates typically only set ``name``."""
        from alfred.integrations.gcal_sync import resolve_gcal_title

        fm = {"name": "Coaching Session"}
        title, source = resolve_gcal_title(fm)
        assert title == "Coaching Session"
        assert source == "name"

    def test_empty_when_nothing_present(self):
        from alfred.integrations.gcal_sync import resolve_gcal_title

        fm = {"type": "event", "summary": "no title fields"}
        title, source = resolve_gcal_title(fm)
        assert title == ""
        assert source == ""

    def test_whitespace_only_gcal_title_is_skipped(self):
        """A blank ``gcal_title`` shouldn't shadow the real title."""
        from alfred.integrations.gcal_sync import resolve_gcal_title

        fm = {"gcal_title": "   ", "name": "Real Name"}
        title, source = resolve_gcal_title(fm)
        assert title == "Real Name"
        assert source == "name"

    def test_non_string_gcal_title_is_skipped(self):
        """Defensive — yaml ``null`` / int / list shouldn't crash."""
        from alfred.integrations.gcal_sync import resolve_gcal_title

        for bogus in (None, 42, [], {}):
            fm = {"gcal_title": bogus, "name": "Real Name"}
            title, source = resolve_gcal_title(fm)
            assert title == "Real Name"
            assert source == "name"

    def test_strips_whitespace_on_gcal_title(self):
        from alfred.integrations.gcal_sync import resolve_gcal_title

        fm = {"gcal_title": "  Novaket  "}
        title, source = resolve_gcal_title(fm)
        assert title == "Novaket"
        assert source == "gcal_title"


# ---------------------------------------------------------------------------
# Sync-layer log emission — title_source field on gcal.sync_created
# ---------------------------------------------------------------------------


def _make_config(*, label: str = "alfred"):
    from alfred.integrations.gcal_config import GCalConfig
    return GCalConfig(
        enabled=True,
        alfred_calendar_id="alfred-cal@group.calendar.google.com",
        alfred_calendar_label=label,
    )


def _seed_event(tmp_path: Path, *, fm: dict) -> Path:
    event_dir = tmp_path / "event"
    event_dir.mkdir(exist_ok=True)
    file_path = event_dir / f"{fm.get('name', 'evt')}.md"
    post = frontmatter.Post("body\n", **fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return file_path


class TestSyncCreateLogsTitleSource:
    """``gcal.sync_created`` carries ``title_source`` (load-bearing for
    operators grepping the daemon log: "which records used the override")."""

    def test_default_title_source_is_name(self, tmp_path):
        """Pre-decoupling regression guard: caller that doesn't pass
        title_source still emits the field with default ``"name"``."""
        from alfred.integrations.gcal_sync import sync_event_create_to_gcal

        file_path = _seed_event(tmp_path, fm={"type": "event", "name": "x"})
        client = MagicMock()
        client.create_event.return_value = "id-default"
        config = _make_config()

        with capture_logs() as captured:
            sync_event_create_to_gcal(
                client=client, config=config,
                file_path=file_path,
                title="x", description="",
                start_dt=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
                end_dt=datetime(2026, 6, 1, 15, tzinfo=timezone.utc),
                correlation_id="t-default-source",
            )
        created_logs = [c for c in captured if c.get("event") == "gcal.sync_created"]
        assert len(created_logs) == 1
        assert created_logs[0]["title_source"] == "name"

    def test_title_source_gcal_title_emitted_on_log(self, tmp_path):
        from alfred.integrations.gcal_sync import sync_event_create_to_gcal

        file_path = _seed_event(tmp_path, fm={"type": "event", "name": "x"})
        client = MagicMock()
        client.create_event.return_value = "id-override"
        config = _make_config()

        with capture_logs() as captured:
            sync_event_create_to_gcal(
                client=client, config=config,
                file_path=file_path,
                title="Novaket", description="",
                start_dt=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
                end_dt=datetime(2026, 6, 1, 15, tzinfo=timezone.utc),
                correlation_id="t-override-source",
                title_source="gcal_title",
            )
        created_logs = [c for c in captured if c.get("event") == "gcal.sync_created"]
        assert len(created_logs) == 1
        assert created_logs[0]["title_source"] == "gcal_title"


class TestSyncUpdateLogsTitleSource:
    def test_title_source_carried_when_title_patched(self, tmp_path):
        from alfred.integrations.gcal_sync import sync_event_update_to_gcal

        client = MagicMock()
        # update_event returns a non-None GCalEvent on success.
        from alfred.integrations.gcal import GCalEvent
        client.update_event.return_value = GCalEvent(
            id="id-1", calendar_id="cal", title="Novaket",
            start=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
            end=datetime(2026, 6, 1, 15, tzinfo=timezone.utc),
        )
        config = _make_config()

        with capture_logs() as captured:
            sync_event_update_to_gcal(
                client=client, config=config,
                gcal_event_id="id-1",
                title="Novaket",
                title_source="gcal_title",
                correlation_id="t-update-source",
            )
        upd_logs = [c for c in captured if c.get("event") == "gcal.sync_updated"]
        assert len(upd_logs) == 1
        assert upd_logs[0]["title_source"] == "gcal_title"

    def test_title_source_none_when_title_untouched(self, tmp_path):
        """Patch that doesn't touch title (e.g. only start changed)
        emits ``title_source=None`` so the log unambiguously says "we
        didn't decide a source on this patch."""
        from alfred.integrations.gcal_sync import sync_event_update_to_gcal
        from alfred.integrations.gcal import GCalEvent

        client = MagicMock()
        client.update_event.return_value = GCalEvent(
            id="id-1", calendar_id="cal", title="unchanged",
            start=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
            end=datetime(2026, 6, 1, 15, tzinfo=timezone.utc),
        )
        config = _make_config()

        with capture_logs() as captured:
            sync_event_update_to_gcal(
                client=client, config=config,
                gcal_event_id="id-1",
                start_dt=datetime(2026, 6, 1, 16, tzinfo=timezone.utc),
                end_dt=datetime(2026, 6, 1, 17, tzinfo=timezone.utc),
                correlation_id="t-no-title-patch",
            )
        upd_logs = [c for c in captured if c.get("event") == "gcal.sync_updated"]
        assert len(upd_logs) == 1
        assert upd_logs[0]["title_source"] is None


# ---------------------------------------------------------------------------
# vault_edit adding gcal_title → PATCH routing (daemon hook contract)
# ---------------------------------------------------------------------------


def _build_update_closure(create_fn, update_fn, log):
    """Mirror the daemon's ``_on_event_updated`` closure — same logic
    as the reproduction in test_telegram_gcal_update_hook.py but
    standalone so this test file doesn't cross-import."""
    from datetime import datetime as _dt

    from alfred.integrations.gcal_sync import resolve_gcal_title

    def _on_event_updated(vault_path_, rel_path, fm, fields_changed):
        gcal_event_id = str(fm.get("gcal_event_id") or "")
        start_raw = fm.get("start")
        end_raw = fm.get("end")

        if not gcal_event_id and start_raw and end_raw:
            try:
                start_dt = _dt.fromisoformat(str(start_raw))
                end_dt = _dt.fromisoformat(str(end_raw))
            except Exception:
                return
            log.info("gcal.sync_promoted_to_create", rel_path=rel_path)
            resolved_title, title_source = resolve_gcal_title(fm)
            create_fn(
                client=MagicMock(), config=MagicMock(), intended_on=False,
                file_path=Path(vault_path_) / rel_path,
                title=resolved_title,
                description=str(fm.get("summary") or ""),
                start_dt=start_dt, end_dt=end_dt,
                correlation_id="",
                title_source=title_source,
            )
            return

        if not gcal_event_id:
            return

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
        update_fn(
            client=MagicMock(), config=MagicMock(), intended_on=False,
            gcal_event_id=gcal_event_id,
            title=title,
            description=None,
            start_dt=None,
            end_dt=None,
            correlation_id="",
            title_source=title_source,
        )

    return _on_event_updated


class TestVaultEditAddingGcalTitleFiresUpdate:
    """vault_edit set ``gcal_title`` on an already-synced record →
    update hook routes through PATCH with the override."""

    def test_adding_gcal_title_to_synced_record_patches_with_override(self, tmp_path):
        create_fn = MagicMock()
        update_fn = MagicMock()
        log = MagicMock()
        closure = _build_update_closure(create_fn, update_fn, log)

        # Record was previously synced (has gcal_event_id); operator
        # adds gcal_title via vault_edit. fields_changed reflects ONLY
        # the new field; existing name + title untouched.
        fm = {
            "type": "event",
            "name": "Novaket — May 13",
            "title": "Novaket — May 13",
            "gcal_title": "Novaket",
            "gcal_event_id": "synced-id-1",
            "start": "2026-05-13T19:00:00-03:00",
            "end": "2026-05-13T22:00:00-03:00",
        }
        closure(tmp_path, "event/Novaket — May 13.md", fm, ["gcal_title"])

        update_fn.assert_called_once()
        create_fn.assert_not_called()
        kwargs = update_fn.call_args.kwargs
        assert kwargs["title"] == "Novaket"
        assert kwargs["title_source"] == "gcal_title"
        assert kwargs["gcal_event_id"] == "synced-id-1"

    def test_unrelated_edit_doesnt_re_push_title(self, tmp_path):
        """vault_edit changes ``tags`` but title fields untouched →
        title=None passed to update_fn (no spurious title patch)."""
        create_fn = MagicMock()
        update_fn = MagicMock()
        log = MagicMock()
        closure = _build_update_closure(create_fn, update_fn, log)

        fm = {
            "type": "event",
            "name": "Novaket — May 13",
            "gcal_title": "Novaket",  # set previously, untouched now
            "gcal_event_id": "synced-id-1",
            "start": "2026-05-13T19:00:00-03:00",
            "end": "2026-05-13T22:00:00-03:00",
            "tags": ["live"],
        }
        closure(tmp_path, "event/Novaket — May 13.md", fm, ["tags"])

        update_fn.assert_called_once()
        kwargs = update_fn.call_args.kwargs
        assert kwargs["title"] is None
        assert kwargs["title_source"] is None


# ---------------------------------------------------------------------------
# Create hook resolves gcal_title — fresh vault_create with override
# ---------------------------------------------------------------------------


class TestCreateHookHonorsGcalTitle:
    def test_create_with_gcal_title_passes_override_to_sync(self, tmp_path):
        """Vault create on an event record with ``gcal_title`` set →
        sync layer receives the override + the source label."""
        from alfred.integrations.gcal_sync import (
            resolve_gcal_title,
            sync_event_create_to_gcal,
        )

        fm = {
            "type": "event",
            "name": "Fergus Bath 2026-05-12",
            "title": "Fergus Bath 2026-05-12",
            "gcal_title": "Fergus Bath",
        }
        file_path = _seed_event(tmp_path, fm=fm)

        client = MagicMock()
        client.create_event.return_value = "id-fergus"
        config = _make_config()

        # Simulate the hook's resolve+pass flow.
        resolved_title, title_source = resolve_gcal_title(fm)
        assert resolved_title == "Fergus Bath"
        assert title_source == "gcal_title"

        with capture_logs() as captured:
            result = sync_event_create_to_gcal(
                client=client, config=config,
                file_path=file_path,
                title=resolved_title,
                description="",
                start_dt=datetime(2026, 5, 12, 18, tzinfo=timezone.utc),
                end_dt=datetime(2026, 5, 12, 19, tzinfo=timezone.utc),
                correlation_id="t-fergus",
                title_source=title_source,
            )
        assert result["event_id"] == "id-fergus"
        # GCal API received the override, NOT the filename stem.
        kwargs = client.create_event.call_args.kwargs
        assert kwargs["title"] == "Fergus Bath"
        # Log carries the source label.
        created_logs = [c for c in captured if c.get("event") == "gcal.sync_created"]
        assert len(created_logs) == 1
        assert created_logs[0]["title_source"] == "gcal_title"

    def test_create_without_gcal_title_falls_back_to_name(self, tmp_path):
        """Regression guard: existing records (no gcal_title) → GCal
        title stays equal to ``name`` / ``title`` (current behavior)."""
        from alfred.integrations.gcal_sync import (
            resolve_gcal_title,
            sync_event_create_to_gcal,
        )

        fm = {
            "type": "event",
            "name": "Coaching Session",
            # No gcal_title, no title
        }
        file_path = _seed_event(tmp_path, fm=fm)

        client = MagicMock()
        client.create_event.return_value = "id-coaching"
        config = _make_config()

        resolved_title, title_source = resolve_gcal_title(fm)
        assert resolved_title == "Coaching Session"
        assert title_source == "name"

        sync_event_create_to_gcal(
            client=client, config=config,
            file_path=file_path,
            title=resolved_title,
            description="",
            start_dt=datetime(2026, 6, 1, 14, tzinfo=timezone.utc),
            end_dt=datetime(2026, 6, 1, 15, tzinfo=timezone.utc),
            correlation_id="t-coaching",
            title_source=title_source,
        )
        kwargs = client.create_event.call_args.kwargs
        assert kwargs["title"] == "Coaching Session"


# ---------------------------------------------------------------------------
# Schema doc — EVENT_GCAL_FIELDS includes gcal_title
# ---------------------------------------------------------------------------


def test_schema_event_gcal_fields_includes_gcal_title():
    """Pin: ``EVENT_GCAL_FIELDS`` lists ``gcal_title`` so the schema
    layer + future janitor / migration code has a single source of
    truth for which optional fields touch the GCal sync layer."""
    from alfred.vault.schema import EVENT_GCAL_FIELDS

    assert "gcal_title" in EVENT_GCAL_FIELDS
    # The other three Phase A+ sync fields stay listed too.
    assert "gcal_event_id" in EVENT_GCAL_FIELDS
    assert "gcal_calendar" in EVENT_GCAL_FIELDS
    assert "gcal_keep_on_cancel" in EVENT_GCAL_FIELDS


# ---------------------------------------------------------------------------
# Backfill — gcal_title flows through to GCal on bulk push
# ---------------------------------------------------------------------------


class TestBackfillHonorsGcalTitle:
    """Pin the backfill CLI's ``cmd_backfill`` resolver — records with
    ``gcal_title`` push the override, not the filename stem."""

    def test_backfill_dry_run_reports_override_title(self, tmp_path, monkeypatch):
        """Dry-run backfill report uses the resolved title (so operator
        sees what GCal would see, not the filename).

        Stubs the lazy imports inside ``cmd_backfill`` at their source
        modules (``gcal_config.load_from_unified`` and
        ``gcal_cli._resolve_vault_path``) so the function-local
        ``from .gcal_config import load_from_unified`` lands on our
        stub.
        """
        from alfred.integrations import gcal_cli, gcal_config

        # Build a fake vault with one event that has gcal_title set.
        vault = tmp_path / "vault"
        event_dir = vault / "event"
        event_dir.mkdir(parents=True)
        record = event_dir / "Novaket — May 13.md"
        record.write_text(
            "---\n"
            "type: event\n"
            "name: Novaket — May 13\n"
            "title: Novaket — May 13\n"
            "gcal_title: Novaket\n"
            "start: '2026-05-13T19:00:00-03:00'\n"
            "end: '2026-05-13T22:00:00-03:00'\n"
            "---\n\n",
            encoding="utf-8",
        )

        from alfred.integrations.gcal_config import GCalConfig
        monkeypatch.setattr(
            gcal_config, "load_from_unified",
            lambda raw: GCalConfig(
                enabled=True,
                alfred_calendar_id="cal-id",
                alfred_calendar_label="alfred",
            ),
        )
        monkeypatch.setattr(
            gcal_cli, "_resolve_vault_path", lambda raw: vault,
        )

        # Capture the JSON output via wants_json + a print monkeypatch.
        captured: list[str] = []
        monkeypatch.setattr(gcal_cli, "_print", lambda s: captured.append(s))

        # Future-dated cutoff would skip the record; pass an early one.
        rc = gcal_cli.cmd_backfill(
            raw={},
            dry_run=True,
            from_date="2026-01-01",
            wants_json=True,
        )
        assert rc == 0, f"cmd_backfill returned {rc}; captured={captured}"
        # Parse the emitted JSON; the synced[0].title is the override,
        # NOT the filename stem.
        import json as _json
        # Find the JSON line (last printed object).
        payload_lines = [c for c in captured if c.lstrip().startswith("{")]
        assert payload_lines, f"no JSON output captured: {captured}"
        data = _json.loads(payload_lines[-1])
        assert data["ok"] is True
        synced = data.get("synced", [])
        assert len(synced) == 1, f"expected 1 record, got {synced}"
        assert synced[0]["title"] == "Novaket"
        assert "Novaket — May 13" not in synced[0]["title"]
