"""Smoke tests for ``alfred.subprocess_env.claude_subprocess_env``.

The whole point of this helper is to keep ``ANTHROPIC_API_KEY`` out of the
env for ``claude -p`` subprocesses (see module docstring). These tests pin
that contract so a future refactor can't silently regress to API billing.
"""

from __future__ import annotations

from alfred.subprocess_env import claude_subprocess_env


def test_anthropic_credential_keys_are_stripped():
    base = {
        "ANTHROPIC_API_KEY": "sk-ant-should-be-removed",
        "ANTHROPIC_AUTH_TOKEN": "should-be-removed",
        "ANTHROPIC_BASE_URL": "https://example.com",
        "PATH": "/usr/bin:/bin",
    }
    env = claude_subprocess_env(base_env=base)
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "ANTHROPIC_BASE_URL" not in env


def test_unrelated_env_vars_are_preserved():
    base = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/andrew",
        "CLAUDE_CODE_OAUTH_TOKEN": "keep-this-oauth-token",
        "ALFRED_VAULT_PATH": "/tmp/vault",
    }
    env = claude_subprocess_env(base_env=base)
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/andrew"
    # OAuth-style keys must NOT be stripped — that's the whole point of the
    # selective allowlist in _ANTHROPIC_CREDENTIAL_KEYS.
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "keep-this-oauth-token"
    assert env["ALFRED_VAULT_PATH"] == "/tmp/vault"


def test_overrides_win_over_base_env():
    base = {"PATH": "/usr/bin", "FOO": "from-base"}
    env = claude_subprocess_env(overrides={"FOO": "from-override"}, base_env=base)
    assert env["FOO"] == "from-override"
    assert env["PATH"] == "/usr/bin"
