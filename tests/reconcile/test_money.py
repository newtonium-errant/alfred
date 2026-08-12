"""Cell parsing — the money, percent, unit and date readers.

The properties under test, in the order they matter:

  1. **Every accepted money spelling has its own fixture.** The house rule
     is that each failure mode triggering a fallback gets a test, because a
     fallback nobody exercises is one that is structurally broken and looks
     fine. :data:`~alfred.reconcile.money.MONEY_VARIANTS` names them and the
     first test asserts the list and the fixtures agree — so ADDING a
     variant without a fixture fails, which is the alarm that matters.
  2. **Negative is read as negative, in all three spellings.** A reversal
     read as a positive payment reports money as received that was clawed
     back. This is the highest-consequence single error in the module.
  3. **Absent is not zero.** A blank column and a 0.00 column are different
     facts, and a report that sums them the same way is wrong.
  4. **Ambiguity is refused, not guessed.** A slash date is refused without
     ``date_order``, and the refusal names the setting that fixes it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from alfred.reconcile.money import (
    ABSENT_CELL_VALUES,
    DATE_ORDER_DMY,
    DATE_ORDER_MDY,
    FULL_PERCENT,
    MONEY_VARIANTS,
    CellParseError,
    format_money,
    is_absent,
    parse_date,
    parse_int,
    parse_money,
    parse_percent,
)

#: One fixture per named variant. The KEY is the variant name from
#: MONEY_VARIANTS; the test below asserts this mapping covers it exactly.
MONEY_FIXTURES: dict[str, tuple[str, Decimal]] = {
    "plain": ("1234.56", Decimal("1234.56")),
    "thousands": ("1,234.56", Decimal("1234.56")),
    "currency": ("$1,234.56", Decimal("1234.56")),
    "currency_spaced": ("$ 1,234.56", Decimal("1234.56")),
    "leading_minus": ("-1,234.56", Decimal("-1234.56")),
    "unicode_minus": ("−1,234.56", Decimal("-1234.56")),
    "parenthesised": ("(1,234.56)", Decimal("-1234.56")),
    "currency_parens": ("$(1,234.56)", Decimal("-1234.56")),
    "trailing_minus": ("1,234.56-", Decimal("-1234.56")),
}


def test_every_named_money_variant_has_a_fixture():
    """The enumeration and the fixtures must not drift apart.

    Without this, a variant added to MONEY_VARIANTS with no fixture reads
    as covered — the list says nine, the tests exercise eight, and nothing
    says which. This is the guard that makes the per-variant tests below
    mean what they appear to mean.
    """
    assert set(MONEY_VARIANTS) == set(MONEY_FIXTURES)


@pytest.mark.parametrize("variant", MONEY_VARIANTS)
def test_money_variant_parses(variant):
    raw, expected = MONEY_FIXTURES[variant]
    assert parse_money(raw) == expected


@pytest.mark.parametrize("raw", sorted(ABSENT_CELL_VALUES))
def test_absent_cells_are_none_not_zero(raw):
    """An absent cell is None. The positive control is the line below it:
    a real zero parses to Decimal 0, so this pin cannot pass by the parser
    simply returning None for everything."""
    assert parse_money(raw) is None
    assert parse_money("0.00") == Decimal("0")


def test_absent_and_zero_are_distinguishable():
    assert is_absent("") and is_absent("—")
    assert not is_absent("0.00")
    assert parse_money("") is None
    assert parse_money("0.00") is not None


@pytest.mark.parametrize("raw", ["N0T-A-NUMBER", "abc", "1.2.3", "$", "--x"])
def test_unreadable_money_raises_with_the_field_named(raw):
    """A refusal must say WHICH field refused, not merely that one did —
    the skip list is read by someone hunting a row in a 460-line note."""
    with pytest.raises(CellParseError) as exc:
        parse_money(raw, field="amount_paid")
    assert "amount_paid" in str(exc.value)


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_money_is_refused(raw):
    """Decimal parses these happily; they are not money and would poison
    every sum they entered."""
    with pytest.raises(CellParseError):
        parse_money(raw)


def test_percent_forms():
    assert parse_percent("100") == FULL_PERCENT
    assert parse_percent("100%") == FULL_PERCENT
    assert parse_percent("80.0") == Decimal("80.0")
    assert parse_percent("—") is None


def test_percent_is_points_not_a_fraction():
    """80% is 80, never 0.8 — the statement prints points and the
    short-pay comparison is against FULL_PERCENT."""
    assert parse_percent("80") == Decimal("80")
    assert FULL_PERCENT == Decimal("100")


def test_units_accepts_whole_and_refuses_fractional():
    assert parse_int("2") == 2
    assert parse_int("2.0") == 2
    assert parse_int("") is None
    with pytest.raises(CellParseError) as exc:
        parse_int("2.5")
    assert "fractional" in str(exc.value)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-06-11", "2026-06-11"),
        ("11 Jun 2026", "2026-06-11"),
        ("11 June 2026", "2026-06-11"),
        ("Jun 11, 2026", "2026-06-11"),
        ("June 11 2026", "2026-06-11"),
    ],
)
def test_unambiguous_dates_parse(raw, expected):
    assert parse_date(raw) == expected


def test_slash_date_is_refused_without_an_order_and_names_the_setting():
    """The refusal has to be actionable: an operator reading it must learn
    what to set. Asserting only that it raised would pass against a parser
    that refuses slash dates for some entirely different reason."""
    with pytest.raises(CellParseError) as exc:
        parse_date("03/04/2026")
    message = str(exc.value)
    assert "ambiguous" in message
    assert "reconcile.date_order" in message


def test_slash_date_parses_both_ways_once_the_order_is_declared():
    """The positive control for the refusal above: the same input DOES
    parse when the ambiguity is resolved, and the two orders genuinely
    disagree — which is what made refusing it correct."""
    assert parse_date("03/04/2026", date_order=DATE_ORDER_DMY) == "2026-04-03"
    assert parse_date("03/04/2026", date_order=DATE_ORDER_MDY) == "2026-03-04"


def test_two_digit_year_in_a_slash_date():
    assert parse_date("03/04/26", date_order=DATE_ORDER_DMY) == "2026-04-03"


def test_impossible_calendar_date_is_refused():
    with pytest.raises(CellParseError) as exc:
        parse_date("2026-13-45")
    assert "not a real calendar date" in str(exc.value)


def test_unparseable_date_is_refused():
    with pytest.raises(CellParseError):
        parse_date("sometime last spring")


def test_format_money_renders_absent_as_a_dash_that_reparses_as_absent():
    """The renderer's output must survive its own parser: the placeholder
    it prints for an absent value has to be one the parser reads back as
    absent, or a round trip turns every blank cell into a zero."""
    rendered = format_money(None)
    assert rendered in ABSENT_CELL_VALUES or is_absent(rendered)
    assert parse_money(rendered) is None


def test_format_money_round_trips_a_negative():
    value = Decimal("-27444.00")
    assert parse_money(format_money(value)) == value
