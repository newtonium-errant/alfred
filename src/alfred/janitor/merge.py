"""Deterministic operator-directed entity merge.

DUP001 duplicates surface during sweeps as triage tasks; human operators
resolve them out-of-band. When the operator approves a merge (winner +
loser), the actual retargeting of inbound wikilinks across the vault is
mechanical work: find every file linking to the loser, rewrite each
reference to point at the winner, copy unique fields from loser →
winner, delete the loser.

Keeping this in Python (rather than widening the janitor LLM scope to
cover every wikilink-bearing frontmatter field) aligns with the Option
E philosophy: LLM for judgment (which pair to merge), deterministic
code for the mechanical rewrites. This module is the "code" half.

The merge helper calls ``vault_ops`` directly, bypassing the CLI scope
gate — it's a privileged operation the operator has already approved,
and no agent scope should ever need these permissions. Every write
still flows through ``log_mutation`` so the ``vault_audit.log`` captures
the merge as a series of edit + delete entries.

Typical invocation from a human operator (via ``python -m`` or a CLI
shim we may add later)::

    from alfred.janitor.merge import merge_entities
    result = merge_entities(
        vault_path=Path("/home/andrew/alfred/vault"),
        winner="org/Pocketpills",
        loser="org/PocketPills",
        session_path="/tmp/merge_session.jsonl",
    )

Not invoked by the janitor daemon; the sweep path emits a triage task
and stops. A human (or a future approval workflow) calls this helper
once the winner is chosen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from alfred.vault.mutation_log import log_mutation
from alfred.vault.ops import (
    VaultError,
    vault_delete,
    vault_edit,
    vault_read,
    vault_search,
)

from .utils import get_logger

log = get_logger(__name__)


# Matches [[target]] or [[target|display]]. Capture group 1 is the target.
# The loser-name match is done case-insensitively inside the rewrite so
# this regex stays generic.
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(\|[^\]]*)?\]\]")

# Directories that the merge sweep should never touch. Mirror the
# defaults used by ``alfred vault`` for search/list.
_IGNORE_DIRS: list[str] = ["_templates", "_bases", "_docs", ".obsidian"]


@dataclass
class MergeResult:
    """Summary of a completed ``merge_entities`` call.

    - ``winner_path`` / ``loser_path``: the records involved (rel paths).
    - ``retargeted_files``: files where an inbound link to the loser
      was rewritten to point at the winner.
    - ``fields_merged``: frontmatter fields copied from loser → winner
      because the winner lacked a value for them.
    - ``body_appended``: True iff the loser's body was non-empty and
      got appended to the winner's body.
    - ``loser_deleted``: True iff the loser record was removed after
      retargeting.
    """

    winner_path: str
    loser_path: str
    retargeted_files: list[str] = field(default_factory=list)
    fields_merged: list[str] = field(default_factory=list)
    body_appended: bool = False
    loser_deleted: bool = False


class MergeError(Exception):
    """Raised when the merge cannot proceed (missing records, etc.)."""


def _normalise(ref: str) -> str:
    """Strip ``.md`` suffix and any wikilink brackets from ``ref``.

    Accepts any of: ``org/Foo``, ``org/Foo.md``, ``[[org/Foo]]``,
    ``[[org/Foo|Display]]``. Returns the bare relative target
    (``org/Foo``) with no suffix and no brackets.
    """
    s = ref.strip()
    if s.startswith("[[") and s.endswith("]]"):
        s = s[2:-2]
    if "|" in s:
        s = s.split("|", 1)[0]
    if s.endswith(".md"):
        s = s[:-3]
    return s


def _rel_path(target: str) -> str:
    """Append ``.md`` so ``target`` can be passed to ``vault_read``/``vault_edit``."""
    return f"{target}.md"


def _rewrite_wikilink_in_string(
    text: str,
    loser_target: str,
    winner_target: str,
) -> tuple[str, int]:
    """Rewrite every wikilink in ``text`` that targets ``loser_target``.

    Case-insensitive match against ``loser_target``; replacement uses
    ``winner_target`` with its exact casing. The display alias (after
    ``|``) is preserved unchanged so hand-written link captions survive
    the merge.

    Returns ``(new_text, n_replacements)``. ``n_replacements`` counts
    every wikilink actually rewritten.
    """
    loser_cf = loser_target.casefold()
    replacements = 0

    def _sub(match: re.Match[str]) -> str:
        nonlocal replacements
        target = match.group(1)
        alias = match.group(2) or ""
        if target.casefold() == loser_cf:
            replacements += 1
            return f"[[{winner_target}{alias}]]"
        return match.group(0)

    new_text = _WIKILINK_RE.sub(_sub, text)
    return new_text, replacements


def _rewrite_wikilinks_in_value(
    value: object,
    loser_target: str,
    winner_target: str,
) -> tuple[object, int]:
    """Recursively rewrite wikilinks in a frontmatter value.

    Strings pass through ``_rewrite_wikilink_in_string``. Lists map the
    same over each element, filtering out None/non-string entries
    unchanged. Other types (bool, int, dict, date) are returned as-is
    with ``n=0``.
    """
    if isinstance(value, str):
        return _rewrite_wikilink_in_string(value, loser_target, winner_target)
    if isinstance(value, list):
        total = 0
        new_list: list[object] = []
        for item in value:
            new_item, n = _rewrite_wikilinks_in_value(item, loser_target, winner_target)
            total += n
            new_list.append(new_item)
        return new_list, total
    return value, 0


def _find_inbound_files(
    vault_path: Path,
    loser_target: str,
) -> list[str]:
    """Return relative paths of files that contain a wikilink to ``loser_target``.

    Uses ``vault_search`` with the stem (last path component) as a grep
    seed — the vault search layer handles ignore_dirs and falls back to
    filesystem grep when Obsidian isn't running. The returned set is
    over-approximate (the stem may match other wikilinks), so the
    caller must confirm each hit actually contains a loser-targeting
    wikilink before rewriting.
    """
    stem = loser_target.split("/")[-1]
    results = vault_search(
        vault_path,
        grep_pattern=stem,
        ignore_dirs=_IGNORE_DIRS,
    )
    return [r["path"] for r in results]


def merge_entities(
    vault_path: Path,
    winner: str,
    loser: str,
    *,
    session_path: str | None = None,
) -> MergeResult:
    """Merge ``loser`` record into ``winner`` with vault-wide link retargeting.

    Steps:
      1. Resolve / validate both records exist.
      2. Copy unique frontmatter fields from loser → winner (fields
         present on loser but absent or empty on winner).
      3. Append loser's body to winner's body (separator = blank line
         + ``<!-- merged from {loser_path} -->``) when the loser body
         has non-whitespace content.
      4. Vault-wide: find every file that links to the loser; rewrite
         each wikilink to point at the winner (case-insensitive match,
         winner's exact casing in the replacement). Frontmatter and
         body are both covered.
      5. Delete the loser record.

    Every mutation logs through ``log_mutation`` so the audit log
    captures the merge as a sequence of edit + delete entries.

    Args:
        vault_path: Root of the vault.
        winner: Target record the loser is being merged INTO. Accepts
            ``org/Foo`` or ``org/Foo.md`` or ``[[org/Foo]]``.
        loser: Record being merged away. Same accepted formats.
        session_path: JSONL mutation log path. Supply one so the audit
            trail captures the merge. None skips audit logging.

    Returns:
        ``MergeResult`` describing what happened.

    Raises:
        MergeError: if either record is missing or cannot be read.
    """
    winner_target = _normalise(winner)
    loser_target = _normalise(loser)

    if winner_target == loser_target:
        raise MergeError(
            f"Winner and loser resolve to the same record: '{winner_target}'"
        )
    if winner_target.casefold() == loser_target.casefold() and winner_target != loser_target:
        # Case-variant siblings — the expected common case. Log for
        # traceability but proceed; the retargeting loop handles it.
        log.info(
            "merge.case_variant",
            winner=winner_target,
            loser=loser_target,
        )

    winner_rel = _rel_path(winner_target)
    loser_rel = _rel_path(loser_target)

    try:
        winner_rec = vault_read(vault_path, winner_rel)
    except VaultError as exc:
        raise MergeError(f"Winner record not found: {winner_rel} ({exc})") from exc
    try:
        loser_rec = vault_read(vault_path, loser_rel)
    except VaultError as exc:
        raise MergeError(f"Loser record not found: {loser_rel} ({exc})") from exc

    result = MergeResult(winner_path=winner_rel, loser_path=loser_rel)

    # --- 2. Merge unique frontmatter fields ---------------------------------
    winner_fm: dict = dict(winner_rec["frontmatter"])
    loser_fm: dict = dict(loser_rec["frontmatter"])
    merge_fields: dict = {}
    # Fields the winner always keeps authoritatively — identity / timing
    # fields that would corrupt the merge if copied from the loser.
    _immutable_on_winner = {"type", "name", "subject", "created"}
    for key, loser_val in loser_fm.items():
        if key in _immutable_on_winner:
            continue
        winner_val = winner_fm.get(key)
        # Copy if winner lacks a value OR has an empty list/string.
        if winner_val is None or winner_val == "" or winner_val == []:
            merge_fields[key] = loser_val

    # --- 3. Append body ------------------------------------------------------
    winner_body: str = winner_rec["body"]
    loser_body: str = loser_rec["body"]
    body_append_text: str | None = None
    if loser_body.strip():
        body_append_text = (
            f"<!-- merged from {loser_rel} -->\n\n{loser_body.rstrip()}"
        )

    # Apply fm merge + body append in a single edit so the audit log
    # has one entry for the winner write.
    if merge_fields or body_append_text is not None:
        try:
            vault_edit(
                vault_path,
                winner_rel,
                set_fields=merge_fields or None,
                body_append=body_append_text,
            )
            log_mutation(
                session_path, "edit", winner_rel,
                fields=list(merge_fields.keys()),
                detail="merge:winner_absorb",
            )
            result.fields_merged = list(merge_fields.keys())
            result.body_appended = body_append_text is not None
            log.info(
                "merge.winner_absorbed",
                winner=winner_rel,
                loser=loser_rel,
                fields=list(merge_fields.keys()),
                body_appended=body_append_text is not None,
            )
        except VaultError as exc:
            raise MergeError(
                f"Failed to write winner record {winner_rel}: {exc}"
            ) from exc

    # --- 4. Vault-wide wikilink retargeting ---------------------------------
    inbound_files = _find_inbound_files(vault_path, loser_target)
    # Exclude the winner and the loser themselves — the winner was just
    # written and must not be re-edited mid-merge, and the loser will be
    # deleted in step 5.
    inbound_files = [
        f for f in inbound_files
        if f not in (winner_rel, loser_rel)
    ]

    for rel in inbound_files:
        try:
            rec = vault_read(vault_path, rel)
        except VaultError:
            # Search can surface paths that fail to re-read (e.g. the
            # structural scanner's near-match quirks). Skip silently —
            # the caller can re-run and the verify step will notice
            # any remaining hits.
            continue

        fm = rec["frontmatter"]
        body = rec["body"]

        # Rewrite frontmatter values containing the loser link.
        fm_updates: dict = {}
        for key, val in fm.items():
            new_val, n = _rewrite_wikilinks_in_value(val, loser_target, winner_target)
            if n > 0:
                fm_updates[key] = new_val

        # Rewrite body. We pre-compute to count hits; the actual write
        # uses a fresh rewriter so ``vault_edit`` re-parses from disk
        # and the rewriter is idempotent if called twice.
        _, body_hits = _rewrite_wikilink_in_string(
            body, loser_target, winner_target,
        )

        if not fm_updates and body_hits == 0:
            # The stem-grep hit something but no actual wikilink to
            # loser was present (probably a stem collision). Skip.
            continue

        # Closure captures loser/winner targets by value so each
        # per-file rewriter is independent.
        def _make_rewriter(lt: str, wt: str):
            def _rewriter(current_body: str) -> str:
                new, _ = _rewrite_wikilink_in_string(current_body, lt, wt)
                return new
            return _rewriter

        try:
            vault_edit(
                vault_path,
                rel,
                set_fields=fm_updates or None,
                body_rewriter=(
                    _make_rewriter(loser_target, winner_target)
                    if body_hits > 0 else None
                ),
            )
            log_mutation(
                session_path, "edit", rel,
                fields=list(fm_updates.keys()),
                detail=f"merge:retarget {loser_target}->{winner_target}",
            )
            result.retargeted_files.append(rel)
            log.info(
                "merge.retargeted",
                file=rel,
                fm_fields=list(fm_updates.keys()),
                body_hits=body_hits,
            )
        except VaultError as exc:
            log.warning(
                "merge.retarget_failed",
                file=rel,
                error=str(exc),
            )

    # --- 5. Delete the loser ------------------------------------------------
    try:
        vault_delete(vault_path, loser_rel)
        log_mutation(
            session_path, "delete", loser_rel,
            detail=f"merge:loser_deleted ({winner_target})",
        )
        result.loser_deleted = True
        log.info("merge.loser_deleted", loser=loser_rel, winner=winner_rel)
    except VaultError as exc:
        log.warning(
            "merge.loser_delete_failed",
            loser=loser_rel,
            error=str(exc),
        )

    log.info(
        "merge.complete",
        winner=winner_rel,
        loser=loser_rel,
        retargeted=len(result.retargeted_files),
        fields_merged=len(result.fields_merged),
        body_appended=result.body_appended,
        loser_deleted=result.loser_deleted,
    )
    return result
