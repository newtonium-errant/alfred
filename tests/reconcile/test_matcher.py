"""The matcher — the judgement piece, and the pins that hold it honest.

Every fixture here is INVENTED. No claimant name, k-number, invoice number
or figure comes from the live snapshot or the live ledger; the shapes are
real, the data is not.

The pins are organised around the ways a matcher can be confidently wrong,
because that is the failure that costs money: a payment attached to the
wrong claimant does not look like an error on the report, it looks like a
result. So the exclusion pins all carry POSITIVE CONTROLS — "X is refused"
is vacuous unless the same test proves X's nearest admissible neighbour is
accepted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import structlog

from alfred.reconcile.aging import find_late
from alfred.reconcile.invoices import (
    Invoice,
    InvoiceLineItem,
    InvoiceSnapshot,
)
from alfred.reconcile.ledger import ClaimLine
from alfred.reconcile.matcher import (
    AMOUNT_AGREES,
    AMOUNT_DISAGREES,
    AMOUNT_OTHER_BASIS,
    AMOUNT_UNCOMPARABLE,
    BAND_MEDIUM_AT,
    BASIS_CLASS_LABELS,
    BASIS_INCL_TAX,
    BASIS_SENTENCES,
    BASIS_TAX_LINE,
    HST_RATIO,
    RATIO_TOLERANCE,
    TAX_LINE_STEP,
    CLASS_SEPARATION,
    SMALLEST_INVOICE_AMOUNT,
    WEIGHT_AMOUNT_OTHER_BASIS,
    _band,
    worst_case_ratio_deviation,
    recognise_basis,
    BAND_HIGH,
    BAND_LOW,
    BAND_MEDIUM,
    BOOST_CITATION_CROSSWALK,
    BOOST_CITATION_UNCONFIRMABLE,
    CITATION_CROSSWALK,
    CITATION_NONE,
    CITATION_UNCONFIRMABLE,
    MATCHABLE_STATUSES,
    PENALTY_AMBIGUOUS_SURNAME,
    PENALTY_AMOUNT_DISAGREES,
    WEIGHT_AMOUNT_AGREES,
    WEIGHT_BUCKET,
    build_accounting_index,
    build_buckets,
    match_snapshot,
    matchable_invoices,
)

_NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
_FRESH = "2026-08-13T02:30:00Z"


def _snapshot(*invoices: Invoice, exported_at: str = _FRESH) -> InvoiceSnapshot:
    """A snapshot whose age is set directly — the loader is tested elsewhere,
    and a fixture that had to write JSON to control freshness would couple
    every matcher pin to the reader's parsing."""
    return InvoiceSnapshot(
        exported_at=exported_at,
        invoices=list(invoices),
        age_hours=1.0,
    )


def _invoice(
    invoice_no: str,
    client_name: str = "Marisol Aldenshaw",
    *,
    status: str = "sent",
    amount: str | None = "300.00",
    dates: tuple[str, ...] = ("2026-06-11",),
    knumber: str = "K1234567",
) -> Invoice:
    return Invoice(
        invoice_no=invoice_no,
        client_name=client_name,
        knumber=knumber,
        status=status,
        date_sent="2026-06-12",
        invoice_date="2026-06-11",
        amount_excl_tax=None if amount is None else Decimal(amount),
        line_items=[
            InvoiceLineItem(date_of_service=d, amount=Decimal("150.00"))
            for d in dates
        ],
    )


def _line(
    claim_no: str = "C-1",
    *,
    surname: str = "Aldenshaw",
    first_name: str = "Marisol",
    dos: str = "2026-06-11",
    billed: str | None = "300.00",
    invoice_no: str = "",
) -> ClaimLine:
    return ClaimLine(
        statement_date="2026-07-01",
        claim_no=claim_no,
        dos=dos,
        surname=surname,
        first_name=first_name,
        benefit_code="BC1",
        total_billed=None if billed is None else Decimal(billed),
        amount_paid=Decimal("250.00"),
        invoice_no=invoice_no,
    )


# --- the join itself ---------------------------------------------------------


def test_a_clean_single_candidate_is_proposed_at_high_confidence() -> None:
    """The positive control the whole file leans on: the happy path works."""
    report = match_snapshot([_line()], _snapshot(_invoice("INV-1")))

    assert len(report.proposals) == 1
    p = report.proposals[0]
    assert p.invoice_no == "INV-1"
    assert p.claimant == "Aldenshaw, Marisol"
    assert p.service_date == "2026-06-11"
    assert p.amount_outcome == AMOUNT_AGREES
    assert p.amount_delta == Decimal("0")
    assert p.band == BAND_HIGH
    # LITERAL, never recomputed from the constants under test: a pin that
    # derives its expectation from the thing it guards moves WITH it and
    # can never detect a change to it.
    assert p.confidence == 0.90
    assert p.citation_status == CITATION_NONE
    assert not report.ambiguous
    assert not report.unmatched


def test_the_bucket_sums_a_multi_leg_day_against_one_invoice() -> None:
    """RRTS day-buckets, so two legs on one day are ONE invoice's worth.

    Matching per-line instead of per-day would compare one leg's billed
    figure against the whole invoice and conclude the amounts disagree on
    every multi-leg day — the systematic false finding this grouping exists
    to prevent.
    """
    legs = [
        _line("C-1", billed="150.00"),
        _line("C-2", billed="150.00"),
    ]
    report = match_snapshot(legs, _snapshot(_invoice("INV-1", amount="300.00")))

    assert len(report.proposals) == 1
    p = report.proposals[0]
    assert p.ledger_billed == Decimal("300.00")
    assert p.amount_outcome == AMOUNT_AGREES
    assert len(p.line_keys) == 2, "both legs must travel with the proposal"


