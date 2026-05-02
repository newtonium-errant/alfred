"""Tests for ``alfred gcal backfill`` + the talker daemon's hook
registration source-pin.

Coverage:
  * CLI: disabled config short-circuits with exit 1
  * CLI: missing alfred_calendar_id (live mode) short-circuits
  * CLI: missing event/ dir is a clean error
  * CLI: invalid --from-date is a clean error
  * CLI: dry-run reports what would sync without making API calls
  * CLI: dry-run does NOT need an alfred_calendar_id (rehearsal-friendly)
  * CLI: live run pushes unsynced events + writes back gcal_event_id
  * CLI: skip categorization (already_synced / no_time / before_cutoff)
  * CLI: failure reporting on per-record API errors
  * CLI: empty event/ dir → "no records found" message (intentional
    blank signal per ``feedback_intentionally_left_blank.md``)
  * CLI: top-level argparse accepts ``alfred gcal backfill --dry-run
    --from-date YYYY-MM-DD --json``
  * Daemon: source-pin confirms the GCal hook closures + registration
    calls live in the talker daemon's GCal init block
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import frontmatter
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_event(
    vault: Path, *, name: str, fields: dict | None = None,
) -> Path:
    event_dir = vault / "event"
    event_dir.mkdir(exist_ok=True)
    fm = {"type": "event", "name": name, "title": name}
    if fields:
        fm.update(fields)
    file_path = event_dir / f"{name}.md"
    post = frontmatter.Post("body\n", **fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return file_path


def _make_raw(
    vault_path: Path,
    *,
    enabled: bool = True,
    alfred_id: str = "alfred-cal@group.calendar.google.com",
    label: str = "alfred",
) -> dict:
    return {
        "vault": {"path": str(vault_path)},
        "gcal": {
            "enabled": enabled,
            "alfred_calendar_id": alfred_id,
            "alfred_calendar_label": label,
        },
    }


# ---------------------------------------------------------------------------
# Disabled / mis-configured short-circuits
# ---------------------------------------------------------------------------


def test_backfill_disabled_config(tmp_path, capsys):
    from alfred.integrations import gcal_cli

    rc = gcal_cli.cmd_backfill(
        _make_raw(tmp_path, enabled=False),
        wants_json=True,
    )
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "disabled" in payload["error"].lower()


def test_backfill_no_calendar_id_live(tmp_path, capsys):
    """Live run requires alfred_calendar_id; dry-run does not."""
    from alfred.integrations import gcal_cli

    rc = gcal_cli.cmd_backfill(
        _make_raw(tmp_path, alfred_id=""),
        wants_json=True,
    )
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "alfred_calendar_id" in payload["error"]


def test_backfill_dry_run_works_without_calendar_id(tmp_path, capsys):
    """Dry-run is rehearsal-friendly — no client needed, no ID needed."""
    from alfred.integrations import gcal_cli

    _seed_event(
        tmp_path, name="Future Event",
        fields={
            "start": "2099-06-01T14:00:00-03:00",
            "end": "2099-06-01T15:00:00-03:00",
        },
    )
    rc = gcal_cli.cmd_backfill(
        _make_raw(tmp_path, alfred_id=""),
        dry_run=True,
        wants_json=True,
    )
    # Dry-run survives even without a calendar ID — operator can
    # rehearse before plugging in real config.
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["synced_count"] == 1


def test_backfill_no_event_dir(tmp_path, capsys):
    """No event/ subdir → clean error, not a crash."""
    from alfred.integrations import gcal_cli

    rc = gcal_cli.cmd_backfill(
        _make_raw(tmp_path), wants_json=True,
    )
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "event/" in payload["error"]


def test_backfill_invalid_from_date(tmp_path, capsys):
    from alfred.integrations import gcal_cli

    (tmp_path / "event").mkdir()
    rc = gcal_cli.cmd_backfill(
        _make_raw(tmp_path),
        from_date="not-a-date",
        wants_json=True,
    )
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert "YYYY-MM-DD" in payload["error"]


# ---------------------------------------------------------------------------
# Skip categorization
# ---------------------------------------------------------------------------


def test_backfill_skips_already_synced(tmp_path, capsys):
    """Records with gcal_event_id are skipped (already pushed)."""
    from alfred.integrations import gcal_cli

    _seed_event(
        tmp_path, name="Already Synced",
        fields={
            "start": "2099-06-01T14:00:00-03:00",
            "end": "2099-06-01T15:00:00-03:00",
            "gcal_event_id": "existing-id-1",
        },
    )
    rc = gcal_cli.cmd_backfill(
        _make_raw(tmp_path), dry_run=True, wants_json=True,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["synced_count"] == 0
    assert payload["skipped_already_synced"] == 1


def test_backfill_skips_no_time(tmp_path, capsys):
    """Date-only events (no start/end) are skipped — we don't fabricate times."""
    from alfred.integrations import gcal_cli

    _seed_event(
        tmp_path, name="Date Only",
        fields={"date": "2099-06-01"},  # no start/end
    )
    rc = gcal_cli.cmd_backfill(
        _make_raw(tmp_path), dry_run=True, wants_json=True,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["synced_count"] == 0
    assert payload["skipped_no_time"] == 1
    assert "event/Date Only.md" in payload["skipped_no_time_paths"]


def test_backfill_skips_before_cutoff(tmp_path, capsys):
    """Events before --from-date are skipped."""
    from alfred.integrations import gcal_cli

    _seed_event(
        tmp_path, name="Old Event",
        fields={
            "start": "2020-01-01T14:00:00-03:00",
            "end": "2020-01-01T15:00:00-03:00",
        },
    )
    rc = gcal_cli.cmd_backfill(
        _make_raw(tmp_path),
        dry_run=True,
        from_date="2024-01-01",
        wants_json=True,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["synced_count"] == 0
    assert payload["skipped_before_cutoff"] == 1


def test_backfill_skips_naive_datetime(tmp_path, capsys):
    """Naive (no tz) start/end are treated as no_time — refuse to guess tz."""
    from alfred.integrations import gcal_cli

    _seed_event(
        tmp_path, name="Naive",
        fields={
            "start": "2099-06-01T14:00:00",  # no tz
            "end": "2099-06-01T15:00:00",
        },
    )
    rc = gcal_cli.cmd_backfill(
        _make_raw(tmp_path), dry_run=True, wants_json=True,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped_no_time"] == 1


# ---------------------------------------------------------------------------
# Live sync path
# ---------------------------------------------------------------------------


def test_backfill_live_sync_writes_back_id(tmp_path, capsys):
    """Live run: unsynced event gets pushed, gcal_event_id written back."""
    from alfred.integrations import gcal_cli

    file_path = _seed_event(
        tmp_path, name="Halifax Music Fest 2026 — Weezer",
        fields={
            "start": "2026-06-27T19:00:00-03:00",
            "end": "2026-06-27T22:00:00-03:00",
            "summary": "TIXR ticket scanned",
        },
    )

    fake_client = MagicMock()
    fake_client.create_event.return_value = "weezer-gcal-id-1"

    with patch("alfred.integrations.gcal.GCalClient", return_value=fake_client):
        rc = gcal_cli.cmd_backfill(
            _make_raw(tmp_path),
            from_date="2025-01-01",  # backfill historical
            wants_json=True,
        )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["synced_count"] == 1
    assert payload["failed_count"] == 0
    # Vault frontmatter has the writeback (the sync function does it
    # via the same gcal_sync.sync_event_create_to_gcal path).
    fm = frontmatter.load(str(file_path))
    assert fm["gcal_event_id"] == "weezer-gcal-id-1"
    assert fm["gcal_calendar"] == "alfred"


def test_backfill_records_failures(tmp_path, capsys):
    """Per-record failures show up in the failed list with code + detail."""
    from alfred.integrations import gcal_cli
    from alfred.integrations.gcal import GCalAPIError

    _seed_event(
        tmp_path, name="Fails To Sync",
        fields={
            "start": "2099-06-01T14:00:00-03:00",
            "end": "2099-06-01T15:00:00-03:00",
        },
    )
    fake_client = MagicMock()
    fake_client.create_event.side_effect = GCalAPIError("simulated quota")

    with patch("alfred.integrations.gcal.GCalClient", return_value=fake_client):
        rc = gcal_cli.cmd_backfill(
            _make_raw(tmp_path), wants_json=True,
        )
    # Failure → exit 1.
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["failed_count"] == 1
    assert payload["failed"][0]["code"] == "api_error"
    assert "quota" in payload["failed"][0]["detail"]


def test_backfill_human_output_no_records(tmp_path, capsys):
    """Empty event/ → explicit 'no records found' message."""
    from alfred.integrations import gcal_cli

    (tmp_path / "event").mkdir()
    rc = gcal_cli.cmd_backfill(
        _make_raw(tmp_path),
        dry_run=True,
        wants_json=False,
    )
    assert rc == 0
    out = capsys.readouterr().out
    # Per feedback_intentionally_left_blank — explicit "ran, nothing to do".
    assert "No event records found" in out


def test_backfill_human_output_synced_lines(tmp_path, capsys):
    """Non-JSON output lists synced records line-by-line."""
    from alfred.integrations import gcal_cli

    _seed_event(
        tmp_path, name="Future One",
        fields={
            "start": "2099-06-01T14:00:00-03:00",
            "end": "2099-06-01T15:00:00-03:00",
        },
    )
    rc = gcal_cli.cmd_backfill(
        _make_raw(tmp_path), dry_run=True, wants_json=False,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "would sync" in out.lower()
    assert "Future One" in out


# ---------------------------------------------------------------------------
# Top-level CLI parser
# ---------------------------------------------------------------------------


def test_alfred_gcal_backfill_subcommand_registered():
    from alfred.cli import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "gcal", "backfill", "--dry-run", "--from-date", "2025-01-01", "--json",
    ])
    assert args.command == "gcal"
    assert args.gcal_cmd == "backfill"
    assert args.dry_run is True
    assert args.from_date == "2025-01-01"
    assert args.json is True


def test_alfred_gcal_backfill_defaults():
    from alfred.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["gcal", "backfill"])
    assert args.dry_run is False
    assert args.from_date is None
    assert args.json is False


# ---------------------------------------------------------------------------
# Talker daemon source-pin (hook registration)
# ---------------------------------------------------------------------------


def test_talker_daemon_registers_all_three_event_hooks():
    """Source-pin: confirm the talker daemon registers create + update +
    delete hooks at startup, alongside the existing GCal init block.

    Pure source-text inspection (same pattern as the prior
    ``test_talker_daemon_sets_intended_on_before_client_construction``)
    — brittle by design so a refactor that drops the hook registration
    must replace it with an equivalent path or this test fails loudly.
    """
    here = Path(__file__).resolve().parent
    daemon_path = here.parent / "src" / "alfred" / "telegram" / "daemon.py"
    source = daemon_path.read_text(encoding="utf-8")

    # All three register helpers must be imported.
    assert "from alfred.vault.ops import (" in source, (
        "talker daemon must import the event-hook register helpers "
        "from alfred.vault.ops"
    )
    assert "register_event_create_hook," in source, (
        "talker daemon must import register_event_create_hook"
    )
    assert "register_event_update_hook," in source, (
        "talker daemon must import register_event_update_hook"
    )
    assert "register_event_delete_hook," in source, (
        "talker daemon must import register_event_delete_hook"
    )

    # All three sync functions must be imported.
    assert "sync_event_create_to_gcal," in source
    assert "sync_event_update_to_gcal," in source
    assert "sync_event_delete_to_gcal," in source

    # All three registration calls must appear.
    assert "register_event_create_hook(_on_event_created)" in source
    assert "register_event_update_hook(_on_event_updated)" in source
    assert "register_event_delete_hook(_on_event_deleted)" in source


def test_talker_daemon_hook_registration_inside_gcal_enabled_block():
    """The hook registration must be inside the ``if config.enabled``
    branch, not above it. Otherwise instances that opted OUT of GCal
    would still register hooks that try to call a None client at
    every vault_create on event records.
    """
    here = Path(__file__).resolve().parent
    daemon_path = here.parent / "src" / "alfred" / "telegram" / "daemon.py"
    source = daemon_path.read_text(encoding="utf-8")

    # Find the order: enabled-check, then hook registration.
    enabled_check_idx = source.find("if gcal_config_candidate.enabled:")
    register_create_idx = source.find("register_event_create_hook(_on_event_created)")
    disabled_log_idx = source.find('log.info("talker.daemon.gcal_disabled")')

    assert enabled_check_idx > 0
    assert register_create_idx > 0
    assert disabled_log_idx > 0
    assert enabled_check_idx < register_create_idx, (
        "hook registration must come AFTER the gcal_config_candidate.enabled "
        "check so disabled instances skip registration entirely"
    )
    assert register_create_idx < disabled_log_idx, (
        "hook registration must be inside the if-branch (above the "
        "else: gcal_disabled log)"
    )
