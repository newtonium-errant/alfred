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

## Why link001 matches links WHITESPACE-TOLERANTLY (#60)

Found live on 2026-08-06 by the watched-first-increment discipline: 4 of the
first 12 ``done`` items were false-dones. The work-list's targets come from the
janitor scanner, which PARSES YAML frontmatter and — by explicit design, see
``janitor/parser.extract_wikilinks`` — collapses internal whitespace runs so a
line-wrapped link resolves like a single-line one. ``work()`` and ``verify()``
matched the EXACT string ``[[<target>]]`` against RAW file text. So for a title
long enough that YAML folds it::

    related:
    - '[[constraint/Multi-Instance Alfred Has Information That Cannot Be Shared Across
      Instances]]'

the scanner reports the link, the raw needle never appears, ``work()`` edits
nothing — and ``verify()``'s ``link not in body`` PASSES, because the *wrapped*
form does not contain the *unwrapped* needle. Detection at the parser level,
repair at the text level: an impedance mismatch, and the failure direction was
the silent one. Vault titles are long; wrapping is the common case, not an edge.

Two structural rules came out of it, and both are load-bearing:

1. **One seam.** :func:`_flexible_ws_re` is the only place a target becomes a
   matcher, and both branches of both methods go through it. Four hand-rolled
   copies is how ``verify()`` drifts away from what ``work()`` edits again.
2. **A verifier must never be satisfiable by the defect it exists to catch.**
   This bug IS that sentence: the verifier's needle and the defect were the same
   missing string, so the one check designed to notice "the edit did not land"
   was disabled by exactly the condition that stopped it landing. ``verify()``
   judges on the same tolerance ``work()`` edits with, or it is decoration.

Matching tolerance alone is NOT the safety property, though, and it is worth
being precise about why: before #60 ``work()`` and ``verify()`` already agreed
perfectly — they shared one exact-string needle — and the campaign still wrote
false-dones. Equality between the two is an anti-DRIFT property. The property
that actually makes a ``done`` row true is that **verify() must be at least as
tolerant as the SCANNER**, because the scanner is what decides whether the link
is still broken. ``_flexible_ws_re``'s ``\\s+`` is exactly the inverse of
``extract_wikilinks``'s ``re.sub(r"\\s+", " ", target)``, which is what closes
the hole; ``tests/drip/test_wrapped_links.py`` pins the agreement against the
scanner directly rather than against ``work()``.

**Known residuals, deliberately not closed.** Both are the same shape as the
bug above — scanner-visible but needle-invisible — and both are documented
rather than fixed because the fix costs over-match surface on 758 IRREVERSIBLE
removals and the measured exposure is nil. Figures are from the vault snapshot,
counted with the scanner's own ``extract_wikilinks``/``WIKILINK_RE`` over 8,614
files: 61,508 wikilink occurrences, 52,813 unique (file, target) pairs.

1. **Aliased links.** ``[[target|display]]`` is normalized by the scanner to
   ``target`` but is not matched by ``[[target]]``. Measured: **8 occurrences,
   0.013%** — and an item only matters here if it is ALSO broken and ALSO on the
   frozen work-list, so the expected count across the 1,283 is approximately
   zero. Closing it means widening the matcher to swallow an optional ``|…``
   tail.
2. **Inner-padded links.** ``extract_wikilinks`` ends in ``.strip()``
   (``janitor/parser.py``), so ``[[ target ]]`` normalizes to ``target`` and is
   scanner-visible, while the needle ``[[target]]`` misses the padding.
   Measured: **0 occurrences vault-wide.** Closing it means allowing ``\\s*``
   immediately inside the brackets.

Revisit either if a future work-list is built over a vault where the shape is
common — the measurement above is the thing to re-run, not the conclusion.

