"""Alfred outbound-push transport.

A lightweight HTTP server hosted inside the talker daemon plus a matching
client helper for every other tool in the monorepo. Other tools call
``alfred.transport.client.send_outbound()`` to deliver messages to the
user via Telegram. The server side also hosts 501 stubs for ``/peer/*``
and ``/canonical/*`` — the substrate that Stage 3.5's peer protocol
extends.

See ``project_outbound_transport.md`` in the memory pack for the
ratified plan and ``docs/architecture`` (forthcoming) for the full
shape once Stage 3.5 lands.
"""
