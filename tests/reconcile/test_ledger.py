"""The ledger model and its store.

The properties under test:

  1. **Money survives the round trip exactly.** Decimals in, Decimals out,
     with no float in between — a cent lost at load time is a report the
     operator stops trusting.
  2. **Load is schema-tolerant BOTH ways.** An older ledger (missing a
     field) and a newer one (carrying an extra) both load. This is the
     house contract and the reason a rollback is not a deployment event.
  3. **Upsert is idempotent and merges.** Re-running a seed changes
     nothing; seeding a second note does not delete the first one's rows.
  4. **A torn tail line costs one row, not the file.**
  5. **The occurrence tiebreak keeps two keyless lines apart.** This is the
     data-loss guard: without it the second ambulance line silently
     overwrites the first.
"""

from __future__ import annotations

import json
from decimal import Decimal

import structlog

from alfred.reconcile.ledger import (
    ROW_CLAIM,
    ROW_STATEMENT,
    ROW_SUBTOTAL,
    ClaimLine,
    LedgerContents,
    Statement,
    group_by_statement,
    line_key,
    load_ledger,
    upsert,
)


def _line(**kw) -> ClaimLine:
    base = dict(
        statement_date="2026-02-26",
        claim_no="90000101",
        dos="2026-02-10",
        surname="Aldenshaw",
        first_name="Marisol",
        benefit_code="700409",
        units=2,
        total_billed=Decimal("240.00"),
        amount_paid=Decimal("240.00"),
        pct_paid=Decimal("100"),
    )
    base.update(kw)
    return ClaimLine(**base)


def test_key_includes_the_four_ratified_parts_and_the_tiebreak():
    k = line_key("2026-02-26", "900", "2026-02-10", "700409", 0)
    for part in ("2026-02-26", "900", "2026-02-10", "700409"):
        assert part in k
    assert k != line_key("2026-02-26", "900", "2026-02-10", "700409", 1)


def test_two_keyless_lines_stay_distinct():
    """The data-loss guard. Two ``(Ambulance Claims)`` lines share all four
    ratified key parts; without the occurrence tiebreak the upsert would
    treat them as one row and lose the money on the second."""
    a = _line(claim_no="(Ambulance Claims)", occurrence=0,
              amount_paid=Decimal("300.00"))
    b = _line(claim_no="(Ambulance Claims)", occurrence=1,
              amount_paid=Decimal("150.00"))
    assert a.key != b.key


def test_money_round_trips_exactly_through_json():
    line = _line(amount_paid=Decimal("-31285.00"), total_billed=Decimal("0.10"))
    encoded = json.dumps(line.to_dict())
    restored = ClaimLine.from_dict(json.loads(encoded))
    assert restored.amount_paid == Decimal("-31285.00")
    assert restored.total_billed == Decimal("0.10")
    # And the sum is exact — the property a float would silently break.
    assert restored.total_billed * 3 == Decimal("0.30")


def test_from_dict_ignores_unknown_fields():
    """A ledger written by a NEWER build loads in an older one."""
    data = _line().to_dict()
    data["a_field_from_the_future"] = "surprise"
    restored = ClaimLine.from_dict(data)
    assert restored.claim_no == "90000101"
    assert not hasattr(restored, "a_field_from_the_future")


def test_from_dict_tolerates_missing_fields():
    """A ledger written by an OLDER build loads in a newer one."""
    restored = ClaimLine.from_dict({"claim_no": "900", "row_type": ROW_CLAIM})
    assert restored.claim_no == "900"
    assert restored.amount_paid is None
    assert restored.surname == ""


def test_from_dict_drops_a_stale_stored_key():
    """``key`` is derived. Honouring a stored one would let a key outlive
    the fields it was built from, so a hand-edited row cannot lie about
    its own identity."""
    data = _line().to_dict()
    data["key"] = "a-stale-key-from-somewhere-else"
    restored = ClaimLine.from_dict(data)
    assert restored.key != "a-stale-key-from-somewhere-else"
    assert restored.key == _line().key


def test_from_dict_reads_a_float_amount_without_binary_error():
    """Hand-edited or legacy rows may carry a JSON number. It is routed
    through str() so it lands on the decimal a human would read."""
    restored = ClaimLine.from_dict({"claim_no": "9", "amount_paid": 0.1})
    assert restored.amount_paid == Decimal("0.1")


def test_upsert_then_reload(tmp_path):
    path = tmp_path / "ledger.jsonl"
    stmt = Statement(statement_date="2026-02-26", provider="Wren Alderly",
                     payment_total=Decimal("240.00"))
    res = upsert(path, statements=[stmt], claim_lines=[_line()])
    assert res.inserted == 2
    assert res.updated == 0

    contents = load_ledger(path)
    assert len(contents.statements) == 1
    assert len(contents.claim_lines) == 1
    assert contents.claim_lines[0].amount_paid == Decimal("240.00")
    assert contents.statements[0].payment_total == Decimal("240.00")


def test_upsert_is_idempotent(tmp_path):
    path = tmp_path / "ledger.jsonl"
    line = _line()
    upsert(path, claim_lines=[line])
    res = upsert(path, claim_lines=[line])
    assert res.inserted == 0
    assert res.updated == 0
    assert res.unchanged == 1
    assert len(load_ledger(path).claim_lines) == 1


