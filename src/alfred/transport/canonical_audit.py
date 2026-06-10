"""Append-only JSONL audit log for canonical record reads.

Every ``GET /canonical/<type>/<name>`` call appends one line to
``transport.canonical.audit_log_path`` (default
``./data/canonical_audit.jsonl``). Line shape:

.. code-block:: json

    {
      "ts": "2026-04-20T21:00:00+00:00",
      "peer": "kal-le",
      "type": "person",
      "name": "Andrew Newton",
      "requested": ["name", "email"],
      "granted": ["name", "email"],
      "denied": ["phone", "addresses"],
      "correlation_id": "ab12cd34ef56..."
    }

Deliberately JSONL rather than a structured DB — audit trails are
append-heavy, grep-friendly, and survive any Alfred restart. Operators
inspect via ``alfred transport tail --peer kal-le`` (c9).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def append_audit(
    audit_log_path: str | Path,
    *,
    peer: str,
    record_type: str,
    name: str,
    requested: list[str],
    granted: list[str],
    denied: list[str],
    correlation_id: str = "",
    ts: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one audit entry to the JSONL log.

    Write semantics:
      - Creates parent directory if missing.
      - Single ``open(..., "a")`` write per call — no in-memory buffer,
        so even a daemon crash mid-request preserves everything up to
        the last successful call.
      - Never raises; disk errors log-and-continue. Audit failures must
        not propagate to the caller and interrupt the canonical read.

    ``extra`` (P1, 2026-06-09) — an OPTIONAL dict of additional fields
    merged into the entry, for the filtered-query (``/peer/search``)
    audit. By-name callers omit it, so their entries are byte-identical
    to pre-P1. Filtered callers pass ``{"kind": "search", "filter": [...],
    "sort": {...}, "limit": N, "match_count": M, "denied_dims": [...]}``.
    Core keys (ts/peer/type/name/requested/granted/denied/correlation_id)
    are written first and CANNOT be overwritten by ``extra`` (the core
    fields are re-applied after the merge), so an ``extra`` carrying a
    conflicting ``peer`` can't corrupt the audit identity.
    """
    if not audit_log_path:
        # Audit explicitly disabled — skip. Used by tests that don't
        # care about the audit trail; prod configs always set this.
        return
    path = Path(audit_log_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Log layer will pick this up; can't raise from audit.
        return

    entry: dict[str, Any] = {}
    # Merge the optional extra fields FIRST so the core identity fields
    # below overwrite any conflicting key — ``extra`` can add (kind,
    # filter, match_count, ...) but can never corrupt peer/type/name.
    if isinstance(extra, dict):
        entry.update(extra)
    entry.update({
        "ts": (ts or datetime.now(timezone.utc)).isoformat(),
        "peer": peer,
        "type": record_type,
        "name": name,
        "requested": list(requested),
        "granted": list(granted),
        "denied": list(denied),
        "correlation_id": correlation_id,
    })
    line = json.dumps(entry, default=str) + "\n"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        # Same rationale — we don't break the canonical read if the
        # audit log is unwriteable. The caller's logger will surface
        # the underlying FS error separately.
        return


def read_audit(audit_log_path: str | Path) -> list[dict[str, Any]]:
    """Read the audit log into a list of dicts.

    Purely for tests and CLI inspection. Production callers should
    grep / tail the file directly.
    """
    path = Path(audit_log_path)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# Re-exported so CLI + handler modules only import one thing.
__all__ = ["append_audit", "read_audit"]


# Tiny convenience for os/path abstraction callers who want to join
# the audit path from raw config without instantiating CanonicalConfig.
def resolve_audit_path(
    raw: dict[str, Any],
    default: str = "./data/canonical_audit.jsonl",
) -> str:
    """Pull ``transport.canonical.audit_log_path`` out of a raw config dict."""
    transport = raw.get("transport", {}) or {}
    canonical = transport.get("canonical", {}) or {}
    path = canonical.get("audit_log_path") or default
    return os.path.expanduser(str(path))
