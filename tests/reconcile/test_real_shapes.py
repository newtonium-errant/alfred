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
    assert len(parsed.claim_lines) == 5
    assert len(parsed.subtotals) == 4
    assert len(parsed.statements) == 2


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
        "Aldenshaw", "Brightwater", "Corvallis", "Dunmoor",
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
