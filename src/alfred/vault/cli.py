"""CLI subcommand handlers for ``alfred vault``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .context import VaultContext
from .mutation_log import append_to_audit_log, build_audit_mutations, log_mutation
from .ops import VaultError, vault_context, vault_create, vault_delete, vault_edit, vault_list, vault_move, vault_read, vault_search
from .retype import vault_retype
from .scope import ScopeError, check_scope
from .snapshot import SnapshotError, get_status, init_repo, restore_file, take_snapshot

# Module-level holder for the per-call ``VaultContext`` threaded down
# from ``cmd_vault`` via :func:`handle_vault_command`. ``None`` means
# the dispatcher didn't supply one — handlers fall back to
# ``VaultContext.from_env()`` and emit a deprecation log so the V2
# migration cycle can grep for remaining env-only call sites.
#
# Module-level state is intentional here (NOT thread-local) — the CLI
# is single-threaded per-process, the dispatcher runs once at top of
# the call chain, and the alternative (threading a kwarg through every
# subcommand handler signature) would churn every test fixture without
# changing the V1 semantics. V2 may revisit if the handler signatures
# get touched for other reasons. See ``src/alfred/vault/context.py``
# module docstring for the V1 vs V2 split.
_dispatcher_context: VaultContext | None = None


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _ctx() -> VaultContext:
    """Resolve the active vault context for this handler invocation.

    Precedence:
      1. If the dispatcher (``cmd_vault``) threaded a context via
         :func:`handle_vault_command`, return it.
      2. Otherwise fall back to :meth:`VaultContext.from_env` with a
         deprecation log. This is the V1 backward-compat path for
         legacy entry points (direct ``alfred vault ...`` invocation
         outside the dispatcher, test-only module-level imports,
         scripts that call handler functions directly).

    V2 will tighten this once the dispatcher always supplies a
    context.
    """
    if _dispatcher_context is not None:
        return _dispatcher_context
    return VaultContext.from_env(caller="vault.cli._ctx")


def _vault_path() -> Path:
    ctx = _ctx()
    p = ctx.vault_path or ""
    if not p:
        print(json.dumps({"error": "ALFRED_VAULT_PATH not set"}))
        sys.exit(1)
    path = Path(p)
    if not path.is_dir():
        print(json.dumps({"error": f"Vault path does not exist: {p}"}))
        sys.exit(1)
    return path


def _scope() -> str | None:
    return _ctx().scope


def _session() -> str | None:
    return _ctx().session_path


def _audit_log_path() -> str | None:
    """Audit-log destination for direct CLI invocations.

    Threaded down from the top-level ``cmd_vault`` dispatcher
    (``src/alfred/cli.py``) via :class:`VaultContext` (V1 path) or
    read back from the ``ALFRED_VAULT_AUDIT_LOG`` env var (legacy
    fallback). Absent when ``alfred vault ...`` is invoked outside
    the dispatcher (e.g. direct module-level test invocations); in
    that case ``_log_or_audit`` silently no-ops, preserving legacy
    behavior.
    """
    return _ctx().audit_log_path


def _single_mutation_dict(op: str, path: str, **extra: str) -> dict:
    """Thin delegator to :func:`alfred.vault.mutation_log.build_audit_mutations`.

    Kept as a module-local wrapper so existing test imports
    (``tests/test_vault_cli_audit_log.py::TestSingleMutationDict``)
    don't churn — they pinned the bucket-dict shape at this
    location before the WARN-3 lift (2026-05-11). The canonical
    implementation now lives next to ``append_to_audit_log`` in
    ``mutation_log.py`` so the three callers (this file,
    ``distiller/cli.py::cmd_promote_proposal``,
    ``distiller/cli.py::cmd_discard_proposal``) share one op-to-
    bucket mapping.

    New CLI code should import ``build_audit_mutations`` directly
    rather than going through this wrapper.
    """
    return build_audit_mutations(op, path, **extra)


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


def _parse_set_args(set_args: list[str] | None, *, append_mode: bool = False) -> dict:
    """Parse ``field=value`` arguments into a dict.

    ``append_mode`` controls how duplicate keys collapse:

    - ``False`` (the ``--set`` contract, UNCHANGED): last-wins. Two
      ``x=1 x=2`` flags resolve to ``{"x": 2}`` — the final value
      overwrites earlier ones, matching frontmatter set-semantics.
    - ``True`` (the ``--append`` contract): every key accumulates into
      a list in flag order. Two ``related=[[A]] related=[[B]]`` flags
      resolve to ``{"related": ["[[A]]", "[[B]]"]}`` rather than
      collapsing to the last value. This fixes the same-field-collapse
      bug where appending two values to one list field silently dropped
      the first. The ops-layer append loop (``vault_edit``) normalizes
      each value to a list and iterates per-element, so a list here
      appends every element without nesting.
    """
    if not set_args:
        return {}
    result: dict = {}
    for item in set_args:
        if "=" not in item:
            _error(f"Invalid --set format: '{item}'. Expected field=value")
        key, _, value = item.partition("=")
        # Try to parse as JSON for lists/numbers, fall back to string
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            parsed = value
        if append_mode:
            # Accumulate duplicate keys into a list (flag order preserved).
            result.setdefault(key, []).append(parsed)
        else:
            # Last-wins (``--set`` semantics, unchanged).
            result[key] = parsed
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
    # ``append_mode=True`` so repeated ``--append field=value`` flags on
    # the SAME field accumulate into an ordered list instead of last-
    # winning (the same-field-collapse bug). ``--set`` stays last-wins.
    append_fields = _parse_set_args(args.append, append_mode=True)
    # argparse's ``action="append"`` returns ``None`` when the flag is
    # absent (default=None above). Normalise to an empty list so the
    # downstream code can treat it as a list uniformly.
    #
    # ``getattr`` with default-None defends against synthetic
    # ``argparse.Namespace`` instances that don't include the new
    # field (test harnesses that pre-date the 2026-05-28 unset-
    # capability ship). Real argparse-built namespaces always carry
    # the field (registered in ``build_vault_parser``); this is
    # belt-and-suspenders for forward-compat hand-rolled Namespaces.
    unset_fields: list[str] = list(getattr(args, "unset", None) or [])

    # Pre-validate that at least one mutation flag was supplied. Without
    # this gate ``vault_edit`` fail-louds at Layer 1 (ops.py no-op gate,
    # per the Hypatia 2026-05-21 incident — see
    # ``feedback_intentionally_left_blank.md``), but the operator sees
    # a JSON error wrapping the deeper ops-layer message that names
    # programmatic kwargs (``set_fields``, ``body_replace``, …) rather
    # than the CLI flags they actually invoked. Surfacing an actionable
    # CLI-layer message here names the flags the operator forgot,
    # before delegating to the Layer 1 gate.
    if not (
        set_fields or append_fields or unset_fields
        or args.body_append or args.body_stdin
    ):
        _error(
            "no edit specified — pass at least one of --set, --append, "
            "--unset, --body-append, or --body-stdin",
        )

    # Compute the union of frontmatter fields being written OR unset.
    # ``--unset`` rides on the ``edit`` operation: a scope with a
    # field-allowlist gate restricts which fields you can unset to
    # the same set you're permitted to edit. Threading unset names
    # into ``fields_list`` validates both write-side (set/append) and
    # remove-side (unset) field names against the same allowlist in
    # one pass.
    #
    # Body-only writes (--body-append, --body-stdin) are gated
    # separately by ``body_write`` — closes the Q3 body-write loophole
    # where a scope with a narrow field allowlist could bypass it by
    # rewriting the body. ``field_allowlist`` still fails closed when
    # empty for frontmatter-carrying edits, so a pure body-write case
    # must not appear as "no fields" against an allowlist scope. We
    # signal that here by only passing ``fields`` when the caller
    # actually supplied frontmatter keys.
    fields_list = (
        list((set_fields or {}).keys())
        + list((append_fields or {}).keys())
        + unset_fields
    )

    # Detect whether a body write was requested so scope can veto.
    body_write_requested = bool(args.body_stdin or args.body_append)

    # When the caller is doing a pure body write (no --set / --append /
    # --unset), skip the field allowlist check — there are no fields
    # to validate and the allowlist rule would otherwise fail closed
    # on the empty fields list. The body_write gate below still applies.
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
            unset_fields=unset_fields or None,
            body_append=body_append,
        )
        # Audit-log emission: distinguish set/append/body edits from
        # unset operations in the operator-visible op-string. Per
        # 2026-05-28 ratification, ``unset`` is a separate op-kind in
        # the audit log so operators can grep on intent (what got
        # removed) distinctly from what got modified-by-write.
        #
        # When a single CLI call combines both write-side and remove-
        # side mutations, emit BOTH log lines so the audit trail
        # captures each operator intent. The fields list on each
        # entry names exactly the keys touched by that op-kind.
        write_side_fields = (
            list((set_fields or {}).keys())
            + list((append_fields or {}).keys())
        )
        if body_write_requested:
            write_side_fields.append("body")
        # Filter unset_fields to those that actually changed (the
        # ops-layer no-op log already fired for already-absent keys;
        # we don't double-emit a CLI log for them). ``fields_changed``
        # from the result includes only keys that actually mutated.
        actually_unset = [
            k for k in unset_fields if k in result["fields_changed"]
        ]
        if actually_unset:
            _log_or_audit(
                "unset", result["path"],
                fields=actually_unset,
            )
        if write_side_fields:
            _log_or_audit(
                "edit", result["path"],
                fields=write_side_fields,
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
    p.add_argument(
        "--as", dest="as_clinician", default=None, metavar="CLINICIAN",
        help="(STAY-C clinical only) attribute this read to a named clinician in the "
             "PHIA s.63 access log. Omitted → honest 'operator' fallback (never fabricated).")

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
    p.add_argument(
        "--unset",
        action="append",
        metavar="field",
        default=None,
        help=(
            "Remove a frontmatter field entirely (repeatable). Distinct "
            "from --set field=null which keeps the key with a null value. "
            "Refuses to remove REQUIRED_FIELDS; emits an info log on "
            "already-absent fields (idempotent no-op)."
        ),
    )
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


def handle_vault_command(
    args: argparse.Namespace,
    *,
    vault_context: VaultContext | None = None,
) -> None:
    """Dispatch to the correct vault subcommand handler.

    ``vault_context`` is the V1 typed thread-through replacing
    ``os.environ`` injection by ``cmd_vault``. When supplied, all
    downstream subcommand handlers see it via :func:`_ctx`. When
    ``None`` (legacy entry point), handlers fall back to
    :meth:`VaultContext.from_env` with a deprecation log. The
    dispatcher still writes the env vars during V1 for cross-process
    safety, so the fallback resolves correctly even in legacy
    invocation paths.
    """
    global _dispatcher_context
    prev_ctx = _dispatcher_context
    _dispatcher_context = vault_context
    try:
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
    finally:
        # Restore prior context so nested invocations (test harnesses,
        # repeated dispatches in same process) don't see stale state.
        _dispatcher_context = prev_ctx
