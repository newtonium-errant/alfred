"""Claimant-name normalisation across the two sides of the loop.

The two sides name people in OPPOSITE orders, and that is measured rather
than assumed (box read, 2026-08-13): the ledger carries structured
``surname`` + ``first_name``; the RRTS snapshot carries one ``client_name``
in "First Last" order, with **zero comma-form in all 159 invoices**.

So the join normalises the SNAPSHOT side down to a surname. That direction
is deliberate — the ledger side is already structured, and re-deriving a
surname from my own rendered claimant string would be a second spelling of
a fact I already hold.

**The hazard these tests exist for**: a wrong split does not merely miss a
match, it can produce a CONFIDENT WRONG one — a payment joined to the wrong
claimant. So ambiguity is reported rather than resolved, and the pins below
assert the ambiguity flag as hard as they assert the split.
"""

from __future__ import annotations

import pytest

from alfred.reconcile.names import (
    normalise_for_compare,
    normalise_knumber,
    split_client_name,
    surname_matches,
)


@pytest.mark.parametrize(
    "raw,surname,first,ambiguous",
    [
        # The overwhelmingly common shape.
        ("Marisol Aldenshaw", "Aldenshaw", "Marisol", False),
        # Particles belong WITH the surname.
        ("Dev van Corvallis", "van Corvallis", "Dev", False),
        ("Tomas de la Brightwater", "de la Brightwater", "Tomas", False),
        # Suffixes are stripped before the surname is taken.
        ("Ivo Falkirk Jr", "Falkirk", "Ivo", False),
        ("Ivo Falkirk Jr.", "Falkirk", "Ivo", False),
        # Hyphenated is ONE token — unambiguous by construction.
        ("Sana Everly-Brightwater", "Everly-Brightwater", "Sana", False),
        # Single token: all we have.
        ("Nils", "Nils", "", False),
        # Comma form, though the live snapshot has none — unambiguous.
        ("Aldenshaw, Marisol", "Aldenshaw", "Marisol", False),
        # THE ambiguous case: three tokens, no particle. Either a middle
        # name or a two-word surname, and the string cannot say which.
        ("Wren Dunmoor Ashby", "Ashby", "Wren Dunmoor", True),
    ],
)
def test_split_client_name(raw, surname, first, ambiguous) -> None:
    assert split_client_name(raw) == (surname, first, ambiguous)


def test_an_empty_name_yields_nothing() -> None:
    assert split_client_name("") == ("", "", False)
    assert split_client_name("   ") == ("", "", False)


def test_the_ambiguous_case_matches_BOTH_readings() -> None:
    """The narrow reading (final token) is what the split returns; the wide
    one (final two) is tried as a fallback. Both count, and both carry the
    ambiguity flag.

    The first version of this fallback re-tried the NARROW reading — testing
    one reading twice and calling it two chances. It was a no-op, and a
    smoke run caught it; a two-word surname would simply never have matched.
    """
    assert surname_matches("Ashby", "Wren Dunmoor Ashby") == (True, True)
    assert surname_matches("Dunmoor Ashby", "Wren Dunmoor Ashby") == (True, True)


def test_a_first_name_is_not_a_surname_match() -> None:
    """The control for the fallback: widening the readings must not make
    everything match. "Wren" is the FIRST name and must not join."""
    assert surname_matches("Wren", "Wren Dunmoor Ashby") == (False, True)


def test_an_unambiguous_match_says_so() -> None:
    """The flag must distinguish, or it means nothing — a matcher weighting
    ambiguous joins down needs the common case to come back clean."""
    matched, ambiguous = surname_matches("Aldenshaw", "Marisol Aldenshaw")
    assert matched is True
    assert ambiguous is False


def test_hyphens_are_kept_because_they_distinguish_people() -> None:
    """"Everly" and "Everly-Brightwater" are different names. Collapsing the
    hyphen would join two claimants who are not the same person — the
    confident-wrong-match failure this module is arranged against.

    THE DISCRIMINATING CASE is the last assertion, and the first version of
    this test did not have it. Both assertions above it hold whether hyphens
    are kept or collapsed, so a mutation that collapsed them scored ZERO —
    the pin could not fire. Only a ledger surname spelled with a SPACE
    against a snapshot spelled with a HYPHEN separates the two behaviours.
    """
    assert surname_matches("Everly", "Sana Everly-Brightwater")[0] is False
    assert surname_matches("Everly-Brightwater", "Sana Everly-Brightwater")[0] is True
    # Space-vs-hyphen: matches ONLY if hyphens were wrongly collapsed.
    assert surname_matches("Everly Brightwater", "Sana Everly-Brightwater")[0] is False


def test_apostrophes_are_dropped_because_they_vary() -> None:
    """The same person written two ways. Both forms appear in transcribed
    statement columns."""
    assert surname_matches("O'Brien", "Nils OBrien")[0] is True
    assert surname_matches("OBrien", "Nils O'Brien")[0] is True


def test_case_and_spacing_do_not_defeat_a_match() -> None:
    assert surname_matches("aldenshaw", "Marisol  ALDENSHAW")[0] is True


def test_an_empty_ledger_surname_never_matches() -> None:
    """Matching on nothing is not matching — the same rule the report's
    aggregate attribution needed. Two blanks agreeing is not an identity.

    THE CASE THAT MATTERS is the last pair, and the first version of this
    test omitted it. Against a NON-EMPTY client name the guard makes no
    difference — "aldenshaw" != "" either way — so a mutation removing it
    scored ZERO. The guard only earns its place when BOTH sides are blank,
    which is precisely the bug it was written for and precisely the case the
    pin was missing. Same shape as the empty-claimant defect in the report
    layer, and I wrote the same hole into its test.
    """
    assert surname_matches("", "Marisol Aldenshaw") == (False, False)
    assert surname_matches("   ", "Marisol Aldenshaw") == (False, False)
    # Both blank: without the guard these agree and return a MATCH.
    assert surname_matches("", "") == (False, False)
    assert surname_matches("   ", "   ") == (False, False)


def test_normalise_for_compare_is_stable() -> None:
    assert normalise_for_compare("  Van  Corvallis ") == "van corvallis"
    assert normalise_for_compare("") == ""


# --- k-numbers: width is NOT assumed ----------------------------------------------


def test_knumber_width_is_not_assumed() -> None:
    """157 of 159 live invoices carry K+7 digits; TWO carry K+4. Padding to
    a fixed width would make those two un-matchable, and padding the other
    way would invent a k-number that does not exist. Compared as written."""
    assert normalise_knumber("K0001234") == "k0001234"
    assert normalise_knumber("K1234") == "k1234"
    assert normalise_knumber("K0001234") != normalise_knumber("K1234")


def test_knumber_ignores_case_and_spacing() -> None:
    assert normalise_knumber(" k 000 1234 ") == normalise_knumber("K0001234")


def test_an_absent_knumber_normalises_to_empty() -> None:
    assert normalise_knumber("") == ""
    assert normalise_knumber(None) == ""  # type: ignore[arg-type]
