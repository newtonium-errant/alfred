"""The segment-rich transcript shape — delta-ready + diarization-ready (P2-b).

This is the LOAD-BEARING data shape for the whole scribe pipeline: it is
designed so P3 (checkpoint-delta) and P4 (diarization) slot in WITHOUT a
rebuild.

  Transcript = {
    source_id, mode, version, processed_through_segment: null, diarized: false,
    segments: [ {id: "S1", start_s, end_s, text,
                 speaker: null, speaker_cluster: null, speaker_conf: null} ]
  }

  * ``segments`` carry STABLE ids (``S1``, ``S2``, ...) — the ``[S#]`` grounding
    contract the note-gen (P2-c) + deterministic grounding-verify both cite.
  * ``speaker`` / ``speaker_cluster`` / ``speaker_conf`` (all null in P2/P3) —
    the P4 DIARIZATION slots: the RESOLVED role, the raw pyannote cluster (for
    re-label without re-STT), and the diarization purity.
  * ``diarized`` (false in P2/P3) — the P4 gate: true once a diarizer has
    resolved the per-segment roles (the P4-2 safety net reads it).
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


class SegmentInvariantError(Exception):
    """The accumulated segment sequence violated its post-append invariant — ids
    are NOT unique + strictly increasing. Raised FAIL-CLOSED by
    :meth:`Transcript.append_chunk`: a broken segment-id sequence means a later
    ``[S#]`` grounding cite could resolve to the WRONG segment (the exact
    medico-legal failure the scribe exists to prevent), so the fold aborts rather
    than persist a corrupt transcript."""


def _assert_segment_ids_monotonic(segments: list["Segment"]) -> None:
    """FAIL-CLOSED invariant: segment ids are UNIQUE and STRICTLY INCREASING.

    The ids are canonical ``S1``, ``S2``, ... (see :func:`make_segment_id`), so a
    correct accumulated transcript has strictly-increasing integer suffixes with
    no duplicates. This guards the append fold against an id-minting bug: a
    duplicate id would make grounding's ``{id: segment}`` map silently last-wins
    overwrite, grounding a claim against the wrong segment.
    """
    ids = [s.id for s in segments]
    if len(set(ids)) != len(ids):
        raise SegmentInvariantError(
            f"duplicate segment ids after append (n={len(ids)}, "
            f"unique={len(set(ids))}) — refusing to persist a corrupt transcript."
        )
    try:
        nums = [int(sid[1:]) for sid in ids]  # "S3" -> 3
    except (ValueError, IndexError) as e:
        raise SegmentInvariantError(
            f"non-canonical segment id in accumulated transcript: {ids!r}"
        ) from e
    if any(b <= a for a, b in zip(nums, nums[1:])):
        raise SegmentInvariantError(
            f"segment ids not strictly increasing: {ids!r}"
        )


def make_segment_id(index_zero_based: int) -> str:
    """Canonical stable segment id: ``S1``, ``S2``, ... (1-indexed).

    THE single source of truth for the ``[S#]`` grounding contract — the STT
    (which mints ids) and the grounding-verify (which resolves ``[S#]`` cites)
    MUST both go through this so the ids never drift.
    """
    return f"S{index_zero_based + 1}"


# --- P4 diarization role vocabulary (the RESOLVED-ROLE slot) ----------------
# ``Segment.speaker`` holds a RESOLVED ROLE from this closed set — NEVER a raw
# pyannote cluster id (that is ``speaker_cluster``). The set is closed by design:
# attribution (P4-2) + note-gen (P4-3) branch on these exact literals, so a role
# outside this set would silently mis-route a clinical claim.
ROLE_CLINICIAN = "clinician"
ROLE_PATIENT = "patient"
ROLE_OTHER = "other"
# The FAIL-CLOSED sentinel. A diarizer that cannot confidently resolve a turn
# (below purity/match threshold, an un-enrolled voice, a raw cluster id like
# ``SPEAKER_00``, or a role-assigner leak) MUST degrade to this — never a silent
# known-role. Downstream attribution treats ``unknown`` as un-verified, so it can
# never be laundered into a clean patient/clinician attribution.
ROLE_UNKNOWN = "unknown"

# Case-insensitive raw-label → canonical-role fold. EVERYTHING not listed —
# including None, ``""``, a raw pyannote cluster (``SPEAKER_00``), or any garbage
# — folds to ``unknown`` (fail-closed).
_ROLE_ALIASES: dict[str, str] = {
    "clinician": ROLE_CLINICIAN,
    "doctor": ROLE_CLINICIAN,
    "provider": ROLE_CLINICIAN,
    "patient": ROLE_PATIENT,
    "caregiver": ROLE_OTHER,
    "family": ROLE_OTHER,
    "other": ROLE_OTHER,
}


def normalize_role(raw: str | None) -> str:
    """Fold a raw role label to a canonical :data:`ROLE_*` — THE single source of
    truth shared by the diarizer-writer (P4-1) and the notegen/attribution-reader
    (P4-2/3).

    Case-insensitive. Maps clinician/doctor/provider→``clinician``,
    patient→``patient``, caregiver/family/other→``other``. EVERYTHING ELSE —
    ``None``, ``""``, a raw pyannote cluster (``SPEAKER_00``), or any garbage —
    folds to ``unknown`` (FAIL-CLOSED: a role-assigner leak degrades to
    un-attributed, never a silent known-role).
    """
    if not isinstance(raw, str):
        return ROLE_UNKNOWN
    return _ROLE_ALIASES.get(raw.strip().lower(), ROLE_UNKNOWN)


@dataclass
class Segment:
    """One transcript segment. ``id`` is the stable ``[S#]`` grounding anchor.

    P4 diarization slots (all null in P2/P3):
      * ``speaker`` — the RESOLVED ROLE (:data:`ROLE_*`; ``normalize_role`` is
        the sole writer). NOT a raw cluster.
      * ``speaker_cluster`` — the raw pyannote cluster id (e.g. ``SPEAKER_00``),
        retained so a future re-label can re-derive roles WITHOUT re-running STT.
      * ``speaker_conf`` — the diarization purity/confidence for this turn (the
        signal P4-2 uses to fail-closed a low-purity turn to ``unknown``).
    """

    id: str
    start_s: float
    end_s: float
    text: str
    speaker: str | None = None
    speaker_cluster: str | None = None
    speaker_conf: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start_s": self.start_s,
            "end_s": self.end_s,
            "text": self.text,
            "speaker": self.speaker,
            "speaker_cluster": self.speaker_cluster,
            "speaker_conf": self.speaker_conf,
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
    # P3-b1 checkpoint-accumulator provenance. One entry per FOLDED chunk (see
    # ``append_chunk``): ``{chunk_key, seq, first_id, last_id, n_segments}``.
    # ``chunk_key`` (a chunk's content-hash) is the idempotency key — a replay of
    # the same chunk is a no-op. ``closed`` latches when the ``_CLOSED`` sentinel
    # finalizes the encounter (no more chunks). Both round-trip through
    # to_dict/from_dict (schema-tolerant), so the persisted ledger resumes.
    chunk_provenance: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False
    # P4 diarization gate (false in P2/P3). True once a diarizer has RESOLVED the
    # per-segment ``speaker`` roles on this transcript. The safety-net reader
    # (P4-2 ``speaker_attribution``) gates on this: only a ``diarized`` transcript
    # carries trustworthy roles, so an un-diarized transcript is never treated as
    # "attribution verified". Round-trips through to_dict/from_dict AND the
    # ``delta()`` constructor (the flat process_source path derives its note from
    # ``.delta()`` — dropping the gate there would silently un-diarize it).
    diarized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "mode": self.mode,
            "version": self.version,
            "processed_through_segment": self.processed_through_segment,
            "segments": [s.to_dict() for s in self.segments],
            "chunk_provenance": [dict(p) for p in self.chunk_provenance],
            "closed": self.closed,
            "diarized": self.diarized,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transcript":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        raw_segments = known.pop("segments", None) or []
        known["segments"] = [
            Segment.from_dict(s) for s in raw_segments if isinstance(s, dict)
        ]
        raw_prov = known.get("chunk_provenance")
        known["chunk_provenance"] = [
            dict(p) for p in raw_prov if isinstance(p, dict)
        ] if isinstance(raw_prov, list) else []
        return cls(**known)

    def has_folded(self, chunk_key: str) -> bool:
        """True iff a chunk with this content-hash was already folded (the
        idempotency check — replay/dup detection)."""
        return any(p.get("chunk_key") == chunk_key for p in self.chunk_provenance)

    def append_chunk(
        self,
        chunk: "Transcript",
        *,
        audio_offset_s: float,
        chunk_key: str,
        seq: int,
    ) -> bool:
        """Fold ``chunk``'s segments onto the end of this accumulated transcript.

        The segment-id continuity CORE (scribe P3-b1). Guarantees:

          (a) IDEMPOTENT on ``chunk_key`` (the chunk's content-hash): a replay or
              duplicate is a NO-OP (returns ``False``), never a second append.
          (b) Final ids are minted at APPEND time as
              ``make_segment_id(len(self.segments) + i)`` — the chunk's own
              STT-local ids are DISCARDED. Already-appended segments are
              IMMUTABLE (prior chunks are never re-STT'd or renumbered).
          (c) Incoming ``start_s`` / ``end_s`` are offset by ``audio_offset_s`` so
              timestamps stay globally monotonic across chunk boundaries.
          (d) Records ``chunk_key`` + ``seq`` + contributed ``[S#]`` id-range in
              ``chunk_provenance``.
          (e) ASSERTS the post-append invariant (ids unique + strictly
              increasing) and fails CLOSED (:class:`SegmentInvariantError`)
              otherwise — no corrupt transcript is ever persisted.

        Returns ``True`` if the chunk was folded, ``False`` on an idempotent
        no-op.
        """
        if self.has_folded(chunk_key):
            return False  # (a) idempotent replay/dup — no second append

        base = len(self.segments)
        new_segments = [
            Segment(
                id=make_segment_id(base + i),          # (b) final ids at APPEND
                start_s=seg.start_s + audio_offset_s,  # (c) global-monotonic offset
                end_s=seg.end_s + audio_offset_s,
                text=seg.text,
                speaker=seg.speaker,                   # P4 resolved role (carried)
                speaker_cluster=seg.speaker_cluster,   # P4 raw cluster (carried)
                speaker_conf=seg.speaker_conf,         # P4 purity (carried)
            )
            for i, seg in enumerate(chunk.segments)
        ]
        self.segments.extend(new_segments)
        # (e) fail-closed BEFORE recording provenance — a corrupt fold must not
        # leave a provenance entry claiming success. Roll the extend back on
        # violation so a caught error leaves the transcript UNMUTATED (never a
        # half-corrupt object that a later save could persist).
        try:
            _assert_segment_ids_monotonic(self.segments)
        except SegmentInvariantError:
            del self.segments[base:]
            raise
        self.chunk_provenance.append({           # (d) provenance
            "chunk_key": chunk_key,
            "seq": seq,
            "first_id": new_segments[0].id if new_segments else None,
            "last_id": new_segments[-1].id if new_segments else None,
            "n_segments": len(new_segments),
        })
        # P4: a diarized chunk latches the accumulated transcript's ``diarized``
        # gate, so the checkpoint reader (P4-2, which loads the LEDGER) sees a
        # truthful gate — otherwise a fully-diarized accumulation would present as
        # un-diarized and be banner-flagged forever. Per-segment ``speaker`` still
        # carries the (possibly ``unknown``) role for any un-attributed segment.
        if chunk.diarized:
            self.diarized = True
        return True

    def unprocessed_segments(self) -> list[Segment]:
        """Segments AFTER ``processed_through_segment`` (the P3 delta cursor).

        For a fresh transcript (cursor ``None``) this is ALL segments — the P2
        whole-file batch. P3 sets the cursor so a later checkpoint dump folds in
        only the NEW segments. A cursor pointing at an id not present is treated
        defensively as "process all" (never silently drop segments)."""
        if self.processed_through_segment is None:
            return list(self.segments)
        ids = [s.id for s in self.segments]
        try:
            cut = ids.index(self.processed_through_segment) + 1
        except ValueError:
            return list(self.segments)
        return list(self.segments[cut:])

    def delta(self) -> "Transcript":
        """A Transcript of only the unprocessed segments (delta-ready wiring).

        The segments keep their ORIGINAL ids (S1, S2, ...) so the ``[S#]``
        grounding contract still resolves. For P2 (cursor ``None``) this is the
        whole transcript; the pipeline processes the delta so P3 slots in
        without a rebuild.

        ⚠ ``diarized`` MUST be carried here — the flat ``process_source`` path
        derives its note from ``.delta()``, so dropping the gate would silently
        un-diarize a diarized transcript (a frozen-contract requirement)."""
        return Transcript(
            source_id=self.source_id,
            mode=self.mode,
            segments=self.unprocessed_segments(),
            version=self.version,
            processed_through_segment=self.processed_through_segment,
            diarized=self.diarized,
        )