def test_the_amount_discriminates_between_same_day_candidates() -> None:
    """The ratified join's second half, doing the job it exists for."""
    report = match_snapshot(
        [_line(billed="425.00")],
        _snapshot(
            _invoice("INV-1", amount="300.00"),
            _invoice("INV-2", amount="425.00"),
        ),
    )

    assert len(report.proposals) == 1
    assert report.proposals[0].invoice_no == "INV-2"
    assert not report.ambiguous


def test_two_same_day_candidates_the_amount_cannot_split_are_refused() -> None:
    """And the POSITIVE CONTROL is in the same test: the identical setup
    with one distinguishing amount DOES produce a proposal.

    Without the control this pin passes just as green against a matcher that
    proposes nothing at all, ever.
    """
    twins = _snapshot(
        _invoice("INV-1", amount="300.00"),
        _invoice("INV-2", amount="300.00"),
    )
    refused = match_snapshot([_line(billed="300.00")], twins)
    assert not refused.proposals
    assert len(refused.ambiguous) == 1
    assert sorted(refused.ambiguous[0].candidate_invoice_nos) == ["INV-1", "INV-2"]
    assert refused.matched_invoice_nos == set()

    # POSITIVE CONTROL — same shape, one amount moved.
    split = _snapshot(
        _invoice("INV-1", amount="300.00"),
        _invoice("INV-2", amount="999.00"),
    )
    accepted = match_snapshot([_line(billed="300.00")], split)
    assert [p.invoice_no for p in accepted.proposals] == ["INV-1"]


def test_no_candidate_at_all_is_reported_not_dropped() -> None:
    """The ledger says a claim was processed; the export has nothing."""
    report = match_snapshot(
        [_line(surname="Quillon", first_name="Bram")],
        _snapshot(_invoice("INV-1")),
    )
    assert not report.proposals
    assert len(report.unmatched) == 1
    assert report.unmatched[0].claimant == "Quillon, Bram"
    assert "nothing to join it to" in report.unmatched[0].reason


def test_a_date_outside_the_invoices_line_items_does_not_match() -> None:
    """Date is half the bucket, with its own positive control."""
    invoice = _invoice("INV-1", dates=("2026-06-11",))
    assert not match_snapshot([_line(dos="2026-06-12")], _snapshot(invoice)).proposals
    # POSITIVE CONTROL: the same claimant on the covered date does match.
    assert match_snapshot([_line(dos="2026-06-11")], _snapshot(invoice)).proposals


# --- the amount's THREE outcomes ---------------------------------------------


def test_a_disagreeing_amount_still_proposes_a_sole_candidate() -> None:
    """Deliberate: whether the ledger's total_billed sits on the same tax
    basis as RRTS's amount_excl_tax is not established anywhere in this repo.
    Requiring agreement would make the matcher propose NOTHING if the bases
    differ — a silent total failure. Stating the delta makes the same
    situation a legible finding on the first report."""
    report = match_snapshot(
        [_line(billed="345.00")],
        _snapshot(_invoice("INV-1", amount="300.00")),
    )

    assert len(report.proposals) == 1
    p = report.proposals[0]
    assert p.amount_outcome == AMOUNT_DISAGREES
    assert p.amount_delta == Decimal("45.00")
    assert p.band == BAND_LOW
    assert any("DISAGREE" in b for b in p.basis)


def test_an_absent_figure_is_uncomparable_not_disagreement() -> None:
    """Absent and zero are different facts — this package's rule everywhere
    else, and a missing cell must not manufacture a money finding."""
    no_invoice_amount = match_snapshot(
        [_line(billed="300.00")],
        _snapshot(_invoice("INV-1", amount=None)),
    )
    assert no_invoice_amount.proposals[0].amount_outcome == AMOUNT_UNCOMPARABLE
    assert no_invoice_amount.proposals[0].amount_delta is None

    no_ledger_amount = match_snapshot(
        [_line(billed=None)],
        _snapshot(_invoice("INV-1", amount="300.00")),
    )
    assert no_ledger_amount.proposals[0].amount_outcome == AMOUNT_UNCOMPARABLE

    # POSITIVE CONTROL: with both figures present the comparison happens.
    both = match_snapshot(
        [_line(billed="300.00")], _snapshot(_invoice("INV-1", amount="300.00"))
    )
    assert both.proposals[0].amount_outcome == AMOUNT_AGREES


def test_a_partial_billed_sum_says_so_on_the_proposal() -> None:
    """One leg with no billed figure makes the sum partial, and comparing a
    partial sum as though it were complete is how a real agreement is read
    off incomplete arithmetic."""
    report = match_snapshot(
        [_line("C-1", billed="150.00"), _line("C-2", billed=None)],
        _snapshot(_invoice("INV-1", amount="150.00")),
    )
    p = report.proposals[0]
    assert any("partial" in b for b in p.basis)


# --- the name judgement travels ----------------------------------------------


def test_an_ambiguous_surname_split_is_weighted_down_and_stated() -> None:
    """A three-token name is either a middle name or a two-word surname and
    the string cannot say which. The match may still be right; the score and
    the basis both say it rests on a guess."""
    report = match_snapshot(
        [_line(surname="Ashby", first_name="Wren")],
        _snapshot(_invoice("INV-1", client_name="Wren Dunmoor Ashby")),
    )

    assert len(report.proposals) == 1
    p = report.proposals[0]
    assert p.ambiguous_name is True
    assert p.confidence == 0.70  # literal, not recomputed from the weights
    assert p.band == BAND_MEDIUM, (
        "an ambiguous split must not read as high confidence — a confident "
        "wrong join is the specific harm this penalty exists for"
    )
    assert any("unambiguously" in b for b in p.basis)

    # POSITIVE CONTROL: the unambiguous twin scores higher and is flagged
    # clean, so the penalty is measuring ambiguity rather than firing always.
    clean = match_snapshot(
        [_line()], _snapshot(_invoice("INV-1"))
    ).proposals[0]
    assert clean.ambiguous_name is False
    assert clean.confidence > p.confidence


