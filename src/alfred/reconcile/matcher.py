"""The matcher — joining what was ANSWERED to what was ASKED.

The statement side says what the provider paid. The invoice side says what
was billed. This is the seam between them, and it is the only place in the
package that makes a JUDGEMENT about identity rather than reading a fact.

**The join, as ratified.** ``client + date_of_service`` finds the BUCKET;
``amount_excl_tax`` picks WITHIN it. RRTS's day-bucketing means one invoice
carries a client's legs for one day, so a claimant-and-day group of ledger
lines is the natural counterpart of one invoice — and the amount is
discriminator and confirmation in one: it chooses between same-day
candidates AND says the chosen one is right.

**Every match is a PROPOSAL.** Nothing here closes, marks paid, writes back
or transitions state. A proposal carries its BASIS (the reasons, in words)
and its CONFIDENCE (a number from named weights, and a band). Auto-close is
a later operator flip and is deliberately not reachable from this module.

**The weighting is a JUDGEMENT and lives in named constants**, not in
literals scattered through the scoring. A number that decides which
payments the operator sees should be findable, quotable and changeable in
one place — and reviewable without reading the algorithm.

Three things this module is deliberately careful about:

* **An ambiguous surname split never resolves itself.**
  :func:`~alfred.reconcile.names.surname_matches` reports ambiguity rather
  than picking a reading, and that ambiguity travels all the way to the
  proposal as a score penalty and a stated basis line. A confident wrong
  join — a payment attached to the wrong claimant — is worse than a missed
  one, and it is the specific harm a two-word surname produces.

* **The ``#NNN`` invoice citation is CORROBORATIVE ONLY, and unconfirmable.**
  See :data:`BOOST_CITATION_UNCONFIRMABLE` — the reasoning is long enough to
  live next to the constant.

* **A stale snapshot matches nothing.** The same promise the reader and the
  watchdog keep. Matching against a three-day-old export would propose that
  a payment belongs to an invoice that may have been superseded, and every
  downstream figure would inherit the error silently.

**The amount comparison has THREE outcomes, not two.** Agrees, disagrees,
and *cannot be compared* — a ledger line with no ``total_billed`` or an
invoice with no ``amount_excl_tax`` supports neither conclusion. Absent and
zero are different facts, and this package refuses to launder one into the
other anywhere else either.

**Amount disagreement does not veto a sole candidate.** Whether the
ledger's ``total_billed`` is on the same tax basis as RRTS's
``amount_excl_tax`` is NOT established by anything in this repo — it is a
semantic question about two external documents. So the matcher STATES the
delta rather than requiring agreement: if the two figures turn out to sit
on different bases, every proposal shows a consistent delta, which is a
legible finding on the first report. A matcher that required agreement
would instead propose nothing at all and look broken, which is the failure
mode that takes a week to diagnose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

import structlog

from .invoices import (
    STATUS_PAID,
    STATUS_SENT,
    Invoice,
    InvoiceSnapshot,
    parse_snapshot_date,
)
from .ledger import ClaimLine
from .names import normalise_for_compare, surname_matches

log = structlog.get_logger(__name__)

#: Statuses an invoice can be matched against. NOT the same set as
#: :data:`~alfred.reconcile.invoices.AGEABLE_STATUSES`, and the difference is
#: the point: a ``paid`` invoice cannot be LATE but is exactly the thing a
#: remittance line should match — it is how it became paid. A ``void``
#: expects no payment and a ``created`` one was never sent, so a payment
#: landing on either is not a match this layer should invent.
#:
#: Unknown statuses are excluded, fail-CLOSED, matching
#: :meth:`Invoice.expects_payment`. That leaves an unknown-status invoice
#: invisible to BOTH the matcher and the watchdog — which is precisely why
#: :attr:`InvoiceSnapshot.unknown_statuses` is surfaced as a count on the
#: report. The count is the safety valve for this exclusion; the two are one
#: design and must not be separated.
MATCHABLE_STATUSES = frozenset({STATUS_SENT, STATUS_PAID})

# --- the weighting: the judgement, in one findable place ---------------------

#: Both halves of the bucket agreed — the claimant's surname matched and the
#: invoice covers the date of service. This is the floor for candidacy: a
#: proposal cannot exist without it, so every proposal starts here.
WEIGHT_BUCKET = 0.55

#: ``amount_excl_tax`` reproduced the ledger's billed sum for the bucket.
#: The confirmation half of the ratified join.
WEIGHT_AMOUNT_AGREES = 0.35

# THE BASE WEIGHTS DELIBERATELY SUM TO 0.90, NOT 1.00, and the headroom is
# load-bearing rather than cosmetic. A clean client + date + amount agreement
# is very good evidence and it is still a HEURISTIC join: no identifier
# tied these two documents together, and claiming 1.00 for it would tell the
# operator the machine is certain when it is inferring.
#
# The consequence is deliberate and worth stating plainly: **full confidence
# is reachable only through a CONFIRMED crosswalk** — an identifier match
# RRTS vouched for — never through the heuristic alone.
#
# The first cut of this scoring had the two summing to 1.00, and the clamp at
# the bottom of _score_candidate then silently swallowed every citation boost
# on exactly the population where the join was cleanest. The boost was inert
# and the arithmetic still looked right; only running it showed the number
# never moved.

#: The snapshot's client name did not split into a surname unambiguously —
#: a bare three-token name is either a middle name or a two-word surname and
#: the string does not say which. The match may still be right; it rests on
#: a guess, and the score says so.
PENALTY_AMBIGUOUS_SURNAME = 0.20

#: Client and date agreed but the money did not. Kept as a proposal (see the
#: module docstring on the tax-basis question) at a much lower confidence,
#: with the delta stated.
PENALTY_AMOUNT_DISAGREES = 0.25

#: A ledger line citing ``Invoice #487`` whose number equals a candidate's
#: RRTS ``invoice_no``.
#:
#: SMALL, and deliberately so. Per the operator, the ``#NNN`` series is a
#: PROVIDER-QUOTED ACCOUNTING REFERENCE — QuickBooks/Wave numbering — which
#: is a DIFFERENT series from RRTS's own invoice numbers. So an equality
#: between them is two unrelated numbering schemes landing on the same
#: integer, and it CANNOT be confirmed against the export. It is worth a
#: nudge among candidates that already agree on client and date; it is not
#: worth the appearance of an identifier match, and the proposal's basis
#: says so in words rather than letting a reader assume the numbers are the
#: same kind of thing.
#:
#: Structurally the citation can only ever RE-WEIGHT a candidate that
#: already cleared client and date. It never creates one. That is enforced
#: in :func:`match_snapshot` and pinned, because a coincidence-prone signal
#: that could originate a match would be a confident wrong join generator.
BOOST_CITATION_UNCONFIRMABLE = 0.05

#: A citation resolved through the CROSSWALK — the future
#: :data:`ACCOUNTING_REF_FIELD` on the invoice side. Larger than the
#: unconfirmable boost because this one is an identifier match in the
#: ordinary sense: both sides are then quoting the same series, and RRTS
#: said so. Empty today; see :func:`build_accounting_index`.
BOOST_CITATION_CROSSWALK = 0.20

#: Score at or above which a proposal reads as HIGH confidence.
BAND_HIGH_AT = 0.85
#: Score at or above which a proposal reads as MEDIUM confidence.
BAND_MEDIUM_AT = 0.55

BAND_HIGH = "high"
BAND_MEDIUM = "medium"
BAND_LOW = "low"

#: The additive field RRTS may ship later, carrying the provider's own
#: accounting reference on the invoice side. Named ONCE, here, so that when
#: it lands the crosswalk needs a dataclass field and nothing else.
ACCOUNTING_REF_FIELD = "accounting_invoice_no"

#: Citation resolution states, carried on the proposal so a reader never has
#: to infer which kind of evidence was used.
CITATION_NONE = ""
CITATION_UNCONFIRMABLE = "unconfirmable"
CITATION_CROSSWALK = "crosswalk_confirmed"

#: The three outcomes of the amount comparison.
AMOUNT_AGREES = "agrees"
AMOUNT_DISAGREES = "disagrees"
AMOUNT_UNCOMPARABLE = "uncomparable"


def _band(score: float) -> str:
    if score >= BAND_HIGH_AT:
        return BAND_HIGH
    if score >= BAND_MEDIUM_AT:
        return BAND_MEDIUM
    return BAND_LOW


@dataclass
class LedgerBucket:
    """One claimant's claim lines for one date of service.

    The unit the matcher joins on, because it is the unit RRTS invoices on:
    their day-bucketing puts a client's legs for one day on one invoice.
    """

    surname: str = ""
    first_name: str = ""
    service_date: date | None = None
    lines: list[ClaimLine] = field(default_factory=list)

    @property
    def claimant(self) -> str:
        s = (self.surname or "").strip()
        f = (self.first_name or "").strip()
        return f"{s}, {f}" if s and f else (s or f)

    @property
    def line_keys(self) -> list[str]:
        return [line.key for line in self.lines]

    @property
    def billed(self) -> Decimal | None:
        """Sum of ``total_billed`` across the bucket, or ``None``.

        ``None`` when NO line in the bucket carries a billed figure — which
        is "we cannot compare", not "the bucket billed zero". Lines that
        individually lack the figure are skipped, and the count of those is
        reported on the proposal so a partial sum is never quietly compared
        as though it were complete.
        """
        present = [l.total_billed for l in self.lines if l.total_billed is not None]
        if not present:
            return None
        total = Decimal("0")
        for value in present:
            total += value
        return total

    @property
    def lines_without_billed(self) -> int:
        return sum(1 for l in self.lines if l.total_billed is None)

    @property
    def citations(self) -> list[str]:
        """The distinct ``#NNN`` references the bucket's lines quote."""
        seen: list[str] = []
        for line in self.lines:
            ref = (line.invoice_no or "").strip()
            if ref and ref not in seen:
                seen.append(ref)
        return seen


