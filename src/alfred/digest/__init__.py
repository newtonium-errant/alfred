"""Cross-project weekly digest writer.

KAL-LE synthesizes review activity across all configured projects into
a single ``YYYY-MM-DD-weekly-digest.md`` file under
``~/aftermath-lab/digests/``. The ``weekly-`` prefix preserves room for
daily / on-demand digests later.

The writer is deterministic Python — no LLM. The cross-project
patterns section emits an explicit
``<!-- TODO: LLM synthesis layer not yet implemented -->`` placeholder
so the future LLM pass has an obvious slot.
"""
