"""Inter-project message bus (V1 — Layer 1 of the KAL-LE coordinator).

A KAL-LE-brokered FILESYSTEM message bus, modeled on
``transport.ticket_forward`` generalized from "one GitHub destination
over HTTP" to "N project inbox dirs over the shared filesystem". Senders
drop a frontmatter+body message-record into ONE shared spool; the
``message_bus`` routing daemon validates → dedups by stable id → places
it atomically into the destination project's ``.msgbus/inbox/`` →
optionally pings the operator; sessions drain their inbox on start.

Records are routing ENVELOPES (plain ``.md`` under ``.msgbus/`` dirs),
NOT vault records — they never touch ``vault/schema.py`` or
``vault/scope.py`` (same reasoning as curator inbox files being plain
frontmatter markdown). This keeps v1 dumb + deterministic, no
validation-gate coupling.

One-box assumption (by settled design): all participating projects live
on the same machine, so routing is shared-filesystem reads/writes. If any
project ever leaves the box, the bus needs a git/HTTP transport (v2
rebuild) — documented loudly in the config block.

Layer-2 contract negotiation builds ON this bus and is a SEPARATE arc.
"""
