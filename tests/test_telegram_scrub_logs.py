"""``alfred telegram scrub-logs`` — token-scrub pins (T4 C6).

Retirement hygiene: BotFather tokens are revoked (2026-08-18), but revoked
tokens sitting in historical log files are still scanner-bait and copy-paste
hazards. The command rewrites them to a fixed placeholder.

Fixture-token note (per builder.md "Test fixtures for secret-shaped values"):
these tests genuinely assert on the Telegram token WIRE SHAPE
(``digits:secret``), which is the documented exception to the
obviously-fake-prefix rule — so the planted values are shape-real but
CONSTRUCTED AT RUNTIME from parts ("1234567" + "89:" + "A"*35), never a
contiguous token-shaped literal in this file, and the secret half is a
run of a single repeated character no scanner scores as entropy.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from alfred.telegram.scrub_logs import (
    PLACEHOLDER,
    TOKEN_PATTERN,
    scrub_file,
    scrub_logs,
)

# Shape-real, entropy-free, assembled at runtime (see module docstring).
FAKE_TOKEN = "1234567" + "89:" + "A" * 35


def _write_log(path: Path, *lines: str) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# The pattern
# ---------------------------------------------------------------------------


def test_pattern_matches_the_wire_shape() -> None:
    """POSITIVE control for every exclusion below: the pattern DOES match a
    token-shaped value, bare and URL-embedded."""
    assert TOKEN_PATTERN.search(FAKE_TOKEN)
    assert TOKEN_PATTERN.search(
        f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
    )


def test_pattern_leaves_ordinary_log_content_alone() -> None:
    """NEGATIVE control: timestamps, dict reprs, ratios — colon-bearing text
    that is NOT a token must survive (paired with the positive above so a
    dead pattern can't green this)."""
    for line in (
        "2026-08-19 12:34:56 talker.daemon.web_only_serving",
        "counts={'a': 1, 'b': 2} ratio=3:4",
        "chat_id=123456789 text_len=42",
    ):
        assert not TOKEN_PATTERN.search(line), line


# ---------------------------------------------------------------------------
# The scrub
# ---------------------------------------------------------------------------


def test_scrub_rewrites_tokens_and_preserves_neighbours(tmp_path: Path) -> None:
    log_file = tmp_path / "talker.log"
    _write_log(
        log_file,
        "before line stays",
        f"dialing https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage",
        "after line stays",
    )
    result = scrub_file(log_file)
    assert result.matches == 1 and result.rewritten is True
    text = log_file.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in text
    assert PLACEHOLDER in text
    assert "before line stays" in text and "after line stays" in text


def test_scrub_is_idempotent(tmp_path: Path) -> None:
    log_file = tmp_path / "talker.log"
    _write_log(log_file, f"token={FAKE_TOKEN}")
    assert scrub_file(log_file).matches == 1
    second = scrub_file(log_file)
    assert second.matches == 0 and second.rewritten is False


def test_dry_run_reports_but_writes_nothing(tmp_path: Path) -> None:
    log_file = tmp_path / "talker.log"
    _write_log(log_file, f"token={FAKE_TOKEN}")
    before = log_file.read_text(encoding="utf-8")
    result = scrub_file(log_file, dry_run=True)
    assert result.matches == 1 and result.rewritten is False
    assert log_file.read_text(encoding="utf-8") == before


def test_extra_token_literals_are_scrubbed(tmp_path: Path) -> None:
    """--token literals cover shapes the generic pattern refuses (e.g. an
    operator's odd historical format)."""
    log_file = tmp_path / "talker.log"
    _write_log(log_file, "weird historical form: TOKENXYZ")
    result = scrub_file(log_file, extra_tokens=("TOKENXYZ",))
    assert result.matches == 1
    assert "TOKENXYZ" not in log_file.read_text(encoding="utf-8")


def test_clean_file_is_left_untouched_no_mtime_churn(tmp_path: Path) -> None:
    log_file = tmp_path / "talker.log"
    _write_log(log_file, "nothing token-shaped here")
    stat_before = log_file.stat().st_mtime_ns
    result = scrub_file(log_file)
    assert result.matches == 0 and result.rewritten is False
    assert log_file.stat().st_mtime_ns == stat_before


# ---------------------------------------------------------------------------
# The directory walk
# ---------------------------------------------------------------------------


def test_scan_covers_rotated_files_and_reports_gz_skips(tmp_path: Path) -> None:
    _write_log(tmp_path / "talker.log", f"live {FAKE_TOKEN}")
    _write_log(tmp_path / "talker.log.1", f"rotated {FAKE_TOKEN}")
    _write_log(tmp_path / "mail.log.2026-08-01", "clean rotation")
    (tmp_path / "old.log.gz").write_bytes(b"\x1f\x8b compressed junk")
    (tmp_path / "not_a_log.txt").write_text(f"ignored {FAKE_TOKEN}\n")

    report = scrub_logs(tmp_path)
    by_name = {Path(f.path).name: f for f in report.files}
    assert by_name["talker.log"].matches == 1
    assert by_name["talker.log.1"].matches == 1
    assert by_name["mail.log.2026-08-01"].matches == 0
    # The gz rotation is REPORTED, not silently invisible.
    assert "compressed" in by_name["old.log.gz"].skipped_reason
    # Non-log files are out of scope for the walk...
    assert "not_a_log.txt" not in by_name
    assert report.total_matches == 2


def test_explicit_extra_paths_reach_beyond_the_log_dir(tmp_path: Path) -> None:
    stray = tmp_path / "elsewhere" / "console_dump.txt"
    stray.parent.mkdir()
    stray.write_text(f"pasted {FAKE_TOKEN}\n", encoding="utf-8")
    report = scrub_logs(tmp_path / "no_such_dir", extra_paths=(stray,))
    assert report.total_matches == 1
    assert FAKE_TOKEN not in stray.read_text(encoding="utf-8")


def test_clean_scan_says_so_out_loud(tmp_path: Path) -> None:
    """Intentionally-left-blank: a scan that finds nothing must emit the
    explicit nothing-to-scrub event (idle distinguishable from broken)."""
    _write_log(tmp_path / "talker.log", "clean")
    with structlog.testing.capture_logs() as captured:
        report = scrub_logs(tmp_path)
    assert report.total_matches == 0
    events = [
        c for c in captured
        if c.get("event") == "telegram.scrub_logs.nothing_to_scrub"
    ]
    assert len(events) == 1
    assert events[0]["scanned"] == 1
    # ...and the per-file line fired too (scanned=1 is backed by a row).
    file_events = [
        c for c in captured if c.get("event") == "telegram.scrub_logs.file"
    ]
    assert len(file_events) == 1


# ---------------------------------------------------------------------------
# CLI dispatch (T5 ledger item A) — through ``alfred.cli.main``
#
# The 10 pins above are module-level; none of them would notice a broken
# ``alfred telegram scrub-logs`` wire (parser not registered, handler not
# routed, instance_data_dir not resolved, an argparse flag renamed without
# its plumbing). This drives the REAL entry point exactly as a shell does —
# the e2e-through-a-production-entry-point class that per-layer unit pins
# structurally cannot give.
# ---------------------------------------------------------------------------


def _run_cli(argv: list[str]) -> int:
    """Install argv and call ``alfred.cli.main`` (it takes no arguments).

    Mirrors ``tests/reconcile/test_cli.py::_run`` — the established idiom
    for dispatch pins in this repo.
    """
    import sys

    from alfred.cli import main

    original = sys.argv
    sys.argv = ["alfred", *argv]
    try:
        main()
    except SystemExit as exc:
        return int(exc.code or 0)
    finally:
        sys.argv = original
    return 0


def test_cli_dispatch_scrub_logs_end_to_end(tmp_path: Path, capsys) -> None:
    """argparse wiring + instance_data_dir resolution + arg plumbing.

    Phase 1 (--dry-run): the handler runs, names the config-resolved data
    dir and both files in its report, and writes NOTHING (dry-run plumbed).
    Phase 2 (wet): the token in the log-dir file and the --token literal in
    the --path file are both rewritten — positive proof the config's
    ``logging.dir`` reached ``scrub_logs`` as the scan root and that
    ``--path`` / ``--token`` reached their keyword arguments.
    """
    import yaml

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    log_file = data_dir / "talker.log"
    _write_log(log_file, f"token={FAKE_TOKEN}", "neighbour line stays")

    # Outside the log-dir walk: reachable only via --path; its content is
    # NOT token-shaped, so it is only scrubbed if --token plumbed through.
    extra = tmp_path / "elsewhere" / "notes.txt"
    extra.parent.mkdir()
    extra.write_text("pasted TOKENXYZ once\n", encoding="utf-8")

    cfg = tmp_path / "config.test.yaml"
    cfg.write_text(
        yaml.safe_dump({
            "logging": {"dir": str(data_dir)},
            "telegram": {"instance": {"name": "Testbed"}},
        }),
        encoding="utf-8",
    )

    argv = [
        "--config", str(cfg),
        "telegram", "scrub-logs",
        "--path", str(extra),
        "--token", "TOKENXYZ",
    ]

    # --- Phase 1: dry run ---------------------------------------------
    code = _run_cli([*argv[:4], "--dry-run", *argv[4:]])
    assert code == 0
    out = capsys.readouterr().out
    # instance_data_dir resolution surfaced in the report header.
    assert str(data_dir) in out
    assert "DRY RUN" in out
    # Both files reached the report (log-dir walk + --path plumbing).
    assert str(log_file) in out
    assert str(extra) in out
    # ...and nothing was written.
    assert FAKE_TOKEN in log_file.read_text(encoding="utf-8")
    assert "TOKENXYZ" in extra.read_text(encoding="utf-8")

    # --- Phase 2: wet run ---------------------------------------------
    code = _run_cli(argv)
    assert code == 0
    out = capsys.readouterr().out
    assert "rewritten in place" in out
    scrubbed_log = log_file.read_text(encoding="utf-8")
    assert FAKE_TOKEN not in scrubbed_log
    assert PLACEHOLDER in scrubbed_log
    assert "neighbour line stays" in scrubbed_log
    scrubbed_extra = extra.read_text(encoding="utf-8")
    assert "TOKENXYZ" not in scrubbed_extra
    assert PLACEHOLDER in scrubbed_extra
