"""The remittance ledger — the model, and the store that holds it.

One JSONL file carries two row types, discriminated by ``row_type``:

* :data:`ROW_STATEMENT` — one per statement (provider, totals, provenance)
* :data:`ROW_CLAIM` — one per claim line
* :data:`ROW_SUBTOTAL` — a per-claimant SUB-TOTAL line from the source

Subtotals are KEPT rather than discarded. They are the provider's own
arithmetic, which makes them the only independent check on ours: the
report cross-foots our per-claimant sums against them, and a mismatch is
a finding. Dropping them at parse time would throw away the check and
leave a re-render unable to reproduce the source document.

**Why this store rewrites rather than appends** — a deliberate divergence
from :mod:`alfred.batch.ledger`, which is strictly append-only. Batch rows
arrive one image at a time across many separate runs, so rewriting per row
would risk everything before a crash. Here the unit of work is a WHOLE
NOTE re-parse: a re-run presents every row again, and identity is a
content key rather than an arrival event. An append-only file under those
conditions grows a duplicate of the entire ledger on every re-run and
pushes dedup onto every reader. So the write is a keyed upsert with an
atomic whole-file replace (``.tmp`` -> ``os.replace``), which is the
house's state-file idiom, and the ``flock`` sidecar convention is kept
from the batch ledger for the same reason it exists there.

**Money is carried as STRINGS in the JSON and Decimals in memory.** A
float round-trip reintroduces exactly the representation error
:mod:`alfred.reconcile.money` exists to avoid, and it would do so
silently, at load time, after the figures had already been checked.

**Load is schema-tolerant in both directions**, per the house contract:
:meth:`ClaimLine.from_dict` filters incoming data against the dataclass's
known fields, so a ledger written by an older build (missing a field) and
one written by a newer build (carrying an extra) both load. A torn final
line — the signature of a crash mid-write — is skipped with a warning
rather than being allowed to make the whole ledger unreadable.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import structlog

log = structlog.get_logger(__name__)

ROW_STATEMENT = "statement"
ROW_CLAIM = "claim"
ROW_SUBTOTAL = "subtotal"

#: Fields carried as Decimal in memory and as strings on disk.
_MONEY_FIELDS = (
    "total_billed",
    "amt_excluded",
    "deduct",
    "amt_eligible",
    "amount_paid",
    "pct_paid",
)

_STATEMENT_MONEY_FIELDS = ("payment_total",)

#: Separator inside the composite line key. A unit separator, because it
#: cannot occur in any of the parts — a printable separator would let
#: ``("A|B", "C")`` and ``("A", "B|C")`` collide into one key.
_KEY_SEP = "\x1f"


def _dec(value: Any) -> Decimal | None:
    """Coerce a loaded JSON value to Decimal, tolerating both spellings.

    Strings are the format this store writes. Ints and floats are accepted
    because a hand-edited ledger (or one written before this contract) may
    carry them, and refusing would make a recoverable file unreadable — but
    a float is routed through ``str()`` first so it lands on the decimal
    value a human would read, not the binary expansion.
    """
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 — an unreadable cell degrades to absent
        return None


def _money_out(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def statement_key(statement_date: str, statement_occurrence: int = 0) -> str:
    """A statement's identity: its date, plus a tiebreak for same-day blocks.

    The date alone is NOT unique. A provider can issue two statements on one
    day, and the note prints each with its own header block — so keying on
    the date alone makes the second block's header overwrite the first's on
    upsert, taking its ``payment_total`` with it and silently attributing
    both blocks' claim lines to one statement. That is not hypothetical: it
    happened on the real note (2026-04-23), where the fold hid a second
    statement's lines inside the first and put the cross-foot 16,894 out.

    ``statement_occurrence`` is assigned by the parser, which folds same-date
    blocks only when their header facts are compatible and splits them
    otherwise. See :func:`alfred.reconcile.parser.parse_note`.
    """
    return f"{statement_date or ''}#{statement_occurrence}"


def line_key(
    statement_date: str,
    claim_no: str,
    dos: str,
    benefit_code: str,
    occurrence: int = 0,
    statement_occurrence: int = 0,
) -> str:
    """The claim line's identity: the ratified key, plus a tiebreak.

    The ratified key is ``(statement_date, claim_no, dos, benefit_code)``.
    That key is NOT unique in the real source, and the source is what
    decides: the ``Claim #`` column sometimes carries ``(Ambulance Claims)``
    instead of a number, so two genuinely different lines on one statement
    can share all four parts. Upserting on the bare four-tuple would make
    the second line silently overwrite the first — a lost claim, and lost
    money, with nothing in the output to show it happened.

    ``occurrence`` is therefore appended: the 0-based index among the lines
    on one statement that share the other four parts, assigned in SOURCE
    ORDER. It is deterministic, so a re-parse of the same note produces the
    same keys and the upsert stays idempotent. It is also visible — the
    parser counts collisions and the report names them — so a key that had
    to disambiguate is never a silent event.
    """
    return _KEY_SEP.join([
        statement_date or "",
        claim_no or "",
        dos or "",
        benefit_code or "",
        str(occurrence),
        # The statement occurrence is part of the LINE key, not only the
        # statement key. Two same-day statements can each carry the same
        # claim_no + dos + benefit_code — a re-billed claim, which is
        # precisely the population a duplicate-denial produces — and without
        # this the two rows collide and the upsert keeps one. Losing a
        # re-billed line is losing the evidence that it was re-billed.
        str(statement_occurrence),
    ])


@dataclass
class ClaimLine:
    """One claim line from a provider payment summary."""

    statement_date: str = ""
    claim_no: str = ""
    dos: str = ""
    surname: str = ""
    first_name: str = ""
    benefit_code: str = ""
    units: int | None = None
    total_billed: Decimal | None = None
    amt_excluded: Decimal | None = None
    deduct: Decimal | None = None
    amt_eligible: Decimal | None = None
    pct_paid: Decimal | None = None
    amount_paid: Decimal | None = None
    eob_code: str = ""
    comments: str = ""
    #: Parsed out of ``comments`` — the join key to the invoice side (P2).
    invoice_no: str = ""
    occurrence: int = 0
    #: Which same-day statement block this line belongs to. Stamped by the
    #: parser at statement-flush time; part of :attr:`key`.
    statement_occurrence: int = 0
    row_type: str = ROW_CLAIM

    # --- provenance -------------------------------------------------------
    #: The note this row was parsed from.
    source_note: str = ""
    #: 1-based line number within that note, so a finding can be pointed at.
    source_line: int = 0
    batch_id: str = ""
    session: str = ""
    capture_ref: str = ""
    #: True when the row sat inside a BEGIN_INFERRED / END_INFERRED span in
    #: the source. Preserved as provenance per the design: an inferred
    #: figure and a transcribed one are different evidence.
    inferred: bool = False

    @property
    def key(self) -> str:
        return line_key(
            self.statement_date,
            self.claim_no,
            self.dos,
            self.benefit_code,
            self.occurrence,
            self.statement_occurrence,
        )

    @property
    def claimant(self) -> str:
        """``Surname, First`` — the per-claimant grouping label."""
        s = (self.surname or "").strip()
        f = (self.first_name or "").strip()
        if s and f:
            return f"{s}, {f}"
        return s or f

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f_ in fields(self):
            value = getattr(self, f_.name)
            if f_.name in _MONEY_FIELDS:
                out[f_.name] = _money_out(value)
            else:
                out[f_.name] = value
        out["key"] = self.key
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClaimLine":
        """Schema-tolerant load — the house contract.

        Unknown keys are dropped (a newer build's extra field must not
        crash an older one) and missing keys fall to their defaults (an
        older build's ledger must load into a newer dataclass). ``key`` is
        deliberately among the dropped: it is DERIVED, and honouring a
        stored one would let a stale key outlive the fields it was built
        from.
        """
        known = {
            k: v for k, v in data.items() if k in cls.__dataclass_fields__
        }
        for name in _MONEY_FIELDS:
            if name in known:
                known[name] = _dec(known[name])
        if "units" in known and known["units"] is not None:
            try:
                known["units"] = int(known["units"])
            except (TypeError, ValueError):
                known["units"] = None
        if "occurrence" in known:
            try:
                known["occurrence"] = int(known["occurrence"] or 0)
            except (TypeError, ValueError):
                known["occurrence"] = 0
        if "statement_occurrence" in known:
            try:
                known["statement_occurrence"] = int(
                    known["statement_occurrence"] or 0
                )
            except (TypeError, ValueError):
                known["statement_occurrence"] = 0
        if "inferred" in known:
            known["inferred"] = bool(known["inferred"])
        return cls(**known)


@dataclass
class Statement:
    """One statement's header — provider, totals, capture provenance."""

    statement_date: str = ""
    provider: str = ""
    company: str = ""
    payment_total: Decimal | None = None
    #: How many claim lines the parser attributed to this statement.
    claim_line_count: int = 0
    #: Labelled figures from a two-column statement-totals block, e.g.
    #: ``{"BC Statement Amount": "40641.00"}``. Amounts are STRINGS for the
    #: same exactness reason the other money fields are; read them through
    #: :meth:`declared_totals_decimal`.
    #:
    #: CAPTURED, NOT INTERPRETED — and the distinction is the whole point.
    #: Which labelled figure is "the" payment total is a semantic question
    #: the note does not answer, so nothing here is ever assigned to
    #: :attr:`payment_total`. ``payment_total`` stays authoritative for the
    #: cross-foot because it is the figure that sits with the claim lines it
    #: summarises; these are reported ALONGSIDE it, and a disagreement
    #: between the two is surfaced as a finding rather than resolved by this
    #: layer guessing which source wins.
    declared_totals: dict[str, str] = field(default_factory=dict)
    #: Distinguishes two statements issued on the SAME DAY. Assigned by the
    #: parser; see :func:`statement_key`.
    statement_occurrence: int = 0
    row_type: str = ROW_STATEMENT

    source_note: str = ""
    source_line: int = 0
    batch_id: str = ""
    session: str = ""
    capture_ref: str = ""
    inferred: bool = False

    @property
    def key(self) -> str:
        return statement_key(self.statement_date, self.statement_occurrence)

    def declared_totals_decimal(self) -> dict[str, Decimal]:
        """The declared totals as Decimals, skipping any that will not parse.

        Unparseable entries are DROPPED here rather than raising: they are
        reported figures, not the ledger's own arithmetic, and one unreadable
        label must not make the readable ones unavailable.
        """
        out: dict[str, Decimal] = {}
        for label, raw in (self.declared_totals or {}).items():
            value = _dec(raw)
            if value is not None:
                out[label] = value
        return out

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f_ in fields(self):
            value = getattr(self, f_.name)
            if f_.name in _STATEMENT_MONEY_FIELDS:
                out[f_.name] = _money_out(value)
            elif f_.name == "declared_totals":
                out[f_.name] = dict(value or {})
            else:
                out[f_.name] = value
        out["key"] = self.key
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Statement":
        """Schema-tolerant load — same contract as :meth:`ClaimLine.from_dict`."""
        known = {
            k: v for k, v in data.items() if k in cls.__dataclass_fields__
        }
        for name in _STATEMENT_MONEY_FIELDS:
            if name in known:
                known[name] = _dec(known[name])
        if "claim_line_count" in known:
            try:
                known["claim_line_count"] = int(known["claim_line_count"] or 0)
            except (TypeError, ValueError):
                known["claim_line_count"] = 0
        if "statement_occurrence" in known:
            try:
                known["statement_occurrence"] = int(
                    known["statement_occurrence"] or 0
                )
            except (TypeError, ValueError):
                known["statement_occurrence"] = 0
        if "declared_totals" in known:
            # Coerced to a str->str mapping. A hand-edited or older ledger may
            # carry numbers here; normalising on load keeps every reader from
            # having to handle both spellings.
            value = known["declared_totals"]
            known["declared_totals"] = (
                {str(k): str(v) for k, v in value.items()}
                if isinstance(value, dict) else {}
            )
        if "inferred" in known:
            known["inferred"] = bool(known["inferred"])
        return cls(**known)


@dataclass
class LedgerContents:
    """Everything in one ledger file, split by row type."""

    statements: list[Statement] = field(default_factory=list)
    claim_lines: list[ClaimLine] = field(default_factory=list)
    subtotals: list[ClaimLine] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.statements or self.claim_lines or self.subtotals)

    def all_lines(self) -> list[ClaimLine]:
        """Claim lines and subtotals together, for renderers that need both."""
        return [*self.claim_lines, *self.subtotals]


