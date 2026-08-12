"""The renderer, and the regenerability property it exists to prove.

**The round trip is the point of this file.** "The note is a render of the
ledger" is a claim, and the only way it stays true as either side changes
is a test that parses a note, renders the ledger back out, re-parses the
render, and compares. Both directions have to keep working: a renderer
that emits a heading the parser does not recognise breaks the loop
silently, and every other test in the suite would stay green.

**What is compared, and what is deliberately not.** The comparison is over
the SEMANTIC payload — keys, money, names, codes, comments, statement
headers. Provenance fields legitimately differ: ``source_line`` describes
where a row was read from, and the render is a different document from the
source. ``inferred`` is in the same category and is called out in its own
test rather than left as a silent gap: the render states inferred
provenance in prose, and does NOT re-emit an attribution marker, because
minting a ``marker_id`` is the audit subsystem's contract and not a
renderer's business.

The renderer is PURE — same ledger, same bytes — which is what makes
wholesale regeneration safe to re-run after a crash.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from alfred.reconcile.ledger import ClaimLine, LedgerContents, Statement
from alfred.reconcile.parser import parse_note
from alfred.reconcile.render import (
    COLUMNS,
    render_note,
    render_row,
    render_statement_section,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CLEAN_NOTE = FIXTURES / "remittance_note_synthetic.md"


def _contents_from(result) -> LedgerContents:
    return LedgerContents(
        statements=result.statements,
        claim_lines=result.claim_lines,
        subtotals=result.subtotals,
    )


def _semantic(line: ClaimLine) -> tuple:
    """Everything about a row EXCEPT where it was read from."""
    return (
        line.key,
        line.statement_date,
        line.claim_no,
        line.dos,
        line.surname,
        line.first_name,
        line.benefit_code,
        line.units,
        line.total_billed,
        line.amt_excluded,
        line.deduct,
        line.amt_eligible,
        line.pct_paid,
        line.amount_paid,
        line.eob_code,
        line.comments,
        line.invoice_no,
        line.row_type,
    )


def test_render_is_pure():
    """Same ledger, same bytes. This is what makes regenerating the note
    wholesale safe to re-run rather than something to be careful about."""
    contents = _contents_from(parse_note(CLEAN_NOTE.read_text(encoding="utf-8")))
    assert render_note(contents) == render_note(contents)


def test_round_trip_preserves_every_claim_line():
    original = parse_note(CLEAN_NOTE.read_text(encoding="utf-8"),
                          source_note="original")
    rendered = render_note(_contents_from(original))
    reparsed = parse_note(rendered, source_note="rendered")

    assert [_semantic(c) for c in reparsed.claim_lines] == [
        _semantic(c) for c in original.claim_lines
    ]


def test_round_trip_preserves_subtotals():
    original = parse_note(CLEAN_NOTE.read_text(encoding="utf-8"))
    reparsed = parse_note(render_note(_contents_from(original)))
    assert [_semantic(s) for s in reparsed.subtotals] == [
        _semantic(s) for s in original.subtotals
    ]


def test_round_trip_preserves_statement_headers():
    original = parse_note(CLEAN_NOTE.read_text(encoding="utf-8"))
    reparsed = parse_note(render_note(_contents_from(original)))
    assert [
        (s.statement_date, s.provider, s.company, s.payment_total)
        for s in reparsed.statements
    ] == [
        (s.statement_date, s.provider, s.company, s.payment_total)
        for s in original.statements
    ]


def test_round_trip_loses_nothing():
    """The render must not produce rows its own parser then refuses."""
    original = parse_note(CLEAN_NOTE.read_text(encoding="utf-8"))
    reparsed = parse_note(render_note(_contents_from(original)))
    assert reparsed.skipped == []
    assert len(reparsed.claim_lines) == len(original.claim_lines)


def test_round_trip_is_stable_on_a_second_pass():
    """Render -> parse -> render must be a fixed point. If it is not, the
    note drifts a little on every regeneration."""
    original = parse_note(CLEAN_NOTE.read_text(encoding="utf-8"))
    first = render_note(_contents_from(original))
    second = render_note(_contents_from(parse_note(first)))
    assert first == second


def test_a_negative_amount_survives_the_round_trip():
    """The highest-consequence value in the file. A reversal rendered as a
    positive would report clawed-back money as received."""
    original = parse_note(CLEAN_NOTE.read_text(encoding="utf-8"))
    reparsed = parse_note(render_note(_contents_from(original)))
    reversal = [c for c in reparsed.claim_lines if c.claim_no == "90000201"]
    assert len(reversal) == 1
    assert reversal[0].amount_paid == Decimal("-480.00")


def test_an_absent_cell_does_not_become_zero_across_the_round_trip():
    """Absent and zero are different facts. A placeholder the parser read
    back as 0.00 would silently invent figures on every regeneration."""
    contents = LedgerContents(
        statements=[Statement(statement_date="2026-01-05")],
        claim_lines=[ClaimLine(
            statement_date="2026-01-05", claim_no="900", dos="2026-01-01",
            benefit_code="700409", total_billed=Decimal("10.00"),
            amount_paid=Decimal("10.00"), deduct=None, units=None,
        )],
    )
    reparsed = parse_note(render_note(contents))
    assert len(reparsed.claim_lines) == 1
    assert reparsed.claim_lines[0].deduct is None
    assert reparsed.claim_lines[0].units is None
    # Positive control: a present value in a neighbouring column DID survive.
    assert reparsed.claim_lines[0].total_billed == Decimal("10.00")


def test_a_pipe_in_a_comment_survives_the_round_trip():
    contents = LedgerContents(
        statements=[Statement(statement_date="2026-01-05")],
        claim_lines=[ClaimLine(
            statement_date="2026-01-05", claim_no="900", dos="2026-01-01",
            benefit_code="700409", amount_paid=Decimal("1.00"),
            comments="split billing | see invoice",
        )],
    )
    reparsed = parse_note(render_note(contents))
    assert len(reparsed.claim_lines) == 1
    assert reparsed.claim_lines[0].comments == "split billing | see invoice"


def test_inferred_provenance_survives_without_minting_a_marker():
    """Two things at once, and both are deliberate.

    The flag SURVIVES the round trip — a statement whose figures were
    inferred at capture is still marked inferred after a regeneration.
    Stating that only in prose was not enough: prose is dropped on the
    second pass, so the note would quietly shed its provenance while every
    other assertion stayed green.

    And it survives WITHOUT a BEGIN_INFERRED marker. Minting a ``marker_id``
    is the audit subsystem's contract; a renderer inventing one would
    fabricate attribution provenance that no audit ever recorded.
    """
    original = parse_note(CLEAN_NOTE.read_text(encoding="utf-8"))
    assert any(s.inferred for s in original.statements)

    rendered = render_note(_contents_from(original))
    assert "BEGIN_INFERRED" not in rendered
    assert "marker_id" not in rendered

    reparsed = parse_note(rendered)
    assert [s.inferred for s in reparsed.statements] == [
        s.inferred for s in original.statements
    ]
    # Positive control: the flag is not simply True everywhere.
    assert not all(s.inferred for s in reparsed.statements)
    assert any(s.inferred for s in reparsed.statements)


def test_inferred_rows_inherit_their_statements_provenance():
    """The row-level half of the flag. The render states the fact once, on
    the statement; its rows must pick it up, or a regenerated ledger loses
    per-row provenance while the header keeps it."""
    original = parse_note(CLEAN_NOTE.read_text(encoding="utf-8"))
    reparsed = parse_note(render_note(_contents_from(original)))
    assert [c.inferred for c in reparsed.claim_lines] == [
        c.inferred for c in original.claim_lines
    ]
    assert sum(1 for c in reparsed.claim_lines if c.inferred) == 1


def test_renderer_column_labels_are_the_documented_schema():
    labels = [label for _, label in COLUMNS]
    assert labels == [
        "Claim #", "Date of Service", "Surname", "First Name", "Benefit Code",
        "Units", "Total Billed", "Amt Excluded", "Deduct", "Amt Eligible",
        "% PD", "Amount Paid", "EOB", "Comments",
    ]


def test_render_row_emits_one_cell_per_column():
    line = ClaimLine(claim_no="900", dos="2026-01-01",
                     amount_paid=Decimal("1.00"))
    row = render_row(line)
    assert row.startswith("|") and row.endswith("|")
    assert row.count("|") == len(COLUMNS) + 1


def test_percent_renders_whole_when_it_is_whole():
    line = ClaimLine(claim_no="900", pct_paid=Decimal("100"))
    assert "| 100 |" in render_row(line)


def test_empty_ledger_renders_an_explicit_empty_state():
    """ILB: an empty ledger must SAY it is empty. A note that rendered
    blank would be indistinguishable from a renderer that broke."""
    body = render_note(LedgerContents())
    assert "ledger is empty" in body
    assert "alfred reconcile seed" in body


def test_statement_with_no_lines_says_so():
    """A partial-page statement whose rows could not be read renders an
    explicit note pointing at the skipped list, not an empty section."""
    section = render_statement_section(
        Statement(statement_date="2026-01-05", provider="Wren Alderly"), []
    )
    assert "No claim lines recorded" in section
    assert "skipped-row list" in section


def test_render_carries_the_regeneration_banner():
    contents = _contents_from(parse_note(CLEAN_NOTE.read_text(encoding="utf-8")))
    body = render_note(contents)
    assert "Machine-generated" in body
    assert "regenerated from the remittance ledger" in body


def test_render_reports_the_totals_it_is_showing():
    contents = _contents_from(parse_note(CLEAN_NOTE.read_text(encoding="utf-8")))
    body = render_note(contents)
    assert "3 statement(s), 7 claim line(s)" in body


def test_render_touches_no_vault(tmp_path, monkeypatch):
    """P1 writes nothing. The renderer returns a string; if it ever grows a
    file write, this fails — which is the alarm, since a vault write is a
    capability that needs a scope rule before it exists."""
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.rglob("*"))
    contents = _contents_from(parse_note(CLEAN_NOTE.read_text(encoding="utf-8")))
    result = render_note(contents)
    assert isinstance(result, str)
    assert set(tmp_path.rglob("*")) == before
