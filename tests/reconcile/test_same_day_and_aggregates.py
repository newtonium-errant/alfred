"""The two findings the real-note re-validation surfaced, and their controls.

The re-validation put one statement 45,178.00 out, decomposed exactly:
28,284.00 of full-width aggregate rows absorbed as claim lines, plus
16,894.00 of a second same-day statement folded into the first. The fixture
reproduces both sums, so these tests fail by a STATED amount if either fix
regresses rather than merely failing somehow.

Both findings share one root shape — **a distinction the ledger could not
express**. An aggregate row and a claim line looked identical to the parser;
two statements issued on one day shared a key. In both cases the merge
direction was the one that loses money silently, and in both cases the fix
is to make the distinction representable and then let the loud path do the
talking.

The controls matter as much as the findings here, and each is in the same
file as the thing it controls:

  * aggregate detection must NOT eat a claim row whose id happens to be
    non-numeric — the `(Ambulance Claims)` population, which is fully
    populated and stays a claim;
  * the same-day SPLIT must NOT turn every re-printed continuation header
    into a phantom statement — a compatible same-date block still folds,
    and folding must MERGE rather than append.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import structlog

from alfred.reconcile.ledger import (
    ROW_CLAIM,
    ROW_SUBTOTAL,
    ClaimLine,
    LedgerContents,
    Statement,
    line_key,
    statement_key,
)
from alfred.reconcile.parser import parse_note
from alfred.reconcile.report import build_report

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SAME_DAY = FIXTURES / "remittance_note_same_day_split.md"
REAL_SHAPES = FIXTURES / "remittance_note_real_shapes.md"

#: The figures from the real re-validation, reproduced by the fixture. Named
#: so a failure says WHICH half regressed instead of just "wrong number".
ABSORBED_AGGREGATES = Decimal("28284.00")
FOLDED_SECOND_STATEMENT = Decimal("16894.00")
DECLARED_PAYMENT_TOTAL = Decimal("20862.00")
PRE_FIX_SUM = Decimal("66040.00")
PRE_FIX_DELTA = Decimal("45178.00")


@pytest.fixture(scope="module")
def parsed():
    return parse_note(SAME_DAY.read_text(encoding="utf-8"), source_note="sds")


def _contents(result) -> LedgerContents:
    return LedgerContents(
        statements=result.statements,
        claim_lines=result.claim_lines,
        subtotals=result.subtotals,
    )


def test_the_fixture_reproduces_the_real_arithmetic():
    """The decomposition, asserted as arithmetic rather than trusted as
    prose. If these three figures stop summing, the fixture has drifted from
    the finding it was built to reproduce and every assertion below is
    measuring something else."""
    assert ABSORBED_AGGREGATES + FOLDED_SECOND_STATEMENT + DECLARED_PAYMENT_TOTAL == (
        PRE_FIX_SUM
    )
    assert PRE_FIX_SUM - DECLARED_PAYMENT_TOTAL == PRE_FIX_DELTA


def test_everything_parses_with_no_losses(parsed):
    assert parsed.ok
    assert parsed.skipped == []


# --- finding 1: full-width aggregate rows -------------------------------------


def test_aggregate_rows_are_not_claim_lines(parsed):
    """The absorbed rows. As claim lines they inflated the paid sum by their
    own value — the provider's arithmetic double-counted into ours."""
    assert len(parsed.aggregate_rows) == 4
    claim_ids = {c.claim_no for c in parsed.claim_lines}
    for label in ("Aldenshaw Group", "Brightwater Group", "Corvallis Group",
                  "Payment Summary"):
        assert label not in claim_ids
    assert all(c.row_type == ROW_CLAIM for c in parsed.claim_lines)


