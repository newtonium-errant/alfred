"""Remittance reconciliation — both sides of the payment loop.

The mission: every invoice ends **paid-confirmed**, **needs-attention** or
**late**, and the operator only ever handles the last two — surfaced
deliberately, never discovered by scrolling a 110KB note at 2 a.m.

**P1 was the STATEMENT side** — what the provider ANSWERED. It parses a
payment summary into a ledger, classifies each claim line for attention,
and produces one bulk-review report, with no dependency on any external
export.

**P2 adds the INVOICE side** — what was ASKED. RRTS posts a nightly
snapshot to the transport receiver; :mod:`~alfred.reconcile.invoices`
reads it, :mod:`~alfred.reconcile.matcher` proposes joins between the two
sides, and :mod:`~alfred.reconcile.aging` answers the question the
provider never answers: which invoices did they not answer at all. All
three surface on the same bulk-review report.

**Everything remains PROPOSE-ONLY.** The matcher proposes; nothing closes,
dismisses, marks paid or auto-chases. Auto-close is a later operator flip
and is not reachable from this package.

**Ledger is truth, note is render.** The same direction the batch
pipeline ships (:mod:`alfred.batch`): accumulation lives in a sidecar
outside the vault, the note is DERIVED, and the derivation is re-run
rather than patched. :mod:`alfred.reconcile.render` is a pure function
for exactly that reason — a half-written note can always be rebuilt, and
an operator editing the note cannot corrupt the underlying figures.

**THIS PACKAGE WRITES NOTHING TO THE VAULT.** Not a record, not a frontmatter
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
    names.py      claimant-name normalisation across the two sides
    invoices.py   the invoice side: RRTS's snapshot, and its 48h promise
    matcher.py    ledger <-> invoice join proposals, with stated confidence
    aging.py      the watchdog: which invoices were never answered
    render.py     ledger -> note text (pure)
    report.py     the bulk-review artifact: CSV + summary
    config.py     the ``reconcile:`` config block
    cli.py        ``alfred reconcile {seed,render,report,status,correct}``
"""

from __future__ import annotations

__all__: list[str] = []
