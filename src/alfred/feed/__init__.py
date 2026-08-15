"""The Feed — one attention-tiered model brief + sync both project into.

Phase A: foundation, invisible. The store accumulates truth; the operator sees
zero change. See ``model.py`` (the FeedItem contract), ``store.py`` (append-only
event log), ``belt.py`` (the swallowed producer entrypoint), ``config.py``.
"""

from __future__ import annotations

from .belt import try_feed_reconcile
from .config import FeedConfig, load_from_unified
from .emit import FeedEmitHandle
from .model import (
    ACTION_RETIRED,
    ATTENTION_FYI,
    ATTENTION_NEEDS_YOU,
    KIND_DEFAULTS,
    KIND_REMINDER_RETURNED,
    KINDS,
    MODE_DECIDE,
    MODE_FYI,
    STATE_ACKED,
    STATE_ACTED,
    STATE_EXPIRED,
    STATE_OPEN,
    FeedItem,
    make_id,
)
from .store import DEFAULT_COMPACT_THRESHOLD_BYTES, FeedStore

__all__ = [
    "ACTION_RETIRED",
    "ATTENTION_FYI",
    "ATTENTION_NEEDS_YOU",
    "DEFAULT_COMPACT_THRESHOLD_BYTES",
    "FeedConfig",
    "FeedEmitHandle",
    "FeedItem",
    "FeedStore",
    "KINDS",
    "KIND_DEFAULTS",
    "KIND_REMINDER_RETURNED",
    "MODE_DECIDE",
    "MODE_FYI",
    "STATE_ACKED",
    "STATE_ACTED",
    "STATE_EXPIRED",
    "STATE_OPEN",
    "load_from_unified",
    "make_id",
    "try_feed_reconcile",
]
