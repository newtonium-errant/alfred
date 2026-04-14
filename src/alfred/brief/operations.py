"""Operations section — daily snapshot of Alfred tool activity from state files and audit log."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from .utils import get_logger

log = get_logger(__name__)


def _count_audit_log(audit_path: Path, since: str) -> dict[str, dict[str, int]]:
    """Count audit log mutations by tool and operation since a given ISO date prefix.

    Returns: {tool: {op: count}}
    """
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    if not audit_path.exists():
        return counts
    try:
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                ts = entry.get("ts", "")
                if ts >= since:
                    tool = entry.get("tool", "unknown")
                    op = entry.get("op", "unknown")
                    counts[tool][op] += 1
            except json.JSONDecodeError:
                continue
    except OSError as e:
        log.warning("operations.audit_read_failed", error=str(e))
    return counts


def _read_json(path: Path) -> dict:
    """Read a JSON file, returning empty dict on failure."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _curator_summary(state: dict, since: str) -> str:
    """Summarize curator activity since date."""
    processed = state.get("processed", {})
    recent = [
        v for v in processed.values()
        if v.get("processed_at", "") >= since
    ]
    if not recent:
        return "No new emails processed"
    files_created = sum(len(v.get("files_created", [])) for v in recent)
    return f"{len(recent)} emails processed, {files_created} records created"


def _janitor_summary(state: dict, since: str) -> str:
    """Summarize janitor activity since date."""
    sweeps = state.get("sweeps", {})
    recent = {k: v for k, v in sweeps.items() if v.get("timestamp", "") >= since}
    if not recent:
        return "No sweeps"
    total_fixed = sum(v.get("files_fixed", 0) for v in recent.values())
    # Report latest sweep's issue snapshot (not cumulative across sweeps)
    latest = max(recent.values(), key=lambda v: v.get("timestamp", ""))
    issues_snapshot = latest.get("issues_found", 0)
    by_sev = latest.get("issues_by_severity", {})
    sev_str = ""
    if by_sev:
        parts = []
        for sev in ("CRITICAL", "WARNING", "INFO"):
            if by_sev.get(sev, 0) > 0:
                parts.append(f"{by_sev[sev]} {sev.lower()}")
        sev_str = f" ({', '.join(parts)})"
    return f"{len(recent)} sweeps, {total_fixed} files fixed, {issues_snapshot} open issues{sev_str}"


def _distiller_summary(state: dict, since: str) -> str:
    """Summarize distiller activity since date."""
    runs = state.get("runs", {})
    recent = {k: v for k, v in runs.items() if v.get("timestamp", "") >= since}
    if not recent:
        return "No extraction runs"
    total_created = {}
    for v in recent.values():
        for lt, count in v.get("records_created", {}).items():
            total_created[lt] = total_created.get(lt, 0) + count
    n = len(recent)
    run_word = "run" if n == 1 else "runs"
    created_str = ", ".join(f"{count} {lt}" for lt, count in sorted(total_created.items()))
    return f"{n} {run_word} — created {created_str}" if created_str else f"{n} {run_word}, no new records"


def _vault_record_count(vault_path: Path, ignore_dirs: list[str] | None = None) -> int:
    """Count total .md files in the vault."""
    ignore = set(ignore_dirs or ["_templates", "_bases", ".obsidian", "view", "inbox"])
    count = 0
    for f in vault_path.rglob("*.md"):
        rel = f.relative_to(vault_path)
        if not any(part in ignore for part in rel.parts):
            count += 1
    return count


def format_operations_section(
    data_dir: str,
    vault_path: str,
    since: str | None = None,
) -> str:
    """Render the Operations section as markdown.

    Args:
        data_dir: Path to Alfred data directory (state files + audit log).
        vault_path: Path to vault root.
        since: ISO date string to count from. Defaults to today.
    """
    if since is None:
        since = date.today().isoformat()

    data = Path(data_dir)
    vault = Path(vault_path)

    # Read state files
    curator_state = _read_json(data / "curator_state.json")
    janitor_state = _read_json(data / "janitor_state.json")
    distiller_state = _read_json(data / "distiller_state.json")

    # Audit log summary
    audit_counts = _count_audit_log(data / "vault_audit.log", since)

    # Tool summaries
    tools = [
        ("Curator", _curator_summary(curator_state, since)),
        ("Janitor", _janitor_summary(janitor_state, since)),
        ("Distiller", _distiller_summary(distiller_state, since)),
    ]

    # Total vault records
    total_records = _vault_record_count(vault)

    # Audit totals
    total_mutations = sum(
        count
        for tool_ops in audit_counts.values()
        for count in tool_ops.values()
    )

    lines = []

    # Tool activity table
    lines.append("| Tool | Activity |")
    lines.append("|------|----------|")
    for tool_name, summary in tools:
        lines.append(f"| {tool_name} | {summary} |")
    lines.append("")

    # Vault stats
    lines.append(f"**Vault:** {total_records:,} records total")
    if total_mutations:
        lines.append(f"**Mutations today:** {total_mutations}")

    # Audit breakdown if there's activity
    if audit_counts:
        lines.append("")
        lines.append("### Mutation Log")
        for tool, ops in sorted(audit_counts.items()):
            ops_str = ", ".join(f"{count} {op}" for op, count in sorted(ops.items()))
            lines.append(f"- **{tool}:** {ops_str}")

    return "\n".join(lines)
