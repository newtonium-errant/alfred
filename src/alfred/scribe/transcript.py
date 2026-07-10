"""The segment-rich transcript shape — delta-ready + diarization-ready (P2-b).

This is the LOAD-BEARING data shape for the whole scribe pipeline: it is
designed so P3 (checkpoint-delta) and P4 (diarization) slot in WITHOUT a
rebuild.

  Transcript = {
    source_id, mode, version, processed_through_segment: null,
    segments: [ {id: "S1", start_s, end_s, text, speaker: null} ]
  }

  * ``segments`` carry STABLE ids (``S1``, ``S2``, ...) — the ``[S#]`` grounding
    contract the note-gen (P2-c) + deterministic grounding-verify both cite.
  * ``speaker`` (null in P2) — the P4 DIARIZATION slot.
  * ``version`` (1 in P2) — the P3 NOTE-UPDATE slot (a checkpoint dump that
    updates a note increments it).
  * ``processed_through_segment`` (null in P2) — the P3 DELTA CURSOR: the id of
    the last segment already folded into the structured dataset, so a later
    dump processes only the NEW segments.

P2 is single-file WHOLE-file batch: one dump → version 1 → cursor stays null.
The shape carries the P3/P4 slots but P2 does not populate them.

Schema-tolerance ``from_dict`` (the load-time forward-compat contract): unknown
keys are dropped, missing keys default — so a transcript written by a newer
version loads on an older one and vice-versa.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def make_segment_id(index_zero_based: int) -> str:
    """Canonical stable segment id: ``S1``, ``S2``, ... (1-indexed).

    THE single source of truth for the ``[S#]`` grounding contract — the STT
    (which mints ids) and the grounding-verify (which resolves ``[S#]`` cites)
    MUST both go through this so the ids never drift.
    """
    return f"S{index_zero_based + 1}"


@dataclass
class Segment:
    """One transcript segment. ``id`` is the stable ``[S#]`` grounding anchor;
    ``speaker`` is the P4 diarization slot (null in P2)."""

    id: str
    start_s: float
    end_s: float
    text: str
    speaker: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "text": self.text,
            "speaker": self.speaker,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Segment":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Transcript:
    """The segment-rich transcript. ``version`` + ``processed_through_segment``
    are the P3 slots (note-update + delta cursor); ``speaker`` on each segment
    is the P4 slot. P2 populates neither P3 slot (single-file batch)."""

    source_id: str
    mode: str
    segments: list[Segment] = field(default_factory=list)
    version: int = 1
    processed_through_segment: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "mode": self.mode,
            "version": self.version,
            "processed_through_segment": self.processed_through_segment,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transcript":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        raw_segments = known.pop("segments", None) or []
        known["segments"] = [
            Segment.from_dict(s) for s in raw_segments if isinstance(s, dict)
        ]
        return cls(**known)
