"""Demotion-proposal section provider — the card that asks (#72 item c).

Renders the ONE pending demotion proposal as a numbered Daily Sync item the
operator answers with ``N confirm`` / ``N reject``, exactly like a canonical
proposal. The queue, the trigger and the cooldown live in
:mod:`.demotion_proposals`; this file is the surface.

It also RUNS the trigger, on every fire, before listing. Two reasons rather than
convenience: the evaluation has to happen somewhere that fires daily whether or
not there is anything to say, and putting it here means the section cannot
render a proposal the trigger has not just re-validated against the override
state.

## Why this section suppresses on empty, when attribution's does not

Attribution's section renders an empty-state header every day because the
quality line rides on it — the operator needs to see the metric on the quiet
days especially. This section has no such passenger: on a healthy instance it
would print "no pending proposals" every morning forever, which is noise, and
the canonical-proposals section already established that convention for a
propose-then-approve queue.

Intentionally-left-blank is still satisfied, and by the stronger signal of the
two: :data:`~.demotion_proposals.TRIGGER_EVENT` fires on EVERY evaluation with
the reason it did not propose (below threshold / already overridden / pending /
cooldown). Silence in the Daily Sync is backed by a line in the log that says
which of those four silences it is. A rendered "nothing pending" would say less
than that, every day, in the operator's morning message.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import structlog

from .attribution_quality import DEFAULT_WINDOW_DAYS, attribution_quality_stats
from .config import DailySyncConfig
from .demotion_proposals import (
    ATTRIBUTION_KIND,
    DemotionProposal,
    list_pending,
    maybe_propose_demotion,
)
from .tier_override import load_overrides

log = structlog.get_logger(__name__)


@dataclass
class DemotionProposalItem:
    """One demotion proposal as a numbered Daily Sync item.

    Persisted into the state file's ``last_batch.demotion_items`` so the reply
    dispatcher can resolve "item 7" → proposal_id without re-reading the queue.
    """

    item_number: int  # 1-indexed, GLOBAL across Daily Sync sections
    proposal_id: str
    kind: str
    demotion_contests: int
    window_days: int
    threshold: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_number": self.item_number,
            "proposal_id": self.proposal_id,
            "kind": self.kind,
            "demotion_contests": self.demotion_contests,
            "window_days": self.window_days,
            "threshold": self.threshold,
        }

    @classmethod
    def from_proposal(
        cls, proposal: DemotionProposal, item_number: int,
    ) -> "DemotionProposalItem":
        return cls(
            item_number=item_number,
            proposal_id=proposal.proposal_id,
            kind=proposal.kind,
            demotion_contests=proposal.demotion_contests,
            window_days=proposal.window_days,
            threshold=proposal.threshold,
        )


def run_trigger(
    config: DailySyncConfig, *, now: datetime | None = None,
) -> DemotionProposal | None:
    """Evaluate the demotion case once. Returns a new proposal, or ``None``.

    Reads the corpus and the override file HERE and hands both to
    :func:`~.demotion_proposals.maybe_propose_demotion` as plain values, so the
    policy stays testable without a filesystem and cannot accidentally consult a
    different corpus than the quality line does.

    The corpus is read a second time this fire (the attribution section at
    priority 25 reads it for the quality line). That is deliberate: both go
    through ``iter_attribution_rows``, the one reader, so they cannot disagree,
    and one extra read of a small append-only file once a day is a fair price
    for two sections that do not have to know about each other.
    """
    attribution = getattr(config, "attribution", None)
    corpus_path = getattr(attribution, "corpus_path", "") or "" if attribution else ""
    if not attribution or not corpus_path:
        # No corpus configured → no evidence can exist, so there is nothing to
        # evaluate. Said out loud rather than returned silently: an instance
        # that quietly never proposes looks identical to one with a clean record.
        log.info(
            "daily_sync.attribution.demotion_no_corpus",
            detail="no attribution corpus configured — the demotion trigger "
                   "has nothing to read",
        )
        return None

    window = int(getattr(attribution, "quality_window_days", 0) or DEFAULT_WINDOW_DAYS)
    threshold = int(getattr(attribution, "demotion_threshold", 0) or 0)
    stats = attribution_quality_stats(corpus_path, window_days=window, now=now)
    overrides = load_overrides(attribution.resolved_tier_override_path())

    return maybe_propose_demotion(
        attribution.resolved_demotion_queue_path(),
        kind=ATTRIBUTION_KIND,
        demotion_contests=stats.demotion_contests,
        window_days=window,
        threshold=threshold,
        override_in_force=overrides.tier_for(ATTRIBUTION_KIND) is not None,
        now=now,
    )


def build_batch(
    config: DailySyncConfig, *, start_index: int = 1,
) -> list[DemotionProposalItem]:
    """Run the trigger, then number whatever is pending. ``[]`` is the norm."""
    attribution = getattr(config, "attribution", None)
    if attribution is None:
        return []
    run_trigger(config)
    pending = list_pending(attribution.resolved_demotion_queue_path())
    return [
        DemotionProposalItem.from_proposal(p, start_index + i)
        for i, p in enumerate(pending)
    ]


def render_batch(items: list[DemotionProposalItem]) -> str | None:
    """Render the section, or ``None`` when nothing is pending.

    The text NAMES THE EVIDENCE — "3 auto-confirmed inferences you contested in
    the last 14 days" — rather than saying a threshold was crossed. The operator
    is being asked to change a tier he ruled on; he needs the count and the span
    to answer, and a card that says only "quality is low" makes approving it an
    act of faith.
    """
    if not items:
        return None
    plural = "s" if len(items) != 1 else ""
    lines = [f"## Attribution tier ({len(items)} proposal{plural})", ""]
    for item in items:
        contest_word = "time" if item.demotion_contests == 1 else "times"
        lines.append(
            f"{item.item_number}. Put {item.kind} cards back under review?"
        )
        lines.append(
            f"   Evidence: {item.demotion_contests} {contest_word} in the last "
            f"{item.window_days} days, an entry auto-confirmed itself after 24h "
            f"and you then said it was wrong."
        )
        lines.append(
            "   Confirming stops the 24h auto-confirm for this card type — "
            "they come back as decisions until you clear it."
        )
        lines.append("")
    lines.append(
        "Reply with `N confirm` to put them back under review, "
        "`N reject` to leave them as they are."
    )
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Section provider entry point + registration
# ---------------------------------------------------------------------------

# Module-level holder so the daemon can read the batch back after assembly.
# Mirrors email / attribution / canonical-proposals — the assembler signature
# returns only a string, so per-section metadata flows through this side channel.
_LAST_BATCH_HOLDER: dict[str, list[DemotionProposalItem]] = {"items": []}


def consume_last_batch() -> list[DemotionProposalItem]:
    """Return and clear the most recently-built batch."""
    items = _LAST_BATCH_HOLDER.get("items", [])
    _LAST_BATCH_HOLDER["items"] = []
    return items


def peek_last_batch_count() -> int:
    """Non-destructive count for the assembler's ``item_count_after`` hook."""
    return len(_LAST_BATCH_HOLDER.get("items", []))


def demotion_proposals_section(
    config: DailySyncConfig,
    today: date,
    *,
    start_index: int = 1,
) -> str | None:
    """Section provider — runs the trigger, renders any pending proposal."""
    items = build_batch(config, start_index=start_index)
    _LAST_BATCH_HOLDER["items"] = items
    return render_batch(items)


def register() -> None:
    """Idempotent provider registration at priority 24.

    Immediately ABOVE the attribution-audit section (25), which is the decision
    recorded in that section's own ``register`` docstring: a card asking
    "should attribution cards go back under review?" is unreadable without the
    attribution batch beneath it, because that batch is the evidence for the
    answer. Filing it at 15 with the other propose-then-approve items would
    separate the question from its evidence by ten priority slots and a screen
    of email calibration.
    """
    from . import assembler
    if "demotion_proposals" in assembler.registered_providers():
        return
    assembler.register_provider(
        "demotion_proposals",
        priority=24,
        provider=demotion_proposals_section,
        item_count_after=peek_last_batch_count,
    )


__all__ = [
    "DemotionProposalItem",
    "build_batch",
    "consume_last_batch",
    "demotion_proposals_section",
    "peek_last_batch_count",
    "register",
    "render_batch",
    "run_trigger",
]