For scale on why the whitespace tolerance itself was worth the change:
**28,930 of the 61,508 occurrences (47%) are wrapped** across physical lines.
Wrapping is not an edge case in this vault, it is the plurality.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml

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
    def build_item(cls, rel_path: str, target: str, *, citer_is_learn: bool) -> str:
        """Freeze one item's branch AT LIST-BUILD. The only place the
        learn-vs-other decision is ever made.

        ``citer_is_learn`` is whether the record HOLDING the link is a learn
        record — not whether the broken target looks like one. D2 rules on the
        citer: a learning that cited a since-deleted source keeps its
        provenance, so the link is annotated rather than removed. The target is
        the deleted source and cannot be a learn record; a broken target does
        not exist at all, so its type could only be guessed from its path.

        Renamed from ``is_learn_target``, which said the opposite of what the
        value means. The behaviour never matched the old name — only the name
        was wrong — but a later "fix" aligning the call site to it would have
        silently inverted every decision in a frozen work-list that drives
        unrecoverable deletions.
        """
        branch = cls.BRANCH_ANNOTATE if citer_is_learn else cls.BRANCH_REMOVE
        return f"{rel_path}::{target}::{branch}"

    def work(self, item_id: str) -> None:
        """Edit the record. WRITES ONLY WHEN THE TEXT ACTUALLY CHANGED.

        The unconditional ``write_text`` this replaced moved the file's mtime on
        a no-op edit, which is the worst possible lie for this campaign: mtime is
        the cheapest evidence an operator has that a repair touched a record, and
        a bumped mtime over unchanged bytes made 4 false-dones look worked. A
        no-op is now a logged event and an untouched file.
        """
        rel_path, target, branch = self.parse_item(item_id)
        from alfred.vault.paths import resolve_in_vault

        path = resolve_in_vault(
            self.vault_path, rel_path, writer="drip.link001_repair",
        )
        original = path.read_text(encoding="utf-8")
        link = f"[[{target}]]"
        link_re = _flexible_ws_re(link)

        if branch == self.BRANCH_ANNOTATE:
            # Provenance annotation; the link STAYS.
            if _flexible_ws_re(f"{link} {_PROVENANCE_MARK}").search(original):
                return                       # already annotated — idempotent
            # The mark goes immediately after the matched ``]]``, so for a link
            # inside a quoted YAML scalar it lands INSIDE the quotes. Measured,
            # not assumed (#60): inside is valid YAML and the scanner still sees
            # the link; placing it after the closing quote raises
            # ParserError and makes the whole record unparseable. See
            # ``_PROVENANCE_MARK`` for the constraint that keeps this true.
            body = link_re.sub(
                lambda m: f"{m.group(0)} {_PROVENANCE_MARK}", original,
            )
        else:
            body = _remove_link(original, link)

        _guard_frontmatter(
            original, body, rel_path=rel_path, branch=branch,
            campaign=self.name, item_id=item_id,
        )

        if body == original:
            # ILB: work() ran and changed nothing. Silence here is what let the
            # first live increment look healthy — so the no-op is a named event,
            # and its level splits on the only thing that distinguishes an
            # anomaly from an idempotent re-run.
            still_present = bool(link_re.search(original))
            emit = log.warning if still_present else log.info
            emit(
                "drip.link001.no_change",
                campaign=self.name,
                item_id=item_id,
                branch=branch,
                path=rel_path,
                target_present=still_present,
                detail=(
                    "target is still in the record but work() could not edit "
                    "it — verify() will fail this item, which is the honest "
                    "direction"
                    if still_present else
                    "ran, nothing to change — the target is already absent "
                    "(an idempotent re-run, not a failure)"
                ),
            )
            return

        path.write_text(body, encoding="utf-8")

    def verify(self, item_id: str) -> bool:
        """Branch-DEPENDENT, because the two branches have different effects.

        A uniform "the link is gone" check would mark every annotation FAILED —
        the annotate branch deliberately keeps the link. This is why the branch
        travels in the item id rather than being looked up.

        **Judged on the SAME tolerance ``work()`` edits with, via the same
        :func:`_flexible_ws_re` seam.** A verifier must never be satisfiable by
        the defect it exists to catch, and until #60 this one was: both used the
        exact string ``[[<target>]]``, so a YAML-wrapped link failed to match in
        ``work()`` (nothing edited) and failed to match here too — which the
        removal branch reads as success. The single condition that stopped the
        repair landing also switched off the check for whether it landed. Any
        future change to how ``work()`` matches must move this method in the same
        commit or it re-opens exactly this hole.
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
            return _flexible_ws_re(
                f"{link} {_PROVENANCE_MARK}"
            ).search(body) is not None
        return _flexible_ws_re(link).search(body) is None

    def spends_quota(self) -> bool:
        return False    # pure vault edits, no LLM

    def verify_is_async(self) -> bool:
        return False    # the edit landed or it didn't


#: The D-ruling's provenance annotation for a surviving learn-record link.
#:
#: **It must never contain an apostrophe.** ``work()`` inserts this INSIDE a
#: single-quoted YAML scalar when the annotated link lives in frontmatter, and
#: YAML's single-quote form escapes a literal ``'`` by doubling it. An
#: un-doubled apostrophe here would terminate the scalar early and corrupt the
#: record's frontmatter — a silent break, since the janitor can then no longer
#: parse the file at all. Pinned, because the constraint is invisible at the
#: point someone would edit the wording.
_PROVENANCE_MARK = "<!-- link-provenance: retained (learn record) -->"

#: Horizontal whitespace only — never a newline. A link at end-of-line must not
#: let its trailing-whitespace match eat the line break and join two lines.
_INLINE_WS = r"[^\S\r\n]"

#: A list entry's bullet. YAML block sequences only ever use ``-``; ``*`` and
#: ``+`` are markdown bullets, which reach :func:`_remove_link` the same way and
#: leave the same debris when a link was the entry's whole content.
_BULLET = r"[-*+]"

#: The leading frontmatter fence and its YAML payload. Non-greedy to the FIRST
#: closing ``---``, matching how ``python-frontmatter`` itself delimits.
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(?P<yaml>.*?)\r?\n---(?:\r?\n|\Z)", re.DOTALL)


def _frontmatter_parses(text: str) -> bool | None:
    """Does this record's frontmatter parse? ``None`` if it has none.

    Three-valued on purpose: "no frontmatter block" and "a frontmatter block
    that does not parse" are different facts, and :func:`_guard_frontmatter`
    needs to tell them apart to avoid firing on body-only records.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    try:
        yaml.safe_load(m.group("yaml"))
    except yaml.YAMLError:
        return False
    return True


