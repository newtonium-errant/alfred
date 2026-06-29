"""Algernon web surface — a browser front-end's backend half.

The ``web`` package hosts the HTTP routes that back the Algernon web
front-end (Milestone 1: a browser chat with the instance, vault-grounded
and scope-enforced, sharing the *exact* ``run_turn`` engine the Telegram
talker uses). The routes mount onto the existing transport aiohttp app
inside the talker daemon — there is no second server / port.

Layering note: the web chat surface needs talker runtime (the Anthropic
client, the ``StateManager``, the ``TalkerConfig``, the per-turn system-
prompt provider, and the boot-time vault-context snapshot). Those live in
the talker daemon, not in the transport layer — so the daemon is the only
place that has BOTH the transport app AND the talker runtime, and the
daemon wires the web routes (via :func:`alfred.web.routes_chat.register_web_routes`)
adjacent to ``wire_transport_app``. This mirrors how the daemon directly
registers GCal vault-ops hooks rather than threading talker runtime
through the transport-scoped wiring function.

Opt-in: with ``web.enabled: false`` (or an absent ``web:`` block) no web
routes are mounted and the transport server is byte-unchanged. M1 targets
Salem only.
"""

from __future__ import annotations