def test_upsert_updates_a_changed_row_in_place(tmp_path):
    path = tmp_path / "ledger.jsonl"
    upsert(path, claim_lines=[_line()])
    upsert(path, claim_lines=[_line(amount_paid=Decimal("199.00"))])
    contents = load_ledger(path)
    assert len(contents.claim_lines) == 1
    assert contents.claim_lines[0].amount_paid == Decimal("199.00")


def test_upsert_preserves_rows_it_was_not_given(tmp_path):
    """A merge, not a replace: seeding a second note must not delete the
    first one's rows."""
    path = tmp_path / "ledger.jsonl"
    upsert(path, claim_lines=[_line(claim_no="A")])
    upsert(path, claim_lines=[_line(claim_no="B")])
    keys = {c.claim_no for c in load_ledger(path).claim_lines}
    assert keys == {"A", "B"}


def test_subtotals_and_claims_do_not_collide_in_the_store(tmp_path):
    """They are separate indexes, so a subtotal sharing a key with a claim
    line does not evict it."""
    path = tmp_path / "ledger.jsonl"
    claim = _line()
    sub = _line(row_type=ROW_SUBTOTAL, amount_paid=Decimal("252.00"))
    upsert(path, claim_lines=[claim], subtotals=[sub])
    contents = load_ledger(path)
    assert len(contents.claim_lines) == 1
    assert len(contents.subtotals) == 1


def test_missing_ledger_is_empty_and_says_so():
    """The pre-seed state is a state, not a failure — and it is announced,
    so 'no ledger' is distinguishable from 'the loader broke'."""
    with structlog.testing.capture_logs() as captured:
        contents = load_ledger("/tmp/definitely/not/here/ledger.jsonl")
    assert contents.is_empty
    events = [c for c in captured if c.get("event") == "reconcile.ledger.absent"]
    assert len(events) == 1
    assert "pre-seed" in events[0]["detail"]


def test_torn_tail_line_costs_one_row_not_the_file(tmp_path):
    path = tmp_path / "ledger.jsonl"
    upsert(path, claim_lines=[_line(claim_no="A"), _line(claim_no="B")])
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"row_type": "claim", "claim_no": "C", "amou')

    with structlog.testing.capture_logs() as captured:
        contents = load_ledger(path)
    assert len(contents.claim_lines) == 2
    events = [
        c for c in captured if c.get("event") == "reconcile.ledger.rows_skipped"
    ]
    assert len(events) == 1
    assert events[0]["skipped"] == 1
    assert events[0]["kept"] == 2


def test_unknown_row_type_is_skipped_and_counted(tmp_path):
    path = tmp_path / "ledger.jsonl"
    path.write_text(
        json.dumps({"row_type": "something_new", "claim_no": "X"}) + "\n"
        + json.dumps(_line().to_dict()) + "\n",
        encoding="utf-8",
    )
    contents = load_ledger(path)
    # The positive control is in the same assertion pair: the KNOWN row
    # still loads, so this cannot pass against a loader that reads nothing.
    assert len(contents.claim_lines) == 1
    assert contents.claim_lines[0].claim_no == "90000101"


def test_upsert_logs_the_idempotent_case_explicitly(tmp_path):
    """ILB: a no-op re-run says it was a no-op. Without this the operator
    cannot tell a correct idempotent seed from a seed that silently did
    nothing."""
    path = tmp_path / "ledger.jsonl"
    upsert(path, claim_lines=[_line()])
    with structlog.testing.capture_logs() as captured:
        upsert(path, claim_lines=[_line()])
    events = [c for c in captured if c.get("event") == "reconcile.ledger.upsert"]
    assert len(events) == 1
    assert events[0]["inserted"] == 0
    assert events[0]["updated"] == 0
    assert "idempotent" in events[0]["detail"]


def test_group_by_statement_synthesises_a_header_rather_than_dropping_lines():
    """Claim lines whose statement header is missing are still returned —
    dropping them would make an unrecognised header look like an empty
    statement."""
    contents = LedgerContents(
        statements=[],
        claim_lines=[_line(statement_date="2026-02-26")],
    )
    with structlog.testing.capture_logs() as captured:
        grouped = group_by_statement(contents)
    assert len(grouped) == 1
    stmt, claims, _subs = grouped[0]
    assert stmt.statement_date == "2026-02-26"
    assert stmt.provider == ""
    assert len(claims) == 1
    events = [
        c for c in captured
        if c.get("event") == "reconcile.ledger.synthesised_statement"
    ]
    assert len(events) == 1
    assert events[0]["statement_date"] == "2026-02-26"


def test_group_by_statement_orders_by_date():
    contents = LedgerContents(
        statements=[
            Statement(statement_date="2026-07-30"),
            Statement(statement_date="2026-02-26"),
        ],
    )
    dates = [s.statement_date for s, _, _ in group_by_statement(contents)]
    assert dates == ["2026-02-26", "2026-07-30"]


def test_statement_row_type_constants_are_distinct():
    assert len({ROW_CLAIM, ROW_STATEMENT, ROW_SUBTOTAL}) == 3


def test_claimant_label():
    assert _line().claimant == "Aldenshaw, Marisol"
    assert _line(first_name="").claimant == "Aldenshaw"
    assert _line(surname="", first_name="Marisol").claimant == "Marisol"
