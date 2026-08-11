"""The append-only sidecar ledger — the batch's source of truth (D2).

The vault record is a RENDER of this file, regenerated wholesale each
checkpoint. That direction is deliberate and follows the scribe
precedent: accumulation lives in a sidecar outside the vault, the note
is derived, and the derivation is re-run rather than patched. It means a
half-written record can always be rebuilt, and it means the operator
editing the note cannot corrupt the underlying results.

Unlike scribe's ledger (a whole-file JSON document rewritten each pass)
this one is genuinely append-only JSONL, because rows arrive one image
at a time across many separate runs and rewriting the whole file per
image would make a crash mid-batch lose everything before it. The
append pattern follows ``scribe.negation_suppression._append_row``:
``flock`` on a STABLE separate lock file, never on the sink fd — a sink
that is ever rotated by ``os.replace`` would orphan the pre-replace
inode and the lock would protect nothing.

Idempotency is content-addressed: a row's ``item_id`` is the image's
content hash, and :func:`processed_item_ids` is the check the worker
runs before spending an API call. A replayed image is a no-op.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import structlog

log = structlog.get_logger(__name__)

#: Terminal row outcomes. ``ok`` means the model returned a result for
#: this image; ``quarantined`` means the image was processed but the
#: model could not read it confidently and the operator must look.
OUTCOME_OK = "ok"
OUTCOME_QUARANTINED = "quarantined"


def _lock_path(sink_path: Path) -> Path:
    return sink_path.with_suffix(sink_path.suffix + ".lock")


@contextmanager
def _sink_lock(sink_path: Path) -> Iterator[None]:
    """Serialize appends via ``flock`` on a stable sidecar lock file.

    Best-effort with a LOUD warning rather than a hard failure: losing
    the lock degrades to a possible interleave, whereas raising would
    lose a completed (already paid-for) model result. The warning is
    what makes the degradation visible.
    """
    lp = _lock_path(sink_path)
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        with open(lp, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        return
    except OSError as e:
        log.warning(
            "batch.ledger.lock_skipped",
            path=sink_path.name,
            error_class=type(e).__name__,
            detail="appending without the lock — a concurrent writer "
                   "could interleave; the row is still written",
        )
    yield


def build_row(
    *,
    item_id: str,
    filename: str,
    outcome: str,
    result: str,
    model: str = "",
    note: str = "",
    when: datetime | None = None,
) -> dict[str, Any]:
    """One ledger row. ``item_id`` is the image's content hash."""
    ts = (when or datetime.now(timezone.utc)).isoformat()
    return {
        "item_id": item_id,
        "filename": filename,
        "outcome": outcome,
        "result": result,
        "model": model,
        "note": note,
        "processed_at": ts,
    }


def append_row(sink_path: Path | str, row: dict[str, Any]) -> None:
    """Append one JSON line. Creates the parent tree if needed."""
    p = Path(sink_path)
    with _sink_lock(p):
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            # The row is the record of a paid-for model call; make it
            # durable before the caller proceeds to the vault write.
            f.flush()
            os.fsync(f.fileno())


def load_rows(sink_path: Path | str) -> list[dict[str, Any]]:
    """Read every well-formed row. A torn tail line is skipped, not fatal.

    An append-only file crashed mid-write leaves a partial final line.
    Dropping just that line keeps every completed result usable, which
    matters because those results cost money to produce.
    """
    p = Path(sink_path)
    if not p.is_file():
        return []
    rows: list[dict[str, Any]] = []
    skipped = 0
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001 — unreadable ledger degrades to empty
        log.warning(
            "batch.ledger.unreadable",
            path=p.name,
            error_class=type(e).__name__,
        )
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001 — torn tail line
            skipped += 1
            continue
        if isinstance(obj, dict) and obj.get("item_id"):
            rows.append(obj)
        else:
            skipped += 1
    if skipped:
        log.warning(
            "batch.ledger.rows_skipped",
            path=p.name,
            skipped=skipped,
            kept=len(rows),
            detail="malformed or partial lines skipped; completed "
                   "results are preserved",
        )
    return rows


def processed_item_ids(rows: list[dict[str, Any]]) -> set[str]:
    """The idempotency key set — item ids that already have a result."""
    return {str(r.get("item_id")) for r in rows if r.get("item_id")}


def has_processed(rows: list[dict[str, Any]], item_id: str) -> bool:
    return item_id in processed_item_ids(rows)


__all__ = [
    "OUTCOME_OK",
    "OUTCOME_QUARANTINED",
    "append_row",
    "build_row",
    "has_processed",
    "load_rows",
    "processed_item_ids",
]
