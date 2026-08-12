"""Attention classification — the six classes, and the correction loop.

The properties under test:

  1. **Unknown EOB codes fail OPEN.** An unmapped code surfaces the line.
     This is a denylist, and the direction is what the whole design rests
     on: an extra card costs a glance, a dropped one costs the money.
  2. **Reversal and short-pay are arithmetic**, derivable with no map.
  3. **A denial suppresses short-pay**, so one event is not counted twice.
  4. **Corrections feed back**, including a ruling of "clear", which is how
     a false positive is retired.
  5. **Proposals are proposals.** Nothing in this module writes a mapping.

Every deny/exclude assertion is paired with its nearest admissible
neighbour in the same test — an exclusion pin with no positive control
passes identically when the whole classifier is broken.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import structlog

from alfred.reconcile.attention import (
    ALL_CLASSES,
    ATTENTION_CLASSES,
    CLASS_DOCUMENTATION_REQUIRED,
    CLASS_DUPLICATE_DENIAL,
    CLASS_IDENTITY_MISMATCH,
    CLASS_RESUBMISSION_REQUIRED,
    CLASS_REVERSAL,
    CLASS_SHORT_PAY,
    CLASS_UNKNOWN_EOB,
    Correction,
    append_correction,
    class_counts,
    classify,
    classify_all,
    load_corrections,
    normalise_eob,
    propose_eob_mappings,
    split_eob_codes,
)
from alfred.reconcile.ledger import ClaimLine


def _line(**kw) -> ClaimLine:
    base = dict(
        statement_date="2026-02-26",
        claim_no="900",
        dos="2026-02-10",
        benefit_code="700409",
        total_billed=Decimal("100.00"),
        amount_paid=Decimal("100.00"),
        pct_paid=Decimal("100"),
    )
    base.update(kw)
    return ClaimLine(**base)


# --- the ratified vocabulary --------------------------------------------------


def test_the_six_ratified_classes_are_exactly_these():
    """A contract pin. The vocabulary was ratified on 2026-08-12 (operator
    walkthrough Q4, which added short-pay as the sixth); widening it is a
    deliberate act that updates this line, never a silent addition."""
    assert ATTENTION_CLASSES == {
        CLASS_DUPLICATE_DENIAL,
        CLASS_DOCUMENTATION_REQUIRED,
        CLASS_RESUBMISSION_REQUIRED,
        CLASS_IDENTITY_MISMATCH,
        CLASS_REVERSAL,
        CLASS_SHORT_PAY,
    }


def test_unknown_eob_is_not_one_of_the_six_but_is_a_valid_class():
    assert CLASS_UNKNOWN_EOB not in ATTENTION_CLASSES
    assert CLASS_UNKNOWN_EOB in ALL_CLASSES


# --- fail-open ----------------------------------------------------------------


def test_unmapped_eob_code_fails_open_into_attention():
    """The denylist direction. Paired with its positive control below: the
    SAME line with the code mapped classifies as the mapped class and NOT
    as unknown — so this cannot pass against a build that flags everything.
    """
    line = _line(eob_code="ZZ14")
    unmapped = classify(line, eob_map={})
    assert CLASS_UNKNOWN_EOB in unmapped.classes
    assert unmapped.needs_attention

    mapped = classify(line, eob_map={"ZZ14": CLASS_DOCUMENTATION_REQUIRED})
    assert mapped.classes == [CLASS_DOCUMENTATION_REQUIRED]
    assert CLASS_UNKNOWN_EOB not in mapped.classes


def test_a_clean_line_carries_no_classes():
    """The other half of the control: a fully-paid line with no code is
    NOT flagged. Without this, every fail-open assertion above would pass
    against a classifier that flags unconditionally."""
    result = classify(_line())
    assert result.classes == []
    assert not result.needs_attention


def test_a_code_configured_to_an_unknown_class_still_fails_open():
    """A typo in config must not SILENCE a line. The line falls through to
    unknown_eob rather than being cleared, and the refusal is logged with
    the reason so the typo is findable."""
    with structlog.testing.capture_logs() as captured:
        result = classify(_line(eob_code="ZZ99"),
                          eob_map={"ZZ99": "not_a_real_class"})
    assert CLASS_UNKNOWN_EOB in result.classes
    events = [
        c for c in captured
        if c.get("event") == "reconcile.attention.unknown_configured_class"
    ]
    assert len(events) == 1
    assert events[0]["code"] == "ZZ99"
    assert events[0]["configured"] == "not_a_real_class"


def test_a_mapped_code_does_not_mask_an_unmapped_sibling():
    """Multi-code cells: taking only the first code would let a mapped one
    hide an unmapped one and quietly close the fail-open path."""
    result = classify(
        _line(eob_code="ZZ14, ZZ22"),
        eob_map={"ZZ14": CLASS_DOCUMENTATION_REQUIRED},
    )
    assert CLASS_DOCUMENTATION_REQUIRED in result.classes
    assert CLASS_UNKNOWN_EOB in result.classes


@pytest.mark.parametrize(
    "cell,expected",
    [
        ("ZZ14", ["ZZ14"]),
        ("ZZ14, ZZ22", ["ZZ14", "ZZ22"]),
        ("ZZ14;ZZ22", ["ZZ14", "ZZ22"]),
        ("ZZ14 ZZ22", ["ZZ14", "ZZ22"]),
        ("", []),
        ("  ", []),
    ],
)
def test_split_eob_codes(cell, expected):
    assert split_eob_codes(cell) == expected


def test_eob_codes_match_case_insensitively():
    assert normalise_eob(" zz14 ") == "ZZ14"
    result = classify(_line(eob_code="zz14"),
                      eob_map={"ZZ14": CLASS_DUPLICATE_DENIAL})
    assert result.classes == [CLASS_DUPLICATE_DENIAL]


# --- arithmetic-derived -------------------------------------------------------


def test_negative_payment_is_a_reversal():
    result = classify(_line(amount_paid=Decimal("-27444.00")))
    assert CLASS_REVERSAL in result.classes
    assert "negative" in " ".join(result.reasons)


def test_reversal_suppresses_short_pay():
    """A clawback is not an underpayment. Reporting it as both would
    double-count one event — and the reversal is the story."""
    result = classify(_line(amount_paid=Decimal("-480.00"),
                            total_billed=Decimal("480.00")))
    assert CLASS_REVERSAL in result.classes
    assert CLASS_SHORT_PAY not in result.classes


def test_short_pay_when_paid_is_below_billed():
    result = classify(_line(
        total_billed=Decimal("120.00"),
        amt_eligible=Decimal("96.00"),
        amount_paid=Decimal("96.00"),
        pct_paid=Decimal("80"),
    ))
    assert CLASS_SHORT_PAY in result.classes
    reason = " ".join(result.reasons)
    assert "96" in reason and "120" in reason


def test_paid_in_full_is_not_a_short_pay():
    """The positive control that keeps the short-pay rule honest."""
    result = classify(_line(total_billed=Decimal("100.00"),
                            amount_paid=Decimal("100.00")))
    assert CLASS_SHORT_PAY not in result.classes


def test_overpayment_is_not_a_short_pay():
    result = classify(_line(total_billed=Decimal("100.00"),
                            amount_paid=Decimal("120.00")))
    assert CLASS_SHORT_PAY not in result.classes


def test_zero_paid_against_a_billed_line_surfaces():
    """Nothing paid at all is inside the ratified vocabulary as a short-pay
    (paid < billed) rather than a seventh class. What matters is that it
    surfaces rather than passing as clean."""
    result = classify(_line(amount_paid=Decimal("0.00")))
    assert result.needs_attention
    assert CLASS_SHORT_PAY in result.classes


def test_a_denial_suppresses_short_pay():
    """A denied line is not an underpaid line."""
    result = classify(
        _line(total_billed=Decimal("900.00"), amount_paid=Decimal("0.00"),
              eob_code="ZZ01"),
        eob_map={"ZZ01": CLASS_DUPLICATE_DENIAL},
    )
    assert CLASS_DUPLICATE_DENIAL in result.classes
    assert CLASS_SHORT_PAY not in result.classes


def test_a_line_with_no_billed_figure_is_not_guessed_at():
    result = classify(_line(total_billed=None, amount_paid=Decimal("0.00")))
    assert CLASS_SHORT_PAY not in result.classes


def test_identity_mismatch_is_reachable_through_the_map():
    """It is not derivable statement-side (detecting it needs a reference
    spelling, which is the P2 export). The class exists and the map reaches
    it — that is exactly the claim being made, no more."""
    result = classify(_line(eob_code="ZZ07"),
                      eob_map={"ZZ07": CLASS_IDENTITY_MISMATCH})
    assert result.classes == [CLASS_IDENTITY_MISMATCH]


@pytest.mark.parametrize("cls", sorted(ATTENTION_CLASSES))
def test_every_ratified_class_is_reachable(cls):
    """No class is vocabulary-only: each one can actually be produced."""
    if cls == CLASS_REVERSAL:
        result = classify(_line(amount_paid=Decimal("-1.00")))
    elif cls == CLASS_SHORT_PAY:
        result = classify(_line(amount_paid=Decimal("1.00")))
    else:
        result = classify(_line(eob_code="ZZ00"), eob_map={"ZZ00": cls})
    assert cls in result.classes


# --- the correction loop ------------------------------------------------------


def test_a_correction_overrides_the_derivation():
    line = _line(amount_paid=Decimal("50.00"))
    derived = classify(line)
    assert CLASS_SHORT_PAY in derived.classes

    corrections = {line.key: Correction(
        line_key=line.key,
        classes=[CLASS_RESUBMISSION_REQUIRED],
        operator="andrew",
    )}
    ruled = classify(line, corrections=corrections)
    assert ruled.classes == [CLASS_RESUBMISSION_REQUIRED]
    assert ruled.source == "operator"
    assert "andrew" in " ".join(ruled.reasons)


def test_a_correction_can_rule_a_line_clear():
    """How a false positive is retired — part 2 of the self-correcting
    standard. An empty ruling must CLEAR, not be ignored."""
    line = _line(amount_paid=Decimal("50.00"))
    assert classify(line).needs_attention

    corrections = {line.key: Correction(
        line_key=line.key, classes=[], operator="andrew",
        note="agreed rate, not a short pay",
    )}
    ruled = classify(line, corrections=corrections)
    assert ruled.classes == []
    assert not ruled.needs_attention
    assert ruled.source == "operator"


def test_corrections_round_trip_through_the_sidecar(tmp_path):
    path = tmp_path / "corrections.jsonl"
    append_correction(path, Correction(
        line_key="k1", classes=[CLASS_DUPLICATE_DENIAL], operator="andrew",
        eob_codes=["ZZ01"],
    ))
    loaded = load_corrections(path)
    assert set(loaded) == {"k1"}
    assert loaded["k1"].classes == [CLASS_DUPLICATE_DENIAL]
    assert loaded["k1"].eob_codes == ["ZZ01"]
    assert loaded["k1"].at  # stamped on append


def test_latest_ruling_supersedes_an_earlier_one(tmp_path):
    """The file is append-only, so a second ruling is the operator changing
    his mind and must win."""
    path = tmp_path / "corrections.jsonl"
    append_correction(path, Correction(
        line_key="k1", classes=[CLASS_DUPLICATE_DENIAL], operator="andrew"))
    append_correction(path, Correction(
        line_key="k1", classes=[CLASS_DOCUMENTATION_REQUIRED], operator="andrew"))
    loaded = load_corrections(path)
    assert loaded["k1"].classes == [CLASS_DOCUMENTATION_REQUIRED]


def test_appending_an_unknown_class_is_refused_with_the_reason(tmp_path):
    """A ruling naming a class nothing consumes would look recorded and do
    nothing. The positive control is in the same test: a KNOWN class is
    accepted, so this cannot pass against a build that refuses everything.
    """
    path = tmp_path / "corrections.jsonl"
    with pytest.raises(ValueError) as exc:
        append_correction(path, Correction(
            line_key="k1", classes=["invented_class"], operator="andrew"))
    assert "unknown attention class" in str(exc.value)
    assert not path.exists()

    append_correction(path, Correction(
        line_key="k1", classes=[CLASS_REVERSAL], operator="andrew"))
    assert load_corrections(path)["k1"].classes == [CLASS_REVERSAL]


def test_missing_corrections_file_is_an_empty_ruling_set():
    assert load_corrections("/tmp/definitely/not/here/corrections.jsonl") == {}


def test_correction_from_dict_is_schema_tolerant():
    corr = Correction.from_dict({
        "line_key": "k", "classes": [CLASS_REVERSAL], "operator": "a",
        "a_future_field": 1,
    })
    assert corr.classes == [CLASS_REVERSAL]
    assert not hasattr(corr, "a_future_field")


def test_correction_from_dict_coerces_a_bare_string_class():
    corr = Correction.from_dict({"line_key": "k", "classes": CLASS_REVERSAL})
    assert corr.classes == [CLASS_REVERSAL]


# --- proposals (part 3: surfaced for approval, never applied) -----------------


def test_repeated_rulings_become_a_proposal():
    corrections = {
        f"k{i}": Correction(
            line_key=f"k{i}", classes=[CLASS_DOCUMENTATION_REQUIRED],
            operator="andrew", eob_codes=["ZZ14"],
        )
        for i in range(2)
    }
    proposals = propose_eob_mappings(corrections)
    assert len(proposals) == 1
    assert proposals[0].code == "ZZ14"
    assert proposals[0].proposed_class == CLASS_DOCUMENTATION_REQUIRED
    assert proposals[0].supporting == 2
    assert proposals[0].conflicting == 0


def test_a_single_ruling_is_not_enough():
    """One ruling is as likely a one-off as a pattern. The positive control
    is the test above: two rulings DO propose."""
    corrections = {"k0": Correction(
        line_key="k0", classes=[CLASS_DOCUMENTATION_REQUIRED],
        operator="andrew", eob_codes=["ZZ14"])}
    assert propose_eob_mappings(corrections) == []


def test_an_already_mapped_code_is_not_proposed():
    """It is no longer a learning."""
    corrections = {
        f"k{i}": Correction(line_key=f"k{i}",
                            classes=[CLASS_DOCUMENTATION_REQUIRED],
                            operator="andrew", eob_codes=["ZZ14"])
        for i in range(3)
    }
    assert propose_eob_mappings(
        corrections, eob_map={"ZZ14": CLASS_DOCUMENTATION_REQUIRED}
    ) == []


def test_conflicting_rulings_are_reported_not_hidden():
    """'The operator has ruled this code three different ways' is exactly
    the thing worth seeing."""
    corrections = {
        "k0": Correction(line_key="k0", classes=[CLASS_DOCUMENTATION_REQUIRED],
                         operator="a", eob_codes=["ZZ14"]),
        "k1": Correction(line_key="k1", classes=[CLASS_DOCUMENTATION_REQUIRED],
                         operator="a", eob_codes=["ZZ14"]),
        "k2": Correction(line_key="k2", classes=[CLASS_DUPLICATE_DENIAL],
                         operator="a", eob_codes=["ZZ14"]),
    }
    proposals = propose_eob_mappings(corrections)
    assert len(proposals) == 1
    assert proposals[0].proposed_class == CLASS_DOCUMENTATION_REQUIRED
    assert proposals[0].conflicting == 1


def test_ruling_a_line_still_unknown_teaches_nothing_about_the_code():
    corrections = {
        f"k{i}": Correction(line_key=f"k{i}", classes=[CLASS_UNKNOWN_EOB],
                            operator="a", eob_codes=["ZZ14"])
        for i in range(3)
    }
    assert propose_eob_mappings(corrections) == []


def test_no_proposals_is_stated_not_silent():
    with structlog.testing.capture_logs() as captured:
        propose_eob_mappings({})
    events = [
        c for c in captured
        if c.get("event") == "reconcile.attention.proposals"
    ]
    assert len(events) == 1
    assert events[0]["proposals"] == 0
    assert "nothing to say" in events[0]["detail"]


# --- batch classification -----------------------------------------------------


def test_classify_all_logs_the_all_clean_case():
    """ILB: 'nothing needs attention' is a result, and saying so is what
    makes it distinguishable from a classifier that returned nothing."""
    with structlog.testing.capture_logs() as captured:
        results = classify_all([_line(), _line(claim_no="901")])
    assert all(not r.needs_attention for r in results)
    events = [
        c for c in captured
        if c.get("event") == "reconcile.attention.classified"
    ]
    assert len(events) == 1
    assert events[0]["flagged"] == 0
    assert events[0]["clean"] == 2
    assert "nothing needs attention" in events[0]["detail"]


def test_class_counts_are_deterministic():
    results = classify_all(
        [_line(amount_paid=Decimal("-1.00")), _line(amount_paid=Decimal("5.00"))]
    )
    counts = class_counts(results)
    assert counts == {CLASS_REVERSAL: 1, CLASS_SHORT_PAY: 1}
    assert list(counts) == sorted(counts)
