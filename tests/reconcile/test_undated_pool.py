"""The third stratum: why 175 real claim lines sat in a dateless pool.

After bc1c the cross-foot got sharp enough to see the next problem, and the
next problem was that ~45% of the note had never reached a statement at
all. Three shapes fed it, and they reduce to ONE corrected rule:

    A fold requires POSITIVE EVIDENCE of identity, not merely the absence
    of contradiction.

  * a shared date is evidence — so bc1c's rule was accidentally right for
    dated blocks;
  * a ``Page N of M`` marker is evidence — so a continuation-with-header
    attaches to its parent even carrying no date of its own;
  * two empty headers are not — so undated blocks stand apart instead of
    merging into one pool.

That third case is the bc1c empty-claimant bug one tier up: two blanks
agreeing, called an identity. It is worth stating in the tests as well as
the code, because the shape recurred at a different altitude within one
day and will recur again wherever a comparison treats "no information" as
"the same information".

The fixture also carries the header variant that lost a declared total, the
capture-metadata date recovery with its weaker provenance, and the
reversal rows (0.00 billed, negative paid, %PD 0).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import structlog

from alfred.reconcile.attention import CLASS_REVERSAL, CLASS_SHORT_PAY
from alfred.reconcile.ledger import ClaimLine, LedgerContents, Statement
from alfred.reconcile.parser import (
    DATE_SOURCE_CAPTURE,
    DATE_SOURCE_HEADER,
    parse_note,
)
from alfred.reconcile.report import build_report

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
POOL = FIXTURES / "remittance_note_undated_pool.md"


@pytest.fixture(scope="module")
def parsed():
    return parse_note(POOL.read_text(encoding="utf-8"), source_note="pool")


def _contents(result) -> LedgerContents:
    return LedgerContents(
        statements=result.statements,
        claim_lines=result.claim_lines,
        subtotals=result.subtotals,
    )


def test_everything_parses(parsed):
    assert parsed.ok
    assert parsed.skipped == []
    assert len(parsed.claim_lines) == 7


# --- the one rule: folds need positive evidence --------------------------------


def test_undated_blocks_do_not_fold_into_each_other(parsed):
    """Shape C, and the bc1c bug one tier up. Two empty headers contradict
    nothing; treating that as identity pooled 175 lines on the real note."""
    dates = [s.statement_date for s in parsed.statements]
    assert dates.count("") == 2, (
        "TWO orphan blocks, neither identifying the other, must stay two. "
        "One orphan cannot test this rule — the assertion would hold "
        "whether it worked or not."
    )
    assert len(parsed.statements) == 4


def test_a_page_continuation_folds_onto_its_parent(parsed):
    """Shape B. ``Page 2 of 2`` is the provider saying in the document that
    this continues something — strong enough to fold a block carrying no
    date of its own, which is exactly the case that was pooling."""
    assert len(parsed.statement_folds) == 1
    june_5 = [s for s in parsed.statements if s.statement_date == "2026-06-05"]
    assert len(june_5) == 1
    assert june_5[0].claim_line_count == 4  # 3 from page 1, 1 from page 2


def test_the_continuations_rows_carry_the_parents_date(parsed):
    """Not just the statement — the ROWS. They are built before the fold
    decides the date, so without stamping them the fold is recorded and has
    no effect: the rows keep an empty date and pool anyway."""
    page_two_row = [c for c in parsed.claim_lines if c.claim_no == "00000704"]
    assert len(page_two_row) == 1
    assert page_two_row[0].statement_date == "2026-06-05"


def test_the_fold_log_names_its_evidence():
    with structlog.testing.capture_logs() as captured:
        parse_note(POOL.read_text(encoding="utf-8"))
    folds = [
        c for c in captured
        if c.get("event") == "reconcile.parser.statement_fold"
    ]
    assert len(folds) == 1
    assert folds[0]["evidence"] == "page_continuation"


def test_a_block_with_no_evidence_at_all_stands_alone(parsed):
    """THE control for the rule. The orphan has no date, no page marker and
    no batch label in range — so nothing identifies it, and it must NOT be
    quietly attached to a neighbour to make the report look clean."""
    orphans = [s for s in parsed.statements if not s.statement_date]
    assert len(orphans) == 2
    assert all(o.claim_line_count == 1 for o in orphans)


# --- shape A: the header variant ------------------------------------------------


def test_an_unknown_header_label_with_money_is_captured_not_dropped(parsed):
    """The half that silently lost a declared total: a bolded multi-word
    label matching no synonym, so the line parsed as prose."""
    assert parsed.unmapped_header_labels == ["Remittance Advice Total"]
    june_5 = [s for s in parsed.statements if s.statement_date == "2026-06-05"][0]
    assert june_5.declared_totals == {"Remittance Advice Total": "1200.00"}


def test_the_captured_label_is_NOT_assigned_to_payment_total(parsed):
    """Captured, not interpreted — the same posture as the two-column
    block. Which labelled figure is *the* payment total is not ours to
    decide from a label we do not recognise."""
    june_5 = [s for s in parsed.statements if s.statement_date == "2026-06-05"][0]
    assert june_5.payment_total is None


def test_header_facts_printed_above_the_heading_reach_the_statement(parsed):
    """The variant prints its total BEFORE the `##` that opens the
    statement. Flushed as its own block it would be a dateless rowless
    phantom; dropped, the total is lost — which is what happened."""
    june_5 = [s for s in parsed.statements if s.statement_date == "2026-06-05"][0]
    assert june_5.declared_totals
    assert june_5.claim_line_count == 4
    # Positive control: no phantom statement was minted for those facts.
    assert len(parsed.statements) == 4


def test_a_money_line_after_the_rows_is_not_captured():
    """The window's reason for existing. Prose following the table must not
    be able to inject a phantom total."""
    note = (
        "## Statement — 2026-01-05\n\n"
        "**Statement Date:** 2026-01-05\n\n"
        "| Claim # | Date of Service | Amount Paid |\n"
        "| --- | --- | --- |\n"
        "| 900 | 2026-01-01 | 50.00 |\n\n"
        "**Some Trailing Note:** $9,999.00\n"
    )
    result = parse_note(note)
    assert result.statements[0].declared_totals == {}
    assert result.unmapped_header_labels == []


# --- capture-metadata dates, and their weaker provenance ------------------------


def test_a_date_recovered_from_capture_metadata_is_marked_as_such(parsed):
    """A batch label is a CAPTURE claim, not a provider one — it can be
    wrong in ways a printed date cannot, and a mistyped label re-homes
    every claim line under it. Kept visibly weaker rather than laundered
    into the same field with the same authority."""
    june_5 = [s for s in parsed.statements if s.statement_date == "2026-06-05"][0]
    assert june_5.date_source == DATE_SOURCE_CAPTURE
    # Positive control: a printed date is marked as the stronger claim.
    june_20 = [s for s in parsed.statements if s.statement_date == "2026-06-20"][0]
    assert june_20.date_source == DATE_SOURCE_HEADER


def test_the_capture_date_recovery_is_logged(parsed):
    with structlog.testing.capture_logs() as captured:
        parse_note(POOL.read_text(encoding="utf-8"))
    events = [
        c for c in captured
        if c.get("event") == "reconcile.parser.date_from_capture_metadata"
    ]
    assert len(events) == 1
    assert events[0]["statement_date"] == "2026-06-05"


def test_a_batch_label_does_not_re_date_every_later_block(parsed):
    """Consumed on use. A label describes the block it precedes; left
    sticky it re-homes unrelated blocks downstream — silently attributing
    claim lines to a statement they never belonged to, which is the exact
    failure this recovery exists to prevent."""
    assert len(parsed.capture_dated) == 1
    orphans = [s for s in parsed.statements if not s.statement_date]
    assert len(orphans) == 2, (
        "the orphan blocks sit after the batch label and must NOT inherit it"
    )


# --- the undated-pool finding class ----------------------------------------------


def test_an_undated_statement_holding_rows_is_its_own_finding(parsed):
    """The check that would have caught this stratum without the cross-foot
    needing to be sharp enough first. A pool is not a cross-foot
    discrepancy — it is rows that never reached a statement to be checked,
    and the cross-foot cannot see what was never filed."""
    report = build_report(_contents(parsed), generated_at="F")
    pool = [t for t in report.statement_totals if not t.statement.statement_date]
    assert len(pool) == 2
    assert all(t.undated_lines == 1 for t in pool)
    assert report.has_discrepancies
    assert "not attributed to any dated statement" in report.summary_text


def test_dated_statements_do_not_raise_the_pool_finding(parsed):
    """The control: the finding must be specific to undated blocks, or it
    fires on every statement and means nothing."""
    report = build_report(_contents(parsed), generated_at="F")
    dated = [t for t in report.statement_totals if t.statement.statement_date]
    assert dated
    assert all(t.undated_lines == 0 for t in dated)


def test_an_undated_statement_with_no_rows_is_not_a_finding():
    """Only rows make a pool. An empty dateless statement is a curiosity,
    not lost money, and flagging it would train the operator to skim past
    the finding that matters."""
    contents = LedgerContents(statements=[Statement(statement_date="")])
    report = build_report(contents, generated_at="F")
    assert report.statement_totals[0].undated_lines == 0


# --- the reversal rows ------------------------------------------------------------


def test_zero_billed_negative_paid_rows_classify_as_reversals(parsed):
    """The shape carrying 12 of the real note's reversals: 0.00 billed,
    negative Amt Paid, %PD 0."""
    report = build_report(_contents(parsed), generated_at="F")
    assert report.class_counts.get(CLASS_REVERSAL) == 2


def test_a_zero_billed_reversal_is_not_also_a_short_pay(parsed):
    """Two suppressions agree here and it is worth pinning that they do:
    reversal suppresses short-pay explicitly, AND the short-pay branch
    requires billed > 0 so it never arms on these rows anyway. If either
    changes alone the count stays right, which is why this asserts the
    outcome rather than the mechanism."""
    report = build_report(_contents(parsed), generated_at="F")
    reversals = [
        c for c in parsed.claim_lines
        if c.amount_paid is not None and c.amount_paid < 0
    ]
    assert len(reversals) == 2
    assert all(c.total_billed == Decimal("0.00") for c in reversals)
    assert report.class_counts.get(CLASS_SHORT_PAY) is None


def test_zero_units_parses_rather_than_refusing(parsed):
    """`Units = 0` on a reversal row is a real value, not an absent one."""
    reversals = [c for c in parsed.claim_lines if c.claim_no == "00000702"]
    assert len(reversals) == 1
    assert reversals[0].units == 0


# --- the label that graduated from unknown to known -------------------------------


def test_statement_total_paid_is_a_payment_total_synonym():
    """Added on EVIDENCE. The real note prints this label bolded in a
    header AND repeats the same figure twice in that statement's
    two-column totals block under two other labels — three spellings of one
    number. A plausible-looking label alone would not have earned this; the
    unknown-label capture is what let it wait in declared_totals until the
    corroboration arrived."""
    note = (
        "## Provider Payment\n\n"
        "**Statement Date:** 2026-06-05\n"
        "**Statement Total Paid:** $54,549.00\n\n"
        "| Claim # | Date of Service | Amount Paid |\n"
        "| --- | --- | --- |\n"
        "| 900 | 2026-06-01 | 54549.00 |\n"
    )
    result = parse_note(note)
    stmt = result.statements[0]
    assert stmt.payment_total == Decimal("54549.00")
    # It is a KNOWN field now, so it must NOT also land in declared_totals.
    assert stmt.declared_totals == {}
    assert result.unmapped_header_labels == []


def test_an_unknown_money_label_still_falls_to_declared_totals():
    """The posture stands. One label graduating on evidence does not mean
    the next plausible one is assumed — it means the capture path is what
    keeps a figure alive until evidence exists."""
    note = (
        "## Provider Payment\n\n"
        "**Statement Date:** 2026-06-05\n"
        "**Some Other Total Label:** $1,234.00\n\n"
        "| Claim # | Date of Service | Amount Paid |\n"
        "| --- | --- | --- |\n"
        "| 900 | 2026-06-01 | 1234.00 |\n"
    )
    result = parse_note(note)
    stmt = result.statements[0]
    assert stmt.payment_total is None
    assert stmt.declared_totals == {"Some Other Total Label": "1234.00"}
    assert result.unmapped_header_labels == ["Some Other Total Label"]
