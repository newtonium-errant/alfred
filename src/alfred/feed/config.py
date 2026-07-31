"""Feed config — the ``feed:`` block of the unified config.

``enabled`` defaults **true**: the feed is a LOCAL JSONL write (never outward),
and the belt makes it safe — so it accumulates truth from day one with no
operator flip.

**Store path is INSTANCE-scoped, not just tool-scoped.** On the box every
instance shares one WorkingDirectory (``/home/andrew/alfred``) and differs only
by ``--config``, so a cwd-relative tool-scoped default (``./data/feed_items.jsonl``)
is STILL one shared file across instances — KAL-LE's sync then writes into
Salem's feed (2026-07-31 cross-instance contamination: Salem's deck dealt KAL-LE's
cards, the router correctly 409'd them). So when ``feed.store_path`` is not
explicitly configured, it resolves to a file in the INSTANCE's own data dir,
anchored on ``logging.dir`` (the codebase's data-dir base — "data_dir defaults to
logging.dir"), falling back to the daily_sync state dir, then ``./data`` (the
legacy cwd-relative default, which keeps Salem's resolved path byte-identical so
its existing store carries over with zero migration). An explicit
``feed.store_path`` ALWAYS wins.

Built by hand (not the recursive ``_build``) — flat, and it sidesteps the
``_build`` collision footgun entirely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .store import DEFAULT_COMPACT_THRESHOLD_BYTES

# The default feed store filename, placed inside the resolved instance data dir.
DEFAULT_STORE_FILENAME = "feed_items.jsonl"
# The legacy cwd-relative default — also the fallback data dir when a config
# carries neither ``logging.dir`` nor a ``daily_sync`` state path.
_LEGACY_DATA_DIR = "./data"


@dataclass
class FeedConfig:
    enabled: bool = True
    # Placeholder default for direct construction; ``load_from_unified`` replaces
    # it with the instance-resolved path whenever ``feed.store_path`` is omitted.
    store_path: str = f"{_LEGACY_DATA_DIR}/{DEFAULT_STORE_FILENAME}"
    compact_threshold_bytes: int = DEFAULT_COMPACT_THRESHOLD_BYTES


def _instance_data_dir(raw: dict[str, Any]) -> str:
    """The instance's data directory — the anchor for the default store path.

    Prefer ``logging.dir`` (the dedicated data-dir base every instance config
    sets: Salem ``./data``, KAL-LE ``/home/andrew/.alfred/kalle/data``); then the
    directory of ``daily_sync.state.path`` (the primary feed producer's own
    per-instance state); finally ``./data`` (legacy cwd-relative — keeps Salem
    unchanged and covers minimal test configs).
    """
    logging_block = raw.get("logging")
    if isinstance(logging_block, dict):
        d = logging_block.get("dir")
        if isinstance(d, str) and d.strip():
            return d.strip()
    ds = raw.get("daily_sync")
    if isinstance(ds, dict):
        state = ds.get("state")
        if isinstance(state, dict):
            p = state.get("path")
            if isinstance(p, str) and p.strip():
                return os.path.dirname(p.strip()) or "."
    return _LEGACY_DATA_DIR


def _default_store_path(raw: dict[str, Any]) -> str:
    # String join (not pathlib) so a ``./data`` anchor keeps its exact string —
    # Salem's resolved default stays byte-identical to the legacy value.
    return f"{_instance_data_dir(raw).rstrip('/')}/{DEFAULT_STORE_FILENAME}"


def load_from_unified(raw: dict[str, Any]) -> FeedConfig:
    """Build FeedConfig from the unified config dict. Schema-tolerant: unknown
    keys in the ``feed:`` block are ignored (forward-compat).

    An explicit ``feed.store_path`` always wins; an omitted / blank one resolves
    per-instance (see module docstring) so co-located instances never share one
    feed file. Every feed caller (daily_sync producer, brief producer, talker/
    transport wiring) reaches this ONE resolver with the same unified ``raw``, so
    they resolve the SAME path within an instance and DIFFERENT paths across.
    """
    block = raw.get("feed", {}) or {}
    known = {k: v for k, v in block.items() if k in FeedConfig.__dataclass_fields__}
    sp = known.get("store_path")
    if not (isinstance(sp, str) and sp.strip()):
        known["store_path"] = _default_store_path(raw)
    return FeedConfig(**known)
