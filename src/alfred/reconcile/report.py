"""The backlog bulk-review report — ONE artifact, CSV plus a summary.

The ratified handling for the Feb-Jul backlog is a single bulk-review
report, NOT four hundred proposal cards; cards are go-forward only. This
module is that report.

It has two halves and they answer different questions. The **CSV** is the
working surface — every line, its classification, and enough provenance to
find the row in the source note; it is what the operator sorts and filters
while working through the backlog. The **summary** is the orientation —
per-class counts, per-statement totals, and the cross-foot — read once at
the top to know the shape of what is in the CSV.

**The cross-foot is the report's only independent check.** Our per-statement
sum of ``Amount Paid`` is compared against two things the PROVIDER wrote:
the statement's own payment total, and the per-claimant SUB-TOTAL rows
(which is why the parser keeps them instead of discarding them). Agreement
means the parse read the numbers the statement actually contains.
Disagreement is a finding and is stated in cents — it is the difference
between "the loop is working" and "the loop is confidently wrong", and no
amount of internal consistency can substitute for it.

**Absence is always stated.** An empty ledger, a statement with no flagged
lines, a report with no discrepancies — each says so explicitly. A report
that renders nothing when it finds nothing is indistinguishable from a
report that failed to run.

**Delivery is a SEAM, not code here** (ratified 2026-08-12). Both halves are
written as files into the instance's own reports directory and nothing in
this package sends them anywhere. The intended read path adds no surface at
all: the operator asks the instance to show him the report, the talker reads
the file, and the web surface's existing fenced-text rendering supplies the
download affordance. That seam is deliberately not built here — pushing a
file outward is an auth decision, and this module's job ends at producing
the artifact. There is NO Python CSV-export path in the tree to ride; the
only CSV code that exists is inbound (``telegram.attachments`` decoding an
UPLOADED CSV into a Markdown table for a prompt), which is the opposite
direction and shares nothing with this.
"""

from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from .aging import LATE_AFTER_DAYS, AgingReport, find_late
from .attention import (
    CLASS_LABELS,
    CLASS_UNKNOWN_EOB,
    Classification,
    Correction,
    ProposedMapping,
    class_counts,
    classify_all,
    propose_eob_mappings,
)
from .invoices import STALE_AFTER_HOURS, InvoiceSnapshot
from .ledger import ClaimLine, LedgerContents, Statement, group_by_statement
from .matcher import BAND_HIGH, MatchReport, match_snapshot
from .parser import DATE_SOURCE_CAPTURE
from .money import format_money

log = structlog.get_logger(__name__)

_ZERO = Decimal("0")

#: CSV column order. Provenance columns come last so the working columns
#: are the ones visible without scrolling.
CSV_COLUMNS: tuple[str, ...] = (
    "statement_date",
    "claim_no",
    "date_of_service",
    "claimant",
    "benefit_code",
    "units",
    "total_billed",
    "amt_excluded",
    "deduct",
    "amt_eligible",
    "pct_pd",
    "amount_paid",
    "eob_code",
    "invoice_no",
    "attention_classes",
    "attention_reasons",
    "classification_source",
    "comments",
    "inferred",
    "source_note",
    "source_line",
    "line_key",
)


def _money_cell(value: Decimal | None) -> str:
    """Money for a CSV: plain, unformatted, so a spreadsheet reads it as a
    number. ``None`` is an EMPTY cell, never ``0.00`` — absent and zero are
    different facts and a spreadsheet will happily sum the difference."""
    return "" if value is None else str(value.quantize(Decimal("0.01")))


