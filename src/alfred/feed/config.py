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
logging.dir"), falling back to the daily_sync state dir. Salem's ``logging.dir``
IS ``./data``, so its resolved path is byte-identical to the old literal and its
existing store carries over with zero migration. An explicit ``feed.store_path``
ALWAYS wins.

**When NOTHING anchors it, the path is EMPTY and the feed is OFF** (#74). The
resolver used to fall back to a cwd-relative ``./data`` as its last rung, which
is how the suite kept writing ``data/feed_items.jsonl`` into the repo tree: the
daily-sync fire path loads this config from a raw dict that, in tests, carries
no ``logging`` block at all. There is no correct answer for a config that names
no data dir, and guessing the cwd is the #53 defect itself — so the guess is
gone. Every real instance config sets ``logging.dir`` (all four on the box, and
``config.yaml.example``), so production never reaches the empty case; when it
somehow does, :meth:`FeedConfig.__post_init__` forces ``enabled`` False with a
loud warning rather than letting a writer aim at the process cwd.

That coercion is deliberately on the DATACLASS, not in ``load_from_unified``:
the leak's sibling half is a bare ``FeedConfig()`` reached through
``BriefConfig``'s ``default_factory``, which never touches the loader. One
mechanism covers both entry points.

Built by hand (not the recursive ``_build``) — flat, and it sidesteps the
``_build`` collision footgun entirely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import structlog

from alfred.common.instance_paths import configured_logging_dir

from .store import DEFAULT_COMPACT_THRESHOLD_BYTES

log = structlog.get_logger(__name__)

# The default feed store filename, placed inside the resolved instance data dir.
DEFAULT_STORE_FILENAME = "feed_items.jsonl"

# Latch for the unanchored-path warning — once per process, not per construction
# (a bare ``FeedConfig()`` is built by several test fixtures per run).
_unanchored_logged = False


@dataclass
class FeedConfig:
    enabled: bool = True
    # NO default path. An unanchored feed is an OFF feed, never a cwd-relative
    # one — see the module docstring. ``load_from_unified`` fills this in from
    # the instance's data dir whenever ``feed.store_path`` is omitted.
    store_path: str = ""
    compact_threshold_bytes: int = DEFAULT_COMPACT_THRESHOLD_BYTES

    def __post_init__(self) -> None:
        """Force ``enabled`` off when there is nowhere to write.

        Fail-SAFE rather than fail-loud-and-crash: the feed is a convenience
        surface and must never break the brief or the daily sync, so an
        unresolvable store turns the feature off instead of raising. It is
        LOUD about it (latched once per process) — an enabled-but-pathless
        feed silently writing nothing is precisely the ambiguity
        ``feedback_intentionally_left_blank.md`` exists to kill.

        Every writer already gates on ``enabled`` (``_emit_brief_feed``, the
        daily-sync fire's feed block, ``FeedEmitHandle``), so flipping this
        one flag is enough to close every write path. The single UNgated
        reader (the brief's medium-waiting line) opens the empty path
        read-only and renders the honest zero-state.
        """
        if self.enabled and not self.store_path.strip():
            self.enabled = False
            global _unanchored_logged
            if not _unanchored_logged:
                _unanchored_logged = True
                log.warning(
                    "feed.config.disabled_no_store_path",
                    reason="unanchored_store_path",
                    detail="feed.store_path is empty and no instance data dir "
                           "could be derived (set logging.dir, or "
                           "feed.store_path explicitly). The feed is OFF for "
                           "this config — it will not write anywhere, and in "
                           "particular will NOT fall back to the process cwd.",
                )


def _instance_data_dir(raw: dict[str, Any]) -> str | None:
    """The instance's data directory, or ``None`` when the config names none.

    Prefer ``logging.dir`` (the dedicated data-dir base every instance config
    sets: Salem ``./data``, KAL-LE ``/home/andrew/.alfred/kalle/data``); then the
    directory of ``daily_sync.state.path`` (the primary feed producer's own
    per-instance state). There is no third rung: the old ``./data`` fallback
    was a guess at the process cwd, and guessing the cwd IS the bug (#74).

    The ``logging.dir`` read is the shared
    :func:`alfred.common.instance_paths.configured_logging_dir`; the
    ``daily_sync`` rung below it is feed-specific (the daily-sync producer is
    the feed's primary writer), so this stays a feed-local function that layers
    that rung onto the shared read.
    """
    d = configured_logging_dir(raw)
    if d:
        return d
    ds = raw.get("daily_sync")
    if isinstance(ds, dict):
        state = ds.get("state")
        if isinstance(state, dict):
            p = state.get("path")
            if isinstance(p, str) and p.strip():
                return os.path.dirname(p.strip()) or "."
    return None


def _default_store_path(raw: dict[str, Any]) -> str:
    """The derived store path, or ``""`` when nothing anchors it."""
    data_dir = _instance_data_dir(raw)
    if not data_dir:
        return ""
    # String join (not pathlib) so a ``./data`` anchor keeps its exact string —
    # Salem's resolved default stays byte-identical to the legacy value.
    return f"{data_dir.rstrip('/')}/{DEFAULT_STORE_FILENAME}"


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