@dataclass
class MatchProposal:
    """One proposed join, with everything the operator needs to judge it."""

    invoice_no: str = ""
    claimant: str = ""
    client_name: str = ""
    service_date: str = ""
    line_keys: list[str] = field(default_factory=list)
    ledger_billed: Decimal | None = None
    invoice_amount_excl_tax: Decimal | None = None
    #: Ledger sum minus invoice amount. ``None`` when not comparable.
    amount_delta: Decimal | None = None
    amount_outcome: str = AMOUNT_UNCOMPARABLE
    confidence: float = 0.0
    band: str = BAND_LOW
    #: The reasons, in words. Every score component contributes one line, so
    #: the number and the sentence can never drift apart.
    basis: list[str] = field(default_factory=list)
    ambiguous_name: bool = False
    citation: str = ""
    citation_status: str = CITATION_NONE


@dataclass
class AmbiguousBucket:
    """A bucket the matcher REFUSED to resolve, and why.

    Reported rather than dropped. A bucket the matcher could not settle is a
    finding about the data — two candidate invoices the amount cannot tell
    apart is exactly the case where a silent pick would attach money to the
    wrong invoice.
    """

    claimant: str = ""
    service_date: str = ""
    reason: str = ""
    candidate_invoice_nos: list[str] = field(default_factory=list)


