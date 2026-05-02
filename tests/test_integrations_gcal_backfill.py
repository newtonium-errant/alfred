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


def _parse_json_payload(captured_stdout: str) -> dict:
    """Pull the JSON object out of captured stdout, ignoring leading log lines.

    The gcal sync path (``gcal_sync.sync_event_create_to_gcal``) emits
    ``log.info("gcal.sync_created", ...)`` etc. via structlog with the
    default ``ConsoleRenderer`` sink, which lands on stdout — same
    sink as the ``--json`` output. Tests that exercise live sync see
    log lines BEFORE the JSON object and ``json.loads`` chokes on the
    leading non-JSON content.

    Find the first line whose stripped form starts with ``{`` and parse
    from there. Robust against any number of leading log lines because
    structlog's ConsoleRenderer never emits lines that start with ``{``
    (they start with a timestamp).

    Tests that DON'T trigger sync (dry-run / empty / pre-flight error
    paths) get a clean stdout where the JSON is the only content;
    those tests can keep using ``json.loads`` directly. This helper is
    only needed for the live-sync tests.
    """
    lines = captured_stdout.splitlines(keepends=True)
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("{"):
            return json.loads("".join(lines[idx:]))
    raise AssertionError(
        f"no JSON object found in captured stdout. "
        f"Captured: {captured_stdout!r}"
    )


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
    # Live sync emits structlog ``gcal.sync_created`` lines on stdout
    # before the JSON payload — use the parser helper that skips
    # leading log lines.
    payload = _parse_json_payload(capsys.readouterr().out)
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
    # Live sync emits ``gcal.sync_create_failed`` warnings on stdout
    # before the JSON payload — use the parser helper that skips
    # leading log lines.
    payload = _parse_json_payload(capsys.readouterr().out)
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


# ---------------------------------------------------------------------------
# --infer-times: time-string parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw, expected", [
    # PM forms
    ("4:00 PM", (16, 0)),
    ("4 PM", (16, 0)),
    ("4:00PM", (16, 0)),
    ("4pm", (16, 0)),
    ("4:30 PM", (16, 30)),
    ("12:00 PM", (12, 0)),  # noon
    ("12 PM", (12, 0)),
    ("12:30 PM", (12, 30)),
    # AM forms
    ("9 a.m.", (9, 0)),
    ("9:00 AM", (9, 0)),
    ("9am", (9, 0)),
    ("9:15 AM", (9, 15)),
    ("12:00 AM", (0, 0)),  # midnight
    ("12 AM", (0, 0)),
    # 24-hour
    ("14:30", (14, 30)),
    ("16:00", (16, 0)),
    ("00:00", (0, 0)),
    ("23:59", (23, 59)),
    # Word forms
    ("noon", (12, 0)),
    ("midnight", (0, 0)),
    ("NOON", (12, 0)),  # case-insensitive
    # Whitespace tolerance
    ("  4:00 PM  ", (16, 0)),
])
def test_parse_time_string_valid(raw, expected):
    from alfred.integrations.gcal_cli import _parse_time_string
    assert _parse_time_string(raw) == expected


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "garbage",
    "25:00",  # hour out of range
    "12:60",  # minute out of range
    "abc:def",
    "4 to 5 PM",  # range, not a time
    None,
])
def test_parse_time_string_invalid(raw):
    from alfred.integrations.gcal_cli import _parse_time_string
    assert _parse_time_string(raw) is None


# ---------------------------------------------------------------------------
# --infer-times: duration heuristics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title, expected_minutes, expected_label_substr", [
    ("Halifax Music Festival", 150, "concert/show"),
    ("Weezer concert", 150, "concert/show"),
    ("VAC marketing meeting", 60, "meal/meeting"),
    ("Dental appointment", 60, "appointment"),
    ("Dr Smith consult", 60, "appointment"),
    ("Eye exam", 60, "appointment"),
    ("Physio session", 60, "appointment"),
    ("Lunch with Dave", 60, "meal/meeting"),
    ("Dinner reservation", 60, "meal/meeting"),
    ("TIXR ticket scanned", 150, "concert/show"),
    # Case insensitivity
    ("FESTIVAL DAY", 150, "concert/show"),
    # Falls back to 1h default
    ("Random thing", 60, "no match"),
    ("", 60, "no title"),
])
def test_infer_duration_minutes(title, expected_minutes, expected_label_substr):
    from alfred.integrations.gcal_cli import _infer_duration_minutes
    minutes, label = _infer_duration_minutes(title)
    assert minutes == expected_minutes
    assert expected_label_substr in label


