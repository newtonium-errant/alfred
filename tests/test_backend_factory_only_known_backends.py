"""Regression pin: ``_create_backend`` accepts only the surviving backend(s).

Post backend-abstraction-collapse (2026-05-25), Claude is the only surviving
agent backend across curator / janitor / distiller. The factory dispatch must:

1. Return a ``ClaudeBackend`` instance when ``agent.backend == "claude"``.
2. Raise a clear ``ValueError`` mentioning the supported backend list when
   asked for any of the deleted backends (``zo``, ``openclaw``, ``hermes``).
3. Raise the same kind of error for any unknown name (typos, future
   re-introductions not yet wired up).

The dispatch silently-defaulting to claude on an unknown name would mask
a config typo for an operator who pasted in a stale ``backend: openclaw``
line — the loud-fail keeps the failure mode obvious. Per
``feedback_intentionally_left_blank.md``.

This pin is intentionally NOT gated behind ``importorskip`` — it runs in
every CI lane. Per ``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

import pytest

from alfred.curator import daemon as curator_daemon
from alfred.curator.backends.cli import ClaudeBackend as CuratorClaudeBackend
from alfred.curator.config import (
    AgentConfig as CuratorAgentConfig,
    ClaudeBackendConfig as CuratorClaudeConfig,
    CuratorConfig,
    VaultConfig as CuratorVaultConfig,
)
from alfred.distiller import daemon as distiller_daemon
from alfred.distiller.backends.cli import ClaudeBackend as DistillerClaudeBackend
from alfred.distiller.config import (
    AgentConfig as DistillerAgentConfig,
    ClaudeBackendConfig as DistillerClaudeConfig,
    DistillerConfig,
    VaultConfig as DistillerVaultConfig,
)
from alfred.janitor import daemon as janitor_daemon
from alfred.janitor.backends.cli import ClaudeBackend as JanitorClaudeBackend
from alfred.janitor.config import (
    AgentConfig as JanitorAgentConfig,
    ClaudeBackendConfig as JanitorClaudeConfig,
    JanitorConfig,
    VaultConfig as JanitorVaultConfig,
)


# ---------------------------------------------------------------------------
# Curator
# ---------------------------------------------------------------------------


def _curator_config(backend: str) -> CuratorConfig:
    return CuratorConfig(
        vault=CuratorVaultConfig(path="/tmp/test-vault"),
        agent=CuratorAgentConfig(
            backend=backend,
            claude=CuratorClaudeConfig(),
        ),
    )


def test_curator_factory_returns_claude_for_claude() -> None:
    backend = curator_daemon._create_backend(_curator_config("claude"))
    assert isinstance(backend, CuratorClaudeBackend)


@pytest.mark.parametrize("dead_name", ["zo", "openclaw", "hermes"])
def test_curator_factory_rejects_dead_backends(dead_name: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        curator_daemon._create_backend(_curator_config(dead_name))
    msg = str(exc_info.value)
    assert dead_name in msg, f"error message must mention {dead_name!r}: {msg}"
    assert "claude" in msg.lower(), f"error must hint at supported backends: {msg}"


def test_curator_factory_rejects_typo() -> None:
    with pytest.raises(ValueError) as exc_info:
        curator_daemon._create_backend(_curator_config("clawd"))
    assert "clawd" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Janitor
# ---------------------------------------------------------------------------


def _janitor_config(backend: str) -> JanitorConfig:
    return JanitorConfig(
        vault=JanitorVaultConfig(path="/tmp/test-vault"),
        agent=JanitorAgentConfig(
            backend=backend,
            claude=JanitorClaudeConfig(),
        ),
    )


def test_janitor_factory_returns_claude_for_claude() -> None:
    backend = janitor_daemon._create_backend(_janitor_config("claude"))
    assert isinstance(backend, JanitorClaudeBackend)


@pytest.mark.parametrize("dead_name", ["zo", "openclaw"])
def test_janitor_factory_rejects_dead_backends(dead_name: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        janitor_daemon._create_backend(_janitor_config(dead_name))
    msg = str(exc_info.value)
    assert dead_name in msg
    assert "claude" in msg.lower()


def test_janitor_factory_rejects_typo() -> None:
    with pytest.raises(ValueError) as exc_info:
        janitor_daemon._create_backend(_janitor_config("claud"))
    assert "claud" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Distiller
# ---------------------------------------------------------------------------


def _distiller_config(backend: str) -> DistillerConfig:
    return DistillerConfig(
        vault=DistillerVaultConfig(path="/tmp/test-vault"),
        agent=DistillerAgentConfig(
            backend=backend,
            claude=DistillerClaudeConfig(),
        ),
    )


def test_distiller_factory_returns_claude_for_claude() -> None:
    backend = distiller_daemon._create_backend(_distiller_config("claude"))
    assert isinstance(backend, DistillerClaudeBackend)


@pytest.mark.parametrize("dead_name", ["zo", "openclaw"])
def test_distiller_factory_rejects_dead_backends(dead_name: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        distiller_daemon._create_backend(_distiller_config(dead_name))
    msg = str(exc_info.value)
    assert dead_name in msg
    assert "claude" in msg.lower()


def test_distiller_factory_rejects_typo() -> None:
    with pytest.raises(ValueError) as exc_info:
        distiller_daemon._create_backend(_distiller_config("clauded"))
    assert "clauded" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Cross-cutting: dead backend modules are gone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_path",
    [
        "alfred.curator.backends.openclaw",
        "alfred.curator.backends.hermes",
        "alfred.curator.backends.http",
        "alfred.janitor.backends.openclaw",
        "alfred.janitor.backends.http",
        "alfred.distiller.backends.openclaw",
        "alfred.distiller.backends.http",
    ],
)
def test_dead_backend_modules_are_uninstalled(module_path: str) -> None:
    """The seven dead backend modules must not be importable.

    If a re-introduction (Q3 MCP / local Ollama backend) lands a file at
    one of these import paths by accident, this pin fires immediately so
    the operator can deliberate whether the resurrection was intended.
    """
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_path)
