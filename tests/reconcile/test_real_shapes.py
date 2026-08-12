"""The three shapes a real provider statement has that the synthetic set lacked.

A read-only dry run of the seeder against a genuine payment summary skipped
146 rows. The fail-loud promise held — every loss was named — but the losses
traced to three structural shapes, and none of them existed in the fixture
population. That is the whole lesson of this file: 277 green tests said
nothing about any of the three, because a fixture set that omits a shape
cannot fail on it however green it is.

Each shape gets its own block below, and each block asserts the thing that
was actually LOST, not merely that parsing did not raise:

  1. **Bolded aggregates** — the amount AND the label. Un-bolding the amount
     alone fixes half the shape and leaves claimant matching broken, so the
     cross-foot is asserted end-to-end rather than at the cell level.
  2. **The headerless continuation table** — real claim lines vanished here.
     The pin asserts the rows are IN the ledger, and a positive control
     proves the same parser still rejects a table that genuinely cannot be
     read.
  3. **The two-column totals block** — captured, and deliberately NOT
     assigned to ``payment_total``. Both halves are pinned, because the
     capture is worthless if the interpretation quietly happens anyway.

Fixture content is wholly invented (see the file's own header).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import structlog

from alfred.reconcile.attention import CLASS_REVERSAL
from alfred.reconcile.ledger import LedgerContents
from alfred.reconcile.money import strip_emphasis
from alfred.reconcile.parser import parse_note
from alfred.reconcile.render import render_note
from alfred.reconcile.report import build_report

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REAL_SHAPES = FIXTURES / "remittance_note_real_shapes.md"


@pytest.fixture(scope="module")
def parsed():
    return parse_note(REAL_SHAPES.read_text(encoding="utf-8"),
                      source_note="real-shapes")


def _contents(result) -> LedgerContents:
    return LedgerContents(
        statements=result.statements,
        claim_lines=result.claim_lines,
        subtotals=result.subtotals,
    )


def test_the_whole_fixture_parses_without_losses(parsed):
    assert parsed.ok
    assert parsed.skipped == []
    assert len(parsed.claim_lines) == 10
    assert len(parsed.subtotals) == 6
    assert len(parsed.statements) == 3


# --- shape 1: bolded aggregates ----------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("**1150.00**", Decimal("1150.00")),
        ("**$40,641.00**", Decimal("40641.00")),
        ("**-52440.00**", Decimal("-52440.00")),
        ("__4104.00__", Decimal("4104.00")),
    ],
)
def test_bolded_amounts_parse(raw, expected):
    from alfred.reconcile.money import parse_money

    assert parse_money(raw) == expected


def test_a_single_asterisk_is_left_alone():
    """Only the DOUBLE markers are emphasis. A lone ``*`` or ``_`` is legal
    inside a claim number or a comment, and stripping it to tidy a style
    marker would corrupt an identifier."""
    assert strip_emphasis("A_1*2") == "A_1*2"
    assert strip_emphasis("**bold**") == "bold"
    assert strip_emphasis("__bold__") == "bold"


def test_bolded_subtotal_amounts_reach_the_ledger(parsed):
    """The first build refused these outright, which zeroed every subtotal
    and left the cross-foot with nothing to check."""
    paid = {s.surname: s.amount_paid for s in parsed.subtotals}
    assert paid["Aldenshaw"] == Decimal("1207.50")
    assert paid["Brightwater"] == Decimal("1150.00")
    assert paid["Dunmoor"] == Decimal("-52440.00")


def test_a_bolded_negative_subtotal_stays_negative(parsed):
    """The highest-consequence cell in the file: a clawback read as a
    positive payment reports money as received that was taken back."""
    dunmoor = [s for s in parsed.subtotals if s.surname == "Dunmoor"][0]
    assert dunmoor.amount_paid == Decimal("-52440.00")
    assert dunmoor.amount_paid < 0


def test_bolded_labels_are_unbolded_too(parsed):
    """The half of this shape that is easy to miss. If the LABEL keeps its
    asterisks, ``**Aldenshaw**`` never matches ``Aldenshaw, Marisol`` and the
    report's per-claimant cross-foot silently degrades to a statement-level
    fallback — fixed amount, broken check."""
    assert {s.surname for s in parsed.subtotals} == {
        "Aldenshaw", "Brightwater", "Corvallis", "Dunmoor", "Everly", "Falkirk",
    }
    assert all("*" not in (s.surname or "") for s in parsed.subtotals)


def test_the_cross_foot_actually_reconciles_with_bolded_figures(parsed):
    """End-to-end, because that is where the two halves of shape 1 meet.
    Three subtotals CHECKED and zero mismatched — the number that was
    structurally unreachable before, when every subtotal parsed as absent."""
    report = build_report(_contents(parsed), generated_at="FIXED")
    first = report.statement_totals[0]
    assert first.subtotals_checked == 3
    assert first.subtotal_mismatches == []
    assert first.paid == Decimal("2932.50")


# --- shape 2: the headerless continuation table --------------------------------


def test_the_continuation_tables_rows_are_in_the_ledger(parsed):
    """The shape that lost real claim lines. Read as a header, the first row
    maps nothing and the whole table was skipped."""
    claims = {c.claim_no for c in parsed.claim_lines}
    assert "00000197" in claims
    assert "00000198" in claims


def test_the_continuation_row_keeps_its_values(parsed):
    """Not just present — correct. A row admitted under an inherited mapping
    with the columns misaligned would be worse than a skipped one, because
    it would be wrong silently."""
    row = [c for c in parsed.claim_lines if c.claim_no == "00000197"][0]
    assert row.surname == "Brightwater"
    assert row.first_name == "Tomas"
    assert row.dos == "2026-02-24"
    assert row.units == 2
    assert row.total_billed == Decimal("1150.00")
    assert row.amount_paid == Decimal("1150.00")
    assert row.invoice_no == "502"


def test_the_continuation_is_reported_not_silent(parsed):
    """An inherited mapping is an INFERENCE, and an inference the operator
    cannot see is one nobody can check."""
    assert len(parsed.continuations) == 1
    assert parsed.continuations[0] > 0


def test_the_continuation_logs_which_side_of_the_statement_line_it_fell_on():
    with structlog.testing.capture_logs() as captured:
        parse_note(REAL_SHAPES.read_text(encoding="utf-8"))
    events = [
        c for c in captured
        if c.get("event") == "reconcile.parser.continuation_table"
    ]
    assert len(events) == 1
    assert events[0]["columns"] == 14
    assert "same_statement" in events[0]


def test_inheritance_requires_an_exact_column_count_match():
    """The gate that stops a differently-shaped table inheriting by accident.
    Paired with its positive control: the SAME note's matching-width
    continuation IS admitted, so this cannot pass against a build where
    inheritance never fires at all."""
    note = (
        "## Statement — 2026-01-05\n\n"
        "| Claim # | Date of Service | Amount Paid |\n"
        "| --- | --- | --- |\n"
        "| 900 | 2026-01-01 | 50.00 |\n"
        "\n"
        "| 901 | 2026-01-02 | 60.00 |\n"          # 3 cells — inherits
        "\n"
        "| 902 | 2026-01-03 | 70.00 | extra |\n"  # 4 cells — must NOT inherit
    )
    result = parse_note(note)
    claims = {c.claim_no for c in result.claim_lines}
    assert "901" in claims          # positive control: inheritance works
    assert "902" not in claims      # the gate held
    assert any("missing required column" in s.reason for s in result.skipped)


def test_a_genuinely_unreadable_table_is_still_rejected():
    """The other positive control for shape 2. Widening the parser to admit
    continuations must not turn it into one that admits anything."""
    note = (
        "## Statement — 2026-01-05\n\n"
        "| Claim # | Surname |\n"
        "| --- | --- |\n"
        "| 900 | Aldenshaw |\n"
    )
    result = parse_note(note)
    assert result.claim_lines == []
    assert any("missing required column" in s.reason for s in result.skipped)


# --- shape 3: the two-column statement-totals block ---------------------------


def test_the_totals_block_is_captured(parsed):
    first = parsed.statements[0]
    assert first.declared_totals == {
        "Carrier Statement Amount": "40641.00",
        "Clearinghouse Payment Amount": "2932.50",
    }


def test_the_totals_block_is_NOT_assigned_to_payment_total(parsed):
    """The half that matters. Capturing the figures is worthless if the
    interpretation happens anyway — choosing which labelled figure is *the*
    payment total is a semantic call the statement does not support."""
    assert parsed.statements[0].payment_total is None


def test_declared_totals_are_readable_as_decimals(parsed):
    values = parsed.statements[0].declared_totals_decimal()
    assert values["Clearinghouse Payment Amount"] == Decimal("2932.50")
    assert values["Carrier Statement Amount"] == Decimal("40641.00")


def test_the_report_says_which_declared_figure_our_sum_reproduces(parsed):
    """Reported, not acted on — and the fixture is built so exactly one of
    the two matches, which is what makes this assertion mean something. A
    fixture where every figure agreed could not tell a working comparison
    from a broken one."""
    report = build_report(_contents(parsed), generated_at="FIXED")
    first = report.statement_totals[0]
    assert first.declared_matching_paid == ["Clearinghouse Payment Amount"]
    assert "Declared statement totals" in report.summary_text
    assert "reported, not interpreted" in report.summary_text
    assert "matches our sum" in report.summary_text


def test_the_totals_block_does_not_swallow_a_two_column_claim_table():
    """The narrowness gate. A two-column table that DOES map the required
    columns is a claim table and must be parsed as one — the header check
    runs before the totals-block check for exactly this reason."""
    note = (
        "## Statement — 2026-01-05\n\n"
        "| Date of Service | Amount Paid |\n"
        "| --- | --- |\n"
        "| 2026-01-01 | 50.00 |\n"
    )
    result = parse_note(note)
    assert len(result.claim_lines) == 1
    assert result.claim_lines[0].amount_paid == Decimal("50.00")
    assert result.statements[0].declared_totals == {}


def test_a_totals_row_with_no_amount_is_skipped_not_stored_empty():
    note = (
        "## Statement — 2026-01-05\n\n"
        "|  | Amount |\n"
        "| --- | --- |\n"
        "| Carrier Statement Amount | 100.00 |\n"
        "| Orphan Label | — |\n"
    )
    result = parse_note(note)
    # Positive control in the same test: the well-formed row DID land.
    assert result.statements[0].declared_totals == {
        "Carrier Statement Amount": "100.00"
    }
    assert any("no label or no amount" in s.reason for s in result.skipped)


# --- the shapes together: regenerability still holds ---------------------------


def test_the_real_shapes_round_trip(parsed):
    """All three shapes at once, through render and back. The totals block is
    re-emitted as a TABLE rather than as metadata lines precisely so it
    survives this — a metadata key the synonym table does not know would be
    dropped on the next parse, and the note would shed the figures one
    regeneration at a time."""
    rendered = render_note(_contents(parsed))
    reparsed = parse_note(rendered)
    assert reparsed.ok
    assert [c.key for c in reparsed.claim_lines] == [
        c.key for c in parsed.claim_lines
    ]
    assert [s.amount_paid for s in reparsed.subtotals] == [
        s.amount_paid for s in parsed.subtotals
    ]
    assert reparsed.statements[0].declared_totals == (
        parsed.statements[0].declared_totals
    )


def test_the_render_is_still_a_fixed_point_with_the_new_shapes(parsed):
    first = render_note(_contents(parsed))
    second = render_note(_contents(parse_note(first)))
    assert first == second


def test_the_reversal_still_classifies(parsed):
    """The bolded negative had to survive parsing AND classification —
    parsing it correctly and then failing to flag it would be the same
    outcome for the operator."""
    report = build_report(_contents(parsed), generated_at="FIXED")
    assert report.class_counts.get(CLASS_REVERSAL) == 1


# --- the marker-boundary continuation: two mechanisms, one outcome ------------
#
# The real continuation resumes immediately after an END_INFERRED /
# BEGIN_INFERRED pair. Whether a blank line surrounds that pair decides WHICH
# mechanism carries the rows, and the two are invisible from the outside:
#
#   no blank line -> the table never closes; rows arrive as ordinary data rows
#   blank line    -> the table closes; the continuation branch inherits
#
# Both are pinned. "It parses" and "it parses for the reason I think" are
# different claims, and only the second survives someone refactoring the
# marker handling.

_HDR = (
    "| Claim # | Date of Service | Surname | First Name | Benefit Code | "
    "Units | Total Billed | Amt Excluded | Deduct | Amt Eligible | % PD | "
    "Amount Paid | EOB | Comments |"
)
_SEP = "| --- " * 14 + "|"
_ROW_A = (
    "| 00000101 | 23 Feb 2026 | Aldenshaw | Marisol | 700409 | 2 | 100.00 | "
    "0.00 | 0.00 | 100.00 | 100 | 100.00 | — | Invoice #501 |"
)
_ROW_B = (
    "| 00000197 | 24 Feb 2026 | Brightwater | Tomas | 700409 | 2 | 200.00 | "
    "0.00 | 0.00 | 200.00 | 100 | 200.00 | — | Invoice #502 |"
)
_MARKERS = (
    '<!-- END_INFERRED marker_id="inf-20260812-fixture-aa11bb" -->\n'
    '<!-- BEGIN_INFERRED marker_id="inf-20260812-fixture-cc22dd" -->'
)


def _marker_boundary_note(gap: str) -> str:
    return (
        "## Statement — 26 Feb 2026\n\n**Statement Date:** 2026-02-26\n\n"
        + _HDR + "\n" + _SEP + "\n" + _ROW_A + "\n"
        + gap + _MARKERS + "\n" + gap + _ROW_B + "\n"
    )


@pytest.mark.parametrize("gap,label", [("", "no blank line"), ("\n", "blank line")])
def test_rows_after_a_marker_pair_survive_either_way(gap, label):
    """The outcome that must hold regardless of mechanism: no lost rows."""
    result = parse_note(_marker_boundary_note(gap))
    assert result.ok, label
    assert [c.claim_no for c in result.claim_lines] == ["00000101", "00000197"]
    assert result.claim_lines[1].amount_paid == Decimal("200.00")


def test_without_a_blank_line_the_table_simply_stays_open():
    """No continuation is logged, because none happened — the markers are
    comment lines inside a table that never closed."""
    with structlog.testing.capture_logs() as captured:
        result = parse_note(_marker_boundary_note(""))
    events = [
        c for c in captured
        if c.get("event") == "reconcile.parser.continuation_table"
    ]
    assert events == []
    assert result.continuations == []
    assert len(result.claim_lines) == 2


def test_with_a_blank_line_the_continuation_branch_carries_the_rows():
    """The other mechanism, same outcome — and here it IS logged, with
    ``same_statement`` true because the marker pair is not a heading."""
    with structlog.testing.capture_logs() as captured:
        result = parse_note(_marker_boundary_note("\n"))
    events = [
        c for c in captured
        if c.get("event") == "reconcile.parser.continuation_table"
    ]
    assert len(events) == 1
    assert events[0]["same_statement"] is True
    assert result.continuations != []
    assert len(result.claim_lines) == 2


def test_rows_after_begin_inferred_are_flagged_inferred():
    """The marker pair is a capture-batch boundary, so what follows is a
    different batch's provenance. Its positive control is in the same
    assertion: the row BEFORE the pair must stay un-inferred."""
    result = parse_note(_marker_boundary_note("\n"))
    assert [c.inferred for c in result.claim_lines] == [False, True]


# --- the bonus shapes from the same block -------------------------------------


def test_a_claim_cell_with_a_parenthetical_is_its_own_claim_number(parsed):
    """``00000301 (Ambulance Claims)``, bare ``(Ambulance Claims)`` and plain
    ``00000301`` are THREE claim numbers, hence three ledger keys.

    That is a decision, not an accident: the cell is what the statement says,
    and normalising the parenthetical away would merge rows the provider
    deliberately kept apart — silently, and in the direction that loses one.
    """
    july = [c for c in parsed.claim_lines if c.statement_date == "2026-07-30"]
    numbers = [c.claim_no for c in july]
    assert "00000301 (Ambulance Claims)" in numbers
    assert "(Ambulance Claims)" in numbers
    assert "00000301" in numbers
    assert len({c.key for c in july}) == len(july)


def test_multiple_ogst_rows_for_one_claimant_stay_distinct(parsed):
    """A second, independent shape exercising the occurrence tiebreak: two
    OGST rows share the ENTIRE ratified four-tuple and differ only in
    amount. Without the tiebreak the larger silently overwrites the smaller.
    """
    ogst = [
        c for c in parsed.claim_lines
        if c.benefit_code == "OGST" and c.statement_date == "2026-07-30"
    ]
    assert len(ogst) == 2
    assert {c.occurrence for c in ogst} == {0, 1}
    assert {c.amount_paid for c in ogst} == {Decimal("20.00"), Decimal("10.00")}
    assert len({c.key for c in ogst}) == 2


def test_the_ogst_collision_is_reported(parsed):
    """Same rule as the ambulance pair: a key that had to disambiguate is
    never a silent event."""
    assert len(parsed.collisions) == 1


def test_an_invoice_reference_with_a_trailing_stop_is_read(parsed):
    """``Invoice #197.`` — the whole comment, full stop included."""
    row = [
        c for c in parsed.claim_lines
        if c.eob_code == "EOB-02" and c.statement_date == "2026-07-30"
    ]
    assert len(row) == 1
    assert row[0].invoice_no == "197"


def test_the_eob_coded_row_fails_open(parsed):
    """``EOB-02`` is unmapped, so it surfaces. The positive control is the
    neighbouring OGST row with no code, which stays clean."""
    report = build_report(_contents(parsed), generated_at="FIXED")
    from alfred.reconcile.attention import CLASS_UNKNOWN_EOB

    assert report.class_counts.get(CLASS_UNKNOWN_EOB) == 2
    clean = [
        c for c in parsed.claim_lines
        if c.benefit_code == "OGST" and not c.eob_code
    ]
    assert clean


def test_the_third_statement_cross_foots(parsed):
    """All the bonus shapes at once, arithmetically. If the parenthetical
    claim numbers or the duplicate OGST rows were being merged, this sum
    would come up short and the subtotals would disagree."""
    report = build_report(_contents(parsed), generated_at="FIXED")
    july = [
        t for t in report.statement_totals
        if t.statement.statement_date == "2026-07-30"
    ][0]
    assert july.paid == Decimal("930.00")
    assert july.subtotals_checked == 2
    assert july.subtotal_mismatches == []
