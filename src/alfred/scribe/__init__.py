"""Sovereign ambient-scribe package (STAY-C clinical instance).

P1-c surface: the ``scribe:`` config, the fail-closed mode-gate (the legal line
in code), and the attestation-integrity controls (forward-only lifecycle +
distinct-human-clinician attester). P1-c does NOT wire the audio→note pipeline
(P2) nor a daemon (P1-d — where the scribe daemon must self-install the
sovereign http guard in its own process).
"""

from __future__ import annotations

from .attestation import (
    SCRIBE_DRAFTER_IDENTITY,
    STATUS_AI_DRAFT,
    STATUS_AMENDED,
    STATUS_ATTESTED,
    AttestationError,
    authorize_attestation,
    validate_attester,
    validate_status_transition,
)
from .config import (
    SCRIBE_MODE_CLINICAL,
    SCRIBE_MODE_SYNTHETIC,
    ScribeConfig,
    ScribeLlmConfig,
    ScribeSttConfig,
    load_from_unified,
)
from .attest import ATTEST_SCOPE, attest, source_id_for
from .identity import EncounterIdentityError, compute_encounter_id
from .grounding import (
    GroundingIntegrityError,
    GroundingResult,
    verify as verify_grounding,
)
from .ingest import ScribeIngestRefused, guard_ingest
from .ledger import ledger_path, load_ledger, save_ledger
from .notegen import (
    GROUNDING_UNVERIFIED,
    NOT_ADDRESSED,
    REASONING_NOT_STATED,
    SOAP_SECTIONS,
    Claim,
    ContextBudgetExceeded,
    NoteGenError,
    StructuredNote,
    generate_structured,
    parse_structured_json,
    render_soap,
)
from .stt import (
    SCRIBE_STT_PROVIDERS,
    MissingSTTDependency,
    STTError,
    ensure_backend_available,
    transcribe,
)
from .transcript import (
    Segment,
    SegmentInvariantError,
    Transcript,
    make_segment_id,
)
# pipeline + state imported LAST — pipeline pulls in stt/notegen/transcript, so
# they must already be package submodules to avoid a partial-init ordering trap.
from .pipeline import (  # noqa: E402
    AccumResult,
    VerifiedNote,
    accumulate_encounter,
    checkpoint_encounter,
    generate_verified_note,
    is_chunk_settled,
    process_source,
    run_sweep,
)
from .state import (  # noqa: E402
    STATE_ATTESTED,
    STATE_BUDGET_CAPPED,
    STATE_DRAFTED,
    STATE_FAILED,
    STATE_HUMAN_EDITED,
    STATE_READY,
    STATE_RECORDED,
    STATE_REFUSED,
    STATE_STRUCTURING,
    STATE_TRANSCRIBING,
    ScribeState,
    SourceState,
)

__all__ = [
    # config
    "ScribeConfig",
    "ScribeSttConfig",
    "ScribeLlmConfig",
    "SCRIBE_MODE_SYNTHETIC",
    "SCRIBE_MODE_CLINICAL",
    "load_from_unified",
    # mode-gate
    "guard_ingest",
    "ScribeIngestRefused",
    # attestation integrity
    "AttestationError",
    "SCRIBE_DRAFTER_IDENTITY",
    "STATUS_AI_DRAFT",
    "STATUS_ATTESTED",
    "STATUS_AMENDED",
    "validate_status_transition",
    "validate_attester",
    "authorize_attestation",
    # structural attestation orchestrator (#41)
    "attest",
    "source_id_for",
    "ATTEST_SCOPE",
    # salted opaque encounter identity (P3-b1)
    "compute_encounter_id",
    "EncounterIdentityError",
    # STT + transcript (P2-b)
    "transcribe",
    "ensure_backend_available",
    "SCRIBE_STT_PROVIDERS",
    "MissingSTTDependency",
    "STTError",
    "Transcript",
    "Segment",
    "SegmentInvariantError",
    "make_segment_id",
    # transcript ledger + checkpoint accumulator (P3-b1)
    "ledger_path",
    "load_ledger",
    "save_ledger",
    "accumulate_encounter",
    "is_chunk_settled",
    "AccumResult",
    # note-gen + grounding (P2-c)
    "generate_structured",
    "parse_structured_json",
    "render_soap",
    "StructuredNote",
    "Claim",
    "NoteGenError",
    "ContextBudgetExceeded",
    "SOAP_SECTIONS",
    "NOT_ADDRESSED",
    "REASONING_NOT_STATED",
    "GROUNDING_UNVERIFIED",
    "verify_grounding",
    "GroundingResult",
    "GroundingIntegrityError",
    # pipeline + state machine (P2-d)
    "generate_verified_note",
    "VerifiedNote",
    "process_source",
    "run_sweep",
    # checkpoint co-pilot (P3-b2)
    "checkpoint_encounter",
    "ScribeState",
    "SourceState",
    "STATE_RECORDED",
    "STATE_TRANSCRIBING",
    "STATE_STRUCTURING",
    "STATE_DRAFTED",
    "STATE_ATTESTED",
    "STATE_REFUSED",
    "STATE_FAILED",
    "STATE_BUDGET_CAPPED",
    "STATE_HUMAN_EDITED",
    "STATE_READY",
]
