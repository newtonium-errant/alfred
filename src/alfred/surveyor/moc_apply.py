"""Apply a MOC-membership suggestion — the WRITE half of the MOC reader
(2026-08-20, operator ruling R3: "build a reader; the surveyor keeps
writing meanwhile").

The old apply path (``telegram/moc_suggestion_views.apply_accept``) died
with bot.py in the T5 deletion and had zero production callers even before
that, so this is a build rather than a restore. The normalization helpers
below are adapted from it — ``f9028f80^`` holds the original — because the
operator-typo shapes they defend against are real vault data, not a guess.

THE MATRIX (scope × type × op) — stated here AND at
``vault.scope.MOC_APPLY_FIELDS``, which is the enforcement:

  * **scope** — ``moc_apply``, a dedicated narrow scope. Not ``talker``
    (the deleted path's choice, which had a whole conversational surface's
    authority) and not ``surveyor`` (the PRODUCER's scope; the reader must
    not inherit the writer's, or a suggester bug could apply its own
    suggestions). Least privilege for an operator-gesture-driven write.
  * **type** — ``MOC_APPLY_TYPES``, which mirrors the Sub-arc A hook's
    ``_MOC_TRIGGER_TYPES``. A membership on any other type is refused
    rather than half-applied; see the module-level premise pin.
  * **op** — ``edit`` with ``set_fields={"mocs": ...}`` only. No create
    (v1 excludes ``propose_new``), no move, no delete, no body surface.

WHAT ONE APPLY DOES. A queue row is ``N members → one MOC``, so one
operator affirm applies the whole row (operator ruling, 2026-08-20:
splitting it into N cards is the noise the deck exists to prevent). Each
member is an independent ``vault_edit``; a failure on one does NOT abandon
the rest — same per-member failure isolation the deleted path had, and the
same discipline as ``zettel_hooks.dispatch_moc_appends`` one layer down.

IDEMPOTENCE, three ways, because a re-apply must be free:
  1. A member already citing the target (in ANY operator-typo shape) is
     skipped without a write — detection normalizes both sides.
  2. The queue row records which members SUCCEEDED, so a retry after a
     partial apply does not rewrite the ones that landed.
  3. The Sub-arc A ``# Contents`` hook is itself idempotent on the body
     side, so a redundant frontmatter write could not double-append even
     if (1) were bypassed.

RETARGETING IS FREE. When the operator holds and picks a DIFFERENT MOC,
the row's ``candidate_members_to_add`` was computed against the PROPOSED
target and may include members that already cite the chosen one. (1) above
absorbs that with no special case — which is why the retarget path needs
no recomputation at the call site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import structlog

from .moc_suggester import INVENTORY_MOC_STEM_PREFIX

log = structlog.get_logger(__name__)


#: The scope every write in this module goes through. Named once so the
#: writer, its pins, and the act dispatcher cannot drift into two spellings.
MOC_APPLY_SCOPE = "moc_apply"


@dataclass
class MemberOutcome:
    """What happened to ONE member record."""

    rel_path: str
    #: "applied" | "already" | "ineligible_type" | "failed"
    outcome: str
    error: str = ""


@dataclass
class ApplyResult:
    """The result of applying one suggestion row.

    ``ok`` means the apply is COMPLETE — every member either landed or was
    already there. A partial apply is ``ok=False`` with a non-empty
    ``failed``: the row stays actionable rather than retiring, per the
    2026-08-20 ruling, and ``applied_members`` is what a retry may skip.
    """

    target_moc_rel_path: str
    applied: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    ineligible: list[MemberOutcome] = field(default_factory=list)
    failed: list[MemberOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """No member failed. Ineligible members do NOT fail the apply —
        they were never applicable, and the card states that count on its
        face before the swipe (see ``feed_producer``'s applicable count)."""
        return not self.failed

    @property
    def partial(self) -> bool:
        """Some landed, some failed — the state the queue must survive."""
        return bool(self.failed) and bool(self.applied or self.already)

    @property
    def touched(self) -> list[str]:
        """Members now carrying the membership — applied THIS call or
        already carrying it. What the queue records so a retry skips them."""
        return list(self.applied) + list(self.already)

    @property
    def first_error(self) -> str:
        return self.failed[0].error if self.failed else ""


def apply_membership(
    vault_path: Path,
    *,
    member_rel_paths: list[str],
    target_moc_rel_path: str,
    scope: str = MOC_APPLY_SCOPE,
) -> ApplyResult:
    """Add ``target_moc_rel_path`` to each member's ``mocs:`` frontmatter.

    Never raises for an addressable failure — every per-member error is
    captured into :class:`ApplyResult`. The caller (the act dispatcher)
    decides what a partial apply means for the operator and the queue.

    An inventory MOC (``MOC/_*.md``) is refused for the WHOLE call rather
    than per-member: inventory MOCs are predicate-driven and system-
    maintained, so a membership written into one would be overwritten by
    its own generator. The suggester filters them at propose time; this is
    the apply-time defence the deleted path also carried, kept because a
    hand-crafted act request does not go through the suggester.
    """
    from alfred.vault import ops as _ops
    from alfred.vault.scope import MOC_APPLY_TYPES

    result = ApplyResult(target_moc_rel_path=target_moc_rel_path)

    if _is_inventory_moc_path(target_moc_rel_path):
        log.warning(
            "surveyor.moc_apply.inventory_target_refused",
            target_moc=target_moc_rel_path,
            reason="inventory MOCs are predicate-driven and system-maintained "
                   "— a membership written into one would be regenerated away",
        )
        for rel in member_rel_paths:
            result.failed.append(
                MemberOutcome(rel, "failed", "target is an inventory MOC"),
            )
        return result

    canonical_target = _canonicalize_target_to_wikilink(target_moc_rel_path)
    target_normalized = _normalize_single_moc_entry(canonical_target)

    for member_rel_path in member_rel_paths:
        try:
            record = _ops.vault_read(vault_path, member_rel_path)
        except Exception as exc:  # noqa: BLE001 — per-member isolation
            result.failed.append(
                MemberOutcome(member_rel_path, "failed", str(exc)[:200]),
            )
            continue

        fm = dict(record.get("frontmatter") or {})
        member_type = str(fm.get("type") or "")

        # Type gate, checked HERE as well as at the scope layer. Not
        # belt-and-braces for its own sake: reaching the scope refusal
        # would cost a ScopeError per member and report as a FAILURE,
        # when an ineligible member is not a failure — it is a member the
        # membership was never applicable to, and the operator was told
        # the applicable count before swiping.
        if member_type not in MOC_APPLY_TYPES:
            result.ineligible.append(
                MemberOutcome(
                    member_rel_path, "ineligible_type",
                    f"type '{member_type or '(none)'}' is not a MOC trigger type",
                ),
            )
            continue

        existing_verbatim = _normalize_mocs_list(fm.get("mocs"))
        existing_normalized = {
            _normalize_single_moc_entry(e) for e in existing_verbatim
        }
        if target_normalized in existing_normalized:
            result.already.append(member_rel_path)
            continue

        # Append in canonical shape, preserving existing entries VERBATIM
        # so the operator's own wikilink spellings are not rewritten.
        new_list = list(existing_verbatim) + [canonical_target]
        try:
            _ops.vault_edit(
                vault_path,
                member_rel_path,
                set_fields={"mocs": new_list},
                scope=scope,
            )
        except Exception as exc:  # noqa: BLE001 — per-member isolation
            result.failed.append(
                MemberOutcome(member_rel_path, "failed", str(exc)[:200]),
            )
            continue
        result.applied.append(member_rel_path)

    # Per ``feedback_intentionally_left_blank.md``: one summary line per
    # apply, always emitted — an apply that touched nothing (every member
    # already cited the target) must be distinguishable from one that
    # never ran. Every count is present even at zero.
    log.info(
        "surveyor.moc_apply.summary",
        target_moc=target_moc_rel_path,
        members=len(member_rel_paths),
        applied=len(result.applied),
        already=len(result.already),
        ineligible=len(result.ineligible),
        failed=len(result.failed),
        ok=result.ok,
        partial=result.partial,
    )
    return result


# ---------------------------------------------------------------------------
# Normalization helpers — adapted from the deleted apply path (f9028f80^).
# Both layers must agree on what "already cites" means, or the suggester's
# propose-time filter and this module's apply-time idempotency disagree.
# ---------------------------------------------------------------------------


def _normalize_mocs_list(raw) -> list[str]:
    """Coerce a raw ``mocs`` frontmatter value into a list of strings,
    VERBATIM — writing the list back must not rewrite the operator's own
    wikilink shapes. The idempotency comparison normalizes separately."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [str(e) for e in raw if e]
    return []


def _canonicalize_target_to_wikilink(target_rel: str) -> str:
    """``MOC/Stoicism MOC.md`` → ``[[MOC/Stoicism MOC]]``. Idempotent on
    inputs that are already wikilinks."""
    text = str(target_rel).strip()
    if text.startswith("[[") and text.endswith("]]"):
        return text
    if text.endswith(".md"):
        text = text[:-3]
    return f"[[{text}]]"


def _normalize_single_moc_entry(entry: str) -> str:
    """Normalize one ``mocs:`` entry to ``MOC/<Stem>.md`` for comparison.

    Accepts the operator-typo shapes the vault actually carries: bracketed
    wikilink, pipe-aliased wikilink, bare stem, rel_path with or without
    the ``.md`` suffix."""
    text = str(entry).strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    if "|" in text:
        text = text.split("|", 1)[0]
    text = text.strip()
    if not text:
        return ""
    if not text.lower().endswith(".md"):
        text = text + ".md"
    if not text.startswith("MOC/"):
        stem = text[:-3] if text.lower().endswith(".md") else text
        text = f"MOC/{stem}.md"
    return text


def _is_inventory_moc_path(rel_path: str) -> bool:
    """``True`` iff ``rel_path`` points at an inventory MOC (``MOC/_*.md``)."""
    text = str(rel_path).strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2]
    if not text.startswith("MOC/"):
        return False
    return text[len("MOC/"):].startswith(INVENTORY_MOC_STEM_PREFIX)


__all__ = [
    "MOC_APPLY_SCOPE",
    "ApplyResult",
    "MemberOutcome",
    "apply_membership",
]
