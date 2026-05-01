"""Core vault operations — create, read, edit, search, move, delete.

When Obsidian is running (1.12+), operations automatically use the Obsidian CLI
for search, link resolution, and moves (which updates wikilinks vault-wide).
Falls back to filesystem operations when Obsidian is unavailable.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Callable

import frontmatter
import structlog
import yaml

from . import obsidian
from .schema import (
    KNOWN_TYPES,
    KNOWN_TYPES_BY_SCOPE,
    LIST_FIELDS,
    NAME_FIELD_BY_TYPE,
    REQUIRED_FIELDS,
    STATUS_BY_TYPE,
    TYPE_DIRECTORY,
)
from .scope import check_scope

log = structlog.get_logger(__name__)


class VaultError(Exception):
    """Raised when a vault operation fails validation.

    Optional ``details`` dict carries structured error metadata that the CLI
    layer surfaces to callers (e.g., the canonical path of a near-match
    collision so the agent can pivot to ``vault_edit``).
    """

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.details = details


def _resolve_vault_path(vault_path: Path, rel_path: str) -> Path:
    """Resolve a relative path within the vault, preventing traversal."""
    full = (vault_path / rel_path).resolve()
    if not str(full).startswith(str(vault_path.resolve())):
        raise VaultError(f"Path traversal denied: {rel_path}")
    return full


def is_ignored_path(rel_path: str | Path, ignore_dirs: set[str] | list[str]) -> bool:
    """Return True if ``rel_path`` should be skipped under ``ignore_dirs``.

    Supports two entry shapes in ``ignore_dirs`` so callers can choose the
    right granularity without changing schema:

    - **Single component** (no ``/`` in the entry) — matches any path
      component. ``".obsidian"`` matches ``foo/.obsidian/bar.md``. This is
      the legacy behavior every scanner already relies on, and the only
      shape fresh installs use today.
    - **Nested path** (contains ``/``) — matches a path prefix. The entry
      ``"inbox/processed"`` matches ``inbox/processed/x.md`` but does NOT
      match ``inbox/x.md`` or ``notes/inbox/processed/x.md``. This lets
      janitor/distiller exclude the curator's audit directory without
      excluding the whole ``inbox/`` (curator needs to see fresh inbox
      items, just not their processed copies).

    Path separators are normalized to ``/`` before matching so Windows
    paths work the same as POSIX.
    """
    rel_str = str(rel_path).replace("\\", "/").strip("/")
    if not rel_str:
        return False
    parts = rel_str.split("/")
    for ig in ignore_dirs:
        if "/" in ig:
            prefix = ig.strip("/")
            if rel_str == prefix or rel_str.startswith(prefix + "/"):
                return True
        else:
            if ig in parts:
                return True
    return False


def _parse_record(file_path: Path) -> tuple[dict, str]:
    """Parse a vault file into (frontmatter_dict, body_str).

    Raises VaultError if the file contains malformed YAML frontmatter.
    """
    try:
        post = frontmatter.load(str(file_path))
    except yaml.YAMLError as exc:
        raise VaultError(
            f"Malformed YAML frontmatter in {file_path.name}: {exc}"
        ) from exc
    return dict(post.metadata), post.content


def _serialize_record(fm: dict, body: str) -> str:
    """Serialize frontmatter + body back to a vault markdown file."""
    post = frontmatter.Post(body, **fm)
    return frontmatter.dumps(post) + "\n"


def _validate_type(record_type: str, scope: str | None = None) -> None:
    """Validate that ``record_type`` is known to the active scope.

    Two-layer contract:

    - ``scope=None`` (default) — preserve the historical behavior:
      only the canonical ``KNOWN_TYPES`` (Salem's 20-type set) are
      accepted. Every CLI / executor path that doesn't propagate a
      scope through stays byte-for-byte unchanged.
    - ``scope`` set — accept the union of canonical types plus any
      scope-specific extension set declared in
      ``schema.KNOWN_TYPES_BY_SCOPE`` (e.g. ``"kalle"`` unlocks
      ``pattern`` / ``principle``; ``"hypatia"`` unlocks ``document``
      / ``concept`` / ``source`` / ``citation`` / ``template``).

    This is the **first** of two gates on ``vault_create`` — it lets
    the type through. The **second** gate is ``check_scope``'s create
    allowlist (``KALLE_CREATE_TYPES``, ``HYPATIA_CREATE_TYPES``,
    ``TALKER_CREATE_TYPES``, etc.), which enforces the per-scope policy.
    Salem-scope agents calling ``vault_create("pattern", scope="talker")``
    will pass ``_validate_type`` here (canonical types only, no extension)
    and then be rejected by ``check_scope``'s ``talker_types_only`` rule.

    Originally this gate hardcoded ``KNOWN_TYPES`` and ran *before*
    ``check_scope``, which silently blocked Hypatia ``document`` and
    KAL-LE ``pattern`` creates with a misleading "Unknown type" error
    even though the type was legal under their scope's allowlist.
    Code-reviewer P1 #2 — release-blocker for Phase 1 Hypatia.
    """
    valid = KNOWN_TYPES_BY_SCOPE.get(scope, KNOWN_TYPES) if scope else KNOWN_TYPES
    if record_type not in valid:
        scope_hint = f" under scope '{scope}'" if scope else ""
        raise VaultError(
            f"Unknown type: '{record_type}'{scope_hint}. "
            f"Valid: {', '.join(sorted(valid))}"
        )


def _validate_status(record_type: str, status: str) -> None:
    if not status:
        return
    valid = STATUS_BY_TYPE.get(record_type, set())
    if valid and status not in valid:
        raise VaultError(
            f"Invalid status '{status}' for type '{record_type}'. "
            f"Valid: {', '.join(sorted(valid))}"
        )


def _coerce_list_fields(fields: dict) -> None:
    """Normalise list-field values so downstream code always sees a list.

    - ``None`` or empty string → ``[]``
    - Non-empty string         → ``[string]``   (single-item list)
    - Already a list           → no change

    Mutates *fields* in place.  Must be called **before** ``_validate_list_fields``.
    """
    for field_name in LIST_FIELDS:
        if field_name not in fields:
            continue
        val = fields[field_name]
        if val is None or val == "":
            fields[field_name] = []
        elif isinstance(val, str):
            fields[field_name] = [val]


def _validate_list_fields(fields: dict) -> None:
    for field_name in LIST_FIELDS:
        val = fields.get(field_name)
        if val is not None and not isinstance(val, list):
            raise VaultError(
                f"Field '{field_name}' must be a list, got {type(val).__name__}"
            )


def _validate_required_fields(fm: dict) -> None:
    for req in REQUIRED_FIELDS:
        if not fm.get(req):
            raise VaultError(f"Missing required field: {req}")


def _check_directory(record_type: str, rel_path: str) -> str | None:
    """Return a warning string if file is in the wrong directory, else None."""
    expected_dir = TYPE_DIRECTORY.get(record_type)
    if not expected_dir:
        return None
    parts = rel_path.replace("\\", "/").split("/")
    if len(parts) > 1 and parts[0] != expected_dir:
        return f"Type '{record_type}' expected in '{expected_dir}/', found in '{parts[0]}/'"
    return None


def _check_near_match(
    vault_path: Path, record_type: str, name: str
) -> tuple[str, str] | None:
    """Return ``(canonical_rel_path, message)`` if a case-insensitive filename
    collision exists, else ``None``.

    Prevents accidental dedup misses like 'PocketPills' vs 'Pocketpills'.
    This is a hard gate inside ``vault_create``: the caller raises ``VaultError``
    with ``details={"canonical_path": ..., "reason": "near_match"}`` so the
    requesting agent can pivot to ``vault_edit`` on the existing record.
    """
    directory = TYPE_DIRECTORY.get(record_type, record_type)
    type_dir = vault_path / directory
    if not type_dir.is_dir():
        return None
    target = name.casefold()
    for existing in type_dir.glob("*.md"):
        if existing.stem.casefold() == target and existing.stem != name:
            canonical = f"{directory}/{existing.stem}.md"
            message = (
                f"Near-match exists: '{canonical}' "
                f"(case-insensitive match for '{name}'). "
                f"Use vault_edit on the existing record instead of creating a duplicate."
            )
            return canonical, message
    return None


def _extract_wikilink_targets(body: str, fm: dict) -> set[str]:
    """Extract all wikilink targets from body text and frontmatter values."""
    link_re = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
    targets: set[str] = set()
    for m in link_re.finditer(body):
        targets.add(m.group(1))
    for v in fm.values():
        if isinstance(v, str):
            for m in link_re.finditer(v):
                targets.add(m.group(1))
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    for m in link_re.finditer(item):
                        targets.add(m.group(1))
    return targets


def _check_wikilinks(body: str, fm: dict, vault_path: Path) -> list[str]:
    """Return list of warning strings for unresolved wikilinks."""
    targets = _extract_wikilink_targets(body, fm)
    if not targets:
        return []

    warnings: list[str] = []
    for target in targets:
        candidate = vault_path / f"{target}.md"
        if not candidate.exists():
            if not (vault_path / target).exists():
                warnings.append(f"Unresolved wikilink: [[{target}]]")
    return warnings


def _load_template(vault_path: Path, record_type: str) -> tuple[dict, str] | None:
    """Load a template from _templates/ if it exists. Returns (fm, body) or None."""
    template_path = vault_path / "_templates" / f"{record_type}.md"
    if not template_path.exists():
        return None
    return _parse_record(template_path)


_BASE_EMBED_RE = re.compile(r"^(##\s+.+\n)?!\[\[.+\.base#.+\]\]$", re.MULTILINE)


def _extract_base_embeds(template_body: str, name: str) -> str:
    """Extract section-heading + base-embed lines from a template body.

    Returns a string like:
        ## Assumptions
        ![[project.base#Assumptions]]

        ## Decisions
        ![[project.base#Decisions]]
    """
    lines = template_body.replace("{{title}}", name).splitlines()
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Check for "## Section\n![[*.base#*]]" pairs
        if line.startswith("## ") and i + 1 < len(lines) and "![[" in lines[i + 1] and ".base#" in lines[i + 1]:
            if result:
                result.append("")
            result.append(line)
            result.append(lines[i + 1])
            i += 2
            continue
        # Standalone base embed without heading
        if "![[" in line and ".base#" in line:
            if result:
                result.append("")
            result.append(line)
        i += 1
    return "\n".join(result) + "\n" if result else ""


# --- Public operations ---


def vault_read(vault_path: Path, rel_path: str) -> dict:
    """Read a vault record. Returns {path, frontmatter, body}."""
    file_path = _resolve_vault_path(vault_path, rel_path)
    if not file_path.exists():
        raise VaultError(f"File not found: {rel_path}")
    if not file_path.suffix == ".md":
        raise VaultError(f"Not a markdown file: {rel_path}")

    fm, body = _parse_record(file_path)
    return {"path": rel_path, "frontmatter": fm, "body": body}


def vault_search(
    vault_path: Path,
    *,
    glob_pattern: str | None = None,
    grep_pattern: str | None = None,
    ignore_dirs: list[str] | None = None,
) -> list[dict]:
    """Search vault files. Returns list of {path, name, type, status}.

    When Obsidian is running, content searches (grep) use Obsidian's live
    index for faster, more accurate results. Falls back to filesystem search.
    """
    # Both branches below filter via ``is_ignored_path`` so callers can
    # use single-component entries (``"_templates"``) AND nested-path
    # entries (``"inbox/processed"``) — matching the public contract
    # documented on ``is_ignored_path``. Pre-2026-05-01 the filter used
    # an inline ``part in ignore`` check that silently dropped slash
    # entries, which made callers think they were filtering ``inbox/processed``
    # while actually filtering nothing. The pipeline-stage constant
    # (``STAGE_LOOKUP_NEVER_INDEX`` in janitor/pipeline.py) relies on
    # the slash entry working.
    ignore = ignore_dirs or []

    # Try Obsidian CLI for content search (grep without glob filter)
    if grep_pattern and not glob_pattern and obsidian.is_available():
        obs_results = obsidian.search_content(grep_pattern)
        if obs_results is not None:
            results: list[dict] = []
            for item in obs_results:
                path = item.get("path", item.get("file", ""))
                if is_ignored_path(path, ignore):
                    continue
                results.append({
                    "path": path,
                    "name": item.get("name", Path(path).stem),
                    "type": item.get("type", ""),
                    "status": item.get("status", ""),
                })
            return results

    # Filesystem fallback
    results: list[dict] = []

    if glob_pattern:
        matches = list(vault_path.glob(glob_pattern))
    else:
        matches = list(vault_path.rglob("*.md"))

    for md_file in sorted(matches):
        rel = md_file.relative_to(vault_path)
        if is_ignored_path(rel, ignore):
            continue

        # If grep, check content
        if grep_pattern:
            try:
                content = md_file.read_text(encoding="utf-8")
                if not re.search(re.escape(grep_pattern), content, re.IGNORECASE):
                    continue
            except (OSError, UnicodeDecodeError):
                continue

        # Parse frontmatter for metadata
        try:
            post = frontmatter.load(str(md_file))
            fm = post.metadata
        except Exception:
            fm = {}

        rel_str = str(rel).replace("\\", "/")
        results.append({
            "path": rel_str,
            "name": fm.get("name") or fm.get("subject") or md_file.stem,
            "type": fm.get("type", ""),
            "status": fm.get("status", ""),
        })

    return results


def vault_list(
    vault_path: Path,
    record_type: str,
    ignore_dirs: list[str] | None = None,
    *,
    scope: str | None = None,
) -> list[dict]:
    """List all records of a given type. Returns list of {path, name, status}.

    ``scope`` (kwarg-only) widens the type-validation gate so a Hypatia
    agent can list ``document`` records and a KAL-LE agent can list
    ``pattern`` records. Default ``None`` preserves the canonical
    ``KNOWN_TYPES`` gate for callers that don't propagate scope.
    """
    _validate_type(record_type, scope=scope)
    ignore = set(ignore_dirs or [])
    results: list[dict] = []

    for md_file in vault_path.rglob("*.md"):
        rel = md_file.relative_to(vault_path)
        if any(part in ignore for part in rel.parts):
            continue
        try:
            post = frontmatter.load(str(md_file))
            if post.metadata.get("type") != record_type:
                continue
        except Exception:
            continue

        rel_str = str(rel).replace("\\", "/")
        results.append({
            "path": rel_str,
            "name": post.metadata.get("name") or post.metadata.get("subject") or md_file.stem,
            "status": post.metadata.get("status", ""),
        })

    return sorted(results, key=lambda r: r["name"])


def vault_context(
    vault_path: Path,
    ignore_dirs: list[str] | None = None,
) -> dict:
    """Build a compact vault summary grouped by type."""
    ignore = set(ignore_dirs or [])
    ignore.add(".obsidian")
    by_type: dict[str, list[dict]] = {}

    for md_file in vault_path.rglob("*.md"):
        rel = md_file.relative_to(vault_path)
        parts = rel.parts
        if any(p in ignore for p in parts):
            continue
        if parts[0] == "inbox":
            continue

        try:
            post = frontmatter.load(str(md_file))
        except Exception:
            continue

        rec_type = post.metadata.get("type", "")
        if not rec_type:
            continue

        rel_str = str(rel).replace("\\", "/")
        if rel_str.endswith(".md"):
            rel_str = rel_str[:-3]

        by_type.setdefault(rec_type, []).append({
            "path": rel_str,
            "name": md_file.stem,
            "status": str(post.metadata.get("status", "")),
        })

    return {"records_by_type": by_type, "total": sum(len(v) for v in by_type.values())}


def vault_create(
    vault_path: Path,
    record_type: str,
    name: str,
    *,
    set_fields: dict | None = None,
    body: str | None = None,
    scope: str | None = None,
) -> dict:
    """Create a new vault record. Returns {path, warnings}.

    Optional ``scope`` runs ``check_scope`` before the write; default
    ``None`` preserves the historical unrestricted behavior. The same
    ``scope`` is also threaded into ``_validate_type`` so scope-specific
    extension types (Hypatia's ``document`` / ``concept`` / ..., KAL-LE's
    ``pattern`` / ``principle``) pass the type gate. Without this,
    ``_validate_type`` rejects extension types before ``check_scope``
    ever runs — release-blocker for Phase 1 Hypatia create operations.
    """
    _validate_type(record_type, scope=scope)
    set_fields = set_fields or {}
    if scope is not None:
        check_scope(
            scope,
            "create",
            record_type=record_type,
            frontmatter=set_fields,
            body_write=body is not None,
        )

    # Determine directory and path
    directory = TYPE_DIRECTORY.get(record_type, record_type)
    rel_path = f"{directory}/{name}.md"
    file_path = _resolve_vault_path(vault_path, rel_path)

    if file_path.exists():
        raise VaultError(f"File already exists: {rel_path}")

    # Case-insensitive near-match is a hard refusal. This runs before any
    # file write so a duplicate spelling can never land on disk. The caller
    # gets the canonical path in VaultError.details so it can pivot to
    # vault_edit on the existing record.
    near = _check_near_match(vault_path, record_type, name)
    if near is not None:
        canonical_path, message = near
        log.error(
            "vault_create.refused",
            reason="near_match",
            attempted_path=rel_path,
            canonical_path=canonical_path,
        )
        raise VaultError(
            message,
            details={
                "canonical_path": canonical_path,
                "reason": "near_match",
                "attempted_path": rel_path,
            },
        )

    # Load template if available
    template = _load_template(vault_path, record_type)
    if template:
        fm, template_body = template
    else:
        fm = {}
        template_body = f"# {name}\n"

    # Set core fields
    fm["type"] = record_type
    title_field = NAME_FIELD_BY_TYPE.get(record_type, "name")
    fm[title_field] = name
    if "created" not in fm or fm["created"] == "{{date}}":
        fm["created"] = date.today().isoformat()

    # Apply user-provided fields
    for k, v in set_fields.items():
        fm[k] = v

    # Coerce + validate
    _coerce_list_fields(fm)
    _validate_status(record_type, fm.get("status", ""))
    _validate_list_fields(fm)
    _validate_required_fields(fm)

    # Resolve body
    if body is not None:
        final_body = body
        # Append base-view embeds from template so entity records get their
        # Dataview sections even when a custom body is provided.
        if template:
            base_embeds = _extract_base_embeds(template_body, name)
            if base_embeds:
                # Check each embed line individually to avoid false negatives
                # from whitespace differences (blank lines, trailing newlines).
                missing = [
                    line for line in base_embeds.splitlines()
                    if line.strip() and "![[" in line and ".base#" in line
                    and line.strip() not in final_body
                ]
                if missing:
                    final_body = final_body.rstrip("\n") + "\n\n" + "\n".join(missing) + "\n"
    else:
        # Process template body — replace {{title}} and {{date}}
        final_body = template_body.replace("{{title}}", name).replace("{{date}}", date.today().isoformat())

    # Check directory placement (soft warning — still writes)
    warnings: list[str] = []
    dir_warn = _check_directory(record_type, rel_path)
    if dir_warn:
        warnings.append(dir_warn)

    # Check wikilinks
    wl_warns = _check_wikilinks(final_body, fm, vault_path)
    warnings.extend(wl_warns)

    # Write file
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(_serialize_record(fm, final_body), encoding="utf-8")

    return {"path": rel_path, "warnings": warnings}


def vault_edit(
    vault_path: Path,
    rel_path: str,
    *,
    set_fields: dict | None = None,
    append_fields: dict | None = None,
    body_append: str | None = None,
    body_rewriter: Callable[[str], str] | None = None,
    scope: str | None = None,
) -> dict:
    """Edit a vault record. Returns {path, fields_changed}.

    ``body_rewriter`` (wk3 commit 7) is an optional callable that takes
    the current body string and returns a new body string. Runs after
    ``body_append`` so a single edit can both append and rewrite, though
    the common case is one or the other. If the rewriter returns the
    body unchanged, ``body`` is NOT added to ``fields_changed`` — the
    caller's check "did anything actually change?" stays honest.

    Used by the telegram calibration writer to surgically replace the
    interior of the ``<!-- ALFRED:CALIBRATION -->`` block without
    disturbing the surrounding person-record body. Generic surface
    rather than a calibration-specific ``body_replace`` kwarg because
    the shape generalises to any marker-fenced rewrite (dynamic briefings,
    section summaries) without another round of vault_ops surgery.

    Optional ``scope`` runs ``check_scope`` before the write; default
    ``None`` preserves historical unrestricted behavior.
    """
    if scope is not None:
        fields_list = (
            list((set_fields or {}).keys()) + list((append_fields or {}).keys())
        )
        body_write_requested = body_append is not None or body_rewriter is not None
        check_scope(
            scope,
            "edit",
            rel_path=rel_path,
            fields=fields_list,
            body_write=body_write_requested,
        )

    file_path = _resolve_vault_path(vault_path, rel_path)
    if not file_path.exists():
        raise VaultError(f"File not found: {rel_path}")

    fm, body = _parse_record(file_path)
    fields_changed: list[str] = []

    # Set fields (overwrite)
    if set_fields:
        for k, v in set_fields.items():
            fm[k] = v
            fields_changed.append(k)

    # Append fields (add to lists)
    if append_fields:
        for k, v in append_fields.items():
            existing = fm.get(k)
            if existing is None:
                fm[k] = [v] if k in LIST_FIELDS else v
            elif isinstance(existing, list):
                existing.append(v)
            else:
                fm[k] = [existing, v]
            fields_changed.append(k)

    # Coerce + validate after edits
    _coerce_list_fields(fm)
    record_type = fm.get("type", "")
    if record_type:
        _validate_status(record_type, fm.get("status", ""))
    _validate_list_fields(fm)

    # Append to body
    if body_append:
        body = body.rstrip() + "\n\n" + body_append + "\n"
        fields_changed.append("body")

    # Rewrite body (wk3 commit 7 — runs last so append + rewrite compose
    # in a predictable order if both are provided on the same call).
    if body_rewriter is not None:
        new_body = body_rewriter(body)
        if new_body != body:
            body = new_body
            if "body" not in fields_changed:
                fields_changed.append("body")

    # Write back
    file_path.write_text(_serialize_record(fm, body), encoding="utf-8")

    return {"path": rel_path, "fields_changed": fields_changed}


def vault_move(
    vault_path: Path,
    from_path: str,
    to_path: str,
    *,
    scope: str | None = None,
) -> dict:
    """Move a vault record. Returns {from, to}.

    When Obsidian is running, uses the Obsidian CLI which automatically
    updates all wikilinks across the vault that reference the moved file.

    Optional ``scope`` runs ``check_scope`` before the move.
    """
    if scope is not None:
        check_scope(scope, "move", rel_path=from_path)

    src = _resolve_vault_path(vault_path, from_path)
    dst = _resolve_vault_path(vault_path, to_path)

    if not src.exists():
        raise VaultError(f"Source not found: {from_path}")
    if dst.exists():
        raise VaultError(f"Destination already exists: {to_path}")

    # Try Obsidian CLI — updates wikilinks vault-wide
    if obsidian.is_available():
        src_name = from_path.removesuffix(".md")
        if obsidian.move_file(src_name, to_path):
            return {"from": from_path, "to": to_path, "wikilinks_updated": True}

    # Filesystem fallback — no wikilink updates
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)

    return {"from": from_path, "to": to_path}


def vault_delete(
    vault_path: Path,
    rel_path: str,
    *,
    scope: str | None = None,
) -> dict:
    """Delete a vault record. Returns {path, deleted}.

    When Obsidian is running, uses the Obsidian CLI which respects the
    configured deletion behavior (system trash, Obsidian trash, or permanent).

    Optional ``scope`` runs ``check_scope`` before the delete.
    """
    if scope is not None:
        check_scope(scope, "delete", rel_path=rel_path)

    file_path = _resolve_vault_path(vault_path, rel_path)
    if not file_path.exists():
        raise VaultError(f"File not found: {rel_path}")

    # Try Obsidian CLI — respects user's trash settings
    if obsidian.is_available():
        file_name = rel_path.removesuffix(".md")
        if obsidian.delete_file(file_name):
            return {"path": rel_path, "deleted": True}

    # Filesystem fallback — permanent delete
    file_path.unlink()
    return {"path": rel_path, "deleted": True}
