"""The local mark-down log (task #81, stage 1).

This is the one file in the package that holds transcript text, deliberately
(design §5.2). The pins that matter are about it being local, private, and
unable to take the capture loop down with it.
"""

from __future__ import annotations

import json
import stat

import structlog

from alfred.jeeves import marklog


def test_a_mark_is_appended_with_its_provenance(tmp_path):
    path = tmp_path / "jeeves" / "marks.jsonl"
    assert marklog.append_mark(
        str(path), "the bearing is a 6203 not a 6202",
        provenance={"lookback_used_seconds": 45.0, "truncated_by_ring": False},
    ) is True

    entry = json.loads(path.read_text(encoding="utf-8").strip())
    assert entry["kind"] == marklog.KIND_MARK
    assert entry["text"] == "the bearing is a 6203 not a 6202"
    assert entry["provenance"]["lookback_used_seconds"] == 45.0
    assert entry["at"]


def test_a_miss_sample_is_tagged_distinctly(tmp_path):
    """Miss reports are the labelled negative examples the design calls the
    highest-value data this system can produce — worth keeping beside the
    marks, worth telling apart from them."""
    path = tmp_path / "marks.jsonl"
    marklog.append_mark(str(path), "something", kind=marklog.KIND_MARK)
    marklog.append_mark(str(path), "missed one", kind=marklog.KIND_MISS)

    kinds = [e["kind"] for e in marklog.read_entries(str(path))]
    assert kinds == [marklog.KIND_MARK, marklog.KIND_MISS]


def test_the_log_is_written_owner_only(tmp_path):
    """A garage-lounge transcript is personal by construction, on a device
    that may sit on a shelf in a shared space. The default umask is not a
    decision anyone made about it."""
    path = tmp_path / "marks.jsonl"
    marklog.append_mark(str(path), "private")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_an_operator_widened_mode_is_not_fought_on_every_append(tmp_path):
    """Re-chmod on every write would silently undo a deliberate change."""
    path = tmp_path / "marks.jsonl"
    marklog.append_mark(str(path), "one")
    path.chmod(0o640)
    marklog.append_mark(str(path), "two")
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_an_empty_transcript_writes_nothing_and_says_so(tmp_path):
    """Intentionally-left-blank: a mark with no words is a real outcome (the
    room was quiet when the cue fired), distinct from a mark that failed."""
    path = tmp_path / "marks.jsonl"
    with structlog.testing.capture_logs() as captured:
        assert marklog.append_mark(str(path), "   ") is False
    assert not path.exists()
    events = [c for c in captured
              if c.get("event") == "jeeves.marklog.empty_text"]
    assert len(events) == 1


def test_an_unset_path_is_an_ERROR_not_a_silent_drop():
    """A capture was transcribed and then discarded — the operator paid for
    that STT call and got nothing."""
    with structlog.testing.capture_logs() as captured:
        assert marklog.append_mark("", "some words") is False
    events = [c for c in captured if c.get("event") == "jeeves.marklog.no_path"]
    assert len(events) == 1
    assert events[0]["log_level"] == "error"


def test_a_write_failure_returns_false_rather_than_raising(tmp_path):
    """A log write that throws would take down the loop that is still
    listening. A lost mark is strictly better than a deaf Jeeves."""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    target = blocker / "marks.jsonl"

    with structlog.testing.capture_logs() as captured:
        assert marklog.append_mark(str(target), "words") is False
    events = [c for c in captured
              if c.get("event") == "jeeves.marklog.write_failed"]
    assert len(events) == 1
    assert events[0]["log_level"] == "error"


def test_the_append_log_carries_a_length_never_the_words(tmp_path):
    """The entry itself is the only place the words live."""
    path = tmp_path / "marks.jsonl"
    secret = "the alarm code is one two three four"
    with structlog.testing.capture_logs() as captured:
        marklog.append_mark(str(path), secret)
    for entry in captured:
        rendered = " ".join(str(v) for v in entry.values())
        assert "alarm code" not in rendered
        assert "one two three four" not in rendered
    events = [c for c in captured if c.get("event") == "jeeves.marklog.appended"]
    assert events[0]["text_chars"] == len(secret)


def test_a_missing_file_reads_as_no_entries(tmp_path):
    assert marklog.read_entries(str(tmp_path / "absent.jsonl")) == []


def test_a_corrupt_line_costs_one_entry_not_the_file(tmp_path):
    path = tmp_path / "marks.jsonl"
    marklog.append_mark(str(path), "first")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("}}not json\n")
    marklog.append_mark(str(path), "second")

    with structlog.testing.capture_logs() as captured:
        entries = marklog.read_entries(str(path))
    assert [e["text"] for e in entries] == ["first", "second"]
    events = [c for c in captured
              if c.get("event") == "jeeves.marklog.rows_skipped"]
    assert len(events) == 1


def test_text_is_stripped_but_otherwise_verbatim(tmp_path):
    path = tmp_path / "marks.jsonl"
    marklog.append_mark(str(path), "  keep   the   inner  spacing  ")
    assert marklog.read_entries(str(path))[0]["text"] == \
        "keep   the   inner  spacing"


def test_unicode_survives_the_round_trip(tmp_path):
    path = tmp_path / "marks.jsonl"
    marklog.append_mark(str(path), "café — 6203 bearing, ø17mm")
    assert marklog.read_entries(str(path))[0]["text"] == \
        "café — 6203 bearing, ø17mm"
