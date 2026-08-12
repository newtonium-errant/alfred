"""A provider payment summary note -> ledger rows. Fail-loud by design.

**Column mapping is HEADER-DRIVEN, never positional.** The parser reads
each table's header row and maps columns by name (with synonyms), so a
statement that reorders, adds or omits a column still parses, and one that
uses an unrecognised heading says so instead of silently reading the wrong
column. Positional parsing of financial data is how a "Deduct" figure ends
up in the "Amount Paid" field with nothing to show for it.

**A partial parse must say what it skipped.** Every row the parser cannot
read is collected into :attr:`ParseResult.skipped` with its line number,
its raw text and a reason — and the CLI prints the count and the list. A
parser that reads 440 of 459 rows and reports success has not succeeded;
it has lost nineteen claim lines and told nobody. This is the
intentionally-left-blank rule applied to the case that matters most,
because the missing rows here are money.

**Re-runs are idempotent.** Rows are keyed content, not arrival events
(see :func:`alfred.reconcile.ledger.line_key`), so parsing the same note
twice produces the same keys and the second upsert reports every row
unchanged.

Structural quirks handled, each because the real source has them:

* per-claimant ``SUB-TOTAL`` rows interleaved with claim lines — kept as
  :data:`~alfred.reconcile.ledger.ROW_SUBTOTAL`, not discarded, so the
  report can cross-foot our sums against the provider's own
* ``(Ambulance Claims)`` appearing in the ``Claim #`` column instead of a
  number — kept verbatim; the occurrence tiebreak keeps two such lines
  from colliding
* OGST sibling lines sharing a claim number with their base benefit line
* negative ``Amount Paid`` values (reversals/clawbacks), in any of the
  spellings :mod:`alfred.reconcile.money` accepts
* ``Invoice #N`` inside the comments column — the P2 join key, extracted
  now so the ledger carries it from the first seed
* attribution ``BEGIN_INFERRED`` / ``END_INFERRED`` spans — rows inside
  one are flagged ``inferred``, preserving the distinction between a
  transcribed figure and an inferred one
* partial-page statements whose header the parser cannot find — their
  lines are still parsed and grouped under a synthesised header rather
  than dropped
* **bolded aggregates** — real statements bold their SUB-TOTAL and
  STATEMENT-TOTAL rows, amount AND label, so emphasis is stripped in the
  money layer for every cell (see
  :func:`alfred.reconcile.money.strip_emphasis`)
* **headerless continuation tables** — a multi-page statement resumes its
  rows without repeating the header, so a table whose first row is data
  inherits the previous table's column mapping, gated on an exact column
  count and logged as ``reconcile.parser.continuation_table``
* **two-column statement-totals blocks** — captured into
  :attr:`alfred.reconcile.ledger.Statement.declared_totals` and
  deliberately NOT assigned to ``payment_total``
* **full-width aggregate rows** — per-claimant sub-totals and grand totals
  printed with the SAME column count as claim lines and a WORD in the id
  cell, so neither the totals-block detector nor a literal-label check sees
  them. Discriminated on field population (see :func:`_looks_like_aggregate`)
  and captured as :data:`~alfred.reconcile.ledger.ROW_SUBTOTAL`. Absorbed as
  claim lines they double-count the provider's own arithmetic into ours,
  which is the one error a cross-foot cannot catch — it corrupts the number
  being checked.
* **two statements issued on one date** — each printed with its own header
  block. The statement key carries an occurrence
  (:func:`~alfred.reconcile.ledger.statement_key`) and so does the LINE key,
  because two same-day statements can each hold the same claim. Blocks fold
  only when their header facts are compatible; a conflict splits them, and
  both outcomes are logged with provenance.

**Validation status, because the earlier version of this note said the
opposite and a docstring that lies is worse than one that hedges.** Two
read-only dry runs against a genuine provider payment summary have now
happened. The first: 23 statements, 274 claim lines, 19 of 21 tables, 146
rows skipped — every loss named, and the three shapes it exposed are the
bold-aggregate, continuation and totals-block bullets above. The second,
after those fixes: **0 rows skipped, 414 claim lines** — and the cross-foot
then caught what it exists to catch, one statement 45,178.00 out,
decomposing exactly into absorbed aggregate rows (28,284.00) and a wrongly
folded same-day statement (16,894.00). Those are the last two bullets.

The pattern across both rounds is worth keeping: **every shape that cost us
money was one the fixtures did not contain**, and in each case a fully green
suite said nothing, because a fixture set that omits a shape cannot fail on
it. The parse layer is now confirmed against real input; the fixtures carry
each shape with invented content.

The COLUMN mapping remains robust by construction — it keys on names, and
unknown headings are reported through
:attr:`ParseResult.unmapped_headings` rather than silently read as some
other column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import structlog

from alfred.vault.attribution import _BEGIN_RE as _INFERRED_BEGIN_RE

from .ledger import (
    ROW_CLAIM,
    ROW_SUBTOTAL,
    ClaimLine,
    Statement,
    line_key,
)
from .money import (
    CellParseError,
    is_absent,
    parse_date,
    parse_int,
    parse_money,
    parse_percent,
    strip_emphasis,
)

log = structlog.get_logger(__name__)

#: END marker for an inferred span. The BEGIN pattern is imported from
#: :mod:`alfred.vault.attribution` rather than re-spelled — the marker
#: contract has one author, and a second copy of the regex is how a writer
#: and a reader drift onto different shapes.
_INFERRED_END_RE = re.compile(r"<!--\s*END_INFERRED\b")

#: Column synonyms. The key is the normalised heading (lowercased, ``%``
#: spelled ``pct``, every non-alphanumeric character removed); the value is
#: the :class:`~alfred.reconcile.ledger.ClaimLine` field it fills.
#:
#: Matching is EXACT on the normalised form, never substring: "paid" is a
#: substring of both "amount paid" and "% pd", and a substring match would
#: put the percentage in the money column on any statement that abbreviates.
COLUMN_SYNONYMS: dict[str, str] = {
    # Claim number
    "claim": "claim_no", "claimno": "claim_no", "claimnum": "claim_no",
    "claimnumber": "claim_no", "claimhash": "claim_no",
    # Date of service
    "dateofservice": "dos", "dos": "dos", "servicedate": "dos",
    "datedeservice": "dos",
    # Names
    "surname": "surname", "lastname": "surname", "last": "surname",
    "firstname": "first_name", "first": "first_name",
    "givenname": "first_name",
    # Benefit
    "benefitcode": "benefit_code", "benefit": "benefit_code",
    "code": "benefit_code", "procedurecode": "benefit_code",
    # Units
    "units": "units", "unit": "units", "qty": "units", "quantity": "units",
    # Money columns
    "totalbilled": "total_billed", "billed": "total_billed",
    "totalsubmitted": "total_billed", "submitted": "total_billed",
    "amtsubmitted": "total_billed",
    "amtexcluded": "amt_excluded", "amountexcluded": "amt_excluded",
    "excluded": "amt_excluded",
    "deduct": "deduct", "deductible": "deduct",
    "amteligible": "amt_eligible", "amounteligible": "amt_eligible",
    "eligible": "amt_eligible",
    "pctpd": "pct_paid", "pctpaid": "pct_paid", "percentpaid": "pct_paid",
    "pd": "pct_paid", "pct": "pct_paid",
    "amountpaid": "amount_paid", "amtpaid": "amount_paid",
    "paid": "amount_paid", "payment": "amount_paid",
    # EOB + comments
    "eob": "eob_code", "eobcode": "eob_code", "eobs": "eob_code",
    "explanation": "eob_code", "explanationofbenefits": "eob_code",
    "comments": "comments", "comment": "comments", "notes": "comments",
    "note": "comments", "remarks": "comments",
}

#: Statement-header metadata synonyms, same normalisation as the columns.
STATEMENT_FIELD_SYNONYMS: dict[str, str] = {
    "provider": "provider", "providername": "provider",
    "practitioner": "provider",
    "company": "company", "companyname": "company", "clinic": "company",
    "payee": "company",
    "paymenttotal": "payment_total", "totalpayment": "payment_total",
    "totalpaid": "payment_total", "chequeamount": "payment_total",
    # "Statement Total Paid" — added on EVIDENCE, not inference. The real
    # note prints it as a bolded header label, and the same figure appears
    # twice more in that statement's two-column totals block under two
    # other labels. Three spellings of one number is corroboration; a
    # plausible-looking label alone would not have been, and the
    # unknown-label capture below exists precisely so a label can wait in
    # declared_totals until evidence like this arrives.
    "statementtotalpaid": "payment_total",
    "checkamount": "payment_total", "amountofpayment": "payment_total",
    "statementdate": "statement_date", "paymentdate": "statement_date",
    "date": "statement_date", "chequedate": "statement_date",
    "checkdate": "statement_date",
    # Capture provenance, written by this package's own renderer. Keying on
    # a structured field rather than prose is what makes render -> parse ->
    # render a fixed point: a fact stated only in prose is dropped on the
    # second regeneration, and the note quietly sheds its provenance.
    "provenance": "inferred",
}

#: The leading token of a ``**Provenance:**`` line that means the figures
#: were inferred at capture rather than transcribed. Shared with
#: :mod:`alfred.reconcile.render`, which writes it — a second spelling on
#: the writing side is how a renderer and its parser drift apart.
INFERRED_TOKEN = "INFERRED"

#: Where a statement date came from. A provider-printed date and one
#: recovered from a scan batch's own label are NOT the same claim, and
#: collapsing them would let a mistyped batch label re-home every claim
#: line under it with no way to tell.
DATE_SOURCE_HEADER = "header"
DATE_SOURCE_CAPTURE = "capture_metadata"

#: ``Page 3 of 4`` in a heading. ``N > 1`` means the provider is stating,
#: in the document, that this block continues a statement already begun —
#: the positive evidence a fold needs when the block carries no date.
_PAGE_RE = re.compile(r"\bpage\s+(\d+)\s+of\s+(\d+)\b", re.IGNORECASE)

#: ``Statement: 05 Jun 2026`` inside a scan-batch attribution comment. This
#: is CAPTURE metadata, not provider content — see :data:`DATE_SOURCE_CAPTURE`.
_CAPTURE_DATE_RE = re.compile(
    r"statement\s*:\s*([0-9]{1,2}\s+[A-Za-z]{3,9}\.?\s+[0-9]{4}"
    r"|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2})",
    re.IGNORECASE,
)

#: A claim table must map these before the parser will read data rows from
#: it. Without an amount-paid column there is nothing to reconcile, and
#: without a date of service there is no key — so a table missing either is
#: reported as unrecognised rather than half-read.
REQUIRED_COLUMNS = frozenset({"amount_paid", "dos"})

#: Normalised first-cell values that mark a row as the provider's own
#: arithmetic rather than a claim line.
_SUBTOTAL_MARKERS = ("subtotal", "subtotals", "total", "totals", "grandtotal")

#: The second heading of a two-column statement-totals block.
_TOTALS_AMOUNT_HEADINGS = frozenset({"amount", "amt", "total", "value"})
#: Its first heading, which is usually blank — the labels are in the rows.
_TOTALS_LABEL_HEADINGS = frozenset({
    "", "description", "item", "label", "type", "statement",
})


def _is_totals_block_header(cells: list[str]) -> bool:
    """Whether this row heads a two-column key-value totals block.

    Exactly two columns, an amount-ish second heading and a blank-or-label
    first one. Deliberately narrow: this branch diverts a table away from
    claim-line parsing entirely, so it must not fire on anything that could
    be a claim table. A two-column table headed ``Date of Service | Amount
    Paid`` maps the required columns and is caught by the header check
    BEFORE this one ever runs — order matters here and is why this is not
    the first test in the chain.
    """
    if len(cells) != 2:
        return False
    return (
        normalise_heading(cells[0]) in _TOTALS_LABEL_HEADINGS
        and normalise_heading(cells[1]) in _TOTALS_AMOUNT_HEADINGS
    )

_INVOICE_RE = re.compile(r"(?:invoice|inv)\.?\s*#?\s*(\d+)", re.IGNORECASE)
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
#: Metadata is matched AFTER emphasis markers are stripped (see
#: :func:`_strip_emphasis`), so this pattern never has to reason about
#: where the asterisks fell. ``**Provider:** Wren`` and ``Provider: Wren``
#: reach it as the same string — trying to encode both shapes in the regex
#: is how the value ends up captured as ``"** Wren"``.
_META_RE = re.compile(
    r"^\s*[-*]?\s*([A-Za-z][A-Za-z0-9 /#%._-]{0,48}?)\s*:\s*(.+?)\s*$"
)
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_ISO_IN_TEXT_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")


#: Emphasis stripping has ONE author, in the money layer, and this module
#: imports it. It used to be a private copy here; the copy was harmless only
#: because the two happened to agree, which is not a property anyone was
#: maintaining. The rule now lives beside the cell parsers that consume it.
_strip_emphasis = strip_emphasis


def normalise_heading(text: str) -> str:
    """Lowercase, ``%`` -> ``pct``, ``#`` -> ``hash``, drop the rest.

    ``"% PD"`` -> ``"pctpd"``, ``"Claim #"`` -> ``"claimhash"``,
    ``"Amt. Eligible"`` -> ``"amteligible"``.
    """
    t = (text or "").strip().lower()
    t = t.replace("%", "pct").replace("#", "hash")
    return re.sub(r"[^a-z0-9]+", "", t)


def split_table_row(line: str) -> list[str]:
    """Split a Markdown table row into cells, honouring ``\\|`` escapes.

    A naive ``line.split("|")`` breaks any cell containing an escaped pipe —
    and the comments column is free text, which is exactly where one shows
    up. Escaped pipes are restored to literal ``|`` in the returned cells.
    """
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]

    cells: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf).strip())
    return cells


def is_table_row(line: str) -> bool:
    return line.strip().startswith("|")


def parse_invoice_no(comments: str) -> str:
    """Extract ``Invoice #163`` -> ``"163"`` from a comments cell.

    Returns ``""`` when the cell names no invoice. Only the FIRST match is
    taken: a comment mentioning two invoices is genuinely ambiguous about
    which one the line belongs to, and picking one silently would create a
    join the operator never made. The full comment stays on the row, so the
    ambiguity is visible in the report.
    """
    m = _INVOICE_RE.search(comments or "")
    return m.group(1) if m else ""


@dataclass
class SkippedRow:
    """A row the parser could not read, with everything needed to find it."""

    line_no: int
    raw: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"line_no": self.line_no, "raw": self.raw, "reason": self.reason}


@dataclass
class ParseResult:
    """Everything one note yielded, including what it did NOT yield."""

    statements: list[Statement] = field(default_factory=list)
    claim_lines: list[ClaimLine] = field(default_factory=list)
    subtotals: list[ClaimLine] = field(default_factory=list)
    skipped: list[SkippedRow] = field(default_factory=list)
    #: Keys that needed a non-zero occurrence to stay distinct. Surfaced so
    #: a disambiguated key is never a silent event.
    collisions: list[str] = field(default_factory=list)
    #: Table headings that matched no known column, per table. A populated
    #: list on the first seed is the signal that the synonym map needs a row.
    unmapped_headings: list[str] = field(default_factory=list)
    #: Header labels carrying money that matched no known statement field.
    #: A populated list is the signal that STATEMENT_FIELD_SYNONYMS needs a
    #: row — the figure is captured meanwhile, never dropped.
    unmapped_header_labels: list[str] = field(default_factory=list)
    #: Line numbers of headerless continuation tables whose column mapping was
    #: inherited from the previous table. Surfaced for the same reason
    #: collisions are: an inherited mapping is an inference, and an inference
    #: the operator cannot see is one nobody can check.
    continuations: list[int] = field(default_factory=list)
    #: Line numbers of full-width rows reclassified as the provider's own
    #: arithmetic on field population rather than on a literal label. Same
    #: reason as ``continuations``: an inference that moves money between the
    #: claim sum and the cross-foot inputs must be visible.
    aggregate_rows: list[int] = field(default_factory=list)
    #: Line numbers of statement blocks whose date was recovered from scan
    #: batch metadata rather than printed by the provider. Surfaced because
    #: it is a weaker claim, and one the operator cannot see is one he
    #: cannot weigh.
    capture_dated: list[int] = field(default_factory=list)
    #: Same-date statement blocks that were kept APART because their header
    #: facts conflicted, as ``(date, occurrence, reason)``.
    statement_splits: list[tuple[str, int, str]] = field(default_factory=list)
    #: Same-date statement blocks folded together as one statement, as
    #: ``(date, occurrence)``. A fold is the legitimate re-printed-header
    #: case; it is logged anyway so a WRONG fold is never silent.
    statement_folds: list[tuple[str, int]] = field(default_factory=list)
    tables_seen: int = 0
    tables_parsed: int = 0

    @property
    def ok(self) -> bool:
        """No rows lost. Note this is about LOSS, not about attention."""
        return not self.skipped

    def summary(self) -> str:
        """A one-line human summary. Never empty — see the ILB rule."""
        if not self.tables_seen:
            return (
                "Parsed the note and found no claim tables at all. If the "
                "note does contain statements, the table headers did not "
                "match any known column names — check "
                "reconcile.parser.COLUMN_SYNONYMS."
            )
        parts = [
            f"{len(self.statements)} statement(s)",
            f"{len(self.claim_lines)} claim line(s)",
            f"{len(self.subtotals)} subtotal row(s)",
        ]
        if self.skipped:
            parts.append(f"{len(self.skipped)} row(s) SKIPPED")
        else:
            parts.append("0 rows skipped")
        if self.collisions:
            parts.append(f"{len(self.collisions)} key collision(s) disambiguated")
        return ", ".join(parts) + "."


def _map_columns(
    header_cells: list[str],
) -> tuple[dict[int, str], list[str]]:
    """``({column_index: field_name}, [unmapped headings])``."""
    mapping: dict[int, str] = {}
    unmapped: list[str] = []
    for idx, cell in enumerate(header_cells):
        norm = normalise_heading(cell)
        if not norm:
            continue
        field_name = COLUMN_SYNONYMS.get(norm)
        if field_name is None:
            unmapped.append(cell.strip())
            continue
        # First mapping wins. A statement with two columns normalising to
        # the same field (e.g. "Paid" and "Amount Paid") is malformed; taking
        # the first and reporting the second as unmapped keeps the anomaly
        # visible instead of letting the later column overwrite the earlier.
        if field_name in mapping.values():
            unmapped.append(cell.strip())
            continue
        mapping[idx] = field_name
    return mapping, unmapped


def _has_subtotal_label(cells: list[str], mapping: dict[int, str]) -> bool:
    """Whether an identity cell carries a literal SUB-TOTAL / TOTAL label."""
    # Check the identity-ish columns: a subtotal row typically carries the
    # label where the claim number or the surname would be.
    for idx, field_name in mapping.items():
        if field_name not in ("claim_no", "surname", "first_name"):
            continue
        if idx >= len(cells):
            continue
        norm = normalise_heading(cells[idx])
        if any(norm.startswith(marker) for marker in _SUBTOTAL_MARKERS):
            return True
    # Some statements put the label in the first cell regardless of column.
    if cells:
        norm_first = normalise_heading(cells[0])
        if any(norm_first.startswith(marker) for marker in _SUBTOTAL_MARKERS):
            return True
    return False


#: Header facts compared when deciding whether two same-date blocks are one
#: statement re-printed or two statements issued the same day. Only fields a
#: provider would not vary WITHIN one statement belong here.
_STATEMENT_IDENTITY_FIELDS = ("provider", "company", "payment_total")


def _header_conflict(a: Statement, b: Statement) -> str | None:
    """The first conflicting header fact between two blocks, or ``None``.

    ``None`` means COMPATIBLE — every field is either absent on one side or
    equal on both. A field present on one side and absent on the other is
    NOT a conflict: that is exactly what a continuation header looks like.

    Compatibility is NECESSARY BUT NOT SUFFICIENT for a fold. See
    :func:`_fold_evidence` — two blocks that agree about nothing are
    compatible and are not the same statement.
    """
    for name in _STATEMENT_IDENTITY_FIELDS:
        left, right = getattr(a, name), getattr(b, name)
        if left in (None, "") or right in (None, ""):
            continue
        if left != right:
            return f"{name} differs ({left!r} vs {right!r})"
    return None


def _fold_evidence(prior: Statement, block: Statement) -> str | None:
    """What POSITIVELY identifies ``block`` as part of ``prior``, or ``None``.

    THE rule this parser folds on, and the correction that the real note
    forced: **a fold requires positive evidence of identity, not merely the
    absence of contradiction.**

    The bc1c rule folded any pair of same-date blocks with no conflicting
    header fact. For DATED blocks that was accidentally sound — the shared
    date IS positive evidence. For UNDATED ones it was not: two empty
    headers contradict nothing, so every undated block folded into a single
    pool, and on the real note that pool swallowed 175 claim lines (~45% of
    the note) under one dateless statement with no declared total.

    That is the same error as an empty claimant matching an empty aggregate
    label — two blanks agreeing, called an identity. It was fixed one tier
    down in bc1c and reappeared here, which is why the rule is now stated
    once, positively, rather than as a list of things that would refuse it.

    Two kinds of evidence, strongest first:

    * ``page_continuation`` — the block is headed ``Page N of M`` with
      ``N > 1``. The provider is saying, in the document, that this is a
      later page of a statement already begun. Strong enough to fold a
      block that carries NO date of its own, which is exactly the case
      that was pooling.
    * ``same_date`` — both blocks name the same statement date.

    A block with neither stands alone, however compatible it looks.
    """
    if block.page_continuation:
        return "page_continuation"
    if block.statement_date and block.statement_date == prior.statement_date:
        return "same_date"
    return None


def _looks_like_aggregate(row: ClaimLine) -> bool:
    """Whether a FULL-WIDTH row is an aggregate wearing a claim row's shape.

    Real statements print per-claimant sub-totals and a grand total as rows
    with the SAME column count as claim lines, so the two-column totals-block
    detector cannot see them and a literal-label check misses any whose id
    cell is a word rather than the string "SUB-TOTAL". Absorbed as claim
    lines they inflate the statement's paid sum by their own value — the
    provider's arithmetic double-counted into ours, which is the one error a
    cross-foot cannot catch, because it corrupts the very number being
    checked.

    The discriminator is the FIELD POPULATION, which is deterministic and
    already parsed: an aggregate carries no date of service, no benefit code
    and no unit count, and its identity cell holds no digit. A real claim
    line has all three, whatever its id looks like.

    That last clause is what keeps this safe. Claim rows whose id is a
    parenthetical (``(Ambulance Claims)``) also hold no digit — and they are
    KEPT, because they are fully populated. The rule tests for the absence
    of claim-shaped data, never for the shape of the id alone.

    Deliberately conservative: every one of the three fields must be empty.
    A row missing only its benefit code is a claim line with a gap, and
    treating it as an aggregate would delete a real payment from the ledger.
    Erring toward "claim line" leaves a visible oddity; erring toward
    "aggregate" loses money quietly.
    """
    if any(ch.isdigit() for ch in (row.claim_no or "")):
        return False
    return not (row.dos or row.benefit_code or row.units is not None)


def parse_note(
    text: str,
    *,
    source_note: str = "",
    batch_id: str = "",
    session: str = "",
    capture_ref: str = "",
    date_order: str | None = None,
) -> ParseResult:
    """Parse a payment-summary note into ledger rows.

    ``date_order`` is passed through to :func:`alfred.reconcile.money.parse_date`
    and is only consulted for slash-form dates, which are refused without it.
    """
    result = ParseResult()
    lines = (text or "").splitlines()

    current_stmt: Statement | None = None
    mapping: dict[int, str] = {}
    header_width = 0
    in_table = False
    inferred_depth = 0
    #: Line number of a table header this parser refused, so the rows under
    #: it can name the cause instead of each re-reporting it.
    rejected_header_line: int | None = None
    #: The last ACCEPTED column mapping, its width, and the statement it was
    #: read under. A headerless continuation table inherits these; the
    #: statement is kept only so the log can say whether the inheritance
    #: crossed a statement boundary.
    last_mapping: dict[int, str] = {}
    last_header_width = 0
    last_mapping_stmt: Statement | None = None
    #: True while reading a two-column statement-totals block.
    in_totals_block = False
    #: Most recent statement date seen in a scan-batch attribution comment.
    pending_capture_date: str = ""
    #: Header facts printed above a heading, awaiting the block they belong to.
    pending_header: Statement | None = None
    #: True between a statement's opening and its first table row. Unknown
    #: header labels carrying money are only captured inside this window —
    #: prose after the rows must not be able to inject a phantom total.
    #: Starts TRUE: a note can open with header facts before any heading,
    #: which is exactly the variant that lost a declared total.
    in_statement_header = True
    # Occurrence counters, per (statement_date, claim_no, dos, benefit_code).
    # Claim lines and subtotal rows count SEPARATELY: they live in separate
    # indexes in the ledger, and sharing one counter would make a claim
    # line's occurrence depend on how many subtotal rows happened to precede
    # it — which is not what "the Nth line sharing this key" should mean.
    occurrences: dict[tuple[str, str, str, str], int] = {}
    sub_occurrences: dict[tuple[str, str, str, str], int] = {}
    claims_for_current: list[ClaimLine] = []
    subs_for_current: list[ClaimLine] = []

    def _flush_statement() -> None:
        """Emit the current statement, unless it is a heading with nothing
        under it.

        A document's title heading (``# Provider Payment Summary``) is a
        heading like any other and would otherwise mint an empty statement
        carrying no date, no provider and no lines — which then renders as
        a phantom section and inflates every statement count in the report.
        A statement is real if it has claim lines OR any header fact.
        """
        nonlocal current_stmt
        if current_stmt is None:
            return
        if (
            not claims_for_current
            and not current_stmt.statement_date
            and (
                current_stmt.declared_totals
                or current_stmt.provider
                or current_stmt.company
                or current_stmt.payment_total is not None
            )
        ):
            # HEADER FACTS PRINTED ABOVE THE HEADING. The real note's header
            # variant puts an address line and a bolded total BEFORE the
            # `## ...` that opens the statement, so this block holds facts
            # and no rows. Flushing it as its own statement would mint a
            # dateless, rowless phantom; dropping it loses the total — which
            # is what happened, to the tune of a declared figure the
            # statement then had none of. It is CARRIED FORWARD to the block
            # the heading opens, which is whose facts they are.
            nonlocal pending_header
            pending_header = current_stmt
            log.info(
                "reconcile.parser.header_facts_carried",
                line_no=current_stmt.source_line,
                declared_totals=list(current_stmt.declared_totals),
                detail="a header block with facts but no rows and no date — "
                       "its facts belong to the statement that follows",
            )
            current_stmt = None
            return

        has_content = bool(claims_for_current) or any((
            current_stmt.statement_date,
            current_stmt.provider,
            current_stmt.company,
            current_stmt.payment_total is not None,
        ))
        if has_content:
            current_stmt.claim_line_count = len(claims_for_current)
            folded_onto = _assign_statement_occurrence(current_stmt)
            # Stamp this block's rows with the occurrence they belong to.
            # Done HERE, not at row-build time, because the fold/split
            # decision needs the whole header block read first.
            for r in (*claims_for_current, *subs_for_current):
                r.statement_occurrence = current_stmt.statement_occurrence
                if not r.statement_date and current_stmt.statement_date:
                    # The row was built before the block's date was known
                    # (a page continuation inherits its parent's at fold
                    # time). Without this the rows keep an empty date and
                    # group under the "(no date)" pool regardless.
                    r.statement_date = current_stmt.statement_date
            if folded_onto is None:
                result.statements.append(current_stmt)
            else:
                _merge_into(folded_onto, current_stmt)
        current_stmt = None

    def _merge_into(prior: Statement, extra: Statement) -> None:
        """Fold a continuation block's header facts onto the block it continues.

        Appending BOTH would leave two Statement rows sharing one key, and
        every downstream reader that indexes by key keeps whichever it saw
        last — which is how the continuation's empty header silently erased
        the real block's declared payment total, and how its claim lines
        landed under a statement whose own count said it had fewer.

        Merge direction: the prior block wins on any field it already has,
        and the continuation fills only what is empty. A continuation
        repeating the header restates the same facts, so this is a no-op in
        the common case; where it is not a no-op, the FIRST statement of a
        fact is the one printed with the claim lines it describes.
        """
        for name in _STATEMENT_IDENTITY_FIELDS:
            if getattr(prior, name) in (None, "") and getattr(extra, name) not in (None, ""):
                setattr(prior, name, getattr(extra, name))
        for label, value in (extra.declared_totals or {}).items():
            prior.declared_totals.setdefault(label, value)
        # Counts ADD. The declared count is per printed block; the ledger
        # holds the union, and the report cross-foots the two against each
        # other — so a sum here is what makes that check meaningful rather
        # than a guaranteed mismatch on every folded statement.
        prior.claim_line_count += extra.claim_line_count
        prior.inferred = prior.inferred or extra.inferred

    def _assign_statement_occurrence(stmt: Statement) -> Statement | None:
        """Decide whether this block folds onto a same-date one, or splits.

        DEFAULT IS TO SPLIT. The asymmetry decides it: two statements where
        the provider issued one is visible and recoverable — the operator
        sees a duplicate and says so. One statement where the provider
        issued two silently swallows the second's payment total and
        attributes its claim lines to the first, which is what happened on
        the real note and what put a statement 16,894 out.

        A block folds only on POSITIVE EVIDENCE of identity — see
        :func:`_fold_evidence`. Compatibility alone is not enough: two
        blocks that contradict each other about nothing agree about nothing
        either, and treating that as identity is what pooled 175 undated
        claim lines into one dateless statement on the real note.

        Returns the statement this block FOLDS ONTO, or ``None`` when it
        stands alone. The caller merges rather than appending in the fold
        case — two Statement rows sharing one key is how a continuation's
        empty header erases the real block's declared total.
        """
        # A page continuation attaches to the statement it continues — the
        # most recent one — rather than to a same-date cohort, because its
        # own header usually carries no date at all. That is the case the
        # date-cohort search structurally cannot serve.
        if stmt.page_continuation and result.statements:
            candidates = [result.statements[-1]]
        else:
            candidates = [
                s for s in result.statements
                if s.statement_date == stmt.statement_date
            ]

        if not candidates:
            stmt.statement_occurrence = 0
            return None

        for prior in candidates:
            evidence = _fold_evidence(prior, stmt)
            if evidence is None:
                continue
            conflict = _header_conflict(prior, stmt)
            if conflict is not None:
                continue
            if not stmt.statement_date:
                # A page continuation inherits its parent's date. Without
                # this the folded rows keep an empty statement_date and
                # group_by_statement files them under the "(no date)" pool
                # anyway — the fold would be recorded and have no effect.
                stmt.statement_date = prior.statement_date
                stmt.date_source = prior.date_source or DATE_SOURCE_HEADER
            stmt.statement_occurrence = prior.statement_occurrence
            result.statement_folds.append(
                (stmt.statement_date, prior.statement_occurrence)
            )
            log.info(
                "reconcile.parser.statement_fold",
                statement_date=stmt.statement_date,
                occurrence=prior.statement_occurrence,
                evidence=evidence,
                prior_line=prior.source_line,
                folded_line=stmt.source_line,
                claim_lines=stmt.claim_line_count,
                detail="block folded onto a prior statement on positive "
                       "evidence of identity; its rows join that block",
            )
            return prior

        occurrence = (
            max(s.statement_occurrence for s in candidates) + 1
            if candidates else 0
        )
        stmt.statement_occurrence = occurrence
        reason = (
            _header_conflict(candidates[-1], stmt)
            or "no positive evidence that these blocks are one statement"
        )
        result.statement_splits.append(
            (stmt.statement_date, occurrence, reason)
        )
        log.warning(
            "reconcile.parser.statement_split",
            statement_date=stmt.statement_date,
            occurrence=occurrence,
            reason=reason,
            prior_line=candidates[-1].source_line,
            split_line=stmt.source_line,
            claim_lines=stmt.claim_line_count,
            detail="a second statement was issued on this date — kept "
                   "separate so its payment total is not overwritten and "
                   "its claim lines are not attributed to the first",
        )
        return None

    def _begin_statement(line_no: int) -> Statement:
        nonlocal current_stmt, claims_for_current, subs_for_current
        nonlocal in_statement_header
        _flush_statement()
        claims_for_current = []
        subs_for_current = []
        nonlocal pending_header
        in_statement_header = True
        current_stmt = Statement(
            source_note=source_note,
            source_line=line_no,
            batch_id=batch_id,
            session=session,
            capture_ref=capture_ref,
            inferred=inferred_depth > 0,
        )
        if pending_header is not None:
            # Seed from the header facts printed above this heading.
            current_stmt.declared_totals.update(pending_header.declared_totals)
            for _f in _STATEMENT_IDENTITY_FIELDS:
                if getattr(current_stmt, _f) in (None, ""):
                    setattr(current_stmt, _f, getattr(pending_header, _f))
            pending_header = None
        return current_stmt

    for i, raw_line in enumerate(lines):
        line_no = i + 1
        stripped = raw_line.strip()

        # Attribution spans. Tracked before anything else so a marker on the
        # same line as content still flags that content.
        capture_hit = _CAPTURE_DATE_RE.search(raw_line)
        if capture_hit and stripped.startswith("<!--"):
            try:
                iso = parse_date(capture_hit.group(1), date_order=date_order)
            except CellParseError:
                iso = None
            if iso:
                pending_capture_date = iso

        if _INFERRED_BEGIN_RE.search(raw_line):
            inferred_depth += 1
            continue
        if _INFERRED_END_RE.search(raw_line):
            inferred_depth = max(0, inferred_depth - 1)
            continue

        if not stripped:
            in_table = False
            in_totals_block = False
            continue

        # A heading always starts a new statement context.
        heading = _HEADING_RE.match(stripped)
        if heading:
            in_table = False
            in_totals_block = False
            stmt = _begin_statement(line_no)
            stmt.inferred = inferred_depth > 0
            # A heading often carries the statement date: "## Statement —
            # 11 Jun 2026". Try it, but never fail the note over a heading.
            title = heading.group(1).strip()
            page = _PAGE_RE.search(title)
            if page and int(page.group(1)) > 1:
                stmt.page_continuation = True
            found = _date_from_free_text(title, date_order)
            if found:
                stmt.statement_date = found
                stmt.date_source = DATE_SOURCE_HEADER
            elif pending_capture_date and not stmt.page_continuation:
                # No printed date. Fall back to the scan batch's own label,
                # recorded as the WEAKER provenance it is. Page continuations
                # are excluded: they inherit their parent's date at fold time,
                # which is a stronger claim than a batch label.
                stmt.statement_date = pending_capture_date
                stmt.date_source = DATE_SOURCE_CAPTURE
                pending_capture_date = ""
                result.capture_dated.append(line_no)
                log.info(
                    "reconcile.parser.date_from_capture_metadata",
                    line_no=line_no,
                    # The STATEMENT's date, not the pending buffer — the
                    # buffer is consumed just above, so logging it reported
                    # an empty string and the line that exists to make this
                    # weaker provenance visible said nothing at all.
                    statement_date=stmt.statement_date,
                    detail="the block printed no date; recovered from the "
                           "scan batch's own label. A CAPTURE claim, not a "
                           "provider one — weaker, and recorded as such",
                )
            continue

        # Table rows.
        if is_table_row(stripped):
            # The header window closes at the statement's first table row.
            in_statement_header = False
            cells = split_table_row(stripped)
            if _TABLE_SEP_RE.match(stripped):
                # The |---|---| separator under a header. Nothing to read.
                continue
            if not in_table:
                # A table starts here. It is a HEADER row if its cells map to
                # the required columns; otherwise it may be a CONTINUATION —
                # a multi-page statement whose second page resumes the rows
                # without repeating the header.
                result.tables_seen += 1
                candidate, unmapped = _map_columns(cells)
                missing = REQUIRED_COLUMNS - set(candidate.values())

                if not missing:
                    mapping = candidate
                    header_width = len(cells)
                    last_mapping = dict(mapping)
                    last_header_width = header_width
                    last_mapping_stmt = current_stmt
                    result.tables_parsed += 1
                    rejected_header_line = None
                    result.unmapped_headings.extend(unmapped)
                    if unmapped:
                        log.info(
                            "reconcile.parser.unmapped_headings",
                            line_no=line_no,
                            headings=unmapped,
                            detail="columns present in the source that this "
                                   "parser has no field for — data in them is "
                                   "not captured; add a COLUMN_SYNONYMS row",
                        )
                    in_table = True
                    if current_stmt is None:
                        _begin_statement(line_no)
                    continue

                if _is_totals_block_header(cells):
                    # A key-value STATEMENT TOTALS block: two columns, a
                    # blank-ish label heading and an "Amount" heading. Its
                    # rows are declared totals (BC statement amount, payment
                    # amount, and friends), which is reconciliation gold.
                    #
                    # It is CAPTURED, not INTERPRETED. Which labelled figure
                    # is "the" payment total is a semantic question the note
                    # does not answer, and picking one would be the same
                    # invent-authority error the empty EOB map exists to
                    # avoid. See ``Statement.declared_totals``.
                    in_table = True
                    in_totals_block = True
                    mapping = {}
                    header_width = len(cells)
                    rejected_header_line = None
                    result.tables_parsed += 1
                    if current_stmt is None:
                        _begin_statement(line_no)
                    log.info(
                        "reconcile.parser.totals_block",
                        line_no=line_no,
                        detail="two-column statement-totals block; rows are "
                               "captured as declared totals and are NOT "
                               "assigned to payment_total",
                    )
                    continue

                if last_mapping and len(cells) == last_header_width:
                    # CONTINUATION. This row is DATA, and it is the shape that
                    # silently ate real claim lines: read as a header it maps
                    # nothing, so the whole continuation table was skipped and
                    # its rows never reached the ledger.
                    #
                    # The column-count match is the gate. It is deliberately
                    # strict — a differently-shaped table (the two-column
                    # totals block, for one) cannot inherit by accident.
                    #
                    # Inheritance is NOT scoped to the current statement, and
                    # that is a considered widening: a continuation may sit
                    # after a page heading, and a statement-scoped rule would
                    # then fail to fix the very case it was written for. The
                    # log records which side of that line it fell on, so an
                    # inherited mapping is never a silent event.
                    mapping = dict(last_mapping)
                    header_width = last_header_width
                    in_table = True
                    rejected_header_line = None
                    result.tables_parsed += 1
                    result.continuations.append(line_no)
                    log.info(
                        "reconcile.parser.continuation_table",
                        line_no=line_no,
                        columns=header_width,
                        same_statement=(last_mapping_stmt is current_stmt),
                        detail="table has no header row; inherited the column "
                               "mapping from the previous table and read this "
                               "row as data",
                    )
                    # Deliberately NO continue — fall through to the data-row
                    # handler below, because this row IS a data row.
                else:
                    log.warning(
                        "reconcile.parser.table_unrecognised",
                        line_no=line_no,
                        missing=sorted(missing),
                        headings=[c.strip() for c in cells],
                        detail="table skipped — without these columns there "
                               "is no key and nothing to reconcile, and no "
                               "prior table's mapping fits its column count",
                    )
                    result.skipped.append(SkippedRow(
                        line_no=line_no,
                        raw=stripped,
                        reason=(
                            "table header missing required column(s): "
                            + ", ".join(sorted(missing))
                        ),
                    ))
                    # Stay "in" the rejected table with an empty mapping. The
                    # alternative — dropping back out — makes the very next
                    # DATA row look like a fresh header, so a single bad table
                    # reports the same header error once per row and buries
                    # the actual cause.
                    mapping = {}
                    in_table = True
                    rejected_header_line = line_no
                    continue

            # A data row.
            if in_totals_block:
                label = strip_emphasis(cells[0] if cells else "").strip()
                amount_raw = cells[1] if len(cells) > 1 else ""
                if not label or is_absent(amount_raw):
                    # A blank label or a blank amount carries no fact. Skipped
                    # rather than stored as an empty entry, and counted so the
                    # absence is visible in the seed output.
                    result.skipped.append(SkippedRow(
                        line_no=line_no,
                        raw=stripped,
                        reason="statement-totals row has no label or no amount",
                    ))
                    continue
                try:
                    amount = parse_money(amount_raw, field=f"declared total {label!r}")
                except CellParseError as exc:
                    result.skipped.append(SkippedRow(
                        line_no=line_no, raw=stripped, reason=str(exc)
                    ))
                    continue
                if current_stmt is not None and amount is not None:
                    current_stmt.declared_totals[label] = str(amount)
                continue

            if not mapping:
                # Belongs to a table whose header this parser rejected. Each
                # row still gets its own skip entry — the operator's question
                # is "how many claim lines did I lose", and answering it with
                # one line per lost row is the only form that answers it.
                result.skipped.append(SkippedRow(
                    line_no=line_no,
                    raw=stripped,
                    reason=(
                        f"row belongs to the table whose header at line "
                        f"{rejected_header_line} was rejected"
                        if rejected_header_line
                        else "row appeared outside any recognised table"
                    ),
                ))
                continue
            if len(cells) != header_width:
                result.skipped.append(SkippedRow(
                    line_no=line_no,
                    raw=stripped,
                    reason=(
                        f"ragged row: {len(cells)} cells against a "
                        f"{header_width}-column header. Refused rather than "
                        f"padded — padding money columns invents figures."
                    ),
                ))
                continue

            row, error = _build_claim_line(
                cells=cells,
                mapping=mapping,
                statement=current_stmt,
                line_no=line_no,
                source_note=source_note,
                batch_id=batch_id,
                session=session,
                capture_ref=capture_ref,
                # A row is inferred if it sits inside an attribution span OR
                # its statement is flagged. The second half is what carries
                # the flag across a regeneration: the render states the fact
                # once, on the statement, and its rows inherit it.
                inferred=(
                    inferred_depth > 0
                    or bool(current_stmt and current_stmt.inferred)
                ),
                date_order=date_order,
            )
            if error is not None:
                result.skipped.append(SkippedRow(
                    line_no=line_no, raw=stripped, reason=error
                ))
                continue
            assert row is not None  # _build_claim_line returns one or the other

            labelled = _has_subtotal_label(cells, mapping)
            unlabelled = _looks_like_aggregate(row)
            is_sub = labelled or unlabelled
            row.row_type = ROW_SUBTOTAL if is_sub else ROW_CLAIM
            if unlabelled and not labelled:
                # The population-derived case is an INFERENCE, unlike a row
                # that says SUB-TOTAL on it. It is logged for the same reason
                # an inherited column mapping is: the operator cannot check a
                # judgement he cannot see, and this one moves money between
                # the claim-line sum and the cross-foot's inputs.
                result.aggregate_rows.append(line_no)
                log.info(
                    "reconcile.parser.aggregate_row",
                    line_no=line_no,
                    claim_no=row.claim_no,
                    amount_paid=str(row.amount_paid),
                    detail="full-width row with no date of service, benefit "
                           "code or units and a non-numeric id — captured as "
                           "the provider's own arithmetic, NOT as a claim "
                           "line",
                )

            # Occurrence disambiguation, in source order.
            ident = (
                row.statement_date, row.claim_no, row.dos, row.benefit_code
            )
            counter = sub_occurrences if is_sub else occurrences
            seen = counter.get(ident, 0)
            row.occurrence = seen
            counter[ident] = seen + 1
            if seen and not is_sub:
                # Only CLAIM-line collisions are reported. Every subtotal row
                # on a statement shares a key by construction (they carry a
                # label instead of a claim number and no date), so counting
                # them would bury the one collision that means something — a
                # real claim line that could have been silently overwritten.
                result.collisions.append(line_key(*ident, seen))

            if is_sub:
                result.subtotals.append(row)
                subs_for_current.append(row)
            else:
                result.claim_lines.append(row)
                claims_for_current.append(row)
            continue

        in_table = False
        in_totals_block = False

        # Statement metadata: "**Provider:** Jane Roe" / "Provider: Jane Roe".
        meta = _META_RE.match(_strip_emphasis(stripped))
        if meta:
            norm = normalise_heading(meta.group(1))
            target = STATEMENT_FIELD_SYNONYMS.get(norm)
            if target:
                # A metadata line can legitimately arrive BEFORE any heading —
                # a partial-page statement whose header row was captured but
                # whose title was not. Opening the statement here is what makes
                # that shape parse instead of crashing.
                if current_stmt is None:
                    _begin_statement(line_no)
                _apply_meta(current_stmt, target, meta.group(2).strip(), date_order)
            elif in_statement_header:
                if current_stmt is None:
                    _begin_statement(line_no)
                # An UNKNOWN header label carrying money. Captured into
                # declared_totals rather than dropped — the same
                # captured-not-interpreted posture the two-column totals
                # block uses, and for the same reason: the figure is real
                # and the label's meaning is not ours to invent.
                #
                # This is the half of the real note's header variant that
                # silently lost $54,549 — a bolded multi-word total label
                # matching no synonym, so the line parsed as prose and the
                # statement opened with no declared total at all.
                #
                # Gated on being INSIDE a header block (before this
                # statement's first table) so a stray "Note: $5 fee" in
                # free text after the rows cannot inject a phantom total.
                raw_value = meta.group(2).strip()
                try:
                    amount = parse_money(raw_value, field="header amount")
                except CellParseError:
                    amount = None
                if amount is not None:
                    label = strip_emphasis(meta.group(1)).strip()
                    current_stmt.declared_totals.setdefault(label, str(amount))
                    result.unmapped_header_labels.append(label)
                    log.info(
                        "reconcile.parser.unmapped_header_amount",
                        line_no=line_no,
                        label=label,
                        amount=str(amount),
                        detail="header line with an unrecognised label and a "
                               "money value — captured as a declared total, "
                               "NOT assigned to payment_total; add a "
                               "STATEMENT_FIELD_SYNONYMS row if it is one",
                    )

    _flush_statement()

    # Statement rows carry the date their claim lines were stamped with; a
    # statement whose header never named a date inherits it from its lines,
    # because the lines' date is what the ledger key uses.
    _backfill_statement_dates(result)

    if not result.tables_seen:
        # ILB: "found nothing" is a result, and it must be stated. A silent
        # empty return is indistinguishable from a parser that crashed.
        log.info(
            "reconcile.parser.no_tables",
            source_note=source_note,
            lines=len(lines),
            detail="the note contained no recognisable claim table; nothing "
                   "was written",
        )
    log.info(
        "reconcile.parser.parsed",
        source_note=source_note,
        statements=len(result.statements),
        claim_lines=len(result.claim_lines),
        subtotals=len(result.subtotals),
        skipped=len(result.skipped),
        collisions=len(result.collisions),
        tables_seen=result.tables_seen,
        tables_parsed=result.tables_parsed,
    )
    if result.skipped:
        log.warning(
            "reconcile.parser.rows_skipped",
            source_note=source_note,
            skipped=len(result.skipped),
            kept=len(result.claim_lines) + len(result.subtotals),
            first_reasons=[s.reason for s in result.skipped[:5]],
            detail="these rows are NOT in the ledger — a partial parse names "
                   "what it lost",
        )
    return result


def _apply_meta(
    stmt: Statement | None,
    target: str,
    value: str,
    date_order: str | None,
) -> None:
    if stmt is None:
        return
    if target == "statement_date":
        try:
            iso = parse_date(value, field="statement date", date_order=date_order)
        except CellParseError:
            iso = None
        if iso:
            stmt.statement_date = iso
        return
    if target == "inferred":
        stmt.inferred = value.strip().upper().startswith(INFERRED_TOKEN)
        return
    if target == "payment_total":
        try:
            stmt.payment_total = parse_money(value, field="payment total")
        except CellParseError:
            log.warning(
                "reconcile.parser.payment_total_unreadable",
                raw=value,
                detail="statement header payment total could not be parsed; "
                       "the statement is kept without it and the report's "
                       "cross-foot for it is skipped",
            )
        return
    setattr(stmt, target, value)


def _date_from_free_text(text: str, date_order: str | None) -> str | None:
    """Best-effort date out of a heading. Never raises.

    An embedded ISO date is looked for FIRST and matched as a unit. The
    token scan below splits on whitespace and dashes, which would tear
    ``2026-02-26`` into three meaningless tokens — so a heading carrying the
    single most machine-readable form of a date would be the one form this
    function failed to read.

    Used only for headings, where a failure costs nothing (the statement
    date is backfilled from its claim lines), so this one place is allowed
    to guess where a data cell never is.
    """
    body = (text or "").strip()
    iso_hit = _ISO_IN_TEXT_RE.search(body)
    if iso_hit:
        try:
            iso = parse_date(iso_hit.group(0), date_order=date_order)
        except CellParseError:
            iso = None
        if iso:
            return iso

    # Em/en dashes separate a title from its date ("Statement — 11 Jun 2026").
    # The ASCII hyphen is deliberately NOT a separator here: it is the ISO
    # date's own separator, handled above.
    tokens = [t for t in re.split(r"[\s—–]+", body) if t]
    for size in (3, 1):
        for start in range(len(tokens)):
            window = " ".join(tokens[start:start + size])
            if not window:
                continue
            try:
                iso = parse_date(window, date_order=date_order)
            except CellParseError:
                continue
            if iso:
                return iso
    return None


def _backfill_statement_dates(result: ParseResult) -> None:
    """Give a dateless statement the date its own claim lines carry."""
    for stmt in result.statements:
        if stmt.statement_date:
            continue
        dates = {
            c.statement_date
            for c in result.claim_lines
            if c.source_line > stmt.source_line and c.statement_date
        }
        if len(dates) == 1:
            stmt.statement_date = next(iter(dates))


def _build_claim_line(
    *,
    cells: list[str],
    mapping: dict[int, str],
    statement: Statement | None,
    line_no: int,
    source_note: str,
    batch_id: str,
    session: str,
    capture_ref: str,
    inferred: bool,
    date_order: str | None,
) -> tuple[ClaimLine | None, str | None]:
    """``(row, None)`` or ``(None, reason)``. Never both, never neither."""
    row = ClaimLine(
        source_note=source_note,
        source_line=line_no,
        batch_id=batch_id,
        session=session,
        capture_ref=capture_ref,
        inferred=inferred,
    )
    row.statement_date = statement.statement_date if statement else ""

    for idx, field_name in mapping.items():
        raw = cells[idx] if idx < len(cells) else ""
        try:
            if field_name == "dos":
                row.dos = parse_date(
                    raw, field="date of service", date_order=date_order
                ) or ""
            elif field_name == "units":
                row.units = parse_int(raw, field="units")
            elif field_name == "pct_paid":
                row.pct_paid = parse_percent(raw, field="% pd")
            elif field_name in (
                "total_billed", "amt_excluded", "deduct",
                "amt_eligible", "amount_paid",
            ):
                setattr(
                    row, field_name, parse_money(raw, field=field_name)
                )
            else:
                # Text columns honour the absent-cell vocabulary too: an
                # em-dash placeholder means "nothing here", and carrying the
                # dash through would put it in the ledger KEY (benefit_code
                # is part of it) and print it back as if it were data.
                # Text columns get emphasis stripped too, and that is not
                # cosmetic. Real statements bold their aggregate ROWS, label
                # and all, so a subtotal's surname arrives as ``**Aldenshaw**``
                # — which then fails to match the claim lines' ``Aldenshaw,
                # Marisol`` in the report's cross-foot, silently turning the
                # one independent check into a statement-level fallback.
                # Un-bolding the amount without un-bolding the label would
                # have fixed half of this shape and left the useful half
                # broken.
                setattr(
                    row,
                    field_name,
                    "" if is_absent(raw) else strip_emphasis(raw).strip(),
                )
        except CellParseError as exc:
            return None, str(exc)

    row.invoice_no = parse_invoice_no(row.comments)
    return row, None


__all__ = [
    "COLUMN_SYNONYMS",
    "REQUIRED_COLUMNS",
    "STATEMENT_FIELD_SYNONYMS",
    "ParseResult",
    "SkippedRow",
    "is_table_row",
    "normalise_heading",
    "parse_invoice_no",
    "parse_note",
    "split_table_row",
]
