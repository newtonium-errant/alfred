"""Phase 2 contamination cleanup — body-text-anchor heuristic.

Background (QA finding 2026-05-03 → Phase 1 ship `96796d4` →
operator config change raise threshold 0.75 → 0.85):

  Phase 1 added per-write attribution logging so future contamination
  is forensically traceable. The threshold raise (in Salem
  config.yaml) prevents NEW contamination — link-add rate dropped
  10.9% → 1.3% on the first post-config sweep.

  But the EXISTING contamination from prior 0.75-threshold sweeps is
  still in the vault — ~1073 records carrying ``person/Ben McMillan.md``
  in ``related_persons`` (and parallel signatures for Jamie / TIXR /
  Halifax Music Fest). This module is the bulk-repair script.

Heuristic — body-text-anchor:

  For each record carrying a target entity in a ``related_<type>``
  field:
    1. Extract the entity's display name (e.g. "Ben McMillan" from
       ``person/Ben McMillan.md``).
    2. Build a "textual presence" search corpus from the record:
         - body text
         - frontmatter title / name / description / summary
         - frontmatter ``related`` list (Obsidian wikilinks)
         - frontmatter ``relationships`` array (machine-generated)
    3. If the entity's display name appears in that corpus AS A WORD
       (boundary-respecting regex), the link is preserved.
    4. If not, the link is marked for removal.

Why exact-word boundary:

  Andrew has both ``person/Ben.md`` AND ``person/Ben McMillan.md``
  in the vault. A naive substring check would over-preserve "Ben"
  for any record containing "Ben McMillan" — which is fine. But the
  reverse trap: a record mentioning only "Ben" (the other person)
  shouldn't preserve "Ben McMillan" via partial substring match.
  Using a word-boundary regex on the FULL display name ("Ben
  McMillan" with both words) prevents this — only records that
  specifically mention "Ben McMillan" as a phrase preserve that
  exact link.

Conservative bias:

  Body-text-anchor only marks for removal when the entity's name has
  ZERO textual presence anywhere in the record's surfaces. Any
  borderline-real association (entity mentioned in passing in body,
  in description, in any related-* list) preserves the link. Better
  to leave 50 stale-but-arguably-related links than to remove 1
  legitimate one.

Test-authoring gotcha (the negation-phrase trap):

  When writing tests for body-text-anchor (or any anchor-style
  text-presence check), express absence SEMANTICALLY — just don't
  mention the name. Do NOT use explicit negation phrases like
  "no mention of X" or "did not include Y" — those phrases CONTAIN
  the names X and Y verbatim, so the heuristic correctly matches
  them and preserves the link the test was trying to verify gets
  removed. First round of
  ``test_partial_removal_preserves_non_target_entries`` (caught
  2026-05-03) shipped with body="No mention of Ben McMillan or
  Jamie." — body-text-anchor matched both names, preserved them,
  test failed. Same trap will hit anyone writing tests for the
  surveyor's reverse-anchor checks.

Dry-run contract:

  ``cleanup_entity_link_contamination(..., dry_run=True)`` walks the
  vault + builds the full report WITHOUT writing. The report (one
  ``CleanupReport`` per call, plus per-record decisions) goes to
  the caller (CLI prints + saves to JSON). Operator approves, then
  re-runs without the flag.

Audit log:

  Every actual removal emits one JSONL line to
  ``data/vault_audit.log`` (``tool: "surveyor-cleanup"``,
  ``op: "modify"``, ``detail: "removed X from Y"``) so a future
  "why was this link removed?" investigation has the same audit
  surface as every other vault mutation. Per
  ``feedback_intentionally_left_blank.md``: silence here is the
  bug Phase 1 was meant to prevent. Apply the same discipline to
  the repair.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter
import structlog

from alfred.vault.mutation_log import append_to_audit_log
from alfred.vault.ops import VaultError, vault_edit

log = structlog.get_logger(__name__)


# Ignore directories that are scaffolding / templates / not real
# vault records. Same set the rest of surveyor uses.
_IGNORE_DIRS: frozenset[str] = frozenset({
    "_templates", "_bases", "_docs", ".obsidian", ".git", "view",
})


# The four typed `related_*` fields the surveyor writes contamination into.
_RELATED_FIELDS_BY_TYPE = {
    "person": "related_persons",
    "matter": "related_matters",
    "org": "related_orgs",
    "project": "related_projects",
}


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class TargetReport:
    """Per-target counts + per-record removal lists."""

    target_path: str  # e.g. "person/Ben McMillan.md"
    target_field: str  # e.g. "related_persons"
    display_name: str  # e.g. "Ben McMillan"
    removed_from: list[str] = field(default_factory=list)
    preserved_in: list[str] = field(default_factory=list)
    not_present_in: int = 0  # records where target wasn't in related_* (skipped)

    def to_dict(self) -> dict:
        return {
            "target_path": self.target_path,
            "target_field": self.target_field,
            "display_name": self.display_name,
            "removed_count": len(self.removed_from),
            "preserved_count": len(self.preserved_in),
            "removed_from": list(self.removed_from),
            "preserved_in": list(self.preserved_in),
        }


@dataclass
class CleanupReport:
    """Aggregated report across all targets."""

    vault_path: str
    dry_run: bool
    targets: list[TargetReport] = field(default_factory=list)
    failed_records: list[dict] = field(default_factory=list)

    @property
    def total_removed(self) -> int:
        return sum(len(t.removed_from) for t in self.targets)

    @property
    def total_preserved(self) -> int:
        return sum(len(t.preserved_in) for t in self.targets)

    @property
    def affected_record_count(self) -> int:
        """Distinct records that lost AT LEAST ONE entry."""
        affected: set[str] = set()
        for t in self.targets:
            affected.update(t.removed_from)
        return len(affected)

    def to_dict(self) -> dict:
        return {
            "vault_path": self.vault_path,
            "dry_run": self.dry_run,
            "total_removed": self.total_removed,
            "total_preserved": self.total_preserved,
            "affected_record_count": self.affected_record_count,
            "targets": [t.to_dict() for t in self.targets],
            "failed_records": list(self.failed_records),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _display_name_from_path(target_path: str) -> str:
    """Derive the human-readable name from a vault path.

    ``person/Ben McMillan.md`` → ``"Ben McMillan"``
    ``org/Halifax Music Fest.md`` → ``"Halifax Music Fest"``

    Strips the type-directory prefix + ``.md`` extension. Used for
    the body-text presence check; matches the filename convention
    the rest of the codebase uses (``_slug_from_rel_path``).
    """
    name = Path(target_path).name
    if name.endswith(".md"):
        name = name[:-3]
    return name


def _has_textual_presence(record_corpus: str, display_name: str) -> bool:
    """Word-boundary regex check for the display name in the corpus.

    "Ben McMillan" matches:
      * "talked to Ben McMillan today"
      * "Ben McMillan: ..."
      * "Re: Ben McMillan"

    Does NOT match (correctly):
      * "Ben said hello" (Ben alone — different person)
      * "McMillan family" (McMillan alone — different context)
      * "Benm Cmillan" (typos)

    Word-boundary on BOTH sides enforces the full-name match. The
    spec calls this out specifically: ``Ben McMillan`` vs ``Ben``
    are distinct person records and over-preservation in either
    direction is a real bug.
    """
    if not display_name:
        return False
    # Escape regex special chars in the display name (e.g. parens,
    # dots — uncommon for person names but possible for orgs like
    # "U.S. Postal Service" or projects with punctuation).
    escaped = re.escape(display_name)
    # ``\b`` word-boundary on both sides. Case-insensitive to handle
    # "ben mcmillan" / "BEN MCMILLAN" / etc. — names get re-cased
    # in transcripts and AI-generated summaries.
    pattern = re.compile(r"\b" + escaped + r"\b", re.IGNORECASE)
    return pattern.search(record_corpus) is not None


def _anchor_term_from_tag(tag: str) -> str:
    """Extract the operator-meaningful anchor term from a tag.

    Hierarchical tags use ``/`` (Obsidian convention) and compound tag
    components use ``-``; the LAST segment after both splits is the
    word we expect to find in the record body.

    Examples:
      * ``events/music`` → ``music``
      * ``live-music`` → ``music``
      * ``events/live-music`` → ``music``
      * ``marketing`` → ``marketing``
      * ``health/care/mental-health`` → ``health``

    Returns empty string for falsy / whitespace-only inputs (defensive
    — caller treats empty as "anchor unfindable" → tag dropped).
    """
    if not tag or not isinstance(tag, str):
        return ""
    stripped = tag.strip()
    if not stripped:
        return ""
    # Split on ``/`` — take the most-specific (rightmost) hierarchy
    # segment. ``events/music`` → ``music``.
    last_slash_seg = stripped.rsplit("/", 1)[-1].strip()
    if not last_slash_seg:
        return ""
    # Split on ``-`` — take the most-specific (rightmost) compound
    # piece. ``live-music`` → ``music``.
    last_dash_seg = last_slash_seg.rsplit("-", 1)[-1].strip()
    return last_dash_seg


def _tag_anchored_in_corpus(tag: str, record_corpus: str) -> bool:
    """Word-boundary check for the tag's anchor term in the corpus.

    Architectural twin to ``_has_textual_presence`` for entity-link
    writes (db9392f); same precision-control predicate, different
    extraction. Surveyor's per-record tag-write gate uses this; a
    future Phase 2 cleanup CLI for historical tag contamination will
    import this same helper for byte-identical parity (the cleanup
    must remove only tags this gate would have rejected; mismatched
    semantics would either over-remove operator-curated tags or
    under-remove cluster-bleed contamination).

    Returns ``True`` when the tag's anchor term (per
    :func:`_anchor_term_from_tag`) appears as a word-boundary match
    in the corpus. Returns ``False`` for empty anchors (defensive —
    a tag with no extractable anchor can't be verified).

    Per-record gate: caller iterates per (tag, record) pair, so a
    cluster's tag-set may filter to different subsets across members.
    """
    anchor = _anchor_term_from_tag(tag)
    if not anchor:
        return False
    return _has_textual_presence(record_corpus, anchor)


def _build_record_corpus(fm: dict, body: str) -> str:
    """Concatenate every searchable surface of the record into one string.

    Includes:
      * body text
      * frontmatter ``title`` / ``name`` / ``description`` / ``summary``
        (the human-readable surfaces)
      * frontmatter ``related`` list (Obsidian wikilinks the operator
        explicitly added)
      * frontmatter ``relationships`` array (machine-generated, but
        the LLM-emitted ``context`` strings name the shared anchor —
        if Ben McMillan is the anchor, his name appears there)

    Excludes:
      * the ``related_<type>`` fields themselves (those are what
        we're potentially cleaning — checking them would short-
        circuit the heuristic to "always preserve")
      * frontmatter dates / status / tags (no textual entity
        references typically)
    """
    parts: list[str] = [body or ""]

    # Human-readable scalar fields.
    for key in ("title", "name", "description", "summary"):
        val = fm.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val)

    # Operator-curated wikilink list. Each entry is typically
    # ``"[[type/Record Name]]"`` — the display name lives between
    # the slash and the closing brackets. Including the raw entries
    # so the regex finds the name inside the wikilink syntax.
    related = fm.get("related")
    if isinstance(related, list):
        for entry in related:
            if isinstance(entry, str):
                parts.append(entry)

    # Machine-generated relationships array. Each entry is a dict
    # with target / type / context / source_anchor / target_anchor
    # fields — the anchor strings name the shared entity.
    relationships = fm.get("relationships")
    if isinstance(relationships, list):
        for rel in relationships:
            if not isinstance(rel, dict):
                continue
            for sub_key in ("target", "context", "source_anchor", "target_anchor"):
                val = rel.get(sub_key)
                if isinstance(val, str) and val.strip():
                    parts.append(val)

    return "\n".join(parts)


def _walk_vault_records(vault_path: Path) -> list[Path]:
    """List every ``*.md`` under the vault, skipping ignored dirs."""
    out: list[Path] = []
    for md_path in vault_path.rglob("*.md"):
        try:
            rel = md_path.relative_to(vault_path)
        except ValueError:
            continue
        if any(part in _IGNORE_DIRS for part in rel.parts):
            continue
        out.append(md_path)
    return sorted(out)


def _infer_field_for_target(target_path: str) -> str | None:
    """Map a target path to its ``related_<type>`` field.

    ``person/Ben McMillan.md`` → ``"related_persons"``
    ``org/TIXR.md`` → ``"related_orgs"``

    Returns None for targets whose type isn't in the
    surveyor-writes-this-field set (e.g. ``event/`` paths — those
    appear in ``alfred_tags`` not ``related_*``).
    """
    parts = Path(target_path).parts
    if not parts:
        return None
    record_type = parts[0]
    return _RELATED_FIELDS_BY_TYPE.get(record_type)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def cleanup_entity_link_contamination(
    vault_path: Path,
    targets: list[str],
    *,
    dry_run: bool = True,
    audit_log_path: Path | str | None = None,
) -> CleanupReport:
    """Bulk-remove contaminated entity links via body-text-anchor heuristic.

    Args:
        vault_path: Vault root.
        targets: List of vault-relative target paths to clean (e.g.
            ``["person/Ben McMillan.md", "person/Jamie.md"]``).
            Each target is checked across every record; removed
            from records where the target's display name has no
            textual presence.
        dry_run: When True (default), populates the report without
            mutating any record. When False, calls ``vault_edit`` to
            persist removals + emits one audit-log line per affected
            file.
        audit_log_path: Path to ``data/vault_audit.log``. Only used
            in non-dry-run mode. When None, audit-log writes are
            skipped (a structured-log warning is emitted instead).

    Returns:
        :class:`CleanupReport` with per-target removal/preservation
        counts + per-record path lists.

    Raises:
        VaultError: only if vault_path itself is invalid. Per-record
        failures (parse error, write error) are caught and recorded
        in ``report.failed_records`` so one bad file can't abort the
        bulk operation.
    """
    if not vault_path.exists() or not vault_path.is_dir():
        raise VaultError(f"vault_path not a directory: {vault_path}")

    # Pre-compute per-target (display_name, field) — done once instead
    # of per-record so the inner loop stays tight.
    target_specs: list[tuple[str, str, str]] = []  # (path, field, display_name)
    target_reports: dict[str, TargetReport] = {}
    for tp in targets:
        field_name = _infer_field_for_target(tp)
        if field_name is None:
            log.warning(
                "surveyor.cleanup.target_field_unknown",
                target_path=tp,
                detail=(
                    "target's type-directory prefix doesn't map to a "
                    "surveyor-written related_* field — skipping. Use "
                    "alfred_tags cleanup for tag contamination."
                ),
            )
            continue
        display = _display_name_from_path(tp)
        target_specs.append((tp, field_name, display))
        target_reports[tp] = TargetReport(
            target_path=tp,
            target_field=field_name,
            display_name=display,
        )

    report = CleanupReport(
        vault_path=str(vault_path),
        dry_run=dry_run,
        targets=list(target_reports.values()),
    )

    if not target_specs:
        log.info(
            "surveyor.cleanup.no_actionable_targets",
            requested=len(targets),
        )
        return report

    log.info(
        "surveyor.cleanup.start",
        vault_path=str(vault_path),
        target_count=len(target_specs),
        dry_run=dry_run,
    )

    # Walk every record once; for each, check every target.
    all_records = _walk_vault_records(vault_path)
    for md_path in all_records:
        try:
            post = frontmatter.load(str(md_path))
        except Exception as exc:  # noqa: BLE001
            report.failed_records.append({
                "path": str(md_path.relative_to(vault_path)),
                "phase": "parse",
                "error": str(exc),
            })
            continue

        fm = dict(post.metadata or {})
        body = post.content or ""
        rel_path = str(md_path.relative_to(vault_path))

        # Build the corpus once per record — each target reuses it.
        corpus = _build_record_corpus(fm, body)

        # Two-phase per-record processing:
        #   Phase 1 (decide): walk every target, classify as
        #     not-present / preserve / remove. Update per-target
        #     report counters and accumulate removals into
        #     ``removals_by_field`` (field → set of paths-to-drop).
        #   Phase 2 (write): for each affected field, build the
        #     post-removal list ONCE from the original ``fm[field]``
        #     minus all collected removals for that field.
        #
        # Bug fixed by this two-phase split (post-d2c30ce):
        # previously the loop wrote ``set_fields[field] = [p for
        # p in existing if p != target_path]`` on EVERY removal
        # decision. When two targets shared the same field (e.g.
        # both Ben McMillan and Jamie in ``related_persons``), the
        # second target's write computed ``existing`` from the
        # untouched ``fm`` and produced a list that omitted Jamie
        # but kept Ben — overwriting the prior removal of Ben.
        # The vault_edit then persisted a list with only ONE of
        # the two targets actually removed. Operator-visible
        # symptom: dry-run report claimed both removed, vault
        # state contained one. Test:
        # ``test_multiple_targets_one_record_one_write``.
        removals_by_field: dict[str, set[str]] = {}
        removed_targets_for_record: list[str] = []

        for target_path, field_name, display_name in target_specs:
            # Skip targets that aren't in this record's related_* field.
            existing = fm.get(field_name)
            if not isinstance(existing, list):
                target_reports[target_path].not_present_in += 1
                continue
            if target_path not in existing:
                target_reports[target_path].not_present_in += 1
                continue

            # Body-text-anchor check.
            if _has_textual_presence(corpus, display_name):
                target_reports[target_path].preserved_in.append(rel_path)
                continue

            # Mark for removal — operator-confirmed contamination.
            target_reports[target_path].removed_from.append(rel_path)
            removed_targets_for_record.append(target_path)
            removals_by_field.setdefault(field_name, set()).add(target_path)

        # No removals on this record? Move on.
        if not removals_by_field:
            continue

        # Phase 2: build the final set_fields payload — ONE filtered
        # list per affected field, derived from the ORIGINAL fm[field]
        # minus ALL accumulated removals for that field. Order
        # preservation: filter in place (existing-list iteration
        # order kept), drop any path that's in the removals set.
        set_fields: dict[str, list] = {}
        for field_name, drop_paths in removals_by_field.items():
            original = fm.get(field_name) or []
            set_fields[field_name] = [
                p for p in original if p not in drop_paths
            ]

        if dry_run:
            log.debug(
                "surveyor.cleanup.would_remove",
                path=rel_path,
                targets=removed_targets_for_record,
                fields=list(set_fields.keys()),
            )
            continue

        # Apply via vault_edit so frontmatter shape + atomic write
        # semantics are preserved. ``vault_edit`` overwrites the
        # field with the filtered list — same as the existing surveyor
        # writer's append-then-cap behavior in reverse.
        try:
            vault_edit(vault_path, rel_path, set_fields=set_fields)
        except Exception as exc:  # noqa: BLE001
            report.failed_records.append({
                "path": rel_path,
                "phase": "write",
                "error": str(exc),
                "fields_attempted": list(set_fields.keys()),
            })
            # Roll back the per-record report entries so the
            # removed_from counts reflect what actually persisted.
            for target_path in removed_targets_for_record:
                target_reports[target_path].removed_from.remove(rel_path)
            continue

        log.info(
            "surveyor.cleanup.removed",
            path=rel_path,
            removed_targets=removed_targets_for_record,
            fields=list(set_fields.keys()),
        )

        # Audit log: one modify line per affected file with detail
        # naming the removed targets so a future grep can answer
        # "what was removed from X.md?".
        if audit_log_path is not None:
            try:
                append_to_audit_log(
                    audit_log_path,
                    tool="surveyor-cleanup",
                    mutations={"files_modified": [rel_path]},
                    detail=(
                        "removed contamination: "
                        + ", ".join(removed_targets_for_record)
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "surveyor.cleanup.audit_log_failed",
                    path=rel_path,
                    error=str(exc),
                )

    log.info(
        "surveyor.cleanup.complete",
        dry_run=dry_run,
        total_removed=report.total_removed,
        total_preserved=report.total_preserved,
        affected_records=report.affected_record_count,
        failed_records=len(report.failed_records),
    )
    return report


# ---------------------------------------------------------------------------
# Phase 2 (tag side) — alfred_tags contamination cleanup
# ---------------------------------------------------------------------------
#
# Architectural twin to ``cleanup_entity_link_contamination`` above; closes
# the alfred_tags arc that started with the per-record write-side gate
# (``47b1b75``) shipped 2026-05-05 and continued with the source-side fix
# (``004ac54``). Phase 1 stops NEW tag contamination at the writer; Phase 2
# scrubs the historical contamination in records written before the gate
# landed.
#
# Why a separate entry point (vs reusing ``cleanup_entity_link_contamination``):
#   1. No target-list — link-side scoped to specific entity paths because
#      the QA finding named specific entities. Tag contamination is general:
#      any record could carry an unanchored tag from cluster-bleed. Walk
#      every record, check every tag in its alfred_tags list.
#   2. Per-record shape — link-side aggregates per-target (one row per
#      Ben McMillan / Jamie / TIXR / etc.). Tag-side has no fixed target
#      set, so the natural shape is per-record-modified (one row per record
#      that had at least one tag scrubbed).
#   3. Frontmatter target — link-side touches related_persons /
#      related_orgs / related_matters / related_projects. Tag-side only
#      touches alfred_tags. No shared write path.
#
# Predicate parity:
#   Both phases use ``_tag_anchored_in_corpus`` (already imported above);
#   the Phase 2 cleanup uses byte-identical semantics to the Phase 1 gate
#   per the docstring on that helper. A tag the gate would reject today
#   is the SAME tag this cleanup removes from history. Mismatched semantics
#   would either over-remove operator-curated tags or under-remove
#   cluster-bleed contamination.


@dataclass
class TagCleanupRecord:
    """One record's tag-cleanup decisions. Only emitted for records that
    actually had at least one tag removed (no-op records aren't surfaced
    individually — their counts roll up into the aggregate)."""

    record_path: str  # vault-relative, e.g. "event/Quiet.md"
    tags_removed: list[str] = field(default_factory=list)
    tags_kept: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "record_path": self.record_path,
            "tags_removed": list(self.tags_removed),
            "tags_kept": list(self.tags_kept),
        }


@dataclass
class TagCleanupReport:
    """Aggregate report from a single ``cleanup_alfred_tags_contamination``
    call. Counters distinguish four buckets so an operator can confirm
    "scanned 1500 records, 800 had alfred_tags, 120 needed scrubbing,
    340 tags removed total" from the report alone."""

    vault_path: str
    dry_run: bool
    records_scanned: int = 0
    records_with_tags: int = 0
    records_modified: int = 0
    tags_removed_total: int = 0
    per_record_modifications: list[TagCleanupRecord] = field(default_factory=list)
    failed_records: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "vault_path": self.vault_path,
            "dry_run": self.dry_run,
            "records_scanned": self.records_scanned,
            "records_with_tags": self.records_with_tags,
            "records_modified": self.records_modified,
            "tags_removed_total": self.tags_removed_total,
            "per_record_modifications": [
                m.to_dict() for m in self.per_record_modifications
            ],
            "failed_records": list(self.failed_records),
        }


