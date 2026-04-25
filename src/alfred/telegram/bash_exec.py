"""SAFETY-CRITICAL: shell command executor for KAL-LE's ``bash_exec`` tool.

Every safety invariant lives in this module. The tool schema in
``conversation.py`` is the LLM-facing contract; this file is the
runtime enforcer. **If you're editing this file, re-read
``src/alfred/_bundled/skills/vault-kalle/SKILL.md``'s capability
section first — the two must stay in sync.**

## Enforced invariants (unit-tested)

1. Command is split via :func:`shlex.split` and executed with
   :func:`asyncio.create_subprocess_exec` — NOT ``shell=True``. No
   shell expansion, no pipes, no ``$()``, no redirects.
2. First-token allowlist. Only commands whose argv[0] is in
   :data:`_ALLOWLIST` (or a specific ``git`` subcommand in
   :data:`_GIT_SUBCOMMAND_ALLOWLIST`) execute. Everything else
   rejects with ``reason="command_not_allowlisted"``.
3. Full-command denylist. Substrings like ``git push``, ``rm -rf``,
   ``curl | sh``, ``chmod``, ``sudo``, ``wget`` reject regardless of
   allowlist. Checked BEFORE allowlist so a false-positive allowlist
   hit can't bypass a denylist hit.
4. Destructive-keyword dry-run gate. Commands containing ``rm -r``,
   ``git reset --hard``, ``truncate``, etc. force ``dry_run=True``.
5. ``cwd`` must resolve (after ``Path.resolve()``) under one of the
   four allowed repo roots: ``~/aftermath-lab``, ``~/aftermath-alfred``,
   ``~/aftermath-rrts``, ``~/alfred``. ``..`` escapes, ``/``, ``$HOME``,
   ``/tmp`` all reject.
6. 300s timeout. Over-runs terminate the subprocess and return
   ``exit_code=-1`` with ``reason="timeout"``.
7. stdout/stderr truncated to 10 KB each. ``truncated: True`` in the
   response when either stream was clipped.
8. Every invocation — successful, rejected, or timed-out — appends one
   JSONL line to ``<data_dir>/bash_exec.jsonl``. No stdout/stderr in
   the audit (too noisy). Command, cwd, exit_code, duration_ms,
   session_id only.

## Tests

See ``tests/test_bash_exec.py``. Every denylist item MUST appear in a
test that fails if the command slips through. Never remove a denylist
test without adding one that covers the same attack vector.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Allowlists + denylists
# ---------------------------------------------------------------------------


# First-token allowlist. A command's argv[0] MUST be in this set or a
# specific git subcommand in the next list. ``python3`` / ``node`` are
# listed so KAL-LE can run scripts, but the denylists below still block
# the common abuse patterns (network egress, installs, etc.).
_ALLOWLIST: frozenset[str] = frozenset({
    # Test runners
    "pytest", "jest", "mocha", "vitest",
    # Build / lint / type
    "npm", "yarn", "tsc", "mypy", "ruff", "black",
    "eslint", "prettier",
    # Python / Node runners
    "python", "python3", "node",
    # Search + inspection
    "grep", "rg", "find", "ls", "cat", "head", "tail",
    "wc", "diff", "file", "stat", "sort", "uniq", "awk", "sed",
    # git — subcommand-gated below
    "git",
    # alfred — subcommand-gated below; admits a curated set of read +
    # write surfaces (reviews, digest, vault read, transport propose-person)
    # so KAL-LE can drive them without escaping bash_exec.
    "alfred",
})


# Only these git subcommands are allowed. NEVER add commit/push/rebase/
# reset/merge/clean/rm to this list — Andrew retains commit authority.
# Unit test ``test_bash_exec_denies_git_commit_push_etc`` asserts this
# set stays tight.
_GIT_SUBCOMMAND_ALLOWLIST: frozenset[str] = frozenset({
    "status", "diff", "log", "show", "blame",
    "branch", "checkout", "switch",
    "ls-files", "ls-tree", "cat-file", "rev-parse",
})


# Two-level allowlist for ``alfred``: the second token (top subcommand)
# must be in the outer key set; the third token (sub-subcommand) must
# be in the matching inner set. ``None`` as the inner value means any
# third token is permitted (i.e. the top subcommand stands alone).
#
# NEVER add ``transport rotate``, ``up``, ``down``, ``vault edit``,
# ``vault delete``, ``vault create``, or any daemon-affecting command
# to this map — Andrew controls daemon lifecycle and canonical
# mutations directly. Reviews + digest + propose-person + vault read
# are the deliberate KAL-LE surfaces, nothing else.
_ALFRED_SUBCOMMAND_ALLOWLIST: dict[str, frozenset[str] | None] = {
    "reviews": frozenset({"write", "list", "read", "mark-addressed"}),
    "digest": frozenset({"write", "preview"}),
    "transport": frozenset({"propose-person"}),
    "vault": frozenset({"read"}),
}


# Full-command denylist — substring scan, case-insensitive.
# Any of these substrings anywhere in the rendered command rejects.
# Kept as a frozenset of lowercased substrings because the check is
# O(n) and n is small. Tests assert each attack vector rejects.
_COMMAND_DENYLIST_SUBSTRINGS: tuple[str, ...] = (
    # Git mutation / commit authority
    "git push", "git commit", "git rebase", "git merge",
    "git reset --hard", "git reset --keep", "git reset --soft",
    "git reset --mixed", "git clean -f", "git clean -fd",
    "git clean -x", "git clean --force",
    "git rm", "git tag -d", "git branch -d", "git branch -D",
    "git stash drop", "git stash clear", "git reflog delete",
    "git filter-branch", "git filter-repo",
    "git remote", "git fetch", "git pull",
    # Destructive filesystem
    "rm -rf", "rm -fr", "rm -r /", "rm -f /",
    # Permission + privilege escalation
    "chmod", "chown", "chgrp", "sudo", "doas",
    # Network egress
    "curl", "wget", "nc ", "netcat",
    "ssh ", "scp ", "rsync",
    # Package installs
    "pip install", "pip3 install", "pipx install",
    "npm install", "npm i ", "yarn add", "yarn install",
    "apt install", "apt-get install",
    "brew install", "cargo install",
    # Remote exec / arbitrary-code-via-interpreter
    " | sh", " | bash", "|sh", "|bash",
    "bash -c", "sh -c",
    "eval ", "exec ",
    # python/node -c lets the model run arbitrary code (including
    # os.system / subprocess). The test runner path (``pytest``,
    # ``python -m pytest``, ``python path/to/file.py``) stays open —
    # only ``-c``/``--command``/``-e`` inline-code invocation rejects.
    "python -c", "python3 -c",
    "python -c'", "python3 -c'",
    "node -e", "nodejs -e",
    # Misc destructive
    ">: ", ":>", "/dev/sda", "mkfs", "dd if=",
)


# Destructive keyword gate — when any of these substrings appear in the
# command, force dry_run=True regardless of caller intent. The caller
# can still invoke with these substrings for inspection, just not live.
# (This is separate from the denylist — denylist commands reject
# entirely; destructive-keyword commands run in dry-run.)
_DESTRUCTIVE_KEYWORDS: tuple[str, ...] = (
    "rm -r", "rm -f", "truncate ",
    "mv ", "cp -r",  # Large moves/recursive copies
)


# ---------------------------------------------------------------------------
# cwd resolution
# ---------------------------------------------------------------------------


def _allowed_repo_roots() -> tuple[Path, ...]:
    """Return the four allowed repo roots as resolved absolute paths.

    Computed at call time so tests can monkey-patch ``HOME`` without
    stale caching. The tuple is ordered by specificity so the first
    matching prefix wins.
    """
    home = Path(os.path.expanduser("~")).resolve()
    return (
        (home / "aftermath-lab").resolve(),
        (home / "aftermath-alfred").resolve(),
        (home / "aftermath-rrts").resolve(),
        (home / "alfred").resolve(),
    )


def _resolve_cwd(cwd_raw: str) -> Path | None:
    """Return the resolved path or ``None`` if it escapes the allowlist.

    Symlinks + ``..`` are collapsed via ``Path.resolve()`` before the
    prefix check, so a symlink pointing outside an allowed root cannot
    bypass the gate.
    """
    if not cwd_raw:
        return None
    try:
        resolved = Path(cwd_raw).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    for allowed in _allowed_repo_roots():
        try:
            # ``is_relative_to`` is 3.9+; also require the target either
            # matches the root exactly or descends from it.
            if resolved == allowed or resolved.is_relative_to(allowed):
                return resolved
        except (OSError, ValueError):
            continue
    return None


# ---------------------------------------------------------------------------
# Core executor
# ---------------------------------------------------------------------------


# Hard limits.
_TIMEOUT_SECONDS: float = 300.0
_MAX_OUTPUT_BYTES: int = 10 * 1024  # 10 KB per stream


def _contains_denylisted_substring(command_lower: str) -> str | None:
    """Return the first denylist substring found, or ``None``."""
    for bad in _COMMAND_DENYLIST_SUBSTRINGS:
        if bad in command_lower:
            return bad
    return None


def _contains_destructive_keyword(command_lower: str) -> str | None:
    for kw in _DESTRUCTIVE_KEYWORDS:
        if kw in command_lower:
            return kw
    return None


def _validate_first_token(argv: list[str]) -> tuple[bool, str]:
    """First-token allowlist check, with the git + alfred subcommand gates."""
    if not argv:
        return False, "empty_argv"
    head = argv[0]
    # Strip a leading path for robustness — ``/usr/bin/pytest`` is fine.
    head_base = os.path.basename(head)
    if head_base not in _ALLOWLIST:
        return False, f"token_not_allowlisted:{head_base}"
    if head_base == "git":
        if len(argv) < 2:
            return False, "git_requires_subcommand"
        sub = argv[1]
        if sub not in _GIT_SUBCOMMAND_ALLOWLIST:
            return False, f"git_subcommand_not_allowlisted:{sub}"
    if head_base == "alfred":
        if len(argv) < 2:
            return False, "alfred_requires_subcommand"
        top_sub = argv[1]
        if top_sub not in _ALFRED_SUBCOMMAND_ALLOWLIST:
            return False, f"alfred_subcommand_not_allowlisted:{top_sub}"
        inner_set = _ALFRED_SUBCOMMAND_ALLOWLIST[top_sub]
        if inner_set is not None:
            if len(argv) < 3:
                return False, f"alfred_{top_sub}_requires_subcommand"
            sub_sub = argv[2]
            if sub_sub not in inner_set:
                return False, (
                    f"alfred_{top_sub}_subcommand_not_allowlisted:{sub_sub}"
                )
    return True, "ok"


def _audit_append(
    audit_path: str | Path,
    *,
    command: str,
    cwd: str,
    exit_code: int,
    duration_ms: int,
    session_id: str,
    reason: str = "",
) -> None:
    """Append one audit line — never raises.

    Contract: command + cwd + exit_code + duration_ms + session_id +
    reason. NO stdout/stderr (too noisy for a per-invocation log).
    """
    try:
        p = Path(audit_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "command": command,
            "cwd": cwd,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "session_id": session_id,
            "reason": reason,
        }
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        # Audit failures never propagate. The operator will see log
        # events for them on the main logger.
        log.warning(
            "talker.bash_exec.audit_failed",
            audit_path=str(audit_path),
        )


async def execute(
    *,
    command: str,
    cwd: str,
    dry_run: bool = False,
    audit_path: str = "./data/bash_exec.jsonl",
    session_id: str = "",
) -> dict[str, Any]:
    """Run a command under the safety gates. Always returns a dict.

    Keys in the return shape:
        - ``exit_code``:     int. ``-1`` on rejection or timeout.
        - ``stdout``:        str. Clipped to 10 KB.
        - ``stderr``:        str. Clipped to 10 KB.
        - ``duration_ms``:   int.
        - ``truncated``:     bool — any stream exceeded the cap.
        - ``dry_run``:       bool — what was actually honoured.
        - ``reason``:        str — gate name if rejected/dry-run,
                              empty if clean.
        - ``argv``:          list[str] (parsed, for dry-run diagnostics).
        - ``cwd``:           str (resolved, or original if rejected).
    """
    # --- Deny-first ----------------------------------------------------
    command_stripped = command.strip()
    command_lower = command_stripped.lower()
    # Denylist BEFORE allowlist so an allowlisted head can't mask a
    # denylisted tail (e.g. "git log --no-pager status; git push"
    # — shlex would reject the semi anyway, but we want the denylist
    # to own the first rejection so the error is specific).
    denylist_hit = _contains_denylisted_substring(command_lower)
    if denylist_hit is not None:
        _audit_append(
            audit_path,
            command=command_stripped,
            cwd=cwd,
            exit_code=-1,
            duration_ms=0,
            session_id=session_id,
            reason=f"denylist:{denylist_hit}",
        )
        log.warning(
            "talker.bash_exec.denylist",
            match=denylist_hit,
        )
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"denied: contains {denylist_hit!r}",
            "duration_ms": 0,
            "truncated": False,
            "dry_run": False,
            "reason": f"denylist:{denylist_hit}",
            "argv": [],
            "cwd": cwd,
        }

    # --- cwd gate ------------------------------------------------------
    resolved_cwd = _resolve_cwd(cwd)
    if resolved_cwd is None:
        _audit_append(
            audit_path,
            command=command_stripped,
            cwd=cwd,
            exit_code=-1,
            duration_ms=0,
            session_id=session_id,
            reason="cwd_not_allowed",
        )
        log.warning("talker.bash_exec.cwd_rejected", cwd=cwd)
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": (
                f"denied: cwd {cwd!r} not under an allowed repo root "
                "(aftermath-lab, aftermath-alfred, aftermath-rrts, alfred)"
            ),
            "duration_ms": 0,
            "truncated": False,
            "dry_run": False,
            "reason": "cwd_not_allowed",
            "argv": [],
            "cwd": cwd,
        }

    # --- shlex split ---------------------------------------------------
    try:
        argv = shlex.split(command_stripped)
    except ValueError as exc:
        _audit_append(
            audit_path,
            command=command_stripped,
            cwd=str(resolved_cwd),
            exit_code=-1,
            duration_ms=0,
            session_id=session_id,
            reason="shlex_parse_error",
        )
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"parse error: {exc}",
            "duration_ms": 0,
            "truncated": False,
            "dry_run": False,
            "reason": "shlex_parse_error",
            "argv": [],
            "cwd": str(resolved_cwd),
        }

    # --- Allowlist -----------------------------------------------------
    ok, reason = _validate_first_token(argv)
    if not ok:
        _audit_append(
            audit_path,
            command=command_stripped,
            cwd=str(resolved_cwd),
            exit_code=-1,
            duration_ms=0,
            session_id=session_id,
            reason=reason,
        )
        log.warning("talker.bash_exec.allowlist_miss", reason=reason)
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"denied: {reason}",
            "duration_ms": 0,
            "truncated": False,
            "dry_run": False,
            "reason": reason,
            "argv": argv,
            "cwd": str(resolved_cwd),
        }

    # --- Destructive-keyword gate --------------------------------------
    forced_dry = False
    destructive_kw = _contains_destructive_keyword(command_lower)
    if destructive_kw is not None:
        forced_dry = True
        dry_run = True

    # --- Dry run (either explicit or forced) ---------------------------
    if dry_run:
        reason_out = (
            f"destructive_keyword:{destructive_kw}" if forced_dry else "dry_run"
        )
        _audit_append(
            audit_path,
            command=command_stripped,
            cwd=str(resolved_cwd),
            exit_code=0,
            duration_ms=0,
            session_id=session_id,
            reason=reason_out,
        )
        return {
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "duration_ms": 0,
            "truncated": False,
            "dry_run": True,
            "reason": reason_out,
            "argv": argv,
            "cwd": str(resolved_cwd),
        }

    # --- Execute -------------------------------------------------------
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(resolved_cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        _audit_append(
            audit_path,
            command=command_stripped,
            cwd=str(resolved_cwd),
            exit_code=-1,
            duration_ms=0,
            session_id=session_id,
            reason="command_not_found",
        )
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"command not found: {argv[0]}",
            "duration_ms": 0,
            "truncated": False,
            "dry_run": False,
            "reason": "command_not_found",
            "argv": argv,
            "cwd": str(resolved_cwd),
        }
    except PermissionError as exc:
        _audit_append(
            audit_path,
            command=command_stripped,
            cwd=str(resolved_cwd),
            exit_code=-1,
            duration_ms=0,
            session_id=session_id,
            reason="permission_error",
        )
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"permission error: {exc}",
            "duration_ms": 0,
            "truncated": False,
            "dry_run": False,
            "reason": "permission_error",
            "argv": argv,
            "cwd": str(resolved_cwd),
        }

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        duration_ms = int((time.monotonic() - start) * 1000)
        _audit_append(
            audit_path,
            command=command_stripped,
            cwd=str(resolved_cwd),
            exit_code=-1,
            duration_ms=duration_ms,
            session_id=session_id,
            reason="timeout",
        )
        return {
            "exit_code": -1,
            "stdout": "",
            "stderr": f"timed out after {_TIMEOUT_SECONDS}s",
            "duration_ms": duration_ms,
            "truncated": False,
            "dry_run": False,
            "reason": "timeout",
            "argv": argv,
            "cwd": str(resolved_cwd),
        }

    duration_ms = int((time.monotonic() - start) * 1000)
    stdout_full = (stdout_b or b"")
    stderr_full = (stderr_b or b"")
    truncated = len(stdout_full) > _MAX_OUTPUT_BYTES or len(stderr_full) > _MAX_OUTPUT_BYTES
    stdout_out = stdout_full[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    stderr_out = stderr_full[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    if len(stdout_full) > _MAX_OUTPUT_BYTES:
        stdout_out += "\n[...truncated...]"
    if len(stderr_full) > _MAX_OUTPUT_BYTES:
        stderr_out += "\n[...truncated...]"

    _audit_append(
        audit_path,
        command=command_stripped,
        cwd=str(resolved_cwd),
        exit_code=proc.returncode if proc.returncode is not None else -1,
        duration_ms=duration_ms,
        session_id=session_id,
        reason="",
    )

    return {
        "exit_code": proc.returncode if proc.returncode is not None else -1,
        "stdout": stdout_out,
        "stderr": stderr_out,
        "duration_ms": duration_ms,
        "truncated": truncated,
        "dry_run": False,
        "reason": "",
        "argv": argv,
        "cwd": str(resolved_cwd),
    }
