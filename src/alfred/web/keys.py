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
# Per-app VoiceSessionManager for the WebRTC voice surface (V0 echo), or
# ``None`` when voice is mounted in aiortc-missing 503 mode. Stashed per-app
# (NOT module-global) for the same reason as KEY_WEB_INFLIGHT — concurrent
# test apps in one process must not share the RTCPeerConnection registry.
KEY_WEB_VOICE_MANAGER = "web.voice_manager"
