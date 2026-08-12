"""The backlog bulk-review report — CSV, summary, and the cross-foot.

The properties under test:

  1. **The cross-foot is real arithmetic against the PROVIDER's figures.**
     Agreement on the clean fixture is asserted, and so is DISAGREEMENT on
     a deliberately-tampered ledger — an agreement-only pin passes just as
     well against a cross-foot that always says "agrees".
  2. **The CSV is a working surface.** One row per claim line, the
     classification on it, and enough provenance to find the row.
  3. **Absent is an empty CSV cell, never 0.00**, or a spreadsheet sums
     the difference.
  4. **Every section states its empty case** rather than rendering blank.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from pathlib import Path

import structlog

from alfred.reconcile.attention import (
    CLASS_DOCUMENTATION_REQUIRED,
    CLASS_REVERSAL,
    CLASS_SHORT_PAY,
    CLASS_UNKNOWN_EOB,
    Correction,
)
from alfred.reconcile.ledger import ClaimLine, LedgerContents, Statement
from alfred.reconcile.parser import parse_note
from alfred.reconcile.report import (
    CSV_COLUMNS,
    build_csv,
    build_report,
    report_stem,
    write_report,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CLEAN_NOTE = FIXTURES / "remittance_note_synthetic.md"


def _clean_contents() -> LedgerContents:
    r = parse_note(CLEAN_NOTE.read_text(encoding="utf-8"), source_note="fixture")
    return LedgerContents(
        statements=r.statements, claim_lines=r.claim_lines, subtotals=r.subtotals
    )


def _rows(csv_text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(csv_text)))


# --- the cross-foot -----------------------------------------------------------


def test_cross_foot_agrees_on_a_consistent_ledger():
    """The fixture's arithmetic is internally consistent by construction, so
    every statement must reconcile. Paired with the tampered case below —
    on its own this passes against a cross-foot that never disagrees."""
    report = build_report(_clean_contents(), generated_at="FIXED")
    assert not report.has_discrepancies
    for totals in report.statement_totals:
        assert totals.payment_total_delta == Decimal("0")
        assert totals.subtotal_mismatches == []
    assert "reconcile against the provider's own totals" in report.summary_text


def test_cross_foot_catches_a_wrong_declared_payment_total():
    """The negative control. A statement whose declared total disagrees with
    our sum must be reported, in figures."""
    contents = _clean_contents()
    contents.statements[0].payment_total = Decimal("999.00")
    report = build_report(contents, generated_at="FIXED")
    assert report.has_discrepancies
    first = report.statement_totals[0]
    assert first.payment_total_delta == Decimal("348.00") - Decimal("999.00")
    assert "Cross-foot findings" in report.summary_text
    assert "declared payment total" in report.summary_text


def test_cross_foot_catches_a_subtotal_that_disagrees():
    contents = _clean_contents()
    aldenshaw = [s for s in contents.subtotals if s.surname == "Aldenshaw"][0]
    aldenshaw.amount_paid = Decimal("111.00")
    report = build_report(contents, generated_at="FIXED")
    assert report.has_discrepancies
    mismatches = [
        m for t in report.statement_totals for m in t.subtotal_mismatches
    ]
    assert len(mismatches) == 1
    assert "Aldenshaw" in mismatches[0]


def test_subtotals_that_agree_are_counted_not_merely_absent():
    """Agreement is a positive signal and is stated. 'No mismatches' could
    equally mean 'no subtotals were checked at all'."""
    report = build_report(_clean_contents(), generated_at="FIXED")
    checked = sum(t.subtotals_checked for t in report.statement_totals)
    assert checked == 4
    assert "subtotal(s) agree" in report.summary_text


def test_statement_totals_sum_only_claim_lines_not_subtotals():
    """Double-counting the provider's own subtotal rows into our sum would
    make the cross-foot compare a number against itself."""
    report = build_report(_clean_contents(), generated_at="FIXED")
    first = report.statement_totals[0]
    assert first.paid == Decimal("348.00")
    assert first.line_count == 3


# --- classification in the report --------------------------------------------


def test_report_counts_the_fixture_classes():
    report = build_report(_clean_contents(), generated_at="FIXED")
    assert report.total_lines == 7
    assert report.class_counts == {
        CLASS_REVERSAL: 1,
        CLASS_SHORT_PAY: 1,
        CLASS_UNKNOWN_EOB: 2,
    }
    assert report.flagged_lines == 2


def test_mapping_a_code_moves_it_out_of_unknown():
    """The positive control for fail-open, at report level."""
    report = build_report(
        _clean_contents(),
        eob_map={"ZZ14": CLASS_DOCUMENTATION_REQUIRED, "ZZ22": CLASS_REVERSAL},
        generated_at="FIXED",
    )
    assert CLASS_UNKNOWN_EOB not in report.class_counts
    assert report.class_counts[CLASS_DOCUMENTATION_REQUIRED] == 1


def test_unknown_eob_lines_are_framed_as_the_loop_starting():
    """The framing has to live in the artifact the operator OPENS.

    A first run against a real statement puts most coded lines in the
    unknown bucket, and a bare count there reads as the classifier failing.
    It is the opposite — nothing is mapped until he maps it — and the
    report is where that has to be said, not a commit message he will never
    read at 2 a.m.
    """
    report = build_report(_clean_contents(), generated_at="FIXED")
    assert report.class_counts[CLASS_UNKNOWN_EOB] == 2
    assert "has not been taught yet" in report.summary_text
    assert "the loop starting, not the classifier failing" in report.summary_text
    assert "invented authority" in report.summary_text
    # It must also tell him what to DO about it, or the framing is just
    # reassurance with no next step.
    assert "alfred reconcile correct" in report.summary_text


def test_the_unknown_eob_framing_is_absent_when_there_is_nothing_to_frame():
    """The positive control for the pin above: a report with every code
    mapped must NOT carry the explanation, or it becomes boilerplate the
    operator learns to skip past — and the pin above would pass against a
    build that printed it unconditionally."""
    report = build_report(
        _clean_contents(),
        eob_map={"ZZ14": CLASS_DOCUMENTATION_REQUIRED, "ZZ22": CLASS_REVERSAL},
        generated_at="FIXED",
    )
    assert CLASS_UNKNOWN_EOB not in report.class_counts
    assert "has not been taught yet" not in report.summary_text


def test_clean_lines_are_counted_not_omitted():
    report = build_report(_clean_contents(), generated_at="FIXED")
    assert "classified clean" in report.summary_text
    assert f"**{report.total_lines - report.flagged_lines}**" in report.summary_text


# --- the CSV ------------------------------------------------------------------


def test_csv_has_one_row_per_claim_line():
    contents = _clean_contents()
    report = build_report(contents, generated_at="FIXED")
    rows = _rows(report.csv_text)
    assert len(rows) == len(contents.claim_lines)
    assert list(rows[0]) == list(CSV_COLUMNS)


def test_csv_carries_the_classification_and_the_reason():
    report = build_report(_clean_contents(), generated_at="FIXED")
    rows = _rows(report.csv_text)
    reversal = [r for r in rows if r["claim_no"] == "90000201"][0]
    assert CLASS_REVERSAL in reversal["attention_classes"]
    assert reversal["attention_reasons"]
    assert reversal["classification_source"] == "derived"


def test_csv_carries_enough_provenance_to_find_the_row():
    report = build_report(_clean_contents(), generated_at="FIXED")
    for row in _rows(report.csv_text):
        assert row["source_note"]
        assert int(row["source_line"]) > 0
        assert row["line_key"]


def test_csv_leaves_an_absent_amount_empty_rather_than_zero():
    """Absent and zero are different facts, and a spreadsheet will happily
    sum the difference. The positive control is in the same row: a PRESENT
    zero renders as 0.00."""
    contents = LedgerContents(
        statements=[Statement(statement_date="2026-01-05")],
        claim_lines=[ClaimLine(
            statement_date="2026-01-05", claim_no="900", dos="2026-01-01",
            deduct=None, amt_excluded=Decimal("0.00"),
            amount_paid=Decimal("1.00"),
        )],
    )
    row = _rows(build_csv(contents.claim_lines, {}))[0]
    assert row["deduct"] == ""
    assert row["amt_excluded"] == "0.00"


def test_csv_survives_a_comment_containing_a_comma_and_a_quote():
    contents = LedgerContents(claim_lines=[ClaimLine(
        statement_date="2026-01-05", claim_no="900",
        comments='one, two "three"',
    )])
    row = _rows(build_csv(contents.claim_lines, {}))[0]
    assert row["comments"] == 'one, two "three"'


def test_csv_shows_a_corrected_line_as_operator_sourced():
    contents = _clean_contents()
    target = contents.claim_lines[0]
    corrections = {target.key: Correction(
        line_key=target.key, classes=[CLASS_DOCUMENTATION_REQUIRED],
        operator="andrew",
    )}
    report = build_report(contents, corrections=corrections, generated_at="FIXED")
    rows = _rows(report.csv_text)
    corrected = [r for r in rows if r["line_key"] == target.key][0]
    assert corrected["classification_source"] == "operator"
    # Positive control: an untouched line is still derived.
    others = [r for r in rows if r["line_key"] != target.key]
    assert all(r["classification_source"] == "derived" for r in others)


# --- empty states -------------------------------------------------------------


def test_empty_ledger_produces_a_report_that_says_it_is_empty():
    """ILB: a report that renders nothing when it finds nothing is
    indistinguishable from a report that failed to run."""
    with structlog.testing.capture_logs() as captured:
        report = build_report(LedgerContents(), generated_at="FIXED")
    assert report.total_lines == 0
    assert "holds no claim lines" in report.summary_text
    assert "alfred reconcile seed" in report.summary_text
    events = [c for c in captured if c.get("event") == "reconcile.report.built"]
    assert len(events) == 1
    assert "ledger is empty" in events[0]["detail"]


def test_no_attention_classes_is_stated_explicitly():
    contents = LedgerContents(
        statements=[Statement(statement_date="2026-01-05",
                              payment_total=Decimal("10.00"))],
        claim_lines=[ClaimLine(
            statement_date="2026-01-05", claim_no="900", dos="2026-01-01",
            total_billed=Decimal("10.00"), amount_paid=Decimal("10.00"),
        )],
    )
    report = build_report(contents, generated_at="FIXED")
    assert report.class_counts == {}
    assert "No line carries an attention class" in report.summary_text
    assert "a real result, not an empty section" in report.summary_text


def test_no_proposals_is_stated_explicitly():
    report = build_report(_clean_contents(), generated_at="FIXED")
    assert report.proposals == []
    assert "The learning loop is running; it has nothing to say yet" in (
        report.summary_text
    )


def test_proposals_appear_when_the_corrections_support_them():
    contents = _clean_contents()
    targets = contents.claim_lines[:2]
    corrections = {
        t.key: Correction(
            line_key=t.key, classes=[CLASS_DOCUMENTATION_REQUIRED],
            operator="andrew", eob_codes=["ZZ14"],
        )
        for t in targets
    }
    report = build_report(contents, corrections=corrections, generated_at="FIXED")
    assert len(report.proposals) == 1
    assert report.proposals[0].code == "ZZ14"
    assert "Nothing is applied automatically" in report.summary_text


def test_summary_states_it_is_propose_only():
    report = build_report(_clean_contents(), generated_at="FIXED")
    assert "Propose-only" in report.summary_text
    assert "has been actioned, closed, or written back" in report.summary_text


# --- writing ------------------------------------------------------------------


def test_write_report_produces_both_halves(tmp_path):
    report = build_report(_clean_contents(), generated_at="FIXED")
    csv_path, summary_path = write_report(report, tmp_path, stem="review-1")
    assert csv_path.is_file() and summary_path.is_file()
    assert csv_path.read_text(encoding="utf-8") == report.csv_text
    assert summary_path.read_text(encoding="utf-8") == report.summary_text


def test_write_report_leaves_no_temp_files(tmp_path):
    """The write is atomic (.tmp -> replace); a leftover .tmp would mean a
    reader could see a half-written report."""
    report = build_report(_clean_contents(), generated_at="FIXED")
    write_report(report, tmp_path, stem="review-1")
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_report_logs_where_it_wrote(tmp_path):
    report = build_report(_clean_contents(), generated_at="FIXED")
    with structlog.testing.capture_logs() as captured:
        write_report(report, tmp_path, stem="review-1")
    events = [c for c in captured if c.get("event") == "reconcile.report.written"]
    assert len(events) == 1
    assert events[0]["rows"] == 7


def test_report_stem_is_a_safe_path_segment():
    from alfred.reconcile.paths import validate_report_name

    stem = report_stem()
    assert stem.startswith("backlog-review-")
    assert validate_report_name(f"{stem}.csv")
