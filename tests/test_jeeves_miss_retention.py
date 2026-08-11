"""Miss-report retention — the ONE path where ring audio survives (#98, r1).

Fence 2 says non-cued audio dies with the ring and cued audio is deleted as
soon as STT returns. Ruling 1 opens exactly one door in that fence: an
explicit miss report retains the window it refers to, because the report IS
the in-the-moment consent and it is worth nothing without the audio.

THE CENTRAL PIN of this file is :func:`test_only_the_miss_path_writes_audio`.
It is an EXCLUSION pin, so it carries its positive control in the same test:
the miss verb must produce an artefact (proving the retention path works at
all) and every other verb must leave the filesystem BYTE-IDENTICAL (proving
the door is only open for one of them). Without the control, a build in
which retention was entirely broken would pass every "no audio was written"
assertion in this module.

"Byte-identical" is meant literally — the snapshot below is a recursive
relpath→sha256 map of the whole data root, not a check that one directory is
missing. A refused write that still creates its parent directory is the
failure shape a looser assertion misses.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import structlog

from alfred.jeeves import cues, miss_store, service, suspend, telemetry
from alfred.jeeves.audio import AudioFormat, MemoryAudioSource, silence, tone
from alfred.jeeves.config import (
    DEFAULT_MISS_AUDIO_RETENTION_DAYS,
    JEEVES_MODE_LIVE,
    JEEVES_MODE_SYNTHETIC,
    JeevesConfig,
    JeevesCueConfig,
    JeevesRingConfig,
    JeevesSttConfig,
    JeevesWindowConfig,
)
from alfred.jeeves.wake import ScriptedWakeDetector
from alfred.telegram.stt_backends import SttResult

FMT = AudioFormat(sample_rate=1000, sample_width=2, channels=1)   # 2000 B/s


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class StubStt:
    def __init__(self, *texts: str):
        self.texts = list(texts) or ["Jeeves, note that"]
        self.calls: list[int] = []

    async def transcribe(self, audio: bytes, mime: str, vocab: list[str]):
        self.calls.append(len(audio))
        text = self.texts[min(len(self.calls) - 1, len(self.texts) - 1)]
        return SttResult(
            text=text, backend_id="stub", tier="comparable",
            has_speech_signal=False, confidence_raw=-0.28,
        )


def build_config(tmp_path, **overrides) -> JeevesConfig:
    root = tmp_path / "data"
    base = dict(
        mode=JEEVES_MODE_LIVE,
        ring=JeevesRingConfig(
            seconds=60, sample_rate=1000, sample_width=2, channels=1),
        window=JeevesWindowConfig(
            lookback_seconds=5.0, extended_lookback_seconds=20.0,
            silence_seconds=2.0, max_lookahead_seconds=5.0,
            max_lookback_seconds=30.0, silence_rms_threshold=0.01),
        stt=JeevesSttConfig(api_key="DUMMY_GROQ_TEST_KEY"),
        cues=JeevesCueConfig(route_target="peerbox"),
        telemetry_path=str(root / "telemetry.jsonl"),
        mark_log_path=str(root / "marks.jsonl"),
        miss_audio_dir=str(root / "miss_audio"),
        suspend_state_path=str(root / "suspended.json"),
    )
    base.update(overrides)
    return JeevesConfig(**base)


def build_service(tmp_path, *, texts, config=None, route_sink=None,
                  provenance=({"synthetic": True},)):
    cfg = config or build_config(tmp_path)
    return service.JeevesService(
        cfg,
        audio_format=FMT,
        detector=ScriptedWakeDetector([10.0], audio_format=FMT),
        stt_backend=StubStt(*texts),
        route_sink=route_sink,
        provenance=provenance[0] if isinstance(provenance, tuple) else provenance,
    )


def speech_then_quiet(speech_s: float = 12.0, quiet_s: float = 4.0):
    blobs = [tone(1.0, FMT) for _ in range(int(speech_s))]
    blobs += [silence(1.0, FMT) for _ in range(int(quiet_s))]
    return MemoryAudioSource(blobs, audio_format=FMT)


def snapshot(root: Path) -> dict[str, str]:
    """Every file under ``root``, relpath → sha256 of its bytes.

    Content-hashed rather than listed: an APPEND to an existing file is a
    write too, and a pin that only compared path sets would not see one.
    """
    if not root.exists():
        return {}
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()).hexdigest()
    return out


def wav_files(root: Path) -> list[Path]:
    return sorted(root.rglob(f"*{miss_store.AUDIO_SUFFIX}")) if root.exists() else []


def released(cfg: JeevesConfig) -> JeevesConfig:
    """A config whose company toggle is explicitly OFF.

    Needed because the suspended state is fail-closed: a device with no
    state file is suspended, and a suspended service captures nothing at
    all — which would make every assertion in this module vacuously true.
    """
    suspend.set_suspended(
        cfg.suspend_state_path, False, source=suspend.SOURCE_MANUAL)
    return cfg


# ---------------------------------------------------------------------------
# THE CENTRAL PIN
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verb_name,transcript", [
    ("mark", "Jeeves, note that — the 6203 bearing"),
    ("route", "Jeeves, tell peerbox the bearing is wrong"),
    ("none", "Jeeves, the weather is holding up"),
    ("suspend", "Jeeves, company"),
])
async def test_only_the_miss_path_writes_audio(tmp_path, verb_name, transcript):
    """THE OPERATOR-FENCE PIN. Ruling 1 opened ONE door; this is the test
    that it is one and not two.

    Exclusion + positive control in the same test: the miss report at the
    bottom MUST produce an artefact, which is what makes the byte-identity
    assertions above it meaningful. A build where retention never fired
    would sail through the exclusions and die on the control.
    """
    root = tmp_path / "data"

    async def sink(capture):
        return True

    cfg = released(build_config(tmp_path))
    svc = build_service(tmp_path, texts=(transcript,), config=cfg, route_sink=sink)
    before = snapshot(root)
    outcomes = await svc.run(speech_then_quiet())
    after = snapshot(root)

    assert outcomes, f"the {verb_name} fixture produced no outcome at all"
    assert outcomes[0].verb != cues.CUE_MISS_REPORT

    # Nothing anywhere under the data root is audio.
    assert wav_files(root) == [], (
        f"the {verb_name} path put audio on disk — ruling 1 opened the fence "
        f"for the miss report ONLY"
    )
    # The miss-audio tree is byte-identical across the run: not merely
    # "no wav", but untouched — no index row, no created directory, no
    # debris of any kind.
    miss_before = {k: v for k, v in before.items() if k.startswith("miss_audio")}
    miss_after = {k: v for k, v in after.items() if k.startswith("miss_audio")}
    assert miss_after == miss_before == {}
    assert not (root / "miss_audio").exists(), (
        f"the {verb_name} path created the retention directory it never wrote to"
    )

    # THE POSITIVE CONTROL, same test: the miss verb DOES retain, so none of
    # the assertions above can be passing because retention is broken.
    # Released first, because one of the parametrised verbs above is the
    # company toggle and it left the device suspended — which is the control
    # earning its place: it caught that sequencing rather than passing.
    svc2 = build_service(
        tmp_path, texts=("Jeeves, you missed that",), config=released(cfg))
    await svc2.run(speech_then_quiet())
    kept = wav_files(root)
    assert len(kept) == 1, "the miss-report path did not retain its window"
    assert miss_store.read_index(cfg.miss_audio_dir), "no index row was written"


async def test_a_refused_capture_touches_nothing_at_all(tmp_path):
    """The gate runs BEFORE extraction, so a refused miss report must not
    create the retention directory either. "Does it write the file" is the
    wrong question; "does it touch anything out there" is the right one."""
    root = tmp_path / "data"
    cfg = released(build_config(tmp_path, mode=JEEVES_MODE_SYNTHETIC))
    svc = build_service(
        tmp_path, texts=("Jeeves, you missed that",), config=cfg,
        provenance=None,
    )
    before = snapshot(root)
    outcomes = await svc.run(speech_then_quiet())
    after = snapshot(root)

    assert outcomes[0].disposition == service.DISPOSITION_REFUSED
    assert wav_files(root) == []
    assert not (root / "miss_audio").exists()
    # The telemetry row for the refusal is the only permitted change.
    assert set(after) - set(before) <= {"telemetry.jsonl"}


async def test_a_suspended_device_retains_nothing_even_for_a_miss(tmp_path):
    """The toggle outranks the retention carve-out. "Jeeves, you missed
    that" while suspended is a phrase spoken into a device that is not
    listening, and the ring is empty anyway."""
    root = tmp_path / "data"
    cfg = build_config(tmp_path)
    suspend.set_suspended(
        cfg.suspend_state_path, True, source=suspend.SOURCE_SPOKEN)
    svc = build_service(tmp_path, texts=("Jeeves, you missed that",), config=cfg)
    await svc.run(speech_then_quiet())
    assert wav_files(root) == []
    assert not (root / "miss_audio").exists()


# ---------------------------------------------------------------------------
# What a retained artefact looks like
# ---------------------------------------------------------------------------


async def test_a_retained_window_is_a_playable_wav_owner_only(tmp_path):
    """The artefact exists to be LISTENED TO — by the operator at review and
    by whatever produces the recogniser example. A bare PCM blob needs its
    format supplied out-of-band, which is exactly what goes missing between
    writing a file and playing it a week later."""
    cfg = released(build_config(tmp_path))
    svc = build_service(tmp_path, texts=("Jeeves, you missed that",), config=cfg)
    await svc.run(speech_then_quiet())

    audio_files = wav_files(tmp_path / "data")
    assert len(audio_files) == 1
    blob = audio_files[0].read_bytes()
    assert blob[:4] == b"RIFF" and blob[8:12] == b"WAVE"
    assert stat.S_IMODE(os.stat(audio_files[0]).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(cfg.miss_audio_dir).st_mode) == 0o700
    assert not list(Path(cfg.miss_audio_dir).glob("*.tmp"))


async def test_the_telemetry_row_marks_the_window_without_carrying_a_path(tmp_path):
    """FENCE 4 SURVIVES RULING 1. The retained-window row carries a BOOLEAN
    and a fixed-shape id; the PATH — the one string here that is long and
    environment-specific — lives in the artefact index instead."""
    cfg = released(build_config(tmp_path))
    svc = build_service(tmp_path, texts=("Jeeves, you missed that",), config=cfg)
    await svc.run(speech_then_quiet())

    rows = [
        r for r in telemetry.read_rows(cfg.telemetry_path)
        if r["event"] == telemetry.EVENT_MISS_REPORTED
    ]
    assert len(rows) == 1
    assert rows[0]["miss_audio_retained"] is True
    assert miss_store.ARTIFACT_ID_RE.match(rows[0]["miss_audio_id"])
    # No row anywhere in the file carries a path or the retained bytes.
    for row in telemetry.read_rows(cfg.telemetry_path):
        for value in row.values():
            assert not (isinstance(value, str) and os.sep in value and
                        value.endswith(miss_store.AUDIO_SUFFIX))


async def test_an_unretained_miss_marks_the_row_false(tmp_path):
    """The CONTROL for the boolean: unset ``miss_audio_dir`` and the same
    verb reports retained=False, so the field tracks reality rather than
    being hard-wired to the verb."""
    cfg = released(build_config(tmp_path, miss_audio_dir=""))
    svc = build_service(tmp_path, texts=("Jeeves, you missed that",), config=cfg)
    with structlog.testing.capture_logs() as captured:
        await svc.run(speech_then_quiet())

    rows = [
        r for r in telemetry.read_rows(cfg.telemetry_path)
        if r["event"] == telemetry.EVENT_MISS_REPORTED
    ]
    assert rows[0].get("miss_audio_retained", False) is False
    assert rows[0].get("miss_audio_id", "") == ""
    # ILB: the operator is told his miss reports are producing no dataset.
    told = [
        c for c in captured
        if c.get("event") == "jeeves.service.miss_audio_not_retained"
    ]
    assert len(told) == 1


async def test_the_artefact_records_how_far_back_the_window_reached(tmp_path):
    """Self-describing: a reader six months on should not have to correlate
    against telemetry to know how this window was taken."""
    cfg = released(build_config(tmp_path))
    svc = build_service(tmp_path, texts=("Jeeves, you missed that",), config=cfg)
    await svc.run(speech_then_quiet())

    artifact = miss_store.read_index(cfg.miss_audio_dir)[0]
    assert artifact.lookback_used_seconds > 0
    assert artifact.sample_rate == FMT.sample_rate
    assert artifact.audio_bytes > 0
    assert artifact.seconds > 0
    assert artifact.deleted_at == "" and artifact.extracted_at == ""


# ---------------------------------------------------------------------------
# The index is not a back door
# ---------------------------------------------------------------------------


def test_the_index_refuses_a_content_shaped_field():
    """THE FENCE THIS MODULE COULD HAVE BECOME. ``test_jeeves_fences`` pins
    that a telemetry row has no field a transcript could be assigned to;
    this is the second RETAINED artefact, so it inherits the rule instead of
    routing around it."""
    with pytest.raises(miss_store.MissIndexRefused) as exc:
        miss_store.validate_index_row({"id": "x", "transcript": "the words"})
    assert exc.value.reason in ("unknown_field", "content_shaped_field")


def test_the_index_refuses_an_unknown_key():
    with pytest.raises(miss_store.MissIndexRefused) as exc:
        miss_store.validate_index_row({"id": "x", "whatever": 1})
    assert exc.value.reason == "unknown_field"
    assert exc.value.field_name == "whatever"


def test_the_index_refuses_a_transcript_sized_string_in_a_known_field():
    """A long value in a field that is SUPPOSED to be a closed vocabulary is
    the signature of spoken content reaching a retained file."""
    with pytest.raises(miss_store.MissIndexRefused) as exc:
        miss_store.validate_index_row({"id": "x" * 500})
    assert exc.value.reason == "string_too_long"
    # ...but a genuinely long PATH is fine, because a path legitimately is.
    miss_store.validate_index_row({"audio_path": "/" + "d/" * 200 + "a.wav"})


def test_the_index_refuses_a_nested_structure():
    with pytest.raises(miss_store.MissIndexRefused) as exc:
        miss_store.validate_index_row({"id": "x", "audio_bytes": {"a": 1}})
    assert exc.value.reason == "unsupported_type"


def test_a_valid_row_passes_the_validator_untouched():
    """The control: the validator is not simply refusing everything."""
    miss_store.validate_index_row({
        "id": miss_store.new_artifact_id(),
        "at": "2026-08-11T09:00:00+00:00",
        "audio_path": "/data/jeeves/miss_audio/a.wav",
        "audio_bytes": 1234, "seconds": 12.5,
        "sample_rate": 16000, "sample_width": 2, "channels": 1,
    })


# ---------------------------------------------------------------------------
# Auto-delete, and ageing out
# ---------------------------------------------------------------------------


def retain_one(tmp_path, *, at: datetime | None = None) -> miss_store.MissArtifact:
    directory = str(tmp_path / "miss_audio")
    artifact = miss_store.persist_miss_window(
        directory, b"\x01\x02" * 500,
        sample_rate=1000, sample_width=2, channels=1,
        lookback_used_seconds=5.0, now=at,
    )
    assert artifact is not None
    return artifact


def test_extraction_deletes_the_audio_and_logs_it(tmp_path):
    """Ruling 1's auto-delete. Retention was FOR the extraction; once it has
    happened, keeping the window is keeping garage audio for no reason."""
    directory = str(tmp_path / "miss_audio")
    artifact = retain_one(tmp_path)
    assert os.path.exists(artifact.audio_path)

    with structlog.testing.capture_logs() as captured:
        updated = miss_store.mark_extracted(directory, artifact.id)

    assert not os.path.exists(artifact.audio_path)
    assert updated is not None
    assert updated.delete_reason == miss_store.DELETE_REASON_EXTRACTED
    assert updated.extracted_at and updated.deleted_at
    events = [c for c in captured if c.get("event") == "jeeves.miss_store.deleted"]
    assert len(events) == 1
    assert events[0]["reason"] == miss_store.DELETE_REASON_EXTRACTED
    assert events[0]["artifact_id"] == artifact.id


def test_the_index_row_survives_the_audio_it_described(tmp_path):
    """"This was kept from X to Y and then deleted because Z" is the
    auditable half of the retention promise, and it carries no audio."""
    directory = str(tmp_path / "miss_audio")
    artifact = retain_one(tmp_path)
    miss_store.mark_extracted(directory, artifact.id)

    rows = miss_store.read_index(directory)
    assert len(rows) == 1, "the latest row per id must win, not accumulate"
    assert rows[0].id == artifact.id
    assert rows[0].deleted_at


def test_extraction_against_an_unknown_id_deletes_nothing_and_says_so(tmp_path):
    directory = str(tmp_path / "miss_audio")
    artifact = retain_one(tmp_path)
    with structlog.testing.capture_logs() as captured:
        assert miss_store.mark_extracted(directory, "miss-nope") is None
    assert os.path.exists(artifact.audio_path), "an unrelated artefact was deleted"
    assert [
        c for c in captured
        if c.get("event") == "jeeves.miss_store.unknown_artifact"
    ]


def test_an_unextracted_window_ages_out_at_the_retention_boundary(tmp_path):
    """Retention WITHOUT extraction is the case ruling 1 leaves to a
    default. Both sides of the boundary are checked, so this cannot pass by
    deleting everything or by deleting nothing."""
    directory = str(tmp_path / "miss_audio")
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)
    fresh = retain_one(tmp_path, at=now - timedelta(days=2))
    stale = retain_one(tmp_path, at=now - timedelta(days=9))

    deleted = miss_store.sweep_expired(
        directory, retention_days=DEFAULT_MISS_AUDIO_RETENTION_DAYS, now=now)

    assert [a.id for a in deleted] == [stale.id]
    assert not os.path.exists(stale.audio_path)
    assert os.path.exists(fresh.audio_path), "a window inside retention was deleted"
    assert deleted[0].delete_reason == miss_store.DELETE_REASON_EXPIRED
    assert deleted[0].extracted_at == "", (
        "an aged-out window was never extracted; recording otherwise would "
        "claim a recogniser example that does not exist"
    )


def test_the_shipped_retention_default_is_seven_days():
    """VALUE PIN. The number is a judgement about two opposite failures —
    too short destroys the scarce signal over a weekend away, too long turns
    a training sample into an audio archive of the garage. Moving it should
    be a deliberate edit that shows up in a diff."""
    assert DEFAULT_MISS_AUDIO_RETENTION_DAYS == 7
    assert JeevesConfig().miss_audio_retention_days == 7


def test_a_sweep_with_nothing_to_do_says_so(tmp_path):
    """ILB: this log line is the only thing standing between "retained for
    review" and "archived"."""
    directory = str(tmp_path / "miss_audio")
    retain_one(tmp_path)
    with structlog.testing.capture_logs() as captured:
        assert miss_store.sweep_expired(directory, retention_days=30) == []
    events = [
        c for c in captured
        if c.get("event") == "jeeves.miss_store.sweep_complete"
    ]
    assert len(events) == 1
    assert events[0]["deleted"] == 0 and events[0]["retained"] == 1
    assert "nothing to do" in events[0]["detail"]


