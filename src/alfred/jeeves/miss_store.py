"""Miss-report retention — the ONE place ring audio survives (#98, ruling 1).

Fence 2 says non-cued audio dies with the ring and cued audio is deleted as
soon as STT returns. This module is the single, deliberate, operator-ruled
exception to the second half of that, and the exception is narrow enough to
state in one sentence: **when the operator says "Jeeves, you missed that",
the window behind that report is written to disk.**

WHY THAT EXCEPTION IS SAFE, in the operator's own reasoning: the miss report
IS the consent. He is looking at the device, in the moment, telling it that
what just happened should have been captured — and the only way that report
becomes useful is if the audio it refers to still exists. A miss report
whose audio was already discarded is a note saying "something went wrong
recently", which trains nothing. Design §4.4 calls this the single
highest-value piece of training data the system can produce; ruling 1 is
what makes collecting it possible.

WHAT KEEPS IT NARROW — four constraints, all enforced here or pinned:

1. **Only the miss path writes.** Every other cue leaves the filesystem
   byte-identical. That is not a claim, it is
   ``tests/test_jeeves_miss_retention.py``'s central pin, which snapshots
   the whole data root around each verb and diffs it.
2. **Sensitive-local, never the vault.** The artefacts live under the
   instance's own data dir (:mod:`alfred.common.instance_paths`), in a 0700
   directory, 0600 per file — the ``~/jeeves-trial`` posture and the mark
   log's. Nothing here syncs, and no vault path is constructible from this
   module.
3. **It deletes itself.** :func:`mark_extracted` removes the audio the
   moment a recogniser example has been taken from it, and
   :func:`sweep_expired` removes anything nobody extracted within
   ``jeeves.miss_audio_retention_days``. Retention with no expiry is an
   archive with a nicer name.
4. **The INDEX IS NOT A BACK DOOR.** ``tests/test_jeeves_fences.py`` pins
   that a telemetry row has no field a transcript could be assigned to;
   this module ships a second retained artefact, so it inherits that pin
   rather than routing around it. :class:`MissArtifact` carries paths,
   timestamps, durations and byte counts — the facts you need to find,
   audit and delete a file — and :func:`validate_index_row` refuses
   anything else, exactly as :func:`alfred.jeeves.telemetry.validate_row`
   does. The one difference is the string ceiling: a filesystem path is
   legitimately longer than 64 characters, so ``audio_path`` gets its own
   larger bound while every other string keeps telemetry's.

THE INDEX IS APPEND-ONLY JSONL and the LAST row for an id wins. Same shape
as the telemetry and mark logs, and the same reason: a device that loses
power mid-write loses one line, never the file. Deletion is therefore also
an appended row (``deleted_at``), which means the record of what was kept
and when it went survives the audio itself — that history is the auditable
half of the retention promise.
"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import structlog

from .stt import wav_bytes

log = structlog.get_logger(__name__)

#: The index file inside the miss-audio directory.
INDEX_FILE_NAME = "index.jsonl"

#: Suffix for the retained window. WAV rather than raw PCM because the
#: artefact's whole purpose is to be LISTENED TO — by the operator at
#: morning review, and by whatever produces the recogniser example. A bare
#: ``.pcm`` needs its format supplied out-of-band, which is exactly the kind
#: of detail that goes missing between writing a file and playing it a week
#: later.
AUDIO_SUFFIX = ".wav"

#: 0700 directory, 0600 files — the mark log's posture (:mod:`.marklog`) and
#: ``~/jeeves-trial``'s. This is raw garage audio on a device that may sit on
#: a shelf in a shared space; the default umask is not a decision anyone made
#: about it.
_DIR_MODE = stat.S_IRWXU
_FILE_MODE = stat.S_IRUSR | stat.S_IWUSR

#: Why an artefact's audio was deleted. Distinct because they mean opposite
#: things about the system: one is the loop working, the other is a signal
#: nobody used.
DELETE_REASON_EXTRACTED = "extracted"
DELETE_REASON_EXPIRED = "expired"

#: Generated artefact id: ``miss-<utc compact stamp>-<8 hex>``. A FIXED
#: shape, 30 characters, which is what lets the id ride in a telemetry row
#: without threatening that file's content-free guarantee — pinned against
#: :data:`alfred.jeeves.telemetry.MAX_STRING_CHARS` in the fence suite.
ARTIFACT_ID_RE = re.compile(r"^miss-\d{8}T\d{6}Z-[0-9a-f]{8}$")

#: Field-name denylist for the index, mirroring the telemetry fence. The
#: index is the second RETAINED artefact in this package, so it inherits the
#: rule rather than being trusted to obey it.
CONTENT_SHAPED_NAMES: frozenset[str] = frozenset({
    "text", "transcript", "body", "content", "words", "utterance", "speech",
    "audio", "audio_b64", "transcript_text", "matched_phrase",
})

#: Ceiling on ordinary string fields — telemetry's, deliberately the same
#: number so the two artefacts cannot drift apart on what "short" means.
MAX_STRING_CHARS = 64
#: Ceiling on ``audio_path`` alone. A path is legitimately long; it is also
#: the only field here that is not drawn from a closed code-side vocabulary,
#: so it is bounded rather than unbounded.
MAX_PATH_CHARS = 4096


@dataclass(frozen=True)
class MissArtifact:
    """One retained miss-report window — the index row.

    Every field is a path, a timestamp, a number or a boolean. There is no
    field a transcript could be assigned to, which is the property
    :func:`validate_index_row` and the fence suite both check rather than
    trust. The transcript of a miss report lives in the mark log, where
    transcripts live; this is the audio's paperwork.
    """

    id: str
    at: str
    audio_path: str
    audio_bytes: int
    seconds: float
    sample_rate: int
    sample_width: int
    channels: int
    #: Content-free capture facts, carried so an artefact is self-describing
    #: — a reader should not have to correlate against telemetry to know how
    #: far back this window reached or whether the ring cut it short.
    lookback_used_seconds: float = 0.0
    lookahead_used_seconds: float = 0.0
    truncated_by_ring: bool = False
    #: Set when a recogniser example has been taken from this window. The
    #: audio is deleted in the same call — extraction is what retention was
    #: FOR, so keeping it afterwards would be keeping it for no reason.
    extracted_at: str = ""
    #: Set when the audio file is gone, with its reason. The ROW survives:
    #: "this was kept from X to Y and then deleted because Z" is the
    #: auditable half of the retention promise, and it carries no audio.
    deleted_at: str = ""
    delete_reason: str = ""


#: The closed set of keys an index row may carry. Auto-derived, and pinned
#: against the dataclass in the fence suite so a field added to one and not
#: the other cannot become a silent drop or an unvalidated write.
ALLOWED_FIELDS: frozenset[str] = frozenset(f.name for f in fields(MissArtifact))


class MissIndexRefused(Exception):
    """An index row was refused before being written (fail-closed)."""

    def __init__(self, reason: str, detail: str, field_name: str = "") -> None:
        self.reason = reason
        self.detail = detail
        self.field_name = field_name
        super().__init__(f"jeeves miss index refused [{reason}]: {detail}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_artifact_id(now: datetime | None = None) -> str:
    """A fresh artefact id of the shape :data:`ARTIFACT_ID_RE` pins.

    The timestamp half makes a directory listing chronological without
    reading the index; the random half is what stops two miss reports in
    the same second from colliding on a filename (the lookahead means two
    cues CAN complete within one second of each other).
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"miss-{stamp}-{uuid.uuid4().hex[:8]}"


