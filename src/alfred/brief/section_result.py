"""SectionResult — the brief's producer seam (Feed Phase A).

``generate_brief`` builds a ``list[SectionResult]``; the RENDERER consumes only
``.markdown`` (exactly as it consumed the old ``(name, markdown)`` tuples), so
the brief render stays byte-identical by construction. A section that ALSO feeds
the store carries ``feed_kind`` + ``feed_items`` (Phase A converts health,
slot_suggestion, event, peer_digest); every other section leaves them ``None``
and is markdown-only, exactly as today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid importing the feed model at brief import time
    from alfred.feed import FeedItem


@dataclass
class SectionResult:
    name: str
    markdown: str
    # When set, ``feed_items`` (possibly empty — e.g. all-ok health → mark stale
    # warns acted) is reconciled into the feed store under this kind. ``None``
    # means markdown-only (no feed projection in Phase A).
    feed_kind: str | None = None
    feed_items: list["FeedItem"] = field(default_factory=list)
