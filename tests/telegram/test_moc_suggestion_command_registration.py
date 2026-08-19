"""Phase 5 Sub-arc D2 — slash command registration gate (2026-05-19).

Pins the Hypatia-only registration gate for ``/moc-suggestions`` +
``/accept-moc`` + ``/reject-moc``. The three commands are registered
ONLY when ``telegram.moc_suggestions.command_enabled: true`` is set in
the instance config. Salem + KAL-LE leave the block absent so the
commands aren't registered.

Mirror of ``test_inventory_views.py``'s command-registration testing
pattern (Phase 4 Sub-arc C). The pattern protects against silent
cross-instance leakage — e.g., Salem accidentally exposing
``/accept-moc`` and writing to its operational vault when no
MOC-suggestion queue exists.
"""

from __future__ import annotations

from alfred.telegram.config import MocSuggestionsConfig


# ---------------------------------------------------------------------------
# Registration gate — defaults
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Config loading — load_from_unified plumbing
# ---------------------------------------------------------------------------


def test_load_from_unified_picks_up_moc_suggestions_block() -> None:
    """``telegram.moc_suggestions:`` block in YAML lands on
    ``TalkerConfig.moc_suggestions`` after load_from_unified."""
    from alfred.telegram.config import load_from_unified
    raw = {
        "telegram": {
            "instance": {"name": "Hypatia"},
            "bot_token": "x",
            "anthropic": {"api_key": "x"},
            "stt": {"api_key": "x"},
            "moc_suggestions": {
                "command_enabled": True,
                "queue_path": "/home/andrew/.alfred/hypatia/data/moc_suggestions.jsonl",
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.moc_suggestions is not None
    assert cfg.moc_suggestions.command_enabled is True
    assert cfg.moc_suggestions.queue_path == "/home/andrew/.alfred/hypatia/data/moc_suggestions.jsonl"


def test_load_from_unified_block_absent_leaves_none() -> None:
    """Block absent → ``moc_suggestions=None`` (the default sentinel)."""
    from alfred.telegram.config import load_from_unified
    raw = {
        "telegram": {
            "instance": {"name": "Salem"},
            "bot_token": "x",
            "anthropic": {"api_key": "x"},
            "stt": {"api_key": "x"},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.moc_suggestions is None


def test_load_from_unified_block_empty_dict_leaves_none() -> None:
    """Block present but empty dict → still None (matches inventory_views shape)."""
    from alfred.telegram.config import load_from_unified
    raw = {
        "telegram": {
            "instance": {"name": "Salem"},
            "bot_token": "x",
            "anthropic": {"api_key": "x"},
            "stt": {"api_key": "x"},
            "moc_suggestions": {},
        },
    }
    cfg = load_from_unified(raw)
    # Same pattern as inventory_views: empty dict treated as "not opt-in".
    # If you want it on, set command_enabled explicitly.
    assert cfg.moc_suggestions is None or cfg.moc_suggestions.command_enabled is False


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_moc_suggestions_config_defaults() -> None:
    """Default state matches the disabled-by-default convention used by
    inventory_views / fiction / voice_train."""
    cfg = MocSuggestionsConfig()
    assert cfg.command_enabled is False
    assert cfg.queue_path is None
