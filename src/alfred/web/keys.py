"""Shared aiohttp ``Application`` storage keys for the web surface.

A leaf module (imports nothing from ``alfred.web``) so both ``routes_chat``
and ``routes_auth`` can reference the same keys without an import cycle.
Namespaced ``web.*`` so they never collide with the transport's own
``transport.*`` keys on the shared Application.
"""

from __future__ import annotations

KEY_WEB_CONFIG = "web.config"
KEY_WEB_ANTHROPIC = "web.anthropic_client"
KEY_WEB_STATE_MGR = "web.state_mgr"
KEY_WEB_TALKER_CONFIG = "web.talker_config"
KEY_WEB_SYSTEM_PROVIDER = "web.system_prompt_provider"
KEY_WEB_VAULT_CTX = "web.vault_context_str"
KEY_WEB_AUTH_STATE = "web.auth_state"
# Per-app in-flight set of session_keys with a run_turn task currently
# running — the concurrent-turn guard (prevents a double-submit from
# double-appending to the transcript). Stashed per-app (NOT module-global)
# so concurrent test apps in one process don't share state.
KEY_WEB_INFLIGHT = "web.inflight_session_keys"
# Per-app in-flight set of (session_key, span_index) pairs with a span
# extraction currently running (capture toggle R1) — a double-tap on the
# extraction offer must not produce two span records. Per-app for the
# same concurrent-test-apps reason as KEY_WEB_INFLIGHT.
KEY_WEB_CAPTURE_EXTRACTING = "web.capture_extracting_spans"
# Per-app VoiceSessionManager for the WebRTC voice surface (V0 echo), or
# ``None`` when voice is mounted in aiortc-missing 503 mode. Stashed per-app
# (NOT module-global) for the same reason as KEY_WEB_INFLIGHT — concurrent
# test apps in one process must not share the RTCPeerConnection registry.
KEY_WEB_VOICE_MANAGER = "web.voice_manager"
# The daemon's data dir (the ./data the brief/transport state lives under),
# or ``None`` when the mount site didn't thread it. The outbound-read route
# (routes_brief.py, #30) serves the brief/daily-sync spool from
# ``<data_dir>/web_outbound/``; an unset dir reads as "nothing spooled"
# (the intentionally-left-blank empty 200), never a crash.
KEY_WEB_DATA_DIR = "web.data_dir"
# The bounded per-user WebNotifyStore (parity #22 KAL-LE ticket → PWA
# notify, poll slice), or ``None`` when notifications are enabled but no
# data_dir was threaded (the read routes then serve the intentionally-
# left-blank empty payload). Filled by the transport-level sink
# (peer_handlers.register_web_notify_sink) on web_notify-tagged notices;
# read by routes_notify keyed to the resolving identity's synthetic id.
KEY_WEB_NOTIFY_STORE = "web.notify_store"
# The contact-surface router's state store (C4), or ``None`` when no state path
# is anchored (``web.contact_router.state_path`` absent AND no ``logging.dir`` to
# derive from). ``None`` is a supported state, not a failure: ``/day/state`` then
# serves ``configured: false`` and the PWA does not route at all — staying put is
# the fail-safe, and a cwd-guessed path would let one instance route on another's
# contact log (#74). Written only by the ``/day/*`` handlers and the feed-act
# dispatcher, both of which run in the talker daemon (single-writer).
KEY_WEB_CONTACT_STORE = "web.contact_store"
# The ``FeedEmitHandle`` the contact router deals pattern cards through, or
# ``None`` when the feed is unwired/disabled. Optional by design: absent means
# overrides are still recorded and simply proposed nowhere (logged, never
# silent) — the same no-emit fail-safe the classify-time email handle uses.
KEY_WEB_CONTACT_FEED = "web.contact_feed"
