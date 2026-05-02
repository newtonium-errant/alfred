"""Cross-arc weekly digest writer.

KAL-LE synthesizes review activity across all configured projects into
a single ``YYYY-MM-DD-weekly-digest.md`` file under
``~/aftermath-lab/digests/``. The ``weekly-`` prefix preserves room for
daily / on-demand digests later.

The writer is deterministic Python — no LLM. The cross-arc patterns
section (section 4) is populated by the synthesis ranker
(:mod:`alfred.distiller.synthesis_ranker`) — a mechanical four-term
formula over distilled-learn records. Phase 2.5 adds an inspector LLM
pass only if observation shows the mechanical ranking is inadequate.
"""
