"""Ledger -> note text. A PURE function, and deliberately so.

The note is a RENDER of the ledger, regenerated wholesale rather than
patched — the direction :mod:`alfred.batch.render` already ships and the
one the design ratified ("ledger is truth, record is render"). Keeping the
render pure is what makes wholesale regeneration safe: the same ledger
always produces the same text, so a crash between the ledger write and the
note write costs nothing, and an operator who edits the note cannot
corrupt the figures underneath it.

**Regenerability is a tested property, not a claim.** The output of
:func:`render_note` is written in the shape
:func:`alfred.reconcile.parser.parse_note` reads, so a rendered ledger
parses back into the same rows. ``tests/reconcile/test_render_roundtrip.py``
runs exactly that loop — parse the fixture, render it, re-parse the render,
compare — which is the only way this claim stays true as either side
changes. Provenance fields (``source_line`` above all) legitimately differ
across the round trip: they describe WHERE a row was read from, and the
render is a different document from the source. The comparison is over the
semantic payload.

**This module performs NO vault write.** It returns a string. Nothing here
opens a file, imports :mod:`alfred.vault.ops`, or knows a vault exists —
which is why P1 adds no vault scope rule: there is no capability to gate.

**The batch-pipeline seam** (documented, deliberately inert in P1): the
integration point is a caller that takes this string and hands it to the
existing carried-record write path, exactly as
``alfred.batch.worker`` does with ``batch.render.render_body``. When that
is wired, the seal guard (``alfred.batch.seal.assert_regenerable``) is the
gate that must be consulted first — a sealed record is one the operator has
taken ownership of, and regenerating over it would destroy his edits. P1
stops short of that wiring on purpose: the write path is where the risk
lives, and proving regenerability does not require taking it.
"""

from __future__ import annotations

from decimal import Decimal

from .ledger import ClaimLine, LedgerContents, Statement, group_by_statement
from .money import format_money
from .parser import INFERRED_TOKEN

#: The canonical column order and headings the renderer emits. These
#: headings are all present in :data:`alfred.reconcile.parser.COLUMN_SYNONYMS`,
#: which is what makes a rendered note re-parseable — change one and the
#: round-trip test fails, which is the intended alarm.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("claim_no", "Claim #"),
    ("dos", "Date of Service"),
    ("surname", "Surname"),
    ("first_name", "First Name"),
    ("benefit_code", "Benefit Code"),
    ("units", "Units"),
    ("total_billed", "Total Billed"),
    ("amt_excluded", "Amt Excluded"),
    ("deduct", "Deduct"),
    ("amt_eligible", "Amt Eligible"),
    ("pct_paid", "% PD"),
    ("amount_paid", "Amount Paid"),
    ("eob_code", "EOB"),
    ("comments", "Comments"),
)

_MONEY_COLUMNS = frozenset({
    "total_billed", "amt_excluded", "deduct", "amt_eligible", "amount_paid",
})

_REGEN_BANNER = (
    "> **Machine-generated.** These statement sections are regenerated from "
    "the remittance ledger, so edits made here are overwritten. The ledger "
    "is the record of what the provider paid; this note is a view of it."
)

#: Rendered for a cell with no value. Must be a member of
#: :data:`alfred.reconcile.money.ABSENT_CELL_VALUES` so a re-parse reads it
#: back as absent rather than as zero — the round trip depends on it.
_ABSENT = "—"


def _escape_cell(text: str) -> str:
    """Make a value safe inside a Markdown table cell.

    Pipes are escaped (the comments column is free text and a raw pipe
    would split the row into the wrong number of cells, which the parser
    correctly refuses as ragged). Newlines collapse to a space — a table
    cell cannot span lines.
    """
    return (
        (text or "")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )


def _cell(line: ClaimLine, field_name: str) -> str:
    value = getattr(line, field_name, None)
    if field_name in _MONEY_COLUMNS:
        return _ABSENT if value is None else format_money(value)
    if field_name == "pct_paid":
        if value is None:
            return _ABSENT
        # Render 100 rather than 100.00 when the value is whole — that is
        # what the source prints, and it re-parses identically either way.
        dec = Decimal(value)
        return str(dec.to_integral_value()) if dec == dec.to_integral_value() else str(dec)
    if field_name == "units":
        return _ABSENT if value is None else str(value)
    text = "" if value is None else str(value)
    return _escape_cell(text) or _ABSENT