def validate_index_row(payload: dict[str, Any]) -> None:
    """Refuse anything that is not a content-free index row. Raises.

    The same three checks the telemetry writer makes — unknown key, string
    too long, non-scalar — plus an explicit content-shaped-NAME check. The
    name check is the one telemetry gets from its dataclass being frozen in
    a reviewed file; this artefact is newer, and a future field called
    ``transcript`` here would be a fence breach that no other test sees.
    """
    unknown = sorted(set(payload) - ALLOWED_FIELDS)
    if unknown:
        raise MissIndexRefused(
            "unknown_field",
            f"miss index rows may only carry {sorted(ALLOWED_FIELDS)}; got "
            f"unknown key(s) {unknown}. This index is a RETAINED artefact "
            f"beside the retained audio — add the field to MissArtifact "
            f"deliberately, or it does not go in the file.",
            field_name=unknown[0],
        )
    banned = sorted(set(payload) & CONTENT_SHAPED_NAMES)
    if banned:
        raise MissIndexRefused(
            "content_shaped_field",
            f"miss index field(s) {banned} are content-shaped. The index "
            f"carries paths, timestamps and durations so an artefact can be "
            f"found, audited and deleted; what was SAID lives in the mark "
            f"log. An index that carries transcripts is a second copy of the "
            f"one thing this package keeps content-free by construction.",
            field_name=banned[0],
        )
    for key, value in payload.items():
        if isinstance(value, str):
            ceiling = MAX_PATH_CHARS if key == "audio_path" else MAX_STRING_CHARS
            if len(value) > ceiling:
                raise MissIndexRefused(
                    "string_too_long",
                    f"miss index field {key!r} carries a {len(value)}-char "
                    f"string (ceiling {ceiling}). Every value here is a path, "
                    f"a timestamp or a closed-vocabulary reason; a long one "
                    f"means something spoken has reached the index.",
                    field_name=key,
                )
        elif not isinstance(value, (int, float, bool, type(None))):
            raise MissIndexRefused(
                "unsupported_type",
                f"miss index field {key!r} carries a "
                f"{type(value).__name__}; rows are flat scalars only "
                f"(a nested structure is where content hides).",
                field_name=key,
            )


