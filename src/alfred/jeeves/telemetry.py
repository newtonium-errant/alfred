"""Content-free telemetry — fence 4 made structural (task #81, stage 1).

The design states it plainly: *the telemetry deliberately carries no
content.* It answers "did the cue fire" and "how far back did we reach",
never "what was said". That keeps ruling 5's ring-size question answerable
without creating a second, quieter copy of the ambient audio's meaning.

A comment saying so would be worth nothing, so this module enforces it
twice:

1. :class:`TelemetryRow` has no text field. Every field is a timestamp, an
   enum from a closed code-side set, a number, or a boolean.
2. :func:`append_row` validates the serialised dict against
   :data:`ALLOWED_FIELDS` before writing, and REFUSES the row if a key it
   does not recognise appears. A future field added to the dataclass without
   being added to the allowlist fails loudly at the first write rather than
   quietly shipping content to a retained file.

The second guard exists because the first one only holds while the dataclass
is the only writer. Retention makes the stakes asymmetric: telemetry is the
one Jeeves artefact that is deliberately KEPT and aggregated to morning
review, so a content leak here outlives everything else in the system.

WHY THE ENUM FIELDS ARE NOT CONTENT. Three fields carry strings:
``matched_phrase`` comes from the closed grammar in :mod:`.cues`,
``wake_variant`` from the closed confusable set the Q6 trial measured, and
``verb`` / ``event`` / ``reason`` from constants in this codebase. None can
take a value the operator spoke — they are code-side vocabularies with a
finite, greppable range. :func:`append_row` treats any UNBOUNDED string as a
violation regardless of its field name, which is the check that actually
holds the line.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Telemetry event kinds. Stable strings — the morning rollup buckets on them.
EVENT_CUE_FIRED = "cue_fired"
EVENT_CAPTURE_MARKED = "capture_marked"
EVENT_CAPTURE_ROUTED = "capture_routed"
EVENT_MISS_REPORTED = "miss_reported"
EVENT_CAPTURE_DROPPED = "capture_dropped"
EVENT_SERVICE_IDLE = "service_idle"
# The company toggle (#98, ruling 3). BOTH directions get a row, from
# WHICHEVER door drove the transition — a rollup that recorded suspensions
# and not releases would show a device that goes deaf and never comes back.
EVENT_SUSPENDED = "suspended"
EVENT_RESUMED = "resumed"


@dataclass(frozen=True)
class TelemetryRow:
    """One content-free observation.

    ``lookback_used_seconds`` is the field ruling 5 asked for by name. The
    rest exist because a distribution of lookbacks is uninterpretable
    without knowing which of them hit the ring's floor, which were spoken
    modifiers rather than the default, and which cues produced nothing at
    all.
    """

    at: str
    event: str
    verb: str = ""
    reason: str = ""
    matched_phrase: str = ""
    wake_variant: str = ""
    confidence: float | None = None
    lookback_used_seconds: float | None = None
    requested_lookback_seconds: float | None = None
    lookahead_used_seconds: float | None = None
    lookahead_end_reason: str = ""
    truncated_by_ring: bool = False
    lookback_clamped: bool = False
    stt_calls: int = 0
    stt_latency_ms: int = 0
    stt_confidence_raw: float | None = None
    has_speech_signal: bool | None = None
    transcript_chars: int = 0
    ring_held_seconds: float | None = None
    #: Which door drove a suspend/resume transition — a member of
    #: :data:`alfred.jeeves.suspend.SUSPEND_SOURCES`, one of three fixed
    #: words. Empty on every other event kind.
    toggle_source: str = ""
    #: MISS-REPORT RETENTION (#98, ruling 1). ``miss_audio_retained`` is a
    #: BOOLEAN — it says whether the ring window behind a miss report was
    #: kept, never what was in it. ``miss_audio_id`` is the generated
    #: artefact id (``miss-<utc stamp>-<hex>``, a fixed shape from
    #: :func:`alfred.jeeves.miss_store.new_artifact_id`), which is what lets
    #: morning review join a telemetry row to the artefact WITHOUT the row
    #: carrying a path — a path is long, environment-specific, and the one
    #: string in this design that could plausibly grow. Paths live in the
    #: artefact index; telemetry carries the id and the boolean.
    miss_audio_retained: bool = False
    miss_audio_id: str = ""


#: The closed set of keys a telemetry row may carry. Kept in lockstep with
#: :class:`TelemetryRow`'s fields — a drift pin in
#: ``tests/test_jeeves_telemetry.py`` asserts the two are identical, so a
#: field added to one and not the other fails at build time rather than at a
#: leak.
ALLOWED_FIELDS: frozenset[str] = frozenset(
    f.name for f in fields(TelemetryRow)
)

#: Ceiling on any string value. Every legitimate value is drawn from a
#: code-side vocabulary and is short; a long string in a telemetry row means
#: something spoken has reached it, which is the exact failure this module
#: exists to prevent. The bound is the backstop that holds even for a field
#: name nobody thought to question.
MAX_STRING_CHARS = 64


class TelemetryRefused(Exception):
    """A telemetry row was refused before being written (fail-closed).

    Raised rather than logged-and-dropped: a row carrying content is a
    defect, and a defect that silently drops one row would be discovered as
    a gap in the ring-size data months later.
    """

    def __init__(self, reason: str, detail: str, field_name: str = "") -> None:
        self.reason = reason
        self.detail = detail
        self.field_name = field_name
        super().__init__(f"jeeves telemetry refused [{reason}]: {detail}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_row(payload: dict[str, Any]) -> None:
    """Refuse anything that is not a content-free row. Raises on violation."""
    unknown = sorted(set(payload) - ALLOWED_FIELDS)
    if unknown:
        raise TelemetryRefused(
            "unknown_field",
            f"telemetry rows may only carry {sorted(ALLOWED_FIELDS)}; got "
            f"unknown key(s) {unknown}. Telemetry is content-free BY "
            f"CONSTRUCTION — add the field to TelemetryRow and to "
            f"ALLOWED_FIELDS deliberately, or it does not go in the file.",
            field_name=unknown[0],
        )
    for key, value in payload.items():
        if isinstance(value, str) and len(value) > MAX_STRING_CHARS:
            raise TelemetryRefused(
                "string_too_long",
                f"telemetry field {key!r} carries a {len(value)}-char string "
                f"(ceiling {MAX_STRING_CHARS}). Every legitimate value comes "
                f"from a closed code-side vocabulary; a long one means "
                f"spoken content has reached the telemetry file.",
                field_name=key,
            )
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise TelemetryRefused(
                "unsupported_type",
                f"telemetry field {key!r} carries a "
                f"{type(value).__name__}; rows are flat scalars only "
                f"(a nested structure is where content hides).",
                field_name=key,
            )


def append_row(path: str, row: TelemetryRow) -> bool:
    """Append one validated row to the JSONL telemetry file.

    Returns True on write. Refusal raises :class:`TelemetryRefused` BEFORE
    anything touches the filesystem — including before the parent directory
    is created — so a refused row leaves no debris behind it.
    """
    payload = {k: v for k, v in asdict(row).items() if v is not None}
    validate_row(payload)

    if not path:
        # Intentionally-left-blank: telemetry with nowhere to go is a
        # configuration state, not an error, and it must be visible — the
        # whole ring-size question depends on these rows existing.
        log.warning(
            "jeeves.telemetry.no_path",
            kind=row.event,
            detail="jeeves.telemetry_path is empty, so this row was "
                   "DISCARDED. The lookback distribution ruling 5 asks for "
                   "is not being collected.",
        )
        return False

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    # ``kind=`` not ``event=``: structlog OWNS the ``event`` key (it is the
    # log message itself), and a row field with the same name silently
    # collides — TypeError at best, a clobbered message at worst.
    log.info(
        "jeeves.telemetry.appended", kind=row.event, verb=row.verb,
        lookback_used_seconds=row.lookback_used_seconds,
    )
    return True


def read_rows(path: str) -> list[dict[str, Any]]:
    """Every row in the telemetry file; unparseable lines are skipped LOUDLY.

    A half-corrupt telemetry file must degrade to "fewer data points", never
    to a crash — the same posture the learned-vocab store takes, and for the
    same reason: this is bookkeeping, and bookkeeping must not be able to
    stop a capture device.
    """
    target = Path(path)
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
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
            rows.append(data)
        else:
            skipped += 1
    if skipped:
        log.warning(
            "jeeves.telemetry.rows_skipped", path=str(target), skipped=skipped,
        )
    return rows
