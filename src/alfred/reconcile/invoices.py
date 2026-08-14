"""The invoice side — reading RRTS's nightly snapshot.

The other half of the loop. The remittance ledger says what the provider
ANSWERED; this says what was ASKED. Only together can they say what was
ignored, which is the question an aging watchdog exists to answer and the
reason P2 exists at all.

**The document's shape is RRTS's.** Loading is schema-tolerant per the
house contract: known fields are kept, unknown ones are dropped, missing
ones fall to defaults. The receiver already refuses to interpret; the
reader keeps that posture, because a reader that required a field would
break on the night their side added one — and it would break at 02:30
with nobody watching.

**The staleness rule is a PROMISE ON THE WIRE, not a heuristic.** RRTS was
told: an export older than 48 hours means the feed is flagged stale,
loudly, and nothing matches silently against old data. That is honoured
here exactly — :data:`STALE_AFTER_HOURS` is the number, and a stale
snapshot marks every downstream consumer rather than quietly serving
figures whose age nobody can see. A matcher fed 3-day-old invoices will
happily report an invoice "unpaid" that was paid two days ago, and the
operator would be chasing a payment he already had.

**Three known anomalies, each handled explicitly rather than defensively.**
Their names are in the code because a nameless "malformed row" bucket is
how a NEW anomaly hides inside an old one:

* the ``INV-ERROR`` junk row — a real row in the live snapshot, carrying no
  usable invoice. Dropped, counted, logged. It must never reach the matcher,
  and it must never be silent.
* the one SENT-UNDATED invoice — sent, but with no ``date_sent``. Their own
  rider says fall back to ``invoice_date`` for aging, so it ages on a
  WEAKER basis and says so (the ``date_source`` pattern this package
  already uses for capture-derived statement dates).
* VOIDS — a void invoice expects no payment. It is never late, never
  unmatched-in-a-way-that-matters, and never ages. Treating a void as
  outstanding would put permanent false rows in front of the operator, and
  a chase list with permanent noise is one that stops being read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: The promise made to RRTS on the thread: older than this and the feed is
#: flagged stale, loudly. A number, not a judgement, because it was told to
#: another team and they are entitled to rely on it.
STALE_AFTER_HOURS = 48

#: Invoice statuses, as the live snapshot uses them.
STATUS_SENT = "sent"
STATUS_VOID = "void"
STATUS_CREATED = "created"
STATUS_PAID = "paid"

#: Statuses that expect a payment and can therefore be LATE. A void expects
#: nothing; a created-but-unsent invoice has not been asked for yet, so its
#: clock has not started. Anything unrecognised is deliberately NOT in here —
#: see :func:`expects_payment`, which fails CLOSED for unknown statuses.
AGEABLE_STATUSES = frozenset({STATUS_SENT})

#: Every status this build understands. A status outside this set is counted
#: onto the snapshot and surfaced in the report — see
#: :attr:`InvoiceSnapshot.unknown_statuses`.
_KNOWN_STATUSES = frozenset(
    {STATUS_SENT, STATUS_VOID, STATUS_CREATED, STATUS_PAID}
)

#: The junk row's invoice number in the live snapshot. Named, not
#: pattern-matched, because a nameless malformed-row bucket is where the
#: NEXT anomaly hides.
JUNK_INVOICE_NO = "INV-ERROR"

#: Where a sent invoice's aging clock starts.
DATE_SOURCE_SENT = "date_sent"
DATE_SOURCE_INVOICE = "invoice_date"


def _money(value: Any) -> Decimal | None:
    """Coerce a snapshot money value to Decimal, or ``None``.

    Their figures arrive as strings or numbers depending on the field. A
    float is routed through ``str()`` so it lands on the decimal a human
    would read rather than its binary expansion — the same rule the ledger
    uses, for the same reason.
    """
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 — an unreadable figure degrades to absent
        return None


@dataclass
class InvoiceLineItem:
    """One line of one invoice. Their shape, kept."""

    date_of_service: str = ""
    amount: Decimal | None = None
    benefit_code: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InvoiceLineItem":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        if "amount" in known:
            known["amount"] = _money(known["amount"])
        for text_field in ("date_of_service", "benefit_code"):
            if text_field in known and known[text_field] is not None:
                known[text_field] = str(known[text_field])
        return cls(**known)


@dataclass
class Invoice:
    """One invoice from the snapshot."""

    invoice_no: str = ""
    client_name: str = ""
    knumber: str = ""
    status: str = ""
    date_sent: str = ""
    invoice_date: str = ""
    total: Decimal | None = None
    #: RRTS's NATIVE primary figure — the one to match on. Their tax ratio is
    #: frozen per invoice (pre-Apr-2025 invoices carry 15% and stay
    #: internally consistent), so a total re-derived from a config rate is
    #: wrong for exactly the invoices most likely to be outstanding.
    amount_excl_tax: Decimal | None = None
    line_items: list[InvoiceLineItem] = field(default_factory=list)

    @property
    def is_void(self) -> bool:
        return (self.status or "").strip().lower() == STATUS_VOID

    @property
    def is_junk(self) -> bool:
        return (self.invoice_no or "").strip().upper() == JUNK_INVOICE_NO

    def expects_payment(self) -> bool:
        """Whether this invoice can be chased at all.

        Fails CLOSED on an unrecognised status: a status this build does not
        know is NOT assumed chaseable, because inventing a chase for a state
        whose meaning we do not have puts a false row in front of the
        operator — and unlike the health-status case, the safe direction
        here is silence. A payment we fail to chase surfaces the next time
        the snapshot lands; a fabricated chase costs his trust in the list.
        The unknown status is logged so it stops being unknown.
        """
        status = (self.status or "").strip().lower()
        if status in AGEABLE_STATUSES:
            return True
        if status not in _KNOWN_STATUSES:
            log.warning(
                "reconcile.invoices.unknown_status",
                invoice_no=self.invoice_no,
                status=self.status,
                known=sorted(_KNOWN_STATUSES),
                detail="status not recognised — NOT treated as chaseable; "
                       "add it to the status set once its meaning is known",
            )
        return False

    def aging_basis(self) -> tuple[str, str]:
        """``(date, source)`` the aging clock starts from, or ``("", "")``.

        ``date_sent`` when present. Otherwise ``invoice_date``, per RRTS's
        own rider for the one sent-undated invoice — and the SOURCE is
        returned alongside so the weaker basis travels with the figure
        rather than being laundered into it. Same posture as a
        capture-derived statement date.
        """
        if (self.date_sent or "").strip():
            return self.date_sent.strip(), DATE_SOURCE_SENT
        if (self.invoice_date or "").strip():
            return self.invoice_date.strip(), DATE_SOURCE_INVOICE
        return "", ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Invoice":
        """Schema-tolerant load — the house contract, their shape kept."""
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        for name in ("total", "amount_excl_tax"):
            if name in known:
                known[name] = _money(known[name])
        for name in (
            "invoice_no", "client_name", "knumber", "status",
            "date_sent", "invoice_date",
        ):
            if name in known:
                known[name] = "" if known[name] is None else str(known[name]).strip()
        items = known.get("line_items")
        known["line_items"] = [
            InvoiceLineItem.from_dict(i)
            for i in (items or [])
            if isinstance(i, dict)
        ]
        return cls(**known)


@dataclass
class InvoiceSnapshot:
    """One loaded export, plus everything the loader had to say about it."""

    exported_at: str = ""
    invoices: list[Invoice] = field(default_factory=list)
    #: Rows dropped as junk, by invoice number. Counted and named — never a
    #: silent filter.
    junk_dropped: list[str] = field(default_factory=list)
    #: True when the file was missing or unreadable. Distinct from "loaded
    #: an empty snapshot", which is a real state RRTS can legitimately send.
    absent: bool = False
    #: Hours between ``exported_at`` and now, or ``None`` when unparseable.
    age_hours: float | None = None
    #: ``{status: count}`` for statuses this build does not recognise.
    #: AGGREGATED, not merely logged: a status that only ever appears in a
    #: log line stays unknown for a month, because nobody greps for a thing
    #: they do not know exists. The report surfaces the count so the unknown
    #: has somewhere to be seen.
    unknown_statuses: dict[str, int] = field(default_factory=dict)

    @property
    def is_stale(self) -> bool:
        """The promise. Unparseable or missing age counts as STALE.

        Fail-stale rather than fail-fresh: an export whose age cannot be
        established is exactly the one nobody should be matching against,
        and the whole point of the promise is that old data never moves
        silently.
        """
        if self.absent:
            return True
        if self.age_hours is None:
            return True
        return self.age_hours > STALE_AFTER_HOURS

    @property
    def chaseable(self) -> list[Invoice]:
        """Invoices that can be late — sent, non-void, non-junk."""
        # ``expects_payment`` is the ONE authority on whether a status can
        # be chased — void is excluded there, via AGEABLE_STATUSES. An
        # explicit ``not i.is_void`` here read as defence-in-depth and was
        # dead code: a mutation removing it changed nothing, which is how
        # the redundancy surfaced. Two places holding one rule is two places
        # to keep right, and the second one is the one that drifts.
        return [
            i for i in self.invoices
            if not i.is_junk and i.expects_payment()
        ]

    def summary(self) -> str:
        """One human line. Never empty — the ILB rule."""
        if self.absent:
            return (
                "No invoice export on disk. The receiver has not been posted "
                "to yet, or the path does not resolve — this is the pre-first-"
                "export state, not a failure."
            )
        parts = [f"{len(self.invoices)} invoice(s)"]
        if self.junk_dropped:
            parts.append(f"{len(self.junk_dropped)} junk row(s) dropped")
        parts.append(f"{len(self.chaseable)} chaseable")
        if self.is_stale:
            age = (
                f"{self.age_hours:.1f}h old" if self.age_hours is not None
                else "age unknown"
            )
            parts.append(f"**STALE** ({age}, promise is {STALE_AFTER_HOURS}h)")
        return ", ".join(parts) + "."


def parse_snapshot_date(value: str) -> "date | None":
    """A calendar date from a snapshot-side date string, or ``None``.

    Never raises. Their dates arrive as ``YYYY-MM-DD`` or as full
    timestamps, and both are accepted; anything else yields ``None``, which
    every caller reads as "no usable date" rather than as a guessed one.

    **Lifted here when the SECOND consumer arrived**, not before. It began
    as a private helper in :mod:`alfred.reconcile.aging` because the
    watchdog was the only thing reading a snapshot date. The matcher reads
    them too, and the commit that adds a second reader is exactly the commit
    that would otherwise create a second spelling — two date parsers that
    agree today and diverge the first time RRTS sends a format one of them
    tolerates. It lives in this module because this module is where "how
    RRTS spells things" is decided.

    Distinct from :func:`alfred.reconcile.money.parse_date`, which reads a
    PROVIDER'S cell: that one carries the ``date_order`` question and RAISES
    on an unreadable cell, because a misread date there becomes part of the
    ledger key. These dates are not keys and their format is RRTS's own, so
    degrading to ``None`` is right here and would be wrong there.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        pass
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _parse_exported_at(value: str) -> datetime | None:
    """Parse the snapshot's timestamp. ``Z`` suffix and fractional seconds
    both occur in the live document (``2026-08-13T22:40:00.116Z``)."""
    raw = (value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_snapshot(
    path: Path | str,
    *,
    now: datetime | None = None,
) -> InvoiceSnapshot:
    """Read the invoices export. Never raises; every degradation is stated.

    A missing file is the PRE-FIRST-EXPORT state, not an error — but it is
    also stale by definition, so nothing downstream matches against nothing
    and calls it fresh.
    """
    p = Path(path)
    if not p.is_file():
        log.info(
            "reconcile.invoices.absent",
            path=str(p),
            detail="no invoice export on disk yet — the receiver has not "
                   "been posted to, or the path does not resolve. Treated as "
                   "STALE so nothing matches against absent data.",
        )
        return InvoiceSnapshot(absent=True)

    try:
        document = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — unreadable export degrades loudly
        log.warning(
            "reconcile.invoices.unreadable",
            path=str(p),
            error_class=type(exc).__name__,
            detail="the export could not be parsed; treated as absent and "
                   "therefore STALE, rather than as an empty invoice set",
        )
        return InvoiceSnapshot(absent=True)

    if not isinstance(document, dict):
        log.warning(
            "reconcile.invoices.unreadable",
            path=str(p),
            detail="the export is not a JSON object at the top level",
        )
        return InvoiceSnapshot(absent=True)

    exported_at = str(document.get("exported_at") or "").strip()
    snapshot = InvoiceSnapshot(exported_at=exported_at)

    stamp = _parse_exported_at(exported_at)
    if stamp is not None:
        reference = now or datetime.now(timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        snapshot.age_hours = (reference - stamp).total_seconds() / 3600.0

    raw_invoices = document.get("invoices")
    for row in raw_invoices if isinstance(raw_invoices, list) else []:
        if not isinstance(row, dict):
            continue
        invoice = Invoice.from_dict(row)
        status = (invoice.status or "").strip().lower()
        if status and status not in _KNOWN_STATUSES:
            snapshot.unknown_statuses[status] = (
                snapshot.unknown_statuses.get(status, 0) + 1
            )
        if invoice.is_junk:
            # Named, counted, logged, and never handed to the matcher. A
            # nameless "malformed" bucket is where the NEXT anomaly hides.
            snapshot.junk_dropped.append(invoice.invoice_no)
            continue
        snapshot.invoices.append(invoice)

    if snapshot.junk_dropped:
        log.warning(
            "reconcile.invoices.junk_dropped",
            path=str(p),
            count=len(snapshot.junk_dropped),
            invoice_nos=snapshot.junk_dropped,
            detail="known junk rows removed before matching; they carry no "
                   "usable invoice and must never reach a proposal",
        )

    if snapshot.is_stale:
        # The promise, honoured loudly. WARNING because a stale feed is the
        # condition under which every downstream figure is untrustworthy.
        log.warning(
            "reconcile.invoices.stale",
            path=str(p),
            exported_at=exported_at or "(none)",
            age_hours=(
                round(snapshot.age_hours, 1)
                if snapshot.age_hours is not None else None
            ),
            threshold_hours=STALE_AFTER_HOURS,
            detail="the invoice export is older than the agreed window (or "
                   "its age cannot be established). Nothing matches silently "
                   "against old data — every consumer must surface this.",
        )
    else:
        log.info(
            "reconcile.invoices.loaded",
            path=str(p),
            exported_at=exported_at,
            invoices=len(snapshot.invoices),
            chaseable=len(snapshot.chaseable),
            junk_dropped=len(snapshot.junk_dropped),
            age_hours=round(snapshot.age_hours, 1) if snapshot.age_hours else 0.0,
        )
    return snapshot


__all__ = [
    "AGEABLE_STATUSES",
    "DATE_SOURCE_INVOICE",
    "DATE_SOURCE_SENT",
    "JUNK_INVOICE_NO",
    "STALE_AFTER_HOURS",
    "STATUS_CREATED",
    "STATUS_PAID",
    "STATUS_SENT",
    "STATUS_VOID",
    "Invoice",
    "InvoiceLineItem",
    "InvoiceSnapshot",
    "load_snapshot",
    "parse_snapshot_date",
]
