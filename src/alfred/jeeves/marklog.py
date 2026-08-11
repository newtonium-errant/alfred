"""The local mark-down log — the cheap verb's destination (task #81, stage 1).

MARK-DOWN is the default verb: cheap, high-frequency, low-consequence, and it
should trip easily. Its whole cost model rests on where it lands — a plain
text file on the device that syncs nowhere and that the operator can delete
with one command. A false-positive mark costs a junk line in a log nobody
has to read.

This is the ONE file in the package that holds transcript text, and it holds
it deliberately (design §5.2: "Cued transcript — MARK-DOWN → local log file
on the device → until the operator deletes it. Syncs nowhere."). Telemetry
is content-free; the ring is RAM-only; routed captures leave via the peer
route. Marks live here and nowhere else.

Written 0600. A garage-lounge transcript is personal by construction, on a
device that may sit on a shelf in a shared space, and the default umask is
not a decision anyone made about it.

Miss-report samples land here too, tagged with their own kind. They are the
labelled negative examples the design calls the highest-value data this
system can produce — worth keeping beside the marks, worth distinguishing
from them.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

#: Why an entry is in the log. ``mark`` is the MARK-DOWN verb; ``miss`` is a
#: capture the recogniser should have taken and didn't, reported after the
#: fact and recovered from the ring.
KIND_MARK = "mark"
KIND_MISS = "miss"

#: File mode for the log — owner read/write only.
_LOG_MODE = stat.S_IRUSR | stat.S_IWUSR


@dataclass(frozen=True)
class MarkEntry:
    """One logged transcript.

    ``provenance`` carries the content-free capture facts (lookback used,
    whether the ring truncated, confidence) so a line in this file is
    self-describing — a reader six months on should not have to correlate it
    against the telemetry file to know how the capture was taken.
    """

    at: str
    kind: str
    text: str
    provenance: dict[str, Any]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_mark(
    path: str,
    text: str,
    *,
    kind: str = KIND_MARK,
    provenance: dict[str, Any] | None = None,
) -> bool:
    """Append one entry. Returns True on write, False when it could not go.

    Never raises on a full disk or a bad path: a capture device whose log
    write throws would take down the loop that is still listening, and a
    lost mark is strictly better than a deaf Jeeves. Every non-write is
    logged with its cause.
    """
    if not path:
        log.error(
            "jeeves.marklog.no_path",
            kind=kind,
            detail="jeeves.mark_log_path is empty — a MARK-DOWN capture was "
                   "transcribed and then DISCARDED because there is nowhere "
                   "to put it. Set the path.",
        )
        return False
    if not text.strip():
        # Intentionally-left-blank: a mark with no words is a real outcome
        # (the room was quiet when the cue fired), distinct from a mark that
        # failed to write.
        log.info(
            "jeeves.marklog.empty_text",
            kind=kind,
            detail="nothing was transcribed for this capture; no entry written",
        )
        return False

    entry = MarkEntry(
        at=now_iso(), kind=kind, text=text.strip(),
        provenance=dict(provenance or {}),
    )
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "at": entry.at,
                "kind": entry.kind,
                "text": entry.text,
                "provenance": entry.provenance,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        existed = target.exists()
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if not existed:
            # Only on creation: re-chmod on every append would fight an
            # operator who deliberately widened it.
            os.chmod(target, _LOG_MODE)
    except OSError as exc:
        log.error(
            "jeeves.marklog.write_failed",
            kind=kind,
            path=str(target),
            error_type=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return False

    log.info(
        "jeeves.marklog.appended",
        kind=kind,
        path=str(target),
        # Length, never content — the entry itself is the only place the
        # words live.
        text_chars=len(entry.text),
    )
    return True


def read_entries(path: str) -> list[dict[str, Any]]:
    """Every entry in the log; unparseable lines are skipped LOUDLY.

    Same degrade-don't-crash posture as the telemetry reader: a corrupt line
    costs one entry, never the file.
    """
    target = Path(path)
    if not target.exists():
        return []
    out: list[dict[str, Any]] = []
    skipped = 0
    for raw in target.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            skipped += 1
            continue
        if isinstance(data, dict):
            out.append(data)
        else:
            skipped += 1
    if skipped:
        log.warning(
            "jeeves.marklog.rows_skipped", path=str(target), skipped=skipped,
        )
    return out
