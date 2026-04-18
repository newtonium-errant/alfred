"""Shared fixtures for the talker (``alfred.telegram``) test package.

The talker modules read from / write to a :class:`StateManager` backed by a
JSON file and interact with an Anthropic client. Tests here want a fresh
state manager per test, a minimal :class:`TalkerConfig`, and — for router /
turn tests — a fake async ``messages.create`` client so no network calls
happen.

Nothing in this file mutates repo state; fixtures all live under ``tmp_path``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from alfred.telegram.config import (
    AnthropicConfig,
    LoggingConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
)
from alfred.telegram.state import StateManager


@pytest.fixture
def state_mgr(tmp_path: Path) -> StateManager:
    """Return a fresh StateManager backed by a temp file."""
    mgr = StateManager(tmp_path / "talker_state.json")
    mgr.load()  # initialise empty state
    return mgr


@pytest.fixture
def talker_config(tmp_path: Path) -> TalkerConfig:
    """Return a minimal TalkerConfig with a temp vault path.

    Doesn't populate tokens / API keys — tests that exercise the daemon's
    config-validation path build their own config inline.
    """
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    # Mirror a subset of the scaffold dirs so ``vault_create`` can land
    # records without blowing up on missing parents.
    for sub in ("session", "task", "note", "project"):
        (vault_dir / sub).mkdir()

    return TalkerConfig(
        bot_token="test-token",
        allowed_users=[1],
        primary_users=["person/Andrew Newton"],
        anthropic=AnthropicConfig(
            api_key="test-key",
            model="claude-sonnet-4-6",
        ),
        stt=STTConfig(api_key="test-stt", model="whisper-large-v3"),
        session=SessionConfig(
            gap_timeout_seconds=1800,
            state_path=str(tmp_path / "talker_state.json"),
        ),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
    )


# --- Fake Anthropic client ------------------------------------------------


@dataclass
class _FakeBlock:
    """Minimal stand-in for an SDK content block (has ``type`` + extras)."""

    type: str
    text: str = ""
    name: str = ""
    id: str = ""
    input: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type}
        if self.type == "text":
            out["text"] = self.text
        elif self.type == "tool_use":
            out["id"] = self.id
            out["name"] = self.name
            out["input"] = dict(self.input)
        return out


@dataclass
class _FakeResponse:
    content: list[_FakeBlock]
    stop_reason: str = "end_turn"


class FakeMessages:
    """Stand-in for ``client.messages`` that returns pre-canned responses.

    The ``responses`` list is consumed in order. Each entry is either a
    ``_FakeResponse`` or a tuple ``(text, stop_reason)``. The last call
    records the kwargs passed so tests can inspect ``model`` / ``max_tokens``.
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        if not self._responses:
            return _FakeResponse(content=[_FakeBlock(type="text", text="(done)")])
        nxt = self._responses.pop(0)
        if isinstance(nxt, _FakeResponse):
            return nxt
        text, stop = nxt
        return _FakeResponse(
            content=[_FakeBlock(type="text", text=text)],
            stop_reason=stop,
        )


class FakeAnthropicClient:
    """Top-level fake with a ``.messages`` attribute, mirroring the SDK."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.messages = FakeMessages(responses or [])


@pytest.fixture
def fake_client() -> FakeAnthropicClient:
    return FakeAnthropicClient()


# Re-export the block helper so tests can build tool_use responses.
FakeBlock = _FakeBlock
FakeResponse = _FakeResponse
