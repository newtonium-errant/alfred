"""Bulk-image batch intake and drip-through processing (#83).

The operator submits N scans with ONE instruction; the system saves them
all immediately and works through them as capacity allows, appending
results to one carried vault record.

The pieces, and why they are split this way:

  * :mod:`~alfred.batch.paths` — config-derived, instance-scoped
    locations. Everything lives OUTSIDE the vault (see the module
    docstring for why the curator-watched inbox is the wrong home).
  * :mod:`~alfred.batch.manifest` — the frozen submission inventory;
    the drip campaign's work-list source.
  * :mod:`~alfred.batch.ledger` — the append-only JSONL sidecar. **The
    truth.** One row per processed image, keyed by content hash.
  * :mod:`~alfred.batch.render` — pure ledger -> record body.
  * :mod:`~alfred.batch.seal` — the guard that refuses to regenerate a
    record the operator has taken ownership of.

The invariant tying them together: the ledger is authoritative and the
vault record is derived. Rendering is idempotent and re-runnable, so a
crash between "result recorded" and "record updated" loses nothing.
"""