def render_row(line: ClaimLine) -> str:
    """One Markdown table row for one ledger line."""
    return "| " + " | ".join(_cell(line, name) for name, _ in COLUMNS) + " |"


def render_table_header() -> list[str]:
    header = "| " + " | ".join(label for _, label in COLUMNS) + " |"
    sep = "| " + " | ".join("---" for _ in COLUMNS) + " |"
    return [header, sep]


def render_statement_section(
    statement: Statement,
    claim_lines: list[ClaimLine],
    subtotals: list[ClaimLine] | None = None,
) -> str:
    """One statement: heading, metadata, and its claim table.

    Subtotal rows are interleaved back into SOURCE ORDER with the claim
    lines rather than appended in a block, so the rendered document has the
    same shape as the statement it came from — a per-claimant subtotal sits
    under its claimant, which is where a reader looks for it.
    """
    subtotals = subtotals or []
    out: list[str] = []

    date_label = statement.statement_date or "date unknown"
    out.append(f"## Statement — {date_label}")
    out.append("")

    if statement.statement_date:
        out.append(f"**Statement Date:** {statement.statement_date}")
    if statement.provider:
        out.append(f"**Provider:** {statement.provider}")
    if statement.company:
        out.append(f"**Company:** {statement.company}")
    if statement.payment_total is not None:
        out.append(f"**Payment Total:** {format_money(statement.payment_total)}")
    if statement.inferred:
        # A STRUCTURED metadata line, not loose prose, and the leading
        # token is what the parser keys on. The distinction matters: this
        # note is regenerated repeatedly, and a fact stated only in prose
        # would be dropped on the second pass — render/parse/render would
        # not be a fixed point and the note would quietly shed its
        # provenance. Deliberately NOT a BEGIN_INFERRED marker: minting a
        # marker_id is the audit subsystem's contract, and a renderer
        # inventing one would fabricate attribution provenance.
        out.append(f"**Provenance:** {INFERRED_TOKEN} — figures in this "
                   f"statement were inferred during capture rather than "
                   f"transcribed directly.")
    out.append("")

    merged = sorted(
        [*claim_lines, *subtotals],
        key=lambda r: (r.source_line, r.claim_no, r.occurrence),
    )

    if not merged:
        # Intentionally-left-blank: an empty statement section must say it
        # is empty ON PURPOSE. A statement header with no table underneath
        # is otherwise indistinguishable from a renderer that broke.
        out.append(
            "_No claim lines recorded for this statement. The statement "
            "header was captured but no readable claim rows were — check "
            "the seed's skipped-row list._"
        )
        out.append("")
        return "\n".join(out)

    out.extend(render_table_header())
    for line in merged:
        out.append(render_row(line))
    out.append("")
    return "\n".join(out)


def render_note(
    contents: LedgerContents,
    *,
    title: str = "Provider Payment Summary",
    include_banner: bool = True,
) -> str:
    """The whole ledger as a note body. Pure: same ledger, same text.

    Statements appear in date order. A ledger with no statements renders an
    explicit empty-state rather than an empty string.
    """
    out: list[str] = [f"# {title}", ""]
    if include_banner:
        out.append(_REGEN_BANNER)
        out.append("")

    grouped = group_by_statement(contents)
    if not grouped:
        out.append(
            "_The remittance ledger is empty — no statements have been "
            "seeded yet. This section fills in when "
            "`alfred reconcile seed` runs._"
        )
        out.append("")
        return "\n".join(out).rstrip() + "\n"

    total_lines = sum(len(c) for _, c, _ in grouped)
    out.append(
        f"**{len(grouped)} statement(s), {total_lines} claim line(s) "
        f"in the ledger.**"
    )
    out.append("")

    for statement, claims, subs in grouped:
        out.append(render_statement_section(statement, claims, subs))

    return "\n".join(out).rstrip() + "\n"


__all__ = [
    "COLUMNS",
    "render_note",
    "render_row",
    "render_statement_section",
    "render_table_header",
]