def test_a_particle_surname_matches_without_being_called_ambiguous() -> None:
    report = match_snapshot(
        [_line(surname="van Corvallis", first_name="Dev")],
        _snapshot(_invoice("INV-1", client_name="Dev van Corvallis")),
    )
    assert len(report.proposals) == 1
    assert report.proposals[0].ambiguous_name is False


# --- the citation: corroborative, and unconfirmable --------------------------


def test_a_citation_matching_the_rrts_number_is_marked_unconfirmable() -> None:
    """Per the operator: the #NNN series is the provider's own accounting
    reference (QuickBooks/Wave), a DIFFERENT series from RRTS's numbers. An
    equality is two schemes landing on one integer, so it is a small nudge
    and the provenance must say it cannot be confirmed."""
    report = match_snapshot(
        [_line(invoice_no="487")],
        _snapshot(_invoice("487")),
    )

    p = report.proposals[0]
    assert p.citation == "487"
    assert p.citation_status == CITATION_UNCONFIRMABLE
    assert p.confidence == 0.95  # literal, not recomputed from the weights
    reason = " ".join(p.basis)
    assert "CANNOT be confirmed" in reason
    assert "ACCOUNTING REFERENCE" in reason


def test_the_citation_boost_is_smaller_than_any_join_component() -> None:
    """A structural pin on the WEIGHTING, not on an outcome. If the
    unconfirmable boost ever grew past a real join component, a coincidence
    between two numbering series would start outweighing evidence that was
    actually confirmed."""
    assert BOOST_CITATION_UNCONFIRMABLE < WEIGHT_AMOUNT_AGREES
    assert BOOST_CITATION_UNCONFIRMABLE < WEIGHT_BUCKET
    assert BOOST_CITATION_UNCONFIRMABLE < PENALTY_AMBIGUOUS_SURNAME
    assert BOOST_CITATION_UNCONFIRMABLE < BOOST_CITATION_CROSSWALK, (
        "an unconfirmable citation must never be worth as much as one "
        "resolved through the crosswalk"
    )


def test_a_citation_never_creates_a_candidate() -> None:
    """THE structural guarantee. The citation may only ever re-weight an
    invoice that already cleared client and date; a coincidence-prone signal
    that could ORIGINATE a match would be a confident-wrong-join generator.

    Here the cited invoice exists and carries the cited number, but names a
    different client — so it must not be proposed at all.
    """
    report = match_snapshot(
        [_line(surname="Quillon", first_name="Bram", invoice_no="487")],
        _snapshot(_invoice("487", client_name="Marisol Aldenshaw")),
    )
    assert not report.proposals
    assert len(report.unmatched) == 1

    # POSITIVE CONTROL: same citation, same invoice, client now agrees.
    ok = match_snapshot(
        [_line(invoice_no="487")],
        _snapshot(_invoice("487", client_name="Marisol Aldenshaw")),
    )
    assert [p.invoice_no for p in ok.proposals] == ["487"]


def test_the_accounting_crosswalk_is_empty_today_and_wired_for_tomorrow() -> None:
    """The seam. RRTS has not shipped the field, and Invoice.from_dict drops
    unknown keys per the schema-tolerance contract, so the index is empty by
    construction — but the lookup is built and reachable, so the day the
    field lands it needs one dataclass field and nothing else."""
    plain = [_invoice("INV-1"), _invoice("INV-2")]
    assert build_accounting_index(plain) == {}, (
        "no ordinary Invoice can carry an accounting reference yet"
    )

    # An invoice that DOES carry the future field lights the crosswalk up
    # without any other change — that is the whole claim of the seam.
    class _FutureInvoice(Invoice):
        accounting_invoice_no: str = ""

    future = _FutureInvoice(
        invoice_no="INV-9",
        client_name="Marisol Aldenshaw",
        status="sent",
        amount_excl_tax=Decimal("300.00"),
        line_items=[InvoiceLineItem(date_of_service="2026-06-11")],
    )
    future.accounting_invoice_no = "487"
    assert build_accounting_index([future]) == {"487": [future]}

    report = match_snapshot([_line(invoice_no="487")], _snapshot(future))
    p = report.proposals[0]
    assert p.citation_status == CITATION_CROSSWALK
    assert p.confidence == 1.0
    assert any("confirmed crosswalk" in b for b in p.basis)


def test_full_confidence_needs_a_confirmed_crosswalk() -> None:
    """The base weights sum to 0.90 on purpose. A clean client+date+amount
    agreement is a HEURISTIC join — no identifier tied the two documents
    together — and scoring it 1.00 would tell the operator the machine is
    certain when it is inferring.

    The first cut had them summing to 1.00, which clamped every citation
    boost into inertness on exactly the cleanest population: the arithmetic
    read correctly and the number never moved. Only running it showed that.
    """
    assert WEIGHT_BUCKET + WEIGHT_AMOUNT_AGREES < 1.0

    heuristic = match_snapshot(
        [_line()], _snapshot(_invoice("INV-1"))
    ).proposals[0]
    assert heuristic.band == BAND_HIGH
    assert heuristic.confidence < 1.0, (
        "a join with no identifier behind it must not read as certainty"
    )

    # Only the crosswalk reaches the ceiling — proven by the pin above.
    assert (
        WEIGHT_BUCKET + WEIGHT_AMOUNT_AGREES + BOOST_CITATION_CROSSWALK >= 1.0
    )


# --- which invoices are matchable at all -------------------------------------


