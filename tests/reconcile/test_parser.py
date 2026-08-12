"""The note parser — header-driven, fail-loud, idempotent.

The properties under test:

  1. **Every structural quirk in the real source is handled**, each with a
     named test: subtotal rows, OGST siblings, negative reversals, missing
     claim numbers, ``Invoice #N`` comments, escaped pipes, INFERRED spans,
     partial-page statements.
  2. **Nothing is lost silently.** The damaged fixture's rows each produce
     a named skip entry — and it carries a well-formed positive-control row
     that MUST parse, so "everything was skipped" cannot pass here.
  3. **Column mapping is by NAME.** Reordered columns still parse; an
     unrecognised heading is reported rather than read as some other column.
  4. **Re-parsing is deterministic**, which is what makes the ledger upsert
     idempotent.

The fixtures are wholly invented (see the header comment in each). No real
claimant, provider, claim number or payment appears in this repository.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import structlog

from alfred.reconcile.ledger import ROW_CLAIM, ROW_SUBTOTAL
from alfred.reconcile.money import DATE_ORDER_DMY
from alfred.reconcile.parser import (
    COLUMN_SYNONYMS,
    normalise_heading,
    parse_invoice_no,
    parse_note,
    split_table_row,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CLEAN_NOTE = FIXTURES / "remittance_note_synthetic.md"
DAMAGED_NOTE = FIXTURES / "remittance_note_damaged.md"


@pytest.fixture(scope="module")
def clean():
    return parse_note(CLEAN_NOTE.read_text(encoding="utf-8"),
                      source_note="clean-fixture")


@pytest.fixture(scope="module")
def damaged():
    return parse_note(DAMAGED_NOTE.read_text(encoding="utf-8"),
                      source_note="damaged-fixture")


# --- the clean fixture: every quirk ------------------------------------------


def test_clean_fixture_parses_with_no_losses(clean):
    assert clean.ok
    assert clean.skipped == []
    assert len(clean.statements) == 3
    assert len(clean.claim_lines) == 7
    assert len(clean.subtotals) == 4


def test_a_title_heading_does_not_mint_an_empty_statement(clean):
    """The note's H1 is a heading like any other. Emitting a statement for
    it would put a phantom section in every render and inflate every
    statement count in the report."""
    assert all(s.statement_date for s in clean.statements)
    assert len(clean.statements) == 3


def test_statement_headers_are_read(clean):
    first = clean.statements[0]
    assert first.statement_date == "2026-02-26"
    assert first.provider == "Wren Alderly"
    assert first.company == "Northbay Therapy Services"
    assert first.payment_total == Decimal("348.00")


def test_bold_metadata_value_is_not_captured_with_its_asterisks(clean):
    """``**Provider:** Wren Alderly`` must yield ``Wren Alderly`` — a
    regex that tried to encode the asterisk positions captured
    ``** Wren Alderly`` instead, and the wrong value reached the ledger."""
    assert clean.statements[0].provider == "Wren Alderly"
    assert "*" not in clean.statements[0].provider


def test_ogst_sibling_keeps_its_own_row(clean):
    """A GST line shares its claim number with the benefit line it sits
    under. Different benefit code means a different key, so both survive."""
    siblings = [c for c in clean.claim_lines if c.claim_no == "90000101"]
    assert len(siblings) == 2
    assert {c.benefit_code for c in siblings} == {"700409", "OGST"}
    assert all(c.occurrence == 0 for c in siblings)


def test_subtotal_rows_are_kept_not_discarded(clean):
    """They are the provider's own arithmetic — the only independent check
    the report has. Discarding them would throw the cross-foot away."""
    assert len(clean.subtotals) == 4
    assert all(s.row_type == ROW_SUBTOTAL for s in clean.subtotals)
    assert all(c.row_type == ROW_CLAIM for c in clean.claim_lines)
    aldenshaw = [s for s in clean.subtotals if s.surname == "Aldenshaw"]
    assert len(aldenshaw) == 1
    assert aldenshaw[0].amount_paid == Decimal("252.00")


def test_negative_amount_paid_is_read_as_a_reversal(clean):
    reversal = [c for c in clean.claim_lines if c.claim_no == "90000201"]
    assert len(reversal) == 1
    assert reversal[0].amount_paid == Decimal("-480.00")


def test_missing_claim_number_lines_are_disambiguated_not_merged(clean):
    """Two ``(Ambulance Claims)`` lines share all four ratified key parts.
    Both must survive with distinct keys, and the collision must be
    REPORTED — a key that had to disambiguate is never a silent event."""
    ambulance = [
        c for c in clean.claim_lines if c.claim_no == "(Ambulance Claims)"
    ]
    assert len(ambulance) == 2
    assert {c.occurrence for c in ambulance} == {0, 1}
    assert len({c.key for c in ambulance}) == 2
    assert {c.amount_paid for c in ambulance} == {
        Decimal("300.00"), Decimal("150.00")
    }
    assert len(clean.collisions) == 1


def test_subtotal_key_collisions_are_not_reported_as_findings(clean):
    """Every subtotal row on a statement shares a key by construction.
    Counting them would bury the one collision that means something."""
    assert len(clean.collisions) == 1
    assert "Ambulance" in clean.collisions[0]


def test_invoice_number_is_extracted_from_comments(clean):
    by_claim = {c.claim_no: c for c in clean.claim_lines}
    assert by_claim["90000102"].invoice_no == "502"
    assert by_claim["90000201"].invoice_no == "511"


def test_escaped_pipe_in_a_comment_survives(clean):
    """The comments column is free text; a pipe inside it must not split
    the row. The positive control is that the row parsed at all."""
    everly = [c for c in clean.claim_lines if c.surname == "Everly"]
    assert len(everly) == 1
    assert "|" in everly[0].comments
    assert everly[0].invoice_no == "520"


def test_inferred_span_flags_its_rows_and_only_its_rows(clean):
    """Provenance: an inferred figure and a transcribed one are different
    evidence. The pin has its own positive control — rows OUTSIDE the span
    must be flagged False, or an always-True bug would pass."""
    inferred = [c for c in clean.claim_lines if c.inferred]
    not_inferred = [c for c in clean.claim_lines if not c.inferred]
    assert len(inferred) == 1
    assert inferred[0].surname == "Everly"
    assert len(not_inferred) == 6
    assert clean.statements[2].inferred is True
    assert clean.statements[0].inferred is False


def test_partial_page_statement_without_a_provider_still_parses(clean):
    """The third statement names no provider. It is kept with an empty
    provider rather than dropped — a header this parser could not fully
    read must not become a missing statement."""
    third = clean.statements[2]
    assert third.statement_date == "2026-07-30"
    assert third.provider == ""
    assert third.claim_line_count == 1


def test_absent_text_cells_do_not_carry_an_em_dash_into_the_key(clean):
    """``benefit_code`` is part of the key. An em-dash placeholder read as
    data would put a dash in the key and print it back as if it were a
    benefit code."""
    for sub in clean.subtotals:
        assert sub.benefit_code == ""
        assert "—" not in sub.key


def test_statement_date_is_stamped_on_every_claim_line(clean):
    for line in clean.claim_lines:
        assert line.statement_date


def test_reparsing_is_deterministic(clean):
    """What makes the ledger upsert idempotent: the same note yields the
    same keys, in the same order, every time."""
    again = parse_note(CLEAN_NOTE.read_text(encoding="utf-8"),
                       source_note="clean-fixture")
    assert [c.key for c in again.claim_lines] == [c.key for c in clean.claim_lines]


def test_provenance_is_recorded_on_every_row():
    result = parse_note(
        CLEAN_NOTE.read_text(encoding="utf-8"),
        source_note="/notes/summary.md",
        batch_id="batch-20260812-aa",
        session="sess-1",
        capture_ref="scan-7",
    )
    for line in result.claim_lines:
        assert line.source_note == "/notes/summary.md"
        assert line.batch_id == "batch-20260812-aa"
        assert line.session == "sess-1"
        assert line.capture_ref == "scan-7"
        assert line.source_line > 0


# --- the damaged fixture: nothing lost silently -------------------------------


def test_damaged_fixture_keeps_its_positive_control(damaged):
    """The control that makes every assertion below meaningful: one row IS
    well-formed and must parse. Without it, 'all rows were skipped' passes
    just as well against a parser that is entirely broken."""
    assert len(damaged.claim_lines) == 1
    assert damaged.claim_lines[0].claim_no == "90000401"
    assert damaged.claim_lines[0].amount_paid == Decimal("100.00")


def test_damaged_fixture_reports_every_lost_row(damaged):
    assert not damaged.ok
    assert len(damaged.skipped) == 7
    for skip in damaged.skipped:
        assert skip.line_no > 0
        assert skip.reason
        assert skip.raw


@pytest.mark.parametrize(
    "fragment",
    [
        "cannot read 'N0T-A-NUMBER'",
        "ambiguous",
        "ragged row",
        "fractional",
        "not a real calendar date",
        "missing required column",
        "header at line",
    ],
)
def test_each_damage_mode_names_its_own_cause(damaged, fragment):
    """Seven rows, seven distinct reasons. A skip list where every entry
    said 'could not parse' would be no more actionable than silence."""
    assert any(fragment in s.reason for s in damaged.skipped)


def test_a_rejected_table_reports_its_rows_once_each(damaged):
    """A table whose header is refused must not re-report the header error
    for every data row underneath it — that buries the actual cause."""
    header_errors = [
        s for s in damaged.skipped if "missing required column" in s.reason
    ]
    assert len(header_errors) == 1


def test_skipped_rows_are_logged_with_counts(damaged):
    with structlog.testing.capture_logs() as captured:
        parse_note(DAMAGED_NOTE.read_text(encoding="utf-8"),
                   source_note="damaged")
    events = [
        c for c in captured if c.get("event") == "reconcile.parser.rows_skipped"
    ]
    assert len(events) == 1
    assert events[0]["skipped"] == 7
    assert events[0]["kept"] == 1


def test_slash_date_parses_once_the_order_is_configured():
    """The positive control for the ambiguous-date refusal: the same row
    DOES land in the ledger when the operator declares the convention."""
    result = parse_note(
        DAMAGED_NOTE.read_text(encoding="utf-8"),
        source_note="damaged",
        date_order=DATE_ORDER_DMY,
    )
    dev = [c for c in result.claim_lines if c.surname == "Corvallis"]
    assert len(dev) == 1
    assert dev[0].dos == "2026-04-03"


# --- header-driven mapping ----------------------------------------------------


def test_columns_map_by_name_not_position():
    note = (
        "## Statement — 2026-01-05\n\n"
        "| Amount Paid | Claim # | Date of Service | Total Billed |\n"
        "| --- | --- | --- | --- |\n"
        "| 50.00 | 900 | 2026-01-01 | 60.00 |\n"
    )
    result = parse_note(note)
    assert len(result.claim_lines) == 1
    line = result.claim_lines[0]
    assert line.amount_paid == Decimal("50.00")
    assert line.total_billed == Decimal("60.00")
    assert line.claim_no == "900"


def test_unrecognised_heading_is_reported_not_silently_read():
    note = (
        "## Statement — 2026-01-05\n\n"
        "| Claim # | Date of Service | Amount Paid | Mystery Column |\n"
        "| --- | --- | --- | --- |\n"
        "| 900 | 2026-01-01 | 50.00 | something |\n"
    )
    with structlog.testing.capture_logs() as captured:
        result = parse_note(note)
    assert result.unmapped_headings == ["Mystery Column"]
    # Positive control: the recognised columns still parsed.
    assert result.claim_lines[0].amount_paid == Decimal("50.00")
    events = [
        c for c in captured
        if c.get("event") == "reconcile.parser.unmapped_headings"
    ]
    assert len(events) == 1


def test_a_table_missing_a_required_column_is_refused_with_the_reason():
    note = (
        "## Statement — 2026-01-05\n\n"
        "| Claim # | Surname |\n"
        "| --- | --- |\n"
        "| 900 | Aldenshaw |\n"
    )
    with structlog.testing.capture_logs() as captured:
        result = parse_note(note)
    assert result.claim_lines == []
    events = [
        c for c in captured
        if c.get("event") == "reconcile.parser.table_unrecognised"
    ]
    assert len(events) == 1
    assert set(events[0]["missing"]) == {"amount_paid", "dos"}


def test_note_with_no_tables_says_so():
    """ILB: 'found nothing' is a result and must be stated. A silent empty
    return is indistinguishable from a parser that crashed."""
    with structlog.testing.capture_logs() as captured:
        result = parse_note("# Just a heading\n\nSome prose, no tables.\n")
    assert result.tables_seen == 0
    assert "no claim tables" in result.summary()
    events = [c for c in captured if c.get("event") == "reconcile.parser.no_tables"]
    assert len(events) == 1


def test_summary_is_never_empty():
    assert parse_note("").summary()
    assert parse_note("# x").summary()


# --- unit-level helpers -------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("% PD", "pctpd"),
        ("Claim #", "claimhash"),
        ("Amt. Eligible", "amteligible"),
        ("  Date of Service  ", "dateofservice"),
    ],
)
def test_normalise_heading(raw, expected):
    assert normalise_heading(raw) == expected


def test_normalised_headings_are_all_present_in_the_synonym_map():
    """The renderer emits these labels; the parser must recognise them, or
    a rendered note stops being re-readable."""
    for label in ("Claim #", "Date of Service", "% PD", "Amount Paid", "EOB"):
        assert normalise_heading(label) in COLUMN_SYNONYMS


def test_split_table_row_honours_escaped_pipes():
    cells = split_table_row(r"| a | b \| c | d |")
    assert cells == ["a", "b | c", "d"]


def test_split_table_row_plain():
    assert split_table_row("| a | b |") == ["a", "b"]


@pytest.mark.parametrize(
    "comment,expected",
    [
        ("Invoice #163", "163"),
        ("invoice 163", "163"),
        ("Inv. #163 partial", "163"),
        ("see INVOICE#163", "163"),
        ("no invoice here", ""),
        ("", ""),
    ],
)
def test_parse_invoice_no(comment, expected):
    assert parse_invoice_no(comment) == expected


def test_parse_invoice_no_takes_only_the_first_of_two():
    """Two invoices in one comment is genuinely ambiguous about which the
    line belongs to. The full comment stays on the row, so the ambiguity
    stays visible rather than being resolved by a silent guess."""
    assert parse_invoice_no("Invoice #163 and Invoice #164") == "163"
