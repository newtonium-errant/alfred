"""Tests for ``alfred.vault.attribution`` — marker schema primitives.

Covers c1 of the calibration audit gap arc:
    * Marker insertion produces a well-formed BEGIN/END pair.
    * Marker IDs are deterministic on (agent, date, content) and change
      when any input changes.
    * ``with_inferred_marker`` is idempotent on already-wrapped content.
    * Audit entries round-trip cleanly through frontmatter.
    * ``find_marker_bounds`` returns the correct line range.
    * ``confirm_marker`` flips the entry; ``reject_marker`` strips both.
    * Malformed audit entries are tolerated (logged + skipped, no crash).
    * Multi-marker case: two inferred sections in one file are independently
      identifiable and operable.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from alfred.vault import attribution as attr


# --- ID determinism --------------------------------------------------------


def test_marker_id_is_deterministic_on_same_inputs() -> None:
    when = datetime(2026, 4, 23, 17, 42, tzinfo=timezone.utc)
    a = attr.make_marker_id("salem", "Substack default-ignore.", date=when)
    b = attr.make_marker_id("salem", "Substack default-ignore.", date=when)
    assert a == b
    assert a.startswith("inf-20260423-salem-")
    # 6-char hash suffix.
    assert len(a.split("-")[-1]) == 6


def test_marker_id_changes_when_agent_changes() -> None:
    when = datetime(2026, 4, 23, 17, 42, tzinfo=timezone.utc)
    salem = attr.make_marker_id("salem", "same content", date=when)
    kalle = attr.make_marker_id("kalle", "same content", date=when)
    assert salem != kalle


def test_marker_id_changes_when_date_changes() -> None:
    a = attr.make_marker_id(
        "salem", "same content",
        date=datetime(2026, 4, 23, tzinfo=timezone.utc),
    )
    b = attr.make_marker_id(
        "salem", "same content",
        date=datetime(2026, 4, 24, tzinfo=timezone.utc),
    )
    assert a != b


def test_marker_id_changes_when_content_changes() -> None:
    when = datetime(2026, 4, 23, tzinfo=timezone.utc)
    a = attr.make_marker_id("salem", "first content", date=when)
    b = attr.make_marker_id("salem", "second content", date=when)
    assert a != b


# --- with_inferred_marker --------------------------------------------------


def test_with_inferred_marker_wraps_body_and_returns_entry() -> None:
    when = datetime(2026, 4, 23, 17, 42, tzinfo=timezone.utc)
    body = "## Sender-Specific Overrides\n\nSubstack → ignore.\n"
    wrapped, entry = attr.with_inferred_marker(
        body,
        section_title="Sender-Specific Overrides",
        agent="salem",
        reason="conversation turn",
        date=when,
    )
    assert "<!-- BEGIN_INFERRED" in wrapped
    assert "<!-- END_INFERRED" in wrapped
    # Same marker_id in both lines.
    assert wrapped.count(entry.marker_id) == 2
    # The original body sits between the markers.
    assert "Substack → ignore." in wrapped
    # Entry shape.
    assert entry.agent == "salem"
    assert entry.section_title == "Sender-Specific Overrides"
    assert entry.reason == "conversation turn"
    assert entry.confirmed_by_andrew is False
    assert entry.confirmed_at is None
    assert entry.date.startswith("2026-04-23T17:42")


def test_with_inferred_marker_is_idempotent_on_already_wrapped() -> None:
    when = datetime(2026, 4, 23, tzinfo=timezone.utc)
    body = "## A\nFirst inferred chunk.\n"
    wrapped_once, entry_once = attr.with_inferred_marker(
        body, "A", "salem", "first call", date=when,
    )
    wrapped_twice, entry_twice = attr.with_inferred_marker(
        wrapped_once, "A", "salem", "second call", date=when,
    )
    # Body unchanged on the second pass.
    assert wrapped_twice == wrapped_once
    # Same marker_id reused.
    assert entry_twice.marker_id == entry_once.marker_id
    # No double-wrap — only one BEGIN line.
    assert wrapped_twice.count("BEGIN_INFERRED") == 1
    assert wrapped_twice.count("END_INFERRED") == 1


# --- frontmatter round-trip ------------------------------------------------


def test_audit_entry_round_trips_through_frontmatter() -> None:
    when = datetime(2026, 4, 23, tzinfo=timezone.utc)
    _, entry = attr.with_inferred_marker(
        "Some inferred prose.",
        section_title="Test",
        agent="salem",
        reason="conversation turn",
        date=when,
    )
    fm: dict = {}
    attr.append_audit_entry(fm, entry)
    parsed = attr.parse_audit_entries(fm)
    assert len(parsed) == 1
    assert parsed[0] == entry


def test_append_audit_entry_idempotent_on_marker_id() -> None:
    fm: dict = {}
    e1 = attr.AuditEntry(
        marker_id="inf-20260423-salem-abc123",
        agent="salem",
        date="2026-04-23T00:00:00+00:00",
        section_title="X",
        reason="r1",
    )
    attr.append_audit_entry(fm, e1)
    # Same marker_id, different reason — should REPLACE, not duplicate.
    e2 = attr.AuditEntry(
        marker_id="inf-20260423-salem-abc123",
        agent="salem",
        date="2026-04-23T00:00:00+00:00",
        section_title="X",
        reason="r2",
    )
    attr.append_audit_entry(fm, e2)
    assert len(fm["attribution_audit"]) == 1
    assert fm["attribution_audit"][0]["reason"] == "r2"


def test_append_audit_entry_preserves_unrelated_entries() -> None:
    fm: dict = {}
    e1 = attr.AuditEntry(
        marker_id="inf-20260423-salem-aaa111",
        agent="salem",
        date="2026-04-23T00:00:00+00:00",
        section_title="X",
        reason="first",
    )
    e2 = attr.AuditEntry(
        marker_id="inf-20260423-kalle-bbb222",
        agent="kalle",
        date="2026-04-23T00:00:00+00:00",
        section_title="Y",
        reason="second",
    )
    attr.append_audit_entry(fm, e1)
    attr.append_audit_entry(fm, e2)
    assert len(fm["attribution_audit"]) == 2
    ids = {e["marker_id"] for e in fm["attribution_audit"]}
    assert ids == {e1.marker_id, e2.marker_id}


# --- find_marker_bounds ----------------------------------------------------


def test_find_marker_bounds_returns_correct_line_range() -> None:
    when = datetime(2026, 4, 23, tzinfo=timezone.utc)
    body = "## A\nbody text\n"
    wrapped, entry = attr.with_inferred_marker(
        body, "A", "salem", "r", date=when,
    )
    full = "intro paragraph\n\n" + wrapped + "\n\noutro paragraph\n"
    bounds = attr.find_marker_bounds(full, entry.marker_id)
    assert bounds is not None
    begin, end = bounds
    lines = full.splitlines()
    assert "BEGIN_INFERRED" in lines[begin]
    assert "END_INFERRED" in lines[end]
    assert begin < end
    # Content between the markers matches the original body.
    interior = "\n".join(lines[begin + 1:end])
    assert "## A" in interior
    assert "body text" in interior


def test_find_marker_bounds_returns_none_for_unknown_id() -> None:
    body = "no markers here at all"
    assert attr.find_marker_bounds(body, "inf-20260101-nope-zzzzzz") is None


# --- confirm / reject ------------------------------------------------------


def test_confirm_marker_flips_audit_entry() -> None:
    when = datetime(2026, 4, 23, tzinfo=timezone.utc)
    _, entry = attr.with_inferred_marker(
        "x", "S", "salem", "r", date=when,
    )
    fm: dict = {}
    attr.append_audit_entry(fm, entry)
    confirm_when = datetime(2026, 4, 24, 9, 0, tzinfo=timezone.utc)
    attr.confirm_marker(fm, entry.marker_id, by="andrew", at=confirm_when)
    parsed = attr.parse_audit_entries(fm)
    assert parsed[0].confirmed_by_andrew is True
    assert parsed[0].confirmed_at is not None
    assert parsed[0].confirmed_at.startswith("2026-04-24T09:00")


def test_confirm_marker_no_op_for_unknown_id_does_not_raise() -> None:
    fm: dict = {"attribution_audit": []}
    # Just shouldn't raise.
    attr.confirm_marker(fm, "inf-20260101-nope-zzzzzz")


def test_reject_marker_strips_section_and_removes_entry() -> None:
    when = datetime(2026, 4, 23, tzinfo=timezone.utc)
    body = "## Override\nrule body\n"
    wrapped, entry = attr.with_inferred_marker(
        body, "Override", "salem", "r", date=when,
    )
    full = "before\n\n" + wrapped + "\n\nafter\n"
    fm: dict = {}
    attr.append_audit_entry(fm, entry)

    new_body, new_fm = attr.reject_marker(full, fm, entry.marker_id)
    assert "BEGIN_INFERRED" not in new_body
    assert "END_INFERRED" not in new_body
    assert "rule body" not in new_body
    # Surrounding text preserved.
    assert "before" in new_body
    assert "after" in new_body
    # Entry removed from frontmatter.
    assert new_fm["attribution_audit"] == []


# --- malformed-entry tolerance --------------------------------------------


def test_parse_audit_entries_skips_malformed_entries(caplog) -> None:
    fm = {
        "attribution_audit": [
            "this is a string, not a dict",
            {"marker_id": "ok-1", "agent": "salem", "date": "2026-04-23T00:00:00+00:00",
             "section_title": "X", "reason": "r"},
            {"marker_id": "broken"},  # missing required keys
            42,  # not a dict
        ],
    }
    parsed = attr.parse_audit_entries(fm)
    assert len(parsed) == 1
    assert parsed[0].marker_id == "ok-1"


def test_parse_audit_entries_returns_empty_when_field_absent() -> None:
    assert attr.parse_audit_entries({}) == []
    assert attr.parse_audit_entries({"attribution_audit": None}) == []


# --- multi-marker file ----------------------------------------------------


def test_multiple_markers_in_one_body_are_independently_identifiable() -> None:
    when_a = datetime(2026, 4, 23, tzinfo=timezone.utc)
    when_b = datetime(2026, 4, 24, tzinfo=timezone.utc)
    wrapped_a, entry_a = attr.with_inferred_marker(
        "## Section A\nFirst inferred section.\n",
        "Section A", "salem", "first turn", date=when_a,
    )
    wrapped_b, entry_b = attr.with_inferred_marker(
        "## Section B\nSecond inferred section.\n",
        "Section B", "salem", "second turn", date=when_b,
    )
    full = wrapped_a + "\n\n" + wrapped_b
    assert entry_a.marker_id != entry_b.marker_id

    bounds_a = attr.find_marker_bounds(full, entry_a.marker_id)
    bounds_b = attr.find_marker_bounds(full, entry_b.marker_id)
    assert bounds_a is not None and bounds_b is not None
    # B comes after A.
    assert bounds_b[0] > bounds_a[1]

    fm: dict = {}
    attr.append_audit_entry(fm, entry_a)
    attr.append_audit_entry(fm, entry_b)
    # Reject A — B and its entry must survive untouched.
    new_body, new_fm = attr.reject_marker(full, fm, entry_a.marker_id)
    assert "First inferred section" not in new_body
    assert "Second inferred section" in new_body
    surviving_ids = {e["marker_id"] for e in new_fm["attribution_audit"]}
    assert surviving_ids == {entry_b.marker_id}
    # And B is still locatable in the trimmed body.
    assert attr.find_marker_bounds(new_body, entry_b.marker_id) is not None