def test_void_created_and_unknown_statuses_are_not_matchable() -> None:
    """Each exclusion with its admissible neighbour in the same assertion
    set, so the pin cannot pass against a matcher that admits nothing."""
    excluded = ["void", "created", "some_status_we_have_never_seen"]
    for status in excluded:
        snap = _snapshot(_invoice("INV-1", status=status))
        assert matchable_invoices(snap) == [], status
        assert not match_snapshot([_line()], snap).proposals, status

    # POSITIVE CONTROLS: both matchable statuses are admitted and proposed.
    for status in sorted(MATCHABLE_STATUSES):
        snap = _snapshot(_invoice("INV-1", status=status))
        assert len(matchable_invoices(snap)) == 1, status
        assert len(match_snapshot([_line()], snap).proposals) == 1, status


def test_a_paid_invoice_is_matchable_even_though_it_can_never_be_late() -> None:
    """The two sets are deliberately different. A paid invoice is exactly
    what a remittance line should match — it is how it became paid — while
    the watchdog must never chase it."""
    snap = _snapshot(_invoice("INV-1", status="paid"))
    assert len(match_snapshot([_line()], snap).proposals) == 1
    assert find_late(snap, now=_NOW).late == []


def test_a_junk_row_never_reaches_the_matcher() -> None:
    junk = _invoice("INV-ERROR")
    snap = _snapshot(junk, _invoice("INV-1"))
    assert [i.invoice_no for i in matchable_invoices(snap)] == ["INV-1"]


# --- staleness ---------------------------------------------------------------


def test_a_stale_snapshot_matches_nothing_and_says_so() -> None:
    stale = InvoiceSnapshot(
        exported_at="2026-08-01T02:30:00Z",
        invoices=[_invoice("INV-1")],
        age_hours=300.0,
    )
    with structlog.testing.capture_logs() as captured:
        report = match_snapshot([_line()], stale)

    assert report.stale_snapshot is True
    assert not report.proposals
    assert report.matched_invoice_nos == set()
    assert "stale" in report.summary().lower()

    events = [
        c for c in captured
        if c.get("event") == "reconcile.matcher.skipped_stale"
    ]
    assert len(events) == 1
    assert events[0]["exported_at"] == "2026-08-01T02:30:00Z"

    # POSITIVE CONTROL: the same invoice from a FRESH snapshot is proposed,
    # so the refusal is about staleness rather than about the fixture.
    assert match_snapshot([_line()], _snapshot(_invoice("INV-1"))).proposals


# --- bucketing ---------------------------------------------------------------


def test_lines_that_cannot_be_bucketed_are_counted_not_dropped() -> None:
    lines = [
        _line("C-1"),
        _line("C-2", surname=""),
        _line("C-3", dos="not a date"),
    ]
    buckets, unbucketable = build_buckets(lines)
    assert len(buckets) == 1
    assert len(unbucketable) == 2

    with structlog.testing.capture_logs() as captured:
        report = match_snapshot(lines, _snapshot(_invoice("INV-1")))
    assert len(report.unbucketable) == 2
    warned = [
        c for c in captured
        if c.get("event") == "reconcile.matcher.unbucketable"
    ]
    assert len(warned) == 1
    assert warned[0]["count"] == 2


def test_two_claimants_sharing_a_surname_stay_in_separate_buckets() -> None:
    """The ledger holds the first name; throwing it away at grouping time
    and re-guessing later is the second-spelling shape."""
    buckets, _ = build_buckets([
        _line("C-1", surname="Aldenshaw", first_name="Marisol"),
        _line("C-2", surname="Aldenshaw", first_name="Tobias"),
    ])
    assert len(buckets) == 2


def test_one_invoice_is_never_proposed_against_two_groups() -> None:
    """An invoice spanning two service dates is a candidate for both, and
    proposing it twice would tell the operator one invoice settled two days'
    claims — and would double-count it in matched_invoice_nos."""
    spanning = _invoice(
        "INV-1", amount="300.00", dates=("2026-06-11", "2026-06-12")
    )
    with structlog.testing.capture_logs() as captured:
        report = match_snapshot(
            [
                _line("C-1", dos="2026-06-11", billed="300.00"),
                _line("C-2", dos="2026-06-12", billed="999.00"),
            ],
            _snapshot(spanning),
        )

    assert len(report.proposals) == 1, "the higher-scoring group holds it"
    assert report.proposals[0].service_date == "2026-06-11"
    assert len(report.ambiguous) == 1
    assert report.matched_invoice_nos == {"INV-1"}

    contested = [
        c for c in captured
        if c.get("event") == "reconcile.matcher.contested_invoices"
    ]
    assert len(contested) == 1
    assert contested[0]["demoted"] == 1


def test_a_tie_between_two_groups_keeps_neither() -> None:
    """Picking between equally-scored proposals would be exactly the
    arbitrary choice this module refuses to make."""
    spanning = _invoice(
        "INV-1", amount="300.00", dates=("2026-06-11", "2026-06-12")
    )
    report = match_snapshot(
        [
            _line("C-1", dos="2026-06-11", billed="300.00"),
            _line("C-2", dos="2026-06-12", billed="300.00"),
        ],
        _snapshot(spanning),
    )
    assert not report.proposals
    assert len(report.ambiguous) == 2
    assert report.matched_invoice_nos == set()


# --- the seam the watchdog consumes ------------------------------------------


def test_a_proposed_invoice_stops_being_chased() -> None:
    """The contract between the two halves, exercised end to end rather than
    asserted about a set literal."""
    old = _invoice("INV-OLD", dates=("2026-06-11",))
    old.date_sent = "2026-01-01"
    other = _invoice("INV-OTHER", client_name="Bram Quillon", dates=("2026-06-11",))
    other.date_sent = "2026-01-01"
    other.knumber = "K7654321"
    snap = _snapshot(old, other)

    match = match_snapshot([_line()], snap)
    assert match.matched_invoice_nos == {"INV-OLD"}

    chased = find_late(
        snap, matched_invoice_nos=match.matched_invoice_nos, now=_NOW
    )
    assert [e.invoice_no for e in chased.late] == ["INV-OTHER"], (
        "the matched invoice must drop off the chase list and the unmatched "
        "one must stay on it"
    )

    # POSITIVE CONTROL: with no match supplied, BOTH are chased — so the
    # exclusion above is the matcher's doing, not an empty chase list.
    assert len(find_late(snap, now=_NOW).late) == 2


