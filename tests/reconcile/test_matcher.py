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
    AMOUNT_UNCOMPARABLE,
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
    assert p.confidence == WEIGHT_BUCKET + WEIGHT_AMOUNT_AGREES
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
    assert p.confidence == (
        WEIGHT_BUCKET + WEIGHT_AMOUNT_AGREES - PENALTY_AMBIGUOUS_SURNAME
    )
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
    assert p.confidence == round(
        WEIGHT_BUCKET + WEIGHT_AMOUNT_AGREES + BOOST_CITATION_UNCONFIRMABLE, 4
    )
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
