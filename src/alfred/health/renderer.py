"""Render HealthReport → human text (streaming) and JSON (batch).

Two output surfaces:

* :func:`render_human` yields one line at a time. The caller (``alfred
  check``, the brief integration) prints each line as it arrives so
  a slow check doesn't leave the user staring at a blank terminal.
  The caller can also pass a ``write`` callable; when present we
  push each line through it instead of returning a generator.

* :func:`render_json` is a straightforward batch serializer. No
  streaming is useful for JSON — callers almost always want the full
  object for piping to ``jq`` or storing in a BIT record.

The renderers are deliberately independent of how the ``HealthReport``
was produced. Unit tests can build a ``HealthReport`` by hand and
assert over both outputs.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable, Iterator
from typing import Any

from .types import HealthReport, Status, ToolHealth


_STATUS_GLYPH: dict[Status, str] = {
    Status.OK: "[ OK ]",
    Status.WARN: "[WARN]",
    Status.FAIL: "[FAIL]",
    Status.SKIP: "[SKIP]",
}


def _status_label(status: Status) -> str:
    """Human-readable single-token label for a status value."""
    return _STATUS_GLYPH.get(status, f"[{status.value.upper()}]")


def render_human(
    report: HealthReport,
    write: Callable[[str], None] | None = None,
) -> Iterator[str] | None:
    """Render a report as human-readable lines.

    If ``write`` is given, each line is pushed through it (and the
    function returns ``None``). Otherwise a generator of lines is
    returned — callers can iterate to stream.

    Formatting follows the brief section style: one header line per
    tool with its rollup status, followed by indented results. Totals
    are summarized at the bottom.
    """
    lines = list(_emit_lines(report))
    if write is None:
        return iter(lines)
    for line in lines:
        write(line)
    return None


def _emit_lines(report: HealthReport) -> Iterator[str]:
    """Build the human-text lines, generator-style."""
    overall = _status_label(report.overall_status)
    yield f"Alfred BIT ({report.mode}) — {overall}"
    yield f"  started:  {report.started_at}"
    yield f"  finished: {report.finished_at}"
    if report.elapsed_ms is not None:
        yield f"  elapsed:  {report.elapsed_ms:.0f} ms"
    yield ""
    if not report.tools:
        yield "  (no tools checked)"
        return
    for tool_health in report.tools:
        yield from _emit_tool_lines(tool_health)
        yield ""
    counts = _status_counts(report)
    yield (
        f"Totals: ok={counts[Status.OK]} "
        f"warn={counts[Status.WARN]} "
        f"fail={counts[Status.FAIL]} "
        f"skip={counts[Status.SKIP]}"
    )


def _emit_tool_lines(tool_health: ToolHealth) -> Iterator[str]:
    """Build the lines for one tool block."""
    label = _status_label(tool_health.status)
    header = f"{label} {tool_health.tool}"
    if tool_health.elapsed_ms is not None:
        header += f"  ({tool_health.elapsed_ms:.0f} ms)"
    if tool_health.detail:
        header += f" — {tool_health.detail}"
    yield header
    for result in tool_health.results:
        sub_label = _status_label(result.status)
        line = f"    {sub_label} {result.name}"
        if result.latency_ms is not None:
            line += f"  ({result.latency_ms:.0f} ms)"
        if result.detail:
            line += f" — {result.detail}"
        yield line


def _status_counts(report: HealthReport) -> dict[Status, int]:
    """Sum of tool-level statuses across the report.

    We count at the tool level (not the individual check level) so the
    summary line matches what a reader scanning tool headers would see.
    """
    counts = {Status.OK: 0, Status.WARN: 0, Status.FAIL: 0, Status.SKIP: 0}
    for tool_health in report.tools:
        counts[tool_health.status] = counts.get(tool_health.status, 0) + 1
    return counts


def render_json(report: HealthReport) -> str:
    """Serialize a report to a JSON string.

    Returns pretty-printed JSON (indent=2) because these reports get
    stored in vault records where human readability of the .md source
    matters. Add ``.loads()`` on the caller if they want a dict.
    """
    return json.dumps(_as_jsonable(report), indent=2, default=str)


def _as_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses / enums / etc. into JSON types."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        result: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            result[f.name] = _as_jsonable(getattr(obj, f.name))
        return result
    if isinstance(obj, Status):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [_as_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _as_jsonable(v) for k, v in obj.items()}
    return obj
