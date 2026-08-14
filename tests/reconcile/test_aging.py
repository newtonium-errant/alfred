"""The aging watchdog — the class the whole loop exists for.

Every other part of the reconciliation answers "what did the provider say
about this claim". This answers what the provider never says: **which
invoices did they not answer at all.** A denied claim is visible on a
statement; an invoice that vanished into a queue is visible nowhere, which
is why it needs a clock rather than a reader.

The properties, in the order they matter:

  1. **LATE is the operator's ruling as a number** — more than six weeks
     after sending. Pinned in both directions, because a threshold with an
     ambiguous edge produces a chase list nobody can predict.
  2. **A stale snapshot ages NOTHING.** The promise made to RRTS, applied
     where it bites: chasing an invoice that was paid two days ago, on a
     three-day-old export, is the specific harm.
  3. **Three exclusions, each with a control** — voids, matched, and
     undateable. A chase list with false rows is one the operator stops
     reading, and then the real rows go unread too.
  4. **A weaker clock stays visibly weaker.** The sent-undated invoice ages
     from ``invoice_date`` and says so.

Fixture data wholly invented; the live ``client_name`` + ``knumber`` are
the PHI floor.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
import structlog

from alfred.reconcile.aging import LATE_AFTER_DAYS, find_late
from alfred.reconcile.invoices import (
    DATE_SOURCE_INVOICE,
    DATE_SOURCE_SENT,
    Invoice,
    InvoiceSnapshot,
)

_NOW = datetime(2026, 8, 14, tzinfo=timezone.utc)


def _invoice(no: str, **overrides) -> Invoice:
    base = {
        "invoice_no": no,
        "client_name": "Marisol Aldenshaw",
        "knumber": "K0001234",
        "status": "sent",
        "date_sent": "2026-06-01",
        "invoice_date": "2026-05-30",
        "amount_excl_tax": "1000.00",
    }
    base.update(overrides)
    return Invoice.from_dict(base)


def _snapshot(*invoices: Invoice, age_hours: float = 1.0) -> InvoiceSnapshot:
    return InvoiceSnapshot(
        exported_at="2026-08-14T00:00:00Z",
        age_hours=age_hours,
        invoices=list(invoices),
    )


# --- the ruling, as a number ------------------------------------------------------


def test_late_is_six_weeks() -> None:
    """The operator's ruling. Not a tuning knob — changing it changes what
    he is told to chase."""
    assert LATE_AFTER_DAYS == 42


def test_the_boundary_is_MORE_than_six_weeks() -> None:
    """Exactly 42 days is NOT late; 43 is. Pinned in both directions,
    because a chase list whose edge nobody can predict is one that gets
    argued with instead of worked."""
    at = _invoice("INV-AT", date_sent="2026-07-03")     # exactly 42 days
    over = _invoice("INV-OVER", date_sent="2026-07-02")  # 43 days
    report = find_late(_snapshot(at, over), now=_NOW)
    assert [e.invoice_no for e in report.late] == ["INV-OVER"]
    assert report.within_window == 1


def test_late_entries_are_oldest_first() -> None:
    """The list is read top-down and the longest outstanding is the one most
    likely to need a phone call."""
    report = find_late(
        _snapshot(
            _invoice("INV-NEW", date_sent="2026-06-20"),
            _invoice("INV-OLD", date_sent="2026-01-01"),
        ),
        now=_NOW,
    )
    assert [e.invoice_no for e in report.late] == ["INV-OLD", "INV-NEW"]
    assert report.late[0].days_outstanding > report.late[1].days_outstanding


def test_a_late_entry_carries_the_evidence() -> None:
    """A chase row the operator cannot verify is one he has to re-derive."""
    entry = find_late(_snapshot(_invoice("INV-1")), now=_NOW).late[0]
    assert entry.invoice_no == "INV-1"
    assert entry.client_name == "Marisol Aldenshaw"
    assert entry.knumber == "K0001234"
    assert entry.amount_excl_tax == Decimal("1000.00")
    assert entry.since == "2026-06-01"
    assert entry.days_outstanding == 74


# --- the staleness promise, where it bites -----------------------------------------


def test_a_stale_snapshot_ages_NOTHING() -> None:
    """The concrete harm: an invoice paid two days ago, chased on the
    strength of a three-day-old export."""
    stale = _snapshot(_invoice("INV-1", date_sent="2026-01-01"), age_hours=72.0)
    with structlog.testing.capture_logs() as captured:
        report = find_late(stale, now=_NOW)
    assert report.stale_snapshot is True
    assert report.late == []
    assert report.examined == 0
    assert "stale" in report.summary().lower()
    assert any(
        c.get("event") == "reconcile.aging.skipped_stale" for c in captured
    )


def test_a_fresh_snapshot_DOES_age() -> None:
    """The control — if staleness suppressed everything unconditionally the
    watchdog would be permanently silent and look identical to healthy."""
    report = find_late(
        _snapshot(_invoice("INV-1", date_sent="2026-01-01")), now=_NOW
    )
    assert report.stale_snapshot is False
    assert len(report.late) == 1


# --- the three exclusions, each with its control -------------------------------------


def test_voids_never_age() -> None:
    """A void expects no payment, so its clock never starts. Its control is
    the identical invoice that is merely sent."""
    void = _invoice("INV-VOID", date_sent="2026-01-01", status="void")
    sent = _invoice("INV-SENT", date_sent="2026-01-01")
    report = find_late(_snapshot(void, sent), now=_NOW)
    assert [e.invoice_no for e in report.late] == ["INV-SENT"]


def test_a_matched_invoice_does_not_age() -> None:
    """If a payment has been proposed against it, it is not silently
    outstanding."""
    report = find_late(
        _snapshot(
            _invoice("INV-1", date_sent="2026-01-01"),
            _invoice("INV-2", date_sent="2026-01-01"),
        ),
        matched_invoice_nos={"INV-1"},
        now=_NOW,
    )
    assert [e.invoice_no for e in report.late] == ["INV-2"]


def test_an_empty_matched_set_ages_EVERYTHING() -> None:
    """The direction that matters. "Nothing matched yet" must mean
    "everything is still outstanding", not "nothing to chase" — a watchdog
    that fell silent because the matcher had not run would be silent on
    exactly the day it mattered most."""
    report = find_late(
        _snapshot(_invoice("INV-1", date_sent="2026-01-01")),
        matched_invoice_nos=set(),
        now=_NOW,
    )
    assert len(report.late) == 1


def test_an_undateable_invoice_is_reported_not_aged() -> None:
    """No date, no clock — and inventing one would age it from a moment
    that never happened. "Cannot be aged" is a finding about the data, so
    it is surfaced rather than dropped."""
    blind = _invoice("INV-BLIND", date_sent="", invoice_date="")
    with structlog.testing.capture_logs() as captured:
        report = find_late(_snapshot(blind), now=_NOW)
    assert report.undateable == ["INV-BLIND"]
    assert report.late == []
    assert report.examined == 1
    events = [
        c for c in captured if c.get("event") == "reconcile.aging.undateable"
    ]
    assert len(events) == 1


def test_an_unparseable_date_routes_to_undateable_not_to_a_guess() -> None:
    report = find_late(
        _snapshot(_invoice("INV-X", date_sent="last spring", invoice_date="")),
        now=_NOW,
    )
    assert report.undateable == ["INV-X"]


# --- the weaker clock stays visibly weaker --------------------------------------------


def test_the_sent_undated_invoice_ages_on_invoice_date_and_says_so() -> None:
    """RRTS's own rider for the one known case. An invoice chased on a
    weaker clock must look different from one chased on the date it was
    actually sent — the same posture the statement side uses for
    capture-derived dates."""
    entry = find_late(
        _snapshot(_invoice("INV-U", date_sent="", invoice_date="2026-05-01")),
        now=_NOW,
    ).late[0]
    assert entry.since == "2026-05-01"
    assert entry.date_source == DATE_SOURCE_INVOICE
    assert entry.basis_is_weak is True


def test_a_normally_sent_invoice_is_not_flagged_weak() -> None:
    """The control — if every entry were weak the flag would say nothing."""
    entry = find_late(_snapshot(_invoice("INV-1")), now=_NOW).late[0]
    assert entry.date_source == DATE_SOURCE_SENT
    assert entry.basis_is_weak is False


# --- the empty states, stated ----------------------------------------------------------


def test_nothing_late_is_stated_as_a_result() -> None:
    """ILB: "the loop has nothing to chase" is a finding, and it must be
    distinguishable from "the watchdog did not run"."""
    with structlog.testing.capture_logs() as captured:
        report = find_late(
            _snapshot(_invoice("INV-1", date_sent="2026-08-10")), now=_NOW
        )
    assert report.late == []
    assert report.within_window == 1
    assert "none late" in report.summary()
    events = [c for c in captured if c.get("event") == "reconcile.aging.computed"]
    assert len(events) == 1
    assert "nothing to chase" in events[0]["detail"]


def test_no_chaseable_invoices_at_all_is_stated() -> None:
    report = find_late(_snapshot(), now=_NOW)
    assert report.examined == 0
    assert "nothing to age" in report.summary()


def test_the_summary_is_never_empty() -> None:
    assert find_late(_snapshot(), now=_NOW).summary()
    assert find_late(_snapshot(_invoice("INV-1")), now=_NOW).summary()
    assert find_late(_snapshot(age_hours=99.0), now=_NOW).summary()


# --- the unknown-status count reaches the snapshot (the report rider) -------------------


def test_unknown_statuses_are_counted_not_only_logged() -> None:
    """A status that only ever appears in a log line stays unknown for a
    month, because nobody greps for a thing they do not know exists. The
    count rides on the snapshot so the report has something to surface."""
    import json

    from alfred.reconcile.invoices import load_snapshot

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "invoices.json"
        p.write_text(json.dumps({
            "exported_at": "2026-08-14T00:00:00Z",
            "invoices": [
                {"invoice_no": "INV-1", "status": "partially_credited"},
                {"invoice_no": "INV-2", "status": "partially_credited"},
                {"invoice_no": "INV-3", "status": "sent"},
            ],
        }), encoding="utf-8")
        snap = load_snapshot(p, now=_NOW)

    assert snap.unknown_statuses == {"partially_credited": 2}
    # Control: known statuses are NOT counted, or the number means nothing.
    assert "sent" not in snap.unknown_statuses