def test_an_ambiguous_group_contributes_nothing_to_the_matched_set() -> None:
    """The safe direction: an invoice wrongly treated as matched would drop
    off the chase list silently."""
    twins = _snapshot(
        _invoice("INV-1", amount="300.00"),
        _invoice("INV-2", amount="300.00"),
    )
    report = match_snapshot([_line(billed="300.00")], twins)
    assert report.matched_invoice_nos == set()


# --- ILB ---------------------------------------------------------------------


def test_the_summary_is_never_empty_in_any_state() -> None:
    states = [
        match_snapshot([], _snapshot()),
        match_snapshot([_line()], _snapshot()),
        match_snapshot([_line()], _snapshot(_invoice("INV-1"))),
        match_snapshot(
            [_line()],
            InvoiceSnapshot(invoices=[_invoice("INV-1")], age_hours=999.0),
        ),
    ]
    for report in states:
        assert report.summary().strip()


def test_nothing_to_join_is_stated_rather_than_silent() -> None:
    report = match_snapshot([], _snapshot(_invoice("INV-1")))
    assert "nothing to join" in report.summary()


def test_the_matched_log_states_when_it_joined_nothing() -> None:
    with structlog.testing.capture_logs() as captured:
        match_snapshot(
            [_line(surname="Quillon", first_name="Bram")],
            _snapshot(_invoice("INV-1")),
        )
    events = [
        c for c in captured if c.get("event") == "reconcile.matcher.matched"
    ]
    assert len(events) == 1
    assert events[0]["proposals"] == 0
    assert events[0]["unmatched"] == 1
    assert "joined nothing" in events[0]["detail"]


def test_the_matched_log_carries_the_counts_that_make_it_greppable() -> None:
    with structlog.testing.capture_logs() as captured:
        match_snapshot([_line()], _snapshot(_invoice("INV-1")))
    event = [
        c for c in captured if c.get("event") == "reconcile.matcher.matched"
    ][0]
    assert event["buckets"] == 1
    assert event["invoices"] == 1
    assert event["proposals"] == 1
    assert event["detail"] == ""


# --- the fourth outcome: recognised delta classes ----------------------------
#
# Ratified constraint: a recognised delta class is AGREEMENT ON A KNOWN OTHER
# BASIS — its own amount_outcome, its own basis sentence, its own weight
# constant beside the others, never a branch inside the disagreement path.
# Exactly two classes ship; the once-observed 0.7826 stays unrecognised,
# because one example is not a class.


def test_a_tax_inclusive_ledger_row_reconciles_rather_than_disagreeing() -> None:
    """x1.14 — the HST factor, which appeared in the data rather than being
    asserted into the module."""
    report = match_snapshot(
        [_line(billed="342.00")],
        _snapshot(_invoice("INV-1", amount="300.00")),
    )

    p = report.proposals[0]
    assert p.amount_outcome == AMOUNT_OTHER_BASIS
    assert p.basis_class == BASIS_INCL_TAX
    assert p.amount_delta == Decimal("42.00"), "the delta is still stated"
    assert p.confidence == 0.75  # literal, not recomputed from the weights
    sentence = " ".join(p.basis)
    assert "RECONCILE" in sentence
    assert "not a disagreement that has been forgiven" in sentence
    assert "DISAGREE" not in sentence, (
        "the fourth outcome must not render the disagreement's words — that "
        "would tell the operator the opposite of what the data shows"
    )


def test_a_tax_line_shape_reconciles_on_its_own_class() -> None:
    report = match_snapshot(
        [_line(billed="42.00")],
        _snapshot(_invoice("INV-1", amount="300.00")),
    )
    p = report.proposals[0]
    assert p.amount_outcome == AMOUNT_OTHER_BASIS
    assert p.basis_class == BASIS_TAX_LINE
    assert "RECONCILE" in " ".join(p.basis)


def test_the_once_observed_ratio_stays_unrecognised() -> None:
    """0.7826 was seen ONCE. One example is not a class, and a vocabulary
    that admits a guess is how a heuristic starts laundering coincidences
    into confidence. POSITIVE CONTROL in the same test: the two measured
    classes ARE recognised, so this is the recogniser discriminating rather
    than recognising nothing."""
    outlier = match_snapshot(
        [_line(billed="234.78")],
        _snapshot(_invoice("INV-1", amount="300.00")),
    ).proposals[0]
    assert outlier.amount_outcome == AMOUNT_DISAGREES
    assert outlier.basis_class == ""
    assert outlier.band == BAND_LOW

    assert recognise_basis(Decimal("342.00"), Decimal("300.00")) == BASIS_INCL_TAX
    assert recognise_basis(Decimal("42.00"), Decimal("300.00")) == BASIS_TAX_LINE


def test_the_unrecognised_bucket_is_the_discovery_channel() -> None:
    """A recogniser that absorbed everything would close the loop that
    produced it. An unrecognised ratio must stay a plain, visible
    disagreement — with its delta and a line saying no class matched."""
    p = match_snapshot(
        [_line(billed="345.00")],
        _snapshot(_invoice("INV-1", amount="300.00")),
    ).proposals[0]
    assert p.amount_outcome == AMOUNT_DISAGREES
    assert p.amount_delta == Decimal("45.00")
    assert "no recognised basis class" in " ".join(p.basis)


