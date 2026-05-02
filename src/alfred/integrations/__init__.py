"""External-service integrations (Google Calendar, etc.).

Each integration is a single-purpose adapter around a third-party API.
Modules here:

    gcal — Google Calendar (OAuth-installed-app flow + Calendar v3 API).
           Used by Salem's transport handler for event conflict-check
           and by the post-create sync that pushes accepted events to
           Andrew's Alfred Calendar so they land on his phone.

Adapters keep their imports of the underlying SDK lazy so an instance
that doesn't enable the integration doesn't pay the import cost (and
doesn't crash when the optional dependency isn't installed).
"""