def test_the_absorbed_amount_is_exactly_the_reported_figure(parsed):
    """28,284.00 — the figure from the real decomposition, now sitting in
    the subtotal bucket instead of the claim sum."""
    aggregates = sum(
        (s.amount_paid for s in parsed.subtotals if s.amount_paid),
        Decimal("0"),
    )
    assert aggregates == ABSORBED_AGGREGATES


def test_a_non_numeric_id_alone_does_not_make_a_row_an_aggregate():
    """THE control for finding 1, and the one that keeps the rule safe.

    `(Ambulance Claims)` rows also carry no digit in the id. They are FULLY
    POPULATED and must stay claim lines — the rule tests for the absence of
    claim-shaped data, never for the shape of the id.
    """
    real_shapes = parse_note(REAL_SHAPES.read_text(encoding="utf-8"))
    ambulance = [
        c for c in real_shapes.claim_lines
        if "Ambulance" in c.claim_no
    ]
    assert len(ambulance) == 2
    assert all(c.row_type == ROW_CLAIM for c in ambulance)
    assert real_shapes.aggregate_rows == []


@pytest.mark.parametrize(
    "field_kept",
    ["dos", "benefit_code", "units"],
)
def test_a_row_keeping_any_one_claim_field_stays_a_claim(field_kept):
    """Conservative by design: ALL THREE fields must be empty. A row missing
    only its benefit code is a claim line with a gap, and reclassifying it
    would delete a real payment from the claim sum."""
    from alfred.reconcile.parser import _looks_like_aggregate

    row = ClaimLine(claim_no="Some Words", amount_paid=Decimal("10.00"))
    setattr(row, field_kept, "2026-01-01" if field_kept == "dos" else (
        "700409" if field_kept == "benefit_code" else 2
    ))
    assert not _looks_like_aggregate(row)

    bare = ClaimLine(claim_no="Some Words", amount_paid=Decimal("10.00"))
    assert _looks_like_aggregate(bare)


def test_the_aggregate_reclassification_is_logged(parsed):
    """It is an INFERENCE that moves money between the claim sum and the
    cross-foot inputs, so it cannot be silent."""
    with structlog.testing.capture_logs() as captured:
        parse_note(SAME_DAY.read_text(encoding="utf-8"))
    events = [
        c for c in captured
        if c.get("event") == "reconcile.parser.aggregate_row"
    ]
    assert len(events) == 4
    assert all(e["claim_no"] for e in events)


# --- finding 2: two statements on one date -------------------------------------


def test_two_same_day_statements_stay_apart(parsed):
    """The fold that hid 16,894.00. Keyed on the date alone, the second
    block's header overwrote the first's and its lines were attributed to a
    statement that never paid them."""
    april_23 = [s for s in parsed.statements if s.statement_date == "2026-04-23"]
    assert len(april_23) == 2
    assert {s.statement_occurrence for s in april_23} == {0, 1}
    assert {s.payment_total for s in april_23} == {
        DECLARED_PAYMENT_TOTAL, FOLDED_SECOND_STATEMENT
    }
    assert len({s.key for s in april_23}) == 2


def test_the_split_names_its_reason(parsed):
    """A split is a judgement about the provider's intent; it says which
    header fact forced it."""
    assert len(parsed.statement_splits) == 1
    date, occurrence, reason = parsed.statement_splits[0]
    assert date == "2026-04-23"
    assert occurrence == 1
    assert "payment_total differs" in reason


def test_claim_lines_follow_their_own_statement(parsed):
    """Attribution, not just separation. Both blocks' lines existing is no
    use if they are grouped under one header."""
    report = build_report(_contents(parsed), generated_at="F")
    by_occurrence = {
        t.statement.statement_occurrence: t
        for t in report.statement_totals
        if t.statement.statement_date == "2026-04-23"
    }
    assert by_occurrence[0].line_count == 6
    assert by_occurrence[0].paid == DECLARED_PAYMENT_TOTAL
    assert by_occurrence[1].line_count == 2
    assert by_occurrence[1].paid == FOLDED_SECOND_STATEMENT


