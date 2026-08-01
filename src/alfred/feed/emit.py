"""``FeedEmitHandle`` — the minimal plumbing contract for a classify-time emit.

#27 slice 1: the curator's email classifier upserts an ``email_urgent`` item at
classify time. It is NOT a feed producer daemon (no reconcile, no batch), so it
does not hold a ``FeedConfig`` or a daemon's instance context — it just needs
enough to construct a ``FeedItem`` and drop it into the right store. This handle
carries exactly that, resolved ONCE at the orchestrator's ``_run_curator`` (where
the unified ``raw`` config lives) and threaded down as an OPTIONAL param whose
default absence (``None``) means NO EMIT — the fail-safe: an unwired caller (the
backfill CLI, older call sites, minimal test configs) never touches the feed.

Three fields, deliberately minimal (resist growth — anything richer belongs in a
producer, not a per-item classify-time emit):

  * ``store_path`` — the instance-resolved feed file (``feed.store_path`` or the
    per-instance default), so the emit lands in the SAME store the sync producer
    and the deck read.
  * ``instance`` — the instance DISPLAY name (``instance_name_from_raw``); the
    ``FeedItem`` needs it for its instance chip and the classifier has no other
    source for it (verified #27: neither ``EmailClassifierConfig`` nor
    ``classify_record``'s signature carries the instance name).
  * ``enabled`` — the feed master switch (``feed.enabled``); False → an explicit
    skip (ILB: idle is distinguishable from broken), never a silent drop.

This module imports stdlib only, so ``alfred.feed`` stays a true leaf (the
curator/email_classifier → feed edge has no back-edge — pinned by the acyclic
import test).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeedEmitHandle:
    """Minimal per-item feed-emit context (see module docstring)."""

    store_path: str
    instance: str
    enabled: bool = True
