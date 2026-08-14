"""The aging watchdog — the class this whole loop exists for.

Every other part of the reconciliation answers "what did the provider say
about this claim". This answers the question the provider never answers:
**which invoices did they not answer at all.** An unpaid claim that was
denied is visible on a statement; an invoice that vanished into a queue is
visible nowhere, which is exactly why it needs a clock.

**LATE = more than six weeks after the invoice was sent** (operator ruling;
the normal payment window is four to six). :data:`LATE_AFTER_DAYS` is that
ruling as a number, not a tuning knob — changing it changes what the
operator is told to chase.

**Propose-only, like everything else in this package.** A late entry is a
row on a report, not a state transition. Nothing here closes, dismisses or
auto-chases.

Three exclusions, each with its reason, because a chase list with false
rows is one the operator stops reading and then the real rows go unread
too:

* **voids never age** — a void expects no payment, so its clock never
  starts. Enforced via :meth:`Invoice.expects_payment`, the one authority.
* **matched invoices never age** — if a payment has been proposed against
  it, it is not silently outstanding. The matcher supplies the matched set;
  an empty set means *nothing matched yet*, which correctly ages everything
  rather than silently ageing nothing.
* **undateable invoices never age, and are REPORTED** — an invoice with
  neither ``date_sent`` nor ``invoice_date`` has no clock to run, and
  inventing one would age it from a moment that never happened. It is
  surfaced separately rather than dropped, because "cannot be aged" is a
  finding about the data, not a reason for silence.

**A weaker basis stays visible.** The one sent-undated invoice ages from
``invoice_date`` per RRTS's own rider, and its entry carries
``date_source`` so the report can say so — the same posture the statement
side uses for capture-derived dates. An invoice chased on a weaker clock
should look different from one chased on the date it was actually sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

import structlog

from .invoices import (
    DATE_SOURCE_SENT,
    Invoice,
    InvoiceSnapshot,
    parse_snapshot_date,
)

log = structlog.get_logger(__name__)

#: The operator's ruling: normal payment window is 4-6 weeks, LATE is more
#: than six weeks after the invoice was sent. Six weeks in days.
LATE_AFTER_DAYS = 42

# The snapshot-date parser used to live here as a private helper, back when
# the watchdog was its only consumer. The matcher reads snapshot dates too,
# so it was LIFTED to :mod:`alfred.reconcile.invoices` — the module that
# owns how RRTS spells things — rather than copied. An unreadable date still
# routes an invoice to the *undateable* bucket rather than to a guessed
# clock; that behaviour is unchanged, it just has one author now.


@dataclass
class LateInvoice:
    """One invoice past the window, with the basis of the judgement."""

    invoice_no: str = ""
    client_name: str = ""
    knumber: str = ""
    amount_excl_tax: Decimal | None = None
    #: The date the clock started from.
    since: str = ""
    #: ``date_sent`` (strong) or ``invoice_date`` (weaker — the sent-undated
    #: case). Carried so the report can show the difference rather than
    #: presenting both at equal confidence.
    date_source: str = ""
    days_outstanding: int = 0

    @property
    def basis_is_weak(self) -> bool:
        return self.date_source != DATE_SOURCE_SENT


@dataclass
class AgingReport:
    """What the watchdog found — including what it could not judge."""

    late: list[LateInvoice] = field(default_factory=list)
    #: Chaseable invoices that have no usable date at all. Reported, never
    #: silently skipped: "cannot be aged" is a finding about the data.
    undateable: list[str] = field(default_factory=list)
    #: Chaseable invoices inside the window — counted so "nothing is late"
    #: is distinguishable from "nothing was examined".
    within_window: int = 0
    #: True when the snapshot driving this was stale. Every figure below is
    #: then suspect, and the report must say so rather than presenting them
    #: as current.
    stale_snapshot: bool = False

    @property
    def examined(self) -> int:
        return len(self.late) + len(self.undateable) + self.within_window

    def summary(self) -> str:
        """One line, never empty — the ILB rule."""
        if self.stale_snapshot:
            return (
                "Aging NOT computed: the invoice export is stale, so nothing "
                "is chased against it. Figures would be about a world that "
                "has moved."
            )
        if not self.examined:
            return (
                "No chaseable invoices in the export — nothing to age. The "
                "watchdog ran and found the loop has nothing to chase."
            )
        parts = [f"{self.examined} chaseable invoice(s) examined"]
        parts.append(
            f"{len(self.late)} late (>{LATE_AFTER_DAYS}d)" if self.late
            else "none late"
        )
        if self.undateable:
            parts.append(f"{len(self.undateable)} with no usable date")
        return ", ".join(parts) + "."


def find_late(
    snapshot: InvoiceSnapshot,
    *,
    matched_invoice_nos: set[str] | None = None,
    now: datetime | None = None,
) -> AgingReport:
    """Age the chaseable invoices against the six-week ruling.

    ``matched_invoice_nos`` is the matcher's output. An EMPTY set means
    nothing has been matched yet, and everything chaseable is therefore
    still outstanding — which is the correct reading and deliberately not
    the reverse. A watchdog that stayed silent because the matcher had not
    run would be silent on exactly the day it mattered most.

    A STALE snapshot produces an empty report flagged ``stale_snapshot``.
    That is the promise made to RRTS, applied where it bites: chasing an
    invoice that was paid two days ago, on the strength of a three-day-old
    export, is the specific harm the staleness rule exists to prevent.
    """
    report = AgingReport()
    if snapshot.is_stale:
        report.stale_snapshot = True
        log.warning(
            "reconcile.aging.skipped_stale",
            exported_at=snapshot.exported_at or "(none)",
            detail="the invoice export is stale — nothing is aged against "
                   "it. A chase computed from old data sends the operator "
                   "after money that may already have arrived.",
        )
        return report

    matched = matched_invoice_nos or set()
    reference = (now or datetime.now(timezone.utc)).date()

    for invoice in snapshot.chaseable:
        if invoice.invoice_no in matched:
            continue
        since, source = invoice.aging_basis()
        started = parse_snapshot_date(since)
        if started is None:
            report.undateable.append(invoice.invoice_no)
            continue
        days = (reference - started).days
        if days > LATE_AFTER_DAYS:
            report.late.append(LateInvoice(
                invoice_no=invoice.invoice_no,
                client_name=invoice.client_name,
                knumber=invoice.knumber,
                amount_excl_tax=invoice.amount_excl_tax,
                since=since,
                date_source=source,
                days_outstanding=days,
            ))
        else:
            report.within_window += 1

    # Oldest first: the chase list is read top-down and the longest
    # outstanding is the one most likely to need a phone call.
    report.late.sort(key=lambda e: (-e.days_outstanding, e.invoice_no))

    if report.undateable:
        log.warning(
            "reconcile.aging.undateable",
            count=len(report.undateable),
            invoice_nos=report.undateable,
            detail="chaseable invoices with neither date_sent nor "
                   "invoice_date — no clock can be started, so they are "
                   "reported rather than aged from an invented moment",
        )
    log.info(
        "reconcile.aging.computed",
        examined=report.examined,
        late=len(report.late),
        within_window=report.within_window,
        undateable=len(report.undateable),
        threshold_days=LATE_AFTER_DAYS,
        detail=(
            "no invoice is late — the loop has nothing to chase"
            if report.examined and not report.late else ""
        ),
    )
    return report


__all__ = ["LATE_AFTER_DAYS", "AgingReport", "LateInvoice", "find_late"]
