"""``alfred fiction`` subcommand handlers — thin CLI wrapper around
:mod:`alfred.telegram.fiction`.

Phase 2.5 follow-up: the SKILL revision routes Hypatia's natural-
language scaffolding ("let's start a fiction project called X")
through ``bash: alfred fiction scaffold "<title>"``. Both paths
(slash command + natural-language) converge on the same Python
function (:func:`alfred.telegram.fiction.scaffold_fiction_project`)
— guaranteed parity, no risk of divergent slug rules or directory
shapes.

Two subcommands:

  * ``alfred fiction scaffold "<title>"`` — scaffolds a fiction
    project. Prints JSON ``{slug, path, files_created,
    already_existed}`` for SKILL consumption.

  * ``alfred fiction slug "<title>"`` — convenience: prints just the
    canonical slug. Useful for SKILL when constructing a wikilink
    before invoking scaffold (e.g., "I'll scaffold draft/fiction/
    <slug>/ for The Lighthouse Keeper").

Both subcommands return integer exit codes (0 OK, 1 error, 2 usage
error) so they compose cleanly with shell pipelines and the parent
CLI's ``sys.exit(...)`` dispatcher.

Output contract (the SKILL depends on this — changes break SKILL
parsing):

  scaffold success::
    {
      "slug": "the-lighthouse-keeper",
      "path": "/home/andrew/library-alexandria/draft/fiction/the-lighthouse-keeper",
      "files_created": ["continuity.md", "story.md", "structure.md",
                        "world.md", "voice.md", "characters/.gitkeep"],
      "already_existed": false
    }

  scaffold idempotent (already_existed)::
    {
      "slug": "the-lighthouse-keeper",
      "path": "/home/andrew/library-alexandria/draft/fiction/the-lighthouse-keeper",
      "files_created": [],
      "already_existed": true
    }

  scaffold error (vault path missing)::
    exit 1; stderr: "ERROR: vault path not accessible: <path>"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import structlog

from .fiction import scaffold_fiction_project, slug_from_title

log = structlog.get_logger(__name__)


def _print(line: str = "") -> None:
    """Print to stdout. Wrapper exists so test fixtures can monkeypatch easily."""
    print(line)


def _print_err(line: str = "") -> None:
    """Print to stderr — used for error messages so JSON stdout stays clean."""
    print(line, file=sys.stderr)


def _resolve_vault_path(raw: dict[str, Any]) -> Path:
    """Pull the vault path out of the unified config.

    Falls back to ``./vault`` when the block is absent, matching the
    convention established by ``alfred gcal backfill``.
    """
    vault_block = raw.get("vault", {}) or {}
    return Path(str(vault_block.get("path", "./vault"))).expanduser()


def _basename_files(rel_dir: str, full_paths: list[str]) -> list[str]:
    """Strip the rel_dir prefix from each path so the SKILL gets the
    short forms (``"continuity.md"``, ``"characters/.gitkeep"``).

    The Python helper returns full vault-relative paths
    (``"draft/fiction/<slug>/continuity.md"``); the SKILL contract
    wants the basenames so it can render a clean file list to Andrew
    without rewriting the prefix.
    """
    prefix = rel_dir.rstrip("/") + "/"
    out: list[str] = []
    for p in full_paths:
        if p.startswith(prefix):
            out.append(p[len(prefix):])
        else:
            # Unexpected — keep the full path so the SKILL operator
            # can see the raw data and correct.
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# `alfred fiction scaffold`
# ---------------------------------------------------------------------------


def cmd_scaffold(raw: dict[str, Any], title: str) -> int:
    """Scaffold a fiction project. Prints JSON; returns exit code."""
    if not title or not title.strip():
        _print_err("ERROR: title is required (e.g. alfred fiction scaffold \"The Glass Forest\")")
        return 2

    vault_path = _resolve_vault_path(raw)
    if not vault_path.exists():
        _print_err(f"ERROR: vault path not accessible: {vault_path}")
        return 1

    log.info(
        "fiction_cli.scaffold_invoked",
        title=title[:80],
        vault_path=str(vault_path),
    )

    result = scaffold_fiction_project(vault_path, title)

    payload = {
        "slug": result.slug,
        "path": str(vault_path / result.rel_dir),
        "files_created": _basename_files(
            result.rel_dir, result.created_files,
        ),
        "already_existed": result.status == "already_exists",
    }
    _print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# `alfred fiction slug`
# ---------------------------------------------------------------------------


def cmd_slug(title: str) -> int:
    """Print the canonical slug for ``title``. Returns exit code."""
    if not title or not title.strip():
        _print_err(
            "ERROR: title is required "
            "(e.g. alfred fiction slug \"The Glass Forest\")"
        )
        return 2
    _print(slug_from_title(title))
    return 0
