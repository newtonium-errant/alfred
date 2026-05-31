"""Tests for the brief's c6 spam-quarantine surface in operations.py.

The brief calls ``format_operations_section(data_dir, vault_path,
since=today)`` once per daily generation. c6 (2026-05-31) added a
quarantine count line:

  - "Spam quarantine: empty" when the directory is absent OR no
    records exist (per feedback_intentionally_left_blank.md: ILB
    explicit-zero so operator knows the check ran)
  - "Spam quarantine: N this week (M this month)" when populated;
    week = rolling 7-day window, month = current YYYY-MM bucket

Tests cover the three surfaces:
  - Empty / missing quarantine root
  - Populated current-month, current-week
  - Older record (>7 days but same month) — counted in month, NOT week

The brief's broader format_operations_section (state files, audit
log) is exercised elsewhere; here we pin only the new c6 line.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

import pytest

from alfred.brief.operations import _quarantine_summary, format_operations_section


def _seed_quarantine_record(vault: Path, month: str, name: str) -> Path:
    """Create a fake quarantined record at
    ``<vault>/quarantine/spam/<month>/<name>.md``."""
    rec_dir = vault / "quarantine" / "spam" / month
    rec_dir.mkdir(parents=True, exist_ok=True)
    rec = rec_dir / f"{name}.md"
    rec.write_text(
        f"---\ntype: note\nname: {name}\npriority: spam\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return rec


def _backdate(file: Path, days_ago: int) -> None:
    """Set ``file`` mtime to ``days_ago`` days in the past (rolling-
    window tests need to push records past the 7-day cutoff)."""
    target = time.time() - (days_ago * 86400)
    os.utime(file, (target, target))


# ---------------------------------------------------------------------------
# _quarantine_summary — direct unit tests
# ---------------------------------------------------------------------------


def test_quarantine_summary_missing_root_returns_empty(tmp_path: Path) -> None:
    """No quarantine/ directory ever created → "empty" string. ILB:
    explicit absence signal (not a missing line, not "N/A")."""
    # tmp_path has no quarantine/ subtree.
    result = _quarantine_summary(tmp_path)
    assert result == "Spam quarantine: empty"


def test_quarantine_summary_empty_directory_returns_empty(
    tmp_path: Path,
) -> None:
    """quarantine/spam/ exists but no records → still "empty". Pins
    the discriminate case from the missing-root case (both produce
    the same string but via different code paths)."""
    (tmp_path / "quarantine" / "spam").mkdir(parents=True)
    result = _quarantine_summary(tmp_path)
    assert result == "Spam quarantine: empty"


def test_quarantine_summary_shows_current_month_count(tmp_path: Path) -> None:
    """3 records this month → format includes the count. Both 'this
    week' and 'this month' show 3 because the records are fresh
    (no backdate)."""
    now = datetime.now()
    month_bucket = now.strftime("%Y-%m")
    for i in range(3):
        _seed_quarantine_record(tmp_path, month_bucket, f"spam-{i}")

    result = _quarantine_summary(tmp_path)
    # Format: "Spam quarantine: N this week (M this month)"
    assert "Spam quarantine:" in result
    assert "3 this week" in result
    assert "(3 this month)" in result


def test_quarantine_summary_old_records_in_month_not_in_week(
    tmp_path: Path,
) -> None:
    """A record from earlier this month (>7 days ago) counts in the
    month total but NOT the week total — the operator can see a
    monthly cumulative without the weekly-noise distortion."""
    now = datetime.now()
    month_bucket = now.strftime("%Y-%m")
    # Two records: one fresh (counts in week + month), one 10 days
    # old (counts in month only).
    _seed_quarantine_record(tmp_path, month_bucket, "fresh")
    old_rec = _seed_quarantine_record(tmp_path, month_bucket, "old")
    _backdate(old_rec, days_ago=10)

    result = _quarantine_summary(tmp_path)
    # 1 in the week (fresh only), 2 in the month (both).
    assert "1 this week" in result
    assert "(2 this month)" in result


def test_quarantine_summary_respects_custom_dir_name(tmp_path: Path) -> None:
    """The brief reads the quarantine directory name from the
    classifier config — the helper accepts the param so a non-
    default dir name (e.g. operator-customized) still surfaces.

    Pins the contract that the brief's caller threads the
    ``quarantine_dir_name`` through (default ``"quarantine"`` matches
    EmailClassifierConfig.quarantine_dir_name default)."""
    now = datetime.now()
    month_bucket = now.strftime("%Y-%m")
    # Seed under a custom dir name to confirm the helper uses the param.
    custom_root = tmp_path / "isolation" / "spam" / month_bucket
    custom_root.mkdir(parents=True)
    rec = custom_root / "spam-record.md"
    rec.write_text(
        "---\ntype: note\nname: spam-record\n---\n", encoding="utf-8",
    )

    # Default dir name "quarantine" would miss this record.
    default_result = _quarantine_summary(tmp_path)
    assert default_result == "Spam quarantine: empty"
    # Custom dir name finds it.
    custom_result = _quarantine_summary(
        tmp_path, quarantine_dir_name="isolation",
    )
    assert "1 this week" in custom_result


# ---------------------------------------------------------------------------
# format_operations_section — integration: quarantine line present
# ---------------------------------------------------------------------------


def test_format_operations_section_includes_quarantine_line_empty(
    tmp_path: Path,
) -> None:
    """The brief's operations section always emits a quarantine line,
    even on the empty state (per ILB — explicit absence)."""
    # Minimal fixture: no state files, no vault contents.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()

    out = format_operations_section(str(data_dir), str(vault))
    # Quarantine line is part of the output, bolded per format.
    assert "**Spam quarantine: empty**" in out


def test_format_operations_section_includes_quarantine_line_populated(
    tmp_path: Path,
) -> None:
    """When quarantine has records, the line shows the counts.
    Confirms the full pipeline (helper called, output composed,
    string lands in the formatted section)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    now = datetime.now()
    month_bucket = now.strftime("%Y-%m")
    _seed_quarantine_record(vault, month_bucket, "test-spam")

    out = format_operations_section(str(data_dir), str(vault))
    assert "**Spam quarantine: 1 this week (1 this month)**" in out