@dataclass
class MatchReport:
    """What the matcher proposed, refused, and could not reach."""

    proposals: list[MatchProposal] = field(default_factory=list)
    ambiguous: list[AmbiguousBucket] = field(default_factory=list)
    #: Buckets with no candidate invoice at all — the ledger says a payment
    #: happened and the export has nothing for that client on that day.
    unmatched: list[AmbiguousBucket] = field(default_factory=list)
    #: Claim lines that could not be bucketed: no surname, or no readable
    #: date of service. Counted, never silently skipped.
    unbucketable: list[str] = field(default_factory=list)
    stale_snapshot: bool = False
    invoices_considered: int = 0
    buckets_examined: int = 0

    @property
    def matched_invoice_nos(self) -> set[str]:
        """The set :func:`alfred.reconcile.aging.find_late` consumes.

        Only PROPOSED invoices. An ambiguous bucket contributes nothing —
        its candidates are still outstanding as far as the watchdog is
        concerned, which is the safe direction: an invoice wrongly treated
        as matched would drop off the chase list silently.
        """
        return {p.invoice_no for p in self.proposals if p.invoice_no}

    def summary(self) -> str:
        """One line, never empty — the ILB rule."""
        if self.stale_snapshot:
            return (
                "Matching NOT run: the invoice export is stale, so no payment "
                "was joined to any invoice. A join made against old data can "
                "attach money to an invoice that has since changed."
            )
        if not self.buckets_examined:
            return (
                "No claimant/date groups in the ledger to match — the matcher "
                "ran and had nothing to join. Seed a payment summary first."
            )
        parts = [f"{self.buckets_examined} claimant/date group(s) examined "
                 f"against {self.invoices_considered} matchable invoice(s)"]
        parts.append(
            f"{len(self.proposals)} proposal(s)" if self.proposals
            else "no proposal met the join — nothing was matched"
        )
        if self.ambiguous:
            parts.append(f"{len(self.ambiguous)} left ambiguous")
        if self.unmatched:
            parts.append(f"{len(self.unmatched)} with no candidate invoice")
        if self.unbucketable:
            parts.append(f"{len(self.unbucketable)} line(s) could not be grouped")
        return ", ".join(parts) + "."