def cleanup_alfred_tags_contamination(
    vault_path: Path,
    *,
    dry_run: bool = True,
    audit_log_path: Path | str | None = None,
) -> TagCleanupReport:
    """Bulk-remove unanchored tags from ``alfred_tags`` frontmatter lists.

    Walks every record under ``vault_path`` (skipping ``_IGNORE_DIRS``).
    For each record's ``alfred_tags`` list, partitions tags into
    "anchored in corpus" (kept) vs "not anchored" (removed). Records
    with no ``alfred_tags`` field, no tags-to-remove, or non-list tag
    fields are no-ops — they roll up into the aggregate counts but
    don't appear in ``per_record_modifications``.

    Args:
        vault_path: Vault root.
        dry_run: When True (default), populates the report without
            mutating any record. When False, calls ``vault_edit`` to
            persist tag removals + emits one audit-log line per
            modified file.
        audit_log_path: Path to ``data/vault_audit.log``. Only used in
            non-dry-run mode. When ``None``, audit-log writes are
            skipped (a structured-log warning is emitted in their
            place).

    Returns:
        :class:`TagCleanupReport` with aggregate counts +
        per-modified-record details.

    Raises:
        VaultError: only if ``vault_path`` itself is invalid.
        Per-record failures (parse error, write error) are caught
        and recorded in ``report.failed_records`` so one bad file
        can't abort the bulk operation.

    Empty-tag-list semantics: when EVERY tag in a record's
    ``alfred_tags`` fails the predicate, the field is preserved as an
    empty list (``alfred_tags: []``) rather than removed entirely.
    Frontmatter shape stays stable so future writes append rather
    than re-introduce the field; operator can grep for
    ``alfred_tags: \\[\\]`` to find records that lost everything (a
    diagnostic signal — the surveyor's labeler matched zero anchors
    on this record's content).
    """
    if not vault_path.exists() or not vault_path.is_dir():
        raise VaultError(f"vault_path not a directory: {vault_path}")

    report = TagCleanupReport(
        vault_path=str(vault_path),
        dry_run=dry_run,
    )

    log.info(
        "surveyor.cleanup_tags.start",
        vault_path=str(vault_path),
        dry_run=dry_run,
    )

    all_records = _walk_vault_records(vault_path)
    for md_path in all_records:
        report.records_scanned += 1
        try:
            post = frontmatter.load(str(md_path))
        except Exception as exc:  # noqa: BLE001
            report.failed_records.append({
                "path": str(md_path.relative_to(vault_path)),
                "phase": "parse",
                "error": str(exc),
            })
            continue

        fm = dict(post.metadata or {})
        body = post.content or ""
        rel_path = str(md_path.relative_to(vault_path))

        existing_tags = fm.get("alfred_tags")
        if not isinstance(existing_tags, list):
            # No alfred_tags field, OR the field is set to a non-list
            # value (scalar string, dict, etc. — operator-malformed
            # data the surveyor never wrote). Skip — not a tag-cleanup
            # concern; janitor's structural-validation pass owns this.
            continue

        # Build the corpus once per record (matches link-side shape).
        corpus = _build_record_corpus(fm, body)
        report.records_with_tags += 1

        tags_removed: list[str] = []
        tags_kept: list[str] = []
        for tag in existing_tags:
            # Preserve non-string entries verbatim — same conservative
            # bias as link-side. Janitor handles structural malformations.
            if not isinstance(tag, str):
                tags_kept.append(tag)
                continue
            if _tag_anchored_in_corpus(tag, corpus):
                tags_kept.append(tag)
            else:
                tags_removed.append(tag)

        if not tags_removed:
            continue

        report.records_modified += 1
        report.tags_removed_total += len(tags_removed)
        report.per_record_modifications.append(
            TagCleanupRecord(
                record_path=rel_path,
                tags_removed=tags_removed,
                tags_kept=tags_kept,
            )
        )

        if dry_run:
            log.debug(
                "surveyor.cleanup_tags.would_remove",
                path=rel_path,
                removed=tags_removed,
                kept_count=len(tags_kept),
            )
            continue

        # Apply via vault_edit — same atomic-write + frontmatter-shape
        # discipline as the link-side cleanup. Empty-tag-list
        # preservation is the natural fallout of writing the filtered
        # list back: ``set_fields={"alfred_tags": []}`` keeps the
        # field with an empty value.
        try:
            vault_edit(
                vault_path, rel_path,
                set_fields={"alfred_tags": tags_kept},
            )
        except Exception as exc:  # noqa: BLE001
            report.failed_records.append({
                "path": rel_path,
                "phase": "write",
                "error": str(exc),
            })
            # Roll back the report entries so counts reflect what
            # actually persisted.
            report.records_modified -= 1
            report.tags_removed_total -= len(tags_removed)
            report.per_record_modifications.pop()
            continue

        log.info(
            "surveyor.cleanup_tags.removed",
            path=rel_path,
            removed=tags_removed,
            kept_count=len(tags_kept),
        )

        if audit_log_path is not None:
            try:
                append_to_audit_log(
                    audit_log_path,
                    tool="surveyor-cleanup-tags",
                    mutations={"files_modified": [rel_path]},
                    detail=(
                        "removed unanchored alfred_tags: "
                        + ", ".join(tags_removed)
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "surveyor.cleanup_tags.audit_log_failed",
                    path=rel_path,
                    error=str(exc),
                )

    log.info(
        "surveyor.cleanup_tags.complete",
        dry_run=dry_run,
        records_scanned=report.records_scanned,
        records_with_tags=report.records_with_tags,
        records_modified=report.records_modified,
        tags_removed_total=report.tags_removed_total,
        failed_records=len(report.failed_records),
    )
    return report
