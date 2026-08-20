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


def test_timeout_sweep_carries_spans_and_suppresses_heuristic(
    mgr, tmp_path
) -> None:
    """The idle-timeout close path: spans ride the sweeper meta, the
    whole-session capture heuristic is suppressed when spans exist (and
    fires when they don't — the positive control), the record stamps
    ``capture_spans`` with the open span closed at the transcript end."""
    import frontmatter as fmlib
    from datetime import timedelta

    from alfred.telegram.session import (
        check_timeouts_with_meta,
        stash_close_contract_metadata,
    )

    vault = tmp_path / "vault"
    (vault / "session").mkdir(parents=True)

    def _make(chat_id: int, with_span: bool) -> None:
        session = open_session(mgr, chat_id, model="claude-sonnet-4-6")
        stash_close_contract_metadata(
            mgr, chat_id,
            vault_path_root=str(vault),
            user_vault_path="person/Andrew Newton",
            stt_model_used="",
            session_type="conversation",
            tool_set="talker",
        )
        # Refresh the dataclass AFTER the stash so _persist keeps the _* keys.
        session = Session.from_dict(mgr.get_active(chat_id))
        if with_span:
            capture_spans.begin_capture(mgr, chat_id)
            session = Session.from_dict(mgr.get_active(chat_id))
        for i in range(3):
            append_turn(
                mgr, session, "user",
                f"substantial dictated line {i} carrying well over fifty "
                f"characters of real content for the substance gate",
            )

    _make(1, with_span=True)   # capture left ON — open span at close
    _make(2, with_span=False)  # the heuristic's positive control

    from datetime import datetime, timezone
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    meta = check_timeouts_with_meta(mgr, later, gap_seconds=1800)
    by_chat = {m["chat_id"]: m for m in meta}
    assert set(by_chat) == {1, 2}

    spanned = by_chat[1]
    assert spanned["capture_candidate"] is False  # suppressed by spans
    assert spanned["capture_spans"] == [
        {"start": 0, "end": 3, "extracted": False, "record": "", "notes": []}
    ]
    assert spanned["user_vault_path"] == "person/Andrew Newton"
    post = fmlib.load(vault / spanned["rel_path"])
    assert post.get("capture_structured") is None
    assert post["capture_spans"] == [
        {"span": "0-3", "turns": 3, "extracted": False, "record": ""}
    ]

    plain = by_chat[2]
    assert plain["capture_candidate"] is True
    assert plain["capture_spans"] == []
    post2 = fmlib.load(vault / plain["rel_path"])
    assert post2["capture_structured"] == "pending"
    assert post2.get("capture_spans") is None


# ---------------------------------------------------------------------------
# extract_span_material — the Hypatia processed-flip (ratified decision (d))
# and the memo-branch read-back
# ---------------------------------------------------------------------------
#
# Both blocks sit AFTER the two awaited pipeline calls, so nothing above
# reaches them: before these pins each could have been deleted whole with
# the suite still green. The pipeline calls themselves are faked (they are
# two LLM round-trips); what is under test is the code around them —
# which branch is taken, and what it writes to the record on disk.


def _closed_span_turns(mgr: StateManager, texts: tuple[str, ...]) -> list:
    """A real closed span's turns, taken through the production mutators
    rather than hand-built — the shape ``extract_span_material`` is fed by
    both the live route and the close-time backstop."""
    session = _session_with_turns(mgr, 0)
    capture_spans.begin_capture(mgr, 1)
    for text in texts:
        append_turn(mgr, session, "user", text)
    capture_spans.end_capture(mgr, 1)
    active = mgr.get_active(1)
    return capture_spans.span_turns(active, capture_spans.raw_spans(active)[0])


def _span_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for sub in ("session", "note", "zettel"):
        (vault / sub).mkdir(parents=True)
    return vault


async def _drive_span(
    mgr: StateManager,
    vault: Path,
    monkeypatch,
    *,
    tool_set: str,
    session_id: str,
    structured: str | None = None,
    memo_record: str = "",
    notes: tuple[str, ...] = ("note/Extracted Note.md",),
    texts: tuple[str, ...] = ("first captured line", "second captured line"),
) -> tuple:
    """Run ``extract_span_material`` with both LLM legs faked.

    ``structured`` is what the (faked) structuring pass stamps onto the
    span record — ``"memo"`` drives the memo branch, anything else falls
    through to note extraction. Returns ``(result, extractor_calls)``.

    ``session_id`` is per-drive because the span record's NAME derives
    from it (``capture-<date>-<slug>-<shortid>-s<start>``): two drives
    sharing one id in one vault collide on ``vault_create``'s
    no-overwrite guard, exactly as two identical sessions would. Distinct
    ids is what production has, not a workaround.
    """
    from alfred.telegram import capture_batch, capture_extract
    from alfred.vault import ops as vault_ops

    async def _fake_structuring(
        client, vault_path, rel, transcript, model, send_follow_up=None,
        **kwargs,
    ) -> None:
        if structured is not None:
            fields: dict = {"capture_structured": structured}
            if memo_record:
                fields["memo_record"] = memo_record
            vault_ops.vault_edit(vault_path, rel, set_fields=fields)

    extractor_calls: list = []

    async def _fake_extract(client, vault_path, rel, model, **kwargs):
        extractor_calls.append(rel)
        return capture_extract.ExtractResult(created_paths=list(notes))

    monkeypatch.setattr(
        capture_batch, "process_capture_session", _fake_structuring
    )
    monkeypatch.setattr(
        capture_extract, "extract_notes_from_record", _fake_extract
    )

    result = await capture_spans.extract_span_material(
        object(),  # client — never reaches the faked legs
        vault,
        _closed_span_turns(mgr, texts),
        parent_session_id=session_id,
        parent_chat_id=1,
        span_start=0,
        span_end=len(texts),
        model="claude-sonnet-4-6",
        agent_slug="hypatia",
        tool_set=tool_set,
    )
    return result, extractor_calls


