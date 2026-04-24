"""Pydantic contracts for the non-agentic distiller rebuild.

Week 1 MVP of the distiller rebuild (see
``docs/proposals/distiller-rebuild-team2-rebuild.md`` and memory
``project_distiller_rebuild.md``). The thesis being tested is that the
1194 ``pipeline.manifest_parse_failed`` events since 2026-04-15 come
from treating an LLM as a structured-output generator over subprocess
stdout — replacing that contract with a non-agentic LLM call plus
Pydantic validation plus a deterministic Python writer should eliminate
the failure class outright.

The models here are the contract the extractor.py validates against.
They intentionally stay locked to ``vault/schema.py`` so a future
schema change (new learn type, new status) forces a simultaneous
update here (enforced by ``scripts/smoke_contract_parity.py``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from alfred.vault.schema import LEARN_TYPES, STATUS_BY_TYPE

# ``Literal`` needs the types spelled out at class-definition time.
# Keep this in sync with ``vault/schema.py::LEARN_TYPES`` — the
# smoke_contract_parity script asserts equality at test time.
LearnTypeLiteral = Literal[
    "assumption", "decision", "constraint", "contradiction", "synthesis",
]

ConfidenceLiteral = Literal["low", "medium", "high"]


class LearningCandidate(BaseModel):
    """One learn record the extractor proposes to write.

    Fields mirror the learn-record frontmatter schema enough for the
    deterministic writer (``writer.py``) to assemble a file without
    the LLM ever composing frontmatter directly. The LLM's job is
    reduced to picking values for these fields — Python owns the
    serialization.

    Validation invariants:
      - ``type`` must be one of ``LEARN_TYPES``.
      - ``status`` must be valid for ``type`` per ``STATUS_BY_TYPE``.
      - ``title`` and ``claim`` have length floors to filter out
        garbage single-word outputs the LLM sometimes emits.
    """

    type: LearnTypeLiteral
    title: str = Field(min_length=5, max_length=150)
    confidence: ConfidenceLiteral
    status: str
    claim: str = Field(min_length=20)
    evidence_excerpt: str = ""
    source_links: list[str] = Field(default_factory=list)
    entity_links: list[str] = Field(default_factory=list)
    project: str | None = None

    @model_validator(mode="after")
    def _status_matches_type(self) -> "LearningCandidate":
        """Assert ``status`` is a legal value for this learn type.

        ``STATUS_BY_TYPE`` is the single source of truth. Keeping the
        check as a model_validator (not a field validator) means the
        LLM's natural output — emit type first, then status — still
        gets validated together rather than in separate passes.
        """
        allowed = STATUS_BY_TYPE.get(self.type, set())
        if self.status not in allowed:
            allowed_sorted = sorted(allowed) if allowed else []
            raise ValueError(
                f"status={self.status!r} is not valid for type={self.type!r}. "
                f"Allowed: {allowed_sorted}"
            )
        return self


class ExtractionResult(BaseModel):
    """Top-level extractor return shape.

    An empty ``learnings`` list is a valid success (the source didn't
    warrant any new learn records). The extractor returns this shape
    both on the happy path and on repair-retry failure; callers
    distinguish the two by log events, not by return-value inspection.
    """

    learnings: list[LearningCandidate] = Field(default_factory=list)


# Module is consumed by extractor.py, writer.py, and
# scripts/smoke_contract_parity.py. Deliberately no ``__all__`` —
# callers import by name and we want new fields to be naturally
# visible on introspection.
