"""The invoice-side reader — the snapshot, its promise, and its anomalies.

The properties, in the order they matter:

  1. **The staleness rule is a PROMISE**, not a heuristic. RRTS was told on
     the thread that an export older than 48h flags the feed stale and
     nothing matches silently against old data. Another team is entitled to
     rely on that, so it is pinned as a number with both directions driven —
     and it FAILS STALE when the age cannot be established, because an
     export whose age is unknown is exactly the one nobody should match on.
  2. **Schema-tolerant both ways.** Their shape is theirs; a reader that
     required a field would break the night they added one, at 02:30, with
     nobody watching.
  3. **Three named anomalies**, each explicit rather than swept into a
     "malformed" bucket — a nameless bucket is where the NEXT anomaly hides.

Fixture data is wholly invented. The live snapshot's ``client_name`` and
``knumber`` ARE the PHI floor: no real client name, k-number, invoice id or
figure appears here, and none may.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import structlog

from alfred.reconcile.invoices import (
    DATE_SOURCE_INVOICE,
    DATE_SOURCE_SENT,
    JUNK_INVOICE_NO,
    STALE_AFTER_HOURS,
    Invoice,
    InvoiceSnapshot,
    load_snapshot,
)

_NOW = datetime(2026, 8, 14, 6, 0, 0, tzinfo=timezone.utc)


def _invoice(**overrides):
    base = {
        "invoice_no": "INV-20260601-00042",
        "client_name": "Aldenshaw, Marisol",
        "knumber": "K000111",
        "status": "sent",
        "date_sent": "2026-06-01",
        "invoice_date": "2026-05-30",
        "total": "1150.00",
        "amount_excl_tax": "1000.00",
        "line_items": [
            {"date_of_service": "2026-05-28", "amount": "1000.00",
             "benefit_code": "700409"},
        ],
    }
    base.update(overrides)
    return base


def _write(tmp_path, *, exported_at: str, invoices: list[dict]):
    p = tmp_path / "invoices.json"
    p.write_text(
        json.dumps({"exported_at": exported_at, "invoices": invoices}),
        encoding="utf-8",
    )
    return p


def _fresh_stamp(hours_old: float = 1.0) -> str:
    return (_NOW - timedelta(hours=hours_old)).isoformat().replace("+00:00", "Z")


# --- the promise ---------------------------------------------------------------


def test_the_stale_window_is_the_number_told_to_rrts() -> None:
    """Pinned as a constant because it was told to another team. Changing it
    is a conversation with them, not a refactor."""
    assert STALE_AFTER_HOURS == 48


def test_a_fresh_export_is_not_stale(tmp_path) -> None:
    p = _write(tmp_path, exported_at=_fresh_stamp(2), invoices=[_invoice()])
    snap = load_snapshot(p, now=_NOW)
    assert snap.is_stale is False
    assert snap.age_hours == pytest.approx(2.0, abs=0.01)


def test_an_old_export_is_stale_and_says_so_loudly(tmp_path) -> None:
    """Both halves: the flag AND the log. A stale feed that is flagged in a
    field nobody reads is the silence the promise exists to prevent."""
    p = _write(tmp_path, exported_at=_fresh_stamp(72), invoices=[_invoice()])
    with structlog.testing.capture_logs() as captured:
        snap = load_snapshot(p, now=_NOW)
    assert snap.is_stale is True
    events = [c for c in captured if c.get("event") == "reconcile.invoices.stale"]
    assert len(events) == 1
    assert events[0]["threshold_hours"] == STALE_AFTER_HOURS
    assert events[0]["age_hours"] == pytest.approx(72.0, abs=0.1)


def test_the_boundary_is_older_than_not_at(tmp_path) -> None:
    """Exactly 48h is NOT stale; a hair over is. Pinned because a promise
    with an ambiguous boundary is one both sides can read differently."""
    at = _write(tmp_path, exported_at=_fresh_stamp(48), invoices=[])
    assert load_snapshot(at, now=_NOW).is_stale is False
    over = _write(tmp_path, exported_at=_fresh_stamp(48.5), invoices=[])
    assert load_snapshot(over, now=_NOW).is_stale is True


def test_an_unparseable_timestamp_fails_STALE(tmp_path) -> None:
    """Fail-stale, not fail-fresh. An export whose age cannot be established
    is precisely the one nothing should match against — and the positive
    control is in the same test: a parseable one is not stale."""
    bad = _write(tmp_path, exported_at="not a timestamp", invoices=[_invoice()])
    snap = load_snapshot(bad, now=_NOW)
    assert snap.age_hours is None
    assert snap.is_stale is True

    good = _write(tmp_path, exported_at=_fresh_stamp(1), invoices=[_invoice()])
    assert load_snapshot(good, now=_NOW).is_stale is False


def test_the_live_timestamp_format_parses(tmp_path) -> None:
    """The real snapshot carries fractional seconds and a Z suffix
    (``2026-08-13T22:40:00.116Z``). Both are pinned because both appear."""
    p = _write(tmp_path, exported_at="2026-08-14T05:00:00.116Z", invoices=[])
    snap = load_snapshot(p, now=_NOW)
    assert snap.age_hours == pytest.approx(1.0, abs=0.01)
    assert snap.is_stale is False


def test_an_absent_export_is_stale_but_named_as_pre_first(tmp_path) -> None:
    """Missing is a STATE, not a failure — and it is still stale, so nothing
    matches against nothing and calls it fresh."""
    with structlog.testing.capture_logs() as captured:
        snap = load_snapshot(tmp_path / "nothing.json", now=_NOW)
    assert snap.absent is True
    assert snap.is_stale is True
    assert snap.invoices == []
    assert any(c.get("event") == "reconcile.invoices.absent" for c in captured)
    assert "pre-first-export" in snap.summary() or "not been posted" in snap.summary()


def test_an_unreadable_export_degrades_to_absent(tmp_path) -> None:
    p = tmp_path / "invoices.json"
    p.write_text("{not json", encoding="utf-8")
    with structlog.testing.capture_logs() as captured:
        snap = load_snapshot(p, now=_NOW)
    assert snap.absent is True
    assert snap.is_stale is True
    assert any(
        c.get("event") == "reconcile.invoices.unreadable" for c in captured
    )


# --- schema tolerance ------------------------------------------------------------


def test_unknown_fields_are_dropped_not_fatal(tmp_path) -> None:
    """Their shape is theirs. A reader that broke on a new field would break
    at 02:30 with nobody watching."""
    p = _write(
        tmp_path,
        exported_at=_fresh_stamp(1),
        invoices=[_invoice(their_new_field={"nested": True})],
    )
    snap = load_snapshot(p, now=_NOW)
    assert len(snap.invoices) == 1
    assert not hasattr(snap.invoices[0], "their_new_field")
    # Positive control: the fields we DO know still arrived.
    assert snap.invoices[0].amount_excl_tax == Decimal("1000.00")


def test_missing_fields_fall_to_defaults() -> None:
    inv = Invoice.from_dict({"invoice_no": "INV-1"})
    assert inv.status == ""
    assert inv.amount_excl_tax is None
    assert inv.line_items == []


def test_amount_excl_tax_is_carried_as_decimal(tmp_path) -> None:
    """Their NATIVE primary figure. Carried exactly — a float round-trip
    would reintroduce the representation error the ledger already refuses,
    and this is the number the matcher will join on."""
    p = _write(tmp_path, exported_at=_fresh_stamp(1),
               invoices=[_invoice(amount_excl_tax=1000.10)])
    inv = load_snapshot(p, now=_NOW).invoices[0]
    assert inv.amount_excl_tax == Decimal("1000.10")
    assert inv.amount_excl_tax * 3 == Decimal("3000.30")


def test_line_items_are_parsed_and_tolerant(tmp_path) -> None:
    p = _write(
        tmp_path, exported_at=_fresh_stamp(1),
        invoices=[_invoice(line_items=[
            {"date_of_service": "2026-05-28", "amount": "500.00",
             "benefit_code": "700409", "unexpected": 1},
            "not a dict",
        ])],
    )
    items = load_snapshot(p, now=_NOW).invoices[0].line_items
    assert len(items) == 1
    assert items[0].amount == Decimal("500.00")
    assert not hasattr(items[0], "unexpected")


# --- anomaly 1: the junk row -----------------------------------------------------


def test_the_junk_row_is_dropped_counted_and_logged(tmp_path) -> None:
    """A real row in the live snapshot. It must never reach a proposal, and
    it must never be silent — the positive control is the good invoice
    beside it, which must survive."""
    p = _write(
        tmp_path, exported_at=_fresh_stamp(1),
        invoices=[_invoice(), _invoice(invoice_no=JUNK_INVOICE_NO)],
    )
    with structlog.testing.capture_logs() as captured:
        snap = load_snapshot(p, now=_NOW)
    assert len(snap.invoices) == 1
    assert snap.invoices[0].invoice_no == "INV-20260601-00042"
    assert snap.junk_dropped == [JUNK_INVOICE_NO]
    events = [
        c for c in captured if c.get("event") == "reconcile.invoices.junk_dropped"
    ]
    assert len(events) == 1
    assert events[0]["count"] == 1


def test_the_junk_row_never_reaches_the_chaseable_set(tmp_path) -> None:
    p = _write(tmp_path, exported_at=_fresh_stamp(1),
               invoices=[_invoice(invoice_no=JUNK_INVOICE_NO)])
    snap = load_snapshot(p, now=_NOW)
    assert snap.chaseable == []


# --- anomaly 2: the sent-undated invoice -------------------------------------------


def test_a_sent_invoice_ages_from_date_sent() -> None:
    inv = Invoice.from_dict(_invoice())
    assert inv.aging_basis() == ("2026-06-01", DATE_SOURCE_SENT)


def test_the_sent_undated_invoice_falls_back_to_invoice_date() -> None:
    """RRTS's own rider for the one known case. The SOURCE travels with the
    date so the weaker basis is visible rather than laundered — the same
    posture as a capture-derived statement date."""
    inv = Invoice.from_dict(_invoice(date_sent=""))
    date, source = inv.aging_basis()
    assert date == "2026-05-30"
    assert source == DATE_SOURCE_INVOICE


def test_an_invoice_with_no_date_at_all_has_no_basis() -> None:
    """No date, no clock. Inventing one would age an invoice from a moment
    that never happened."""
    inv = Invoice.from_dict(_invoice(date_sent="", invoice_date=""))
    assert inv.aging_basis() == ("", "")


# --- anomaly 3: voids ---------------------------------------------------------------


def test_a_void_invoice_never_ages() -> None:
    """A void expects no payment. Treating it as outstanding would put a
    permanent false row in front of the operator, and a chase list with
    permanent noise stops being read."""
    inv = Invoice.from_dict(_invoice(status="void"))
    assert inv.is_void is True
    assert inv.expects_payment() is False


def test_voids_are_excluded_from_chaseable(tmp_path) -> None:
    p = _write(
        tmp_path, exported_at=_fresh_stamp(1),
        invoices=[_invoice(), _invoice(invoice_no="INV-2", status="void")],
    )
    snap = load_snapshot(p, now=_NOW)
    assert len(snap.invoices) == 2, "the void is KEPT — it is real data"
    assert [i.invoice_no for i in snap.chaseable] == ["INV-20260601-00042"]


@pytest.mark.parametrize("status", ["created", "paid"])
def test_created_and_paid_are_not_chaseable(status) -> None:
    """A created invoice has not been asked for yet — its clock has not
    started. A paid one has been answered."""
    assert Invoice.from_dict(_invoice(status=status)).expects_payment() is False


def test_an_unknown_status_fails_CLOSED_and_is_logged() -> None:
    """A fabricated chase costs the operator's trust in the list; a missed
    one surfaces on the next snapshot. So the safe direction here is
    silence — the opposite of the health-status denylist, and deliberately
    so. The unknown status is logged so it stops being unknown."""
    inv = Invoice.from_dict(_invoice(status="partially_credited"))
    with structlog.testing.capture_logs() as captured:
        assert inv.expects_payment() is False
    events = [
        c for c in captured
        if c.get("event") == "reconcile.invoices.unknown_status"
    ]
    assert len(events) == 1
    assert events[0]["status"] == "partially_credited"

    # Positive control: a KNOWN chaseable status still returns True, so this
    # cannot pass against a build that refuses everything.
    assert Invoice.from_dict(_invoice()).expects_payment() is True


# --- the summary line ----------------------------------------------------------------


def test_the_summary_is_never_empty(tmp_path) -> None:
    assert InvoiceSnapshot(absent=True).summary()
    p = _write(tmp_path, exported_at=_fresh_stamp(1), invoices=[])
    assert load_snapshot(p, now=_NOW).summary()


def test_a_stale_summary_says_stale(tmp_path) -> None:
    p = _write(tmp_path, exported_at=_fresh_stamp(72), invoices=[_invoice()])
    summary = load_snapshot(p, now=_NOW).summary()
    assert "STALE" in summary
    assert str(STALE_AFTER_HOURS) in summary


def test_a_fresh_summary_does_not_say_stale(tmp_path) -> None:
    """The control — if it said STALE always it would say nothing."""
    p = _write(tmp_path, exported_at=_fresh_stamp(1), invoices=[_invoice()])
    assert "STALE" not in load_snapshot(p, now=_NOW).summary()
