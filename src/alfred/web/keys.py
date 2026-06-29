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
