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

    def _fe_deck_verb_kinds(self) -> set[str]:
        """The kinds the web deck's own verb map claims it can act on."""
        src = self._fe_source()
        block = re.search(
            r"^export const DECK_VERBS: Record<string, DeckVerbs> = \{$(.*?)^\};$",
            src,
            re.S | re.M,
        )
        assert block, "DECK_VERBS block not found — the FE table moved or was renamed"
        kinds = set(re.findall(r"^  ([a-z_]+): \{", block.group(1), re.M))
        # Guard the denominator: an over-tight regex would silently make every
        # set comparison below trivially true.
        assert len(kinds) >= 5, f"parsed only {kinds} — the regex has gone stale"
        return kinds

    def test_the_FE_never_offers_a_kind_the_CEILING_REFUSES(self):
        """The failure direction that reaches the operator: a card that 400s.

        ``isDeckDealt`` deals any kind with a ``DECK_VERBS`` entry, so a kind the
        FE knows and the ceiling does not becomes a dealt card whose verb the
        router answers ``invalid_action``. Neither side can notice alone.

        ONE KNOWN EXCEPTION, recorded rather than hidden (found 2026-08-12
        during the deck identity lane): ``recurrence``. The FE offers a heavy
        "Promote"; ``FEED_ACTIONS`` has no ``recurrence`` entry at all. It is
        LATENT, not live — no producer constructs a ``recurrence`` FeedItem
        today (the tier-recurrence loop is CLI-only), though ``recurrence`` IS a
        declared decide-mode kind in ``feed.model``, and step-2 G6 boards the
        card redirect. So this fires the day either half moves: a producer
        landing makes it live, and closing the gap by giving the ceiling a
        ``recurrence`` entry ALSO reds this, which is the point — the exception
        must be deleted deliberately, not decay quietly.

        Retires with ``DECK_VERBS`` itself. Consuming the served ``actions[]``
        dissolves this whole class by construction, because the FE would then
        only ever offer what the ceiling served.
        """
        KNOWN_LATENT_EXCEPTIONS = {"recurrence"}
        fe_only = self._fe_deck_verb_kinds() - set(FEED_ACTIONS)
        assert fe_only == KNOWN_LATENT_EXCEPTIONS, (
            "the FE's deck verb map and the capability ceiling have drifted: "
            f"FE-only kinds are {sorted(fe_only)}, expected {sorted(KNOWN_LATENT_EXCEPTIONS)}"
        )

    def test_the_two_tables_genuinely_overlap(self):
        """Positive control for the exception pin above.

        ``fe_only == {"recurrence"}`` would also hold if the FE map had somehow
        shrunk to nothing but recurrence, or if the ceiling had swallowed
        everything. Assert the agreement is real and broad, so the pin above is
        measuring drift rather than collapse.
        """
        fe = self._fe_deck_verb_kinds()
        shared = fe & set(FEED_ACTIONS)
        assert len(shared) >= 6, f"only {sorted(shared)} in common — one side has collapsed"
        # And every shared kind actually advertises at least one verb.
        for kind in sorted(shared):
            assert actions_for(kind), kind