def _guard_frontmatter(
    original: str,
    mutated: str,
    *,
    rel_path: str,
    branch: str,
    campaign: str,
    item_id: str,
) -> None:
    """Refuse a mutation that would turn parseable frontmatter into garbage.

    MEASURED, not defensive decoration (#60). Annotating a link that sits in an
    UNQUOTED plain scalar injects ``": "`` into it, and PyYAML then rejects the
    whole document with *"mapping values are not allowed here"*::

        title: See [[learn/L]] here
        →  title: See [[learn/L]] <!-- link-provenance: retained (…) --> here

    The quoted-scalar case is safe (the mark lands inside the quotes and carries
    no apostrophe — see ``_PROVENANCE_MARK``), which is exactly why this needs a
    guard rather than a rule: the safe and unsafe shapes are indistinguishable
    at the point the substitution is made.

    Writing that file would convert a *broken link* — an annoyance the janitor
    reports and this campaign repairs — into an *unparseable record*, which the
    janitor cannot even read to report. That is strictly worse than the defect,
    and it is unrecoverable from the vault. So the mutation raises instead, the
    runner records the item FAILED with the reason, and the file is left byte
    for byte as it was.

    Compares BEFORE against AFTER rather than asserting the result parses: a
    record whose frontmatter was ALREADY broken must still be repairable, or the
    guard would quarantine precisely the records most in need of the sweep.
    """
    if _frontmatter_parses(original) is not True:
        return
    if _frontmatter_parses(mutated) is not False:
        return
    log.warning(
        "drip.link001.yaml_guard",
        campaign=campaign,
        item_id=item_id,
        branch=branch,
        path=rel_path,
        detail="the edit would have made this record's frontmatter "
               "unparseable — refused, and the file is untouched",
    )
    raise ValueError(
        f"{rel_path}: the {branch} edit would corrupt the record's YAML "
        f"frontmatter — refusing to write (the link stays broken, which is "
        f"recoverable; an unparseable record is not)"
    )