def test_a_window_of_unknown_age_is_deleted_not_kept(tmp_path):
    """An audio file whose age cannot be established cannot be shown to be
    inside any retention window, and the fail-safe direction for retained
    audio is removal."""
    directory = str(tmp_path / "miss_audio")
    artifact = retain_one(tmp_path)
    index = Path(miss_store.index_path(directory))
    rows = [json.loads(line) for line in index.read_text().splitlines() if line]
    rows[0]["at"] = "not a timestamp"
    index.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    deleted = miss_store.sweep_expired(directory, retention_days=365)
    assert [a.id for a in deleted] == [artifact.id]
    assert not os.path.exists(artifact.audio_path)


def test_a_swept_artefact_is_not_swept_twice(tmp_path):
    directory = str(tmp_path / "miss_audio")
    retain_one(tmp_path, at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert len(miss_store.sweep_expired(directory, retention_days=1)) == 1
    assert miss_store.sweep_expired(directory, retention_days=1) == []


# ---------------------------------------------------------------------------
# The morning-review hook
# ---------------------------------------------------------------------------


def test_pending_lists_only_windows_nobody_has_dealt_with(tmp_path):
    """The surface ruling 1 asks for; the card that renders it is Part C's
    and consumes exactly this list."""
    directory = str(tmp_path / "miss_audio")
    waiting = retain_one(tmp_path)
    done = retain_one(tmp_path)
    miss_store.mark_extracted(directory, done.id)

    assert [a.id for a in miss_store.pending(directory)] == [waiting.id]


def test_pending_says_so_when_there_is_nothing_to_review(tmp_path):
    """ILB: "no misses were reported" and "the retention path is broken" are
    the same empty list to a caller, and only one is good news."""
    with structlog.testing.capture_logs() as captured:
        assert miss_store.pending(str(tmp_path / "miss_audio")) == []
    assert [
        c for c in captured if c.get("event") == "jeeves.miss_store.none_pending"
    ]


def test_an_unconfigured_directory_writes_nothing_and_says_so():
    """The fail-closed default survives ruling 1: setting the directory is
    the same deliberate at-deploy edit as ``mode: live``."""
    with structlog.testing.capture_logs() as captured:
        assert miss_store.persist_miss_window(
            "", b"\x01\x02", sample_rate=1000, sample_width=2, channels=1,
        ) is None
    assert [
        c for c in captured if c.get("event") == "jeeves.miss_store.unconfigured"
    ]


def test_an_empty_window_is_not_written_as_an_empty_file(tmp_path):
    directory = str(tmp_path / "miss_audio")
    with structlog.testing.capture_logs() as captured:
        assert miss_store.persist_miss_window(
            directory, b"", sample_rate=1000, sample_width=2, channels=1,
        ) is None
    assert not Path(directory).exists()
    assert [
        c for c in captured if c.get("event") == "jeeves.miss_store.empty_window"
    ]


def test_a_corrupt_index_line_costs_one_artefact_not_the_sweep(tmp_path):
    """Degrade-don't-crash: bookkeeping must never be able to stop the thing
    that deletes audio."""
    directory = str(tmp_path / "miss_audio")
    good = retain_one(tmp_path)
    index = Path(miss_store.index_path(directory))
    with open(index, "a", encoding="utf-8") as fh:
        fh.write("{not json\n")

    with structlog.testing.capture_logs() as captured:
        rows = miss_store.read_index(directory)
    assert [a.id for a in rows] == [good.id]
    assert [
        c for c in captured if c.get("event") == "jeeves.miss_store.rows_skipped"
    ]


def test_the_index_tolerates_a_field_from_a_newer_build(tmp_path):
    """The CLAUDE.md load() contract — a rollback must still be able to
    sweep."""
    directory = str(tmp_path / "miss_audio")
    artifact = retain_one(tmp_path)
    index = Path(miss_store.index_path(directory))
    rows = [json.loads(line) for line in index.read_text().splitlines() if line]
    rows[0]["a_field_from_the_future"] = "x"
    index.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    assert [a.id for a in miss_store.read_index(directory)] == [artifact.id]


def test_two_windows_in_the_same_second_do_not_collide(tmp_path):
    """The lookahead means two cues CAN complete inside one second, and a
    filename collision would silently overwrite the scarcer signal."""
    at = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)
    first = retain_one(tmp_path, at=at)
    second = retain_one(tmp_path, at=at)
    assert first.id != second.id
    assert os.path.exists(first.audio_path)
    assert os.path.exists(second.audio_path)
