"""R4 — the Python half of "the voice-calibration card is quiet".

THE SPLIT, so neither half is mistaken for the whole. The card's silence is a
cross-language contract:

  * TypeScript owns whether an item SHAPED like this rings or deals —
    ``web/tests/calibrationCardQuiet.test.ts`` drives the real
    ``isNeedsYouItem`` / ``isDeckCandidate`` against a card built from the
    served-actions fixture, and reads this module's seed rather than minting it.
  * This file owns the Python-side facts the web predicates cannot see: that the
    seed is in the table at all, and that ACTION_META carries NO ``group`` — the
    mutation that would deal this card to the deck lives HERE, in a presentation
    table nowhere near the seed.

Neither half is redundant. Delete the seed and the web test throws (it cannot
find the entry); add a ``group`` and only the web test's control notices what it
means — but the change itself is a one-word Python edit that this file reds on
immediately, next to the code being edited.
"""

from __future__ import annotations

from alfred.daily_sync.action_router import (
    ACTION_META,
    CALIBRATION_APPLY_ACTION,
    CALIBRATION_DISCARD_ACTION,
    CALIBRATION_KIND,
    FEED_ACTIONS,
    actions_for,
)
from alfred.feed.model import (
    ATTENTION_FYI,
    ATTENTION_NEEDS_YOU,
    KIND_CALIBRATION,
    KIND_DEFAULTS,
    KINDS,
    MODE_DECIDE,
    MODE_FYI,
)


def test_the_kind_is_registered_and_seeded_quiet() -> None:
    """The seed pair, asserted with its premise stated at the assertion.

    ``(MODE_FYI, ATTENTION_FYI)`` is not a style preference: ``isNeedsYouItem``
    is ``attention === 'needs_you' || mode === 'decide'`` and the push poller
    fetches on that predicate with NO kind allowlist, so MODE_DECIDE would ring
    the phone REGARDLESS of attention. There is no decide/fyi combination that
    stays quiet — which is why the seeding rule ("a card that ASKS reads like a
    decide kind") is deliberately overruled for this kind.
    """
    assert KIND_CALIBRATION in KINDS
    assert KIND_DEFAULTS[KIND_CALIBRATION] == (MODE_FYI, ATTENTION_FYI)
    # Said the other way, so the pin fails on the specific hazard rather than on
    # any tuple change: this kind must never be seeded loud.
    mode, attention = KIND_DEFAULTS[KIND_CALIBRATION]
    assert mode != MODE_DECIDE
    assert attention != ATTENTION_NEEDS_YOU


def test_neither_calibration_verb_carries_a_suggested_choice_group() -> None:
    """THE NON-OBVIOUS HAZARD, pinned at the table where it would be introduced.

    ``isDeckCandidate`` is ``mode === 'decide' || hasSuggestedChoice``, so a
    grouped verb family deals the card to the DECK — a needs-you surface —
    without touching mode or attention. The web control measures that a group
    shared by BOTH verbs is what does it (``holdChoicesForVerb`` requires
    ``family.length >= 2``); this asserts neither carries one at all, which is
    the stronger and simpler invariant to hold at the source.
    """
    meta = ACTION_META[CALIBRATION_KIND]
    for verb in (CALIBRATION_APPLY_ACTION, CALIBRATION_DISCARD_ACTION):
        assert "group" not in meta[verb], (
            f"{verb} gained a choice group — this deals the calibration card to "
            "the deck. See KIND_DEFAULTS' note in feed/model.py."
        )

    # Over the SERVED payload too, not just the meta table: the served list is
    # what the client actually reads, and a group could in principle be
    # introduced by the serving layer rather than by the table above.
    for action in actions_for(CALIBRATION_KIND):
        assert "group" not in action, action


def test_the_ceiling_is_exactly_the_two_verbs_plus_the_generic_defers() -> None:
    """The capability ceiling — an action absent from it can never reach a handler.

    Pinned as a SET rather than as membership checks: the population is the
    property that matters (a third verb appearing is exactly what this should
    catch), and per-member assertions are blind to it.
    """
    from alfred.daily_sync.action_router import DEFER_ACTIONS

    assert set(FEED_ACTIONS[CALIBRATION_KIND]) == {
        CALIBRATION_APPLY_ACTION,
        CALIBRATION_DISCARD_ACTION,
        *DEFER_ACTIONS,
    }


def test_calibration_does_not_borrow_the_contest_verb() -> None:
    """A contest is shaped like a reject and MEANS something else.

    It carries ``contested_section`` bound to ``CONTEST_SECTIONS`` and says
    "this inference is wrong, and THIS capture section produced it". A wording
    proposal has no capture section, so borrowing the verb would drag a
    meaningless picker onto the card. Pinned because the two are shape-
    compatible and nothing else would notice the substitution.
    """
    from alfred.daily_sync.action_router import CONTEST_ACTION

    assert CONTEST_ACTION not in FEED_ACTIONS[CALIBRATION_KIND]
    # POSITIVE CONTROL: the verb really is admitted somewhere, so the absence
    # above is a scoping fact rather than a check on a symbol nobody uses.
    assert CONTEST_ACTION in FEED_ACTIONS["attribution"]


def test_calibration_verbs_are_not_admitted_for_any_other_kind() -> None:
    """The per-kind gate from the other side — the blast-radius control.

    The ruling that authorised this affordance rested on both gates being
    per-kind. This is the backend half of that claim, asserted over the WHOLE
    ceiling rather than spot-checked on a neighbour.
    """
    for kind, verbs in FEED_ACTIONS.items():
        if kind == CALIBRATION_KIND:
            continue
        assert CALIBRATION_APPLY_ACTION not in verbs, kind
        assert CALIBRATION_DISCARD_ACTION not in verbs, kind

    # And attribution specifically still offers exactly what it did — the kind
    # the ruling named as the one that must not grow buttons.
    assert set(FEED_ACTIONS["attribution"]) >= {"confirm", "reject", "contest"}