def build_buckets(claim_lines: list[ClaimLine]) -> tuple[
    list[LedgerBucket], list[str]
]:
    """Group claim lines into ``(claimant, date-of-service)`` buckets.

    Returns ``(buckets, unbucketable_line_keys)``. A line with no surname or
    no readable ``dos`` cannot be joined to anything and is returned in the
    second list rather than dropped — "could not be grouped" is a finding
    about the ledger, not a reason for silence.

    The grouping key carries BOTH names. Grouping on surname alone would
    merge two different people who share one on the same day, and the ledger
    holds the first name already — throwing it away at grouping time to
    re-guess it later is the second-spelling shape this package avoids
    everywhere else.
    """
    buckets: dict[tuple[str, str, str], LedgerBucket] = {}
    unbucketable: list[str] = []
    for line in claim_lines:
        surname = (line.surname or "").strip()
        parsed = parse_snapshot_date(line.dos)
        if not surname or parsed is None:
            unbucketable.append(line.key)
            continue
        key = (
            normalise_for_compare(surname),
            normalise_for_compare(line.first_name),
            parsed.isoformat(),
        )
        bucket = buckets.get(key)
        if bucket is None:
            bucket = LedgerBucket(
                surname=surname,
                first_name=(line.first_name or "").strip(),
                service_date=parsed,
            )
            buckets[key] = bucket
        bucket.lines.append(line)
    ordered = sorted(
        buckets.values(),
        key=lambda b: (b.service_date or date.min, b.surname, b.first_name),
    )
    return ordered, unbucketable


def matchable_invoices(snapshot: InvoiceSnapshot) -> list[Invoice]:
    """Invoices a payment can legitimately be joined to.

    Junk rows are already gone at load; this applies
    :data:`MATCHABLE_STATUSES` on top.
    """
    return [
        inv for inv in snapshot.invoices
        if not inv.is_junk
        and (inv.status or "").strip().lower() in MATCHABLE_STATUSES
    ]


def _service_dates(invoice: Invoice) -> set[date]:
    """The distinct service dates an invoice covers, from its line items."""
    out: set[date] = set()
    for item in invoice.line_items:
        parsed = parse_snapshot_date(item.date_of_service)
        if parsed is not None:
            out.add(parsed)
    return out


def index_by_service_date(
    invoices: list[Invoice],
) -> dict[date, list[Invoice]]:
    """``{service date: invoices covering it}``.

    The date is an EXACT key and the surname is a judgement, so the index is
    built on the date and :func:`~alfred.reconcile.names.surname_matches`
    runs over the small per-date candidate set. That keeps the name rule in
    one authority instead of growing a second, index-shaped copy of it.
    """
    index: dict[date, list[Invoice]] = {}
    for invoice in invoices:
        for day in _service_dates(invoice):
            index.setdefault(day, []).append(invoice)
    return index