def test_concert_beats_appointment_when_both_match():
    """If a title contains both 'concert' and 'appointment', the longer
    duration wins (concert listed first in the heuristic table)."""
    from alfred.integrations.gcal_cli import _infer_duration_minutes
    minutes, label = _infer_duration_minutes("concert appointment overlap")
    assert minutes == 150
    assert "concert/show" in label


# ---------------------------------------------------------------------------
# --infer-times: per-record inference
# ---------------------------------------------------------------------------


def test_infer_times_for_record_happy_path():
    """date+time present, parseable, with a heuristic-matching title."""
    from alfred.integrations.gcal_cli import _infer_times_for_record

    fm = {
        "type": "event",
        "title": "Halifax Music Fest 2026 — Weezer",
        "date": "2026-06-27",
        "time": "7:00 PM",
    }
    result = _infer_times_for_record(fm)
    assert result is not None
    assert "reason" not in result
    # June → ADT (-03:00)
    assert result["start"] == "2026-06-27T19:00:00-03:00"
    assert result["duration_min"] == 150  # concert/show heuristic
    assert "concert/show" in result["heuristic"]
    # End is start + 2.5h
    assert "21:30:00-03:00" in result["end"]


def test_infer_times_for_record_winter_uses_ast():
    """December → AST (-04:00)."""
    from alfred.integrations.gcal_cli import _infer_times_for_record

    fm = {
        "title": "Winter dentist appointment",
        "date": "2026-12-15",
        "time": "10:00 AM",
    }
    result = _infer_times_for_record(fm)
    assert result["start"] == "2026-12-15T10:00:00-04:00"
    assert result["duration_min"] == 60


def test_infer_times_for_record_no_date_skipped():
    from alfred.integrations.gcal_cli import _infer_times_for_record

    result = _infer_times_for_record({"time": "4:00 PM"})
    assert result == {"reason": "no_date"}


def test_infer_times_for_record_no_time_string_skipped():
    """Date-only record (no time field) → skipped under explicit reason."""
    from alfred.integrations.gcal_cli import _infer_times_for_record

    result = _infer_times_for_record({"date": "2026-06-27"})
    assert result == {"reason": "no_time_string"}


def test_infer_times_for_record_unparseable_time_skipped():
    from alfred.integrations.gcal_cli import _infer_times_for_record

    result = _infer_times_for_record({
        "date": "2026-06-27", "time": "garbage time string"
    })
    assert result["reason"] == "unparseable_time"
    assert result["raw_time"] == "garbage time string"


def test_infer_times_for_record_already_has_iso_returns_none():
    """Record already has start/end → no inference, return None."""
    from alfred.integrations.gcal_cli import _infer_times_for_record

    result = _infer_times_for_record({
        "start": "2026-06-27T19:00:00-03:00",
        "end": "2026-06-27T21:30:00-03:00",
        "date": "2026-06-27",
        "time": "7:00 PM",
    })
    assert result is None


# ---------------------------------------------------------------------------
# --infer-times: end-to-end CLI
# ---------------------------------------------------------------------------


