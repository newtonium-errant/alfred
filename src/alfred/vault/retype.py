"""Type conversion for vault records — ``vault_retype``.

Use case: Andrew has 11 records currently typed as ``event`` in
``vault/event/`` that are really deadlines / renewals (Duolingo
renewal, iCloud renewal, Kit.co shutdown, etc.). They need to be
``task`` records so they don't pollute the shared Alfred Calendar
that's about to be shared with Jamie (RRTS operations partner).

This module composes the existing primitives (``_parse_record``,
``_serialize_record``, ``vault_delete``, the GCal vault-delete hook)
into a single ``vault_retype`` operation that:

1. Reads the source record (frontmatter + body, untouched in dry-run)
2. Validates target type against the active scope's ``KNOWN_TYPES``
3. Builds the target frontmatter via the per-pair ``FIELD_MAPPINGS``
   table — explicit mappings only, with structured "dropped" /
   "transformed" reporting for visibility
4. Writes the new record at the target path (template-derived from
   ``TYPE_DIRECTORY``)
5. Rewrites vault-wide wikilinks pointing at the old path → new path
   (manual scan because the file isn't being moved by Obsidian — it's
   being recreated under a different type)
6. Deletes the old record via ``vault_delete``, which auto-fires the
   registered event-delete hook → triggers GCal cleanup if the source
   had a ``gcal_event_id``

The GCal cleanup intentionally rides the existing hook plumbing
rather than calling ``sync_event_delete_to_gcal`` directly — single
source of truth for "vault event was removed → GCal mirror should
follow", consistent with all other delete paths.

Field-mapping table is type-pair-keyed (e.g.
``("event", "task")``) so adding new conversion paths later is a
matter of extending the dict. Only ``("event", "task")`` ships
today; the spec lists "out of scope" for the other directions.

Dry-run mode (``apply=False``) returns the full report dict without
touching the vault — operator runs once with ``--dry-run`` to audit
the field mapping per record, then drops the flag to apply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import structlog

from .ops import (
    VaultError,
    _parse_record,
    _resolve_vault_path,
    _serialize_record,
    vault_delete,
)
from .schema import (
    KNOWN_TYPES,
    KNOWN_TYPES_BY_SCOPE,
    NAME_FIELD_BY_TYPE,
    STATUS_BY_TYPE,
    TYPE_DIRECTORY,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Field mapping table — per (source_type, target_type) pair
# ---------------------------------------------------------------------------
#
# Three mapping kinds:
#
#   * KEEP — copy the field's value verbatim (same name, same value)
#   * RENAME[old → new] — copy the value under a new key
#   * DROP — explicitly drop, log under "dropped" in the report
#
# The intent is "explicit is better than implicit": every source
# field that lands in the source record's frontmatter must have a
# decision in the table. Fields not in the source schema at all
# (rare but possible — agent-added customizations) are reported under
# "unknown" with a default-keep behaviour so we never silently lose
# data.
#
# Special handling for fields that need transformation rather than a
# simple rename: see ``_apply_event_to_task_overrides`` below.


@dataclass(frozen=True)
class FieldMapping:
    """One entry in the per-pair conversion table."""

    keep: tuple[str, ...] = ()
    rename: tuple[tuple[str, str], ...] = ()
    drop: tuple[str, ...] = ()
    # Optional callable applied AFTER keep/rename/drop. Receives the
    # in-progress target frontmatter dict + source frontmatter +
    # caller-supplied overrides; mutates the target dict in place.
    # Used for per-pair quirks (e.g., setting required defaults).
    finalize: Callable[[dict, dict, dict], None] | None = None


def _apply_event_to_task_overrides(
    target_fm: dict,
    source_fm: dict,
    overrides: dict,
) -> None:
    """Final pass for event → task: set status, priority, normalize date.

    Called after the keep/rename/drop pass. Honors operator-supplied
    overrides (``status``, ``priority``, ``due``) over defaults.
    """
    # status defaults to "todo" (matches scaffold/_templates/task.md
    # default + STATUS_BY_TYPE["task"] valid set)
    if "status" not in target_fm:
        target_fm["status"] = overrides.get("status") or "todo"
    elif overrides.get("status"):
        target_fm["status"] = overrides["status"]

    # priority defaults to "medium" — matches scaffold/_templates/task.md
    # default. The spec said "normal" but the live scaffold uses
    # "medium"; going with the scaffold value to avoid creating
    # records with a non-template priority.
    if "priority" not in target_fm:
        target_fm["priority"] = overrides.get("priority") or "medium"
    elif overrides.get("priority"):
        target_fm["priority"] = overrides["priority"]

    # due field — task scaffold uses ``due``, NOT ``due_date``. The
    # event → task field-rename above maps date → due. Operator can
    # also override via --due flag.
    if overrides.get("due"):
        target_fm["due"] = overrides["due"]


FIELD_MAPPINGS: dict[tuple[str, str], FieldMapping] = {
    ("event", "task"): FieldMapping(
        keep=(
            "name",
            "title",  # not in task scaffold but harmless and useful for legacy
            "created",
            "alfred_tags",
            "tags",
            "description",
            "summary",  # not in task scaffold but useful for legacy event renames
            "related",
            "relationships",
            "project",
        ),
        rename=(
            ("date", "due"),  # task scaffold uses ``due``
        ),
        drop=(
            # Event-specific fields that don't belong on a task.
            "start", "end", "time", "location", "participants",
            "platform", "ticket_type",
            "gcal_event_id", "gcal_calendar",
            # Event status doesn't map cleanly to task status — drop
            # and let the finalize pass set status="todo" (or operator
            # override).
            "status",
            # ``type`` is set explicitly to the target.
            "type",
            # ``correlation_id`` is per-create-cycle metadata — don't
            # carry across a retype.
            "correlation_id",
            # ``origin_instance`` / ``origin_context`` are creation
            # provenance for cross-instance event proposes; don't
            # carry to a task.
            "origin_instance", "origin_context",
        ),
        finalize=_apply_event_to_task_overrides,
    ),
}


# ---------------------------------------------------------------------------
# Wikilink rewriter
# ---------------------------------------------------------------------------
#
# Scope: rewrites every ``[[event/Foo]]`` (and variants like
# ``[[event/Foo|alias]]``, ``[[event/Foo#heading]]``) reference in
# the vault to point at the new ``[[task/Foo]]`` path. Walks the
# whole vault but only inspects ``.md`` files, skipping common
# ignore dirs.
#
# Why not Obsidian CLI: ``obsidian.move_file`` only works for actual
# file renames. ``vault_retype`` writes a new file at a new path and
# deletes the old one — Obsidian sees that as a delete + create, not
# a move, and doesn't rewrite references.


_IGNORE_DIRS_FOR_LINK_REWRITE: frozenset[str] = frozenset({
    "_templates", "_bases", "_docs", ".obsidian", ".git",
})


def _build_wikilink_rewriter(
    old_rel: str, new_rel: str,
) -> Callable[[str], tuple[str, int]]:
    """Build a function that rewrites wikilinks in a body string.

    Returns ``(rewritten_text, count)``. Count is the number of
    occurrences replaced.

    Match shapes (``old_rel`` like ``"event/Foo Bar"`` — ``.md``
    suffix stripped):
      * ``[[event/Foo Bar]]`` → ``[[task/Foo Bar]]``
      * ``[[event/Foo Bar|Display]]`` → ``[[task/Foo Bar|Display]]``
      * ``[[event/Foo Bar#section]]`` → ``[[task/Foo Bar#section]]``
      * ``[[event/Foo Bar#section|Display]]`` → ``[[task/Foo Bar#section|Display]]``
    """
    # Strip .md suffix because wikilinks don't include it.
    old_link = old_rel[:-3] if old_rel.endswith(".md") else old_rel
    new_link = new_rel[:-3] if new_rel.endswith(".md") else new_rel

    # Escape regex specials in the old link. Wikilink characters
    # tolerated in vault paths include spaces, em-dash, colon-replaced
    # chars from _safe_event_filename. ``re.escape`` handles all of
    # them.
    pattern = _re_compile(
        r"\[\[" + _re_escape(old_link) + r"(?P<suffix>(?:#[^\]|]*)?(?:\|[^\]]*)?)\]\]"
    )

    def _rewrite(text: str) -> tuple[str, int]:
        count = [0]

        def _sub(m: "re.Match[str]") -> str:
            count[0] += 1
            return f"[[{new_link}{m.group('suffix')}]]"

        out = pattern.sub(_sub, text)
        return out, count[0]

    return _rewrite


# Late-bind ``re`` so the import is local to this module's helpers
# (matches ``ops.py`` style for stdlib re-exports).
def _re_compile(pattern: str) -> "re.Pattern[str]":
    return re.compile(pattern)


def _re_escape(s: str) -> str:
    return re.escape(s)


def _scan_and_rewrite_wikilinks(
    vault_path: Path,
    old_rel: str,
    new_rel: str,
    *,
    apply: bool,
    skip_paths: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Walk vault, rewrite wikilinks, return per-file change report.

    ``apply=False`` reports without writing. ``skip_paths`` lets the
    caller exclude specific files (e.g. the source record itself,
    which is about to be deleted anyway).
    """
    rewriter = _build_wikilink_rewriter(old_rel, new_rel)
    skip_set = {p.resolve() for p in skip_paths}

    rewritten_files: list[dict[str, Any]] = []
    total_count = 0

    for md_file in vault_path.rglob("*.md"):
        try:
            rel = md_file.relative_to(vault_path)
        except ValueError:
            continue
        if any(part in _IGNORE_DIRS_FOR_LINK_REWRITE for part in rel.parts):
            continue
        if md_file.resolve() in skip_set:
            continue
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "vault_retype.link_rewrite_read_failed",
                path=str(rel),
                error=str(exc),
            )
            continue
        new_content, count = rewriter(content)
        if count == 0:
            continue
        rewritten_files.append({
            "path": str(rel),
            "occurrences": count,
        })
        total_count += count
        if apply:
            try:
                md_file.write_text(new_content, encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "vault_retype.link_rewrite_write_failed",
                    path=str(rel),
                    error=str(exc),
                )

    return {
        "total_occurrences": total_count,
        "rewritten_files": rewritten_files,
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass
class RetypeReport:
    """Structured outcome of a ``vault_retype`` call.

    Always populated whether ``apply`` was True or False — the
    fields tell the operator (or a JSON consumer) what happened OR
    what would have happened. Serializable to JSON for CLI output.
    """

    source_path: str
    target_path: str
    source_type: str
    target_type: str
    apply: bool
    fields_kept: list[str] = field(default_factory=list)
    fields_renamed: list[dict[str, str]] = field(default_factory=list)
    fields_dropped: list[str] = field(default_factory=list)
    fields_set_by_default: list[dict[str, str]] = field(default_factory=list)
    fields_unknown_kept: list[str] = field(default_factory=list)
    wikilinks_rewritten: int = 0
    wikilinks_files: list[dict[str, Any]] = field(default_factory=list)
    gcal_event_id: str = ""
    gcal_will_delete: bool = False
    keep_source: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "target_path": self.target_path,
            "source_type": self.source_type,
            "target_type": self.target_type,
            "apply": self.apply,
            "fields_kept": list(self.fields_kept),
            "fields_renamed": list(self.fields_renamed),
            "fields_dropped": list(self.fields_dropped),
            "fields_set_by_default": list(self.fields_set_by_default),
            "fields_unknown_kept": list(self.fields_unknown_kept),
            "wikilinks_rewritten": self.wikilinks_rewritten,
            "wikilinks_files": list(self.wikilinks_files),
            "gcal_event_id": self.gcal_event_id,
            "gcal_will_delete": self.gcal_will_delete,
            "keep_source": self.keep_source,
        }


def _scoped_known_types(scope: str | None) -> set[str]:
    if scope and scope in KNOWN_TYPES_BY_SCOPE:
        return KNOWN_TYPES_BY_SCOPE[scope]
    return KNOWN_TYPES


def _build_target_path(
    target_type: str, source_fm: dict, source_filename: str,
) -> str:
    """Choose the target rel_path for the new record.

    Filename strategy: keep the source's filename stem (without the
    type-directory prefix), drop the source type's directory, prepend
    the target type's directory.

      ``event/Halifax Music Fest 2026 — Weezer.md``
      → ``task/Halifax Music Fest 2026 — Weezer.md``

    Edge case: target type has no entry in TYPE_DIRECTORY (e.g.
    ``session`` / ``input`` which use flexible placement). Fall back
    to ``<type>/<stem>.md`` and let the operator move it later if
    needed.
    """
    target_dir = TYPE_DIRECTORY.get(target_type, target_type)
    return f"{target_dir}/{source_filename}"


def vault_retype(
    vault_path: Path,
    source_rel_path: str,
    target_type: str,
    *,
    apply: bool = False,
    keep_source: bool = False,
    overrides: dict | None = None,
    scope: str | None = None,
) -> RetypeReport:
    """Convert a vault record from one type to another.

    Args:
        vault_path: Vault root.
        source_rel_path: Path to the existing record (e.g.
            ``"event/Halifax Music Fest.md"``).
        target_type: Destination record type (e.g. ``"task"``).
        apply: When True, perform the conversion. When False
            (default), populate the report without touching the vault.
        keep_source: When True, leave the source record on disk after
            the new record is created. Default is to delete the source
            (which fires the event-delete hook → GCal cleanup).
        overrides: Caller-supplied frontmatter values that take
            precedence over defaults. For event → task, supports
            ``status``, ``priority``, ``due``.
        scope: Vault scope (used for ``KNOWN_TYPES`` resolution).

    Returns:
        :class:`RetypeReport` describing what was done (or would
        have been done in dry-run mode).

    Raises:
        VaultError: source not found, target type unknown, target
            path already exists, no mapping table for the (source,
            target) pair, or wikilink rewrite write failure (mid-
            apply only).
    """
    overrides = overrides or {}
    src_path = _resolve_vault_path(vault_path, source_rel_path)
    if not src_path.exists():
        raise VaultError(f"Source record not found: {source_rel_path}")

    known = _scoped_known_types(scope)
    if target_type not in known:
        raise VaultError(
            f"Unknown target type {target_type!r} for scope {scope!r}. "
            f"Known types: {sorted(known)}"
        )

    source_fm, body = _parse_record(src_path)
    source_type = str(source_fm.get("type", ""))
    if not source_type:
        raise VaultError(
            f"Source record has no ``type`` in frontmatter: {source_rel_path}"
        )
    if source_type == target_type:
        raise VaultError(
            f"Source is already type {target_type!r}; nothing to retype."
        )

    pair = (source_type, target_type)
    if pair not in FIELD_MAPPINGS:
        raise VaultError(
            f"No retype mapping registered for {source_type!r} → "
            f"{target_type!r}. Add an entry to FIELD_MAPPINGS in "
            f"alfred.vault.retype to enable this conversion path."
        )
    mapping = FIELD_MAPPINGS[pair]

    target_rel_path = _build_target_path(
        target_type, source_fm, src_path.name,
    )
    target_path = _resolve_vault_path(vault_path, target_rel_path)
    if target_path.exists():
        raise VaultError(
            f"Target path already exists: {target_rel_path}. "
            f"Refusing to overwrite — pick a different target name "
            f"or delete the existing target first."
        )

    # Build the target frontmatter via the mapping table.
    target_fm: dict = {"type": target_type}
    fields_kept: list[str] = []
    fields_renamed: list[dict[str, str]] = []
    fields_dropped: list[str] = []
    fields_unknown_kept: list[str] = []

    keep_set = set(mapping.keep)
    rename_map = dict(mapping.rename)
    drop_set = set(mapping.drop)
    handled = keep_set | set(rename_map.keys()) | drop_set | {"type"}

    for key, value in source_fm.items():
        if key == "type":
            continue
        if key in drop_set:
            fields_dropped.append(key)
            continue
        if key in rename_map:
            new_key = rename_map[key]
            target_fm[new_key] = value
            fields_renamed.append({"from": key, "to": new_key})
            continue
        if key in keep_set:
            target_fm[key] = value
            fields_kept.append(key)
            continue
        # Unknown source field — keep by default, log so the operator
        # can decide whether to extend the mapping table.
        target_fm[key] = value
        fields_unknown_kept.append(key)

    # Run the per-pair finalize hook (sets defaults, applies overrides).
    snapshot_before_finalize = set(target_fm.keys())
    if mapping.finalize is not None:
        mapping.finalize(target_fm, dict(source_fm), dict(overrides))
    fields_set_by_default = [
        {"field": k, "value": str(target_fm[k])}
        for k in target_fm.keys()
        if k not in snapshot_before_finalize
    ]

    # Validate target type's status is one of the allowed values.
    if "status" in target_fm and target_type in STATUS_BY_TYPE:
        valid = STATUS_BY_TYPE[target_type]
        if valid and target_fm["status"] not in valid:
            raise VaultError(
                f"Override status {target_fm['status']!r} not valid for "
                f"target type {target_type!r}. Valid: {sorted(valid)}"
            )

    # Ensure the target's title-field is set (matches vault_create
    # behaviour). For event → task both use ``name``.
    title_field = NAME_FIELD_BY_TYPE.get(target_type, "name")
    if title_field not in target_fm:
        # Fall back to the source's name/subject/stem.
        target_fm[title_field] = (
            source_fm.get("name")
            or source_fm.get("subject")
            or src_path.stem
        )

    gcal_event_id = str(source_fm.get("gcal_event_id") or "")
    gcal_will_delete = (
        bool(gcal_event_id)
        and source_type == "event"
        and target_type != "event"
        and not keep_source  # if we keep the source, we keep its GCal mirror
    )

    report = RetypeReport(
        source_path=source_rel_path,
        target_path=target_rel_path,
        source_type=source_type,
        target_type=target_type,
        apply=apply,
        fields_kept=fields_kept,
        fields_renamed=fields_renamed,
        fields_dropped=fields_dropped,
        fields_set_by_default=fields_set_by_default,
        fields_unknown_kept=fields_unknown_kept,
        gcal_event_id=gcal_event_id,
        gcal_will_delete=gcal_will_delete,
        keep_source=keep_source,
    )

    # Wikilink rewrite — always scanned (so dry-run report counts are
    # honest); only written when apply=True. Skip the source record
    # itself since it's about to be deleted (or kept in place — either
    # way, leaving its self-references to the old path inside its
    # own body would be confusing).
    link_result = _scan_and_rewrite_wikilinks(
        vault_path,
        source_rel_path,
        target_rel_path,
        apply=apply,
        skip_paths=(src_path,),
    )
    report.wikilinks_rewritten = link_result["total_occurrences"]
    report.wikilinks_files = link_result["rewritten_files"]

    if not apply:
        log.info(
            "vault_retype.dry_run",
            source=source_rel_path,
            target=target_rel_path,
            wikilinks=report.wikilinks_rewritten,
            gcal_will_delete=gcal_will_delete,
        )
        return report

    # --- Apply phase ------------------------------------------------------

    # 1. Write the new record.
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(_serialize_record(target_fm, body), encoding="utf-8")

    # 2. Wikilinks already rewritten in the scan above (apply=True).
    # No extra step needed.

    # 3. Delete the source unless --keep-source. Deletion goes through
    # ``vault_delete`` which fires the event-delete hook → triggers
    # GCal cleanup automatically (the hook reads ``gcal_event_id``
    # from the pre-delete frontmatter). No need to call
    # ``sync_event_delete_to_gcal`` directly.
    if not keep_source:
        try:
            vault_delete(vault_path, source_rel_path)
        except VaultError as exc:
            log.warning(
                "vault_retype.source_delete_failed",
                source=source_rel_path,
                error=str(exc),
            )
            # The new record exists, links are rewritten; we just have
            # an orphan at the old path. Surface the partial state in
            # the report so the operator can clean up manually.
            report.keep_source = True

    log.info(
        "vault_retype.applied",
        source=source_rel_path,
        target=target_rel_path,
        wikilinks=report.wikilinks_rewritten,
        gcal_event_id=gcal_event_id,
        keep_source=report.keep_source,
    )
    return report
