"""Capture-close section provider — the card that asks (#64).

Renders each pending propose-close as a numbered Daily Sync item the operator
answers with ``N confirm`` / ``N reject``. The scan lives in
:mod:`.capture_close_scan`, the queue and the suppressions in
:mod:`.capture_close_proposals`, the scoring in :mod:`.capture_close_match`;
this file is the surface, and it RUNS the trigger on every fire before listing.

Running the trigger here rather than in the daemon is the shape
``demotion_section`` established, for the same two reasons: the evaluation has
to happen somewhere that fires daily whether or not there is anything to say,
and putting it here means the section cannot render a proposal the trigger has
not just re-validated.

## PRIORITY 26

Between attribution (25) and routine_match (27). Both neighbours are load-
bearing rather than incidental: 27 is the other learned-matcher card — the one
this feature's whole shape is mirrored from — so the two fuzzy-match questions
sit together and the operator answers them in one frame of mind. And the band
below attribution is where the propose-then-approve items live (demotion at 24),
which is the class this belongs to. It is a question about HIS work, not about
the machine's, so it sits below the machine's own audit rather than above it.

## Why the empty section renders nothing

Same reasoning as the demotion section, and the intentionally-left-blank
obligation is met by the stronger of the two signals: :data:`SCAN_EVENT` fires
on every pass with the counts, and
:data:`~.capture_close_proposals.TRIGGER_EVENT` fires with the REASON nothing
was proposed. A rendered "no fulfilled tasks today" would say less than either,
every morning, in the operator's message.

## The already-resolved suppression lives HERE

A pending row whose task is no longer open must not render. The task can leave
the open set in three ways: he closed it by hand, a confirm's vault write landed
but its queue bookkeeping did not, or another instance touched it. In all three
the question is stale, and asking him to close a task that is already done is
the "I confirmed and it came back" complaint the propose-then-approve channel
cannot afford. Checked against the LIVE record at render time, because that is
the only source of truth about whether the task is still open.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import frontmatter
import structlog

from .capture_close_match import append_pending, load_glossary
from .capture_close_proposals import (
    CaptureCloseProposal,
    list_pending,
    maybe_propose_closes,
)
from .capture_close_scan import OPEN_TASK_STATUSES, scan_for_closes
from .config import DailySyncConfig

log = structlog.get_logger(__name__)

_PRIORITY = 26

#: Provider name — the registry key, the state-payload key stem, and the
#: reply-dispatch item family all derive from this one spelling.
SECTION_NAME = "capture_close"

_VAULT_PATH_HOLDER: dict[str, Path | None] = {"path": None}


def set_vault_path(vault_path: Path) -> None:
    """Inject the vault root. Called once by the daemon at startup."""
    _VAULT_PATH_HOLDER["path"] = Path(vault_path)


@dataclass
class CaptureCloseItem:
    """One propose-close as a numbered Daily Sync item.

    Persisted into the state file's ``capture_close_items`` so the reply
    dispatcher can resolve "item 7" → proposal_id + task_path without
    re-reading the queue. The evidence fields are carried rather than
    re-derived: the operator must be answering the question he was asked.
    """

    item_number: int  # 1-indexed, GLOBAL across Daily Sync sections
    proposal_id: str
    task_path: str
    task_text: str
    evidence_path: str
    evidence_name: str
    score: float
    match_source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_number": self.item_number,
            "proposal_id": self.proposal_id,
            "task_path": self.task_path,
            "task_text": self.task_text,
            "evidence_path": self.evidence_path,
            "evidence_name": self.evidence_name,
            "score": self.score,
            "match_source": self.match_source,
        }

    @classmethod
    def from_proposal(
        cls, proposal: CaptureCloseProposal, item_number: int,
    ) -> "CaptureCloseItem":
        return cls(
            item_number=item_number,
            proposal_id=proposal.proposal_id,
            task_path=proposal.task_path,
            task_text=proposal.task_text,
            evidence_path=proposal.evidence_path,
            evidence_name=proposal.evidence_name,
            score=proposal.score,
            match_source=proposal.match_source,
        )


def _configured(config: DailySyncConfig) -> Any | None:
    """The capture_close config block when it is usable, else ``None``.

    "Usable" means enabled AND all three store paths resolved. An empty path is
    NOT "use the default" — see ``CaptureCloseConfig``: there is no default,
    because a cwd-relative one would be shared across every instance on the box.
    A misconfigured instance is told so rather than quietly writing somewhere
    another instance reads.
    """
    cc = getattr(config, "capture_close", None)
    if cc is None or not getattr(cc, "enabled", False):
        return None
    missing = [
        name for name in ("queue_path", "corpus_path", "pending_path")
        if not (getattr(cc, name, "") or "").strip()
    ]
    if missing:
        log.warning(
            "daily_sync.capture_close.not_configured",
            missing=missing,
            detail="capture_close is enabled but has unresolved store paths — "
                   "no propose-close pass ran. These are normally derived from "
                   "logging.dir at config load.",
        )
        return None
    return cc


def _task_is_open(vault_path: Path, task_path: str) -> bool:
    """Is the task still outstanding? A record we cannot read counts as NOT
    open — a proposal about a task that has vanished is not a question worth
    asking, and the safe direction for a card is to withhold it."""
    target = vault_path / task_path
    try:
        post = frontmatter.load(str(target))
    except Exception:  # noqa: BLE001 — missing or malformed: withhold the card
        return False
    return str(post.metadata.get("status") or "todo") in OPEN_TASK_STATUSES


def run_trigger(
    config: DailySyncConfig, *, now: datetime | None = None,
) -> list[CaptureCloseProposal]:
    """Scan, record the near misses, and raise up to the card budget.

    Returns the proposals RAISED this pass (usually none). Reads the glossary
    here and hands plain values down, so the scoring and the policy each stay
    testable without the other.
    """
    cc = _configured(config)
    if cc is None:
        return []
    vault_path = _VAULT_PATH_HOLDER.get("path")
    if vault_path is None:
        # The daemon wires this at startup; a section that silently no-ops
        # because an injection was missed is the accepted-then-ignored shape.
        log.warning(
            "daily_sync.capture_close.no_vault_path",
            detail="set_vault_path was never called — the propose-close pass "
                   "cannot read the vault and did not run",
        )
        return []

    result = scan_for_closes(
        vault_path,
        threshold=cc.threshold,
        floor=cc.floor,
        window_days=cc.window_days,
        max_tasks=cc.max_tasks,
        glossary=load_glossary(cc.corpus_path),
        now=now,
    )
    for near in result.near_misses:
        try:
            append_pending(cc.pending_path, near)
        except OSError as exc:
            # A lost near miss costs one row of threshold evidence, never a
            # card and never a vault change — logged, never raised.
            log.warning(
                "daily_sync.capture_close.pending_write_failed",
                path=cc.pending_path, error=str(exc),
            )
            break
    return maybe_propose_closes(
        cc.queue_path,
        result.candidates,
        threshold=cc.threshold,
        window_days=cc.window_days,
        max_proposals=cc.max_proposals,
        now=now,
    )


def build_batch(
    config: DailySyncConfig, *, start_index: int = 1,
) -> list[CaptureCloseItem]:
    """Run the trigger, then number whatever is pending AND still open.

    ``[]`` is the norm and is not an error.
    """
    cc = _configured(config)
    if cc is None:
        return []
    run_trigger(config)
    vault_path = _VAULT_PATH_HOLDER.get("path")
    if vault_path is None:
        return []
    pending = list_pending(cc.queue_path)
    live = [p for p in pending if _task_is_open(vault_path, p.task_path)]
    stale = len(pending) - len(live)
    if stale:
        log.info(
            "daily_sync.capture_close.stale_suppressed",
            count=stale,
            detail="pending proposals whose task is no longer open were not "
                   "rendered — already closed by hand, or a confirm whose "
                   "queue bookkeeping did not land",
        )
    return [
        CaptureCloseItem.from_proposal(p, start_index + i)
        for i, p in enumerate(live)
    ]


def render_batch(items: list[CaptureCloseItem]) -> str | None:
    """Render the section, or ``None`` when nothing is pending.

    The text NAMES THE EVIDENCE and QUOTES THE PROMISE. The operator is being
    asked whether a thing he said he would do is done; he cannot answer that
    from a task filename and a number. A card that said only "close this task?"
    would make confirming an act of faith — and the answer is the correction
    signal the matcher learns from, so a guessed answer poisons the glossary as
    surely as a wrong one.
    """
    if not items:
        return None
    plural = "s" if len(items) != 1 else ""
    lines = [f"## Captured tasks ({len(items)} to close{plural})", ""]
    for item in items:
        lines.append(f'{item.item_number}. Done with "{item.task_text}"?')
        learned = " (you confirmed this pairing before)" \
            if item.match_source == "glossary" else ""
        lines.append(
            f"   Evidence: [[{item.evidence_path}]] — {item.evidence_name}"
            f"{learned}"
        )
        lines.append(
            f"   Confirming marks {item.task_path} done. Rejecting leaves it "
            f"open and teaches me this wasn't the evidence."
        )
        lines.append("")
    lines.append(
        "Reply with `N confirm` to close it, `N reject` to leave it open."
    )
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Section provider entry point + registration
# ---------------------------------------------------------------------------

# Module-level holder so the daemon can read the batch back after assembly.
# Mirrors email / attribution / demotion — the assembler signature returns only
# a string, so per-section metadata flows through this side channel.
_LAST_BATCH_HOLDER: dict[str, list[CaptureCloseItem]] = {"items": []}


def consume_last_batch() -> list[CaptureCloseItem]:
    """Return and clear the most recently-built batch."""
    items = _LAST_BATCH_HOLDER.get("items", [])
    _LAST_BATCH_HOLDER["items"] = []
    return items


def peek_last_batch_count() -> int:
    """Non-destructive count for the assembler's ``item_count_after`` hook."""
    return len(_LAST_BATCH_HOLDER.get("items", []))


def capture_close_section(
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
    """Idempotent provider registration at priority 26."""
    from . import assembler
    if SECTION_NAME in assembler.registered_providers():
        return
    assembler.register_provider(
        SECTION_NAME,
        priority=_PRIORITY,
        provider=capture_close_section,
        item_count_after=peek_last_batch_count,
    )


__all__ = [
    "SECTION_NAME",
    "CaptureCloseItem",
    "build_batch",
    "capture_close_section",
    "consume_last_batch",
    "peek_last_batch_count",
    "register",
    "render_batch",
    "run_trigger",
    "set_vault_path",
]
