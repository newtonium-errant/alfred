"""Scaffold sync — diff-and-copy bundled scaffold content into existing vaults.

The bundled scaffold (``src/alfred/_bundled/scaffold/``) is the canonical
template tree used by ``alfred quickstart`` to populate a NEW vault.
``alfred scaffold sync`` is the **update path** for already-initialized
vaults: when a release ships new templates, base views, or doc updates,
operators run ``alfred scaffold sync --apply`` to pull the deltas in
without re-running quickstart (which would clobber operator content).

Per-file semantics:

* **CREATE** — file exists in scaffold but not in vault. Sync creates it.
* **NOOP** — file exists in both and bytes match. Sync skips it.
* **CONFLICT** — file exists in both but bytes differ. Sync skips by
  default (operator content preserved); ``--force`` flips to overwrite.

Default-include set covers the four sync-worthy buckets:

* ``_templates/`` — per-type Markdown templates with placeholders
* ``_bases/`` — Dataview ``.base`` views per type
* ``view/`` — user-facing dashboard views (CRM.md, Home.md, etc.)
* Top-level docs: ``CLAUDE.md``, ``README.md``, ``Start Here.md``,
  ``user-profile.md``

Opt-in-only via ``--include .obsidian``:

* ``.obsidian/`` — workspace.json captures operator-customized layout;
  default-syncing would clobber pane state. Double-opt-in (``--include``
  + likely ``--force`` since every file CONFLICTs on a long-lived vault)
  is intentional friction.

Out of scope entirely:

* Content dirs (``person/``, ``account/``, ...) — empty in scaffold; the
  scan would surface zero candidates regardless of include flags
* Bundled skills (``src/alfred/_bundled/skills/``) — agent-prompt
  territory, runtime-located not vault-located
"""

from alfred.scaffold.sync import (
    DEFAULT_EXCLUDE,
    DEFAULT_INCLUDE,
    ScaffoldItem,
    SyncStatus,
    apply_sync,
    scan_scaffold,
)

__all__ = [
    "DEFAULT_EXCLUDE",
    "DEFAULT_INCLUDE",
    "ScaffoldItem",
    "SyncStatus",
    "apply_sync",
    "scan_scaffold",
]
