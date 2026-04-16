"""Vault git snapshot — track vault state in a separate git repo."""

from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def build_snapshot_summary(audit_log_path: Path, since: str | None = None) -> str:
    """Build a human-readable commit message from vault_audit.log entries.

    Reads JSONL entries from *audit_log_path*, filters to entries after *since*
    (ISO timestamp string), groups by tool, and formats a multi-line summary.
    Falls back to a bare timestamp message when the log is missing or empty.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    audit_log_path = Path(audit_log_path)

    if not audit_log_path.exists():
        return f"Vault snapshot {now}"

    # Parse entries, filtering by timestamp
    tool_ops: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    tool_sweeps: dict[str, set[str]] = defaultdict(set)
    skipped = 0
    total = 0

    with audit_log_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                skipped += 1
                continue
            ts = entry.get("ts", "")
            if since and ts <= since:
                continue
            tool = entry.get("tool", "unknown")
            op = entry.get("op", "unknown")
            tool_ops[tool][op] += 1
            total += 1
            # Track sweep IDs for janitor traceability
            if tool == "janitor" and entry.get("detail"):
                tool_sweeps["janitor"].add(entry["detail"])

    if total == 0:
        suffix = f" ({skipped} unparseable lines skipped)" if skipped else ""
        return f"Vault snapshot {now} — no activity since last snapshot{suffix}"

    # Build per-tool lines
    lines = [f"Vault snapshot {now}", ""]
    for tool in sorted(tool_ops):
        parts = []
        for op in ("create", "modify", "delete"):
            count = tool_ops[tool].get(op, 0)
            if count:
                parts.append(f"{count} {op}d" if op != "modify" else f"{count} modified")
        detail = ", ".join(parts)
        if tool == "janitor" and tool_sweeps.get("janitor"):
            ids = sorted(tool_sweeps["janitor"])
            if len(ids) > 5:
                sweep_ids = ", ".join(ids[:5]) + f" +{len(ids) - 5} more"
            else:
                sweep_ids = ", ".join(ids)
            detail += f" (sweep {sweep_ids})"
        lines.append(f"{tool}: {detail}")

    lines.append("")
    lines.append(f"Total: {total} operations across {len(tool_ops)} tools")
    if skipped:
        lines.append(f"({skipped} unparseable lines skipped)")

    return "\n".join(lines)


class SnapshotError(Exception):
    """Raised when a snapshot operation fails."""


def _git(vault_path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a git command inside the vault directory."""
    return subprocess.run(
        ["git", *args],
        cwd=str(vault_path),
        capture_output=True,
        text=True,
        check=check,
    )


def _is_initialized(vault_path: Path) -> bool:
    """Check if the vault has a git repo."""
    return (vault_path / ".git").is_dir()


def init_repo(vault_path: Path) -> str:
    """Initialize a git repo inside the vault directory.

    Creates vault/.gitignore, stages everything, and makes an initial commit.
    Returns the initial commit hash.
    """
    vault_path = Path(vault_path)
    if _is_initialized(vault_path):
        raise SnapshotError("Vault git repo already initialized")

    # git init
    _git(vault_path, "init")

    # Create .gitignore inside vault
    gitignore = vault_path / ".gitignore"
    gitignore.write_text(
        "# Managed by Alfred vault snapshot\n"
        ".obsidian/\n"
        "inbox/processed/\n",
        encoding="utf-8",
    )

    # Initial commit
    _git(vault_path, "add", "-A")
    _git(vault_path, "commit", "-m", "Initial vault snapshot")

    result = _git(vault_path, "rev-parse", "HEAD")
    return result.stdout.strip()


def take_snapshot(vault_path: Path, message: str | None = None) -> str | None:
    """Stage all changes and commit.

    Returns the commit hash, or None if there was nothing to commit.
    """
    vault_path = Path(vault_path)
    if not _is_initialized(vault_path):
        raise SnapshotError("Vault git repo not initialized — run: alfred vault snapshot --init")

    # Stage everything
    _git(vault_path, "add", "-A")

    # Check if there's anything to commit
    status = _git(vault_path, "diff", "--cached", "--stat")
    if not status.stdout.strip():
        return None

    # Build commit message
    if not message:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        # Count changed files from the staged diff
        numstat = _git(vault_path, "diff", "--cached", "--numstat")
        count = len([line for line in numstat.stdout.strip().splitlines() if line])
        message = f"Vault snapshot {now} — {count} records"

    _git(vault_path, "commit", "-m", message)

    result = _git(vault_path, "rev-parse", "HEAD")
    return result.stdout.strip()


def get_status(vault_path: Path) -> dict:
    """Return snapshot status info for the vault.

    Keys: initialized, last_commit_date, last_commit_hash,
          uncommitted_count, total_commits
    """
    vault_path = Path(vault_path)
    info: dict = {"initialized": _is_initialized(vault_path)}

    if not info["initialized"]:
        info.update(
            last_commit_date=None,
            last_commit_hash=None,
            uncommitted_count=0,
            total_commits=0,
        )
        return info

    # Last commit
    log_result = _git(vault_path, "log", "-1", "--format=%H %aI", check=False)
    if log_result.returncode == 0 and log_result.stdout.strip():
        parts = log_result.stdout.strip().split(" ", 1)
        info["last_commit_hash"] = parts[0]
        info["last_commit_date"] = parts[1] if len(parts) > 1 else None
    else:
        info["last_commit_hash"] = None
        info["last_commit_date"] = None

    # Total commits
    count_result = _git(vault_path, "rev-list", "--count", "HEAD", check=False)
    if count_result.returncode == 0:
        info["total_commits"] = int(count_result.stdout.strip())
    else:
        info["total_commits"] = 0

    # Uncommitted changes
    status_result = _git(vault_path, "status", "--porcelain")
    lines = [line for line in status_result.stdout.splitlines() if line.strip()]
    info["uncommitted_count"] = len(lines)

    return info


def restore_file(vault_path: Path, rel_path: str, commit: str | None = None) -> str:
    """Restore a file from a previous commit.

    Args:
        vault_path: Path to the vault root.
        rel_path: Vault-relative file path (e.g. "person/John Smith.md").
        commit: Git commit hash to restore from. Defaults to HEAD~1.

    Returns:
        The commit hash the file was restored from.
    """
    vault_path = Path(vault_path)
    if not _is_initialized(vault_path):
        raise SnapshotError("Vault git repo not initialized")

    target = commit or "HEAD~1"

    # Verify the commit exists
    verify = _git(vault_path, "rev-parse", "--verify", target, check=False)
    if verify.returncode != 0:
        raise SnapshotError(f"Commit not found: {target}")

    # Restore the file
    result = _git(vault_path, "checkout", target, "--", rel_path, check=False)
    if result.returncode != 0:
        err = result.stderr.strip() or f"Failed to restore {rel_path} from {target}"
        raise SnapshotError(err)

    # Return the actual commit hash used
    resolved = _git(vault_path, "rev-parse", target)
    return resolved.stdout.strip()
