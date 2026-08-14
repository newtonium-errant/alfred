"""Tests for ``alfred.web.contact_patterns`` — C4 pattern-surfacing.

The self-correcting loop's LEARN and PROPOSE arrows. Load-bearing pins:

* **BOTH BARS, NOT EITHER** — ``min_observations`` alone fires on 3 of 300;
  ``threshold_ratio`` alone fires on 1 of 1.
* **PROPOSE-ONLY** — detection writes a card and NOTHING else. Nothing here may
  change the router's behaviour; only the operator's ``adopt`` tap can.
* **A DISMISSED PATTERN STAYS DISMISSED** — the acked-cards-revive failure
  CLAUDE.md records twice, pointed at this producer.
* **THE BELT HOLDS** — a feed fault must not cost the operator the override that
  was already recorded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from alfred.web.contact_patterns import (
    ACTION_ADOPT,
    ACTION_IGNORE,
    PATTERN_KIND,
    SurfacedPattern,
    build_pattern_item,
    detect_patterns,
    emit_pattern_cards,
)
from alfred.web.contact_state import (
    RULE_DEFAULT,
    RULE_FIRST_CONTACT_AFTER_GAP,
    SURFACE_BRIEF,
    SURFACE_CHAT,
    SURFACE_DECK,
    SURFACE_FEED,
    WebContactStore,
)

NOW = datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc)

CONFIG = {
    "enabled": True,
    "min_observations": 3,
    "window_days": 14,
    "same_context_required": True,
    "threshold_ratio": 0.6,
}


def _contact(
    *,
    rule: str = RULE_DEFAULT,
    surface: str = SURFACE_CHAT,
    landed: str | None = None,
    overridden: bool = False,
    ago_days: float = 1.0,
    cid: str = "c",
) -> dict:
    return {
        "id": cid,
        "ts": (NOW - timedelta(days=ago_days)).isoformat(),
        "rule": rule,
        "surface": surface,
        "landed": landed or surface,
        "overridden": overridden,
        "state": {},
    }


def _overrides(n: int, *, to: str = SURFACE_FEED, rule: str = RULE_DEFAULT) -> list[dict]:
    return [
        _contact(rule=rule, landed=to, overridden=True, cid=f"o{i}", ago_days=i + 1)
        for i in range(n)
    ]


class _FeedStore:
    def __init__(self, boom: bool = False):
        self.items: list = []
        self.boom = boom

    def upsert(self, item):  # noqa: ANN001
        if self.boom:
            raise OSError("disk went away")
        self.items.append(item)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class TestDetection:
    def test_three_of_three_overrides_to_one_surface_fires(self):
        found = detect_patterns(_overrides(3), config=CONFIG, now=NOW)
        assert len(found) == 1
        p = found[0]
        assert p.rule == RULE_DEFAULT
        assert p.surface == SURFACE_FEED
        assert p.overrides == 3
        assert p.observations == 3
        assert p.key == f"{RULE_DEFAULT}->{SURFACE_FEED}"

    def test_below_min_observations_does_not_fire(self):
        """Two overrides out of two is a perfect ratio and still not evidence."""
        assert detect_patterns(_overrides(2), config=CONFIG, now=NOW) == []

    def test_below_the_ratio_does_not_fire_even_with_enough_overrides(self):
        """3 of 10 clears min_observations and must still fail the ratio bar."""
        contacts = _overrides(3) + [
            _contact(cid=f"n{i}", ago_days=i + 1) for i in range(7)
        ]
        assert detect_patterns(contacts, config=CONFIG, now=NOW) == []
        # Positive control: the same 3 overrides against 4 contacts DOES fire,
        # so the assertion above is the ratio biting, not a dead detector.
        contacts = _overrides(3) + [_contact(cid="n0")]
        assert len(detect_patterns(contacts, config=CONFIG, now=NOW)) == 1

    def test_overrides_to_different_surfaces_do_not_pool(self):
        """'You keep going somewhere else' is not actionable — only 'you keep
        going to the deck' is."""
        contacts = [
            _contact(landed=SURFACE_FEED, overridden=True, cid="a", ago_days=1),
            _contact(landed=SURFACE_BRIEF, overridden=True, cid="b", ago_days=2),
            _contact(landed=SURFACE_DECK, overridden=True, cid="c", ago_days=3),
        ]
        assert detect_patterns(contacts, config=CONFIG, now=NOW) == []

    def test_contacts_outside_the_window_do_not_count(self):
        old = [
            _contact(landed=SURFACE_FEED, overridden=True, cid=f"o{i}", ago_days=20)
            for i in range(3)
        ]
        assert detect_patterns(old, config=CONFIG, now=NOW) == []

    def test_the_window_lever_is_what_decides(self):
        old = [
            _contact(landed=SURFACE_FEED, overridden=True, cid=f"o{i}", ago_days=20)
            for i in range(3)
        ]
        wide = {**CONFIG, "window_days": 30}
        assert len(detect_patterns(old, config=wide, now=NOW)) == 1

    def test_two_rules_are_two_contexts(self):
        contacts = _overrides(3, rule=RULE_DEFAULT) + _overrides(
            3, rule=RULE_FIRST_CONTACT_AFTER_GAP, to=SURFACE_CHAT
        )
        found = detect_patterns(contacts, config=CONFIG, now=NOW)
        assert {(p.rule, p.surface) for p in found} == {
            (RULE_DEFAULT, SURFACE_FEED),
            (RULE_FIRST_CONTACT_AFTER_GAP, SURFACE_CHAT),
        }

    def test_a_tie_breaks_deterministically_on_surface_name(self):
        """Same input, same card identity — never a coin flip.

        The ratio bar is relaxed here on purpose: at the default 0.6 a 3/3 tie
        is 3 of 6 contacts and nothing fires, so the test would pass on two
        empty lists and prove nothing about the tiebreak.
        """
        contacts = (
            [_contact(landed=SURFACE_DECK, overridden=True, cid=f"d{i}") for i in range(3)]
            + [_contact(landed=SURFACE_BRIEF, overridden=True, cid=f"b{i}") for i in range(3)]
        )
        loose = {**CONFIG, "threshold_ratio": 0.4}
        first = detect_patterns(contacts, config=loose, now=NOW)
        second = detect_patterns(list(reversed(contacts)), config=loose, now=NOW)
        assert [p.surface for p in first] == [p.surface for p in second] == [SURFACE_BRIEF]

    def test_an_undatable_contact_is_skipped_not_assumed_recent(self):
        contacts = _overrides(2) + [
            {"id": "x", "ts": "garbage", "rule": RULE_DEFAULT,
             "landed": SURFACE_FEED, "overridden": True},
        ]
        with structlog.testing.capture_logs() as captured:
            found = detect_patterns(contacts, config=CONFIG, now=NOW)
        assert found == []  # 2 datable overrides, below the bar
        line = [c for c in captured if c["event"] == "web.contact_patterns.detected"][0]
        assert line["undatable_skipped"] == 1

    def test_an_override_to_an_unknown_surface_is_ignored(self):
        contacts = [
            _contact(landed="hologram", overridden=True, cid=f"h{i}") for i in range(3)
        ]
        assert detect_patterns(contacts, config=CONFIG, now=NOW) == []

    def test_disabled_detects_nothing_and_says_so(self):
        with structlog.testing.capture_logs() as captured:
            found = detect_patterns(
                _overrides(3), config={**CONFIG, "enabled": False}, now=NOW
            )
        assert found == []
        assert any(c["event"] == "web.contact_patterns.disabled" for c in captured)

    def test_same_context_false_is_not_implemented_and_warns(self):
        """Declared, not silently honoured: a pooled pattern names no rule, and
        ``adopt`` would then have no defined target."""
        with structlog.testing.capture_logs() as captured:
            found = detect_patterns(
                _overrides(3), config={**CONFIG, "same_context_required": False},
                now=NOW,
            )
        assert len(found) == 1  # still detected, per-rule
        warns = [
            c for c in captured
            if c["event"] == "web.contact_patterns.same_context_forced"
        ]
        assert len(warns) == 1

    def test_detection_logs_on_every_run_including_the_empty_one(self):
        with structlog.testing.capture_logs() as captured:
            detect_patterns([], config=CONFIG, now=NOW)
        lines = [c for c in captured if c["event"] == "web.contact_patterns.detected"]
        assert len(lines) == 1
        assert lines[0]["found"] == 0
        assert lines[0]["detail"] == "ran, no pattern over threshold"


