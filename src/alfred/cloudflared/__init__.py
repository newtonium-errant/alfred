"""Cloudflared tunnel daemon — supervised subprocess wrapper.

Wraps the ``cloudflared`` Go binary (Cloudflare Tunnel client) as a
supervised child of ``alfred up`` so it auto-restarts with the other
daemons.

Background: the mail webhook receiver was supervised by ``alfred up``
already, but ``cloudflared`` — the Cloudflare tunnel that bridges
``${webhook host} → localhost:<mail port>`` — was started manually
via ``nohup``. On 2026-05-13 we discovered the Outlook → vault pipeline
had been silently disconnected for ~2 weeks because cloudflared stopped
running after a WSL2 restart and nobody noticed. Bringing it under
``alfred up`` closes that observability gap.

See :mod:`alfred.cloudflared.daemon` for the Pattern-A wrapper that
runs the binary, captures stdout to a flat log file, and propagates
SIGTERM to the child for clean shutdown.
"""
