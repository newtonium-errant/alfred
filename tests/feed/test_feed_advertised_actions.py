"""Advertised verbs — the ceiling serves its own capability list (#102 1b).

The web deck carried a hand-written per-kind verb map whose own comment conceded
it had to mirror ``FEED_ACTIONS``. Mirrors drift silently in both directions: a
verb the client offers that the ceiling refuses 400s in the operator's hand, and
a verb the ceiling gains that no client shows is a capability nobody can reach.
These pin the derivation that removes the mirror.
"""

from __future__ import annotations

import re
from pathlib import Path

from alfred.daily_sync.action_router import ACTION_META, FEED_ACTIONS, actions_for


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


class TestCrossLanguageAgreement:
    """The FE's own table still exists until 1b-ii deletes it — hold them equal.

    Same shape as the SNOOZE_ACTIONS / CONTEST_SECTIONS pins: neither side can
    notice the drift alone, so a test that reads both is the only thing that
    can. This pin RETIRES with `DECK_VERBS` — it is scaffolding for exactly one
    lane's worth of overlap, and says so.
    """

    def _fe_source(self) -> str:
        p = Path(__file__).resolve().parents[2] / "web" / "lib" / "algernon" / "feedConstants.ts"
        return p.read_text(encoding="utf-8")

    def test_fe_weights_agree_with_the_served_weights(self):
        src = self._fe_source()
        for kind in ("attribution", "proposal", "recurrence"):
            block = re.search(
                rf"^  {kind}: \{{(.*?)\}},$|^  {kind}: \{{$(.*?)^  \}},$",
                src,
                re.S | re.M,
            )
            assert block, f"{kind} not found in DECK_VERBS"
        # The one that carries the exposure, checked by value on both sides.
        assert "rejectWeight: 'heavy'" in src
        served = {a["verb"]: a for a in actions_for("attribution")}
        assert served["reject"]["weight"] == "heavy"

    def test_the_arm_note_is_the_SAME_sentence_on_both_sides(self):
        """One operator-facing sentence, two renderers, until the FE loses its copy."""
        src = self._fe_source()
        served = {a["verb"]: a for a in actions_for("attribution")}
        note = served["reject"]["note"]
        assert note in src, "the FE arm note and the served note have drifted"
