"""Shared helpers for building subprocess environments.

The talker (`src/alfred/telegram/`) uses the Anthropic Python SDK in-process and
needs `ANTHROPIC_API_KEY` present in its environment (or injected via config).
Every other tool invokes `claude -p` as a subprocess — and the Claude Code CLI
switches from OAuth/Max-plan auth to API-credit billing whenever that env var
is set. That silently burns paid API credits instead of riding the user's Max
subscription.

`claude_subprocess_env()` returns a copy of the current environment with the
offending keys stripped, so `claude -p` falls back to its cached OAuth creds.
All four `claude -p` call sites
(`curator|janitor|distiller/backends/cli.py` and `temporal/activities.py`)
route through this helper.
"""

from __future__ import annotations

import os

# Keys that flip `claude -p` into API-billing mode. Strip every time.
#
# Do NOT add CLAUDE_CODE_* or ANTHROPIC_OAUTH_* here — those carry the OAuth
# session we WANT the CLI to use.
_ANTHROPIC_CREDENTIAL_KEYS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
)


def claude_subprocess_env(
    overrides: dict[str, str] | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return an env dict suitable for spawning `claude -p`.

    Starts from ``base_env`` (defaults to ``os.environ``), removes the
    Anthropic credential keys that force API-credit billing, then layers
    ``overrides`` on top. Overrides win — callers may explicitly re-inject
    keys if they ever need to (not expected in normal flow).
    """
    env = dict(base_env if base_env is not None else os.environ)
    for key in _ANTHROPIC_CREDENTIAL_KEYS:
        env.pop(key, None)
    if overrides:
        env.update(overrides)
    return env
