"""Remittance reconciliation — the statement side of the payment loop (P1).

The mission: every invoice ends **paid-confirmed**, **needs-attention** or
**late**, and the operator only ever handles the last two — surfaced
deliberately, never discovered by scrolling a 110KB note at 2 a.m.

P1 is the STATEMENT side only, and it has no dependency on any external
export. It parses a provider payment summary into a ledger, classifies
each claim line for attention, and produces one bulk-review report. The
matcher, the aging watchdog and the late/unmatched cards are P2 — they
need an invoices export that does not exist yet.

**Ledger is truth, note is render.** The same direction the batch
pipeline ships (:mod:`alfred.batch`): accumulation lives in a sidecar
outside the vault, the note is DERIVED, and the derivation is re-run
rather than patched. :mod:`alfred.reconcile.render` is a pure function
for exactly that reason — a half-written note can always be rebuilt, and
an operator editing the note cannot corrupt the underlying figures.

**P1 WRITES NOTHING TO THE VAULT.** Not a record, not a frontmatter
field, not a tag. The renderer returns a string to its caller and stops
there; nothing in this package imports :mod:`alfred.vault.ops`, and there
is no scope rule to add because there is no capability to gate. The
batch-pipeline integration seam is documented in
:func:`alfred.reconcile.render.render_note` and is deliberately inert.

Module map::

    paths.py      where the store lives (instance-scoped, config-derived)
    money.py      money/percent/date cell parsing — every fallback stated
    ledger.py     the model: ClaimLine + Statement, keyed upsert store
    parser.py     a payment-summary note -> ledger rows (fail-loud)
    attention.py  the six attention classes; unknown EOB fails OPEN
    render.py     ledger -> note text (pure)
    report.py     the bulk-review artifact: CSV + summary
    config.py     the ``reconcile:`` config block
    cli.py        ``alfred reconcile {seed,render,report,status}``
"""

from __future__ import annotations

__all__: list[str] = []