def _flexible_ws_re(needle: str) -> re.Pattern[str]:
    """Compile ``needle`` so each of its spaces matches ANY whitespace run.

    THE SEAM (#60). Both branches of both ``Link001Campaign`` methods build
    their matcher here, so ``verify()`` cannot judge on a different tolerance
    than ``work()`` edits with. Four hand-rolled copies is how the two drifted
    apart in the first place.

    Mechanically: split on whitespace, escape each token, rejoin with ``\\s+``.
    ``\\s+`` matches one-or-more, so the SAME pattern matches the unwrapped
    ``[[A B]]`` and the YAML-folded ``[[A\\n  B]]`` — there is no second code
    path for wrapped links, which is what keeps the unwrapped population's
    behaviour unchanged.

    Safe against over-matching because the needles always carry their own
    delimiters: a target's tokens must appear between a literal ``[[`` and
    ``]]``, separated by whitespace ONLY. ``[[Cox and Palmer]]`` therefore cannot
    match ``[[Cox and Palmer Halifax]]`` — the closing brackets bound it — and
    nothing but a real wikilink to the same target can satisfy it.

    Returns a pattern with NO capture groups, so callers may wrap it in their
    own; the callers that do use NAMED groups anyway, so this stays true even if
    that changes.
    """
    tokens = needle.split()
    if not tokens:
        return re.compile(re.escape(needle))
    return re.compile(r"\s+".join(re.escape(t) for t in tokens))


def _remove_link(body: str, link: str) -> str:
    """Delete ``link`` and leave the structure it sat in intact.

    Two cases, and the entry case is tried FIRST because it subsumes the other.

    **A list entry whose entire content is the link loses the WHOLE ENTRY.**
    Healing alone would turn ``- '[[X]]'`` into ``- ''`` — an empty string
    sitting in a list of wikilinks, which is not a link and not absence either.
    Measured (#60): dropping the entry leaves ``related: ['[[person/Other]]']``
    where healing leaves ``related: ['', '[[person/Other]]']``. When the link was
    the SOLE entry the key becomes null (``related:`` → ``None``), which both
    live consumers already tolerate — ``janitor/autofix`` coerces a non-list to
    ``[]`` and ``surveyor/cleanup`` guards on ``isinstance(..., list)``. Applies
    to markdown bullets too, for the same reason and with the same debris.

    An entry carrying anything ELSE (``- '[[X]]' # note``, ``- [[X]] — my
    accountant``) is NOT wholly the link, so it falls through to the heal below
    and keeps its remaining content.

    **Everything else is healed inline.** A plain ``body.replace(link, "")``
    leaves a double space behind every inline link (``See [[X]] here.`` → ``See
    here.``). One record it is a typo; across the ~2,000 the campaign drains it
    becomes a second cleanup campaign, which is why this is fixed at the removal
    site rather than later. The rule: consume the link together with the
    horizontal whitespace on either side, then put back a SINGLE space only when
    the link had whitespace on BOTH sides (i.e. it sat between words). A link
    that was leading, trailing, or hugging punctuation leaves nothing behind, so
    ``See [[X]].`` becomes ``See.`` rather than ``See .``.

    A frontmatter SCALAR field (``source_a: '[[X]]'``) deliberately keeps its
    key and is left as ``''``. Deleting a declared field is a schema change, and
    this campaign has no mandate to make one; an empty value is already how an
    unset field is spelled.

    Both cases match whitespace-tolerantly via :func:`_flexible_ws_re`, so a
    YAML-folded link is removed exactly like an unwrapped one.
    """
    link_re = _flexible_ws_re(link)

    entry_re = re.compile(
        rf"^{_INLINE_WS}*{_BULLET}{_INLINE_WS}*"
        rf"(?P<quote>['\"]?){link_re.pattern}(?P=quote)"
        rf"{_INLINE_WS}*(?:\r?\n|\Z)",
        re.MULTILINE,
    )
    body = entry_re.sub("", body)

    heal_re = re.compile(
        rf"(?P<before>{_INLINE_WS}*){link_re.pattern}(?P<after>{_INLINE_WS}*)"
    )
    return heal_re.sub(
        lambda m: " " if (m.group("before") and m.group("after")) else "", body,
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
