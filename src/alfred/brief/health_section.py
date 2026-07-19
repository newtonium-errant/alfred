"""Brief integration for the BIT daemon's most recent run.

The Morning Brief includes a Health section that summarizes the latest
``vault/run/Alfred BIT {date}.md`` record (or, as a fallback, the
BIT state file).  Records written before 2026-06-12 live in
``vault/process/`` (the old mis-routed default — janitor DIR001), so
the lookup checks both directories until the historical records are
migrated.  The goal is a compact snapshot the reader can interpret at
a glance:

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

from .utils import SectionReadStatus, get_logger, safe_read_section_file

log = get_logger(__name__)


def _parse_frontmatter(path: Path) -> dict[str, Any] | None:
    """Parse YAML frontmatter out of a Markdown file.

    Returns ``None`` if the file has no frontmatter or can't be read.
    We don't use ``python-frontmatter`` here because we only need the
    frontmatter dict — the rendered body is irrelevant to this section.
    """
    if not path.exists():
        return None
    # Defensive read via the shared helper — the old bare ``except OSError``
    # missed UnicodeDecodeError (a ValueError subclass), so a non-UTF-8 BIT
    # record escaped this degrade path and crashed the whole brief (this
    # render is called BARE by the daemon).
    read = safe_read_section_file(path)
    if read.status is not SectionReadStatus.OK:
        # Load failure (non-UTF-8 / I/O error) → no-record path. Emit a
        # signal so a corrupt BIT record is distinguishable from "BIT hasn't
        # run yet" (ILB: broken must not masquerade as idle). Mirrors the
        # sibling brief.watches_state_load_failed convention.
        log.warning(
            "brief.health_record_load_failed",
            path=str(path),
            stage="read",
            error=read.detail,
            error_type=read.error_type,
        )
        return None
    text = read.text
    if not text.startswith("---"):
        return None
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if match is None:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        # Frontmatter present but not valid YAML → same no-record path,
        # same broken-vs-idle signal (stage distinguishes it from a read
        # failure above).
        log.warning(
            "brief.health_record_load_failed",
            path=str(path),
            stage="yaml",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return None
    if not isinstance(data, dict):
        return None
    return data


def _find_latest_bit_record(
    vault_path: Path,
    bit_dirs: tuple[str, ...] = ("run", "process"),
) -> Path | None:
    """Find the most recent Alfred BIT record in the vault.

    The BIT daemon writes ``vault/run/Alfred BIT {date}.md`` (since
    2026-06-12; ``vault/process/`` before that), so we glob for
    ``Alfred BIT *.md`` in every directory in ``bit_dirs`` and return
    the lexicographically-last FILENAME overall — since the dates are
    ISO-8601, lexicographic order is also chronological order. On a
    filename tie across directories (migration-overlap case), the
    earlier-listed directory wins (``run/``, the canonical home).
    """
    candidates: list[tuple[str, int, Path]] = []
    for dir_index, bit_dir in enumerate(bit_dirs):
        dir_path = vault_path / bit_dir
        if not dir_path.exists():
            continue
        for p in dir_path.glob("Alfred BIT *.md"):
            # Sort key: filename first (chronological), then negated
            # dir index so an earlier-listed dir sorts LAST on a
            # filename tie (we take the final element below).
            candidates.append((p.name, -dir_index, p))
    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    return candidates[-1][2]


def _read_state_latest(state_path: Path) -> dict[str, Any] | None:
    """Fall back to the BIT state file's most recent run."""
    if not state_path.exists():
        return None
    # Defensive read via the shared helper. The old ``(OSError,
    # json.JSONDecodeError)`` catch missed UnicodeDecodeError — a sibling of
    # JSONDecodeError under ValueError, NOT a subclass — so a non-UTF-8 state
    # file escaped and crashed the brief. Helper handles the read; the
    # json.loads catch below stays for JSON-syntax errors on a clean read.
    read = safe_read_section_file(state_path)
    if read.status is not SectionReadStatus.OK:
        # Load failure of the fallback state file → no-record path. Signal
        # it (ILB) — the state file existing-but-unreadable is broken, not
        # idle. Mirrors the sibling brief.watches_state_load_failed.
        log.warning(
            "brief.health_state_load_failed",
            path=str(state_path),
            stage="read",
            error=read.detail,
            error_type=read.error_type,
        )
        return None
    try:
        data = json.loads(read.text)
    except json.JSONDecodeError as exc:
        # Read cleanly but not valid JSON → same no-record path, same signal.
        log.warning(
            "brief.health_state_load_failed",
            path=str(state_path),
            stage="json",
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
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
            ``vault/run/Alfred BIT *.md`` (and ``vault/process/`` for
            pre-2026-06-12 legacy records).
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
            # Guard the second (body) read too. ``_parse_frontmatter`` just
            # read this file cleanly, but a race / transient I/O error here
            # would otherwise crash the whole brief (bare render). On a
            # body-read failure, render from the already-parsed frontmatter
            # with an empty body — ``_render_from_frontmatter`` falls back to
            # ``tool_counts`` when the body yields no per-tool lines.
            body_read = safe_read_section_file(record_path)
            if body_read.status is SectionReadStatus.OK:
                body = body_read.text
            else:
                # Frontmatter read cleanly but the body read failed (race /
                # transient I/O). We still render from the parsed frontmatter,
                # but the per-tool breakdown is lost — signal the partial
                # degrade rather than silently dropping to tool_counts.
                log.warning(
                    "brief.health_body_read_failed",
                    path=str(record_path),
                    error=body_read.detail,
                    error_type=body_read.error_type,
                )
                body = ""
            return _render_from_frontmatter(
                frontmatter,
                body,
                today_str,
                record_dir=record_path.parent.name,
            )

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
    record_dir: str = "process",
) -> str:
    """Format the Health section from a parsed BIT record.

    ``record_dir`` is the directory the record was actually found in —
    the "See full report" wikilink must point at the real location,
    whether that's the canonical ``run/`` or a legacy ``process/``
    record from before 2026-06-12.
    """
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
    lines.append(f"See full report: [[{record_dir}/{record_link}]]")
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