def test_a_compatible_same_date_block_still_folds(parsed):
    """THE control for finding 2. Splitting unconditionally would turn every
    re-printed continuation header into a phantom statement — trading a
    silent error for a noisy one."""
    assert len(parsed.statement_folds) == 1
    april_30 = [s for s in parsed.statements if s.statement_date == "2026-04-30"]
    assert len(april_30) == 1


def test_a_fold_merges_rather_than_appending(parsed):
    """Two Statement rows sharing one key would leave every key-indexed
    reader holding whichever it saw last — which is how the continuation's
    empty header erased the real block's declared total."""
    april_30 = [s for s in parsed.statements if s.statement_date == "2026-04-30"][0]
    assert april_30.payment_total == Decimal("500.00")
    assert april_30.claim_line_count == 2


def test_statement_key_distinguishes_same_day_blocks():
    assert statement_key("2026-04-23", 0) != statement_key("2026-04-23", 1)
    assert statement_key("2026-04-23", 0) == statement_key("2026-04-23")


def test_the_line_key_carries_the_statement_occurrence():
    """A re-billed claim — same claim_no, dos and benefit code on two
    same-day statements — is exactly the population a duplicate denial
    produces. Without this the two rows collide and the upsert keeps one,
    losing the evidence that it was re-billed."""
    a = line_key("2026-04-23", "900", "2026-04-01", "700409", 0, 0)
    b = line_key("2026-04-23", "900", "2026-04-01", "700409", 0, 1)
    assert a != b


def test_two_same_day_statements_can_hold_the_same_claim():
    contents = LedgerContents(
        statements=[
            Statement(statement_date="2026-04-23", statement_occurrence=0),
            Statement(statement_date="2026-04-23", statement_occurrence=1),
        ],
        claim_lines=[
            ClaimLine(statement_date="2026-04-23", claim_no="900",
                      dos="2026-04-01", benefit_code="700409",
                      statement_occurrence=0, amount_paid=Decimal("10.00")),
            ClaimLine(statement_date="2026-04-23", claim_no="900",
                      dos="2026-04-01", benefit_code="700409",
                      statement_occurrence=1, amount_paid=Decimal("20.00")),
        ],
    )
    assert len({c.key for c in contents.claim_lines}) == 2


# --- both fixes together: the arithmetic closes ---------------------------------


def test_every_statement_reconciles_to_zero(parsed):
    """The end-to-end pin, and the one worth the most: with both fixes, each
    statement's claim lines sum to its own declared total exactly. Either
    fix regressing puts a named figure back on the board."""
    report = build_report(_contents(parsed), generated_at="F")
    for totals in report.statement_totals:
        assert totals.payment_total_delta == Decimal("0"), (
            f"{totals.statement.statement_date}"
            f"#{totals.statement.statement_occurrence}"
        )
    assert not report.has_discrepancies


def test_the_pre_fix_sum_is_what_the_broken_path_would_have_produced(parsed):
    """Ties the fixture to the reported failure. Everything the parser saw
    under 2026-04-23 — both blocks' claim lines AND the aggregates — sums to
    66,040.00, which against the declared 20,862.00 is the 45,178.00 that
    was reported. This is what the old code produced; the test above is what
    the new code produces."""
    april_23_claims = sum(
        (c.amount_paid for c in parsed.claim_lines
         if c.statement_date == "2026-04-23" and c.amount_paid),
        Decimal("0"),
    )
    april_23_aggregates = sum(
        (s.amount_paid for s in parsed.subtotals
         if s.statement_date == "2026-04-23" and s.amount_paid),
        Decimal("0"),
    )
    assert april_23_claims + april_23_aggregates == PRE_FIX_SUM
    assert (april_23_claims + april_23_aggregates) - DECLARED_PAYMENT_TOTAL == (
        PRE_FIX_DELTA
    )


