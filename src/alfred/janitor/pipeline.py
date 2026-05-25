"""Janitor pipeline helpers — surviving utilities after backend collapse.

History: this module previously contained the 3-stage janitor pipeline
(``run_pipeline`` + Stage 2 link-repair LLM + Stage 3 stub-enrich LLM)
used only when ``agent.backend == "openclaw"``. The 3-stage path was
OpenClaw-only by design; Claude (the production backend) always ran
the legacy single-call path in ``daemon.py``.

The backend-abstraction-collapse arc (2026-05-25) removed the OpenClaw
backend and with it the dead pipeline orchestration. What survives here
are the pure-Python helpers that other call sites still use:

- :data:`STAGE_LOOKUP_NEVER_INDEX` + :func:`_stage_lookup_ignore_dirs`
  — system-dir exclusion set for narrower lookups (separate from
  scanner.py's record-validity index). Used today by
  ``tests/test_vault_dont_scan_index_split.py`` to pin the
  dont_scan_dirs vs. dont_index_dirs split contract.
- :func:`_find_link_candidates` / :func:`_is_unambiguous_match` /
  :func:`_fix_link_in_python` — pure-Python link-repair helpers. Today
  reachable only through the deleted Stage 2 orchestration, but the
  bodies are pure functions (no LLM dispatch) and are sufficient on
  their own to fix unambiguous link breaks. A future re-introduction
  of a link-repair pass should call these directly.
- :func:`_format_candidates` — prompt-formatting helper for candidate
  lists. Lightweight; left in place against future LLM re-introduction.
- :func:`_collect_linked_records` — gathers a stub record's
  inbound/outbound linked record bodies as a prompt context block.
  Used by ``tests/test_vault_dont_scan_index_split.py`` to pin the
  dont_scan vs dont_index lookup-dir contract.

Future re-introduction of a multi-stage pipeline (Q3 MCP migration or
similar) should either re-add the orchestration here or move these
helpers into more-aptly-named modules (e.g. ``links.py``,
``link_repair.py``). The deliberate choice to leave them in
``pipeline.py`` is to avoid introducing new files in a pure-subtractive
cleanup commit.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from alfred.vault.mutation_log import log_mutation
from alfred.vault.ops import VaultError, vault_read, vault_search

from .config import JanitorConfig
from .parser import extract_wikilinks
from .utils import get_logger

log = get_logger(__name__)


# Vault-system infrastructure dirs that link-candidate search and
# stub inbound-context lookup MUST always skip, regardless of the
# operator's ``dont_index_dirs`` config:
#
# - ``_templates`` / ``_bases`` / ``_docs`` — scaffold + Dataview views,
#   not real records. A stub's stem may collide with a placeholder
#   (e.g. ``Sample Person``) and pollute the LLM context.
# - ``.obsidian`` — Obsidian app config (workspace.json, plugins/...) is
#   not a record source.
# - ``inbox/processed`` — raw email bodies stored as the audit trail of
#   what curator already consumed. A stub's stem can show up in raw
#   email body text without being a meaningful inbound link.
#
# These are union'd with ``config.vault.dont_index_dirs`` at the call
# sites below. The scanner's ``_build_stem_index`` / ``_build_inbound_index``
# (in scanner.py) intentionally do NOT use this constant — those indexes
# define "what counts as a valid record" and the operator config is the
# single source of truth there. Pipeline-stage lookup is the narrower
# context-enrichment use case where these system dirs are never
# legitimate linkers.
#
# Precedent: ``merge.py::_IGNORE_DIRS`` and ``vault/cli.py::_ignore_dirs()``
# follow the same pattern — system dirs that bypass user config because
# they're never legitimate sources for the operation in question.
STAGE_LOOKUP_NEVER_INDEX: tuple[str, ...] = (
    "_templates",
    "_bases",
    "_docs",
    ".obsidian",
    "inbox/processed",
)


def _stage_lookup_ignore_dirs(config: JanitorConfig) -> list[str]:
    """Return the union of ``config.vault.dont_index_dirs`` and
    ``STAGE_LOOKUP_NEVER_INDEX``, preserving operator-config order and
    appending only the system dirs the operator hasn't already listed.
    """
    operator = list(config.vault.dont_index_dirs)
    return operator + [d for d in STAGE_LOOKUP_NEVER_INDEX if d not in operator]


# ---------------------------------------------------------------------------
# Link-repair helpers (pure Python — formerly Stage 2 utilities)
# ---------------------------------------------------------------------------


def _find_link_candidates(
    broken_target: str,
    vault_path: Path,
    ignore_dirs: list[str],
) -> list[dict]:
    """Search the vault for records that might match a broken wikilink target.

    ``ignore_dirs`` is sourced from ``config.vault.dont_index_dirs`` (NOT
    ``dont_scan_dirs``). A broken wikilink might legitimately resolve to a
    record under ``session/`` or any other dont-scan dir; the candidate
    search must consider those records. See ``alfred.vault.config_helpers``
    for the split rationale.
    """
    candidates: list[dict] = []

    # Strategy 1: search by stem name (the last component of the target)
    stem = broken_target.split("/")[-1] if "/" in broken_target else broken_target
    results = vault_search(vault_path, grep_pattern=stem, ignore_dirs=ignore_dirs)
    for r in results:
        candidates.append(r)

    # Strategy 2: if the target has a directory prefix, try glob in that directory
    if "/" in broken_target:
        dir_part = broken_target.split("/")[0]
        glob = f"{dir_part}/*.md"
        glob_results = vault_search(vault_path, glob_pattern=glob, ignore_dirs=ignore_dirs)
        for r in glob_results:
            if r not in candidates:
                candidates.append(r)

    # Deduplicate by path
    seen: set[str] = set()
    unique: list[dict] = []
    for c in candidates:
        if c["path"] not in seen:
            seen.add(c["path"])
            unique.append(c)

    return unique


def _is_unambiguous_match(
    broken_target: str,
    candidates: list[dict],
) -> str | None:
    """If exactly one candidate matches unambiguously, return its wikilink path.

    Returns the wikilink-style path (without .md) or None if ambiguous.
    """
    if len(candidates) != 1:
        return None

    match = candidates[0]
    match_path = match["path"]
    match_stem = Path(match_path).stem
    target_stem = broken_target.split("/")[-1] if "/" in broken_target else broken_target

    # Unambiguous if the stem matches exactly (case-insensitive)
    if match_stem.lower() == target_stem.lower():
        return match_path.removesuffix(".md") if match_path.endswith(".md") else match_path

    return None


def _fix_link_in_python(
    file_path: str,
    broken_target: str,
    correct_target: str,
    vault_path: Path,
    session_path: str,
) -> bool:
    """Fix a broken wikilink directly in Python. Returns True on success."""
    try:
        record = vault_read(vault_path, file_path)
    except VaultError:
        return False

    fm = record["frontmatter"]
    body = record["body"]
    changed = False

    # Fix in body text
    old_link = f"[[{broken_target}]]"
    new_link = f"[[{correct_target}]]"
    if old_link in body:
        body = body.replace(old_link, new_link)
        changed = True

    # Fix in frontmatter values (wikilinks in string/list fields)
    for key, val in fm.items():
        if isinstance(val, str) and f"[[{broken_target}]]" in val:
            fm[key] = val.replace(f"[[{broken_target}]]", f"[[{correct_target}]]")
            changed = True
        elif isinstance(val, list):
            new_list = []
            for item in val:
                if isinstance(item, str) and f"[[{broken_target}]]" in item:
                    new_list.append(item.replace(f"[[{broken_target}]]", f"[[{correct_target}]]"))
                    changed = True
                else:
                    new_list.append(item)
            if changed:
                fm[key] = new_list

    if not changed:
        return False

    # Write the raw file directly since vault_edit doesn't support body replacement
    import frontmatter as fm_lib

    full_path = vault_path / file_path
    post = fm_lib.Post(body, **fm)
    full_path.write_text(fm_lib.dumps(post) + "\n", encoding="utf-8")
    log_mutation(session_path, "edit", file_path)

    return True


def _format_candidates(candidates: list[dict]) -> str:
    """Format candidate matches for a prompt block.

    Retained for future LLM re-introduction; the deleted Stage 2
    orchestration was its only caller.
    """
    if not candidates:
        return "(no candidates found -- the target may need to be created or is a typo)"

    lines: list[str] = []
    for c in candidates[:15]:
        name = c.get("name", "")
        rec_type = c.get("type", "")
        status = c.get("status", "")
        path = c["path"]
        line = f"- **{path}** (name: {name}, type: {rec_type}"
        if status:
            line += f", status: {status}"
        line += ")"
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stub-context helpers (pure Python — formerly Stage 3 utilities)
# ---------------------------------------------------------------------------


def _collect_linked_records(
    file_path: str,
    vault_path: Path,
    ignore_dirs: list[str],
) -> str:
    """Read all records that link to or from the given file.

    Returns a formatted text block with the content of linked records.

    ``ignore_dirs`` should be sourced from ``config.vault.dont_index_dirs``
    (NOT ``dont_scan_dirs``). The stub-enrichment use case needs the full
    set of records that link TO the stub for context — including records
    in dont_scan_dirs which are still legitimate inbound linkers. See
    ``alfred.vault.config_helpers`` for the split rationale.
    """
    # Read the stub record to find outbound links
    try:
        record = vault_read(vault_path, file_path)
    except VaultError:
        return "(could not read stub record)"

    raw_text = ""
    full_path = vault_path / file_path
    try:
        raw_text = full_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        pass

    outbound_targets = set(extract_wikilinks(raw_text))

    # Find inbound links by searching for the stem name
    stem = Path(file_path).stem
    inbound_results = vault_search(vault_path, grep_pattern=re.escape(stem), ignore_dirs=ignore_dirs)

    # Collect all linked file paths
    linked_paths: set[str] = set()

    # Add inbound links
    for r in inbound_results:
        if r["path"] != file_path:
            linked_paths.add(r["path"])

    # Resolve outbound targets to file paths
    for target in outbound_targets:
        # Try with .md extension
        candidate = f"{target}.md"
        if (vault_path / candidate).exists():
            linked_paths.add(candidate)
        # Try as-is (might already have .md)
        if (vault_path / target).exists():
            linked_paths.add(target)

    # Read each linked record and format
    parts: list[str] = []
    for linked_path in sorted(linked_paths):
        try:
            linked_record = vault_read(vault_path, linked_path)
            fm_str = json.dumps(linked_record["frontmatter"], indent=2, default=str)
            body = linked_record["body"]
            # Truncate very long bodies
            if len(body) > 2000:
                body = body[:2000] + "\n... (truncated)"
            parts.append(f"### {linked_path}\n```yaml\n{fm_str}\n```\n{body}\n")
        except VaultError:
            parts.append(f"### {linked_path}\n(could not read)\n")

    if not parts:
        return "(no linked records found)"

    return "\n---\n".join(parts)
