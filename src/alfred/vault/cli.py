"""CLI subcommand handlers for ``alfred vault``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .mutation_log import append_to_audit_log, log_mutation
from .ops import VaultError, vault_context, vault_create, vault_delete, vault_edit, vault_list, vault_move, vault_read, vault_search
from .retype import vault_retype
from .scope import ScopeError, check_scope
from .snapshot import SnapshotError, get_status, init_repo, restore_file, take_snapshot


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _vault_path() -> Path:
    p = _env("ALFRED_VAULT_PATH")
    if not p:
        print(json.dumps({"error": "ALFRED_VAULT_PATH not set"}))
        sys.exit(1)
    path = Path(p)
    if not path.is_dir():
        print(json.dumps({"error": f"Vault path does not exist: {p}"}))
        sys.exit(1)
    return path


def _scope() -> str | None:
    return _env("ALFRED_VAULT_SCOPE") or None


def _session() -> str | None:
    return _env("ALFRED_VAULT_SESSION") or None


def _audit_log_path() -> str | None:
    """Audit-log destination for direct CLI invocations.

    Set by the top-level ``cmd_vault`` dispatcher
    (``src/alfred/cli.py``) to the per-instance ``logging.dir /
    vault_audit.log``. Absent when ``alfred vault ...`` is invoked
    outside the dispatcher (e.g. direct module-level test
    invocations); in that case ``_log_or_audit`` silently no-ops,
    preserving legacy behavior.
    """
    return _env("ALFRED_VAULT_AUDIT_LOG") or None


def _single_mutation_dict(op: str, path: str, **extra: str) -> dict:
    """Build the ``{files_created, files_modified, files_deleted}``
    shape from one mutation. Mirrors
    ``alfred.vault.mutation_log.read_mutations`` exactly so the CLI
    fallback and the agent-backend flush path stay in lockstep —
    same mapping of op-strings to bucket lists, same handling of
    move's old/new path split.

    Move semantics: a single move op is recorded as one delete (old
    path) + one create (new path), matching ``read_mutations`` line
    67-72. Caller must pass ``to=<new_path>`` in ``extra`` so the
    create side has a target.

    Retype semantics: retype is a create-at-target + delete-source
    composite (see ``vault_retype``). The CLI passes
    ``target=<target_path>`` in ``extra`` so we record both sides.
    ``read_mutations`` doesn't know about retype because the agent
    backends don't issue retype ops — only direct CLI does. This is
    the one place CLI's op-set is a superset of the session-file
    op-set; keeping the divergence here (not in mutation_log)
    avoids polluting the session-file format with a CLI-only op.

    Unknown op-strings produce an empty dict (no buckets filled) +
    are silently skipped by the audit-log append.
    """
    created: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    if op == "create":
        created.append(path)
    elif op == "edit":
        modified.append(path)
    elif op == "move":
        deleted.append(path)
        to_path = str(extra.get("to", ""))
        if to_path:
            created.append(to_path)
    elif op == "delete":
        deleted.append(path)
    elif op == "retype":
        # retype = create new target + delete source (composite).
        deleted.append(path)
        target = str(extra.get("target", ""))
        if target:
            created.append(target)
    return {
        "files_created": created,
        "files_modified": modified,
        "files_deleted": deleted,
    }


def _log_or_audit(op: str, path: str, **extra: str | list[str]) -> None:
    """Record a single mutation — session file when under agent
    context, audit log when invoked directly via CLI.

    Per-instance audit-log path comes from the
    ``ALFRED_VAULT_AUDIT_LOG`` env var set by ``cmd_vault``
    (mirrors ``logging.dir`` convention). Absent env var = no audit
    context = silent no-op (preserves legacy behavior for callers
    outside the dispatcher).

    Precedence: when ``ALFRED_VAULT_SESSION`` is set, the agent
    backend will collect the session file and flush it to the
    audit log at wrap-up time — so the CLI must NOT also write to
    the audit log here, or the mutation would be double-counted.
    The session-file path takes precedence; audit-log fallback
    only fires when no session is active.

    Issue #64 (2026-05-10): direct ``alfred --config <c> vault ...``
    invocations silently bypassed the audit log for ~10 days of
    operator workflow because ``log_mutation`` early-returned on
    missing session. This helper closes the gap.
    """
    session = _session()
    if session:
        log_mutation(session, op, path, **extra)
        return
    audit_path = _audit_log_path()
    if not audit_path:
        return
    # str-only extras for _single_mutation_dict (list values like
    # "fields" from edit are session-file-only diagnostics; the
    # audit log doesn't carry them).
    str_extras: dict[str, str] = {
        k: v for k, v in extra.items() if isinstance(v, str)
    }
    mutations = _single_mutation_dict(op, path, **str_extras)
    append_to_audit_log(audit_path, "cli", mutations, detail=f"vault {op} via CLI")


def _ignore_dirs() -> list[str]:
    """Standard ignore dirs for search/list operations."""
    return ["_templates", "_bases", "_docs", ".obsidian"]


def _output(data: dict) -> None:
    print(json.dumps(data, default=str))


def _error(msg: str, code: int = 1, details: dict | None = None) -> None:
    payload: dict = {"error": msg}
    if details:
        payload["details"] = details
    print(json.dumps(payload, default=str))
    sys.exit(code)


def _parse_set_args(set_args: list[str] | None) -> dict:
    """Parse --set field=value arguments into a dict."""
    if not set_args:
        return {}
    result: dict = {}
    for item in set_args:
        if "=" not in item:
            _error(f"Invalid --set format: '{item}'. Expected field=value")
        key, _, value = item.partition("=")
        # Try to parse as JSON for lists/numbers, fall back to string
        try:
            result[key] = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            result[key] = value
    return result


# --- Subcommand handlers ---


def cmd_read(args: argparse.Namespace) -> None:
    scope = _scope()
    try:
        check_scope(scope, "read", rel_path=args.path)
    except ScopeError as e:
        _error(str(e))

    vault = _vault_path()
    try:
        result = vault_read(vault, args.path)
        _output(result)
    except VaultError as e:
        _error(str(e))


def cmd_search(args: argparse.Namespace) -> None:
    scope = _scope()
    try:
        check_scope(scope, "search")
    except ScopeError as e:
        _error(str(e))

    vault = _vault_path()
    try:
        results = vault_search(
            vault,
            glob_pattern=args.glob,
            grep_pattern=args.grep,
            ignore_dirs=_ignore_dirs(),
        )
        _output({"results": results, "count": len(results)})
    except VaultError as e:
        _error(str(e))


def cmd_list(args: argparse.Namespace) -> None:
    scope = _scope()
    try:
        check_scope(scope, "list")
    except ScopeError as e:
        _error(str(e))

    vault = _vault_path()
    try:
        results = vault_list(
            vault, args.type,
            ignore_dirs=_ignore_dirs(),
            scope=scope,
        )
        _output({"results": results, "count": len(results)})
    except VaultError as e:
        _error(str(e))


def cmd_context(args: argparse.Namespace) -> None:
    scope = _scope()
    try:
        check_scope(scope, "context")
    except ScopeError as e:
        _error(str(e))

    vault = _vault_path()
    result = vault_context(vault, ignore_dirs=_ignore_dirs())
    _output(result)


def cmd_create(args: argparse.Namespace) -> None:
    scope = _scope()
    vault = _vault_path()
    set_fields = _parse_set_args(args.set)

    # Body-write gate for create mirrors the edit path: scopes with
    # ``allow_body_writes: False`` must not be able to set a custom
    # body via --body-stdin. Janitor triage-task creation flows through
    # here too and never uses --body-stdin, so the gate is inert for
    # the expected happy path and only fires on misuse.
    body_write_requested = bool(args.body_stdin)

    try:
        check_scope(
            scope,
            "create",
            record_type=args.type,
            frontmatter=set_fields,
            body_write=body_write_requested,
        )
    except ScopeError as e:
        _error(str(e))

    body = None
    if args.body_stdin:
        body = sys.stdin.read()

    try:
        result = vault_create(
            vault, args.type, args.name,
            set_fields=set_fields, body=body,
            scope=scope,
        )
        _log_or_audit("create", result["path"])
        _output(result)
    except VaultError as e:
        _error(str(e), details=getattr(e, "details", None))


def cmd_edit(args: argparse.Namespace) -> None:
    scope = _scope()
    vault = _vault_path()
    set_fields = _parse_set_args(args.set)
    append_fields = _parse_set_args(args.append)

    # Compute the union of frontmatter fields being written. Body-only
    # writes (--body-append, --body-stdin) are gated separately by
    # ``body_write`` — closes the Q3 body-write loophole where a scope
    # with a narrow field allowlist could bypass it by rewriting the
    # body. ``field_allowlist`` still fails closed when empty for
    # frontmatter-carrying edits, so a pure body-write case must not
    # appear as "no fields" against an allowlist scope. We signal that
    # here by only passing ``fields`` when the caller actually supplied
    # frontmatter keys.
    fields_list = list((set_fields or {}).keys()) + list((append_fields or {}).keys())

    # Detect whether a body write was requested so scope can veto.
    body_write_requested = bool(args.body_stdin or args.body_append)

    # When the caller is doing a pure body write (no --set / --append),
    # skip the field allowlist check — there are no fields to validate
    # and the allowlist rule would otherwise fail closed on the empty
    # fields list. The body_write gate below still applies.
    try:
        if fields_list:
            check_scope(
                scope, "edit",
                rel_path=args.path,
                fields=fields_list,
                body_write=body_write_requested,
            )
        else:
            # Frontmatter untouched — validate the operation itself and
            # the body-write gate only. Pass an empty fields list so
            # ``field_allowlist`` scopes that require fields won't fail
            # closed when the caller is legitimately doing a body-only
            # edit on a scope that allows body writes.
            check_scope(
                scope, "edit",
                rel_path=args.path,
                fields=[],
                body_write=body_write_requested,
            )
    except ScopeError as e:
        _error(str(e))

    body_append = None
    if args.body_stdin:
        body_append = sys.stdin.read()
    elif args.body_append:
        body_append = args.body_append

    try:
        result = vault_edit(
            vault, args.path,
            set_fields=set_fields or None,
            append_fields=append_fields or None,
            body_append=body_append,
        )
        _log_or_audit(
            "edit", result["path"],
            fields=result["fields_changed"],
        )
        _output(result)
    except VaultError as e:
        _error(str(e))


def cmd_move(args: argparse.Namespace) -> None:
    scope = _scope()
    try:
        check_scope(scope, "move", rel_path=args.source)
    except ScopeError as e:
        _error(str(e))

    vault = _vault_path()
    try:
        result = vault_move(vault, args.source, args.dest)
        _log_or_audit(
            "move", result["from"],
            to=result["to"],
        )
        _output(result)
    except VaultError as e:
        _error(str(e))


def cmd_retype(args: argparse.Namespace) -> None:
    """Convert a vault record from one type to another.

    Composes ``vault_retype`` (writes new record at target path,
    rewrites vault-wide wikilinks, deletes source unless
    ``--keep-source``). The source delete fires the registered
    event-delete hook → triggers GCal cleanup automatically when the
    source had a ``gcal_event_id`` and the target type is non-event.

    Default is to apply; pass ``--dry-run`` to preview without
    touching the vault.
    """
    scope = _scope()
    vault = _vault_path()

    overrides: dict = {}
    if getattr(args, "status", None):
        overrides["status"] = args.status
    if getattr(args, "priority", None):
        overrides["priority"] = args.priority
    if getattr(args, "due", None):
        overrides["due"] = args.due

    try:
        report = vault_retype(
            vault,
            args.path,
            args.to,
            apply=not getattr(args, "dry_run", False),
            keep_source=getattr(args, "keep_source", False),
            overrides=overrides,
            scope=scope,
        )
    except VaultError as exc:
        details = getattr(exc, "details", None) or {}
        _error(str(exc), details=details)

    if not getattr(args, "dry_run", False):
        _log_or_audit(
            "retype", args.path,
            target=report.target_path,
            target_type=report.target_type,
        )

    _output(report.to_dict())


def cmd_delete(args: argparse.Namespace) -> None:
    scope = _scope()
    try:
        check_scope(scope, "delete", rel_path=args.path)
    except ScopeError as e:
        _error(str(e))

    vault = _vault_path()
    try:
        result = vault_delete(vault, args.path)
        _log_or_audit("delete", result["path"])
        _output(result)
    except VaultError as e:
        _error(str(e))


def cmd_triage_id(args: argparse.Namespace) -> None:
    """Compute a deterministic triage id for a candidate set.

    Order-independent: the same candidates in any permutation yield the
    same id. Used by the janitor agent when creating Layer 3 triage tasks
    (see ``alfred.janitor.triage``).
    """
    from alfred.janitor.triage import compute_triage_id

    try:
        triage_id = compute_triage_id(args.kind, list(args.candidates))
    except ValueError as e:
        _error(str(e))
        return
    _output({"triage_id": triage_id, "kind": args.kind, "candidates": list(args.candidates)})


def cmd_snapshot(args: argparse.Namespace) -> None:
    vault = _vault_path()

    try:
        if args.init:
            commit_hash = init_repo(vault)
            _output({"ok": True, "action": "init", "commit": commit_hash})
        elif args.status:
            status = get_status(vault)
            _output(status)
        elif args.restore:
            restored_from = restore_file(vault, args.restore)
            _output({"ok": True, "action": "restore", "path": args.restore, "from_commit": restored_from})
        else:
            commit_hash = take_snapshot(vault)
            if commit_hash:
                _output({"ok": True, "action": "snapshot", "commit": commit_hash})
            else:
                _output({"ok": True, "action": "snapshot", "commit": None, "message": "Nothing to commit"})
    except SnapshotError as e:
        _error(str(e))


# --- Parser builder ---


def build_vault_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register ``alfred vault`` subcommands on the given subparsers action."""
    vault = subparsers.add_parser("vault", help="Vault operations (mediated file access)")
    vault_sub = vault.add_subparsers(dest="vault_cmd")

    # read
    p = vault_sub.add_parser("read", help="Read a vault record")
    p.add_argument("path", help="Relative path to the record (e.g. person/John Smith.md)")

    # search
    p = vault_sub.add_parser("search", help="Search vault files")
    p.add_argument("--glob", default=None, help="Glob pattern (e.g. 'person/*.md')")
    p.add_argument("--grep", default=None, help="Regex to search file contents")

    # list
    p = vault_sub.add_parser("list", help="List records by type")
    p.add_argument("type", help="Record type (e.g. person, task, project)")

    # context
    vault_sub.add_parser("context", help="Compact vault summary")

    # create
    p = vault_sub.add_parser("create", help="Create a new vault record")
    p.add_argument("type", help="Record type")
    p.add_argument("name", help="Record name/title")
    p.add_argument("--set", action="append", metavar="field=value", help="Set a frontmatter field")
    p.add_argument("--body-stdin", action="store_true", help="Read body content from stdin")

    # edit
    p = vault_sub.add_parser("edit", help="Edit a vault record")
    p.add_argument("path", help="Relative path to the record")
    p.add_argument("--set", action="append", metavar="field=value", help="Set a frontmatter field")
    p.add_argument("--append", action="append", metavar="field=value", help="Append to a list field")
    p.add_argument("--body-append", default=None, help="Text to append to body")
    p.add_argument("--body-stdin", action="store_true", help="Read body append content from stdin")

    # move
    p = vault_sub.add_parser("move", help="Move a vault record")
    p.add_argument("source", help="Source relative path")
    p.add_argument("dest", help="Destination relative path")

    # delete
    p = vault_sub.add_parser("delete", help="Delete a vault record")
    p.add_argument("path", help="Relative path to the record")

    # retype
    p = vault_sub.add_parser(
        "retype",
        help="Convert a vault record from one type to another",
    )
    p.add_argument("path", help="Relative path to the source record")
    p.add_argument(
        "--to", required=True, dest="to",
        help="Target record type (e.g. task)",
    )
    p.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Report what would happen without touching the vault",
    )
    p.add_argument(
        "--keep-source", action="store_true", default=False,
        help=(
            "Leave the source record on disk after creating the target. "
            "Default is to delete the source (which fires the GCal "
            "delete hook for events). Useful for safety-checking the "
            "target record before committing to the deletion."
        ),
    )
    # Per-target-type overrides — currently only task. As future
    # type-pairs land, document additional overrides here.
    p.add_argument(
        "--status", default=None,
        help=(
            "Override the target's ``status`` field. For task target, "
            "default is ``todo``. Must be one of the target type's "
            "valid statuses (see STATUS_BY_TYPE in vault/schema.py)."
        ),
    )
    p.add_argument(
        "--priority", default=None,
        help=(
            "Override the target's ``priority`` field (task target only). "
            "Default is ``medium`` (matches scaffold/_templates/task.md)."
        ),
    )
    p.add_argument(
        "--due", default=None,
        help=(
            "Override the target's ``due`` field (task target only). "
            "Defaults to the source's ``date`` field if present."
        ),
    )

    # triage-id
    p = vault_sub.add_parser(
        "triage-id",
        help="Compute deterministic triage id for a candidate set",
    )
    p.add_argument("kind", help="Triage kind (e.g. dedup, orphan)")
    p.add_argument(
        "candidates",
        nargs="+",
        help="Candidate paths or wikilinks (order-independent)",
    )

    # snapshot
    p = vault_sub.add_parser("snapshot", help="Git snapshot of vault state")
    p.add_argument("--init", action="store_true", help="Initialize vault git repo")
    p.add_argument("--status", action="store_true", help="Show snapshot status")
    p.add_argument("--restore", metavar="PATH", default=None, help="Restore a file from previous commit")


def handle_vault_command(args: argparse.Namespace) -> None:
    """Dispatch to the correct vault subcommand handler."""
    handlers = {
        "read": cmd_read,
        "search": cmd_search,
        "list": cmd_list,
        "context": cmd_context,
        "create": cmd_create,
        "edit": cmd_edit,
        "move": cmd_move,
        "delete": cmd_delete,
        "retype": cmd_retype,
        "triage-id": cmd_triage_id,
        "snapshot": cmd_snapshot,
    }
    handler = handlers.get(args.vault_cmd)
    if handler:
        handler(args)
    else:
        print(json.dumps({"error": f"Unknown vault subcommand: {args.vault_cmd}"}))
        sys.exit(1)
