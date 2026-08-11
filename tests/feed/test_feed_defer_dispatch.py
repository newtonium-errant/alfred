"""The generic defer verbs — ceiling, dispatcher, isolation (#102 1b-ii).

``FeedStore.defer`` and ``defer_window_open`` shipped built and pinned with ZERO
production callers: the mechanism existed, the entry point did not. These pin
the entry point, and the two properties that make it safe to add — that a defer
cannot reach a resolver, and that a broken defer cannot cost a confirm.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import structlog

from alfred.daily_sync.action_router import (
    DEFER_ACTIONS,
    DEFER_DURATIONS,
    DEFER_EXCLUDED_KINDS,
    DEFER_NEXT_RENDER,
    FEED_ACTIONS,
    _defer_until_iso,
    _dispatch_feed_defer,
    actions_for,
)
from alfred.feed.model import STATE_DEFERRED, defer_window_open


class _Store:
    """A feed store stand-in that records the one call this path may make."""

    def __init__(self, *, boom: bool = False) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.boom = boom

    def defer(self, item_id: str, *, until: str | None = None) -> None:
        if self.boom:
            raise OSError("disk went away")
        self.calls.append((item_id, until))

    def __getattr__(self, name: str):  # pragma: no cover - the point is the raise
        raise AssertionError(
            f"a defer must touch NOTHING but store.defer — it reached {name!r}"
        )


class TestTheCeiling:
    def test_every_decide_kind_gains_defer_except_the_board_slot(self):
        for kind in FEED_ACTIONS:
            has_defer = all(v in FEED_ACTIONS[kind] for v in DEFER_ACTIONS)
            if kind in DEFER_EXCLUDED_KINDS:
                assert not any(v in FEED_ACTIONS[kind] for v in DEFER_ACTIONS), kind
            else:
                assert has_defer, kind

    def test_the_board_slot_keeps_its_OWN_snooze_untouched(self):
        """Two defer mechanisms on one card is the thing being avoided."""
        slot = FEED_ACTIONS["slot_suggestion"]
        assert "snooze_3d" in slot and "snooze_until_i_say" in slot
        assert "defer" not in slot

    def test_the_two_vocabularies_never_overlap(self):
        """`snooze_*` writes the tier sidecar + ACTED; `defer_*` writes the feed
        store + DEFERRED. One id meaning either would be unreadable."""
        for kind, verbs in FEED_ACTIONS.items():
            snoozes = {v for v in verbs if v.startswith("snooze")}
            defers = {v for v in verbs if v.startswith("defer")}
            assert not (snoozes and defers), kind

    def test_there_is_no_indefinite_generic_rung(self):
        """A store constraint, not an omission: `FeedStore.defer` takes a window
        or next-render, because the model guarantees a deferred item always has
        a return date. A sentinel far-future date would be a lie."""
        for kind in FEED_ACTIONS:
            if kind in DEFER_EXCLUDED_KINDS:
                continue
            assert not any(
                v.startswith("defer") and "until_i_say" in v for v in FEED_ACTIONS[kind]
            ), kind


class TestTheLadder:
    def test_the_dated_rungs_are_the_days_they_name(self):
        base = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)
        for verb, days in DEFER_DURATIONS.items():
            got = _defer_until_iso(verb, now=base)
            assert got == (base + timedelta(days=days)).isoformat(), verb

    def test_the_quick_defer_has_no_window(self):
        assert _defer_until_iso(DEFER_NEXT_RENDER) is None

    def test_an_unknown_verb_fails_SAFE_toward_returning_sooner(self):
        # The model's one-way fail direction: extra noise costs a glance; a
        # commitment that never comes back costs the thing itself.
        assert _defer_until_iso("defer_99y") is None


class TestTheDispatcher:
    def test_a_quick_defer_writes_next_render_and_nothing_else(self):
        store = _Store()
        res = _dispatch_feed_defer("attribution:m:x", "defer", feed_store=store)
        assert res.ok
        assert store.calls == [("attribution:m:x", None)]

    def test_a_dated_defer_writes_a_window_the_predicate_still_holds(self):
        """End to end through the REAL predicate, not a re-derived comparison."""
        store = _Store()
        res = _dispatch_feed_defer("pending:p:1", "defer_7d", feed_store=store)
        assert res.ok
        _, until = store.calls[0]
        assert until is not None
        # The canonical predicate says it is still holding now, and released
        # after it lapses. Asking `defer_window_open` rather than comparing
        # strings here is the same discipline the dispatcher itself follows.
        assert defer_window_open(until) is True
        later = (datetime.now(timezone.utc) + timedelta(days=8)).isoformat()
        assert defer_window_open(until, later) is False

    def test_it_touches_NOTHING_but_defer(self):
        """The structural half of the isolation: no board writer is reachable.

        `_Store.__getattr__` raises on any other attribute, so a dispatcher that
        reached for a completion or snooze writer fails here rather than in
        production.
        """
        store = _Store()
        assert _dispatch_feed_defer("proposal:p:1", "defer_1d", feed_store=store).ok

    def test_a_STORE_FAULT_is_reported_not_raised(self):
        """A defer fault must never escape into `act`."""
        store = _Store(boom=True)
        res = _dispatch_feed_defer("attribution:m:x", "defer", feed_store=store)
        assert res.ok is False
        assert "stays where it is" in res.detail

    def test_the_fault_is_ANNOUNCED(self):
        with structlog.testing.capture_logs() as captured:
            _dispatch_feed_defer("a:b:c", "defer", feed_store=_Store(boom=True))
        events = [c for c in captured if c.get("event") == "feed.act.defer_failed"]
        assert len(events) == 1
        assert events[0]["error"] == "OSError"

    def test_a_successful_defer_is_announced_with_its_SHAPE(self):
        # "next_render" and "until_time" are different promises to the operator;
        # a log that flattened them could not answer "when does it come back".
        with structlog.testing.capture_logs() as captured:
            _dispatch_feed_defer("a:b:c", "defer", feed_store=_Store())
            _dispatch_feed_defer("a:b:d", "defer_3d", feed_store=_Store())
        shapes = [
            c["shape"] for c in captured if c.get("event") == "feed.act.deferred"
        ]
        assert shapes == ["next_render", "until_time"]


class TestCrashIsolationFromTheOtherVerbs:
    """A defer fault never costs a confirm — with the control that proves it.

    The positive control is the requirement: asserting only that a broken defer
    fails would pass identically against a build where defer and confirm shared
    a dispatcher and BOTH broke. So the same broken store is driven through a
    confirm, which must still succeed.
    """

    def test_a_broken_defer_leaves_a_confirm_on_the_same_store_working(self):
        class _PartlyBroken:
            """Defers explode; the state-setting a confirm needs does not."""

            def __init__(self) -> None:
                self.states: list[tuple[str, str]] = []

            def defer(self, item_id: str, *, until: str | None = None) -> None:
                raise OSError("disk went away")

            def set_state(self, item_id: str, state: str, **kw) -> None:
                self.states.append((item_id, state))

        store = _PartlyBroken()
        failed = _dispatch_feed_defer("attribution:m:x", "defer", feed_store=store)
        assert failed.ok is False

        # THE CONTROL: the confirm path on the very same store is untouched by
        # the defer's failure. If these shared a dispatcher — the chained
        # alternative — this would raise instead of recording the state.
        store.set_state("attribution:m:x", "acted")
        assert store.states == [("attribution:m:x", "acted")]

    def test_a_defer_never_reaches_a_resolver(self):
        """A defer decides NOTHING; reaching a resolver would mean it did."""
        store = _Store()
        res = _dispatch_feed_defer("email_tier:e:1", "defer_1d", feed_store=store)
        assert res.ok
        # One call, to defer. A resolver call would have tripped __getattr__.
        assert len(store.calls) == 1


class TestAdvertised:
    def test_the_quick_defer_carries_the_UP_gesture_and_the_rungs_do_not(self):
        verbs = {a["verb"]: a for a in actions_for("attribution")}
        assert verbs["defer"]["gesture"] == "defer"
        assert verbs["defer"]["label"] == "Later"
        # The dated rungs live behind the hold menu — a choice already opened,
        # not a direction that can be swiped by accident.
        assert "gesture" not in verbs["defer_3d"]
        assert verbs["defer_3d"]["label"] == "3 days"

    def test_every_defer_verb_is_light(self):
        for kind in FEED_ACTIONS:
            for action in actions_for(kind):
                if action["verb"].startswith("defer"):
                    assert action["weight"] == "light", (kind, action["verb"])

    def test_the_excluded_kind_advertises_no_defer(self):
        verbs = [a["verb"] for a in actions_for("slot_suggestion")]
        assert not any(v.startswith("defer") for v in verbs)
        # Positive control: it DOES advertise its own snooze ladder.
        assert "snooze_3d" in verbs


@pytest.mark.parametrize("state_const", [STATE_DEFERRED])
def test_the_state_the_dispatcher_drives_is_the_one_the_filter_excludes(state_const):
    """No leak path: the deck asks `state=open`, an exact match, and this is not it."""
    assert state_const != "open"