async def test_hypatia_span_extraction_flips_processed_and_extracted_to(
    mgr, tmp_path, monkeypatch
) -> None:
    """Ratified decision (d): span extraction runs to COMPLETION, so on
    Hypatia the span record must not sit in the "Unprocessed captures"
    Bases view forever — ``processed: true`` + ``extracted_to`` are
    stamped on the record ON DISK.

    POSITIVE CONTROL in the same test: the same drive under
    ``tool_set="talker"`` — the nearest admissible neighbour — writes a
    record and returns the same notes but leaves BOTH fields absent. So
    the pin proves the flip is Hypatia-GATED, not that the drive happens
    to write something.
    """
    import frontmatter as fmlib

    vault = _span_vault(tmp_path)
    result, _ = await _drive_span(
        mgr, vault, monkeypatch,
        tool_set="hypatia", session_id="hyp00001-parent",
        notes=("note/A.md", "zettel/B.md"),
    )
    assert result.record.startswith("session/capture-")
    assert result.notes == ["note/A.md", "zettel/B.md"]
    post = fmlib.load(vault / result.record)
    assert post["processed"] is True
    assert post["extracted_to"] == ["[[note/A.md]]", "[[zettel/B.md]]"]

    # POSITIVE CONTROL — same drive, non-Hypatia tool_set.
    mgr2 = StateManager(Path(mgr.path).parent / "state_talker.json")
    mgr2.load()
    result2, _ = await _drive_span(
        mgr2, vault, monkeypatch,
        tool_set="talker", session_id="tlk00002-parent",
        notes=("note/A.md", "zettel/B.md"),
    )
    assert result2.notes == ["note/A.md", "zettel/B.md"]  # drive DID run
    post2 = fmlib.load(vault / result2.record)
    assert post2.get("processed") is None
    assert post2.get("extracted_to") is None


async def test_span_extraction_memo_branch_read_back(
    mgr, tmp_path, monkeypatch
) -> None:
    """The memo read-back: when the structuring pass stamps
    ``capture_structured: memo`` on the record (Hypatia's ≤1-message
    rule — the memo IS the artifact), the extractor must NOT run, the
    memo record becomes the span's single note, and ``skipped_reason``
    says ``memo``.

    Three drives, because the branch has three distinguishable outcomes
    and only their contrast pins it:

    1. ``memo`` + a memo record → extractor NOT called, notes = [memo],
       and the Hypatia flip stamps THAT record as ``extracted_to``;
    2. ``memo`` with an EMPTY ``memo_record`` → still terminal, notes
       empty (never ``[""]``);
    3. POSITIVE CONTROL — a normally-structured record → the extractor
       IS called and its notes come back, so (1) and (2) are the memo
       branch and not a dead extractor.
    """
    import frontmatter as fmlib

    vault = _span_vault(tmp_path)

    # (1) memo terminal.
    result, calls = await _drive_span(
        mgr, vault, monkeypatch,
        tool_set="hypatia", session_id="memo0003-parent",
        structured="memo", memo_record="note/Memo Artifact.md",
    )
    assert result.skipped_reason == "memo"
    assert result.notes == ["note/Memo Artifact.md"]
    assert calls == []  # the extractor never ran
    post = fmlib.load(vault / result.record)
    assert post["capture_structured"] == "memo"
    assert post["processed"] is True
    assert post["extracted_to"] == ["[[note/Memo Artifact.md]]"]

    # (2) memo with nothing to point at — terminal, but no phantom note.
    mgr2 = StateManager(Path(mgr.path).parent / "state_memo_empty.json")
    mgr2.load()
    result2, calls2 = await _drive_span(
        mgr2, vault, monkeypatch,
        tool_set="hypatia", session_id="memo0004-parent",
        structured="memo", memo_record="",
    )
    assert result2.skipped_reason == "memo"
    assert result2.notes == []
    assert calls2 == []
    assert fmlib.load(vault / result2.record)["extracted_to"] == []

    # (3) POSITIVE CONTROL — not a memo, so the extractor runs.
    mgr3 = StateManager(Path(mgr.path).parent / "state_structured.json")
    mgr3.load()
    result3, calls3 = await _drive_span(
        mgr3, vault, monkeypatch,
        tool_set="hypatia", session_id="strc0005-parent",
        structured="true", notes=("note/Real Extraction.md",),
    )
    assert result3.skipped_reason == ""
    assert result3.notes == ["note/Real Extraction.md"]
    assert calls3 == [result3.record]


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