# ---------------------------------------------------------------------------
# The card
# ---------------------------------------------------------------------------


class TestCard:
    def _pattern(self) -> SurfacedPattern:
        return SurfacedPattern(
            rule=RULE_DEFAULT, surface=SURFACE_FEED,
            overrides=3, observations=4, window_days=14,
        )

    def test_the_card_is_a_decide_card_with_a_stable_identity(self):
        item = build_pattern_item(self._pattern(), instance="Salem", user_key="u1")
        assert item.kind == PATTERN_KIND
        assert item.id == f"{PATTERN_KIND}:{RULE_DEFAULT}->{SURFACE_FEED}"
        assert item.mode == "decide"
        assert item.attention == "needs_you"

    def test_re_detecting_the_same_pattern_is_the_same_card(self):
        a = build_pattern_item(self._pattern(), instance="Salem", user_key="u1")
        b = build_pattern_item(self._pattern(), instance="Salem", user_key="u1")
        assert a.id == b.id

    def test_the_title_carries_the_counts_and_the_destination(self):
        item = build_pattern_item(self._pattern(), instance="Salem", user_key="u1")
        assert "3 of the last 4" in item.title
        assert SURFACE_FEED in item.title

    def test_the_user_key_rides_in_source_ref_not_evidence(self):
        """The act path writes back to the key the card was minted from;
        evidence is rendered to the operator and source_ref is not."""
        item = build_pattern_item(self._pattern(), instance="Salem", user_key="u1")
        assert item.source_ref["user_key"] == "u1"
        assert "user_key" not in item.evidence

    def test_the_card_declares_what_it_cannot_offer(self):
        item = build_pattern_item(self._pattern(), instance="Salem", user_key="u1")
        assert "inferred condition" in item.evidence["not_offered"]


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