# --- the tolerance, bounded at BOTH ends -------------------------------------


def test_the_tolerance_absorbs_cent_rounding_at_the_small_end() -> None:
    """Lower bound. Both figures come from independently rounded documents;
    at the smallest amounts these carry, the induced ratio error is ~0.00094,
    so a tighter tolerance would reject genuine members for rounding alone."""
    # 10.00 * 1.14 = 11.40 exactly; the neighbouring cents must still land.
    for billed in ("11.39", "11.40", "11.41"):
        assert recognise_basis(Decimal(billed), Decimal("10.00")) == (
            BASIS_INCL_TAX
        ), billed


def test_the_two_classes_cannot_collide_at_the_shipped_tolerance() -> None:
    """Upper bound, and the reason precedence is currently non-binding.

    1.14 is NOT an exact multiple of 0.14 — the nearest is 1.12, a gap of
    0.02, twenty times the tolerance. Pinned rather than assumed, because
    the whole classification rests on it.
    """
    gap = abs(HST_RATIO - Decimal("8") * TAX_LINE_STEP)
    assert gap == Decimal("0.02")
    assert gap == CLASS_SEPARATION, "the derived separation must equal the gap"
    assert RATIO_TOLERANCE * 10 <= gap, (
        "the tolerance has grown far enough that the two classes could claim "
        "the same row — precedence would then be doing real work and must be "
        "re-argued, not left to branch order"
    )
    # The neighbouring 0.14-multiple is NOT read as the HST class...
    assert recognise_basis(Decimal("336.00"), Decimal("300.00")) == (
        BASIS_TAX_LINE
    ), "1.12 is a tax-line multiple, not the HST ratio"
    # ...and the HST ratio is NOT read as a tax-line multiple.
    assert recognise_basis(Decimal("342.00"), Decimal("300.00")) == (
        BASIS_INCL_TAX
    )


def test_a_ratio_just_outside_the_tolerance_is_not_recognised() -> None:
    """The tolerance is a width, not a wish — something must fall outside
    it, or 'within tolerance' would be a check that cannot fail."""
    # 300 * 1.145 = 343.50, which is 0.005 off the HST ratio — 5x tolerance.
    assert recognise_basis(Decimal("343.50"), Decimal("300.00")) == ""


# --- defensive shapes --------------------------------------------------------


def test_signs_must_agree_before_a_ratio_means_anything() -> None:
    """A pair whose signs differ is not on another basis, it is on the other
    side of the ledger — calling that a tax shape would explain away a real
    finding. A reversal (negative on BOTH sides) is a genuine ratio."""
    assert recognise_basis(Decimal("-342.00"), Decimal("300.00")) == ""
    assert recognise_basis(Decimal("342.00"), Decimal("-300.00")) == ""
    assert recognise_basis(Decimal("-342.00"), Decimal("-300.00")) == (
        BASIS_INCL_TAX
    )


def test_a_zero_on_either_side_has_no_ratio() -> None:
    assert recognise_basis(Decimal("342.00"), Decimal("0.00")) == ""
    assert recognise_basis(Decimal("0.00"), Decimal("300.00")) == ""


def test_every_recognised_class_has_a_sentence_and_a_label() -> None:
    """Exhaustive by construction: a class added without a sentence would
    KeyError at render time, and one without a label would print its raw
    identifier at the operator."""
    classes = {BASIS_INCL_TAX, BASIS_TAX_LINE}
    assert set(BASIS_SENTENCES) == classes
    assert set(BASIS_CLASS_LABELS) == classes
    for text in BASIS_SENTENCES.values():
        assert "RECONCILE" in text, (
            "a basis sentence must say the figures reconcile — that is what "
            "distinguishes this outcome from a forgiven disagreement"
        )


# --- confidence: the headroom rule holds under the new outcome ---------------


def test_a_recognised_basis_never_reaches_full_confidence() -> None:
    """Full stays reserved for exact agreement plus a confirmed crosswalk.
    The tuning must not widen what counts as certainty."""
    ceiling = (
        WEIGHT_BUCKET + WEIGHT_AMOUNT_OTHER_BASIS + BOOST_CITATION_CROSSWALK
    )
    assert ceiling < 1.0, (
        "recognised-other-basis plus a crosswalk must stay below full"
    )
    assert WEIGHT_AMOUNT_OTHER_BASIS < WEIGHT_AMOUNT_AGREES, (
        "an inference about which basis a row is on must not outrank a raw "
        "agreement"
    )
    assert WEIGHT_BUCKET + WEIGHT_AMOUNT_AGREES + BOOST_CITATION_CROSSWALK >= 1.0


def test_a_recognised_class_rises_out_of_the_low_band() -> None:
    """The point of the tuning, asserted as a band movement rather than as a
    number: the same pair scored LOW before the class existed."""
    p = match_snapshot(
        [_line(billed="342.00")],
        _snapshot(_invoice("INV-1", amount="300.00")),
    ).proposals[0]
    assert p.band == BAND_MEDIUM

    # The before-figure as a LITERAL: this pair scored 0.30 (LOW) before the
    # class existed. Recomputing it from the weights would make the pin
    # follow any change to them and prove nothing.
    scored_as_plain_disagreement = 0.30
    assert scored_as_plain_disagreement < BAND_MEDIUM_AT, (
        "before the class existed this pair scored into the low band — that "
        "is the before-figure this tuning moves"
    )
    assert p.confidence > scored_as_plain_disagreement


# --- discrimination: exact still outranks a recognised class -----------------


def test_an_exact_agreement_outranks_a_recognised_other_basis() -> None:
    report = match_snapshot(
        [_line(billed="300.00")],
        _snapshot(
            _invoice("INV-EXACT", amount="300.00"),
            _invoice("INV-BASIS", amount="263.16"),  # 300/1.14
        ),
    )
    assert [p.invoice_no for p in report.proposals] == ["INV-EXACT"]


