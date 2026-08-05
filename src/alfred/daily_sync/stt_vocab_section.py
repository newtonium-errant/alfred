"""Daily Sync section — recurring speech corrections awaiting the operator's yes.

The SURFACE half of the learned-vocabulary loop (#54). The web chat route
captures every (transcript, sent) pair on a voice-seeded send; this section
reads that corpus, extracts the term-level corrections, and lists the ones the
operator has made often enough to be worth a decision.

## Why this reuses ``propose_vocab_additions`` rather than filtering here

The "don't re-ask a question he already answered" filter lives INSIDE
``propose_vocab_additions``, deliberately — that is the groundhog bug
``routine.match_calibration.filter_pending_for_review`` had to fix once, and
putting the filter in the callers is how it comes back. This section therefore
does no filtering of its own: it hands the pairs and the decided store to the
one function that knows the rules, and renders what comes out.

## Read-only, like ``routine_match_section`` Phase 1

Surfacing a proposal changes nothing. The vocabulary mutates only when the
operator runs ``alfred stt-vocab approve``/``reject``, which appends to the
decided store. That keeps the human step a human step: this card biases no
future transcription by existing.

## The full list is shown, not just the proposals

Every render ends with the vocabulary that is ACTUALLY biasing right now —
shipped terms plus learned ones — because the operator is being asked to GROW a
list, and a growth decision made without seeing the list is made blind. It also
makes the learned-term cap legible before he hits it rather than after.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type
from typing import Any

import structlog

from alfred.telegram.stt_vocab_learning import (
    MAX_LEARNED_TERMS,
    MIN_CORRECTION_COUNT,
    effective_vocab_terms,
    iter_correction_pairs,
    load_decisions,
    propose_vocab_additions,
)

from . import assembler
from .config import DailySyncConfig

log = structlog.get_logger(__name__)

# Priority slot — immediately after the routine-match calibration section (27),
# grouping this with the other review/calibration surfaces.
_PRIORITY = 28

# How many distinct mis-hearings to name in one line. The evidence is there to
# make the term recognisable ("heard as 'frontrun'"), not to enumerate every
# variant; a long tail on one line costs more legibility than it buys.
MAX_HEARD_SHOWN = 3


@dataclass
class SttVocabItem:
    """One proposed vocabulary term, ready for the operator's yes/no.

    Mirrors ``routine_match_section.RoutineMatchItem``: the underlying
    ``VocabProposal`` stays a pure extraction record, and this display item
    carries the ``item_number`` (GLOBAL across Daily Sync sections, assigned
    from the assembler's ``start_index``).

    Unlike the routine-match item, the routing key is the TERM, not the number —
    the CLI approves by term, so a stale number can never approve the wrong
    thing.
    """

    item_number: int
    term: str
    heard: list[str]
    count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_number": self.item_number,
            "term": self.term,
            "heard": list(self.heard),
            "count": self.count,
        }


# Module-level batch holder, mirroring the sibling sections, so the assembler's
# ``item_count_after`` hook can number the NEXT section's items continuously
# after these.
_LAST_BATCH_HOLDER: dict[str, list[SttVocabItem]] = {"items": []}


def consume_last_batch() -> list[SttVocabItem]:
    """Return and clear the most recently-surfaced batch."""
    items = _LAST_BATCH_HOLDER.get("items", [])
    _LAST_BATCH_HOLDER["items"] = []
    return items


def peek_last_batch_count() -> int:
    """Non-destructive count for the assembler's ``item_count_after`` hook."""
    return len(_LAST_BATCH_HOLDER.get("items", []))


def _format_item(item: SttVocabItem) -> str:
    """Render one proposal as a numbered line.

    ``N. "term" — corrected 3x (heard "a", "b")``

    The count leads because it is the whole argument for the term: the operator
    is being told how often he has already fixed this by hand.
    """
    line = f"{item.item_number}. “{item.term}” — corrected {item.count}×"
    shown = item.heard[:MAX_HEARD_SHOWN]
    if shown:
        heard = ", ".join(f"“{h}”" for h in shown)
        more = len(item.heard) - len(shown)
        line += f" (heard {heard}{f', +{more} more' if more > 0 else ''})"
    return line


def _current_vocabulary(cfg) -> tuple[list[str], int, int]:
    """The vocabulary biasing transcription right now: (terms, static, learned).

    Goes through ``effective_vocab_terms`` — the ONE seam — rather than merging
    the static list with the decided store here. ``SttVocabConfig`` mirrors the
    talker's field names precisely so it can be passed straight in; a second
    implementation of the union is exactly what that seam exists to prevent.
    """
    static = [str(t) for t in (cfg.vocab_terms or [])]
    effective = effective_vocab_terms(cfg)
    return effective, len(static), max(0, len(effective) - len(static))


def stt_vocab_section(
    config: DailySyncConfig,
    today: date_type,
    start_index: int = 1,
) -> str | None:
    """Section provider — recurring speech corrections worth a decision.

    Returns ``None`` (section omitted) when disabled, so an instance that does
    not run the learning loop is unaffected. When ENABLED it always renders:
    the proposals, or the intentionally-left-blank sentinel — idle has to be
    distinguishable from broken, and a loop whose whole job is to notice
    patterns is a loop whose silence is genuinely ambiguous.
    """
    sv = config.stt_vocab
    if not sv.enabled:
        _LAST_BATCH_HOLDER["items"] = []
        return None

    # ``today`` is unused: unlike routine_match, a correction does not go stale.
    # A term the operator fixed a month ago and has not fixed since is still a
    # term he wanted spelled his way, and the decided store — not a clock — is
    # what stops it being re-asked.
    del today

    current, n_static, n_learned = _current_vocabulary(sv)
    decided = load_decisions(sv.vocab_decided_path)
    pairs = list(iter_correction_pairs(sv.vocab_corpus_path))

    proposals = propose_vocab_additions(
        pairs,
        current_vocab=current,
        decided=decided,
        min_count=MIN_CORRECTION_COUNT,
        max_learned=MAX_LEARNED_TERMS,
    )

    items = [
        SttVocabItem(
            item_number=start_index + i,
            term=p.term,
            heard=list(p.heard),
            count=p.count,
        )
        for i, p in enumerate(proposals)
    ]
    _LAST_BATCH_HOLDER["items"] = items

    biasing = (
        f"Currently biasing {len(current)} term{'' if len(current) == 1 else 's'} "
        f"({n_static} shipped + {n_learned} learned, cap {MAX_LEARNED_TERMS})."
    )

    if not items:
        # ILB: enabled but nothing to propose. Say WHICH of the two quiet cases
        # it is — no corrections recorded at all reads very differently from
        # corrections recorded but none repeated yet, and the operator can only
        # act on one of them (turn capture on).
        log.info(
            "stt_vocab.no_proposals",
            pairs=len(pairs),
            corpus_path=sv.vocab_corpus_path,
            detail="ran, nothing to propose",
        )
        if not pairs:
            why = (
                "No speech corrections recorded yet — nothing to propose. "
                "(If this stays empty, check `talker.stt.vocab_learning_enabled`.)"
            )
        else:
            # Deliberately does NOT name one reason. Three different things
            # produce an empty list here — not yet repeated, already approved,
            # already rejected — and `propose_vocab_additions` only LOGS which
            # (``stt.vocab_proposals.already_ruled``). Asserting the wrong one
            # in the operator's face would be worse than naming all three: a
            # card that says "none repeated twice yet" about a term he approved
            # this morning reads as the loop being broken.
            plural = "" if len(pairs) == 1 else "s"
            why = (
                f"{len(pairs)} correction pair{plural} recorded, nothing new to "
                f"propose — a term already approved, already rejected, or not "
                f"yet corrected {MIN_CORRECTION_COUNT}× does not surface."
            )
        return f"## Speech vocabulary review\n\n{why}\n\n{biasing}"

    log.info(
        "stt_vocab.surfaced",
        count=len(items),
        pairs=len(pairs),
        corpus_path=sv.vocab_corpus_path,
    )
    plural = "s" if len(items) != 1 else ""
    lines = [f"## Speech vocabulary review ({len(items)} proposal{plural})", ""]
    for item in items:
        lines.append(_format_item(item))
    lines.append("")
    lines.append("Approve: `alfred stt-vocab approve \"<term>\" --operator <name>`")
    lines.append("Reject:  `alfred stt-vocab reject \"<term>\" --operator <name>`")
    lines.append("")
    lines.append(biasing)
    return "\n".join(lines).rstrip()


def register() -> None:
    """Register the section provider (idempotent — re-fire safe)."""
    if "stt_vocab" in assembler.registered_providers():
        return
    assembler.register_provider(
        "stt_vocab",
        priority=_PRIORITY,
        provider=stt_vocab_section,
        item_count_after=peek_last_batch_count,
    )


__all__ = [
    "MAX_HEARD_SHOWN",
    "SttVocabItem",
    "consume_last_batch",
    "peek_last_batch_count",
    "register",
    "stt_vocab_section",
]
