"""Feed config — the ``feed:`` block of the unified config.

``enabled`` defaults **true**: the feed is a LOCAL JSONL write (never outward),
and the belt makes it safe — so it accumulates truth from day one with no
operator flip. The store path defaults tool-scoped (``./data/feed_items.jsonl``)
per the state-path rule so an instance that omits the block never collides with
another tool's file.

Built by hand (not the recursive ``_build``) — flat, and it sidesteps the
``_build`` collision footgun entirely (none of ``enabled`` / ``store_path`` /
``compact_threshold_bytes`` is a ``_DATACLASS_MAP`` key, but hand-wiring means a
future nested key can't silently dispatch to the wrong dataclass either).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .store import DEFAULT_COMPACT_THRESHOLD_BYTES


@dataclass
class FeedConfig:
    enabled: bool = True
    store_path: str = "./data/feed_items.jsonl"
    compact_threshold_bytes: int = DEFAULT_COMPACT_THRESHOLD_BYTES


def load_from_unified(raw: dict[str, Any]) -> FeedConfig:
    """Build FeedConfig from the unified config dict. Schema-tolerant: unknown
    keys in the ``feed:`` block are ignored (forward-compat)."""
    block = raw.get("feed", {}) or {}
    known = {k: v for k, v in block.items() if k in FeedConfig.__dataclass_fields__}
    return FeedConfig(**known)