def test_a_sole_recognised_basis_wins_when_nothing_agrees_exactly() -> None:
    report = match_snapshot(
        [_line(billed="342.00")],
        _snapshot(
            _invoice("INV-BASIS", amount="300.00"),   # x1.14
            _invoice("INV-NOISE", amount="511.00"),   # no class
        ),
    )
    assert [p.invoice_no for p in report.proposals] == ["INV-BASIS"]

    # POSITIVE CONTROL: two recognised candidates cannot be split, and the
    # matcher refuses rather than picking.
    twins = match_snapshot(
        [_line(billed="342.00")],
        _snapshot(
            _invoice("INV-A", amount="300.00"),
            _invoice("INV-B", amount="300.00"),
        ),
    )
    assert not twins.proposals
    assert len(twins.ambiguous) == 1
    assert "recognised other basis" in twins.ambiguous[0].reason


def test_two_exact_agreements_are_not_broken_by_a_recognised_basis() -> None:
    """The guard the precedence actually lives in, reached on purpose.

    The obvious precedence pin — one exact candidate beside one other-basis
    candidate — CANNOT fire against a broken guard: the sole-exact branch
    settles it and returns before the guard is ever consulted. Dropping the
    ``not agreeing`` condition scored ZERO against that pin, and only the
    mutation figure showed it.

    The reaching case is TWO exact agreements plus a recognised other basis.
    The amount cannot single one out, so the bucket is ambiguous; a guard
    that only asked ``len(on_basis) == 1`` would hand the payment to the
    other-basis invoice while two better candidates sat tied beside it.
    """
    report = match_snapshot(
        [_line(billed="300.00")],
        _snapshot(
            _invoice("INV-EXACT-A", amount="300.00"),
            _invoice("INV-EXACT-B", amount="300.00"),
            _invoice("INV-BASIS", amount="263.16"),  # 300 / 1.14
        ),
    )
    assert not report.proposals, (
        "two tied exact agreements must leave the bucket unresolved — the "
        "recognised-basis candidate must not win by being the only one of "
        "its kind"
    )
    assert len(report.ambiguous) == 1
    assert "INV-BASIS" in report.ambiguous[0].candidate_invoice_nos

    # POSITIVE CONTROL: with the tie broken, the exact agreement wins and the
    # recognised-basis candidate still does not.
    broken_tie = match_snapshot(
        [_line(billed="300.00")],
        _snapshot(
            _invoice("INV-EXACT-A", amount="300.00"),
            _invoice("INV-BASIS", amount="263.16"),
        ),
    )
    assert [p.invoice_no for p in broken_tie.proposals] == ["INV-EXACT-A"]


# --- gate-1 pre-registered criteria ------------------------------------------


def test_the_cannot_compare_population_stays_medium() -> None:
    """A KNIFE EDGE, pinned because nobody editing the disagree path is
    looking at it.

    A proposal resting on client and date alone adds nothing to the base, so
    it scores exactly WEIGHT_BUCKET — which happens to EQUAL BAND_MEDIUM_AT,
    and `_band` compares with `>=`. The whole cannot-compare population is
    therefore MEDIUM by the boundary and nothing else. Drop WEIGHT_BUCKET by
    a hundredth, or add any penalty to that path, and every one of them
    silently becomes LOW — collateral from a change aimed somewhere else
    entirely.

    Pinned as a BAND, not as a number, so the pin survives a deliberate
    re-tune that keeps the intent and fails a change that does not.
    """
    uncomparable = match_snapshot(
        [_line(billed=None)],
        _snapshot(_invoice("INV-1", amount="300.00")),
    ).proposals[0]
    assert uncomparable.amount_outcome == AMOUNT_UNCOMPARABLE
    assert uncomparable.band == BAND_MEDIUM, (
        "the cannot-compare population dropped out of MEDIUM. If that was "
        "intended, say so here; if it was collateral from a weight change "
        "aimed at the disagreement path, this is the pin catching it"
    )

    # The edge itself, stated so the next reader sees WHY this is fragile.
    # LITERALS on both sides. Written as _band(0.55)/_band(0.54) rather
    # than _band(WEIGHT_BUCKET)/_band(WEIGHT_BUCKET - 0.01) precisely so
    # that moving WEIGHT_BUCKET or BAND_MEDIUM_AT is DETECTED here instead
    # of being tracked silently.
    assert _band(0.55) == BAND_MEDIUM
    assert _band(0.54) == BAND_LOW, (
        "one hundredth below the base flips the whole population — that is "
        "the margin this pin is protecting"
    )


def test_a_recognised_class_is_never_a_match_claim() -> None:
    """Gate-explicit form of the ratified constraint: a recognised class must
    be distinguishable from AGREEMENT in BOTH score and words.

    x1.14 recognised is *a disagreement with an explanation*. It must never
    present as a match — not at the agreement score, not in the HIGH band,
    and not with a sentence a reader could mistake for one.
    """
    agree = match_snapshot(
        [_line(billed="300.00")], _snapshot(_invoice("INV-1", amount="300.00"))
    ).proposals[0]
    basis = match_snapshot(
        [_line(billed="342.00")], _snapshot(_invoice("INV-1", amount="300.00"))
    ).proposals[0]

    # Score: strictly between the disagree and agree paths, never equal to
    # the agreement score, never HIGH.
    assert -PENALTY_AMOUNT_DISAGREES < WEIGHT_AMOUNT_OTHER_BASIS
    assert WEIGHT_AMOUNT_OTHER_BASIS < WEIGHT_AMOUNT_AGREES
    assert basis.confidence < agree.confidence
    assert basis.band == BAND_MEDIUM and agree.band == BAND_HIGH
    assert basis.band != BAND_HIGH

    # Words: the basis line names WHICH class, not merely that some basis
    # applied — and it still carries the delta, because the figures did not
    # match and the report must not imply they did.
    sentence = " ".join(basis.basis)
    assert "1.14" in sentence and "HST" in sentence
    assert str(basis.amount_delta) in sentence
    assert basis.basis_class == BASIS_INCL_TAX

    # The two classes are distinguishable from EACH OTHER in words too.
    tax_line = match_snapshot(
        [_line(billed="42.00")], _snapshot(_invoice("INV-1", amount="300.00"))
    ).proposals[0]
    assert "tax-line" in " ".join(tax_line.basis)
    assert " ".join(tax_line.basis) != sentence


