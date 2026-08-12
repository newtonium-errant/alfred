"""Cell parsing for money, percentages and dates — every fallback stated.

This module exists because a payment statement is arithmetic the operator
will act on. Three decisions are worth reading before changing anything.

**Money is :class:`~decimal.Decimal`, never float.** A statement's figures
are summed per claimant, per statement and per report; binary floats turn
``0.1 + 0.2`` into ``0.30000000000000004`` and a reconciliation report that
disagrees with the provider's own total by a cent is a report nobody
trusts. Decimals are carried through the ledger as STRINGS (see
:mod:`alfred.reconcile.ledger`) so the round-trip is exact — a float in the
JSONL would reintroduce the same error at load time.

**Ambiguous dates are REFUSED, not guessed.** ``03/04/2026`` is 3 April
under ``dmy`` and 4 March under ``mdy``, and there is no way to tell from
the cell. Guessing would silently mis-date claim lines — and a date is part
of the ledger KEY, so a wrong guess does not just mislabel a row, it splits
one claim into two or merges two into one. The parser therefore refuses a
slash date unless the operator has declared the order in config
(``reconcile.date_order``), and the refusal message names that setting.
Unambiguous forms (ISO ``2026-06-11``, ``11 Jun 2026``) always parse.

**Every accepted variant is a NAMED fallback with its own fixture.** The
house rule (tokenizer-fallback fixture coverage) is that each failure mode
which triggers a fallback gets a test exercising it, because a fallback
nobody exercises is a fallback that is structurally broken. The variants
are enumerated in :data:`MONEY_VARIANTS` for exactly that reason: the list
is what the test file iterates.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

#: Cell contents that mean "no value here" rather than zero. A statement
#: leaves a column blank when it does not apply; reading that as ``0.00``
#: would make an absent deductible arithmetically indistinguishable from a
#: waived one, and the two are different facts.
ABSENT_CELL_VALUES = frozenset({
    "", "-", "--", "–", "—", "n/a", "na", "none", ".",
})

#: The money spellings this parser accepts, each a distinct fallback path.
#: Named so :mod:`tests.reconcile.test_money` can assert one fixture per
#: entry rather than trusting that a sampled few imply the rest.
MONEY_VARIANTS = (
    "plain",             # 1234.56
    "thousands",         # 1,234.56
    "currency",          # $1,234.56
    "currency_spaced",   # $ 1,234.56
    "leading_minus",     # -1,234.56
    "unicode_minus",     # −1,234.56  (U+2212, what some PDFs emit)
    "parenthesised",     # (1,234.56) — accounting negative
    "currency_parens",   # $(1,234.56)
    "trailing_minus",    # 1,234.56-
)

_CURRENCY_CHARS = "$£€¥"
#: Unicode minus (U+2212) and the ASCII hyphen both mean negative. PDF text
#: layers emit the former often enough that treating it as garbage would
#: reject real reversal rows — the highest-consequence rows in the file.
_MINUS_CHARS = "-−"

_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_SLASH_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$")
_TEXT_DATE_RE = re.compile(
    r"^(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(\d{4})$"
)
_TEXT_DATE_LEADING_MONTH_RE = re.compile(
    r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})$"
)

#: Month names, FULL and abbreviated. Both spellings are required: the
#: date regexes accept 3-9 letters, so a map holding only abbreviations
#: refuses "11 June 2026" while accepting "11 Jun 2026" — the same date
#: written the way a person writes it.
_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_MONTHS: dict[str, int] = {}
for _idx, _name in enumerate(_MONTH_NAMES, start=1):
    _MONTHS[_name] = _idx
    _MONTHS[_name[:3]] = _idx
#: The one abbreviation that is not a simple three-letter truncation.
_MONTHS["sept"] = 9

#: Accepted values for ``reconcile.date_order``. Deliberately has no
#: default — see the module docstring on refusing ambiguity.
DATE_ORDER_DMY = "dmy"
DATE_ORDER_MDY = "mdy"
DATE_ORDERS = frozenset({DATE_ORDER_DMY, DATE_ORDER_MDY})


class CellParseError(ValueError):
    """A cell could not be parsed. Carries the raw text for the skip list.

    Raised rather than returning a sentinel because the caller
    (:mod:`alfred.reconcile.parser`) must be able to tell "this column was
    blank" (a fact) from "this column held something I could not read" (a
    parse failure the operator has to see). Collapsing the two is how a
    partial parse becomes a silent one.
    """


def is_absent(text: str | None) -> bool:
    """Whether a cell means "no value", as opposed to zero."""
    if text is None:
        return True
    return text.strip().lower() in ABSENT_CELL_VALUES


def parse_money(text: str | None, *, field: str = "amount") -> Decimal | None:
    """Parse a money cell to :class:`~decimal.Decimal`.

    Returns ``None`` for an absent cell (see :data:`ABSENT_CELL_VALUES`) and
    raises :class:`CellParseError` for text that is present but unreadable.
    Every variant in :data:`MONEY_VARIANTS` is accepted.

    Negative is recognised three ways — a leading minus, a Unicode minus,
    and accounting parentheses — because reversal rows are the ones that
    matter most and each spelling appears in real remittance output. A
    reversal read as a positive payment is the single worst error this
    module could make: it would report money as received that was in fact
    clawed back.
    """
    if is_absent(text):
        return None
    raw = str(text).strip()

    negative = False

    # Accounting parentheses: (1,234.56) and $(1,234.56) both mean negative.
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
        raw = raw[1:-1].strip()
    else:
        # $(1,234.56) — currency symbol OUTSIDE the parens.
        stripped_currency = raw.lstrip(_CURRENCY_CHARS).strip()
        if stripped_currency.startswith("(") and stripped_currency.endswith(")"):
            negative = True
            raw = stripped_currency[1:-1].strip()

    # Currency symbol, either side, with or without a space.
    raw = raw.strip(_CURRENCY_CHARS).strip()

    # Trailing minus (1,234.56-) — some ledgers put the sign last.
    if raw and raw[-1] in _MINUS_CHARS:
        negative = True
        raw = raw[:-1].strip()

    if raw and raw[0] in _MINUS_CHARS:
        negative = True
        raw = raw[1:].strip()

    # A second currency strip: "$ -1,234.56" leaves "-1,234.56" above, and
    # "- $1,234.56" leaves "$1,234.56" here.
    raw = raw.strip(_CURRENCY_CHARS).strip()

    # Thousands separators. Spaces are used as separators in some locales
    # and are never meaningful inside a number here.
    raw = raw.replace(",", "").replace(" ", "").replace(" ", "")

    if not raw:
        raise CellParseError(
            f"{field}: {text!r} has no digits after stripping currency and "
            f"sign characters"
        )

    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise CellParseError(
            f"{field}: cannot read {text!r} as an amount"
        ) from exc

    if not value.is_finite():
        # Decimal happily parses "NaN" and "Infinity". Neither is money, and
        # both would poison every sum they entered.
        raise CellParseError(
            f"{field}: {text!r} parsed as a non-finite value, which is not "
            f"an amount"
        )

    return -value if negative else value


def parse_percent(text: str | None, *, field: str = "percent") -> Decimal | None:
    """Parse a ``% PD`` cell. ``"80%"``, ``"80"`` and ``"80.0"`` all give 80.

    Returns a percentage POINT value (80, not 0.8) because that is what the
    statement prints and what the operator reads. Callers comparing against
    "paid in full" compare to :data:`FULL_PERCENT`, never to ``1``.
    """
    if is_absent(text):
        return None
    raw = str(text).strip().rstrip("%").strip()
    raw = raw.replace(",", "")
    if raw and raw[0] in _MINUS_CHARS:
        raw = "-" + raw[1:]
    if not raw:
        raise CellParseError(f"{field}: {text!r} has no digits")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise CellParseError(
            f"{field}: cannot read {text!r} as a percentage"
        ) from exc
    if not value.is_finite():
        raise CellParseError(
            f"{field}: {text!r} parsed as a non-finite percentage"
        )
    return value


#: "Paid in full" in percentage points. Named so no caller writes a bare
#: ``100`` and no reader has to work out whether 1 or 100 was meant.
FULL_PERCENT = Decimal("100")


def parse_int(text: str | None, *, field: str = "units") -> int | None:
    """Parse a units cell. Accepts ``"2"`` and ``"2.0"``; refuses ``"2.5"``.

    A fractional unit count is refused rather than truncated: units drive
    per-unit arithmetic, and silently turning 2.5 into 2 would produce a
    figure that looks right and is wrong.
    """
    if is_absent(text):
        return None
    raw = str(text).strip().replace(",", "")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise CellParseError(
            f"{field}: cannot read {text!r} as a unit count"
        ) from exc
    if not value.is_finite():
        raise CellParseError(f"{field}: {text!r} is not a finite unit count")
    if value != value.to_integral_value():
        raise CellParseError(
            f"{field}: {text!r} is fractional — refused rather than "
            f"truncated, because a truncated unit count produces arithmetic "
            f"that looks correct and is not"
        )
    return int(value)


def parse_date(
    text: str | None,
    *,
    field: str = "date",
    date_order: str | None = None,
) -> str | None:
    """Parse a date cell to an ISO ``YYYY-MM-DD`` string.

    Accepts unambiguously: ISO (``2026-06-11``), day-first text
    (``11 Jun 2026``) and month-first text (``Jun 11, 2026``).

    A slash date (``03/04/2026``) is AMBIGUOUS and is refused unless
    ``date_order`` is ``"dmy"`` or ``"mdy"``. The refusal is the point: a
    date is part of the ledger key, so a wrong guess does not merely
    mislabel a row — it splits one claim into two ledger entries or merges
    two distinct claims into one.
    """
    if is_absent(text):
        return None
    raw = str(text).strip()

    m = _ISO_DATE_RE.match(raw)
    if m:
        return _build_date(
            int(m.group(1)), int(m.group(2)), int(m.group(3)), raw, field
        )

    m = _TEXT_DATE_RE.match(raw)
    if m:
        month = _MONTHS.get(m.group(2).strip(".").lower())
        if month is None:
            raise CellParseError(
                f"{field}: {text!r} — {m.group(2)!r} is not a month name"
            )
        return _build_date(int(m.group(3)), month, int(m.group(1)), raw, field)

    m = _TEXT_DATE_LEADING_MONTH_RE.match(raw)
    if m:
        month = _MONTHS.get(m.group(1).strip(".").lower())
        if month is None:
            raise CellParseError(
                f"{field}: {text!r} — {m.group(1)!r} is not a month name"
            )
        return _build_date(int(m.group(3)), month, int(m.group(2)), raw, field)

    m = _SLASH_DATE_RE.match(raw)
    if m:
        order = (date_order or "").strip().lower()
        if order not in DATE_ORDERS:
            raise CellParseError(
                f"{field}: {text!r} is ambiguous — 03/04 is 3 April under "
                f"day-first and 4 March under month-first, and the cell does "
                f"not say which. Refused rather than guessed because the date "
                f"is part of the ledger key. Set reconcile.date_order to "
                f"'dmy' or 'mdy' to accept this statement's convention."
            )
        a, b = int(m.group(1)), int(m.group(2))
        year = int(m.group(3))
        if year < 100:
            # Two-digit year. 26 -> 2026. Statements in this loop are
            # contemporary; a 19xx remittance is not a case worth guessing at.
            year += 2000
        day, month = (a, b) if order == DATE_ORDER_DMY else (b, a)
        return _build_date(year, month, day, raw, field)

    raise CellParseError(f"{field}: cannot read {text!r} as a date")


def _build_date(
    year: int, month: int, day: int, raw: str, field: str
) -> str:
    try:
        return date(year, month, day).isoformat()
    except ValueError as exc:
        raise CellParseError(
            f"{field}: {raw!r} is not a real calendar date ({exc})"
        ) from exc


def format_money(value: Decimal | None) -> str:
    """Render a Decimal for display: ``-27444.00`` -> ``-27,444.00``.

    ``None`` renders as an em-dash rather than ``0.00`` — the display has
    the same duty as the parser to keep "absent" distinct from "zero".
    """
    if value is None:
        return "—"
    quantised = value.quantize(Decimal("0.01"))
    return f"{quantised:,.2f}"


def today_iso() -> str:
    """Today in ISO form. One place, so tests can reason about it."""
    return datetime.now().date().isoformat()


__all__ = [
    "ABSENT_CELL_VALUES",
    "DATE_ORDERS",
    "DATE_ORDER_DMY",
    "DATE_ORDER_MDY",
    "FULL_PERCENT",
    "MONEY_VARIANTS",
    "CellParseError",
    "format_money",
    "is_absent",
    "parse_date",
    "parse_int",
    "parse_money",
    "parse_percent",
    "today_iso",
]
