"""Email topical-filing axis (#7 7c-i) — the deterministic n8n rule port + LLM fallback + email_category.

Orthogonal sibling to ``email_classifier`` (the priority axis). Curator entry point:
:func:`classify_filing_for_inbox`. INERT re: Gmail — writes vault frontmatter only; the Gmail-side label
re-application is 7c-ii.
"""

from __future__ import annotations

from .classifier import FilingResult, classify_filing_for_inbox
from .config import EmailFilingConfig, load_from_unified

__all__ = [
    "FilingResult",
    "classify_filing_for_inbox",
    "EmailFilingConfig",
    "load_from_unified",
]
