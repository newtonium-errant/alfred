"""Player context-primer contract (Phase C3a — defined for C3c, no consumer yet).

When the operator pauses the briefing player and asks a question ("why is that
yellow?", "dig into Yarmouth"), the question rides the EXISTING chat transport
carrying a small primer so Salem's answer resolves against WHAT'S ON SCREEN — the
brief being played and the current slide/segment. This module DEFINES that
carry-shape now (with a validation pin) so C3c wires the producer (the player) +
consumer (the chat turn) against a stable contract; nothing consumes it yet.

The shape is deliberately tiny — two stable keys:
  * ``brief_date`` — the brief being played (ISO ``YYYY-MM-DD``), so the answer
    is grounded in that day's brief, not a re-derived "today".
  * ``section_id`` — the on-screen segment's stable id (one of the narration
    :data:`~alfred.brief.narration.SEGMENT_ORDER` values), so "that" / "it"
    resolves to the slide the operator is looking at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .narration import SEGMENT_ORDER

# The section ids a primer may name — the narration segment vocabulary. A primer
# with an unknown section_id is a client/version drift signal (fail-loud on
# validate, never silently ground a question on a bogus slide).
KNOWN_PRIMER_SECTIONS: frozenset[str] = frozenset(SEGMENT_ORDER)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class PlayerContextPrimer:
    """The chat-turn primer carrying the player's on-screen context.

    ``valid`` is the fail-loud gate C3c's consumer checks before grounding a
    turn on the primer: a bad date or an unknown section id ⟹ ignore the primer
    and answer without slide-grounding (never fabricate a slide context).
    """

    brief_date: str
    section_id: str

    @property
    def valid(self) -> bool:
        return (
            bool(_ISO_DATE.match(self.brief_date or ""))
            and self.section_id in KNOWN_PRIMER_SECTIONS
        )

    def to_dict(self) -> dict[str, Any]:
        return {"brief_date": self.brief_date, "section_id": self.section_id}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PlayerContextPrimer":
        d = data or {}
        return cls(
            brief_date=str(d.get("brief_date", "") or ""),
            section_id=str(d.get("section_id", "") or ""),
        )

    def context_line(self) -> str:
        """The human-readable grounding note the chat turn injects (C3c). Empty
        when the primer is invalid — the consumer then answers un-grounded."""
        if not self.valid:
            return ""
        return (
            f"[Player context: the operator is on the '{self.section_id}' slide "
            f"of the {self.brief_date} briefing. Resolve deictic references "
            f"('that', 'it', 'this') against that slide.]"
        )


__all__ = [
    "KNOWN_PRIMER_SECTIONS",
    "PlayerContextPrimer",
]
