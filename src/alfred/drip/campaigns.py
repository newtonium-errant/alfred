"""The two ratified campaigns (#44 D4: both run immediately, own caps each).

Registry is a plain dict. Two campaigns do not justify a plugin system —
speculative generality bought with real complexity; revisit at four.

## Why `gmail_backlog` MOVES files instead of structuring them

The curator daemon actively watches ``vault/inbox/`` and polls every 5s. The
07-30 recovery already re-queued the 907 as ``-recov`` files INTO that watched
directory, where they wait for the Aug-6 quota reset. **At reset the watcher
will chew the whole cohort at full speed** — so a budget that doesn't change
what the watcher SEES is decorative.

Hence the throttle is a MOVE: the work-list lives in a staging directory
outside the watched tree, and ``work()`` moves exactly one item in. That keeps
the runner's contract intact — it owns pacing, the curator still owns recovery
and structuring — and it makes the budget bind on the only thing that matters,
arrival rate into the watched dir.

**The consequence, and it drove a runner change:** the curator structures
asynchronously, so ``verify()`` immediately after the move is *guaranteed*
False. Treating that as failure would mark every dispatched item FAILED on the
run that dispatched it. So this campaign declares ``verify_is_async()`` and the
runner leaves a dispatched item ``in_flight`` for a later run's verify-first to
resolve — bounded by ``max_awaiting_runs`` so a dispatch that never lands still
surfaces as FAILED rather than sitting invisible.

The 907 guard stays FULL STRENGTH for sync campaigns (``link001_repair``):
work returned + no observable effect ⇒ FAILED, immediately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: The 07-30 rename-requeue suffix. Curator dedups by filename, so the live
#: cohort's names differ from the 2026-07-26 artifact list byte-for-byte. The
#: STAGING INVENTORY at run time is the authoritative work-list; the artifact
#: is provenance only.
RECOV_SUFFIX = "-recov"


@dataclass
class GmailBacklogCampaign:
    """Drip the recovered Gmail backlog into the watched inbox, one at a time.

    ``staging_dir`` holds the cohort OUTSIDE ``vault/inbox/``. The deploy-time
    move of the current inbox cohort into staging is an operator action; this
    campaign only accepts the path.
    """

    staging_dir: Path
    inbox_dir: Path
    vault_path: Path
    name: str = "gmail_backlog"

    def worklist(self) -> list[str]:
        """Filenames present in staging, sorted. Inventory at RUN TIME.

        Deliberately not read from the 0726 artifact: the live files carry the
        ``-recov`` suffix, so the artifact's names no longer match. Inventorying
        the directory also means the work-list shrinks as items are dispatched,
        which is what makes the campaign resumable without reconciliation.
        """
        if not self.staging_dir.is_dir():
            # ILB: an absent staging dir is a legitimate "nothing staged" state
            # (pre-deploy, or campaign complete), not a broken campaign.
            log.info(
                "drip.gmail.no_staging_dir", staging_dir=str(self.staging_dir),
            )
            return []
        return sorted(p.name for p in self.staging_dir.glob("*.md"))

    def work(self, item_id: str) -> None:
        """Move ONE item into the watched inbox. That is the whole operation.

        Not a copy: a copy would leave the item in staging and the next run
        would dispatch it again, duplicating the curator's work — the 907
        inverted. ``Path.rename`` is atomic within a filesystem, so the item is
        in exactly one place at every instant.
        """
        src = self.staging_dir / item_id
        dst = self.inbox_dir / item_id
        if not src.exists():
            raise FileNotFoundError(f"staged item vanished: {item_id}")
        if dst.exists():
            raise FileExistsError(
                f"{item_id} is already in the inbox — refusing to double-queue"
            )
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        src.rename(dst)

    def verify(self, item_id: str) -> bool:
        """Did the curator produce a vault record FOR THIS ITEM?

        ITEM-KEYED, per the contract: derived from this item's own filename, so
        an earlier item's output can never satisfy it. A verifier asking "did
        any note appear?" would mark untouched items done — the 907 wearing the
        verifier's uniform.

        NOT the negation of the selector. The selector asks "is this file still
        staged?"; answering *that* would call an item done the instant it left
        staging, which is the moment BEFORE any work happens. Learned in the
        field on 2026-08-04, when #40's neutralize pass used its selector's
        inverse and over-reached onto ~914 genuine records.
        """
        stem = self._record_stem(item_id)
        note_dir = self.vault_path / "note"
        if not note_dir.is_dir():
            return False
        needle = stem.lower()
        return any(
            needle in p.stem.lower() for p in note_dir.glob("*.md")
        )

    @staticmethod
    def _record_stem(item_id: str) -> str:
        """The identifying slug the curator's note carries from the inbox file.

        Strips the ``.md`` extension and the ``-recov`` requeue suffix — the
        suffix is a queueing artifact of the 07-30 recovery and never appears
        in the structured record's name.
        """
        stem = item_id[:-3] if item_id.endswith(".md") else item_id
        if stem.endswith(RECOV_SUFFIX):
            stem = stem[: -len(RECOV_SUFFIX)]
        return stem

    def spends_quota(self) -> bool:
        return True     # the curator calls `claude -p` per dispatched item

    def verify_is_async(self) -> bool:
        return True     # the curator polls; the effect appears seconds later


@dataclass
class Link001Campaign:
    """Repair dangling links: annotate `learn/` targets, remove the rest.

    **The branch is decided at list-BUILD and FROZEN** (D4a). Re-deriving it per
    run would let the same item annotate on Monday and delete on Tuesday,
    depending on whether a ``learn/`` record happened to exist in between — the
    campaign's behaviour must be a function of its input, not of when it ran.

    The two branches have opposite failure modes: a wrongly-annotated link is
    noise, a wrongly-removed one is DATA LOSS and is not recoverable from the
    vault. So the removal branch is the one that earns the frozen decision.

    Synchronous: the edit either landed in the file or it didn't, so the 907
    guard applies at full strength.
    """

    worklist_items: list[str]
    vault_path: Path
    name: str = "link001_repair"

    #: ``<record_rel_path>::<link_target>::<branch>`` — the frozen decision
    #: travels IN the item id, so state, logs and the verifier all read the
    #: same branch and none of them can re-derive a different one.
    ITEM_RE = re.compile(r"^(?P<path>.+?)::(?P<target>.+?)::(?P<branch>annotate|remove)$")

    BRANCH_ANNOTATE = "annotate"
    BRANCH_REMOVE = "remove"

    def worklist(self) -> list[str]:
        return list(self.worklist_items)

    @classmethod
    def parse_item(cls, item_id: str) -> tuple[str, str, str]:
        m = cls.ITEM_RE.match(item_id)
        if not m:
            raise ValueError(
                f"malformed link001 item (need path::target::branch): {item_id!r}"
            )
        return m.group("path"), m.group("target"), m.group("branch")

    @classmethod
    def build_item(cls, rel_path: str, target: str, *, is_learn_target: bool) -> str:
        """Freeze one item's branch AT LIST-BUILD. The only place the
        learn-vs-other decision is ever made."""
        branch = cls.BRANCH_ANNOTATE if is_learn_target else cls.BRANCH_REMOVE
        return f"{rel_path}::{target}::{branch}"

    def work(self, item_id: str) -> None:
        rel_path, target, branch = self.parse_item(item_id)
        from alfred.vault.paths import resolve_in_vault

        path = resolve_in_vault(
            self.vault_path, rel_path, writer="drip.link001_repair",
        )
        body = path.read_text(encoding="utf-8")
        link = f"[[{target}]]"
        if branch == self.BRANCH_ANNOTATE:
            # Provenance annotation; the link STAYS.
            if f"{link} " + _PROVENANCE_MARK in body:
                return                       # already annotated — idempotent
            body = body.replace(link, f"{link} {_PROVENANCE_MARK}")
        else:
            body = _remove_link(body, link)
        path.write_text(body, encoding="utf-8")

    def verify(self, item_id: str) -> bool:
        """Branch-DEPENDENT, because the two branches have different effects.

        A uniform "the link is gone" check would mark every annotation FAILED —
        the annotate branch deliberately keeps the link. This is why the branch
        travels in the item id rather than being looked up.
        """
        rel_path, target, branch = self.parse_item(item_id)
        from alfred.vault.paths import resolve_in_vault

        path = resolve_in_vault(
            self.vault_path, rel_path, writer="drip.link001_repair.verify",
        )
        if not path.exists():
            return False
        body = path.read_text(encoding="utf-8")
        link = f"[[{target}]]"
        if branch == self.BRANCH_ANNOTATE:
            return f"{link} {_PROVENANCE_MARK}" in body
        return link not in body

    def spends_quota(self) -> bool:
        return False    # pure vault edits, no LLM

    def verify_is_async(self) -> bool:
        return False    # the edit landed or it didn't


#: The D-ruling's provenance annotation for a surviving learn-record link.
_PROVENANCE_MARK = "<!-- link-provenance: retained (learn record) -->"

#: Horizontal whitespace only — never a newline. A link at end-of-line must not
#: let its trailing-whitespace match eat the line break and join two lines.
_INLINE_WS = r"[^\S\r\n]"


def _remove_link(body: str, link: str) -> str:
    """Delete ``link`` and heal the whitespace it sat in.

    A plain ``body.replace(link, "")`` leaves a double space behind every
    inline link (``See [[X]] here.`` → ``See  here.``). One record it is a
    typo; across the ~2,000 the campaign drains it becomes a second cleanup
    campaign, which is why this is worth fixing at the removal site rather
    than later.

    The rule: consume the link together with the horizontal whitespace on
    either side, then put back a SINGLE space only when the link had
    whitespace on BOTH sides (i.e. it sat between words). A link that was
    leading, trailing, or hugging punctuation leaves nothing behind, so
    ``See [[X]].`` becomes ``See.`` rather than ``See .``.
    """
    pattern = re.compile(f"({_INLINE_WS}*){re.escape(link)}({_INLINE_WS}*)")
    return pattern.sub(
        lambda m: " " if (m.group(1) and m.group(2)) else "", body,
    )


#: name → campaign factory. A dict, on purpose (see the module docstring).
CAMPAIGN_KINDS = {
    "gmail_backlog": GmailBacklogCampaign,
    "link001_repair": Link001Campaign,
}


__all__ = [
    "CAMPAIGN_KINDS",
    "RECOV_SUFFIX",
    "GmailBacklogCampaign",
    "Link001Campaign",
]
