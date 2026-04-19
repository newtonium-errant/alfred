"""Brief integration for the BIT daemon's most recent run.

The Morning Brief includes a Health section that summarizes the latest
``vault/process/Alfred BIT {date}.md`` record (or, as a fallback, the
BIT state file).  The goal is a compact snapshot the reader can
interpret at a glance:

    ## Health

    **Overall:** ok (last run 2026-04-19 05:55 ADT, quick mode)
    - curator: ok
    - janitor: ok
    - distiller: ok
    - surveyor: warn — ollama 404 on /
    - brief:    ok
    - mail:     ok
    - talker:   skip

If there's no BIT record from today (e.g., BIT hasn't run yet because
it's earlier than its scheduled time), the section returns a single
line explaining the situation — no noisy empty table.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml


def _parse_frontmatter(path: Path) -> dict[str, Any] | None:
    """Parse YAML frontmatter out of a Markdown file.

    Returns ``None`` if the file has no frontmatter or can't be read.
    We don't use ``python-frontmatter`` here because we only need the
    frontmatter dict — the rendered body is irrelevant to this section.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if match is None:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _find_latest_bit_record(vault_path: Path, bit_dir: str = "process") -> Path | None:
    """Find the most recent Alfred BIT record in the vault.

    The BIT daemon writes ``vault/<bit_dir>/Alfred BIT {date}.md``, so
    we glob for ``Alfred BIT *.md`` and return the lexicographically
    last one — since the dates are ISO-8601, lexicographic order is
    also chronological order.
    """
    dir_path = vault_path / bit_dir
    if not dir_path.exists():
        return None
    candidates = sorted(dir_path.glob("Alfred BIT *.md"))
    return candidates[-1] if candidates else None


def _read_state_latest(state_path: Path) -> dict[str, Any] | None:
    """Fall back to the BIT state file's most recent run."""
    if not state_path.exists():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    runs = data.get("runs") or []
    if not runs:
        return None
    return runs[-1]


def _per_tool_lines(body: str) -> list[tuple[str, str, str]]:
    """Extract per-tool (name, status_word, detail) tuples from the body.

    The BIT record body contains a ``## Summary`` block with the human
    rendering — lines like ``[WARN] surveyor  (92 ms) — ollama 404``.
    We parse those so the brief's Health section can re-render in a
    denser format without re-running the checks.

    Returns a list of tuples in the order they appear. If parsing
    fails, returns an empty list — the caller falls back to the
    frontmatter's ``tool_counts`` summary.
    """
    tool_line_re = re.compile(
        r"^\[(?P<status>[A-Z ]+)\]\s+(?P<tool>\w+)"
        r"(?:\s+\(\d+\s*ms\))?"
        r"(?:\s*—\s*(?P<detail>.*))?$"
    )
    out: list[tuple[str, str, str]] = []
    in_summary = False
    for line in body.splitlines():
        if line.startswith("## Summary"):
            in_summary = True
            continue
        if in_summary and line.startswith("## "):
            break
        if not in_summary:
            continue
        # Tool-header lines start at column 0; check-result lines start
        # with four spaces and we want to skip those.
        if line.startswith(" "):
            continue
        stripped = line.strip()
        if not stripped.startswith("["):
            continue
        m = tool_line_re.match(stripped)
        if m is None:
            continue
        status = m.group("status").strip().lower()
        tool = m.group("tool")
        detail = (m.group("detail") or "").strip()
        out.append((tool, status, detail))
    return out


def render_health_section(
    vault_path: str | Path,
    state_path: str | Path | None = None,
    today: str | None = None,
) -> str:
    """Render the Health section markdown for the Morning Brief.

    Args:
        vault_path: Path to vault root.  We look under
            ``vault/process/Alfred BIT *.md``.
        state_path: Optional path to the BIT state JSON. Used only as
            a fallback when no vault record is found.
        today: ISO date string. If the latest BIT record isn't from
            today, we still render it but note the stale date.

    Returns:
        Markdown string suitable for embedding as the body of a
        ``## Health`` section.  The section header itself is written
        by the brief renderer.
    """
    vault = Path(vault_path)
    today_str = today or date.today().isoformat()

    record_path = _find_latest_bit_record(vault)
    if record_path is not None:
        frontmatter = _parse_frontmatter(record_path)
        if frontmatter is not None:
            body = record_path.read_text(encoding="utf-8")
            return _render_from_frontmatter(frontmatter, body, today_str)

    # Fallback to state file
    if state_path is not None:
        latest = _read_state_latest(Path(state_path))
        if latest is not None:
            return _render_from_state(latest, today_str)

    return "_No BIT run recorded yet. Start the BIT daemon via `alfred up` or run `alfred bit run-now`._"


def _render_from_frontmatter(
    frontmatter: dict[str, Any],
    body: str,
    today_str: str,
) -> str:
    """Format the Health section from a parsed BIT record."""
    overall = str(frontmatter.get("overall_status", "unknown"))
    mode = str(frontmatter.get("mode", "?"))
    record_date = str(frontmatter.get("created", ""))
    started = str(frontmatter.get("started", ""))

    lines: list[str] = []

    stale = record_date and record_date != today_str
    date_note = f" — stale ({record_date})" if stale else ""
    lines.append(
        f"**Overall:** {overall} "
        f"(last run {started}, {mode} mode{date_note})"
    )

    # Prefer per-tool breakdown from the body; fall back to tool_counts
    per_tool = _per_tool_lines(body)
    if per_tool:
        # Pad tool names to a consistent width for readability
        width = max(len(t[0]) for t in per_tool) if per_tool else 0
        for tool, status, detail in per_tool:
            suffix = f" — {detail}" if detail else ""
            lines.append(f"- {tool:<{width}}  {status}{suffix}")
    else:
        tool_counts = frontmatter.get("tool_counts") or {}
        if tool_counts:
            counts_str = ", ".join(
                f"{v} {k}" for k, v in sorted(tool_counts.items()) if v
            )
            lines.append(f"- tool summary: {counts_str or 'no tools'}")

    record_link = frontmatter.get("name") or "latest BIT"
    lines.append("")
    lines.append(f"See full report: [[process/{record_link}]]")
    return "\n".join(lines)


def _render_from_state(latest: dict[str, Any], today_str: str) -> str:
    """Fallback renderer when no vault record is readable."""
    overall = latest.get("overall_status", "unknown")
    mode = latest.get("mode", "?")
    generated = latest.get("generated_at", "")
    record_date = latest.get("date", "")
    counts = latest.get("tool_counts") or {}

    stale = record_date and record_date != today_str
    stale_note = f" — stale ({record_date})" if stale else ""

    lines = [
        f"**Overall:** {overall} (last run {generated}, {mode} mode{stale_note})",
    ]
    if counts:
        counts_str = ", ".join(
            f"{v} {k}" for k, v in sorted(counts.items()) if v
        )
        lines.append(f"- tool summary: {counts_str or 'no tools'}")
    lines.append("")
    lines.append(
        "_Full report unavailable — falling back to BIT state file._"
    )
    return "\n".join(lines)
