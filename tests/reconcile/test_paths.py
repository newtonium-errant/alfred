"""Store paths — config-derived, instance-scoped, and single-authored.

The properties under test:

  1. **Instance-scoped, blank refused.** An unscoped path is shared across
     every instance on the box, which is the shape that already bit for
     real. Blank is a hard error at derivation, not a silent default.
  2. **Two entry points, one set of filenames.** ``(data_dir, instance)``
     and an already-resolved root must land on identical paths — the second
     exists because an operator may configure ``store_dir`` explicitly.
  3. **Report names are rejected, not sanitised.** A traversal attempt does
     not get quietly rewritten into something safe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.reconcile.paths import (
    RemittancePathError,
    corrections_path,
    corrections_path_in,
    instance_segment,
    ledger_path,
    ledger_path_in,
    remittance_root,
    report_path,
    reports_dir,
    reports_dir_in,
    validate_report_name,
)

_DATA_DIR = "/tmp/does-not-need-to-exist/data"


def test_root_is_instance_scoped():
    a = remittance_root(_DATA_DIR, "Salem")
    b = remittance_root(_DATA_DIR, "VERA")
    assert a != b
    assert a.name == "salem"
    assert b.name == "vera"
    assert a.parent == b.parent == Path(_DATA_DIR) / "remittance"


def test_instance_name_is_slugged_consistently():
    assert instance_segment("KAL-LE") == "kal-le"
    assert instance_segment("  Dame Bluebird ") == "dame-bluebird"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_instance_is_a_hard_error(blank):
    """The refusal must fire for the RIGHT reason. Asserting only that it
    raised would pass against a build that raised on every input, so the
    message is asserted too — and the positive control below proves a
    non-blank instance is accepted."""
    with pytest.raises(RemittancePathError) as exc:
        remittance_root(_DATA_DIR, blank)
    assert "instance name" in str(exc.value)
    assert remittance_root(_DATA_DIR, "VERA").name == "vera"


def test_both_entry_points_agree_on_every_file():
    """The drift guard. ``(data_dir, instance)`` and a resolved root reach
    the same files, so a config-supplied store_dir and a derived one cannot
    end up pointing at differently-named ledgers."""
    root = remittance_root(_DATA_DIR, "VERA")
    assert ledger_path(_DATA_DIR, "VERA") == ledger_path_in(root)
    assert corrections_path(_DATA_DIR, "VERA") == corrections_path_in(root)
    assert reports_dir(_DATA_DIR, "VERA") == reports_dir_in(root)


def test_file_names_are_what_the_layout_documents():
    root = remittance_root(_DATA_DIR, "VERA")
    assert ledger_path_in(root).name == "ledger.jsonl"
    assert corrections_path_in(root).name == "corrections.jsonl"
    assert reports_dir_in(root).name == "reports"


def test_valid_report_name_is_accepted():
    assert validate_report_name("backlog-review-20260812-120000") == (
        "backlog-review-20260812-120000"
    )
    assert validate_report_name("report.csv") == "report.csv"


@pytest.mark.parametrize(
    "bad",
    ["", "../escape", "a/b", "..", ".hidden", "-leading-dash", "with space"],
)
def test_bad_report_names_are_rejected_not_sanitised(bad):
    """Rejected, not rewritten: a silently sanitised name decouples the file
    on disk from the name the operator was told to open."""
    with pytest.raises(RemittancePathError):
        validate_report_name(bad)


def test_report_path_cannot_escape_the_reports_directory():
    """The traversal pin, with its positive control in the same test: a
    legitimate name DOES resolve inside the reports directory, so this
    cannot pass against a build where report_path is broken for everything.
    """
    good = report_path(_DATA_DIR, "VERA", "backlog-review-1.csv")
    assert good.parent == reports_dir(_DATA_DIR, "VERA")

    with pytest.raises(RemittancePathError):
        report_path(_DATA_DIR, "VERA", "../../etc/passwd")
