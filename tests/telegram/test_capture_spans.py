"""Unit pins for ``alfred.telegram.capture_spans`` — the span-state half.

Route-level behaviour (toggle endpoint, turn gating, receipts) is pinned
in ``tests/test_web_chat_capture.py``; these pins cover the pure helpers
and state mutators the routes and the close path both lean on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.telegram import capture_spans
from alfred.telegram.session import append_turn, open_session, Session
from alfred.telegram.state import StateManager


@pytest.fixture
def mgr(tmp_path: Path) -> StateManager:
    m = StateManager(tmp_path / "state.json")
    m.load()
    return m


def _session_with_turns(mgr: StateManager, n_turns: int) -> Session:
    session = open_session(mgr, 1, model="claude-sonnet-4-6")
    for i in range(n_turns):
        append_turn(mgr, session, "user", f"turn {i}")
    return session


def test_begin_capture_opens_span_at_transcript_end(mgr) -> None:
    _session_with_turns(mgr, 3)
    state = capture_spans.begin_capture(mgr, 1)
    assert state["capture_active"] is True
    assert state["spans"] == [
        {"index": 0, "start": 3, "end": None, "turns": 0, "extracted": False}
    ]
    # Server truth persisted, not just returned.
    active = mgr.get_active(1)
    assert capture_spans.capture_active(active) is True


def test_begin_capture_survives_append_turn_persist(mgr) -> None:
    """The stashed keys must survive ``_persist`` merges — the exact
    ``_*``-preservation contract ``_session_type`` relies on. If a future
    refactor drops the underscore prefix, THIS pin goes red."""
    session = _session_with_turns(mgr, 1)
    capture_spans.begin_capture(mgr, 1)
    # An append_turn round-trips the Session dataclass through _persist —
    # the historical wipe-path for non-dataclass keys.
    append_turn(mgr, session, "user", "captured material")
    active = mgr.get_active(1)
    assert capture_spans.capture_active(active) is True
    assert capture_spans.spans_summary(active) == [
        {"index": 0, "start": 1, "end": None, "turns": 1, "extracted": False}
    ]


def test_end_capture_closes_span_exclusive_end(mgr) -> None:
    session = _session_with_turns(mgr, 2)
    capture_spans.begin_capture(mgr, 1)
    append_turn(mgr, session, "user", "in-span 1")
    append_turn(mgr, session, "user", "in-span 2")
    state, closed = capture_spans.end_capture(mgr, 1)
    assert closed == {"index": 0, "turns": 2}
    assert state["spans"] == [
        {"index": 0, "start": 2, "end": 4, "turns": 2, "extracted": False}
    ]
    assert capture_spans.capture_active(mgr.get_active(1)) is False


def test_end_capture_drops_empty_span(mgr) -> None:
    _session_with_turns(mgr, 2)
    capture_spans.begin_capture(mgr, 1)
    state, closed = capture_spans.end_capture(mgr, 1)
    assert closed is None
    assert state["spans"] == []
    # POSITIVE CONTROL in the same test: a non-empty cycle is kept.
    session = Session.from_dict(mgr.get_active(1))
    capture_spans.begin_capture(mgr, 1)
    append_turn(mgr, session, "user", "material")
    state, closed = capture_spans.end_capture(mgr, 1)
    assert closed == {"index": 0, "turns": 1}
    assert len(state["spans"]) == 1


def test_begin_and_end_are_idempotent(mgr) -> None:
    session = _session_with_turns(mgr, 1)
    capture_spans.begin_capture(mgr, 1)
    capture_spans.begin_capture(mgr, 1)  # double-tap: no forked span
    append_turn(mgr, session, "user", "one line")
    assert len(capture_spans.raw_spans(mgr.get_active(1))) == 1
    capture_spans.end_capture(mgr, 1)
    state, closed = capture_spans.end_capture(mgr, 1)
    assert closed is None
    assert state["capture_active"] is False
    assert len(state["spans"]) == 1


def test_normalized_spans_closes_trailing_open_span(mgr) -> None:
    """A session that closes while capture is ON yields the same span the
    operator would have gotten by toggling off first — the idle-timeout
    never loses material."""
    session = _session_with_turns(mgr, 1)
    capture_spans.begin_capture(mgr, 1)
    append_turn(mgr, session, "user", "dictated then idle")
    active = mgr.get_active(1)
    spans = capture_spans.normalized_spans(active)
    assert spans == [
        {"start": 1, "end": 2, "extracted": False, "record": "", "notes": []}
    ]
    # An open span with NOTHING captured normalizes away entirely.
    mgr2 = StateManager(Path(mgr.path).parent / "state2.json")
    mgr2.load()
    _session_with_turns(mgr2, 2)
    capture_spans.begin_capture(mgr2, 1)
    assert capture_spans.normalized_spans(mgr2.get_active(1)) == []


def test_unextracted_spans_filters_open_and_extracted(mgr) -> None:
    session = _session_with_turns(mgr, 0)
    capture_spans.begin_capture(mgr, 1)
    append_turn(mgr, session, "user", "span zero")
    capture_spans.end_capture(mgr, 1)
    capture_spans.begin_capture(mgr, 1)
    append_turn(mgr, session, "user", "span one — still open")
    active = mgr.get_active(1)
    pending = capture_spans.unextracted_spans(active)
    assert [i for i, _ in pending] == [0]  # open span 1 excluded
    ok = capture_spans.mark_span_extracted(
        mgr, 1, 0, record="session/capture-x.md", notes=["note/A.md"]
    )
    assert ok is True
    assert capture_spans.unextracted_spans(mgr.get_active(1)) == []


def test_mark_span_extracted_missing_session_or_span(mgr) -> None:
    assert (
        capture_spans.mark_span_extracted(mgr, 99, 0, record="r", notes=[])
        is False
    )
    _session_with_turns(mgr, 1)
    capture_spans.begin_capture(mgr, 1)
    assert (
        capture_spans.mark_span_extracted(mgr, 1, 5, record="r", notes=[])
        is False
    )


def test_spans_frontmatter_shape(mgr) -> None:
    session = _session_with_turns(mgr, 0)
    capture_spans.begin_capture(mgr, 1)
    append_turn(mgr, session, "user", "a")
    append_turn(mgr, session, "user", "b")
    capture_spans.end_capture(mgr, 1)
    capture_spans.mark_span_extracted(
        mgr, 1, 0, record="session/capture-rec.md", notes=["note/N.md"]
    )
    # A second span left OPEN — the frontmatter builder closes it at the
    # transcript end and shows it visibly unextracted.
    capture_spans.begin_capture(mgr, 1)
    append_turn(mgr, session, "user", "c")
    fm = capture_spans.spans_frontmatter(mgr.get_active(1))
    assert fm == [
        {
            "span": "0-2",
            "turns": 2,
            "extracted": True,
            "record": "[[session/capture-rec]]",
        },
        {"span": "2-3", "turns": 1, "extracted": False, "record": ""},
    ]
