"""The controlled vocabulary of capture Structured Summary sections (#72 item 4).

ONE list, consumed by the renderer that produces the sections and by the
attribution stats that count contests against them. It exists because those two
were about to disagree.

The headings were inline string literals in ``capture_batch.render_summary
_markdown``, and lifting them was the explicit instruction rather than copying:
two hand-maintained copies of a vocabulary drift, and when they do, the stats
key on headings the card no longer shows — a section quietly stops accumulating
contests and reads as healthy. Same failure the snooze ladder had.

The renderer is the AUTHORITY. This module holds the order and the spelling; it
does not know how a section is rendered, and nothing here should grow rendering
logic.

A note for whoever adds the ninth: the eight are not one uniform block in the
renderer. Six go through the local ``_section`` helper, ``Discarded Noise``
follows after an intervening comment, and ``Re-encounters`` is appended
directly because its body is pre-rendered elsewhere. A grep of ``_section(``
finds seven of the eight. That is exactly how a previous pass undercounted this
list, which is the second reason it lives here now.
"""

from __future__ import annotations

# Order matches the rendered summary, top to bottom. The operator sees this
# order when tapping the section a bad inference came from, so it is part of the
# contract rather than incidental.
SUMMARY_SECTIONS: tuple[str, ...] = (
    "Topics",
    "Decisions",
    "Open Questions",
    "Action Items",
    "Key Insights",
    "Raw Contradictions",
    "Discarded Noise",
    "Re-encounters",
)

# The one section whose body is produced elsewhere (capture_source_anchor
# renders it; the summary renderer only supplies the heading). Named because the
# renderer has to branch on it, and a branch on a bare string literal is how the
# spelling escapes back out of this module.
RE_ENCOUNTERS_SECTION = "Re-encounters"

# The brief-mode recap renders a SUBSET of the same vocabulary — same spellings,
# fewer sections. Named rather than re-listed so it cannot drift into a second
# spelling of "Key Insights".
BRIEF_RECAP_SECTIONS: tuple[str, ...] = ("Topics", "Key Insights")

# Membership test for anything arriving from outside (an operator tap, a corpus
# row written by an older build). A section name that is not in the vocabulary
# is not rejected outright by callers — it is filed under the unknown bucket, so
# a renamed heading degrades to "uncategorised" rather than to a crash.
SUMMARY_SECTION_SET: frozenset[str] = frozenset(SUMMARY_SECTIONS)


def is_known_section(name: str) -> bool:
    """True when ``name`` is one of the rendered summary headings, exactly.

    Deliberately exact — no casefolding, no stripping beyond the caller's own.
    The taps that produce these values pick from the list; a value that differs
    in case came from somewhere else and should be visible as unknown rather
    than silently coerced into a bucket it may not belong in.
    """
    return name in SUMMARY_SECTION_SET


__all__ = [
    "BRIEF_RECAP_SECTIONS",
    "RE_ENCOUNTERS_SECTION",
    "SUMMARY_SECTIONS",
    "SUMMARY_SECTION_SET",
    "is_known_section",
]
