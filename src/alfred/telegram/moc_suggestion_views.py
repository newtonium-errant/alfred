"""``/moc-suggestions`` + ``/accept-moc`` + ``/reject-moc`` view-and-action
surface for the cluster→MOC suggestion mechanism (Phase 5 Sub-arc D2,
2026-05-19).

These commands consume the JSONL queue written by surveyor's Stage 8
(D1 ship — ``surveyor/moc_suggester.py`` + ``moc_suggestion_queue.py``).
Three operator-facing surfaces share one queue:

  * ``collect_pending`` — read-only filter to pending suggestions for the
    ``/moc-suggestions`` listing.
  * ``render_suggestions`` — Markdown reply grouped by target MOC
    (alphabetical, propose-new last) with reasoning + apply-payload count.
  * ``apply_accept`` — accept-path WRITE: per-member ``vault_edit`` of
    each ``candidate_members_to_add`` record's ``mocs:`` frontmatter to
    append the target MOC. Triggers Phase 4 Sub-arc A's existing
    member-append hook (``vault/zettel_hooks.py``) — ONE canonical write
    path to the MOC's ``# Contents``. NO direct write to MOC body. NO
    new scope rules; surveyor's allowlist is unchanged because the
    accept path doesn't go through surveyor scope, it goes through
    talker scope's existing ``edit`` allowance on the member records.

For ``propose_new`` suggestions the accept path additionally
``vault_create``s the new MOC record before iterating members.

Per the design ratified 2026-05-19 (Q3.5 = b): the accept-path writes
to MEMBER frontmatter, NOT to MOC body. Reading the locked-plan
discipline: "the operator only writes wikilinks to MOC's ``# Contents``
indirectly, via the canonical Phase 4 Sub-arc A hook on member
``mocs:`` mutation."

Inventory-MOC filter (defense-in-depth, ratified Q7): the apply path
re-checks ``not target.startswith(INVENTORY_MOC_STEM_PREFIX)`` before
any vault_edit, even though D1 already filters at propose-time.
Three sites = three layers of defense.

Read-only ``/moc-suggestions`` does NOT write to the vault. The accept
path is the only write surface in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import structlog

from alfred.surveyor.moc_suggester import (
    INVENTORY_MOC_STEM_PREFIX,
    MocSuggestion,
)
from alfred.surveyor.moc_suggestion_queue import (
    load_queue,
    update_status,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants — group labels + emoji conventions.
# ---------------------------------------------------------------------------

#: Header group key for propose-new suggestions. Sorted LAST in the
#: render output so existing-MOC suggestions surface first (operator
#: typically wants the high-confidence member_overlap suggestions
#: before the speculative new-MOC proposals).
_PROPOSE_NEW_GROUP_KEY: str = "__propose_new__"

#: Empty-state message per ``feedback_intentionally_left_blank.md``.
#: Distinguishes "no pending suggestions" from "command broken" /
#: "queue file missing".
_EMPTY_STATE_MESSAGE: str = "📋 No pending MOC suggestions."

#: Header for the propose-new section. Single quote marks chosen so the
#: rendered Telegram reply doesn't conflict with Markdown emphasis.
_PROPOSE_NEW_HEADER: str = "## ✨ Propose new MOC"


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ApplyResult:
    """Outcome of an ``/accept-moc`` apply pass.

    Captures both the partial-failure case (some members succeeded, some
    failed — we keep the successes and flip status back to pending) AND
    the all-success case (status → applied). Fields are deliberately
    plain primitives so the bot handler can render them without
    re-importing this module's types.
    """

    suggestion_id: str
    target_label: str  #: human-readable: ``"MOC/Stoicism MOC.md"`` or
                       #: ``"new MOC: Stoicism MOC"`` for propose-new
    members_total: int  #: len(candidate_members_to_add)
    members_succeeded: list[str] = field(default_factory=list)
    members_failed: list[tuple[str, str]] = field(default_factory=list)
    #: list of (member_path, error_message) for failures
    new_moc_created: bool = False  #: True for propose-new path on success
    new_moc_create_error: str | None = None  #: vault_create failure msg

    @property
    def all_succeeded(self) -> bool:
        """True iff zero failures AND zero create errors."""
        return (
            not self.members_failed
            and self.new_moc_create_error is None
            and self.members_total == len(self.members_succeeded)
        )

    @property
    def partial(self) -> bool:
        """True iff at least one success AND at least one failure.

        Distinguishes from the all-failed case (where the apply is
        effectively a no-op and we want a different reply shape).
        """
        return bool(self.members_succeeded) and bool(self.members_failed)

    @property
    def first_error(self) -> str:
        """Concise error string for the bot reply.

        Returns the new-MOC-create error if present, otherwise the first
        per-member error. Truncated to 200 chars (matches the queue's
        ``last_apply_error`` 200-char convention via the status-flip
        path).
        """
        if self.new_moc_create_error:
            return self.new_moc_create_error[:200]
        if self.members_failed:
            _path, err = self.members_failed[0]
            return err[:200]
        return ""


# ---------------------------------------------------------------------------
# Read path — list + filter + render.
# ---------------------------------------------------------------------------


def collect_pending(queue_path: str | Path) -> list[MocSuggestion]:
    """Load the queue, return only pending suggestions.

    Sorted: existing-MOC targets alphabetically by target path, then
    propose-new entries (target=None) sorted alphabetically by proposed
    name. Stable across re-loads given a stable queue.

    Missing queue file is a legitimate empty state — surveyor hasn't
    proposed anything yet, or the operator manually purged. Returns
    empty list silently (the handler's empty-state reply surfaces the
    visible signal).

    Per-line failure isolation lives in ``load_queue`` (corrupt JSONL
    line drops out of the list with a warning). This function adds
    only the pending-status filter on top.
    """
    all_suggestions = load_queue(queue_path)
    pending = [s for s in all_suggestions if s.status == "pending"]
    pending.sort(key=_sort_key_for_listing)
    return pending


def _sort_key_for_listing(s: MocSuggestion) -> tuple[int, str]:
    """Sort key — propose-new entries last; alphabetical within group.

    The tuple's first element is 0 for existing-MOC targets, 1 for
    propose-new, so any tuple ordering puts existing targets before
    propose-new. Second element is the target path for existing or the
    proposed name for propose-new.
    """
    if s.target_moc_rel_path is not None:
        return (0, s.target_moc_rel_path)
    return (1, s.proposed_new_moc_name or "")


def render_suggestions(suggestions: list[MocSuggestion]) -> str:
    """Render pending suggestions grouped by target MOC.

    Empty case:
        ``📋 No pending MOC suggestions.``

    Non-empty case:
        ```
        📋 Pending MOC suggestions (N total)

        ## [[MOC/Stoicism MOC]] (2 suggestions)
        - `ms-20260519-aaaaaaaa` — 3/5 members already cite; 2 to add
        - `ms-20260519-bbbbbbbb` — 4/6 members already cite; 2 to add

        ## ✨ Propose new MOC
        - `ms-20260519-cccccccc` — Task Management Todo List MOC (3 candidate(s))
        ```

    Per ``feedback_intentionally_left_blank.md``: empty state is
    explicit, not a blank reply. The empty message names the surface
    so the operator can grep their workflow.
    """
    if not suggestions:
        return _EMPTY_STATE_MESSAGE

    total = len(suggestions)
    lines: list[str] = [
        f"📋 Pending MOC suggestions ({total} total)",
        "",
    ]

    # Group by target_moc_rel_path; propose_new entries land in a
    # synthetic group keyed by _PROPOSE_NEW_GROUP_KEY so the sort
    # ordering below pushes them to the bottom.
    groups: dict[str, list[MocSuggestion]] = {}
    for s in suggestions:
        if s.target_moc_rel_path is not None:
            groups.setdefault(s.target_moc_rel_path, []).append(s)
        else:
            groups.setdefault(_PROPOSE_NEW_GROUP_KEY, []).append(s)

    # Alphabetical group keys, propose-new last.
    moc_keys = sorted(
        k for k in groups.keys() if k != _PROPOSE_NEW_GROUP_KEY
    )
    if _PROPOSE_NEW_GROUP_KEY in groups:
        moc_keys.append(_PROPOSE_NEW_GROUP_KEY)

    for key in moc_keys:
        group = groups[key]
        count = len(group)
        if key == _PROPOSE_NEW_GROUP_KEY:
            lines.append(_PROPOSE_NEW_HEADER)
        else:
            wikilink_stem = key[:-3] if key.endswith(".md") else key
            label = "suggestion" if count == 1 else "suggestions"
            lines.append(f"## [[{wikilink_stem}]] ({count} {label})")

        for s in group:
            lines.append(_render_one_suggestion_line(s, propose_new=(key == _PROPOSE_NEW_GROUP_KEY)))
        lines.append("")  # blank between groups

    lines.append("Use /accept-moc <id> or /reject-moc <id> to act.")
    return "\n".join(lines).rstrip("\n")


def _render_one_suggestion_line(s: MocSuggestion, *, propose_new: bool) -> str:
    """One Markdown bullet for a single suggestion entry.

    For existing-MOC targets: ``- `<id>` — <reasoning>``
    For propose-new: ``- `<id>` — <proposed_name> (<N> candidate(s))``

    Reasoning is taken verbatim from the suggester's stored string —
    that's where the operator-facing "why" lives.
    """
    if propose_new:
        candidate_count = len(s.candidate_members_to_add)
        plural = "candidate" if candidate_count == 1 else "candidates"
        name = s.proposed_new_moc_name or "(unnamed)"
        return f"- `{s.id}` — {name} ({candidate_count} {plural})"
    return f"- `{s.id}` — {s.reasoning}"


# ---------------------------------------------------------------------------
# Write path — accept / reject.
# ---------------------------------------------------------------------------


def lookup_suggestion(
    queue_path: str | Path, suggestion_id: str,
) -> MocSuggestion | None:
    """Return the suggestion with matching id, or None.

    Used by both ``/accept-moc`` and ``/reject-moc`` handlers to validate
    the id before any status flip. The lookup walks the full queue
    (load + linear scan); the queue is small enough that an index
    isn't worth the maintenance cost.

    Status is NOT filtered here — both accepts of pending entries AND
    rejects of pending entries are valid; the actual status-transition
    enforcement lives in ``update_status``. Returning ``None`` means
    "id does not exist in queue" — distinct from "id exists but is in
    a terminal state."
    """
    all_suggestions = load_queue(queue_path)
    for s in all_suggestions:
        if s.id == suggestion_id:
            return s
    return None


def apply_accept(
    *,
    suggestion: MocSuggestion,
    queue_path: str | Path,
    vault_path: Path,
    scope: str,
) -> ApplyResult:
    """Execute the accept-path for one suggestion.

    Three steps, in order:

      1. Status flip: pending → accepted (via ``update_status``). If
         the queue's state machine refuses this transition (e.g. the
         suggestion was already accepted or rejected by a concurrent
         handler), return an ApplyResult with no work done — the bot
         handler renders the "already acted" reply.
      2. (propose-new only) ``vault_create("MOC", proposed_name, scope=...)``
         to instantiate the new MOC record. If this fails (e.g. name
         collision, scope refuses), flip status back to pending with
         ``last_apply_error`` and return.
      3. For each member in ``candidate_members_to_add``:
         ``vault_edit(member, set_fields={"mocs": new_list_with_target},
         scope=...)`` to append the target MOC to the member's
         ``mocs:`` frontmatter. The vault layer's post-edit hook (Phase
         4 Sub-arc A) appends a wikilink to the MOC's ``# Contents``
         section. Idempotent — the hook's ``_wikilink_target_present``
         check (pipe-alias-aware) means re-running this step is a
         no-op on members already cited.

    Final step: if all members succeeded AND no propose-new error,
    status → applied. Otherwise status → pending with the first error
    in ``last_apply_error`` — the operator can fix the underlying
    issue and re-run ``/accept-moc <id>``; succeeded members are
    silently skipped by Phase 4 Sub-arc A's idempotency.

    Inventory-MOC defense-in-depth: this function refuses to apply if
    the target starts with ``MOC/_`` (inventory namespace per Sub-arc
    B). D1's suggester already filters at propose-time but the bot
    handler re-checks because operator-typed IDs can technically
    point at any queue entry; if someone manually edited the queue
    to inject an inventory-MOC target, we refuse here.

    All vault writes go through the caller-supplied ``scope`` (required)
    so the talker scope's ``edit`` allowance on the member records'
    types governs. No new scope rules introduced.
    """
    # Defense-in-depth (1/3): inventory MOC filter at apply time.
    # The suggester filters at propose time (D1); the queue persists
    # what was proposed; this gate catches any drift.
    target_rel = suggestion.target_moc_rel_path or ""
    if target_rel and _is_inventory_moc_path(target_rel):
        log.warning(
            "moc_suggestion_views.apply_inventory_moc_blocked",
            suggestion_id=suggestion.id,
            target_moc=target_rel,
        )
        # Treat as create-error so the bot renders the partial-fail
        # path; status flips back to pending with the error stored.
        result = ApplyResult(
            suggestion_id=suggestion.id,
            target_label=target_rel,
            members_total=len(suggestion.candidate_members_to_add),
            new_moc_create_error=(
                f"Target {target_rel!r} is an inventory MOC "
                f"(``MOC/_*.md`` namespace); refusing to apply."
            ),
        )
        return result

    proposed_name = suggestion.proposed_new_moc_name or ""
    if proposed_name and _is_inventory_moc_name(proposed_name):
        log.warning(
            "moc_suggestion_views.apply_inventory_moc_name_blocked",
            suggestion_id=suggestion.id,
            proposed_new_moc_name=proposed_name,
        )
        return ApplyResult(
            suggestion_id=suggestion.id,
            target_label=f"new MOC: {proposed_name}",
            members_total=len(suggestion.candidate_members_to_add),
            new_moc_create_error=(
                f"Proposed new MOC name {proposed_name!r} starts with "
                "``_`` (inventory namespace); refusing to apply."
            ),
        )

    # Step 1: state machine transition pending → accepted. If the
    # queue refuses (already non-pending), return a no-work result.
    transition_ok = update_status(queue_path, suggestion.id, "accepted")
    if not transition_ok:
        log.info(
            "moc_suggestion_views.accept_transition_denied",
            suggestion_id=suggestion.id,
            current_status=suggestion.status,
        )
        result = ApplyResult(
            suggestion_id=suggestion.id,
            target_label=target_rel or f"new MOC: {proposed_name}",
            members_total=len(suggestion.candidate_members_to_add),
            new_moc_create_error=(
                f"Status transition denied (suggestion is "
                f"``{suggestion.status}``, not ``pending``)."
            ),
        )
        return result

    # Step 2: propose-new path creates the new MOC record first. If
    # vault_create fails, flip status back to pending and return.
    new_moc_target_rel: str | None = None
    new_moc_created = False
    if suggestion.target_moc_rel_path is None:
        # Propose-new path. ``vault_create("MOC", name, scope=...)``
        # uses the MOC template in scaffold/_templates/MOC.md.
        from alfred.vault import ops as _ops
        try:
            create_result = _ops.vault_create(
                vault_path,
                "MOC",
                proposed_name,
                scope=scope,
            )
            new_moc_target_rel = create_result.get("path")
            new_moc_created = True
        except Exception as exc:  # noqa: BLE001
            err = f"vault_create failed: {type(exc).__name__}: {exc}"
            log.warning(
                "moc_suggestion_views.new_moc_create_failed",
                suggestion_id=suggestion.id,
                proposed_new_moc_name=proposed_name,
                error=err[:300],
            )
            # Flip back to pending so operator can retry.
            update_status(
                queue_path, suggestion.id, "pending",
                last_apply_error=err[:200],
            )
            return ApplyResult(
                suggestion_id=suggestion.id,
                target_label=f"new MOC: {proposed_name}",
                members_total=len(suggestion.candidate_members_to_add),
                new_moc_create_error=err,
            )
    else:
        new_moc_target_rel = suggestion.target_moc_rel_path

    target_label = new_moc_target_rel or target_rel
    result = ApplyResult(
        suggestion_id=suggestion.id,
        target_label=target_label,
        members_total=len(suggestion.candidate_members_to_add),
        new_moc_created=new_moc_created,
    )

    # Step 3: per-member vault_edit. Each failure captured; loop
    # continues so partial success is preserved.
    if new_moc_target_rel is None:
        # Defensive — shouldn't reach here given the branches above.
        result.new_moc_create_error = "internal: no target resolved"
        update_status(
            queue_path, suggestion.id, "pending",
            last_apply_error=result.new_moc_create_error,
        )
        return result

    for member_rel in suggestion.candidate_members_to_add:
        try:
            _append_moc_to_member(
                vault_path=vault_path,
                member_rel_path=member_rel,
                target_moc_rel=new_moc_target_rel,
                scope=scope,
            )
            result.members_succeeded.append(member_rel)
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            result.members_failed.append((member_rel, err))
            log.warning(
                "moc_suggestion_views.member_edit_failed",
                suggestion_id=suggestion.id,
                member_path=member_rel,
                target_moc=new_moc_target_rel,
                error=err[:300],
            )

    # Final status decision: applied iff every member succeeded.
    # Otherwise pending with first-error captured.
    if result.all_succeeded:
        update_status(queue_path, suggestion.id, "applied")
        log.info(
            "moc_suggestion_views.apply_success",
            suggestion_id=suggestion.id,
            target=target_label,
            members_applied=len(result.members_succeeded),
            new_moc_created=new_moc_created,
        )
    else:
        update_status(
            queue_path, suggestion.id, "pending",
            last_apply_error=result.first_error,
        )
        log.info(
            "moc_suggestion_views.apply_partial_or_failed",
            suggestion_id=suggestion.id,
            target=target_label,
            members_succeeded=len(result.members_succeeded),
            members_failed=len(result.members_failed),
            new_moc_create_error=result.new_moc_create_error,
        )

    return result


def reject_suggestion(
    *,
    queue_path: str | Path,
    suggestion_id: str,
) -> bool:
    """Flip a pending suggestion to rejected.

    Negative-learning preserved per ratified Q5: the rejected entry
    stays in the queue indefinitely so surveyor's idempotent upsert
    never re-proposes the same (members, target) pair.

    Returns True on success; False if the id doesn't exist OR the
    suggestion is already non-pending (state machine refuses).
    """
    return update_status(queue_path, suggestion_id, "rejected")


# ---------------------------------------------------------------------------
# Internal helpers.
# ---------------------------------------------------------------------------


def _append_moc_to_member(
    *,
    vault_path: Path,
    member_rel_path: str,
    target_moc_rel: str,
    scope: str,
) -> None:
    """Append the target MOC wikilink to the member record's ``mocs:``
    frontmatter list.

    Reads current ``mocs``, normalizes operator-typo shapes (string,
    bracketed wikilink, pipe-aliased) to the canonical
    ``[[MOC/<Stem>]]`` shape, idempotently adds the target, and writes
    back via ``vault_edit(set_fields={"mocs": new_list}, scope=...)``.
    The post-edit hook (Phase 4 Sub-arc A) fires and appends to the
    MOC's ``# Contents``.

    Idempotent — if the member already cites the target (in any of
    the operator-typo shapes), the function early-returns without
    writing. Detection mirrors the suggester's ``_extract_member_mocs``
    normalization (both layers must agree on what "already cites"
    means, or D1's propose-time filter and D2's apply-time idempotency
    could disagree).

    Raises whatever ``vault_read`` / ``vault_edit`` raise — caller
    catches per-member and continues with the next member.
    """
    from alfred.vault import ops as _ops

    record = _ops.vault_read(vault_path, member_rel_path)
    fm = record.get("frontmatter") or {}
    existing_normalized = _normalize_mocs_list(fm.get("mocs"))

    # Canonicalize the target into the wikilink shape we'll write.
    canonical_target = _canonicalize_target_to_wikilink(target_moc_rel)
    # Normalized form for the idempotency check.
    canonical_normalized = _normalize_single_moc_entry(canonical_target)

    if canonical_normalized in {_normalize_single_moc_entry(e) for e in existing_normalized}:
        # Already cited — no-op. Phase 4 Sub-arc A's hook is
        # idempotent on the body side; matching that idempotency at
        # the frontmatter side prevents needless re-writes.
        log.info(
            "moc_suggestion_views.member_already_cites_target",
            member_path=member_rel_path,
            target_moc=target_moc_rel,
        )
        return

    # Append in canonical wikilink shape. Preserve any existing
    # entries verbatim so the operator's chosen shape isn't
    # rewritten under them.
    new_list = list(existing_normalized) + [canonical_target]
    _ops.vault_edit(
        vault_path,
        member_rel_path,
        set_fields={"mocs": new_list},
        scope=scope,
    )


def _normalize_mocs_list(raw) -> list[str]:
    """Coerce the raw ``mocs`` frontmatter value into a list of strings.

    Same operator-typo defense as the suggester's ``_extract_member_mocs``,
    but returns the strings VERBATIM (not normalized) so that writing
    the list back doesn't rewrite the operator's chosen wikilink
    shapes. The idempotency check at the caller compares NORMALIZED
    forms separately.

    Empty / None / non-list-non-string → empty list.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, list):
        return [str(e) for e in raw if e]
    return []