def build_accounting_index(
    invoices: list[Invoice],
) -> dict[str, list[Invoice]]:
    """``{provider accounting reference: invoices}`` — the CROSSWALK seam.

    EMPTY today, by construction and not by accident: RRTS's export carries
    no accounting reference yet, and :meth:`Invoice.from_dict` drops unknown
    keys per the schema-tolerance contract, so the attribute does not exist
    on a loaded invoice.

    It is built here anyway, through :data:`ACCOUNTING_REF_FIELD` and a
    ``getattr``, so that the day RRTS ships the field the crosswalk needs
    ONE dataclass field added to :class:`~alfred.reconcile.invoices.Invoice`
    and nothing else — no new lookup, no new call site, no re-reasoning
    about the citation's weight. The alternative was to write this when the
    field arrives, which is the same "one END exists so one resolution
    exists" shape the export-path derivation was lifted out of.
    """
    index: dict[str, list[Invoice]] = {}
    for invoice in invoices:
        ref = str(getattr(invoice, ACCOUNTING_REF_FIELD, "") or "").strip()
        if ref:
            index.setdefault(ref, []).append(invoice)
    return index


def _compare_amounts(
    bucket_billed: Decimal | None, invoice_amount: Decimal | None
) -> tuple[str, Decimal | None]:
    """``(outcome, delta)`` — three outcomes, never two.

    ``None`` on either side yields :data:`AMOUNT_UNCOMPARABLE` with no
    delta. Treating an absent figure as a zero that disagrees would
    manufacture a finding out of a missing cell.
    """
    if bucket_billed is None or invoice_amount is None:
        return AMOUNT_UNCOMPARABLE, None
    delta = bucket_billed - invoice_amount
    if delta == 0:
        return AMOUNT_AGREES, delta
    return AMOUNT_DISAGREES, delta


def _score_candidate(
    bucket: LedgerBucket,
    invoice: Invoice,
    *,
    ambiguous_name: bool,
    citation_status: str,
) -> MatchProposal:
    """Build the scored proposal for one (bucket, invoice) pair.

    Every score component appends its own basis line, so the number and the
    words are produced together and cannot drift.
    """
    billed = bucket.billed
    outcome, delta = _compare_amounts(billed, invoice.amount_excl_tax)

    score = WEIGHT_BUCKET
    basis = [
        f"client surname and date of service both agree "
        f"({bucket.claimant} on {bucket.service_date.isoformat() if bucket.service_date else '(no date)'})"
    ]

    if outcome == AMOUNT_AGREES:
        score += WEIGHT_AMOUNT_AGREES
        basis.append(
            f"amount_excl_tax reproduces the ledger's billed sum for this "
            f"day ({billed})"
        )
    elif outcome == AMOUNT_DISAGREES:
        score -= PENALTY_AMOUNT_DISAGREES
        basis.append(
            f"amounts DISAGREE: ledger billed {billed}, invoice "
            f"amount_excl_tax {invoice.amount_excl_tax} (delta {delta}). The "
            f"client and date still agree, so this is proposed rather than "
            f"discarded — but the money is the confirmation, and it did not "
            f"confirm."
        )
    else:
        basis.append(
            "the amounts could not be compared: one side carries no figure. "
            "That is not agreement and not disagreement — it is a missing "
            "cell, and this proposal rests on client and date alone."
        )

    if bucket.lines_without_billed:
        basis.append(
            f"{bucket.lines_without_billed} of {len(bucket.lines)} ledger "
            f"line(s) in this group carry no billed figure, so the sum above "
            f"is partial"
        )

    if ambiguous_name:
        score -= PENALTY_AMBIGUOUS_SURNAME
        basis.append(
            f"the snapshot's client name {invoice.client_name!r} did not "
            f"split into a surname unambiguously — the match rests on a "
            f"guess about which token is the surname, and a wrong split "
            f"attaches this payment to the wrong person"
        )

    citation = bucket.citations[0] if bucket.citations else ""
    if citation_status == CITATION_CROSSWALK:
        score += BOOST_CITATION_CROSSWALK
        basis.append(
            f"the ledger cites Invoice #{citation} and the invoice carries "
            f"the same accounting reference — a confirmed crosswalk"
        )
    elif citation_status == CITATION_UNCONFIRMABLE:
        score += BOOST_CITATION_UNCONFIRMABLE
        basis.append(
            f"the ledger cites Invoice #{citation}, which equals this "
            f"invoice's RRTS number — but the cited series is the PROVIDER'S "
            f"OWN ACCOUNTING REFERENCE, a different numbering scheme from "
            f"RRTS's. This agreement CANNOT be confirmed against the export "
            f"and is treated as corroboration only, never as an identifier "
            f"match."
        )

    score = max(0.0, min(1.0, score))
    return MatchProposal(
        invoice_no=invoice.invoice_no,
        claimant=bucket.claimant,
        client_name=invoice.client_name,
        service_date=(
            bucket.service_date.isoformat() if bucket.service_date else ""
        ),
        line_keys=bucket.line_keys,
        ledger_billed=billed,
        invoice_amount_excl_tax=invoice.amount_excl_tax,
        amount_delta=delta,
        amount_outcome=outcome,
        confidence=round(score, 4),
        band=_band(score),
        basis=basis,
        ambiguous_name=ambiguous_name,
        citation=citation,
        citation_status=citation_status,
    )


