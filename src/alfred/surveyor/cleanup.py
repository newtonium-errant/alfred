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

        # Track per-record set_fields for vault_edit (one write per
        # record covering all targets; minimises file I/O).
        set_fields: dict[str, list] = {}
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
            # Build the post-removal list, preserving order.
            new_list = [p for p in existing if p != target_path]
            set_fields[field_name] = new_list

        # No removals on this record? Move on.
        if not set_fields:
            continue

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