def _lock_path(sink_path: Path) -> Path:
    return sink_path.with_suffix(sink_path.suffix + ".lock")


@contextmanager
def _sink_lock(sink_path: Path) -> Iterator[None]:
    """Serialize writes via ``flock`` on a stable sidecar lock file.

    The lock is on a SEPARATE file, never on the ledger fd, because this
    store replaces the ledger with ``os.replace``: a lock held on the sink
    would protect the pre-replace inode, which after the swap is a file
    nobody else will ever open. That is a lock that appears to work and
    protects nothing.

    Best-effort with a loud warning rather than a hard failure — losing the
    lock degrades to a possible interleave, whereas raising would lose a
    completed parse.
    """
    lp = _lock_path(sink_path)
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        with open(lp, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        return
    except OSError as e:
        log.warning(
            "reconcile.ledger.lock_skipped",
            path=sink_path.name,
            error_class=type(e).__name__,
            detail="writing without the lock — a concurrent writer could "
                   "interleave; the write still proceeds",
        )
    yield


def load_ledger(path: Path | str) -> LedgerContents:
    """Read a ledger file. A missing file is an EMPTY ledger, not an error.

    A torn tail line (the signature of a crash mid-write) is skipped with a
    warning rather than being fatal: every complete row before it is still
    good data, and refusing to load them would turn a recoverable file into
    a lost one.
    """
    p = Path(path)
    out = LedgerContents()
    if not p.is_file():
        log.info(
            "reconcile.ledger.absent",
            path=str(p),
            detail="no ledger yet — this is the pre-seed state, not a "
                   "failure; run 'alfred reconcile seed' to create it",
        )
        return out
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — unreadable ledger degrades to empty
        log.warning(
            "reconcile.ledger.unreadable",
            path=str(p),
            error_class=type(e).__name__,
        )
        return out

    skipped = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001 — torn tail line
            skipped += 1
            continue
        if not isinstance(obj, dict):
            skipped += 1
            continue
        row_type = str(obj.get("row_type") or "")
        if row_type == ROW_STATEMENT:
            out.statements.append(Statement.from_dict(obj))
        elif row_type == ROW_CLAIM:
            out.claim_lines.append(ClaimLine.from_dict(obj))
        elif row_type == ROW_SUBTOTAL:
            out.subtotals.append(ClaimLine.from_dict(obj))
        else:
            skipped += 1

    if skipped:
        log.warning(
            "reconcile.ledger.rows_skipped",
            path=str(p),
            skipped=skipped,
            kept=len(out.statements) + len(out.claim_lines) + len(out.subtotals),
            detail="malformed, partial, or unknown-row_type lines skipped; "
                   "every complete row is preserved",
        )
    else:
        log.info(
            "reconcile.ledger.loaded",
            path=str(p),
            statements=len(out.statements),
            claim_lines=len(out.claim_lines),
            subtotals=len(out.subtotals),
        )
    return out


@dataclass
class UpsertResult:
    """What an upsert did — the numbers the CLI reports to the operator."""

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0

    @property
    def total(self) -> int:
        return self.inserted + self.updated + self.unchanged


def upsert(
    path: Path | str,
    *,
    statements: list[Statement] | None = None,
    claim_lines: list[ClaimLine] | None = None,
    subtotals: list[ClaimLine] | None = None,
) -> UpsertResult:
    """Merge rows into the ledger by key. Idempotent: a re-run is a no-op.

    Existing rows not present in the incoming set are PRESERVED — this is a
    merge, not a replace. That matters because the ledger accumulates across
    many statements parsed at different times, and a seed of one note must
    never delete another note's rows.

    The write is atomic (``.tmp`` -> ``os.replace``) so a crash mid-write
    leaves the previous ledger intact rather than a truncated one.
    """
    p = Path(path)
    statements = statements or []
    claim_lines = claim_lines or []
    subtotals = subtotals or []

    result = UpsertResult()

    with _sink_lock(p):
        existing = load_ledger(p)

        stmt_by_key = {s.key: s for s in existing.statements}
        claim_by_key = {c.key: c for c in existing.claim_lines}
        sub_by_key = {c.key: c for c in existing.subtotals}

        def _merge(incoming: list[Any], index: dict[str, Any]) -> None:
            for row in incoming:
                prior = index.get(row.key)
                if prior is None:
                    index[row.key] = row
                    result.inserted += 1
                elif prior.to_dict() == row.to_dict():
                    result.unchanged += 1
                else:
                    index[row.key] = row
                    result.updated += 1

        _merge(statements, stmt_by_key)
        _merge(claim_lines, claim_by_key)
        _merge(subtotals, sub_by_key)

        # Deterministic on-disk order: statements first (a reader scanning
        # the head sees the shape of the file), then claim lines and
        # subtotals in key order. A stable order makes a diff of two ledger
        # files mean something.
        rows: list[dict[str, Any]] = []
        rows.extend(
            s.to_dict() for s in sorted(stmt_by_key.values(), key=lambda r: r.key)
        )
        rows.extend(
            c.to_dict() for c in sorted(claim_by_key.values(), key=lambda r: r.key)
        )
        rows.extend(
            c.to_dict() for c in sorted(sub_by_key.values(), key=lambda r: r.key)
        )

        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)

    log.info(
        "reconcile.ledger.upsert",
        path=str(p),
        inserted=result.inserted,
        updated=result.updated,
        unchanged=result.unchanged,
        detail=(
            "no change — every incoming row was already in the ledger "
            "(an idempotent re-run)"
            if result.inserted == 0 and result.updated == 0
            else ""
        ),
    )
    return result