def index_path(miss_audio_dir: str) -> str:
    """The index file for a miss-audio directory ("" when unconfigured)."""
    return str(Path(miss_audio_dir) / INDEX_FILE_NAME) if miss_audio_dir else ""


def _append_index(miss_audio_dir: str, artifact: MissArtifact) -> bool:
    """Append one validated row. Never raises on I/O — logs and returns."""
    payload = asdict(artifact)
    validate_index_row(payload)
    target = Path(index_path(miss_audio_dir))
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(target.parent, _DIR_MODE)
        existed = target.exists()
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        if not existed:
            os.chmod(target, _FILE_MODE)
    except OSError as exc:
        log.error(
            "jeeves.miss_store.index_write_failed",
            artifact_id=artifact.id,
            path=str(target),
            error_type=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return False
    return True


def read_index(miss_audio_dir: str) -> list[MissArtifact]:
    """Every artefact, latest row per id winning; bad lines skipped LOUDLY.

    Degrade-don't-crash, the posture the telemetry and mark readers take: a
    corrupt line costs one artefact's paperwork, never the sweep that is
    supposed to be deleting things.
    """
    if not miss_audio_dir:
        return []
    target = Path(index_path(miss_audio_dir))
    if not target.exists():
        return []
    try:
        raw_text = target.read_text(encoding="utf-8")
    except OSError as exc:
        log.error(
            "jeeves.miss_store.index_unreadable",
            path=str(target),
            error_type=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return []

    latest: dict[str, MissArtifact] = {}
    order: list[str] = []
    skipped = 0
    known = {f.name for f in fields(MissArtifact)}
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            skipped += 1
            continue
        # Schema tolerance, the CLAUDE.md load() contract: a row written by a
        # newer build with an extra key must not crash a rollback's sweep.
        try:
            artifact = MissArtifact(**{k: v for k, v in data.items() if k in known})
        except TypeError:
            skipped += 1
            continue
        if artifact.id not in latest:
            order.append(artifact.id)
        latest[artifact.id] = artifact
    if skipped:
        log.warning(
            "jeeves.miss_store.rows_skipped", path=str(target), skipped=skipped,
        )
    return [latest[i] for i in order]


def pending(miss_audio_dir: str) -> list[MissArtifact]:
    """MORNING-REVIEW HOOK: retained windows nobody has dealt with yet.

    Artefacts whose audio is still on disk — not extracted, not swept. This
    is the surface ruling 1 asks for ("surfaces in morning review for the
    recogniser example"); the card that renders it is Part C's, and it
    consumes exactly this list.

    Intentionally-left-blank: an empty result is logged, because "no misses
    were reported" and "the retention path is broken" are the same empty
    list to a caller, and only one of them is good news.
    """
    artifacts = [
        a for a in read_index(miss_audio_dir) if not a.deleted_at and not a.extracted_at
    ]
    if not artifacts:
        log.info(
            "jeeves.miss_store.none_pending",
            path=miss_audio_dir or "(unconfigured)",
            detail="ran, nothing to do — no retained miss-report window is "
                   "awaiting a recogniser example",
        )
    return artifacts


def persist_miss_window(
    miss_audio_dir: str,
    audio: bytes,
    *,
    sample_rate: int,
    sample_width: int,
    channels: int,
    lookback_used_seconds: float = 0.0,
    lookahead_used_seconds: float = 0.0,
    truncated_by_ring: bool = False,
    now: datetime | None = None,
) -> MissArtifact | None:
    """Write the window behind a miss report. Returns None when nothing was.

    The ONLY function in this package that puts captured audio on disk.
    Returns ``None`` — never raises — for every reason it might not write,
    because a retention problem must not take down the loop that is still
    listening. Each of those reasons logs its own event: an unconfigured
    directory, an empty window, and a failed write are three different
    things to fix.
    """
    if not miss_audio_dir:
        # Intentionally-left-blank: the operator reporting a miss deserves
        # to know the audio behind it is not being kept, rather than
        # producing a dataset that does not exist.
        log.info(
            "jeeves.miss_store.unconfigured",
            detail="a miss report fired but jeeves.miss_audio_dir is unset, "
                   "so its transcript was kept and its AUDIO was not. Set the "
                   "directory to retain the window for the recogniser "
                   "example (#98 ruling 1); the audio auto-deletes once the "
                   "example is taken.",
        )
        return None
    if not audio:
        log.warning(
            "jeeves.miss_store.empty_window",
            detail="a miss report fired but the extracted window held no "
                   "audio — nothing to retain. The ring was empty at that "
                   "position.",
        )
        return None

    stamp = now or datetime.now(timezone.utc)
    artifact_id = new_artifact_id(stamp)
    directory = Path(miss_audio_dir)
    audio_file = directory / f"{artifact_id}{AUDIO_SUFFIX}"
    payload = wav_bytes(audio, sample_rate, sample_width, channels)

    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, _DIR_MODE)
        # tmp → rename: a half-written WAV that a sweep later finds and
        # deletes is fine, but one that morning review tries to PLAY is a
        # confusing bug report about a broken file.
        tmp = audio_file.with_name(audio_file.name + ".tmp")
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, _FILE_MODE)
        os.replace(tmp, audio_file)
    except OSError as exc:
        log.error(
            "jeeves.miss_store.write_failed",
            artifact_id=artifact_id,
            path=str(audio_file),
            error_type=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return None

    artifact = MissArtifact(
        id=artifact_id,
        at=stamp.isoformat(),
        audio_path=str(audio_file),
        audio_bytes=len(payload),
        seconds=round(
            len(audio) / float(max(1, sample_rate * sample_width * channels)), 3,
        ),
        sample_rate=sample_rate,
        sample_width=sample_width,
        channels=channels,
        lookback_used_seconds=round(lookback_used_seconds, 3),
        lookahead_used_seconds=round(lookahead_used_seconds, 3),
        truncated_by_ring=truncated_by_ring,
    )
    _append_index(miss_audio_dir, artifact)
    log.info(
        "jeeves.miss_store.retained",
        artifact_id=artifact.id,
        path=artifact.audio_path,
        seconds=artifact.seconds,
        audio_bytes=artifact.audio_bytes,
        detail="the window behind a miss report was retained "
               "SENSITIVE-LOCAL for the recogniser example. It is deleted "
               "when the example is taken, and aged out if it is not.",
    )
    return artifact


def _delete_audio(artifact: MissArtifact, reason: str) -> tuple[bool, str]:
    """Remove one artefact's audio file. Returns (removed, error detail)."""
    if not artifact.audio_path:
        return False, "no path"
    target = Path(artifact.audio_path)
    try:
        target.unlink()
    except FileNotFoundError:
        # Already gone — the deletion still gets recorded, because the index
        # row is the durable statement about what happened to this audio and
        # an absent file is the outcome we wanted either way.
        log.info(
            "jeeves.miss_store.already_absent",
            artifact_id=artifact.id, path=artifact.audio_path, reason=reason,
        )
        return True, ""
    except OSError as exc:
        log.error(
            "jeeves.miss_store.delete_failed",
            artifact_id=artifact.id,
            path=artifact.audio_path,
            reason=reason,
            error_type=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return False, str(exc)[:200]
    return True, ""


def _record_deletion(
    miss_audio_dir: str, artifact: MissArtifact, reason: str, stamp: str,
    *, extracted: bool,
) -> MissArtifact:
    updated = MissArtifact(
        **{
            **asdict(artifact),
            "extracted_at": stamp if extracted else artifact.extracted_at,
            "deleted_at": stamp,
            "delete_reason": reason,
        }
    )
    _append_index(miss_audio_dir, updated)
    log.info(
        "jeeves.miss_store.deleted",
        artifact_id=updated.id,
        path=updated.audio_path,
        reason=reason,
        seconds=updated.seconds,
        detail="the retained miss-report window was deleted; its index row "
               "survives as the record that it was kept and then removed.",
    )
    return updated


def mark_extracted(
    miss_audio_dir: str, artifact_id: str, *, now: str = "",
) -> MissArtifact | None:
    """The recogniser example has been taken — delete the audio.

    Ruling 1's auto-delete. Retention was FOR the extraction; once it has
    happened, keeping the window is keeping garage audio for no stated
    reason, which is the thing this whole package is built not to do.

    Returns the updated artefact, or ``None`` when the id is unknown (logged
    — an extraction claiming an id nobody retained is a real defect).
    """
    for artifact in read_index(miss_audio_dir):
        if artifact.id != artifact_id:
            continue
        if artifact.deleted_at:
            log.info(
                "jeeves.miss_store.already_deleted",
                artifact_id=artifact_id,
                reason=artifact.delete_reason,
                detail="ran, nothing to do — this window's audio was already "
                       "removed",
            )
            return artifact
        removed, _ = _delete_audio(artifact, DELETE_REASON_EXTRACTED)
        if not removed:
            return artifact
        return _record_deletion(
            miss_audio_dir, artifact, DELETE_REASON_EXTRACTED,
            now or now_iso(), extracted=True,
        )
    log.warning(
        "jeeves.miss_store.unknown_artifact",
        artifact_id=artifact_id,
        path=miss_audio_dir or "(unconfigured)",
        detail="an extraction was recorded against an id this index does not "
               "know. Nothing was deleted.",
    )
    return None


def _parse_at(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def sweep_expired(
    miss_audio_dir: str,
    *,
    retention_days: int,
    now: datetime | None = None,
) -> list[MissArtifact]:
    """Age out retained windows nobody extracted. Returns what was deleted.

    Retention WITHOUT extraction is the case ruling 1 leaves to a default:
    the operator reported a miss, the example was never taken, and the audio
    is still sitting there. ``retention_days`` (7 by default — see
    :data:`alfred.jeeves.config.DEFAULT_MISS_AUDIO_RETENTION_DAYS`) is how
    long that is allowed to last.

    An artefact whose ``at`` cannot be parsed is deleted rather than kept:
    an audio file whose age is unknown cannot be shown to be within any
    retention window, and the fail-safe direction for retained audio is
    removal.
    """
    if not miss_audio_dir:
        return []
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=max(0, retention_days))
    deleted: list[MissArtifact] = []
    live = 0

    for artifact in read_index(miss_audio_dir):
        if artifact.deleted_at:
            continue
        at = _parse_at(artifact.at)
        if at is not None and at > cutoff:
            live += 1
            continue
        if at is None:
            log.warning(
                "jeeves.miss_store.unparseable_timestamp",
                artifact_id=artifact.id,
                at=artifact.at[:64],
                detail="a retained window's timestamp could not be parsed, so "
                       "its age cannot be shown to be inside the retention "
                       "window. Deleting — unknown-age audio is not retained.",
            )
        removed, _ = _delete_audio(artifact, DELETE_REASON_EXPIRED)
        if not removed:
            continue
        deleted.append(_record_deletion(
            miss_audio_dir, artifact, DELETE_REASON_EXPIRED,
            moment.isoformat(), extracted=False,
        ))

    # Intentionally-left-blank: a sweep that deleted nothing is the normal
    # case and must be distinguishable from a sweep that never ran — this is
    # the only thing standing between "retained for review" and "archived".
    log.info(
        "jeeves.miss_store.sweep_complete",
        path=miss_audio_dir,
        deleted=len(deleted),
        retained=live,
        retention_days=retention_days,
        detail="ran, nothing to do — no retained window was older than the "
               "retention window" if not deleted else
               "aged-out miss-report windows were deleted without their "
               "recogniser example ever being taken",
    )
    return deleted
