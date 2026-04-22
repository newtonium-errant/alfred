"""Email classifier — per-instance priority + action_hint tagging.

Filed under ``email-surfacing c1`` of the multi-chunk arc in
``project_email_surfacing.md``. The classifier reads email-derived note
records the curator has just produced, asks an LLM for a tier
(``high``/``medium``/``low``/``spam``) and an optional ``action_hint``
free-text recommendation, and writes both back into the note's
frontmatter.

Per-instance design (Stage 3.5): Salem configures the classifier via the
``email_classifier:`` block in ``config.yaml``. STAY-C will plug in later
through ``config.stayc.yaml`` with its own clinical cold prompt — no code
change required, the architecture is per-instance from day one.
"""

from .classifier import (
    ClassificationResult,
    classify_record,
    classify_records_for_inbox,
    is_email_inbox,
)
from .config import EmailClassifierConfig, load_from_unified

__all__ = [
    "ClassificationResult",
    "EmailClassifierConfig",
    "classify_record",
    "classify_records_for_inbox",
    "is_email_inbox",
    "load_from_unified",
]
