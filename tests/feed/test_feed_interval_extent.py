"""Interval extents on the feed model (D7, ratified 2026-08-11).

Time-shaped data stores a SPAN, not a point. Before the amendment the only
time an item carried was ``created_at`` (when the FEED learned about it) plus,
for events, a ``time_display`` string in evidence — a rendered ``"%H:%M"`` with
no end, no date and no offset, so any render wanting a duration had to re-parse
a display string and could not recover one that was never there.

These pins cover the three layers the amendment touches: the model (fields +
schema tolerance both directions), the store (extents survive the JSON event
log), and the ``event`` producer end-to-end from real vault records through
the production entry point. Every "absent" assertion is paired with a positive
control in the same test, so a pin cannot pass by the whole path being dead.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from alfred.brief.feed_producer import event_feed_items
from alfred.brief.upcoming_events import _iso_extent
from alfred.feed import FeedItem
from alfred.feed.model import snapshot_fingerprint
from alfred.feed.store import FeedStore


class _Cfg:
    max_days_ahead = 30


TODAY = date(2026, 8, 11)


def _write_event(
    vault: Path,
    name: str,
    *,
    start: str | None = None,
    end: str | None = None,
    quoted: bool = False,
) -> Path:
    """Write a real event record. ``quoted`` forces the YAML value to stay a
    STRING; unquoted, PyYAML resolves an ISO timestamp to a ``datetime``, and
    both shapes reach ``_iso_extent`` in production."""
    d = vault / "event"
    d.mkdir(parents=True, exist_ok=True)
    q = '"' if quoted else ""
    lines = ["---", "type: event", f"name: {name}"]
    if start is not None:
        lines.append(f"start: {q}{start}{q}")
    if end is not None:
        lines.append(f"end: {q}{end}{q}")
    lines += ["---", "", "body", ""]
    path = d / f"{name}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# --- model: the fields themselves -------------------------------------------


def test_extent_defaults_to_absent_and_is_carried_when_given() -> None:
    """Pair comparison: a producer that passes no extent gets None/None (every
    pre-amendment producer's behaviour, unchanged), and one that passes an
    extent has it carried through ``create``."""
    timeless = FeedItem.create(
        kind="health", stable_key="curator", instance="salem", title="t",
    )
    assert (timeless.starts_at, timeless.ends_at) == (None, None)

    spanned = FeedItem.create(
        kind="weather", stable_key="fog", instance="salem", title="t",
        starts_at="2026-08-11T11:00:00-03:00",
        ends_at="2026-08-11T15:00:00-03:00",
    )
    assert spanned.starts_at == "2026-08-11T11:00:00-03:00"
    assert spanned.ends_at == "2026-08-11T15:00:00-03:00"


def test_an_instant_carries_a_start_with_no_end() -> None:
    """``ends_at=None`` means "no known end", NOT "ends immediately" — a moment
    (the 09:30 run) is start-only and must stay distinguishable from a span."""
    moment = FeedItem.create(
        kind="event", stable_key="k", instance="salem", title="t",
        starts_at="2026-08-11T09:30:00-03:00",
    )
    assert moment.starts_at is not None
    assert moment.ends_at is None


def test_extent_round_trips_and_load_is_schema_tolerant_both_directions() -> None:
    """The house ``from_dict`` contract, on the new fields."""
    item = FeedItem.create(
        kind="event", stable_key="k", instance="salem", title="t",
        starts_at="2026-08-11T09:30:00-03:00", ends_at="2026-08-11T10:30:00-03:00",
    )
    assert FeedItem.from_dict(item.to_dict()) == item

    # BACKWARD: an event written before the amendment has no extent keys at all.
    # It must load with the fields absent rather than raising.
    old = item.to_dict()
    del old["starts_at"]
    del old["ends_at"]
    revived = FeedItem.from_dict(old)
    assert (revived.starts_at, revived.ends_at) == (None, None)
    assert revived.id == item.id  # positive control: the rest still loaded

    # FORWARD: a newer writer's unknown field is filtered, extents preserved.
    newer = dict(item.to_dict(), recurrence_rule="FREQ=WEEKLY")
    ahead = FeedItem.from_dict(newer)
    assert ahead.starts_at == item.starts_at
    assert not hasattr(ahead, "recurrence_rule")


def test_extent_survives_the_store_event_log(tmp_path: Path) -> None:
    """Extents go through JSON serialization in the append-only log, so pin the
    fold rather than trusting the dataclass."""
    store = FeedStore(tmp_path / "feed.jsonl")
    store.upsert(FeedItem.create(
        kind="event", stable_key="k", instance="salem", title="t",
        starts_at="2026-08-11T09:30:00-03:00", ends_at="2026-08-11T10:30:00-03:00",
    ))
    loaded = store.load()["event:k"]
    assert loaded.starts_at == "2026-08-11T09:30:00-03:00"
    assert loaded.ends_at == "2026-08-11T10:30:00-03:00"


# --- _iso_extent: parse what frontmatter actually hands us -------------------


def test_iso_extent_accepts_the_real_frontmatter_shapes() -> None:
    """PyYAML resolves an unquoted ISO timestamp to ``datetime`` and a bare date
    to ``date``; a quoted one stays ``str``. All three reach this helper."""
    aware = datetime(2026, 8, 11, 9, 30, tzinfo=timezone.utc)
    assert _iso_extent(aware) == aware.isoformat()
    assert _iso_extent(date(2026, 8, 11)) == "2026-08-11"
    assert _iso_extent("2026-08-11T09:30:00-03:00") == "2026-08-11T09:30:00-03:00"


def test_an_all_day_date_is_not_promoted_to_midnight() -> None:
    """Regression pin. ``datetime.fromisoformat("2026-08-11")`` succeeds on
    3.11+ and returns midnight, so a datetime-first parse turns an all-day
    event into a zero-length one at 00:00 — inventing a clock time the record
    never carried. Caught by running the helper, not by reading it.

    Positive control: a real timestamp still keeps its time, so this cannot
    pass by the parser having been narrowed to dates only.
    """
    assert _iso_extent("2026-08-11") == "2026-08-11"
    assert _iso_extent("2026-08-11T09:30:00") == "2026-08-11T09:30:00"


def test_iso_extent_preserves_the_offset_rather_than_shifting_the_clock() -> None:
    """An offset must survive verbatim. Coercing to UTC here would move the
    operator's appointment by the offset — the exact lossiness this field
    exists to end."""
    assert _iso_extent("2026-08-11T09:30:00-03:00").endswith("-03:00")


def test_iso_extent_refuses_to_guess() -> None:
    """Unparseable input yields None, because a WRONG interval is worse than an
    absent one. Paired with a positive control so this cannot pass by the
    helper being uniformly broken."""
    for junk in ("", "   ", "tomorrow", "09:30", None, 42, [], {"a": 1}):
        assert _iso_extent(junk) is None, junk
    assert _iso_extent("2026-08-11T09:30:00") == "2026-08-11T09:30:00"


# --- producer: end-to-end from real records through the production path ------


def test_event_producer_stamps_the_real_interval(tmp_path: Path) -> None:
    """The load-bearing pin: a record carrying ``start`` + ``end`` produces a
    feed item whose extent is that interval — read from the record, not from
    the rendered display string."""
    _write_event(
        tmp_path, "Yarmouth run",
        start="2026-08-12T09:30:00-03:00", end="2026-08-12T13:30:00-03:00",
    )
    items = event_feed_items(_Cfg(), tmp_path, TODAY, instance="salem")
    assert len(items) == 1
    assert items[0].starts_at == "2026-08-12T09:30:00-03:00"
    assert items[0].ends_at == "2026-08-12T13:30:00-03:00"


def test_start_only_event_keeps_an_open_end_while_a_spanned_one_does_not(
    tmp_path: Path,
) -> None:
    """Pair comparison in ONE test. A hand-authored event has no ``end`` (only
    gcal_sync writes one), so ``ends_at`` is None — and the spanned sibling in
    the same scan proves that None is a genuine absence rather than the whole
    extent path being dead."""
    _write_event(tmp_path, "Open ended", start="2026-08-12T09:30:00-03:00")
    _write_event(
        tmp_path, "Bounded",
        start="2026-08-13T09:30:00-03:00", end="2026-08-13T11:00:00-03:00",
    )
    by_name = {
        it.evidence["name"]: it
        for it in event_feed_items(_Cfg(), tmp_path, TODAY, instance="salem")
    }
    assert by_name["Open ended"].starts_at == "2026-08-12T09:30:00-03:00"
    assert by_name["Open ended"].ends_at is None
    assert by_name["Bounded"].ends_at == "2026-08-13T11:00:00-03:00"


def test_a_task_asserts_no_interval_but_still_reaches_the_feed(tmp_path: Path) -> None:
    """A task's ``due`` is a DEADLINE, not a span it occupies, so stamping it as
    a start would assert an interval the record never claimed. The task must
    still produce an item — otherwise this pin would pass on a producer that
    dropped tasks entirely."""
    d = tmp_path / "task"
    d.mkdir(parents=True, exist_ok=True)
    (d / "Payroll.md").write_text(
        "---\ntype: task\nname: Payroll\ndue: 2026-08-12\nstatus: open\n---\n\nbody\n",
        encoding="utf-8",
    )
    items = event_feed_items(_Cfg(), tmp_path, TODAY, instance="salem")
    assert len(items) == 1
    assert items[0].evidence["rec_type"] == "task"
    assert (items[0].starts_at, items[0].ends_at) == (None, None)


def test_unparseable_start_degrades_to_no_extent_not_a_lost_item(
    tmp_path: Path,
) -> None:
    """A malformed ``start`` must cost the EXTENT, never the item — the event
    still has a date and still belongs in the feed."""
    _write_event(tmp_path, "Vague", start="sometime tuesday", quoted=True)
    (tmp_path / "event" / "Vague.md").write_text(
        "---\ntype: event\nname: Vague\ndate: 2026-08-12\nstart: \"sometime tuesday\"\n---\n\nbody\n",
        encoding="utf-8",
    )
    items = event_feed_items(_Cfg(), tmp_path, TODAY, instance="salem")
    assert len(items) == 1
    assert items[0].starts_at is None


def test_a_refused_extent_is_announced_not_silently_dropped(tmp_path: Path) -> None:
    """ILB. A present-but-unparseable ``start`` loses the extent, and silence
    there is indistinguishable from "this event has no time" — an operator
    wondering why their event lays out as timeless has nothing to grep.

    ``capture_logs`` rather than caplog: this module logs through structlog,
    the idiom already used for ``upcoming_events.closed_status_excluded``.
    Asserts the FIELDS too, so a rename/drop fails rather than just a
    whole-event disappearance.
    """
    from structlog.testing import capture_logs

    (tmp_path / "event").mkdir(parents=True, exist_ok=True)
    (tmp_path / "event" / "Vague.md").write_text(
        "---\ntype: event\nname: Vague\ndate: 2026-08-12\nstart: \"sometime tuesday\"\n---\n\nbody\n",
        encoding="utf-8",
    )
    with capture_logs() as captured:
        event_feed_items(_Cfg(), tmp_path, TODAY, instance="salem")
    matches = [
        c for c in captured
        if c.get("event") == "upcoming_events.extent_unparseable"
    ]
    assert len(matches) == 1, f"expected 1 extent_unparseable log, got {captured}"
    assert matches[0]["field"] == "start"
    assert matches[0]["raw"] == "sometime tuesday"

    # Positive control: a WELL-FORMED event must stay quiet, or the log is
    # firing on everything and carries no signal.
    _write_event(tmp_path, "Fine", start="2026-08-12T09:30:00-03:00")
    (tmp_path / "event" / "Vague.md").unlink()
    with capture_logs() as captured2:
        event_feed_items(_Cfg(), tmp_path, TODAY, instance="salem")
    assert not [
        c for c in captured2
        if c.get("event") == "upcoming_events.extent_unparseable"
    ]


# --- the extent must not leak into evidence / the ack fingerprint ------------


def test_extent_stays_out_of_evidence_and_the_snapshot_fingerprint(
    tmp_path: Path,
) -> None:
    """``event`` is a SNAPSHOT kind: its fingerprint decides whether an acked
    appointment revives. The extent is deliberately structural, not evidence,
    so it must not join that fingerprint by the back door — otherwise a purely
    representational change (an ``end`` that normalizes differently) would
    revive an ack for no operator-visible reason.

    Paired with a positive control: a genuinely MOVED appointment — which is
    what the operator does want re-surfaced — still changes the fingerprint.
    """
    _write_event(
        tmp_path, "Dentist",
        start="2026-08-12T09:30:00-03:00", end="2026-08-12T10:30:00-03:00",
    )
    item = event_feed_items(_Cfg(), tmp_path, TODAY, instance="salem")[0]
    assert "starts_at" not in item.evidence
    assert "ends_at" not in item.evidence

    baseline = snapshot_fingerprint("event", item.evidence)
    # Same appointment, extent differs only in representation.
    extent_only = dict(item.evidence)
    assert snapshot_fingerprint("event", extent_only) == baseline

    # Positive control: a real move (the display time changed) DOES revive.
    moved = dict(item.evidence, time_display="14:00")
    assert snapshot_fingerprint("event", moved) != baseline
