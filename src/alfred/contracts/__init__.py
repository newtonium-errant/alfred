"""Layer-2 contract negotiation — the meet-in-the-middle solver.

Built ON the V1 inter-project message bus (``alfred.msgbus``). The bus
carries INFO (handover/request/fyi/reply); Layer-2 carries AGREEMENT: one
KAL-LE-held versioned CONTRACT artifact per seam, six negotiation kinds
(propose/counter/accept/ratify/reject/block) that ride the same bus, a
``draft→proposed→countered→ratified→blocked/superseded`` state machine, a
build-gate (``alfred contract check`` exits 0 only when ratified), and an
operator-ratify CLI + brief surface.

Contracts are ``.md``-under-data artifacts (``<store_dir>/<id>.md``,
frontmatter = machine state, body = the human interface spec) — NOT vault
records (no ``schema.py`` / ``scope.py`` coupling), same envelope ethos as
the bus.

DORMANT-SAFE: no always-on daemon. With no contracts, the CLI no-ops, the
bus dispatch hook only fires on a CONTRACT_KINDS message, and the brief
section renders nothing (ILB). Deterministic — no LLM.
"""