def build_csv(
    lines: list[ClaimLine],
    classifications: dict[str, Classification],
) -> str:
    """The CSV half. One row per claim line, classification included."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for line in lines:
        cls = classifications.get(line.key)
        writer.writerow([
            line.statement_date,
            line.claim_no,
            line.dos,
            line.claimant,
            line.benefit_code,
            "" if line.units is None else str(line.units),
            _money_cell(line.total_billed),
            _money_cell(line.amt_excluded),
            _money_cell(line.deduct),
            _money_cell(line.amt_eligible),
            "" if line.pct_paid is None else str(line.pct_paid),
            _money_cell(line.amount_paid),
            line.eob_code,
            line.invoice_no,
            " ".join(cls.classes) if cls else "",
            " | ".join(cls.reasons) if cls else "",
            cls.source if cls else "",
            line.comments,
            "yes" if line.inferred else "no",
            line.source_note,
            str(line.source_line),
            line.key,
        ])
    return buf.getvalue()


@dataclass
class StatementTotals:
    """One statement's arithmetic, ours beside the provider's."""

    statement: Statement
    line_count: int = 0
    billed: Decimal = _ZERO
    paid: Decimal = _ZERO
    flagged: int = 0
    #: Our sum of claim-line ``amount_paid`` minus the statement's own
    #: declared payment total. ``None`` when the statement declared none.
    payment_total_delta: Decimal | None = None
    #: Per-claimant subtotal comparisons that did NOT agree.
    subtotal_mismatches: list[str] = field(default_factory=list)
    #: Claimants whose subtotal agreed — counted so agreement is visible
    #: rather than merely implied by the absence of a mismatch.
    subtotals_checked: int = 0
    #: Labels from the statement's two-column totals block whose figure
    #: equals our computed paid sum. Reported rather than acted on: it tells
    #: the operator which declared figure our arithmetic reproduces, without
    #: this layer deciding that the matching one is therefore "the" payment
    #: total. That call is his, and it needs the provider's semantics.
    declared_matching_paid: list[str] = field(default_factory=list)
    #: The statement's own declared claim-line count against what the ledger
    #: actually holds for it, when they disagree. A free self-check: both
    #: numbers are already stored, and on the real note this one alone would
    #: have caught a wrong same-day fold (declared 24, held 41) without any
    #: reference to the provider's money figures.
    line_count_mismatch: tuple[int, int] | None = None
    #: Aggregate rows naming no claimant that were NOT the grand total.
    #: Recorded rather than alarmed about — see
    #: :func:`compute_statement_totals`. Informational, so deliberately not
    #: part of :func:`_is_discrepant`.
    unattributed_aggregates: list[str] = field(default_factory=list)
    #: Claim lines sitting under a statement with NO date. Its own finding
    #: class, because an undated pool is not a cross-foot discrepancy — it
    #: is rows that never reached a statement to be checked against, and
    #: the cross-foot cannot see what was never filed. On the real note a
    #: pool of 175 lines (~45% of the note) sat undated with no declared
    #: total, and every dated statement's figures were collateral.
    undated_lines: int = 0


def _is_discrepant(totals: "StatementTotals") -> bool:
    """Whether a statement has ANY cross-foot finding.

    ONE predicate, consulted by the summary, the has_discrepancies flag and
    the log line. Three copies of this comparison is how a new finding class
    gets counted in one place and missed in the other two — the same drift
    the health-status doctrine names.
    """
    return bool(
        (totals.payment_total_delta not in (None, _ZERO))
        or totals.subtotal_mismatches
        or totals.line_count_mismatch
        or totals.undated_lines
    )


def _claimant_paid_sums(lines: list[ClaimLine]) -> dict[str, Decimal]:
    sums: dict[str, Decimal] = {}
    for line in lines:
        if line.amount_paid is None:
            continue
        sums[line.claimant] = sums.get(line.claimant, _ZERO) + line.amount_paid
    return sums


def compute_statement_totals(
    statement: Statement,
    claim_lines: list[ClaimLine],
    subtotals: list[ClaimLine],
    classifications: dict[str, Classification],
) -> StatementTotals:
    """Fold one statement, cross-footing against the provider's own figures."""
    totals = StatementTotals(statement=statement, line_count=len(claim_lines))
    for line in claim_lines:
        if line.total_billed is not None:
            totals.billed += line.total_billed
        if line.amount_paid is not None:
            totals.paid += line.amount_paid
        cls = classifications.get(line.key)
        if cls is not None and cls.needs_attention:
            totals.flagged += 1

    if statement.payment_total is not None:
        totals.payment_total_delta = totals.paid - statement.payment_total

    if not statement.statement_date and claim_lines:
        # An undated statement holding rows is a POOL: lines the parser
        # could not attribute to any dated statement. Counted here so it
        # can never be silent again — the cross-foot only checks rows that
        # reached a statement, so a pool is invisible to every other check
        # in this function by construction.
        totals.undated_lines = len(claim_lines)

    if (
        statement.claim_line_count
        and statement.claim_line_count != len(claim_lines)
    ):
        totals.line_count_mismatch = (
            statement.claim_line_count, len(claim_lines)
        )

    totals.declared_matching_paid = [
        label
        for label, value in sorted(statement.declared_totals_decimal().items())
        if value == totals.paid
    ]

    ours = _claimant_paid_sums(claim_lines)
    unattributable: list[ClaimLine] = []

    for sub in subtotals:
        if sub.amount_paid is None:
            continue
        claimant = sub.claimant
        # Match the aggregate to a claimant where possible: exact label
        # first, then a single unambiguous surname match.
        #
        # An EMPTY claimant never matches. Aggregate rows carry no name, and
        # so do claim lines on statements that omit the name columns — so a
        # bare `ours.get("")` pairs an aggregate with "the claim lines that
        # also named nobody" and calls that an attribution. It is not one;
        # it is two blanks agreeing, and it routes a grand total into the
        # per-claimant check where it is guaranteed to mismatch.
        our_value = ours.get(claimant) if claimant else None
        if our_value is None:
            candidates = [
                v for k, v in ours.items()
                if sub.surname and sub.surname.strip().lower() in k.lower()
            ]
            our_value = candidates[0] if len(candidates) == 1 else None

        if our_value is None:
            # Names nobody — deferred to the pass below rather than compared
            # against the statement total here. A statement carries SEVERAL
            # unattributable aggregates (per-claimant figures whose id cell
            # is a word, plus one grand total), and comparing each of them
            # against the statement sum reports every non-grand-total row as
            # a mismatch. That is crying wolf, and a cross-foot that cries
            # wolf is one the operator stops reading — which costs more than
            # the check is worth.
            unattributable.append(sub)
            continue

        totals.subtotals_checked += 1
        label = f"{claimant or sub.surname} (line {sub.source_line})"
        if our_value != sub.amount_paid:
            totals.subtotal_mismatches.append(
                f"{label}: ours {format_money(our_value)} vs provider "
                f"{format_money(sub.amount_paid)} "
                f"(delta {format_money(our_value - sub.amount_paid)})"
            )

    if unattributable:
        # Among rows that name no claimant, the GRAND TOTAL is the one that
        # should equal our statement sum. If one does, the check passes and
        # the rest are per-claimant figures we simply could not attribute —
        # recorded, not alarmed about. If NONE does, that is a real finding:
        # the statement's own total does not reproduce our arithmetic.
        matching = [s for s in unattributable if s.amount_paid == totals.paid]
        totals.subtotals_checked += 1
        totals.unattributed_aggregates = [
            f"{format_money(s.amount_paid)} (line {s.source_line})"
            for s in unattributable if s not in matching
        ]
        if not matching:
            amounts = ", ".join(
                format_money(s.amount_paid) for s in unattributable
            )
            totals.subtotal_mismatches.append(
                f"no statement-level aggregate equals our sum "
                f"{format_money(totals.paid)} — the unattributed aggregate "
                f"rows are {amounts}"
            )
    return totals