def group_by_statement(
    contents: LedgerContents,
) -> list[tuple[Statement, list[ClaimLine], list[ClaimLine]]]:
    """``[(statement, claim_lines, subtotals)]`` in date-then-occurrence order.

    Grouping is on ``(statement_date, statement_occurrence)``, NOT on the
    date alone. Two statements issued on one day are two groups, and their
    claim lines follow their own block — grouping by date would put both
    blocks' lines under whichever header won the upsert and hand the
    cross-foot a sum that belongs to two statements and reconciles against
    neither.

    Claim lines whose group matches no statement header are still returned,
    under a SYNTHESISED statement carrying that date and occurrence.
    Dropping them would be the silent-absence failure: a note whose header
    this parser did not recognise would render as no statement at all
    rather than as a statement with an unknown provider.
    """
    def _group(row: Any) -> tuple[str, int]:
        return (row.statement_date, row.statement_occurrence)

    by_group: dict[tuple[str, int], Statement] = {
        _group(s): s for s in contents.statements
    }
    claims_by_group: dict[tuple[str, int], list[ClaimLine]] = {}
    subs_by_group: dict[tuple[str, int], list[ClaimLine]] = {}

    for c in contents.claim_lines:
        claims_by_group.setdefault(_group(c), []).append(c)
    for c in contents.subtotals:
        subs_by_group.setdefault(_group(c), []).append(c)

    all_groups = set(by_group) | set(claims_by_group) | set(subs_by_group)
    out: list[tuple[Statement, list[ClaimLine], list[ClaimLine]]] = []
    for key in sorted(all_groups):
        date, occurrence = key
        stmt = by_group.get(key)
        if stmt is None:
            stmt = Statement(
                statement_date=date,
                provider="",
                company="",
                statement_occurrence=occurrence,
            )
            log.info(
                "reconcile.ledger.synthesised_statement",
                statement_date=date,
                statement_occurrence=occurrence,
                claim_lines=len(claims_by_group.get(key, [])),
                detail="claim lines carry a statement date with no matching "
                       "statement header row — grouped under a header with "
                       "no provider rather than dropped",
            )
        claims = sorted(
            claims_by_group.get(key, []), key=lambda r: (r.source_line, r.key)
        )
        subs = sorted(
            subs_by_group.get(key, []), key=lambda r: (r.source_line, r.key)
        )
        out.append((stmt, claims, subs))
    return out


__all__ = [
    "ROW_CLAIM",
    "ROW_STATEMENT",
    "ROW_SUBTOTAL",
    "ClaimLine",
    "LedgerContents",
    "Statement",
    "UpsertResult",
    "group_by_statement",
    "line_key",
    "load_ledger",
    "upsert",
]