class TestEmission:
    def _store_with_overrides(self, tmp_path, n=3, to=SURFACE_FEED):
        store = WebContactStore.create(tmp_path / "c.json")
        for i in range(n):
            e = store.record_contact(
                "u1", rule=RULE_DEFAULT, surface=SURFACE_CHAT,
                now=NOW - timedelta(days=i + 1),
            )
            store.record_override("u1", e["id"], surface=to, now=NOW)
        return store

    def test_a_detected_pattern_is_dealt(self, tmp_path):
        store = self._store_with_overrides(tmp_path)
        feed = _FeedStore()
        n = emit_pattern_cards(
            contact_store=store, user_key="u1", feed_store=feed,
            instance="Salem", config=CONFIG, now=NOW,
        )
        assert n == 1
        assert feed.items[0].kind == PATTERN_KIND

    def test_a_suppressed_pattern_is_not_re_dealt(self, tmp_path):
        store = self._store_with_overrides(tmp_path)
        store.suppress_pattern("u1", f"{RULE_DEFAULT}->{SURFACE_FEED}", days=14, now=NOW)
        feed = _FeedStore()
        with structlog.testing.capture_logs() as captured:
            n = emit_pattern_cards(
                contact_store=store, user_key="u1", feed_store=feed,
                instance="Salem", config=CONFIG, now=NOW,
            )
        assert n == 0
        assert feed.items == []
        line = [c for c in captured if c["event"] == "web.contact_patterns.emitted"][0]
        assert line["suppressed"] == 1

    def test_suppression_lapses_and_the_pattern_returns(self, tmp_path):
        store = self._store_with_overrides(tmp_path)
        store.suppress_pattern("u1", f"{RULE_DEFAULT}->{SURFACE_FEED}", days=14, now=NOW)
        feed = _FeedStore()
        later = NOW + timedelta(days=15)
        # Re-date the contacts so they are still inside the detection window.
        for e in store.contacts["u1"]:
            e["ts"] = (later - timedelta(days=1)).isoformat()
        n = emit_pattern_cards(
            contact_store=store, user_key="u1", feed_store=feed,
            instance="Salem", config=CONFIG, now=later,
        )
        assert n == 1

    def test_an_already_adopted_pattern_is_not_re_proposed(self, tmp_path):
        store = self._store_with_overrides(tmp_path)
        store.adopt_default("u1", rule=RULE_DEFAULT, surface=SURFACE_FEED)
        feed = _FeedStore()
        with structlog.testing.capture_logs() as captured:
            n = emit_pattern_cards(
                contact_store=store, user_key="u1", feed_store=feed,
                instance="Salem", config=CONFIG, now=NOW,
            )
        assert n == 0
        line = [c for c in captured if c["event"] == "web.contact_patterns.emitted"][0]
        assert line["already_adopted"] == 1

    def test_adopting_ONE_surface_does_not_silence_a_pattern_for_another(
        self, tmp_path
    ):
        store = self._store_with_overrides(tmp_path, to=SURFACE_DECK)
        store.adopt_default("u1", rule=RULE_DEFAULT, surface=SURFACE_FEED)
        feed = _FeedStore()
        n = emit_pattern_cards(
            contact_store=store, user_key="u1", feed_store=feed,
            instance="Salem", config=CONFIG, now=NOW,
        )
        assert n == 1

    def test_a_feed_fault_never_raises_into_the_override_write(self, tmp_path):
        store = self._store_with_overrides(tmp_path)
        with structlog.testing.capture_logs() as captured:
            n = emit_pattern_cards(
                contact_store=store, user_key="u1", feed_store=_FeedStore(boom=True),
                instance="Salem", config=CONFIG, now=NOW,
            )
        assert n == 0
        fails = [
            c for c in captured if c["event"] == "web.contact_patterns.emit_failed"
        ]
        assert len(fails) == 1
        assert fails[0]["error"] == "OSError"

    def test_emission_logs_on_every_run_including_the_empty_one(self, tmp_path):
        store = WebContactStore.create(tmp_path / "c.json")
        with structlog.testing.capture_logs() as captured:
            emit_pattern_cards(
                contact_store=store, user_key="u1", feed_store=_FeedStore(),
                instance="Salem", config=CONFIG, now=NOW,
            )
        lines = [c for c in captured if c["event"] == "web.contact_patterns.emitted"]
        assert len(lines) == 1
        assert lines[0]["emitted"] == 0
        assert "nothing" in lines[0]["detail"]


class TestTheVerbsAreSpelledOnce:
    def test_the_producers_verbs_are_the_ceilings_verbs(self):
        """One vocabulary, two readers — the producer's card note names these
        verbs and the dispatcher answers them."""
        from alfred.daily_sync.action_router import (
            FEED_ACTIONS,
            PATTERN_ADOPT,
            PATTERN_IGNORE,
            PATTERN_KIND as ROUTER_KIND,
        )

        assert ROUTER_KIND == PATTERN_KIND
        assert PATTERN_ADOPT == ACTION_ADOPT
        assert PATTERN_IGNORE == ACTION_IGNORE
        assert set(FEED_ACTIONS[PATTERN_KIND]) == {ACTION_ADOPT, ACTION_IGNORE}