def _citation_status(
    bucket: LedgerBucket,
    invoice: Invoice,
    accounting_index: dict[str, list[Invoice]],
) -> str:
    """How (if at all) the bucket's citation corroborates this invoice.

    The citation NEVER creates a candidate — this is only ever asked about
    an invoice that already cleared client and date. See
    :data:`BOOST_CITATION_UNCONFIRMABLE`.
    """
    for citation in bucket.citations:
        if invoice in accounting_index.get(citation, []):
            return CITATION_CROSSWALK
        if citation and citation == (invoice.invoice_no or "").strip():
            return CITATION_UNCONFIRMABLE
    return CITATION_NONE


def match_snapshot(
    claim_lines: list[ClaimLine],
    snapshot: InvoiceSnapshot,
) -> MatchReport:
    """Propose joins between ledger claim lines and snapshot invoices.

    Propose-only. The returned :attr:`MatchReport.matched_invoice_nos` is
    what :func:`alfred.reconcile.aging.find_late` consumes so a proposed
    invoice stops being chased.
    """
    report = MatchReport()

    if snapshot.is_stale:
        report.stale_snapshot = True
        log.warning(
            "reconcile.matcher.skipped_stale",
            exported_at=snapshot.exported_at or "(none)",
            detail="the invoice export is stale — nothing was matched "
                   "against it. A join made from old data can attach a "
                   "payment to an invoice that has since changed.",
        )
        return report

    invoices = matchable_invoices(snapshot)
    report.invoices_considered = len(invoices)
    by_date = index_by_service_date(invoices)
    accounting_index = build_accounting_index(invoices)

    buckets, unbucketable = build_buckets(claim_lines)
    report.unbucketable = unbucketable
    report.buckets_examined = len(buckets)

    for bucket in buckets:
        label_date = (
            bucket.service_date.isoformat() if bucket.service_date else ""
        )
        same_day = by_date.get(bucket.service_date or date.min, [])

        candidates: list[MatchProposal] = []
        for invoice in same_day:
            matched, ambiguous = surname_matches(
                bucket.surname, invoice.client_name
            )
            if not matched:
                continue
            candidates.append(_score_candidate(
                bucket,
                invoice,
                ambiguous_name=ambiguous,
                citation_status=_citation_status(
                    bucket, invoice, accounting_index
                ),
            ))

        if not candidates:
            report.unmatched.append(AmbiguousBucket(
                claimant=bucket.claimant,
                service_date=label_date,
                reason=(
                    "no invoice in the export names this client with a line "
                    "item on this date. The ledger says a claim was "
                    "processed; the invoice side has nothing to join it to."
                ),
            ))
            continue

        if len(candidates) == 1:
            report.proposals.append(candidates[0])
            continue

        # Several same-day candidates for one client: the AMOUNT is the
        # discriminator, which is the half of the ratified join that exists
        # for exactly this case.
        agreeing = [c for c in candidates if c.amount_outcome == AMOUNT_AGREES]
        if len(agreeing) == 1:
            report.proposals.append(agreeing[0])
            continue

        report.ambiguous.append(AmbiguousBucket(
            claimant=bucket.claimant,
            service_date=label_date,
            reason=(
                f"{len(candidates)} invoices name this client on this date "
                f"and the amount could not single one out "
                f"({len(agreeing)} reproduce the ledger's billed sum). "
                f"Left unresolved rather than picked: attaching this payment "
                f"to the wrong same-day invoice is not recoverable from the "
                f"report."
            ),
            candidate_invoice_nos=[c.invoice_no for c in candidates],
        ))

    _demote_contested(report)

    if report.unbucketable:
        log.warning(
            "reconcile.matcher.unbucketable",
            count=len(report.unbucketable),
            detail="claim lines with no surname or no readable date of "
                   "service — they cannot be joined to any invoice and are "
                   "reported rather than dropped",
        )
    log.info(
        "reconcile.matcher.matched",
        buckets=report.buckets_examined,
        invoices=report.invoices_considered,
        proposals=len(report.proposals),
        ambiguous=len(report.ambiguous),
        unmatched=len(report.unmatched),
        unbucketable=len(report.unbucketable),
        detail=(
            "no bucket produced a proposal — the matcher ran and joined "
            "nothing"
            if report.buckets_examined and not report.proposals else ""
        ),
    )
    return report