def test_backfill_without_infer_times_skips_legacy_records(tmp_path, capsys):
    """Default behavior: legacy date+time records are SKIPPED (no inference)."""
    from alfred.integrations import gcal_cli

    _seed_event(
        tmp_path, name="Legacy",
        fields={"date": "2099-06-27", "time": "4:00 PM"},
    )
    rc = gcal_cli.cmd_backfill(
        _make_raw(tmp_path), dry_run=True, wants_json=True,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["synced_count"] == 0
    assert payload["skipped_no_time"] == 1
    assert payload.get("inferred_count", 0) == 0


def test_backfill_with_infer_times_dry_run_inferred(tmp_path, capsys):
    """--infer-times in dry-run reports inference but writes nothing."""
    from alfred.integrations import gcal_cli

    file_path = _seed_event(
        tmp_path, name="Halifax Music Fest 2026 — Weezer",
        fields={"date": "2099-06-27", "time": "7:00 PM"},
    )
    rc = gcal_cli.cmd_backfill(
        _make_raw(tmp_path),
        dry_run=True,
        infer_times=True,
        wants_json=True,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # Inferred bucket populated; not synced (dry-run).
    assert payload["inferred_count"] == 1
    assert payload["synced_count"] == 1  # would-sync after inference
    assert payload["skipped_no_time"] == 0
    inferred = payload["inferred"][0]
    assert inferred["raw_date"] == "2099-06-27"
    assert inferred["raw_time"] == "7:00 PM"
    assert inferred["start"].startswith("2099-06-27T19:00:00")
    assert inferred["duration_min"] == 150
    # Vault NOT touched on dry-run.
    fm = frontmatter.load(str(file_path))
    assert "start" not in fm.metadata
    assert "end" not in fm.metadata


def test_backfill_with_infer_times_live_writes_back_then_syncs(tmp_path, capsys):
    """Live --infer-times writes start/end to vault BEFORE pushing to GCal."""
    from alfred.integrations import gcal_cli

    file_path = _seed_event(
        tmp_path, name="Halifax Music Fest 2026 — Weezer",
        fields={"date": "2099-06-27", "time": "7:00 PM"},
    )
    fake_client = MagicMock()
    fake_client.create_event.return_value = "weezer-id-99"

    with patch("alfred.integrations.gcal.GCalClient", return_value=fake_client):
        rc = gcal_cli.cmd_backfill(
            _make_raw(tmp_path),
            infer_times=True,
            wants_json=True,
        )
    assert rc == 0
    payload = _parse_json_payload(capsys.readouterr().out)
    assert payload["inferred_count"] == 1
    assert payload["synced_count"] == 1
    # Vault frontmatter has BOTH the inferred start/end AND the
    # gcal_event_id from the sync writeback.
    fm = frontmatter.load(str(file_path))
    assert fm["start"].startswith("2099-06-27T19:00:00")
    assert fm["end"].startswith("2099-06-27T21:30:00")
    assert fm["gcal_event_id"] == "weezer-id-99"
    assert fm["gcal_calendar"] == "alfred"


def test_backfill_with_infer_times_unparseable_time_skipped(tmp_path, capsys):
    """Garbage time string → bucket as skipped_unparseable_time, not inferred."""
    from alfred.integrations import gcal_cli

    _seed_event(
        tmp_path, name="Bad Time Event",
        fields={"date": "2099-06-27", "time": "totally not a time"},
    )
    rc = gcal_cli.cmd_backfill(
        _make_raw(tmp_path),
        dry_run=True,
        infer_times=True,
        wants_json=True,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["inferred_count"] == 0
    assert payload["skipped_unparseable_time"] == 1
    assert (
        payload["skipped_unparseable_time_records"][0]["raw"]
        == "totally not a time"
    )


def test_backfill_with_infer_times_no_time_string_skipped(tmp_path, capsys):
    """Date-only legacy record (no time field at all) → skipped explicitly."""
    from alfred.integrations import gcal_cli

    _seed_event(
        tmp_path, name="Date Only Legacy",
        fields={"date": "2099-06-27"},  # no time
    )
    rc = gcal_cli.cmd_backfill(
        _make_raw(tmp_path),
        dry_run=True,
        infer_times=True,
        wants_json=True,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped_no_time_string"] == 1
    assert payload["inferred_count"] == 0


def test_backfill_infer_times_respects_from_date(tmp_path, capsys):
    """Inferred records still respect --from-date cutoff."""
    from alfred.integrations import gcal_cli

    _seed_event(
        tmp_path, name="Past Legacy",
        fields={"date": "2020-04-15", "time": "4:00 PM"},
    )
    rc = gcal_cli.cmd_backfill(
        _make_raw(tmp_path),
        dry_run=True,
        infer_times=True,
        from_date="2024-01-01",
        wants_json=True,
    )
    payload = json.loads(capsys.readouterr().out)
    # Inference happened (record had date+time), but the cutoff filtered it.
    assert payload["inferred_count"] == 1
    assert payload["skipped_before_cutoff"] == 1
    assert payload["synced_count"] == 0


def test_alfred_gcal_backfill_infer_times_flag_registered():
    from alfred.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["gcal", "backfill", "--infer-times"])
    assert args.infer_times is True
    args2 = parser.parse_args(["gcal", "backfill"])
    assert args2.infer_times is False
