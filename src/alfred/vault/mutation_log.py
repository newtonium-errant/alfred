"""Session-scoped mutation tracking via JSONL files."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def create_session_file() -> str:
    """Create a temporary JSONL file for mutation logging. Returns the path."""
    fd, path = tempfile.mkstemp(prefix="alfred_vault_", suffix=".jsonl")
    os.close(fd)
    return path


def log_mutation(
    session_path: str | None,
    op: str,
    path: str,
    **extra: str | list[str],
) -> None:
    """Append a mutation entry to the session file."""
    if not session_path:
        return
    entry = {
        "op": op,
        "path": path,
        "ts": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    with open(session_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def read_mutations(session_path: str) -> dict:
    """Read a session file and return {files_created, files_modified, files_deleted}.

    Returns:
        Dict with lists of affected file paths grouped by mutation type.

    TODO (CLI-superset divergence): the CLI helper
    :func:`build_audit_mutations` handles additional op-strings the
    agent-backend session-file format doesn't produce — currently
    ``retype`` (vault/cli.py:cmd_retype), ``promote`` (distiller/cli.py:
    cmd_promote_proposal), and ``discard`` (distiller/cli.py:
    cmd_discard_proposal). These are CLI-only ops; agent backends
    don't emit them into session files so this reader doesn't need
    to handle them. If a future agent backend starts issuing any of
    these ops via the session file, extend the op-strings recognized
    here to match :func:`build_audit_mutations`. Keep the two in
    lockstep — they're two sides of the same op-to-bucket-mapping
    contract.
    """
    created: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []

    path = Path(session_path)
    if not path.exists():
        return {"files_created": created, "files_modified": modified, "files_deleted": deleted}

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        op = entry.get("op", "")
        file_path = entry.get("path", "")
        if op == "create":
            created.append(file_path)
        elif op == "edit":
            modified.append(file_path)
        elif op == "move":
            # Move = delete old + create new
            deleted.append(file_path)
            to_path = entry.get("to", "")
            if to_path:
                created.append(to_path)
        elif op == "delete":
            deleted.append(file_path)

    return {"files_created": created, "files_modified": modified, "files_deleted": deleted}


def build_audit_mutations(op: str, path: str, **extra: str) -> dict:
    """Build the ``{files_created, files_modified, files_deleted}``
    bucket-dict shape for a single op from a CLI command. The result
    is passed straight to :func:`append_to_audit_log` as the
    ``mutations`` argument.

    Lifted to this module (from ``vault/cli.py::_single_mutation_dict``)
    on 2026-05-11 (WARN-3 cure) so the three callers — ``vault/cli.py``
    direct CLI fallback, ``distiller/cli.py::cmd_promote_proposal``,
    ``distiller/cli.py::cmd_discard_proposal`` — share one canonical
    op-to-bucket mapping rather than each maintaining its own. Same
    shape ``read_mutations`` produces for the agent-backend session
    flush path, so audit-log row shapes stay uniform regardless of
    which code path produced the mutation.

    Recognized op-strings:

      * ``"create"`` — file created at ``path``. Maps to one entry
        in ``files_created``.
      * ``"edit"`` — file modified at ``path``. Maps to ``files_modified``.
      * ``"delete"`` — file deleted at ``path``. Maps to ``files_deleted``.
      * ``"move"`` — file relocated from ``path`` to ``extra["to"]``.
        Maps to one ``files_deleted`` (source) + one ``files_created``
        (destination), mirroring ``read_mutations`` line 67-72.
      * ``"retype"`` — record type changed via create-at-target +
        delete-source composite. ``path`` is the source; ``extra
        ["target"]`` is the target. Maps to one ``files_deleted``
        + one ``files_created``. CLI-only op (vault/cli.py:cmd_retype);
        agent backends don't issue retype.
      * ``"promote"`` — proposal moved from inbox to canonical. ``path``
        is the inbox file; ``extra["target"]`` is the canonical target.
        Maps to one ``files_deleted`` + one ``files_created``. CLI-only
        op (distiller/cli.py:cmd_promote_proposal); same bucket shape
        as ``retype`` since both are "convert-via-create-plus-delete"
        composites, kept as separate op-strings so the audit-log row
        detail string distinguishes the operator intent.
      * ``"discard"`` — proposal removed without canonical replacement.
        ``path`` is the inbox file. Maps to one ``files_deleted``.
        CLI-only op (distiller/cli.py:cmd_discard_proposal).

    Unknown op-strings produce an empty dict (no buckets filled). The
    audit-log append is a no-op on an empty bucket set, so passing an
    unknown op silently skips rather than raising — matches the
    fail-soft pattern of the agent-backend flush path. Caller is
    responsible for emitting a warning when an unknown op surfaces.
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
    elif op == "promote":
        # promote-proposal = create canonical + delete inbox.
        deleted.append(path)
        target = str(extra.get("target", ""))
        if target:
            created.append(target)
    elif op == "discard":
        # discard-proposal = delete inbox only.
        deleted.append(path)
    return {
        "files_created": created,
        "files_modified": modified,
        "files_deleted": deleted,
    }


def append_to_audit_log(
    audit_path: str | Path,
    tool: str,
    mutations: dict,
    detail: str = "",
) -> None:
    """Append one JSONL line per affected file to the unified audit log.

    Args:
        audit_path: Path to the audit log file (e.g. data/vault_audit.log).
        tool: Tool name. Known values:
            ``"curator"``, ``"janitor"``, ``"distiller"``, ``"exec"``,
            ``"cli"`` (the catch-all bucket for direct ``alfred vault ...``
            invocations per the issue #64 fix; surfaces in audit-log
            rows so an operator can grep "what mutations came from
            ad-hoc CLI invocations" distinct from per-tool agent
            backends). ``"distiller"`` also covers the promote-proposal
            / discard-proposal CLI paths (2026-05-11), in addition to
            its prior agent-backend usage.
        mutations: Dict as returned by ``read_mutations()`` OR
            ``build_audit_mutations()`` with files_created /
            files_modified / files_deleted lists.
        detail: Correlation info (run_id, sweep_id, inbox filename, etc.).
    """
    ts = datetime.now(timezone.utc).isoformat()
    lines: list[str] = []

    for path in mutations.get("files_created", []):
        lines.append(json.dumps({"ts": ts, "tool": tool, "op": "create", "path": path, "detail": detail}))
    for path in mutations.get("files_modified", []):
        lines.append(json.dumps({"ts": ts, "tool": tool, "op": "modify", "path": path, "detail": detail}))
    for path in mutations.get("files_deleted", []):
        lines.append(json.dumps({"ts": ts, "tool": tool, "op": "delete", "path": path, "detail": detail}))

    if not lines:
        return

    audit = Path(audit_path)
    audit.parent.mkdir(parents=True, exist_ok=True)
    with open(audit, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def cleanup_session_file(session_path: str) -> None:
    """Remove a session file after processing."""
    try:
        Path(session_path).unlink(missing_ok=True)
    except OSError:
        pass
