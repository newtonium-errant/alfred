"""Diff-and-copy bundled scaffold content into an existing vault.

See :mod:`alfred.scaffold` package docstring for semantics, include set,
and CONFLICT behavior.

The scan is byte-comparison-based: a file is NOOP only when its bytes
are identical to the scaffold version. Whitespace-only divergence,
encoding drift, or trailing-newline difference all surface as CONFLICT.
This is intentional — false-NOOPs would silently leave operator-modified
files unchanged while the dry-run summary said "fully synced." False-
CONFLICTs (where the operator hand-replicated the scaffold content) are
rare and resolved by ``--force``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

import structlog

log = structlog.get_logger(__name__)


class SyncStatus(str, Enum):
    """Per-file scan outcome."""

    CREATE = "CREATE"
    NOOP = "NOOP"
    CONFLICT = "CONFLICT"


@dataclass
class ScaffoldItem:
    """One file from the scaffold tree paired with its target in the vault.

    Attributes:
        relpath: Path relative to the scaffold root (e.g. ``"_templates/person.md"``).
            Also serves as the vault-relative path of the destination.
        scaffold_path: Absolute path to the source file in the bundled scaffold.
        vault_path: Absolute path the file would land at in the target vault
            (may not exist yet for CREATE items).
        status: One of :class:`SyncStatus`.
    """

    relpath: str
    scaffold_path: Path
    vault_path: Path
    status: SyncStatus


# Default include set — the four sync-worthy top-level buckets the
# scaffold ships. ``.obsidian`` is intentionally NOT here; operator
# must opt-in via explicit --include .obsidian.
DEFAULT_INCLUDE: tuple[str, ...] = (
    "_templates",
    "_bases",
    "view",
    "CLAUDE.md",
    "README.md",
    "Start Here.md",
    "user-profile.md",
)

# Default exclude set — operator-customizable / runtime / placeholder
# buckets that should never auto-sync. ``.obsidian`` is the canonical
# carve-out; the .gitkeep entries are scaffold-only placeholders that
# create empty dirs in fresh vaults but shouldn't propagate.
DEFAULT_EXCLUDE: tuple[str, ...] = (
    ".obsidian",
    ".gitkeep",
)


def _is_under(relpath: str, prefix: str) -> bool:
    """Return True iff ``relpath`` equals ``prefix`` or sits beneath it.

    Path-segment comparison, not string-prefix — avoids the bug where
    ``_templates_old/foo.md`` would match prefix ``_templates``. Both
    ``"_templates/foo.md"`` matches prefix ``"_templates"`` (True) and
    ``"_templates"`` itself matches (True).
    """
    if relpath == prefix:
        return True
    return relpath.startswith(prefix.rstrip("/") + "/")


def _should_include(
    relpath: str,
    include: Iterable[str],
    exclude: Iterable[str],
) -> bool:
    """Apply include/exclude filters to a candidate relpath.

    Exclude wins over include — if a relpath matches both an include
    entry and an exclude entry, the file is skipped. The ``.gitkeep``
    exclude is name-based (matches the filename anywhere in the tree);
    everything else is path-prefix-based.
    """
    # name-based exclude (matches at any depth)
    name = Path(relpath).name
    for ex in exclude:
        if not ex.startswith(".") or "/" in ex:
            # path-style exclude
            if _is_under(relpath, ex):
                return False
        else:
            # dotfile-name exclude (e.g. ``.gitkeep``)
            if name == ex:
                return False
            # also treat as path-prefix (e.g. ``.obsidian``)
            if _is_under(relpath, ex):
                return False

    for inc in include:
        if _is_under(relpath, inc):
            return True
    return False


def _classify(scaffold_file: Path, vault_file: Path) -> SyncStatus:
    """Byte-compare a scaffold/vault file pair, return per-file status."""
    if not vault_file.exists():
        return SyncStatus.CREATE
    try:
        scaffold_bytes = scaffold_file.read_bytes()
        vault_bytes = vault_file.read_bytes()
    except OSError:
        # If we can't read the vault file we treat as CONFLICT to be
        # safe — write would also fail without --force surfacing. Better
        # to flag than silently swallow.
        return SyncStatus.CONFLICT
    if scaffold_bytes == vault_bytes:
        return SyncStatus.NOOP
    return SyncStatus.CONFLICT


def scan_scaffold(
    scaffold_dir: Path,
    vault_dir: Path,
    include: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
) -> list[ScaffoldItem]:
    """Walk the scaffold tree and classify each file against the vault.

    Args:
        scaffold_dir: Root of the bundled scaffold (typically
            ``alfred._data.get_scaffold_dir()``).
        vault_dir: Root of the target vault on disk.
        include: Sequence of path-prefixes to include. Defaults to
            :data:`DEFAULT_INCLUDE`.
        exclude: Sequence of path-prefixes or dot-name exclusions.
            Defaults to :data:`DEFAULT_EXCLUDE`.

    Returns:
        List of :class:`ScaffoldItem` for every scaffold file that
        survives the include/exclude filter. Empty list iff no
        candidates match — caller is responsible for emitting an
        explicit "no candidates" message so the absence is
        distinguishable from a broken scan.
    """
    if include is None:
        include = DEFAULT_INCLUDE
    if exclude is None:
        exclude = DEFAULT_EXCLUDE

    if not scaffold_dir.is_dir():
        raise FileNotFoundError(f"scaffold_dir does not exist: {scaffold_dir}")
    # vault_dir not required to exist; CREATE-only sync onto a missing
    # vault is a valid use case (operator bootstrapping). But we DO
    # require the parent to exist so we don't accidentally pave over
    # an arbitrary subdir with a typo.
    if not vault_dir.parent.exists():
        raise FileNotFoundError(
            f"vault_dir parent does not exist: {vault_dir.parent} "
            f"(refusing to create vault root from typo)"
        )

    items: list[ScaffoldItem] = []
    for scaffold_file in sorted(scaffold_dir.rglob("*")):
        if not scaffold_file.is_file():
            continue
        relpath = str(scaffold_file.relative_to(scaffold_dir))
        # normalize to forward slashes for consistent matching across OS
        relpath = relpath.replace("\\", "/")
        if not _should_include(relpath, include, exclude):
            continue
        vault_file = vault_dir / relpath
        status = _classify(scaffold_file, vault_file)
        items.append(
            ScaffoldItem(
                relpath=relpath,
                scaffold_path=scaffold_file,
                vault_path=vault_file,
                status=status,
            )
        )
    return items


@dataclass
class SyncSummary:
    """Aggregate result of :func:`apply_sync`.

    Attributes:
        created: relpaths of files newly written to the vault.
        overwritten: relpaths of files where ``--force`` flipped a
            CONFLICT into an overwrite.
        skipped_conflicts: relpaths of CONFLICT files left untouched
            (operator content preserved).
        skipped_noops: relpaths of files identical in both trees.
        dry_run: True iff no filesystem writes occurred (apply=False).
    """

    created: list[str] = field(default_factory=list)
    overwritten: list[str] = field(default_factory=list)
    skipped_conflicts: list[str] = field(default_factory=list)
    skipped_noops: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def total_writes(self) -> int:
        return len(self.created) + len(self.overwritten)

    def to_audit_mutations(self) -> dict:
        """Return ``build_audit_mutations``-shaped dict for audit-log append.

        CREATEs land in ``files_created``; overwrites land in
        ``files_modified`` (the file existed pre-sync, contents now
        differ — audit-log "modify" is the right op-string).
        """
        return {
            "files_created": list(self.created),
            "files_modified": list(self.overwritten),
            "files_deleted": [],
        }


def _cleanup_orphan_tmp_files(items: list[ScaffoldItem]) -> list[str]:
    """Remove ``<vault_path>.tmp`` orphans left by a previously-crashed sync.

    :func:`_write_file` writes via ``.tmp`` + ``replace`` for atomicity.
    If a previous ``apply_sync`` run crashed between ``tmp.write_bytes``
    and ``tmp.replace`` (process kill, OOM, disk-full at replace-time),
    the ``.tmp`` file lingers in the vault. Without cleanup, those
    orphans accumulate operational debt and confuse vault tooling.

    The sweep walks ONLY the include/exclude-filtered set that the
    current sync already processed — i.e. exactly the locations where
    a prior sync could have crashed leaving an orphan. We don't scan
    arbitrary subtrees; the filter contract is preserved.

    Args:
        items: The same filtered :class:`ScaffoldItem` list that
            :func:`apply_sync` is about to process. Each item's
            ``vault_path`` tells us where a prior tmp could live.

    Returns:
        List of relpaths whose orphan tmp was unlinked. Useful for
        callers that want to attribute the cleanup to operator output.
    """
    removed: list[str] = []
    for item in items:
        tmp_path = item.vault_path.with_suffix(item.vault_path.suffix + ".tmp")
        if tmp_path.exists():
            try:
                tmp_path.unlink()
                log.info(
                    "scaffold.sync.orphan_tmp_removed",
                    path=str(tmp_path),
                    relpath=item.relpath,
                )
                removed.append(item.relpath)
            except OSError as e:
                # Best-effort cleanup — don't block the sync. The
                # operator will see the structured warning and can
                # clean up manually.
                log.warning(
                    "scaffold.sync.orphan_tmp_unlink_failed",
                    path=str(tmp_path),
                    relpath=item.relpath,
                    error=str(e),
                )
    return removed


def apply_sync(
    items: list[ScaffoldItem],
    apply: bool = False,
    force: bool = False,
) -> SyncSummary:
    """Execute (or dry-run) the sync plan over a list of scan items.

    Args:
        items: Output of :func:`scan_scaffold`. Order preserved in
            the summary.
        apply: If False (default), no filesystem writes occur — items
            are categorized into the summary as if writes had happened,
            but ``dry_run=True`` and the vault is untouched. Used to
            surface the diff plan before committing.
        force: If True, CONFLICT items are overwritten instead of
            skipped. Default behavior preserves operator content; force
            is the "I want the scaffold wins" override.

    Returns:
        :class:`SyncSummary` with per-category relpath lists.

    Side effects:
        On ``apply=True`` only, runs a pre-flight cleanup pass over
        ``<vault_path>.tmp`` orphans (from a previously-crashed sync)
        across the same filtered set this call will process. Dry-runs
        do not mutate the filesystem and therefore do not clean.
    """
    # Pre-flight: remove orphan .tmp files from any prior crashed sync.
    # Only runs when we'd actually write — dry-run contract is "no
    # filesystem mutation," which includes cleanup.
    if apply:
        _cleanup_orphan_tmp_files(items)

    summary = SyncSummary(dry_run=not apply)

    for item in items:
        if item.status == SyncStatus.NOOP:
            summary.skipped_noops.append(item.relpath)
            continue
        if item.status == SyncStatus.CREATE:
            if apply:
                _write_file(item.scaffold_path, item.vault_path)
            summary.created.append(item.relpath)
            continue
        if item.status == SyncStatus.CONFLICT:
            if force:
                if apply:
                    _write_file(item.scaffold_path, item.vault_path)
                summary.overwritten.append(item.relpath)
            else:
                summary.skipped_conflicts.append(item.relpath)
            continue

    return summary


def _write_file(src: Path, dst: Path) -> None:
    """Copy ``src`` bytes to ``dst``, creating parent dirs as needed.

    Uses byte-mode read/write to avoid any encoding drift between
    scaffold and vault — same as :func:`_classify` byte-compare. Atomic
    write via .tmp + rename to ensure partial writes don't corrupt an
    existing vault file mid-sync.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.write_bytes(src.read_bytes())
    tmp.replace(dst)
