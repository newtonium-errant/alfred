"""Advertised verbs — the ceiling serves its own capability list (#102 1b).

The web deck carried a hand-written per-kind verb map whose own comment conceded
it had to mirror ``FEED_ACTIONS``. Mirrors drift silently in both directions: a
verb the client offers that the ceiling refuses 400s in the operator's hand, and
a verb the ceiling gains that no client shows is a capability nobody can reach.
These pin the derivation that removes the mirror.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from alfred.daily_sync.action_router import (
    ACTION_META,
    FEED_ACTIONS,
    actions_for,
    actions_for_item,
)


class TestDerivedFromTheCeiling:
    def test_every_ceiling_pair_is_advertised(self):
        """The set comes from FEED_ACTIONS, not from the presentation table."""
        for kind, ceiling in FEED_ACTIONS.items():
            advertised = [a["verb"] for a in actions_for(kind)]
            assert advertised == list(ceiling), kind

    def test_a_verb_ADDED_TO_THE_CEILING_is_advertised_with_no_meta_edit(self):
        """The F3-by-construction claim, exercised rather than asserted.

        This is the whole point of deriving: a capability gained by the ceiling
        must reach clients without anyone remembering a second file. The old
        mirror failed exactly here, silently.
        """
        FEED_ACTIONS["pending"]["snooze_forever"] = {}
        try:
            verbs = [a["verb"] for a in actions_for("pending")]
            assert "snooze_forever" in verbs
            # Unlabelled, so it ships under its raw id and carries NO gesture —
            # visibly unfinished rather than invisibly absent.
            entry = next(a for a in actions_for("pending") if a["verb"] == "snooze_forever")
            assert entry["label"] == "snooze_forever"
            assert "gesture" not in entry
            assert entry["weight"] == "light"
        finally:
            del FEED_ACTIONS["pending"]["snooze_forever"]

    def test_meta_never_advertises_beyond_the_ceiling(self):
        """A presentation entry for a pair the ceiling refuses must not ship.

        The failure direction that matters: a button that 400s when pressed.
        """
        ACTION_META.setdefault("pending", {})["obliterate"] = {
            "label": "Obliterate", "weight": "light", "gesture": "affirm",
        }
        try:
            assert "obliterate" not in [a["verb"] for a in actions_for("pending")]
            # Positive control: a REAL pending verb is still advertised, so this
            # is not passing because the function returned nothing at all.
            assert "noted" in [a["verb"] for a in actions_for("pending")]
        finally:
            del ACTION_META["pending"]["obliterate"]

    def test_unknown_kind_is_empty_and_a_known_one_is_not(self):
        assert actions_for("weather") == []
        assert actions_for("nonsense") == []
        assert actions_for("attribution") != []


class TestTheWeightsOnTheWire:
    def test_attribution_reject_is_heavy_and_says_what_it_removes(self):
        """The 1a exposure, now carried on the wire instead of client-side only.

        ``vault.attribution.reject_marker`` strips the marked line range out of
        the record body and drops its audit entry. A client that learns the verb
        from the wire must learn its weight from the wire too, or it will render
        a destructive verb as a one-swipe commit.
        """
        verbs = {a["verb"]: a for a in actions_for("attribution")}
        assert verbs["reject"]["weight"] == "heavy"
        assert "Removes the marked section" in verbs["reject"]["note"]
        assert verbs["reject"]["gesture"] == "reject"
        # The asymmetry is the claim: the confirm half stays light.
        assert verbs["confirm"]["weight"] == "light"
        assert "note" not in verbs["confirm"]

    def test_the_mutation_bearing_half_of_proposal_is_the_heavy_one(self):
        verbs = {a["verb"]: a for a in actions_for("proposal")}
        assert verbs["confirm"]["weight"] == "heavy"
        assert verbs["confirm"]["note"]
        assert verbs["reject"]["weight"] == "light"

    def test_every_heavy_verb_declares_its_consequence(self):
        """A heavy verb with no note would arm a card that cannot say why."""
        for kind in FEED_ACTIONS:
            for action in actions_for(kind):
                if action["weight"] == "heavy":
                    assert action.get("note"), (kind, action["verb"])

    def test_gestures_are_at_most_one_per_direction_per_kind(self):
        """Two affirms on one kind would make the swipe ambiguous."""
        for kind in FEED_ACTIONS:
            gestures = [a.get("gesture") for a in actions_for(kind) if a.get("gesture")]
            assert len(gestures) == len(set(gestures)), kind


class TestPerItemNarrowing:
    """``actions_for_item`` — the kind's ceiling, minus what THIS item refuses.

    ``actions_for`` can only answer the per-KIND question. Some of the router's
    refusals are per-ITEM: it reads the item's stamped evidence and declines. A
    verb that will be refused must not be OFFERED, or the operator gets a
    control that 400s in their hand — the same failure the served verb list was
    built to remove, just one level down.
    """

    def _slot(self, **evidence: object) -> dict:
        """A serialised slot item — the shape the read path holds."""
        return {
            "kind": "slot_suggestion",
            "evidence": {"tier": 1, "origin": "task", **evidence},
        }

    def test_a_candidate_slot_IS_offered_accept(self):
        """Positive control for the whole class: the filter can be passed."""
        verbs = [a["verb"] for a in actions_for_item(self._slot(candidate=True))]
        assert "accept" in verbs

    def test_a_COMMITTED_slot_is_NOT_offered_accept(self):
        """The narrowing itself — mirrors the guard at ``_dispatch_slot_confirm``.

        A committed slot is already on today's plan; there is nothing to accept
        and the router says so (``invalid_action``, no write).
        """
        item = self._slot(candidate=False)
        verbs = [a["verb"] for a in actions_for_item(item)]
        assert "accept" not in verbs
        # Positive control in the same test: SURGICAL, not a collapse. Only the
        # one unreachable verb goes; a pin that just checked "accept absent"
        # would pass identically against a build that returned nothing at all.
        assert {"done", "undo_done", "unsnooze", "snooze_1d"} <= set(verbs)

    def test_the_flag_is_matched_BY_IDENTITY_so_a_near_miss_fails_CLOSED(self):
        """``is not True`` — the router's exact predicate, not truthiness.

        A producer that stamps ``1`` or ``"yes"`` has not stamped a candidate.
        Withholding the verb on anything but a real ``True`` keeps this table on
        the same side of the line as the gate it mirrors; matching truthiness
        here would advertise a verb the router then refuses.
        """
        for flag in ({}, {"candidate": None}, {"candidate": "yes"}, {"candidate": 1}):
            item = {"kind": "slot_suggestion", "evidence": dict(flag)}
            assert "accept" not in [a["verb"] for a in actions_for_item(item)], flag

    def test_malformed_evidence_withholds_accept_without_raising(self):
        """Evidence is producer-written and reaches here as parsed JSON."""
        for ev in (None, [], "candidate", 7):
            item = {"kind": "slot_suggestion", "evidence": ev}
            assert "accept" not in [a["verb"] for a in actions_for_item(item)], ev

    def test_the_surviving_entries_keep_their_FULL_presentation(self):
        """Filtering drops entries; it must not flatten the ones it keeps."""
        served = {a["verb"]: a for a in actions_for_item(self._slot(candidate=True))}
        assert served["accept"]["label"] == "Take it"
        assert served["accept"]["gesture"] == "affirm"
        assert served["accept"]["weight"] == "light"

    def test_done_and_undo_done_are_STILL_advertised_unconditionally(self):
        """Recorded as a BOARDED follow-up, not an oversight.

        Their per-item conditions (the completion-lane matrix; undo's
        accepted-vs-done distinction) are their own lane. This pin exists so
        that widening into them is a DELIBERATE act that reds a test, rather
        than a quiet behaviour change nobody chose.
        """
        verbs = [a["verb"] for a in actions_for_item(self._slot(candidate=False))]
        assert "done" in verbs and "undo_done" in verbs

    def test_an_URGENT_item_still_arrives_with_its_ONLY_verb(self):
        """THE TRAP. ``email_urgent`` is MODE_DECIDE, and the universal FYI gate
        refuses ``ack`` on any non-FYI item — but the urgent ack is intercepted
        BEFORE that gate reaches it, on purpose. A generalised "strip the verbs
        the gates would refuse" pass that consulted the FYI gate would take the
        ONLY verb off every urgent interrupt card, leaving an un-actionable
        card with a stamped-out face.
        """
        verbs = [a["verb"] for a in actions_for_item({"kind": "email_urgent", "evidence": {}})]
        assert "ack" in verbs

    def test_ONLY_slots_are_narrowed(self):
        """Every other kind passes through byte-identical to the per-kind answer.

        The blast radius of the per-item layer, asserted rather than assumed.
        """
        narrowed = []
        for kind in FEED_ACTIONS:
            per_item = actions_for_item({"kind": kind, "evidence": {}})
            if per_item != actions_for(kind):
                narrowed.append(kind)
        assert narrowed == ["slot_suggestion"], narrowed

    def test_an_unknown_kind_is_still_empty(self):
        assert actions_for_item({"kind": "weather", "evidence": {}}) == []
        assert actions_for_item({}) == []


class TestTheFEFixtureMatchesWhatIsServed:
    """The web deck's TEST FIXTURE must equal what this module actually serves.

    This is NOT the retired mirror pin in a new coat, and the distinction is the
    whole point. That pin held two PRODUCTION tables equal, and it existed
    because the client had its own opinion about verbs; the client no longer
    does, so that class is gone by construction.

    What replaced it is a hazard peculiar to deriving from the wire: the FE's
    ~1,800 deck tests now need items that CARRY served verbs, and a fixture that
    invents them is a fixture of a payload the server never sends. Such a test
    is green and meaningless — worse than a missing test, because it reports
    coverage of behaviour nobody has exercised. The fixture is therefore
    generated from :func:`actions_for` and pinned here, so the FE suite is
    always testing against the real wire shape.

    The comparison is over the SAME JSON file the FE imports — not a re-typed
    copy — so there is no third table to drift.
    """

    def _fixture(self) -> dict:
        p = (
            Path(__file__).resolve().parents[2]
            / "web" / "tests" / "helpers" / "servedActions.json"
        )
        return json.loads(p.read_text(encoding="utf-8"))

    def test_every_kind_matches_verb_for_verb_and_in_order(self):
        fixture = self._fixture()["kinds"]
        for kind in FEED_ACTIONS:
            assert fixture.get(kind) == actions_for(kind), kind

    def test_the_fixture_covers_every_kind_and_invents_none(self):
        """Both directions: a missing kind starves the FE tests of a real
        payload; an extra one would let the FE test a card the ceiling cannot
        produce (exactly the `recurrence` shape that made the old pin necessary).
        """
        assert set(self._fixture()["kinds"]) == set(FEED_ACTIONS)

    def test_the_comparison_is_not_vacuous(self):
        """Positive control: the file really parsed and really has content.

        Every assertion above holds trivially against an empty dict read from a
        path that silently moved.
        """
        fixture = self._fixture()["kinds"]
        assert len(fixture) >= 6, fixture
        assert any(a.get("gesture") for a in fixture["email_tier"])

# ---------------------------------------------------------------------------
# RETIRED: TestCrossLanguageAgreement (#102 1b-ii)
# ---------------------------------------------------------------------------
# Four tests held the FE's `DECK_VERBS` table equal to this module's ceiling by
# reading `feedConstants.ts` as TEXT. They are deleted here DELIBERATELY, in the
# same commit that deletes the table they read — this is the retirement their own
# docstrings called for ("Retires with `DECK_VERBS` itself"), not a casualty of
# one.
#
# The whole class is dissolved BY CONSTRUCTION rather than replaced: the FE now
# consumes the served `actions[]`, so it cannot offer a verb the ceiling does not
# serve. There is no longer a second table to disagree with.
#
# That includes the `recurrence` exception those tests carried — the FE offered a
# heavy "Promote" for a kind with NO `FEED_ACTIONS` entry at all, latent only
# because no producer builds a `recurrence` item today. Its pin said the
# exception "must be deleted deliberately, not decay quietly", so: `recurrence`
# is gone from the client with the rest of the table, and a `recurrence` item
# would now arrive with `actions: []` and simply not be dealt — the misfire is
# closed rather than recorded. If `recurrence` is ever meant to be actionable,
# the fix is a `FEED_ACTIONS` entry, and the deck will pick it up with no client
# change. That is the F3 property this lane bought.
#
# What replaced it is NOT another mirror pin: `TestTheFEFixtureMatchesWhatIsServed`
# above guards the FE's TEST FIXTURE, which is a different hazard (a fixture that
# invents a payload the server never sends makes ~1,800 deck tests green and
# meaningless). Production-to-production agreement needs no pin now; fixture-to-
# production does.