@dataclass
class BacklogReport:
    """The whole artifact, in memory."""

    csv_text: str = ""
    summary_text: str = ""
    class_counts: dict[str, int] = field(default_factory=dict)
    statement_totals: list[StatementTotals] = field(default_factory=list)
    flagged_lines: int = 0
    total_lines: int = 0
    proposals: list[ProposedMapping] = field(default_factory=list)
    #: The invoice side, when an export was available. ``None`` means the
    #: caller passed no snapshot at all — a DIFFERENT state from "the export
    #: is missing from disk", which is an :class:`InvoiceSnapshot` with
    #: ``absent=True``. The report distinguishes them because they need
    #: different actions: one is unconfigured, the other is a feed that
    #: stopped arriving.
    snapshot: InvoiceSnapshot | None = None
    match: MatchReport | None = None
    aging: AgingReport | None = None

    @property
    def has_discrepancies(self) -> bool:
        return any(_is_discrepant(t) for t in self.statement_totals)


def _invoice_side_sections(
    snapshot: InvoiceSnapshot | None,
    match: MatchReport | None,
    aging: AgingReport | None,
) -> list[str]:
    """The invoice side: the feed's state, the late list, the proposed joins.

    **Rendered ABOVE the empty-ledger early return, deliberately.** These are
    OBSERVATIONS ABOUT THE INVOICE FEED, and the feed's state does not depend
    on whether anything has been seeded into the ledger — an instance with an
    empty ledger and a three-day-old export needs to be told about the export.
    Placing them after the early return would silence exactly the case where
    the signal matters most, which is the same trap the surveyor's
    ``no_changed_clusters`` early-return already documents.
    """
    out: list[str] = []
    out.append("## Invoice side")
    out.append("")

    if snapshot is None:
        # ILB, and distinct from "the export is missing". No invoice side was
        # supplied at all, so nothing here was even attempted.
        out.append(
            "_No invoice export was read for this report, so nothing on this "
            "page can say what was ASKED — only what the provider answered. "
            "Late-invoice detection and payment matching are both unavailable "
            "until `reconcile.invoices_path` resolves to RRTS's export._"
        )
        out.append("")
        return out

    out.append(snapshot.summary())
    out.append("")

    if snapshot.unknown_statuses:
        # The count line the loader's aggregation exists for. A status that
        # only ever appears in a log line stays unknown for a month, because
        # nobody greps for a thing they do not know exists. It is also the
        # SAFETY VALVE for two deliberate fail-closed exclusions: an
        # unknown-status invoice is matched by nothing and aged by nothing,
        # so this line is the only place it can surface at all.
        total = sum(snapshot.unknown_statuses.values())
        out.append(
            f"**{total} invoice(s) carry a status this build does not "
            f"recognise.** They are neither chased nor matched — both gates "
            f"fail closed on an unknown status rather than inventing a "
            f"meaning for it — so this line is the only place they appear. "
            f"Teach the status or check the export:"
        )
        for status, n in sorted(snapshot.unknown_statuses.items()):
            out.append(f"- `{status}`: {n}")
        out.append("")
    else:
        out.append(
            "_Every invoice status in the export is one this build "
            "recognises — the check ran and found nothing unknown._"
        )
        out.append("")

    if snapshot.is_stale:
        out.append(
            f"> **The export is STALE** (the agreed window is "
            f"{STALE_AFTER_HOURS}h). Nothing was matched or aged against it: "
            f"a chase computed from old data sends you after money that may "
            f"already have arrived, and a join made from it can attach a "
            f"payment to an invoice that has since changed."
        )
        out.append("")
        return out

    out.append("## Late invoices")
    out.append("")
    if aging is None:
        out.append(
            "_The aging watchdog did not run for this report._"
        )
    elif not aging.examined:
        out.append(
            "_No chaseable invoice in the export — there is nothing to age. "
            "The watchdog ran and the loop has nothing to chase._"
        )
    elif not aging.late:
        # The ILB line the watchdog exists to be able to say.
        out.append(
            f"_**No invoice is late — the loop has nothing to chase.** All "
            f"{aging.examined} chaseable invoice(s) are inside the "
            f"{LATE_AFTER_DAYS}-day window. This is the watchdog reporting a "
            f"clean result, not an empty section._"
        )
    else:
        out.append(
            f"{len(aging.late)} invoice(s) are more than {LATE_AFTER_DAYS} "
            f"days past the date they were sent, with no payment matched "
            f"against them. Oldest first."
        )
        out.append("")
        out.append("| Invoice | Client | Amount | Sent | Days | Basis |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for entry in aging.late:
            basis = (
                "**invoice_date (weaker)**" if entry.basis_is_weak
                else "date_sent"
            )
            out.append(
                f"| {entry.invoice_no} "
                f"| {entry.client_name or '(unnamed)'} "
                f"| {format_money(entry.amount_excl_tax)} "
                f"| {entry.since} "
                f"| {entry.days_outstanding} "
                f"| {basis} |"
            )
        if any(e.basis_is_weak for e in aging.late):
            out.append("")
            out.append(
                "_A **weaker** basis means the invoice carried no `date_sent` "
                "and its clock was started from `invoice_date` instead, per "
                "RRTS's own rider. It is a real clock on a weaker fact — "
                "worth confirming before making the call._"
            )
    if aging is not None and aging.undateable:
        out.append("")
        out.append(
            f"_{len(aging.undateable)} chaseable invoice(s) carry neither "
            f"`date_sent` nor `invoice_date`, so no clock can be started for "
            f"them. They are reported rather than aged from an invented "
            f"moment: {', '.join(aging.undateable)}._"
        )
    out.append("")

    out.append("## Proposed payment matches")
    out.append("")
    if match is None:
        out.append("_The matcher did not run for this report._")
        out.append("")
        return out

    out.append(
        "**Proposals only.** Nothing below has been actioned, closed or "
        "written back, and the confidence is this build's own judgement — "
        "the join is client and date of service for the group, with "
        "`amount_excl_tax` confirming which invoice inside it."
    )
    out.append("")
    if not match.proposals:
        out.append(
            f"_No proposal met the join. {match.summary()}_"
        )
    else:
        high = sum(1 for p in match.proposals if p.band == BAND_HIGH)
        out.append(
            f"{len(match.proposals)} proposal(s), {high} at high confidence."
        )
        out.append("")
        out.append(
            "| Invoice | Claimant | Service date | Ledger billed | "
            "Invoice amount | Confidence |"
        )
        out.append("| --- | --- | --- | --- | --- | --- |")
        for p in match.proposals:
            out.append(
                f"| {p.invoice_no} "
                f"| {p.claimant or '(unnamed)'} "
                f"| {p.service_date} "
                f"| {format_money(p.ledger_billed)} "
                f"| {format_money(p.invoice_amount_excl_tax)} "
                f"| {p.band} ({p.confidence:.2f}) |"
            )
        out.append("")
        out.append("Why each one, in the matcher's own words:")
        out.append("")
        for p in match.proposals:
            out.append(f"- **{p.invoice_no}** — {p.claimant}, {p.service_date}")
            for reason in p.basis:
                out.append(f"  - {reason}")
    out.append("")

    if match.ambiguous:
        out.append(
            f"**{len(match.ambiguous)} group(s) were left UNRESOLVED** rather "
            f"than guessed at. Attaching a payment to the wrong same-day "
            f"invoice is not recoverable from this report, so the matcher "
            f"refuses instead:"
        )
        for amb in match.ambiguous:
            candidates = (
                f" (candidates: {', '.join(amb.candidate_invoice_nos)})"
                if amb.candidate_invoice_nos else ""
            )
            out.append(
                f"- {amb.claimant or '(unnamed)'}, {amb.service_date} — "
                f"{amb.reason}{candidates}"
            )
        out.append("")
    if match.unmatched:
        out.append(
            f"**{len(match.unmatched)} claimant/date group(s) have no "
            f"candidate invoice at all.** The ledger says a claim was "
            f"processed and the export has nothing to join it to — worth a "
            f"look at whether the invoice was ever raised:"
        )
        for miss in match.unmatched:
            out.append(f"- {miss.claimant or '(unnamed)'}, {miss.service_date}")
        out.append("")
    if match.unbucketable:
        out.append(
            f"_{len(match.unbucketable)} claim line(s) could not be grouped "
            f"at all — no surname, or no readable date of service. They can "
            f"be joined to nothing and are counted here rather than dropped._"
        )
        out.append("")
    return out


def build_summary(
    grouped_totals: list[StatementTotals],
    counts: dict[str, int],
    *,
    total_lines: int,
    flagged_lines: int,
    proposals: list[ProposedMapping],
    generated_at: str,
    snapshot: InvoiceSnapshot | None = None,
    match: MatchReport | None = None,
    aging: AgingReport | None = None,
) -> str:
    """The orientation half. Never empty, whatever the ledger holds."""
    out: list[str] = []
    out.append("# Blue Cross remittance — backlog bulk review")
    out.append("")
    out.append(f"_Generated {generated_at}. Propose-only: nothing in this "
               f"report has been actioned, closed, or written back._")
    out.append("")

    # ABOVE the empty-ledger early return. See _invoice_side_sections: the
    # invoice feed's state is independent of whether the ledger holds
    # anything, and a stale export on an unseeded instance is exactly the
    # case a post-return placement would silence.
    out.extend(_invoice_side_sections(snapshot, match, aging))

    if not total_lines:
        # ILB — an empty ledger is a state, and it must be readable as one.
        out.append(
            "**The ledger holds no claim lines.** Nothing has been seeded "
            "yet, so there is nothing to review. Run `alfred reconcile seed "
            "--note <path>` to parse a payment summary into the ledger."
        )
        out.append("")
        return "\n".join(out)

    clean = total_lines - flagged_lines
    out.append("## What is here")
    out.append("")
    out.append(f"- **{total_lines}** claim line(s) across "
               f"**{len(grouped_totals)}** statement(s)")
    out.append(f"- **{flagged_lines}** need attention")
    out.append(f"- **{clean}** classified clean (paid as expected)")
    out.append("")

    out.append("## By attention class")
    out.append("")
    if not counts:
        out.append(
            "_No line carries an attention class. Every claim line was paid "
            "as billed with no unrecognised EOB code — a real result, not an "
            "empty section._"
        )
    else:
        for cls, n in counts.items():
            out.append(f"- **{CLASS_LABELS.get(cls, cls)}** ({cls}): {n}")
    out.append("")

    if counts.get(CLASS_UNKNOWN_EOB):
        # The framing belongs HERE, next to the number, not in a commit
        # message or a config comment. A first run against a real statement
        # puts most coded lines in this bucket, and without the explanation
        # that reads as the classifier failing. It is the opposite: no EOB
        # code is mapped until the operator maps it, because inventing a
        # provider's code meanings would classify real money on fabricated
        # authority. This section is where that mapping gets built.
        out.append(
            f"> **{counts[CLASS_UNKNOWN_EOB]} line(s) carry an EOB code this "
            f"instance has not been taught yet.** That is the loop starting, "
            f"not the classifier failing — no code is mapped until you map "
            f"it, because guessing a provider's code meanings would classify "
            f"real money on invented authority. Unmapped codes surface rather "
            f"than staying silent, which is the safe direction: an extra line "
            f"to read costs a glance, a missed one costs the payment."
        )
        out.append(">")
        out.append(
            "> Rule them with `alfred reconcile correct --line-key <key> "
            "--operator <you> --classes <class>` (empty `--classes` rules a "
            "line clear). Once the same code has been ruled the same way "
            "twice, it appears under *Proposed EOB mappings* below for you to "
            "accept into `reconcile.eob_classes` — the tool never applies one "
            "itself."
        )
        out.append("")

    out.append("## By statement")
    out.append("")
    out.append("| Statement | Provider | Lines | Billed | Paid | Flagged | Cross-foot |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for t in grouped_totals:
        if t.payment_total_delta is None:
            crossfoot = "no declared total"
        elif t.payment_total_delta == _ZERO:
            crossfoot = "agrees"
        else:
            crossfoot = f"**off by {format_money(t.payment_total_delta)}**"
        if t.subtotal_mismatches:
            crossfoot += f"; {len(t.subtotal_mismatches)} subtotal mismatch(es)"
        elif t.subtotals_checked:
            crossfoot += f"; {t.subtotals_checked} subtotal(s) agree"
        # NOTE-1: a capture-derived date is rendered DISTINCTLY, not just
        # recorded distinctly. Shown as an ordinary dated statement it reads
        # with the same authority as a printed one, which is exactly what
        # compounds a stale-label misattribution: the row that should invite
        # a second look invites none.
        date_cell = t.statement.statement_date or "(no date)"
        if t.statement.date_source == DATE_SOURCE_CAPTURE:
            date_cell += " ~"
        out.append(
            f"| {date_cell} "
            f"| {t.statement.provider or '(unknown)'} "
            f"| {t.line_count} "
            f"| {format_money(t.billed)} "
            f"| {format_money(t.paid)} "
            f"| {t.flagged} "
            f"| {crossfoot} |"
        )
    out.append("")

    with_declared = [t for t in grouped_totals if t.statement.declared_totals]
    if with_declared:
        out.append("## Declared statement totals")
        out.append("")
        out.append(
            "Figures the statement declares in its own totals block, shown "
            "beside what our claim lines sum to. They are **reported, not "
            "interpreted**: which labelled figure is *the* payment total is "
            "a question the statement does not answer, so nothing here is "
            "used as the reconciliation baseline. `payment_total` — the "
            "figure that sits with the claim lines it summarises — stays "
            "authoritative for the cross-foot above."
        )
        out.append("")
        for t in with_declared:
            out.append(
                f"- **{t.statement.statement_date or '(no date)'}** — our "
                f"claim lines sum to {format_money(t.paid)}"
            )
            for label, value in sorted(
                t.statement.declared_totals_decimal().items()
            ):
                match = (
                    "  ← matches our sum"
                    if label in t.declared_matching_paid else ""
                )
                out.append(f"  - {label}: {format_money(value)}{match}")
            if not t.declared_matching_paid:
                # ILB: "none of them matched" is a finding, and leaving it
                # implicit would let a reader assume the check was not run.
                out.append(
                    "  - _none of the declared figures equals our sum — worth "
                    "a look before trusting either side._"
                )
        out.append("")

    if any(
        t.statement.date_source == DATE_SOURCE_CAPTURE for t in grouped_totals
    ):
        # Stated, not left to be inferred from a glyph.
        out.append(
            "_`~` marks a statement whose DATE was recovered from a scan "
            "batch's own label rather than printed by the provider. That is "
            "a capture claim, not a provider one — it can be wrong in ways a "
            "printed date cannot, and every line filed under it inherits the "
            "doubt. Worth confirming before acting on those rows._"
        )
        out.append("")

    discrepancies = [t for t in grouped_totals if _is_discrepant(t)]
    out.append("## Cross-foot findings")
    out.append("")
    if not discrepancies:
        out.append(
            "_Every statement's figures reconcile against the provider's own "
            "totals. This is the check that says the parse read the numbers "
            "the statements actually contain._"
        )
    else:
        out.append(
            "Our sums disagree with the provider's own totals below. Treat "
            "these as parse findings first — a mis-read column shows up here "
            "before it shows up anywhere else."
        )
        out.append("")
        for t in discrepancies:
            out.append(f"- **{t.statement.statement_date or '(no date)'}**")
            if t.payment_total_delta not in (None, _ZERO):
                out.append(
                    f"  - declared payment total "
                    f"{format_money(t.statement.payment_total)}, our sum "
                    f"{format_money(t.paid)} "
                    f"(delta {format_money(t.payment_total_delta)})"
                )
            for m in t.subtotal_mismatches:
                out.append(f"  - {m}")
            if t.undated_lines:
                out.append(
                    f"  - **{t.undated_lines} claim line(s) are not "
                    f"attributed to any dated statement.** They parsed "
                    f"cleanly but their block carried no date the parser "
                    f"could read, so they are pooled under a dateless "
                    f"statement and reconcile against nothing. Check the "
                    f"seed's date_from_capture_metadata and statement_split "
                    f"lines for where the attribution broke."
                )
            if t.line_count_mismatch:
                declared, held = t.line_count_mismatch
                out.append(
                    f"  - the statement declares {declared} claim line(s), "
                    f"the ledger holds {held}. A gap here usually means two "
                    f"statements issued on one date were folded into one, or "
                    f"rows were attributed to the wrong block — check the "
                    f"seed's statement_split / statement_fold lines."
                )
    out.append("")

    out.append("## Proposed EOB mappings")
    out.append("")
    if not proposals:
        out.append(
            "_No proposals. Either no corrections have been recorded yet, or "
            "no code has enough consistent rulings behind it. The learning "
            "loop is running; it has nothing to say yet._"
        )
    else:
        out.append(
            "Repeated operator rulings suggest these code mappings. **Nothing "
            "is applied automatically** — add the ones you agree with to "
            "`reconcile.eob_classes` in config."
        )
        out.append("")
        for p in proposals:
            conflict = (
                f" ({p.conflicting} conflicting ruling(s) — check before "
                f"accepting)" if p.conflicting else ""
            )
            out.append(
                f"- `{p.code}` -> **{p.proposed_class}** "
                f"({p.supporting} supporting ruling(s)){conflict}"
            )
    out.append("")
    return "\n".join(out)


def build_report(
    contents: LedgerContents,
    *,
    eob_map: dict[str, str] | None = None,
    corrections: dict[str, Correction] | None = None,
    generated_at: str | None = None,
    snapshot: InvoiceSnapshot | None = None,
    now: datetime | None = None,
) -> BacklogReport:
    """Build the whole bulk-review artifact from a loaded ledger.

    ``snapshot`` is the invoice side. When given, the matcher and the aging
    watchdog BOTH run here — one production path, in the required order: the
    matcher's proposals feed :func:`~alfred.reconcile.aging.find_late` so a
    proposed invoice stops being chased. Passing them in pre-computed was the
    alternative and it was worse: two callers could then run them in the
    wrong order, or run one and not the other, and the report would look
    complete either way.

    ``None`` means no invoice side was configured, and the report says so
    explicitly rather than rendering a page that silently answers only half
    the question.
    """
    stamp = generated_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    classifications_list = classify_all(
        contents.claim_lines, eob_map=eob_map, corrections=corrections
    )
    by_key = {c.line_key: c for c in classifications_list}

    grouped = group_by_statement(contents)
    totals = [
        compute_statement_totals(stmt, claims, subs, by_key)
        for stmt, claims, subs in grouped
    ]

    counts = class_counts(classifications_list)
    flagged = sum(1 for c in classifications_list if c.needs_attention)
    proposals = propose_eob_mappings(corrections or {}, eob_map=eob_map)

    match: MatchReport | None = None
    aging: AgingReport | None = None
    if snapshot is not None:
        match = match_snapshot(contents.claim_lines, snapshot)
        aging = find_late(
            snapshot,
            matched_invoice_nos=match.matched_invoice_nos,
            now=now,
        )

    report = BacklogReport(
        csv_text=build_csv(contents.claim_lines, by_key),
        summary_text=build_summary(
            totals,
            counts,
            total_lines=len(contents.claim_lines),
            flagged_lines=flagged,
            proposals=proposals,
            generated_at=stamp,
            snapshot=snapshot,
            match=match,
            aging=aging,
        ),
        class_counts=counts,
        statement_totals=totals,
        flagged_lines=flagged,
        total_lines=len(contents.claim_lines),
        proposals=proposals,
        snapshot=snapshot,
        match=match,
        aging=aging,
    )

    log.info(
        "reconcile.report.built",
        total_lines=report.total_lines,
        flagged=report.flagged_lines,
        statements=len(totals),
        discrepancies=sum(1 for t in totals if _is_discrepant(t)),
        invoice_side=snapshot is not None,
        late=len(aging.late) if aging else 0,
        match_proposals=len(match.proposals) if match else 0,
        detail=(
            "ledger is empty — the report states that rather than rendering "
            "blank sections"
            if not report.total_lines else ""
        ),
    )
    if snapshot is None:
        log.info(
            "reconcile.report.no_invoice_side",
            detail="no invoice export was supplied, so the report covers "
                   "only what the provider answered — no late detection and "
                   "no payment matching. This is stated on the report too.",
        )
    return report


def write_report(
    report: BacklogReport,
    reports_dir: Path | str,
    *,
    stem: str,
) -> tuple[Path, Path]:
    """Write the CSV and the summary. Returns ``(csv_path, summary_path)``.

    Both files are written atomically (``.tmp`` -> ``os.replace``) so a
    reader never sees a half-written report.
    """
    d = Path(reports_dir)
    d.mkdir(parents=True, exist_ok=True)
    csv_path = d / f"{stem}.csv"
    summary_path = d / f"{stem}.md"
    _atomic_write(csv_path, report.csv_text)
    _atomic_write(summary_path, report.summary_text)
    log.info(
        "reconcile.report.written",
        csv_path=str(csv_path),
        summary_path=str(summary_path),
        rows=report.total_lines,
    )
    return csv_path, summary_path


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def report_stem(now: datetime | None = None) -> str:
    """``backlog-review-YYYYMMDD-HHMMSS`` — safe as a path segment."""
    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return f"backlog-review-{ts}"


__all__: list[str] = [
    "CSV_COLUMNS",
    "BacklogReport",
    "StatementTotals",
    "build_csv",
    "build_report",
    "build_summary",
    "compute_statement_totals",
    "report_stem",
    "write_report",
]
