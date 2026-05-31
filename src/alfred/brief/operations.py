"""Operations section — daily snapshot of Alfred tool activity from state files and audit log."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .utils import get_logger

log = get_logger(__name__)


def _quarantine_summary(
    vault_path: Path,
    quarantine_dir_name: str = "quarantine",
) -> str:
    """Summarize email-quarantine activity for the operator review surface.

    c6 (2026-05-31). Counts spam records currently sitting under
    ``<vault>/<quarantine_dir>/spam/<YYYY-MM>/`` for the current
    month + 7-day rolling window. The brief surfaces these so the
    operator can periodically scan for misclassifications and
    re-process if any contact ended up quarantined incorrectly.

    Returns one of:
      - ``"Spam quarantine: empty"`` — directory missing or no records
        (per feedback_intentionally_left_blank.md: explicit absence
        signal so the operator knows the check ran)
      - ``"Spam quarantine: N this week (M this month)"`` — both counts
        when populated. ``this week`` is the rolling 7-day window
        (file mtime >= now - 7d); ``this month`` is the current
        YYYY-MM bucket directory count
    """
    quarantine_root = vault_path / quarantine_dir_name / "spam"
    if not quarantine_root.exists():
        return "Spam quarantine: empty"

    now = datetime.now()
    month_bucket = now.strftime("%Y-%m")
    week_cutoff = now - timedelta(days=7)

    month_dir = quarantine_root / month_bucket
    month_count = 0
    if month_dir.exists():
        month_count = sum(1 for _ in month_dir.glob("*.md"))

    # Rolling 7-day window — walk ALL month buckets since some weeks
    # straddle month boundaries. Cheap because quarantine volume is
    # low (operator-scale, not bulk-scale).
    week_count = 0
    try:
        for md_file in quarantine_root.rglob("*.md"):
            try:
                mtime = datetime.fromtimestamp(md_file.stat().st_mtime)
            except OSError:
                continue
            if mtime >= week_cutoff:
                week_count += 1
    except OSError as exc:
        log.warning("operations.quarantine_walk_failed", error=str(exc))
        return "Spam quarantine: (read error — check log)"

    if week_count == 0 and month_count == 0:
        # Per ILB: still emit the explicit-zero so operator knows
        # the check ran. Distinct from "directory missing" above.
        return "Spam quarantine: empty"
    return f"Spam quarantine: {week_count} this week ({month_count} this month)"


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
    quarantine_dir_name: str = "quarantine",
) -> str:
    """Render the Operations section as markdown.

    Args:
        data_dir: Path to Alfred data directory (state files + audit log).
        vault_path: Path to vault root.
        since: ISO date string to count from. Defaults to today.
        quarantine_dir_name: Vault-relative top-level directory name
            for the c6 spam quarantine surface (default ``"quarantine"``
            matches ``EmailClassifierConfig.quarantine_dir_name``).
            Threaded through to ``_quarantine_summary`` so per-instance
            overrides surface in the operator brief.
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

    # c6 spam quarantine surface (2026-05-31). Operator-discoverable
    # count of recently-quarantined spam emails — recovery surface for
    # misclassification review. Always emit (per ILB) so the operator
    # knows the check ran; "empty" is a valid state worth surfacing.
    lines.append(
        f"**{_quarantine_summary(vault, quarantine_dir_name=quarantine_dir_name)}**"
    )

    # Audit breakdown if there's activity
    if audit_counts:
        lines.append("")
        lines.append("### Mutation Log")
        for tool, ops in sorted(audit_counts.items()):
            ops_str = ", ".join(f"{count} {op}" for op, count in sorted(ops.items()))
            lines.append(f"- **{tool}:** {ops_str}")

    return "\n".join(lines)
