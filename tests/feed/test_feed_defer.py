"""Defer — "later", in both shapes (D2, ratified 2026-08-11).

A deferred item is not open (it leaves the deck and every ``state=open`` query)
and is not decided (the operator judged nothing, he only moved it). The whole
amendment therefore rests on ONE property: **a defer must return.** Suppression
that outlives its window is a silent drop with a politer name.

Two shapes: ``deferred_until=None`` returns at the next render;
``deferred_until=<ISO>`` returns at the first fire at/after that instant.

The three assertions the ruling named, each with a positive control in the same
test so none of them can pass against a build where the whole path is dead:
  1. inside its window, a deferred item STAYS suppressed;
  2. past its window, the SAME item RETURNS;
  3. adding the state does not disturb the pre-existing sticky-ack suppression.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from alfred.feed import FeedItem
from alfred.feed.model import (
    STATE_ACKED,
    STATE_DEFERRED,
    STATE_OPEN,
    defer_window_open,
)
from alfred.feed.store import FeedStore


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# Fixed instant for the PREDICATE tests, which pass ``now=`` explicitly.
NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


# Store-level tests must build windows against the REAL clock: ``reconcile``
# stamps its own ``_now_iso()``, so a window pinned to a fixed constant is
# already in the past by the time the test runs and every "still held" case
# silently becomes a "returned" one.
def _future(hours: int = 1) -> str:
    return _iso(datetime.now(timezone.utc) + timedelta(hours=hours))


def _past(hours: int = 1) -> str:
    return _iso(datetime.now(timezone.utc) - timedelta(hours=hours))


IN_AN_HOUR = _iso(NOW + timedelta(hours=1))
AN_HOUR_AGO = _iso(NOW - timedelta(hours=1))


def _proposal(key: str = "corr-1", title: str = "Merge Ben McMillan") -> FeedItem:
    return FeedItem.create(
        kind="proposal", stable_key=key, instance="salem", title=title,
    )


def _fog() -> FeedItem:
    """The motivating fixture from the sketch README: weather is not a moment,
    it is four hours. Carries a real interval extent (D7) AND gets deferred
    (D2) — the two amendments meet on one item."""
    return FeedItem.create(
        kind="weather", stable_key="fog|2026-08-11", instance="salem",
        title="Fog, 11:00–15:00",
        starts_at="2026-08-11T11:00:00-03:00",
        ends_at="2026-08-11T15:00:00-03:00",
    )


# --- the predicate ----------------------------------------------------------


def test_defer_window_predicate_holds_then_releases() -> None:
    """The one canonical predicate. True = still held, False = returns."""
    assert defer_window_open(IN_AN_HOUR, now=_iso(NOW)) is True
    assert defer_window_open(AN_HOUR_AGO, now=_iso(NOW)) is False


def test_defer_window_fails_towards_returning() -> None:
    """Every ambiguous input RETURNS the item. Extra noise costs a glance; a
    commitment that never comes back costs the thing itself, and its absence is
    exactly what the operator cannot notice in order to ask about it.

    Positive control: a genuinely-open window still holds, so this cannot pass
    against a predicate hard-wired to False.
    """
    assert defer_window_open(None, now=_iso(NOW)) is False          # next-render
    assert defer_window_open("", now=_iso(NOW)) is False            # empty
    assert defer_window_open("whenever", now=_iso(NOW)) is False    # unparseable
    assert defer_window_open(IN_AN_HOUR, now="not-a-time") is False  # unreadable clock
    assert defer_window_open(IN_AN_HOUR, now=_iso(NOW)) is True


def test_naive_window_is_read_as_utc_not_crashed() -> None:
    """A naive timestamp must not raise mid-reconcile (aware/naive comparison
    is a TypeError). Read as UTC, so it still holds while genuinely future."""
    naive_future = (NOW + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    naive_past = (NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    assert defer_window_open(naive_future, now=_iso(NOW)) is True
    assert defer_window_open(naive_past, now=_iso(NOW)) is False


# --- 1 + 2: held inside the window, RETURNS past it -------------------------


def test_deferred_until_a_time_is_held_then_returns(tmp_path: Path) -> None:
    """Assertions 1 and 2 as one story, on one item, because they are only
    meaningful together: being suppressed is acceptable ONLY because it ends."""
    store = FeedStore(tmp_path / "feed.jsonl")
    store.reconcile("proposal", [_proposal()])
    window = _future()
    store.defer("proposal:corr-1", until=window)

    stored = store.load()["proposal:corr-1"]
    assert stored.state == STATE_DEFERRED
    assert stored.deferred_until == window

    # (1) The producer re-emits it while the window is open → still deferred.
    counts = store.reconcile("proposal", [_proposal()])
    assert store.load()["proposal:corr-1"].state == STATE_DEFERRED
    assert counts["deferred_held"] == 1
    assert counts["defer_returned"] == 0

    # (2) The window lapses → the SAME item returns to open.
    store.defer("proposal:corr-1", until=_past())
    counts = store.reconcile("proposal", [_proposal()])
    returned = store.load()["proposal:corr-1"]
    assert returned.state == STATE_OPEN
    assert returned.deferred_until is None, "a returned item must not keep a stale window"
    assert counts["defer_returned"] == 1
    assert counts["deferred_held"] == 0


def test_deferred_to_next_render_returns_on_the_very_next_fire(tmp_path: Path) -> None:
    """The other shape. ``until=None`` promises the NEXT render, so the next
    reconcile must return it — with no window there is nothing to hold against.

    Positive control: the same store, same item, deferred WITH a live window
    instead, is still held — so this cannot pass against a build that simply
    never suppresses anything.
    """
    store = FeedStore(tmp_path / "feed.jsonl")
    store.reconcile("weather", [_fog()])

    store.defer("weather:fog|2026-08-11")  # next-render shape
    assert store.load()["weather:fog|2026-08-11"].state == STATE_DEFERRED
    counts = store.reconcile("weather", [_fog()])
    assert store.load()["weather:fog|2026-08-11"].state == STATE_OPEN
    assert counts["defer_returned"] == 1

    store.defer("weather:fog|2026-08-11", until=_future())  # until-a-time shape
    counts = store.reconcile("weather", [_fog()])
    assert store.load()["weather:fog|2026-08-11"].state == STATE_DEFERRED
    assert counts["deferred_held"] == 1


def test_a_state_transition_out_of_deferred_clears_the_window(tmp_path: Path) -> None:
    """The window must die with the state on the STATE-EVENT path too.

    Caught by mutation: the sibling assertion in the held-then-returns test is
    VACUOUS on its own, because a return happens via an UPSERT and the incoming
    producer item carries ``deferred_until=None`` anyway — so the fold's
    clearing logic was never exercised and deleting it stayed green. The path
    that genuinely depends on it is ``set_state``, which is what the action
    router calls for undo (``set_state(id, STATE_OPEN)``) and for acting on an
    item. Without clearing, an OPEN item would carry a stale window that a
    later reader could mistake for a live defer.

    Both transitions out are asserted, with the still-deferred case as the
    positive control.
    """
    store = FeedStore(tmp_path / "feed.jsonl")
    store.reconcile("proposal", [_proposal()])

    # Positive control: while deferred, the window IS held.
    store.defer("proposal:corr-1", until=_future())
    assert store.load()["proposal:corr-1"].deferred_until is not None

    # deferred → open (the router's undo path)
    store.set_state("proposal:corr-1", STATE_OPEN)
    revived = store.load()["proposal:corr-1"]
    assert revived.state == STATE_OPEN
    assert revived.deferred_until is None

    # deferred → acted (the operator judged it instead of re-parking it)
    store.defer("proposal:corr-1", until=_future())
    store.set_state("proposal:corr-1", "acted", action="confirm")
    decided = store.load()["proposal:corr-1"]
    assert decided.state == "acted"
    assert decided.deferred_until is None


def test_a_returning_item_keeps_its_interval_extent(tmp_path: Path) -> None:
    """D7 and D2 on one item: the fog's 11:00–15:00 span survives a defer and
    its return, because the extent is content, not lifecycle."""
    store = FeedStore(tmp_path / "feed.jsonl")
    store.reconcile("weather", [_fog()])
    store.defer("weather:fog|2026-08-11", until=_past())
    store.reconcile("weather", [_fog()])
    back = store.load()["weather:fog|2026-08-11"]
    assert back.state == STATE_OPEN
    assert back.starts_at == "2026-08-11T11:00:00-03:00"
    assert back.ends_at == "2026-08-11T15:00:00-03:00"


def test_defer_pauses_an_episode_rather_than_ending_it(tmp_path: Path) -> None:
    """``created_at`` is the episode's first-seen. A defer means "later", not
    "done", so the age must survive the round trip — otherwise a week-deferred
    item reads as brand new and the attention policy loses the one signal that
    says the operator keeps putting this off."""
    store = FeedStore(tmp_path / "feed.jsonl")
    store.reconcile("proposal", [_proposal()])
    first_seen = store.load()["proposal:corr-1"].created_at

    store.defer("proposal:corr-1", until=_past())
    store.reconcile("proposal", [_proposal()])
    assert store.load()["proposal:corr-1"].created_at == first_seen


# --- 3: the new state must not disturb the existing sticky-ack suppression ---


def test_sticky_ack_suppression_is_untouched_by_the_new_state(tmp_path: Path) -> None:
    """The regression assertion the ruling asked for. An acked SNAPSHOT item
    with unchanged content must still stay acked (the groundhog fix), and a
    CHANGED one must still revive — both unchanged by adding STATE_DEFERRED.
    """
    store = FeedStore(tmp_path / "feed.jsonl")

    def event(time_display: str) -> FeedItem:
        return FeedItem.create(
            kind="event", stable_key="2026-08-12|Dentist", instance="salem",
            title="Dentist",
            evidence={
                "date_iso": "2026-08-12", "name": "Dentist",
                "rec_type": "event", "time_display": time_display,
            },
        )

    store.reconcile("event", [event("09:30")])
    store.set_state("event:2026-08-12|Dentist", STATE_ACKED)

    # Unchanged content → the ack STICKS (no groundhog).
    counts = store.reconcile("event", [event("09:30")])
    assert store.load()["event:2026-08-12|Dentist"].state == STATE_ACKED
    assert counts["suppressed"] == 1
    assert counts["deferred_held"] == 0, "a sticky ack is not a defer"

    # Positive control: a MOVED appointment still revives.
    store.reconcile("event", [event("14:00")])
    assert store.load()["event:2026-08-12|Dentist"].state == STATE_OPEN


def test_a_deferred_item_resolved_elsewhere_does_not_become_immortal(
    tmp_path: Path,
) -> None:
    """The ghost guard. If the owning store resolves a thing while it is parked,
    the producer stops emitting it — and a deferred item absent from the open
    set must be marked acted like any other, or it stays deferred forever with
    no producer left to return it.

    Positive control: a deferred item that IS still emitted stays deferred, so
    this cannot pass against a build that mass-acts every deferred item.
    """
    store = FeedStore(tmp_path / "feed.jsonl")
    store.reconcile("proposal", [_proposal("a"), _proposal("b", "Second")])
    store.defer("proposal:a", until=_future())
    store.defer("proposal:b", until=_future())

    # 'a' vanishes from the producer's set; 'b' is still emitted.
    store.reconcile("proposal", [_proposal("b", "Second")])
    items = store.load()
    assert items["proposal:a"].state == "retired"
    assert items["proposal:b"].state == STATE_DEFERRED


# --- ILB: both directions are announced -------------------------------------


def test_deferral_and_return_are_both_logged(tmp_path: Path) -> None:
    """A card silently reappearing is as confusing as one silently vanishing.
    Assert the FIELDS too, so a rename/drop fails rather than only a full-event
    disappearance."""
    from structlog.testing import capture_logs

    store = FeedStore(tmp_path / "feed.jsonl")
    store.reconcile("proposal", [_proposal()])

    with capture_logs() as out_logs:
        store.defer("proposal:corr-1", until=_past())
    deferred = [c for c in out_logs if c.get("event") == "feed.store.deferred"]
    assert len(deferred) == 1, out_logs
    assert deferred[0]["id"] == "proposal:corr-1"
    assert deferred[0]["shape"] == "until_time"

    with capture_logs() as back_logs:
        store.reconcile("proposal", [_proposal()])
    returned = [c for c in back_logs if c.get("event") == "feed.store.defer_returned"]
    assert len(returned) == 1, back_logs
    assert returned[0]["count"] == 1
    assert returned[0]["ids"] == ["proposal:corr-1"]

    # The next-render shape names itself differently, so the log distinguishes
    # the two promises rather than flattening them.
    with capture_logs() as nr_logs:
        store.defer("proposal:corr-1")
    assert [c for c in nr_logs if c.get("event") == "feed.store.deferred"][0][
        "shape"
    ] == "next_render"