def _canonicalize_target_to_wikilink(target_rel: str) -> str:
    """Convert a rel_path like ``MOC/Stoicism MOC.md`` to the canonical
    wikilink shape ``[[MOC/Stoicism MOC]]`` that frontmatter ``mocs:``
    entries are typically written in.

    Strips trailing ``.md`` so the wikilink is the conventional
    Obsidian shape. Idempotent on inputs that are already wikilinks
    (returns them unchanged).
    """
    text = target_rel.strip()
    if text.startswith("[[") and text.endswith("]]"):
        return text
    if text.endswith(".md"):
        text = text[:-3]
    return f"[[{text}]]"


def _normalize_single_moc_entry(entry: str) -> str:
    """Normalize a single ``mocs:`` entry to its canonical rel_path
    form ``MOC/<Stem>.md`` for comparison.

    Mirror of the suggester's per-entry normalization. Operator-typo
    shapes accepted: bracketed wikilink, pipe-aliased wikilink, bare
    stem, full rel_path with or without .md suffix. Output is always
    ``MOC/<Stem>.md`` — comparison-friendly.
    """
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
    """``True`` iff the rel_path points at an inventory MOC
    (``MOC/_*.md`` per Phase 4 Sub-arc B).
    """
    if not rel_path.startswith("MOC/"):
        return False
    stem = rel_path[len("MOC/"):]
    return stem.startswith(INVENTORY_MOC_STEM_PREFIX)


def _is_inventory_moc_name(name: str) -> bool:
    """``True`` iff the proposed-new MOC name starts with ``_``."""
    return name.startswith(INVENTORY_MOC_STEM_PREFIX)


__all__ = [
    "ApplyResult",
    "apply_accept",
    "collect_pending",
    "lookup_suggestion",
    "reject_suggestion",
    "render_suggestions",
]