def test_the_unrecognised_outlier_lands_audibly() -> None:
    """Not merely 'not recognised' — SAID. A ratio that matched no class
    must announce that it matched none, or the operator cannot tell a
    considered miss from a check that never ran."""
    p = match_snapshot(
        [_line(billed="234.78")],
        _snapshot(_invoice("INV-1", amount="300.00")),
    ).proposals[0]

    assert p.amount_outcome == AMOUNT_DISAGREES
    assert p.basis_class == ""
    sentence = " ".join(p.basis)
    assert "no recognised basis class" in sentence, (
        "the miss must be audible — silence here is indistinguishable from a "
        "recogniser that never ran"
    )
    assert "how the next class gets found" in sentence
    assert str(p.amount_delta) in sentence


def test_the_tolerance_sits_inside_both_of_its_bounds() -> None:
    """The RELATIONSHIP, not the literal — a literal cannot notice that its
    own justification stopped being true.

    THE BUG THIS REPLACES, because it shipped: the first bound computed
    ``0.005/invoice + 0.005/ledger``, which is the RELATIVE error ``dr/r``,
    and compared it against an ABSOLUTE tolerance. That reads a factor of
    ``ratio`` too small — 0.00094 where the truth is 0.00107 — so the stated
    width was violated by the very worked example the docstring cited, and a
    genuine x1.14 member at the smallest amount would have been rejected for
    rounding alone. The failure was SAFE (the row stayed audibly
    unrecognised) and it was still a bound that lied.

    Both directions are asserted, because a one-directional bound is the
    original gap wearing a pin's clothes.
    """
    worst = worst_case_ratio_deviation(SMALLEST_INVOICE_AMOUNT)
    assert worst == Decimal("0.00107"), (
        "the worst-case rounding deviation moved; re-derive the bound rather "
        "than re-fitting the number to it"
    )

    # LOWER: strictly above the worst rounding error at the smallest amount
    # the data carries. At or below it, genuine members are rejected.
    assert RATIO_TOLERANCE > worst

    # UPPER: a 10x margin under the class separation. Bare `< separation` is
    # NOT enough — 0.015 satisfies that while sitting close enough to make
    # the classes neighbours, which is why the relation carries the margin.
    assert RATIO_TOLERANCE * 10 <= CLASS_SEPARATION

    # And the relation must actually be able to fail, in BOTH directions.
    too_tight = Decimal("0.0005")
    too_wide = Decimal("0.015")
    assert not (too_tight > worst), (
        "0.0005 must fail the LOWER relation — it rejects genuine members"
    )
    assert not (too_wide * 10 <= CLASS_SEPARATION), (
        "0.015 must fail the UPPER relation — bare '< 0.02' would pass it, "
        "which is exactly the gap the margin closes"
    )


def test_the_widening_admits_nothing_new_in_the_fixtures() -> None:
    """The tolerance fold is a PRODUCTION change — it widens what gets
    recognised — so its reach is measured rather than assumed.

    Every ratio these fixtures exercise is checked for whether it sits in
    the newly-admitted band between the old width and the new one. None
    does: the nearest non-member is 1.145 (deviation 0.005, still outside),
    and the 0.7826 outlier's nearest class centre is 0.84 (deviation
    0.0574). So the widening changes no classification here — it restores
    the members the wrong bound would have rejected at the small end.
    """
    old_width = Decimal("0.001")
    fixtures = [
        ("clean x1.14", Decimal("342.00"), Decimal("300.00")),
        ("small x1.14", Decimal("11.40"), Decimal("10.00")),
        ("cent low", Decimal("11.39"), Decimal("10.00")),
        ("cent high", Decimal("11.41"), Decimal("10.00")),
        ("tax line 0.14", Decimal("42.00"), Decimal("300.00")),
        ("tax line 0.28", Decimal("84.00"), Decimal("300.00")),
        ("just outside", Decimal("343.50"), Decimal("300.00")),
        ("the 0.7826 outlier", Decimal("234.78"), Decimal("300.00")),
        ("plain disagreement", Decimal("345.00"), Decimal("300.00")),
    ]
    newly_admitted = []
    for label, ledger, invoice in fixtures:
        ratio = ledger / invoice
        steps = (ratio / TAX_LINE_STEP).to_integral_value()
        deviation = min(
            abs(ratio - HST_RATIO),
            abs(ratio - steps * TAX_LINE_STEP) if steps >= 1 else Decimal("99"),
        )
        if old_width < deviation <= RATIO_TOLERANCE:
            newly_admitted.append((label, deviation))

    assert newly_admitted == [], (
        f"the widening changed classification for {newly_admitted} — that is "
        f"a real behaviour change and must be stated, not discovered"
    )

    # POSITIVE CONTROL: the band the check looks at is not empty by
    # construction — a ratio placed inside it IS detected.
    planted = HST_RATIO + Decimal("0.0015")
    assert old_width < abs(planted - HST_RATIO) <= RATIO_TOLERANCE, (
        "the newly-admitted band must be non-empty, or the assertion above "
        "is vacuous"
    )
