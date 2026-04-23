"""Daily Sync — 09:00 OODA conversation channel.

Filed under email-surfacing c2 of the multi-chunk arc. Salem fires a
daily Telegram message at 09:00 ADT containing one or more "sections"
that ask Andrew to make small calibration / friction / open-question
decisions. The pattern is per-instance: every instance runs its own
Daily Sync against its own principal (Salem ↔ Andrew today, STAY-C ↔
Jamie later).

Section providers are callables ``(config, today) → str | None``. The
assembler runs them in priority order and joins their non-``None``
outputs into one multi-section message. The first concrete provider is
:func:`daily_sync.email_section.email_calibration_section` — c2 ships
that one and leaves hooks for friction-queue + open-question providers.

The reply parser consumes Telegram's ``reply_to_message`` to match
Andrew's terse replies back to the items in the most recent Daily Sync
batch. Calibration corrections land in
``data/email_calibration.{instance}.jsonl`` (per-instance JSONL, append
only). The classifier in :mod:`alfred.email_classifier` rotates the tail
of that corpus into its few-shot example slots — Phase 1 of the
corpus → classifier feedback loop in ``project_email_surfacing.md``.
"""

from .assembler import (
    SectionProvider,
    assemble_message,
    register_provider,
    registered_providers,
    parse_reply,
    ReplyParseResult,
    ReplyCorrection,
    EMPTY_SYNC_BODY,
)
from .attribution_corpus import AttributionCorpusEntry, append_entry as append_attribution_entry
from .config import AttributionConfig, DailySyncConfig, load_from_unified
from .confidence import (
    list_confidence,
    set_confidence,
    load_state,
    save_state,
)
from .corpus import (
    CorpusEntry,
    append_correction,
    iter_corrections,
    recent_corrections,
)

__all__ = [
    "AttributionConfig",
    "AttributionCorpusEntry",
    "DailySyncConfig",
    "EMPTY_SYNC_BODY",
    "SectionProvider",
    "append_attribution_entry",
    "assemble_message",
    "load_from_unified",
    "register_provider",
    "registered_providers",
    "parse_reply",
    "ReplyParseResult",
    "ReplyCorrection",
    "list_confidence",
    "set_confidence",
    "load_state",
    "save_state",
    "CorpusEntry",
    "append_correction",
    "iter_corrections",
    "recent_corrections",
]
