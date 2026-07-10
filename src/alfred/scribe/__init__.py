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
from .ingest import ScribeIngestRefused, guard_ingest
from .stt import (
    SCRIBE_STT_PROVIDERS,
    MissingSTTDependency,
    STTError,
    ensure_backend_available,
    transcribe,
)
from .transcript import Segment, Transcript, make_segment_id

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
    # STT + transcript (P2-b)
    "transcribe",
    "ensure_backend_available",
    "SCRIBE_STT_PROVIDERS",
    "MissingSTTDependency",
    "STTError",
    "Transcript",
    "Segment",
    "make_segment_id",
]
