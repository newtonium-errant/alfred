"""``alfred scaffold ...`` CLI subcommands.

Currently exposes one subcommand:

* ``sync`` — diff bundled scaffold vs vault, print plan, optionally apply.

Subparser shape mirrors :mod:`alfred.audit.cli`: a top-level ``scaffold``
parser with a ``scaffold_cmd`` dest, dispatched from :mod:`alfred.cli`
via :func:`cmd_scaffold`.

The CLI module is intentionally thin — it parses args, calls
:func:`alfred.scaffold.sync.scan_scaffold` + :func:`apply_sync`, prints
a structured plan/summary, and (when applying) appends to the unified
vault audit log via the canonical :func:`alfred.vault.mutation_log.
append_to_audit_log` helper with ``tool="scaffold"``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from alfred._data import get_scaffold_dir
from alfred.scaffold.config import load_from_unified
from alfred.scaffold.sync import (
    DEFAULT_EXCLUDE,
    DEFAULT_INCLUDE,
    ScaffoldItem,
    SyncStatus,
    apply_sync,
    scan_scaffold,
)

if TYPE_CHECKING:
    from alfred.vault.context import VaultContext

log = structlog.get_logger(__name__)


def build_parser(parent_subparsers: argparse._SubParsersAction) -> None:
    """Register ``alfred scaffold ...`` with the top-level CLI parser.

    Called from :mod:`alfred.cli` during ``build_parser``.
    """
    scaffold_p = parent_subparsers.add_parser(
        "scaffold",
        help="Bundled scaffold sync into existing instance vaults",
    )
    scaffold_sub = scaffold_p.add_subparsers(dest="scaffold_cmd")

    sync_p = scaffold_sub.add_parser(
        "sync",
        help=(
            "Diff bundled scaffold against the configured vault and "
            "create missing files. Defaults to dry-run; use --apply "
            "to write."
        ),
    )
    sync_p.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write the plan (default is dry-run).",
    )
    sync_p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "Explicit dry-run (default behavior; flag for symmetry with "
            "--apply). If both are passed, --dry-run wins."
        ),
    )
    sync_p.add_argument(
        "--include",
        default=None,
        help=(
            "Comma-separated path-prefixes to include. FULL OVERRIDE of "
            "the default include set (NOT a merge) — pass the complete "
            "list you want. If you pass --include _templates alone you "
            "will lose _bases, view, and the top-level docs in this run. "
            f"Defaults: {','.join(DEFAULT_INCLUDE)}. Example: "
            "--include _templates,_bases,.obsidian"
        ),
    )
    sync_p.add_argument(
        "--exclude",
        default=None,
        help=(
            "Comma-separated path-prefixes to exclude. FULL OVERRIDE of "
            "the default exclude set (NOT a merge) — pass the complete "
            "list you want. If you pass --exclude .gitkeep alone you "
            "will lose the .obsidian exclude in this run. "
            f"Defaults: {','.join(DEFAULT_EXCLUDE)}."
        ),
    )
    sync_p.add_argument(
        "--force",
        action="store_true",
        default=False,
        help=(
            "Overwrite CONFLICT files (vault file exists with different "
            "content). Default is skip — operator content is preserved."
        ),
    )
    sync_p.add_argument(
        "--vault-path",
        default=None,
        help=(
            "Override the vault root path. Default: read vault.path "
            "from config.yaml."
        ),
    )


def dispatch(
    args: argparse.Namespace,
    raw_config: dict[str, Any],
    *,
    vault_context: "VaultContext | None" = None,
) -> int:
    """Dispatch the right ``scaffold`` subcommand. Returns exit code.

    Args:
        args: Parsed argparse namespace.
        raw_config: The unified config dict (passed through so the
            handler can resolve ``vault.path`` and ``logging.dir``
            without re-parsing). Empty dict acceptable if the caller
            couldn't load config; ``--vault-path`` is the manual escape.
        vault_context: Typed dispatcher-state from
            :func:`alfred.cli.cmd_scaffold`. When supplied the
            audit-log path is read from this object instead of from
            ``ALFRED_VAULT_AUDIT_LOG``. None → legacy env-fallback
            (V1 backward-compat).
    """
    sub = getattr(args, "scaffold_cmd", None)
    if sub == "sync":
        return cmd_sync(args, raw_config, vault_context=vault_context)
    print("usage: alfred scaffold sync [--apply] [--include ...] [--exclude ...] [--force]")
    return 1


def _resolve_filter(
    *,
    cli_value: str | None,
    config_value: list[str] | None,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    """Apply the 3-layer include/exclude precedence (CLI > config > default).

    Args:
        cli_value: The ``--include`` / ``--exclude`` argparse value
            (comma-separated string, or ``None`` when the flag wasn't
            passed). ``None`` is distinct from an empty string — the
            flag with an empty arg is a malformed invocation that
            collapses to the empty-set via the comma-split below.
        config_value: The per-instance config value (list of strings,
            or ``None`` when ``scaffold.include`` / ``scaffold.exclude``
            wasn't set in the config block). ``None`` here means "fall
            through to module default"; an explicit empty list ``[]``
            means "operator intentionally zeroed out the filter."
        default: The module-level fallback (always Salem-shape).

    Returns:
        Tuple of path-prefixes (frozen, hashable) for the layer that
        won the precedence race.
    """
    # Layer 1 — CLI wins everything. Empty / whitespace-only CLI values
    # fall through (treated as "no flag passed") rather than zeroing
    # out the filter — the empty-list opt-out path lives in the config
    # layer, not the CLI layer. Operator who wants the empty filter
    # explicitly should set ``scaffold.include: []`` in their config.
    if cli_value is not None:
        items = tuple(p.strip() for p in cli_value.split(",") if p.strip())
        if items:
            return items
    # Layer 2 — config-set value (including explicit empty list).
    if config_value is not None:
        return tuple(config_value)
    # Layer 3 — module-level fallback.
    return default


def cmd_sync(
    args: argparse.Namespace,
    raw_config: dict[str, Any],
    *,
    vault_context: "VaultContext | None" = None,
) -> int:
    """Execute ``alfred scaffold sync``. Returns exit code (0 OK, 1 error).

    ``vault_context`` carries the audit-log path resolved by the
    dispatcher (V1 typed thread-through). When None, falls back to
    :meth:`VaultContext.from_env` with deprecation log.
    """
    # Resolve vault dir. --vault-path wins over config; config is
    # required iff --vault-path absent.
    vault_path_arg = getattr(args, "vault_path", None)
    if vault_path_arg:
        vault_dir = Path(vault_path_arg).expanduser().resolve()
    else:
        vault_cfg = raw_config.get("vault", {}) or {}
        vault_path_str = vault_cfg.get("path")
        if not vault_path_str:
            print(
                "vault.path not set in config and --vault-path not given. "
                "Pass --vault-path /abs/path/to/vault or set vault.path "
                "in config.yaml.",
                file=sys.stderr,
            )
            return 1
        vault_dir = Path(vault_path_str).expanduser().resolve()

    # Resolve include/exclude via 3-layer precedence:
    #
    #   1. CLI flag (``--include`` / ``--exclude``) — operator override,
    #      highest precedence. Wins everything.
    #   2. Per-instance config (``scaffold.include`` / ``scaffold.exclude``)
    #      — the per-vault default. KAL-LE and Hypatia use a trimmed
    #      include here to avoid the Salem-shape default writing 50+
    #      dead files into canonical-curation / knowledge-work vaults.
    #   3. Module-level default (Salem-shape) — final fallback when
    #      neither CLI nor config supplied a value.
    #
    # Empty-list semantics: a config (or CLI flag) that explicitly
    # supplies an empty list is honored — operator can intentionally
    # zero out the include set via ``scaffold.include: []`` to opt out
    # of all auto-sync without unsetting the block. ``no_candidates``
    # empty-state signal will fire downstream so idle is distinguishable
    # from broken (per ``feedback_intentionally_left_blank.md``).
    scaffold_cfg = load_from_unified(raw_config)
    include = _resolve_filter(
        cli_value=args.include,
        config_value=scaffold_cfg.include,
        default=DEFAULT_INCLUDE,
    )
    exclude = _resolve_filter(
        cli_value=args.exclude,
        config_value=scaffold_cfg.exclude,
        default=DEFAULT_EXCLUDE,
    )

    # --dry-run wins over --apply if both passed; argparse can't express
    # mutual exclusion across two store_true flags cleanly, so we do it
    # here with explicit precedence.
    apply_writes = bool(args.apply) and not bool(args.dry_run)

    scaffold_dir = get_scaffold_dir()

    log.info(
        "scaffold.sync.start",
        scaffold_dir=str(scaffold_dir),
        vault_dir=str(vault_dir),
        include=list(include),
        exclude=list(exclude),
        apply=apply_writes,
        force=bool(args.force),
    )

    try:
        items = scan_scaffold(scaffold_dir, vault_dir, include=include, exclude=exclude)
    except FileNotFoundError as e:
        print(f"Scan failed: {e}", file=sys.stderr)
        log.error("scaffold.sync.scan_failed", error=str(e))
        return 1

    # Intentionally-left-blank: empty scan must surface as an explicit
    # "ran, nothing to do" line so an operator can tell idle apart from
    # broken. See feedback_intentionally_left_blank.md.
    if not items:
        msg = (
            "[scaffold sync] No candidate files matched include/exclude filters. "
            "Nothing to do.\n"
            f"  scaffold_dir: {scaffold_dir}\n"
            f"  vault_dir:    {vault_dir}\n"
            f"  include:      {','.join(include)}\n"
            f"  exclude:      {','.join(exclude)}"
        )
        print(msg)
        log.info("scaffold.sync.no_candidates", scaffold_dir=str(scaffold_dir), vault_dir=str(vault_dir))
        return 0

    summary = apply_sync(items, apply=apply_writes, force=bool(args.force))

    # Build per-status counts for the headline.
    n_create = len(summary.created)
    n_overwrite = len(summary.overwritten)
    n_conflict_skipped = len(summary.skipped_conflicts)
    n_noop = len(summary.skipped_noops)

    mode = "DRY-RUN" if summary.dry_run else "APPLIED"
    print(
        f"[scaffold sync] {mode}: "
        f"{n_create} create, {n_overwrite} overwrite, "
        f"{n_conflict_skipped} conflict-skipped, {n_noop} noop"
    )

    # Orphan tmp cleanup signal — unconditional on --apply, even when N=0.
    # That's the "intentionally left blank" contract: silence is
    # indistinguishable from broken, so the operator should be able to
    # tell from stdout whether the pre-flight cleanup ran. Dry-run
    # suppresses the line because cleanup didn't run (no-mutation
    # contract); printing "0 cleaned" on dry-run would imply the sweep
    # happened, which it didn't.
    if not summary.dry_run:
        print(f"Orphan tmp files cleaned: {len(summary.orphans_cleaned)}")

    # Per-file detail. Group by status; sort within each group for
    # stable output (helps diff-of-diffs across reruns).
    if summary.created:
        print("\nCREATE:")
        for rp in summary.created:
            print(f"  + {rp}")
    if summary.overwritten:
        print("\nOVERWRITE (--force):")
        for rp in summary.overwritten:
            print(f"  ~ {rp}")
    if summary.skipped_conflicts:
        print("\nCONFLICT (skipped; pass --force to overwrite):")
        for rp in summary.skipped_conflicts:
            print(f"  ! {rp}")

    # Audit-log append on actual writes only. Dry-run produces no
    # mutation rows — the audit log records what the FILESYSTEM did,
    # not what the planner predicted.
    if apply_writes and summary.total_writes > 0:
        try:
            from alfred.vault.mutation_log import append_to_audit_log

            # V1 of env-var → function-arg refactor: prefer typed
            # ``vault_context.audit_log_path``, fall back to
            # ``VaultContext.from_env()`` (with deprecation log)
            # when dispatcher didn't supply one.
            if vault_context is not None:
                audit_path = vault_context.audit_log_path
            else:
                from alfred.vault.context import VaultContext as _VC
                audit_path = _VC.from_env(
                    caller="scaffold.cli.cmd_sync",
                ).audit_log_path

            if audit_path:
                detail = (
                    f"include={','.join(include)} "
                    f"exclude={','.join(exclude)} "
                    f"force={bool(args.force)}"
                )
                append_to_audit_log(
                    audit_path=audit_path,
                    tool="scaffold",
                    mutations=summary.to_audit_mutations(),
                    detail=detail,
                )
                log.info(
                    "scaffold.sync.audit_appended",
                    audit_path=audit_path,
                    files_created=n_create,
                    files_modified=n_overwrite,
                )
            else:
                # Without the env var we have no resolved audit-log
                # path. Don't guess — surface and skip. Mirrors the
                # vault/cli.py CLI-fallback behavior.
                log.warning(
                    "scaffold.sync.audit_skipped_no_env",
                    reason="ALFRED_VAULT_AUDIT_LOG not set",
                )
        except Exception as e:
            # Audit-log append is best-effort; never let it block the
            # sync's success exit code. Surface via structured log so
            # an operator grep finds it.
            log.warning("scaffold.sync.audit_failed", error=str(e))

    if summary.dry_run:
        print("\nRe-run with --apply to write the plan.")

    log.info(
        "scaffold.sync.complete",
        dry_run=summary.dry_run,
        created=n_create,
        overwritten=n_overwrite,
        conflict_skipped=n_conflict_skipped,
        noop=n_noop,
    )
    return 0


__all__ = ["build_parser", "dispatch", "cmd_sync"]
