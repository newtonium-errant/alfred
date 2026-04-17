"""Persistent state tracking for active/closed talker sessions.

Schema:
    {
      "version": 1,
      "active_sessions": {
        "<chat_id>": {
          "session_id": "uuid",
          "started_at": "iso",
          "last_message_at": "iso",
          "model": "claude-sonnet-4-6",
          "transcript": [{"role": "user|assistant", "content": "..."}],
          "vault_ops": []
        }
      },
      "closed_sessions": [ ... up to MAX_CLOSED entries ... ]
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .utils import get_logger

log = get_logger(__name__)

MAX_CLOSED = 50


def _empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "active_sessions": {},
        "closed_sessions": [],
    }


class StateManager:
    """Load/save talker session state from a JSON file.

    Writes are atomic (tmp file + rename). The manager holds the most
    recently loaded/saved state dict in `self.state` so callers that
    want the full dict can grab it, but the public API is chat-id
    scoped.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.state: dict[str, Any] = _empty_state()

    # --- persistence ---

    def load(self) -> dict[str, Any]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                # Fill in any missing top-level keys defensively.
                merged = _empty_state()
                merged.update(data or {})
                merged.setdefault("active_sessions", {})
                merged.setdefault("closed_sessions", [])
                self.state = merged
                log.info(
                    "state.loaded",
                    active=len(self.state["active_sessions"]),
                    closed=len(self.state["closed_sessions"]),
                )
            except (json.JSONDecodeError, KeyError) as e:
                log.warning("state.load_failed", error=str(e))
                self.state = _empty_state()
        else:
            self.state = _empty_state()
        return self.state

    def save(self, state: dict[str, Any] | None = None) -> None:
        if state is not None:
            self.state = state
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self.path)
        log.debug("state.saved", path=str(self.path))

    # --- session helpers ---

    def get_active(self, chat_id: int | str) -> dict[str, Any] | None:
        """Return the active session dict for `chat_id`, or None."""
        return self.state.get("active_sessions", {}).get(str(chat_id))

    def set_active(self, chat_id: int | str, session: dict[str, Any]) -> None:
        """Upsert an active session for `chat_id`. Does not save to disk."""
        self.state.setdefault("active_sessions", {})[str(chat_id)] = session

    def pop_active(self, chat_id: int | str) -> dict[str, Any]:
        """Remove and return the active session for `chat_id`.

        Raises KeyError if no active session exists for the chat.
        """
        sessions = self.state.setdefault("active_sessions", {})
        return sessions.pop(str(chat_id))

    def append_closed(self, summary: dict[str, Any]) -> None:
        """Append a closed-session summary, trimming to MAX_CLOSED entries."""
        closed = self.state.setdefault("closed_sessions", [])
        closed.append(summary)
        if len(closed) > MAX_CLOSED:
            # Keep the most recent MAX_CLOSED entries.
            del closed[: len(closed) - MAX_CLOSED]