def _demote_contested(report: MatchReport) -> None:
    """Keep at most ONE proposal per invoice; demote the rest.

    An invoice whose line items span two dates can be a candidate for two
    different buckets of the same client, and proposing it twice would tell
    the operator one invoice settled two days' claims. The highest-scoring
    proposal is kept; the others become ambiguous with the reason stated. A
    TIE keeps none of them — picking between equally-scored proposals would
    be the arbitrary choice this module exists to refuse.
    """
    by_invoice: dict[str, list[MatchProposal]] = {}
    for proposal in report.proposals:
        by_invoice.setdefault(proposal.invoice_no, []).append(proposal)

    kept: list[MatchProposal] = []
    for invoice_no, group in by_invoice.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        best = max(c.confidence for c in group)
        winners = [c for c in group if c.confidence == best]
        losers = [c for c in group if c not in winners]
        if len(winners) == 1:
            kept.append(winners[0])
        else:
            losers = group
        for loser in losers:
            report.ambiguous.append(AmbiguousBucket(
                claimant=loser.claimant,
                service_date=loser.service_date,
                reason=(
                    f"invoice {invoice_no} is a candidate for "
                    f"{len(group)} different claimant/date groups; "
                    + (
                        "another group scored higher and holds it"
                        if len(winners) == 1 else
                        "they scored equally, so none of them holds it — an "
                        "arbitrary pick here would attach one day's payment "
                        "to another day's invoice"
                    )
                ),
                candidate_invoice_nos=[invoice_no],
            ))
    if len(kept) != len(report.proposals):
        log.warning(
            "reconcile.matcher.contested_invoices",
            demoted=len(report.proposals) - len(kept),
            detail="one invoice was proposed against more than one "
                   "claimant/date group; the surplus proposals were demoted "
                   "to ambiguous rather than double-counting the invoice",
        )
    report.proposals = sorted(
        kept, key=lambda p: (p.service_date, p.claimant, p.invoice_no)
    )


__all__ = [
    "ACCOUNTING_REF_FIELD",
    "AMOUNT_AGREES",
    "AMOUNT_DISAGREES",
    "AMOUNT_UNCOMPARABLE",
    "BAND_HIGH",
    "BAND_HIGH_AT",
    "BAND_LOW",
    "BAND_MEDIUM",
    "BAND_MEDIUM_AT",
    "BOOST_CITATION_CROSSWALK",
    "BOOST_CITATION_UNCONFIRMABLE",
    "CITATION_CROSSWALK",
    "CITATION_NONE",
    "CITATION_UNCONFIRMABLE",
    "MATCHABLE_STATUSES",
    "PENALTY_AMBIGUOUS_SURNAME",
    "PENALTY_AMOUNT_DISAGREES",
    "WEIGHT_AMOUNT_AGREES",
    "WEIGHT_BUCKET",
    "AmbiguousBucket",
    "LedgerBucket",
    "MatchProposal",
    "MatchReport",
    "build_accounting_index",
    "build_buckets",
    "index_by_service_date",
    "match_snapshot",
    "matchable_invoices",
]
