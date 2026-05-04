"""Friction analyzer — KAL-LE Daily Sync K3 c1.

Reads KAL-LE's ``bash_exec.jsonl`` audit log, scores friction events
along three categories, and appends to a friction-event JSONL file
that the K3 c2 section provider surfaces in the Daily Sync.

## Why this exists

Per ``project_kalle_daily_sync.md``: KAL-LE's 09:00 ADT Daily Sync
should surface things-Andrew-may-want-to-decide from the previous
day's coding sessions. The bash_exec audit log carries everything
KAL-LE actually ran via Telegram tool-use, which is the highest-
fidelity friction signal we have without crossing into target-repo
parsing or pytest-output scraping (deferred ship).

## Three friction categories (this c1 ships all three)

1. **failed_pattern** — same command-prefix failed N+ times in 24h.
   Suggests the prefix needs allowlisting, the binary is missing,
   or the command shape is wrong. Threshold default: 3.

2. **repeated_pattern** — same exact command succeeded N+ times in
   24h. Suggests scripting / aliasing / Make-target opportunity.
   Threshold default: 5.

3. **missing_tool** — bash_exec audit logged ``reason=command_not_found``
   for an attempted invocation. Single occurrence is enough; missing
   tool is binary state. The OS reports this when ``execvp`` can't
   find the binary on PATH.

## Schema notes — bash_exec.jsonl is what we get

The audit log shape (per ``alfred.telegram.bash_exec._audit_append``):

    {
      "ts": "<ISO UTC>",
      "command": str,           # full command string
      "cwd": str,
      "exit_code": int,         # 0=success, -1=rejected/timeout, other=fail
      "duration_ms": int,
      "session_id": str,
      "reason": str,            # gate name when -1; "" otherwise
    }

NOTE the audit log does **not** include ``stderr``. The K3 spec
proposed parsing stderr for "command not found" patterns; that's not
possible from this log. Instead, missing-tool detection uses the
``reason="command_not_found"`` gate signal, which is emitted exactly
when ``FileNotFoundError`` fires on ``execvp`` — semantically the
same outcome (binary not on PATH) without needing stderr.

## Idempotency via deterministic event_id

Each friction event gets a hash of (kind + grouping-key + day-bucket-
local-date). Re-running the analyzer on the same data → same hash →
existing-events check skips the duplicate. Day-bucket uses local-
timezone date, not UTC, so an event detected late on 2026-05-04 ADT
doesn't collide with one detected on 2026-05-05 ADT after midnight.

## Output — append-only friction log

One JSONL row per detected event. Schema:

    {
      "event_id": "<sha256>",      # idempotency key
      "kind": "failed_pattern" | "repeated_pattern" | "missing_tool",
      "detected_at": "<ISO UTC>",
      "day_bucket": "YYYY-MM-DD",  # local-tz date when detected
      "prefix": str,               # for failed_pattern
      "command": str,              # for repeated_pattern
      "tool": str,                 # for missing_tool
      "count": int,                # how many bash_exec entries triggered
      "last_failure": str,         # for failed_pattern
      "sample_command": str,       # for failed_pattern
      "failed_command": str,       # for missing_tool
      "suggestion": str,           # human-readable next step
      "surfaced_at": null,         # set by section provider after render
    }
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Audit log reader — schema-tolerant
# ---------------------------------------------------------------------------


@dataclass
class AuditEntry:
    """Parsed bash_exec.jsonl row.

    Schema-tolerant: malformed rows or rows missing required fields
    are skipped (logged at info), not raised. Mirrors the load-time
    schema-tolerance contract from CLAUDE.md.
    """

    ts: datetime  # tz-aware UTC
    command: str
    cwd: str
    exit_code: int
    duration_ms: int
    session_id: str
    reason: str

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "AuditEntry | None":
        try:
            ts_raw = row.get("ts")
            if not isinstance(ts_raw, str) or not ts_raw:
                return None
            # Defensive ISO parse — bash_exec emits with explicit TZ
            # offset; tolerate the ``Z`` suffix shape too.
            ts_str = ts_raw.replace("Z", "+00:00") if ts_raw.endswith("Z") else ts_raw
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            command = str(row.get("command") or "")
            if not command:
                return None
            return cls(
                ts=ts,
                command=command,
                cwd=str(row.get("cwd") or ""),
                exit_code=int(row.get("exit_code", 0)),
                duration_ms=int(row.get("duration_ms", 0)),
                session_id=str(row.get("session_id") or ""),
                reason=str(row.get("reason") or ""),
            )
        except (TypeError, ValueError, KeyError):
            return None


def load_audit_entries(audit_path: Path) -> list[AuditEntry]:
    """Read every bash_exec.jsonl row, skipping malformed lines.

    Returns ``[]`` when the file is missing — not an error case
    (KAL-LE may not have run any bash_exec calls yet).
    """
    if not audit_path.is_file():
        return []
    entries: list[AuditEntry] = []
    try:
        with audit_path.open("r", encoding="utf-8") as fh:
            for line_num, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    log.info(
                        "friction_analyzer.audit_skip",
                        line=line_num, error=str(exc),
                    )
                    continue
                entry = AuditEntry.from_dict(row)
                if entry is not None:
                    entries.append(entry)
    except OSError as exc:
        log.warning(
            "friction_analyzer.audit_read_failed",
            path=str(audit_path), error=str(exc),
        )
        return []
    return entries


# ---------------------------------------------------------------------------
# Tokenization — shlex with whitespace fallback
# ---------------------------------------------------------------------------


def _tokenize_command(command: str) -> list[str]:
    """Return the command's argv-style tokens.

    Uses ``shlex.split`` (POSIX rules, matching what bash_exec passes
    to ``asyncio.create_subprocess_exec``). Falls back to a guarded
    whitespace split when shlex raises (typically unbalanced quotes)
    so a malformed command in the audit log doesn't crash the
    analyzer AND doesn't leak the malformed token into the prefix
    key.

    Why the truncate-at-first-quote step in the fallback: the binary
    name is what prefix grouping cares about. A quote-bearing token
    is malformed and shouldn't propagate into the prefix; otherwise
    ``echo "unbalanced`` would group under prefix ``echo "unbalanced``
    and never match its well-formed sibling ``echo "balanced text"``
    (which shlex parses to prefix ``echo``). Different prefix keys
    → never hits the failed-pattern threshold → friction event never
    fires.

    Tokens before the first quote-bearing token are returned as-is;
    everything from the malformed token onward is dropped. Empty
    list when the very first token is quote-bearing (e.g., a leading
    ``"`` with no preceding binary).
    """
    try:
        return shlex.split(command)
    except ValueError:
        tokens = command.split()
        for i, t in enumerate(tokens):
            if '"' in t or "'" in t:
                return tokens[:i]
        return tokens


def _command_prefix(command: str, *, max_tokens: int = 2) -> str:
    """Return the first ``max_tokens`` tokens joined by space.

    ``"uv sync --no-cache"`` → ``"uv sync"``.
    ``"pytest"`` → ``"pytest"`` (single token; max_tokens caps but
    doesn't pad).
    Empty string when the command has no parseable tokens.

    Compound commands like ``"cd X && Y"`` collapse to ``"cd X"``
    under the 2-token rule. That's coarser than ideal — Y might be
    the actually-failing binary — but the alternative (parsing
    shell operators) is well outside scope. Operator can spot the
    "cd X" prefix in the friction queue and read the sample_command
    field for the full text.
    """
    tokens = _tokenize_command(command)
    if not tokens:
        return ""
    return " ".join(tokens[:max_tokens])


# ---------------------------------------------------------------------------
# Day-bucket helper — local-tz date, not UTC
# ---------------------------------------------------------------------------


def _local_day_bucket(now_utc: datetime, tz_name: str) -> str:
    """Return the local-tz date as ``YYYY-MM-DD``.

    Used as the third component of every event_id hash so
    "uv sync failed 4 times" detected on Mon doesn't collide with the
    same prefix detected on Tue. Local-tz (not UTC) so a late-evening
    detection doesn't roll over the bucket mid-session.
    """
    tz = ZoneInfo(tz_name)
    return now_utc.astimezone(tz).date().isoformat()


# ---------------------------------------------------------------------------
# Event idempotency
# ---------------------------------------------------------------------------


def _event_id(*, kind: str, key: str, day_bucket: str) -> str:
    """Deterministic event_id for idempotency.

    Hash of ``kind|key|day_bucket`` → sha256 hex digest (truncated to
    16 chars for readable JSONL rows). Re-running the analyzer on the
    same input produces the same event_id, which the existing-events
    check uses to skip duplicates.
    """
    payload = f"{kind}|{key}|{day_bucket}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def load_existing_event_ids(log_path: Path) -> set[str]:
    """Read the friction log and return the set of existing event_ids.

    Returns empty set when the log doesn't exist (first-run case).
    Malformed rows are skipped.
    """
    if not log_path.is_file():
        return set()
    ids: set[str] = set()
    try:
        with log_path.open("r", encoding="utf-8") as fh:
            for line_num, raw in enumerate(fh, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    log.info(
                        "friction_analyzer.log_skip",
                        line=line_num, error=str(exc),
                    )
                    continue
                eid = row.get("event_id")
                if isinstance(eid, str) and eid:
                    ids.add(eid)
    except OSError as exc:
        log.warning(
            "friction_analyzer.log_read_failed",
            path=str(log_path), error=str(exc),
        )
        return set()
    return ids


def append_events(log_path: Path, events: list[dict[str, Any]]) -> None:
    """Append friction events to the log JSONL. No-op when events is empty."""
    if not events:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# Detection — three categories
# ---------------------------------------------------------------------------


def detect_failed_patterns(
    entries: list[AuditEntry],
    *,
    threshold: int,
    day_bucket: str,
    now_utc: datetime,
) -> list[dict[str, Any]]:
    """Group failed entries by command prefix; emit events for prefixes
    seen ``threshold+`` times in the window.

    "Failed" = ``exit_code != 0``. That covers gate denials (-1),
    OS errors (-1), normal command failures (1+), and timeouts (-1).
    All four are friction from the operator's perspective.

    Excludes ``reason="command_not_found"`` from this category — those
    are routed to ``detect_missing_tools`` instead. Otherwise the same
    audit row would surface as both "failed pattern: rg" AND "missing
    tool: rg" which is redundant noise.
    """
    by_prefix: dict[str, list[AuditEntry]] = defaultdict(list)
    for entry in entries:
        if entry.exit_code == 0:
            continue
        if entry.reason == "command_not_found":
            continue
        prefix = _command_prefix(entry.command)
        if not prefix:
            continue
        by_prefix[prefix].append(entry)

    events: list[dict[str, Any]] = []
    for prefix, hits in by_prefix.items():
        if len(hits) < threshold:
            continue
        # Sort newest-first so last_failure + sample_command pick up
        # the freshest entry.
        hits.sort(key=lambda e: e.ts, reverse=True)
        sample = hits[0]
        suggestion = (
            f"`{prefix}` failed {len(hits)} times in the last "
            f"window — consider adding to bash_exec allowlist, "
            f"checking the binary install, or fixing the command shape"
        )
        events.append({
            "event_id": _event_id(
                kind="failed_pattern", key=prefix, day_bucket=day_bucket,
            ),
            "kind": "failed_pattern",
            "detected_at": now_utc.isoformat(),
            "day_bucket": day_bucket,
            "prefix": prefix,
            "count": len(hits),
            "last_failure": sample.ts.isoformat(),
            "sample_command": sample.command,
            "suggestion": suggestion,
            "surfaced_at": None,
        })
    return events


def detect_repeated_patterns(
    entries: list[AuditEntry],
    *,
    threshold: int,
    day_bucket: str,
    now_utc: datetime,
) -> list[dict[str, Any]]:
    """Group SUCCESSFUL entries by exact command equality; emit events
    for commands seen ``threshold+`` times in the window.

    Exact-equality grouping (not prefix) so ``pytest tests/foo.py -v``
    and ``pytest tests/bar.py -v`` stay separate even though they
    share a prefix — the actionable insight is "this exact invocation
    repeats", not "you use pytest a lot".
    """
    counts = Counter(e.command for e in entries if e.exit_code == 0)
    events: list[dict[str, Any]] = []
    for command, count in counts.items():
        if count < threshold:
            continue
        suggestion = (
            f"You ran `{command}` {count} times in the last window "
            f"— consider scripting, aliasing, or a Make target"
        )
        events.append({
            "event_id": _event_id(
                kind="repeated_pattern", key=command, day_bucket=day_bucket,
            ),
            "kind": "repeated_pattern",
            "detected_at": now_utc.isoformat(),
            "day_bucket": day_bucket,
            "command": command,
            "count": count,
            "suggestion": suggestion,
            "surfaced_at": None,
        })
    return events


# Match the trailing portion of "command not found" stderr text the
# OS would emit, to extract the tool name when bash_exec stored it.
# Not used for detection (we use ``reason==command_not_found``); kept
# for future stderr-aware analyzers.
_NOT_FOUND_TOOL_RE = re.compile(r"^\s*(?P<tool>\S+)")


def detect_missing_tools(
    entries: list[AuditEntry],
    *,
    day_bucket: str,
    now_utc: datetime,
) -> list[dict[str, Any]]:
    """Find audit entries where ``reason=="command_not_found"`` and
    emit one event per distinct tool name (deduplicated within day).

    The OS-reported command_not_found is a definitive signal — no
    threshold needed. Single occurrence is enough.
    """
    seen_tools: set[str] = set()
    events: list[dict[str, Any]] = []
    # Newest-first so per-tool sample shows the most recent attempt.
    sorted_entries = sorted(entries, key=lambda e: e.ts, reverse=True)
    for entry in sorted_entries:
        if entry.reason != "command_not_found":
            continue
        tokens = _tokenize_command(entry.command)
        if not tokens:
            continue
        tool = tokens[0]
        if tool in seen_tools:
            continue
        seen_tools.add(tool)
        suggestion = (
            f"`{tool}` is not installed (or not on KAL-LE's PATH) — "
            f"install in KAL-LE's venv or system, or replace with "
            f"a present alternative"
        )
        events.append({
            "event_id": _event_id(
                kind="missing_tool", key=tool, day_bucket=day_bucket,
            ),
            "kind": "missing_tool",
            "detected_at": now_utc.isoformat(),
            "day_bucket": day_bucket,
            "tool": tool,
            "failed_command": entry.command,
            "suggestion": suggestion,
            "surfaced_at": None,
        })
    return events


# ---------------------------------------------------------------------------
# Window filter
# ---------------------------------------------------------------------------


def filter_window(
    entries: list[AuditEntry],
    *,
    window_hours: int,
    now_utc: datetime,
) -> list[AuditEntry]:
    """Return entries within the last ``window_hours`` of ``now_utc``."""
    cutoff = now_utc - timedelta(hours=window_hours)
    return [e for e in entries if e.ts >= cutoff]


# ---------------------------------------------------------------------------
# Top-level orchestration — used by the CLI + the daemon
# ---------------------------------------------------------------------------


@dataclass
class FrictionRunResult:
    """Outcome summary returned by :func:`run_friction_analysis`.

    ``events`` is the list of events ACTUALLY appended (post-dedup);
    ``skipped`` is the count of detections that matched an existing
    event_id and were dropped by the idempotency gate.
    """

    day_bucket: str
    events: list[dict[str, Any]] = field(default_factory=list)
    skipped: int = 0
    audit_entries_scanned: int = 0
    audit_entries_in_window: int = 0


def run_friction_analysis(
    audit_log_path: Path,
    log_path: Path,
    *,
    failed_pattern_threshold: int = 3,
    repeated_pattern_threshold: int = 5,
    window_hours: int = 24,
    schedule_timezone: str = "America/Halifax",
    now_utc: datetime | None = None,
) -> FrictionRunResult:
    """End-to-end friction analysis: read audit → detect → dedup → append.

    Args:
        audit_log_path: Path to bash_exec.jsonl (KAL-LE's audit log).
        log_path: Path to the friction-event JSONL output.
        failed_pattern_threshold: Min failures-of-same-prefix to surface.
        repeated_pattern_threshold: Min successes-of-same-command to surface.
        window_hours: Lookback window for both pattern categories.
        schedule_timezone: TZ used to compute the local-date day-bucket.
        now_utc: Override for tests. Defaults to current UTC.

    Returns: :class:`FrictionRunResult` summary.
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    elif now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)

    day_bucket = _local_day_bucket(now_utc, schedule_timezone)
    all_entries = load_audit_entries(audit_log_path)
    in_window = filter_window(
        all_entries, window_hours=window_hours, now_utc=now_utc,
    )

    detected: list[dict[str, Any]] = []
    detected.extend(detect_failed_patterns(
        in_window,
        threshold=failed_pattern_threshold,
        day_bucket=day_bucket,
        now_utc=now_utc,
    ))
    detected.extend(detect_repeated_patterns(
        in_window,
        threshold=repeated_pattern_threshold,
        day_bucket=day_bucket,
        now_utc=now_utc,
    ))
    detected.extend(detect_missing_tools(
        in_window,
        day_bucket=day_bucket,
        now_utc=now_utc,
    ))

    existing_ids = load_existing_event_ids(log_path)
    fresh_events = [e for e in detected if e["event_id"] not in existing_ids]
    skipped = len(detected) - len(fresh_events)

    append_events(log_path, fresh_events)

    return FrictionRunResult(
        day_bucket=day_bucket,
        events=fresh_events,
        skipped=skipped,
        audit_entries_scanned=len(all_entries),
        audit_entries_in_window=len(in_window),
    )


__all__ = [
    "AuditEntry",
    "FrictionRunResult",
    "append_events",
    "detect_failed_patterns",
    "detect_missing_tools",
    "detect_repeated_patterns",
    "filter_window",
    "load_audit_entries",
    "load_existing_event_ids",
    "run_friction_analysis",
]