def test_format_operations_section_threads_non_default_quarantine_dir(
    tmp_path: Path,
) -> None:
    """End-to-end regression pin for the WARN on 164839a code-review:
    the brief's operations section must honor a non-default
    ``quarantine_dir_name``, NOT silently default to ``quarantine``
    when the classifier writes elsewhere.

    Fixture seeds records ONLY at the non-default location
    (``spam_archive/spam/<YYYY-MM>/``). If the brief defaults to
    ``quarantine``, this test fails with "Spam quarantine: empty"
    instead of the count — same failure shape as the production
    silent-misroute the WARN identified."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    now = datetime.now()
    month_bucket = now.strftime("%Y-%m")
    # Seed under the NON-DEFAULT dir name only.
    non_default_root = vault / "spam_archive" / "spam" / month_bucket
    non_default_root.mkdir(parents=True)
    rec = non_default_root / "non-default-spam.md"
    rec.write_text(
        "---\ntype: note\nname: non-default-spam\n---\n",
        encoding="utf-8",
    )

    # Default invocation — would miss the record (silent-misroute
    # shape from the original WARN).
    default_out = format_operations_section(str(data_dir), str(vault))
    assert "**Spam quarantine: empty**" in default_out

    # Threaded invocation — finds the record. Wired path works.
    threaded_out = format_operations_section(
        str(data_dir),
        str(vault),
        quarantine_dir_name="spam_archive",
    )
    assert "**Spam quarantine: 1 this week (1 this month)**" in threaded_out
