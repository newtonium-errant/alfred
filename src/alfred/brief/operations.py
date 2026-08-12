"""Operations section — daily snapshot of Alfred tool activity from state files and audit log."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .utils import SectionReadStatus, get_logger, safe_read_section_file

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


def _medium_waiting_summary(feed_store_path: str) -> str:
    """Count OPEN medium-tier ``email_tier`` items in the feed store → one line.

    #27 slice 2 — "medium waits, but never SILENTLY." Medium emails surface as
    ``email_tier`` calibration cards; while the operator hasn't confirmed / re-
    tiered them they sit OPEN in the feed store. This is the brief's honest count
    of that held set (tap-through on the FE = the feed filtered to waiting emails
    — no new brief machinery; the feed already holds them).

    "Waiting-medium" = feed item ``kind == "email_tier"`` AND ``state == "open"``
    AND the classifier's verdict (``evidence.classifier_priority``) was
    ``"medium"``. A confirmed / re-tiered card is ``acted`` (excluded); the
    classifier verdict is the tier axis (a re-tier acts the item, so open items
    still carry the original verdict).

    Returns (always a line — ILB: a held-count of 0 is a real, tested state, not
    silence):
      - ``"📥 No medium emails waiting"`` — explicit zero-state
      - ``"📥 N medium email(s) waiting"`` — N > 0
      - ``"📥 Medium-email count unavailable"`` — read/fold error (warned; never
        crashes the bare-called Operations render)

    Read via ``FeedStore.load`` (brief → feed is a leaf import; ``load`` is itself
    lock-free + torn-line-tolerant). Belt-wrapped so a feed fault can never break
    the Operations section (the daemon calls it bare).
    """
    try:
        from alfred.feed import FeedStore

        items = FeedStore(feed_store_path).load()
        n = sum(
            1
            for it in items.values()
            if it.kind == "email_tier"
            and it.state == "open"
            and (it.evidence or {}).get("classifier_priority") == "medium"
        )
    except Exception as exc:  # noqa: BLE001 — Operations is called bare; never crash
        log.warning(
            "operations.medium_waiting_failed",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return "📥 Medium-email count unavailable"
    if n == 0:
        return "📥 No medium emails waiting"
    return f"📥 {n} medium email{'s' if n != 1 else ''} waiting"


def _count_audit_log(audit_path: Path, since: str) -> dict[str, dict[str, int]]:
    """Count audit log mutations by tool and operation since a given ISO date prefix.

    Returns: {tool: {op: count}}
    """
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    if not audit_path.exists():
        return counts
    # Defensive read via the shared helper — the old bare ``except OSError``
    # missed UnicodeDecodeError (a ValueError subclass), so a non-UTF-8 audit
    # log escaped and crashed the whole brief (this section is called BARE by
    # the daemon at daemon.py). A read failure → count-0 (empty counts), warned.
    read = safe_read_section_file(audit_path)
    if read.status is not SectionReadStatus.OK:
        log.warning(
            "operations.audit_read_failed",
            error=read.detail,
            error_type=read.error_type,
        )
        return counts
    for line in read.text.splitlines():
        try:
            entry = json.loads(line)
            ts = entry.get("ts", "")
            if ts >= since:
                tool = entry.get("tool", "unknown")
                op = entry.get("op", "unknown")
                counts[tool][op] += 1
        except json.JSONDecodeError:
            continue
    return counts


def _read_json(path: Path) -> dict:
    """Read a JSON file, returning empty dict on failure."""
    if not path.exists():
        return {}
    # Defensive read via the shared helper — the old ``(json.JSONDecodeError,
    # OSError)`` catch missed UnicodeDecodeError (a SIBLING of JSONDecodeError
    # under ValueError, not a subclass), so a non-UTF-8 state file escaped and
    # crashed the whole brief (bare render at daemon.py). Helper handles the
    # read; the json.loads catch below stays for JSON-syntax errors on a clean
    # read. Mirrors health_section._read_state_latest (ea8a749).
    # N1 (ILB symmetry): a corrupt state file used to degrade SILENTLY ({}) —
    # a broken curator_state.json rendered as "No new emails processed", idle
    # masquerading as broken. Both load-failure branches now warn.
    # (N2: payload fields carry only errno/position/path from the exception,
    # never file content — safe for these alfred-written state files. A future
    # renderer over PERSONAL vault content must keep log payloads content-free.)
    read = safe_read_section_file(path)
    if read.status is not SectionReadStatus.OK:
        log.warning(
            "operations.state_read_failed",
            path=str(path),
            stage="read",
            error=read.detail,
            error_type=read.error_type,
        )
        return {}
    try:
        return json.loads(read.text)
    except json.JSONDecodeError as exc:
        log.warning(
            "operations.state_read_failed",
            path=str(path),
            stage="json",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
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
    feed_store_path: str | None = None,
) -> str:
    """Render the FULL Operations status snapshot as markdown.

    **NO LONGER THE BRIEF'S §5 (Phase C, 2026-08-12).** The morning section is
    now notable-events-only — ``ops_notable.render_ops_notable_section`` — on
    the ratified reasoning that a section of the brief is an attention claim
    and steady state, even steady-bad, is not news. Restating every metric each
    morning is what trained the eye to skip the section.

    This snapshot answers a different and still-legitimate question — "what ARE
    the numbers right now" — which belongs to a surface the operator opens on
    purpose. No such surface exists yet, so this function is currently
    UNCALLED in production: a deletion candidate held back from the same commit
    as the restructure, alongside ``brief/routine_section.py``.

    Its ``feed_store_path`` "medium emails waiting" line did NOT leave the
    brief with it — that line is an open-item count rather than a delta, so §5
    renders it directly via :func:`_medium_waiting_summary`.

    Args:
        data_dir: Path to Alfred data directory (state files + audit log).
        vault_path: Path to vault root.
        since: ISO date string to count from. Defaults to today.
        quarantine_dir_name: Vault-relative top-level directory name
            for the c6 spam quarantine surface (default ``"quarantine"``
            matches ``EmailClassifierConfig.quarantine_dir_name``).
            Threaded through to ``_quarantine_summary`` so per-instance
            overrides surface in the operator brief.
        feed_store_path: Path to the feed store (#27 slice 2). When provided,
            the "medium emails waiting" line is appended (an OPEN-medium
            ``email_tier`` count). ``None`` (the default for non-daemon callers /
            existing tests) omits the line, keeping their output byte-identical.
            The daemon threads it UNCONDITIONALLY (not gated on ``feed.enabled``)
            so the feed-enabled and feed-disabled briefs stay byte-identical —
            the feed-parity golden gate — since both read the same store at
            Operations-render time (before any brief feed-write, and the brief
            never writes ``email_tier``).
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

    # #27 slice 2 — medium emails waiting. Sits beside the quarantine line
    # (both are email-classifier operator-review surfaces). Rendered ONLY when
    # the caller threads a feed store path (existing callers pass None → byte-
    # identical output). Always a line when threaded, incl. the explicit
    # zero-state per ILB — held medium email must never be SILENTLY held.
    if feed_store_path is not None:
        lines.append(f"**{_medium_waiting_summary(feed_store_path)}**")

    # Audit breakdown if there's activity
    if audit_counts:
        lines.append("")
        lines.append("### Mutation Log")
        for tool, ops in sorted(audit_counts.items()):
            ops_str = ", ".join(f"{count} {op}" for op, count in sorted(ops.items()))
            lines.append(f"- **{tool}:** {ops_str}")

    return "\n".join(lines)
