"""Content-free telemetry (task #81, stage 1).

Telemetry is the ONE Jeeves artefact that is deliberately kept and
aggregated to morning review, so a content leak here outlives everything
else in the system. The dataclass makes a leak hard; the write-time
validator makes it fail. These pins drive the validator, because a guard
that has never refused anything is a guard nobody has tested.
"""

from __future__ import annotations

import json

import pytest
import structlog

from alfred.jeeves import telemetry


def row(**kw) -> telemetry.TelemetryRow:
    base = {"at": telemetry.now_iso(), "event": telemetry.EVENT_CUE_FIRED}
    base.update(kw)
    return telemetry.TelemetryRow(**base)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_a_row_is_appended_as_one_json_line(tmp_path):
    path = tmp_path / "jeeves" / "telemetry.jsonl"
    assert telemetry.append_row(str(path), row(
        verb="mark_down", lookback_used_seconds=45.0,
    )) is True

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["verb"] == "mark_down"
    assert parsed["lookback_used_seconds"] == 45.0


def test_the_parent_directory_is_created():
    """The device's data dir may not exist on first boot; a capture must not
    be lost to a missing folder."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "a" / "b" / "c" / "telemetry.jsonl"
        assert telemetry.append_row(str(path), row()) is True
        assert path.exists()


def test_rows_accumulate(tmp_path):
    path = tmp_path / "t.jsonl"
    for _ in range(3):
        telemetry.append_row(str(path), row())
    assert len(telemetry.read_rows(str(path))) == 3


def test_none_valued_fields_are_omitted_not_written_as_null(tmp_path):
    """A row about a cue that produced no capture should not carry a
    confidence of null — absent is the honest encoding."""
    path = tmp_path / "t.jsonl"
    telemetry.append_row(str(path), row(confidence=None))
    parsed = telemetry.read_rows(str(path))[0]
    assert "confidence" not in parsed


def test_the_ruling_5_field_is_carried_by_name(tmp_path):
    """RULING 5 asked for ``lookback_used_seconds`` by name. Renaming it
    would silently orphan the question it exists to answer."""
    path = tmp_path / "t.jsonl"
    telemetry.append_row(str(path), row(lookback_used_seconds=12.5))
    assert telemetry.read_rows(str(path))[0]["lookback_used_seconds"] == 12.5


# ---------------------------------------------------------------------------
# The content fence — refusal pins that assert WHY, not just that
# ---------------------------------------------------------------------------


def test_an_unknown_field_is_REFUSED_with_its_reason(tmp_path):
    path = tmp_path / "t.jsonl"
    with pytest.raises(telemetry.TelemetryRefused) as exc:
        telemetry.validate_row({
            "at": "x", "event": "cue_fired", "transcript": "the words",
        })
    assert exc.value.reason == "unknown_field"
    assert exc.value.field_name == "transcript"
    assert not path.exists()


def test_a_long_string_is_REFUSED_even_in_a_known_field():
    """The backstop that holds for a field name nobody thought to question.
    Every legitimate value comes from a closed code-side vocabulary and is
    short; a long one means spoken content has reached the file."""
    with pytest.raises(telemetry.TelemetryRefused) as exc:
        telemetry.validate_row({
            "at": "x", "event": "cue_fired",
            "matched_phrase": "the operator said a great deal " * 10,
        })
    assert exc.value.reason == "string_too_long"
    assert exc.value.field_name == "matched_phrase"


def test_a_nested_structure_is_REFUSED():
    """Rows are flat scalars: a nested structure is where content hides."""
    with pytest.raises(telemetry.TelemetryRefused) as exc:
        telemetry.validate_row({
            "at": "x", "event": "cue_fired", "verb": {"text": "note that"},
        })
    assert exc.value.reason == "unsupported_type"


def test_a_refused_row_LEAVES_NO_DEBRIS(tmp_path):
    """The right question for a refusal is not "did it write the file" but
    "did it touch anything at all" — including the parent directory it would
    otherwise have created on the way."""
    path = tmp_path / "never" / "created" / "t.jsonl"
    with pytest.raises(telemetry.TelemetryRefused):
        telemetry.append_row(str(path), telemetry.TelemetryRow(
            at="x" * 200,     # over the string ceiling
            event=telemetry.EVENT_CUE_FIRED,
        ))
    assert not path.exists()
    assert not path.parent.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_legitimate_row_passes_the_validator_untouched():
    """The guard must not be so tight that real rows fail — the mutation's
    other direction."""
    telemetry.validate_row({
        "at": "2026-08-11T00:00:00+00:00",
        "event": telemetry.EVENT_CAPTURE_ROUTED,
        "verb": "route",
        "matched_phrase": "send that to peerbox",
        "wake_variant": "cheese",
        "lookback_used_seconds": 45.0,
        "truncated_by_ring": False,
        "transcript_chars": 812,
    })


def test_a_transcript_sized_string_would_be_refused():
    """The realistic leak: someone adds ``matched_phrase=transcript`` in a
    refactor. A garage capture is hundreds of characters; the ceiling is 64."""
    transcript = (
        "so the bearing is the wrong size, it's a 6203 not a 6202, and I "
        "need to order a replacement before Thursday"
    )
    assert len(transcript) > telemetry.MAX_STRING_CHARS
    with pytest.raises(telemetry.TelemetryRefused):
        telemetry.validate_row({
            "at": "x", "event": "cue_fired", "matched_phrase": transcript,
        })


# ---------------------------------------------------------------------------
# No path configured
# ---------------------------------------------------------------------------


def test_an_unset_path_is_reported_not_silently_dropped():
    """Intentionally-left-blank: the whole ring-size question depends on
    these rows existing, so 'nowhere to write them' must be visible."""
    with structlog.testing.capture_logs() as captured:
        assert telemetry.append_row("", row()) is False
    events = [c for c in captured if c.get("event") == "jeeves.telemetry.no_path"]
    assert len(events) == 1
    assert "DISCARDED" in events[0]["detail"]


def test_the_write_is_logged():
    """Log-emission pin: the operator's grep workflow depends on this line
    carrying the event kind and the lookback."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "t.jsonl")
        with structlog.testing.capture_logs() as captured:
            telemetry.append_row(str(path), row(
                verb="mark_down", lookback_used_seconds=45.0))
        events = [c for c in captured
                  if c.get("event") == "jeeves.telemetry.appended"]
        assert len(events) == 1
        assert events[0]["verb"] == "mark_down"
        assert events[0]["lookback_used_seconds"] == 45.0


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------


def test_a_missing_file_reads_as_no_rows(tmp_path):
    assert telemetry.read_rows(str(tmp_path / "absent.jsonl")) == []


def test_a_corrupt_line_costs_one_row_not_the_file(tmp_path):
    """Bookkeeping must never be able to stop a capture device."""
    path = tmp_path / "t.jsonl"
    telemetry.append_row(str(path), row(verb="a"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    telemetry.append_row(str(path), row(verb="b"))

    with structlog.testing.capture_logs() as captured:
        rows = telemetry.read_rows(str(path))
    assert [r["verb"] for r in rows] == ["a", "b"]
    events = [c for c in captured
              if c.get("event") == "jeeves.telemetry.rows_skipped"]
    assert len(events) == 1
    assert events[0]["skipped"] == 1
