"""Outbound-failure detector — emit queue rows from session frontmatter.

Reads ``vault/session/*.md`` records, looks for the
``outbound_failures`` field (shipped in commit ``c0685ba``), and emits
a ``pending_items`` JSONL row for each failure that hasn't been
emitted yet. Tracks emitted (session_id, turn_index) tuples in a
small JSON state file so a re-scan doesn't duplicate.

The session frontmatter shape (per ``c0685ba``):

```yaml
outbound_failures:
  - turn_index: 7
    timestamp: "2026-04-28T16:00:57+00:00"
    error: "Message is too long"
    length: 4852
    chunks_attempted: 1
    chunks_sent: 0
    delivered: false
```

The detector emits one queue entry per failure. The ``deliver_text``
action plan carries the originating session id + turn_index so the
executor can re-fetch the assistant turn's text from the session
record at resolution time. We deliberately do NOT inline the text
into the queue row — keeping it in the session record means the
queue stays compact and the audit trail (session record is the
source of truth for what was meant to be sent) stays uncontested.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter
import structlog

from .queue import (
    ActionPlan,
    CATEGORY_OUTBOUND_FAILURE,
    PendingItem,
    ResolutionOption,
    append_item,
    new_item_id,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# State (already-emitted tuples)
# ---------------------------------------------------------------------------


def _load_emitted_state(state_path: str | Path) -> set[str]:
    """Return the set of ``(session_id, turn_index)`` tuples already emitted.

    Stored as a JSON file with shape ``{"emitted": ["<sid>:<idx>", ...]}``
    so the format stays human-readable. Missing file → empty set.
    """
    path = Path(state_path)
    if not path.exists():
        return set()
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw or "{}")
    except (OSError, json.JSONDecodeError):
        return set()
    emitted = data.get("emitted") or []
    if not isinstance(emitted, list):
        return set()
    return {str(x) for x in emitted}


def _save_emitted_state(state_path: str | Path, emitted: set[str]) -> bool:
    """Persist the emitted set, atomic write. Returns True on success."""
    path = Path(state_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    payload = {"emitted": sorted(emitted)}
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _emit_key(session_id: str, turn_index: int) -> str:
    """Stable key for the emitted-state set."""
    return f"{session_id}:{turn_index}"


# ---------------------------------------------------------------------------
# Render the context shown to Andrew in the Daily Sync
# ---------------------------------------------------------------------------


def _format_context(
    session_id: str,
    failure: dict[str, Any],
) -> str:
    """Compose the human-readable context string (≤500 chars).

    Mirrors the spec example: ``"Yesterday at HH:MM ADT I tried to
    send you a N-char response. Telegram rejected for ERROR. Full
    text in session/SHORT-ID."`` Falls back to a compact form when
    timestamp parsing fails.
    """
    short_id = (session_id or "unknown").split("-")[0] or "unknown"
    length = failure.get("length") or 0
    error = str(failure.get("error") or "unknown error")[:200]
    timestamp_raw = str(failure.get("timestamp") or "")
    when_phrase = "earlier"
    try:
        ts = datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
        # Local-time formatting deferred — Daily Sync runs in Halifax,
        # but per-session timestamps stay UTC for audit. The phrase
        # is built from the raw UTC iso string for consistency.
        when_phrase = f"on {ts.strftime('%Y-%m-%d at %H:%M UTC')}"
    except (ValueError, TypeError):
        pass
    context = (
        f"{when_phrase}, an outbound reply ({length} chars) failed to "
        f"deliver via Telegram. Error: {error}. "
        f"Full text in session/{short_id}."
    )
    # Hard cap at 500 chars per the spec.
    if len(context) > 500:
        context = context[:497] + "..."
    return context


def _build_resolution_options(
    session_id: str,
    turn_index: int,
) -> list[ResolutionOption]:
    """Two options per outbound_failure: noted, show_me.

    ``noted`` carries no action plan (the resolver just flips status).
    ``show_me`` carries a structured ``deliver_text`` plan that the
    Phase 1 executor implements.
    """
    return [
        ResolutionOption(
            id="noted",
            label="Noted, no action needed",
            action_plan=None,
        ),
        ResolutionOption(
            id="show_me",
            label="Show me what was supposed to come",
            action_plan=ActionPlan(
                type="deliver_text",
                params={
                    "source": "session_record",
                    "session_id": session_id,
                    "turn_index": int(turn_index),
                },
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def _iter_session_records(
    vault_path: Path,
    session_subpath: str,
) -> list[Path]:
    """Return every ``*.md`` under ``vault/<session_subpath>/``.

    Skips Obsidian template files (``_*``) and dotfiles.
    """
    base = vault_path / session_subpath
    if not base.exists() or not base.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(base.glob("*.md")):
        if path.name.startswith("_") or path.name.startswith("."):
            continue
        out.append(path)
    return out


def _extract_session_id(post: frontmatter.Post, file_path: Path) -> str:
    """Pull the session id out of a session record.

    Looks at ``telegram.session_id`` first (canonical for talker
    sessions), then falls back to the file stem so non-talker session
    records still get a stable key.
    """
    fm = dict(post.metadata or {})
    telegram = fm.get("telegram") or {}
    if isinstance(telegram, dict):
        sid = telegram.get("session_id")
        if isinstance(sid, str) and sid:
            return sid
    return file_path.stem


def scan_and_emit(
    *,
    vault_path: Path,
    queue_path: str | Path,
    state_path: str | Path,
    instance_name: str,
    session_subpath: str = "session",
) -> dict[str, Any]:
    """Walk the session vault, emit queue rows for new failures.

    Returns a summary dict::

        {
          "scanned_records": <int>,
          "emitted": <int>,
          "skipped_already_emitted": <int>,
          "errors": [<str>, ...]
        }

    Idempotent: a second run over the same vault state emits zero
    new rows. The state file is the discriminator.
    """
    summary: dict[str, Any] = {
        "scanned_records": 0,
        "emitted": 0,
        "skipped_already_emitted": 0,
        "errors": [],
    }

    emitted_set = _load_emitted_state(state_path)
    new_emissions: list[str] = []

    for record_path in _iter_session_records(vault_path, session_subpath):
        summary["scanned_records"] += 1
        try:
            post = frontmatter.load(str(record_path))
        except Exception as exc:  # noqa: BLE001
            summary["errors"].append(
                f"{record_path.name}: parse failed: {exc}"
            )
            continue
        fm = dict(post.metadata or {})
        failures = fm.get("outbound_failures") or []
        if not isinstance(failures, list) or not failures:
            continue
        session_id = _extract_session_id(post, record_path)

        for failure in failures:
            if not isinstance(failure, dict):
                continue
            try:
                turn_index = int(failure.get("turn_index", -1))
            except (TypeError, ValueError):
                turn_index = -1
            if turn_index < 0:
                summary["errors"].append(
                    f"{record_path.name}: bad turn_index"
                )
                continue
            key = _emit_key(session_id, turn_index)
            if key in emitted_set:
                summary["skipped_already_emitted"] += 1
                continue

            item = PendingItem(
                id=new_item_id(),
                category=CATEGORY_OUTBOUND_FAILURE,
                created_at=datetime.now(timezone.utc).isoformat(),
                created_by_instance=instance_name or "unknown",
                session_id=session_id,
                context=_format_context(session_id, failure),
                resolution_options=_build_resolution_options(
                    session_id, turn_index,
                ),
            )
            ok = append_item(queue_path, item)
            if not ok:
                summary["errors"].append(
                    f"{record_path.name}: append_item failed"
                )
                continue
            emitted_set.add(key)
            new_emissions.append(key)
            summary["emitted"] += 1
            log.info(
                "pending_items.outbound_failure.emitted",
                session_id=session_id,
                turn_index=turn_index,
                length=failure.get("length"),
                instance=instance_name,
                item_id=item.id,
            )

    if new_emissions:
        if not _save_emitted_state(state_path, emitted_set):
            summary["errors"].append(
                "save_emitted_state failed — re-emission risk on next scan"
            )

    return summary


__all__ = ["scan_and_emit"]
