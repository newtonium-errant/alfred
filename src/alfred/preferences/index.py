"""JSON index of active Shape A preferences — atomic rebuild.

The index is a cheap projection over the vault's preference records:
loader.py walks the directory, this module filters to Shape A
(action) records, projects the matcher dispatch into a flat list,
and writes the result atomically to ``data/operator_preferences.json``.

Consumer modules (curator, brief) read the index file rather than
walking the vault on every invocation. The index re-builds on:

- CLI invocation: ``alfred prefs rebuild-index``
- Daemon-side: each consumer cycle re-loads via ``load_active_preferences``
  directly when the index isn't sufficient (the index is an
  optimisation, not a contract — consumers may bypass it).

Atomic write: ``.tmp`` + rename, mirroring the state-file pattern
used across the rest of the codebase. Truncated index would silently
break gate dispatch; atomicity guarantees either old-snapshot or
new-snapshot, never half-written.

Schema (V1):

    {
      "generated_at": "<ISO 8601 UTC timestamp>",
      "instance": "<instance name passed by caller>",
      "vault_path": "<vault path projected from>",
      "active_preferences": [
        {
          "slug": "<filename stem>",
          "name": "<display name>",
          "shape": "action",
          "scope": "universal" | "instance",
          "applies_to_instance": <str | null>,
          "matcher": {"domain": "...", "rule": "...", "args": {...}},
          "source_session": "<wikilink>",
          "path": "<absolute path>"
        },
        ...
      ]
    }

Shape B records are intentionally NOT projected here — they're
consumed by the talker system-prompt assembly path which reads
``loader.load_active_preferences(shape="voice")`` directly. Mixing
both shapes in one index would force every reader to filter; the
two consumers have different cadences (talker is per-session, gate
consumers are per-record).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from .loader import Preference, load_active_preferences

log = structlog.get_logger(__name__)
_logging_log = logging.getLogger(__name__)


def _project_action(pref: Preference) -> dict[str, Any]:
    """Project one Shape A preference into the index schema dict."""
    return {
        "slug": pref.slug,
        "name": pref.name,
        "shape": pref.shape,
        "scope": pref.scope,
        "applies_to_instance": pref.applies_to_instance,
        "matcher": pref.matcher or {},
        "source_session": pref.source_session,
        "path": str(pref.path),
    }


def rebuild_index(
    vault_path: str | Path,
    output_path: str | Path,
    *,
    instance: str | None = None,
) -> dict[str, Any]:
    """Rebuild the operator-preferences index atomically.

    Args:
        vault_path: vault root to project from.
        output_path: where to write the index JSON. Parent directory
            is created if missing.
        instance: optional instance name (Salem / Hypatia / KAL-LE)
            to stamp into the index. Defaults to None when the caller
            doesn't have an instance handle (e.g. CLI usage without
            a configured instance).

    Returns:
        The index dict that was written (caller can inspect the
        ``active_preferences`` count without re-reading from disk).

    Atomic write: writes to ``<output>.tmp`` then renames over
    ``<output>``. POSIX rename guarantees the target either points at
    the old file or the new file but never a half-written one. The
    pattern matches state file writers across the codebase.

    Per ``feedback_intentionally_left_blank.md``: emits a
    ``preferences.index_rebuilt`` log every call with the
    ``count`` field, including the zero-preferences case. Silent "no
    preferences" is indistinguishable from "loader broken."
    """
    vault = Path(vault_path)
    output = Path(output_path)

    active = load_active_preferences(vault, shape="action")
    projected = [_project_action(p) for p in active]

    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "instance": instance,
        "vault_path": str(vault),
        "active_preferences": projected,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(output))

    log.info(
        "preferences.index_rebuilt",
        vault_path=str(vault),
        output_path=str(output),
        instance=instance,
        count=len(projected),
    )
    _logging_log.info(
        "preferences.index_rebuilt vault_path=%s output_path=%s "
        "instance=%s count=%d",
        str(vault), str(output), instance, len(projected),
    )
    return payload


def load_index(index_path: str | Path) -> dict[str, Any] | None:
    """Read a previously-rebuilt index from disk.

    Returns the parsed dict, or None if the file doesn't exist or
    parses as something other than a dict (forward-compat). Callers
    that get None should fall back to ``load_active_preferences``
    directly — the index is an optimisation, not a contract.

    Per ``feedback_intentionally_left_blank.md``: emits a
    ``preferences.index_missing`` log when the file isn't there so
    the operator-grep pattern surfaces the "rebuild never ran" case
    rather than silently degrading to no-preferences-loaded.
    """
    path = Path(index_path)
    if not path.exists():
        log.info(
            "preferences.index_missing",
            path=str(path),
            detail="index file not present — caller should fall back to loader",
        )
        _logging_log.info("preferences.index_missing path=%s", str(path))
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "preferences.index_read_failed",
            path=str(path),
            error=str(exc),
        )
        _logging_log.warning(
            "preferences.index_read_failed path=%s error=%s",
            str(path), str(exc),
        )
        return None
    if not isinstance(data, dict):
        log.warning(
            "preferences.index_malformed",
            path=str(path),
            type=type(data).__name__,
        )
        return None
    return data