# --- rider (b): the free self-check ---------------------------------------------


def test_a_declared_line_count_that_disagrees_is_a_finding():
    """Free, because both numbers are already stored — and on the real note
    this check alone would have caught the wrong fold without reference to
    any money figure."""
    contents = LedgerContents(
        statements=[Statement(
            statement_date="2026-01-05", claim_line_count=1,
            payment_total=Decimal("20.00"),
        )],
        claim_lines=[
            ClaimLine(statement_date="2026-01-05", claim_no="900",
                      dos="2026-01-01", amount_paid=Decimal("10.00")),
            ClaimLine(statement_date="2026-01-05", claim_no="901",
                      dos="2026-01-02", amount_paid=Decimal("10.00")),
        ],
    )
    report = build_report(contents, generated_at="F")
    totals = report.statement_totals[0]
    assert totals.line_count_mismatch == (1, 2)
    assert report.has_discrepancies
    assert "declares 1 claim line(s), the ledger holds 2" in report.summary_text
    assert "statement_split / statement_fold" in report.summary_text


def test_a_matching_line_count_is_not_a_finding(parsed):
    """The control: the fixture's statements all agree, so this check must
    stay quiet there — or it would fire on every statement and mean nothing.
    """
    report = build_report(_contents(parsed), generated_at="F")
    assert all(t.line_count_mismatch is None for t in report.statement_totals)


# --- the unattributable-aggregate refinement ------------------------------------


def test_unattributed_aggregates_do_not_cry_wolf(parsed):
    """A statement carries several aggregates that name no claimant. Only
    the grand total should equal the statement sum; comparing every one
    against it would report the rest as mismatches, and a cross-foot that
    cries wolf is one the operator stops reading."""
    report = build_report(_contents(parsed), generated_at="F")
    first = [
        t for t in report.statement_totals
        if t.statement.statement_occurrence == 0
        and t.statement.statement_date == "2026-04-23"
    ][0]
    assert first.subtotal_mismatches == []
    assert len(first.unattributed_aggregates) == 3


def test_no_matching_grand_total_IS_a_finding():
    """The other direction, and the reason the refinement is not just noise
    suppression: when NO unattributable aggregate equals our sum, the
    statement's own total does not reproduce our arithmetic and that must
    surface."""
    contents = LedgerContents(
        statements=[Statement(statement_date="2026-01-05")],
        claim_lines=[ClaimLine(
            statement_date="2026-01-05", claim_no="900", dos="2026-01-01",
            amount_paid=Decimal("10.00"),
        )],
        subtotals=[ClaimLine(
            statement_date="2026-01-05", claim_no="Some Words",
            row_type=ROW_SUBTOTAL, amount_paid=Decimal("999.00"),
        )],
    )
    report = build_report(contents, generated_at="F")
    totals = report.statement_totals[0]
    assert totals.subtotal_mismatches
    assert "no statement-level aggregate equals our sum" in (
        totals.subtotal_mismatches[0]
    )
    assert report.has_discrepancies


# --- payment_total=None stays honest --------------------------------------------


def test_a_missing_payment_total_is_not_treated_as_zero():
    """A statement that declares no total has NO delta — not a delta against
    zero, which would report every such statement as off by its own value."""
    contents = LedgerContents(
        statements=[Statement(statement_date="2026-01-05")],
        claim_lines=[ClaimLine(
            statement_date="2026-01-05", claim_no="900", dos="2026-01-01",
            amount_paid=Decimal("10.00"),
        )],
    )
    report = build_report(contents, generated_at="F")
    totals = report.statement_totals[0]
    assert totals.payment_total_delta is None
    assert "no declared total" in report.summary_text
    # The positive control: a statement that DOES declare one gets a delta.
    contents.statements[0].payment_total = Decimal("10.00")
    assert build_report(contents, generated_at="F").statement_totals[0]. \
        payment_total_delta == Decimal("0")
