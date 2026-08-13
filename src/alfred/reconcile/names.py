"""Claimant-name normalisation across the two sides of the loop.

The two sides name people differently, and the difference is measured, not
assumed. From the live data (2026-08-13 box read):

* the remittance ledger carries STRUCTURED ``surname`` + ``first_name``
  fields, parsed from the statement's own columns;
* the RRTS snapshot carries ONE ``client_name`` string in **"First Last"**
  order — zero comma-form in all 159 invoices.

That is the OPPOSITE order from ``ClaimLine.claimant`` ("Surname, First"),
so the join normalises the SNAPSHOT side down to a surname and compares
surname-to-surname. Deliberately that direction: the ledger side is already
structured and parsing my own rendered claimant string back apart would be
re-deriving a fact I already hold — the classic second spelling.

**Surname extraction from a free-text name is a JUDGEMENT**, and this module
is honest about where it is weak rather than pretending "the last token"
always works. Two-word and hyphenated surnames are the known hazard, and the
box read flagged them specifically. A wrong split does not merely miss a
match — it can produce a CONFIDENT WRONG one, joining a payment to the wrong
claimant, so ambiguity is reported rather than resolved.
"""

from __future__ import annotations

#: Surname particles that belong WITH the following token: "van der Berg",
#: "de Souza", "Mac Donald". Lower-cased for comparison. The list is
#: deliberately short — it covers the forms that actually appear in
#: Nova Scotian and Atlantic-Canadian rolls rather than attempting a
#: universal name grammar, which does not exist.
_SURNAME_PARTICLES = frozenset({
    "van", "von", "de", "del", "della", "der", "den", "di", "da", "dos",
    "du", "la", "le", "mac", "mc", "st", "st.", "saint", "ter", "ten",
})

#: Generational suffixes, stripped before the surname is taken. "Ivo Falkirk
#: Jr" has surname Falkirk, not Jr.
_SUFFIXES = frozenset({"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"})


def normalise_for_compare(value: str) -> str:
    """Casefold + collapse whitespace + drop punctuation that varies.

    Hyphens are KEPT: "Smith-Jones" and "Smith Jones" are different names,
    and collapsing them would join two claimants who are not the same
    person. Apostrophes are dropped because "O'Brien" and "OBrien" are the
    same person written two ways, and both forms appear in transcribed
    statement columns.
    """
    text = (value or "").strip().casefold().replace("'", "").replace("’", "")
    return " ".join(text.split())


def split_client_name(client_name: str) -> tuple[str, str, bool]:
    """``(surname, first_names, is_ambiguous)`` from a "First Last" string.

    Handles, because the live data contains them:

    * plain ``"Marisol Aldenshaw"`` -> ``("Aldenshaw", "Marisol", False)``
    * particles ``"Dev van Corvallis"`` -> ``("van Corvallis", "Dev", False)``
    * suffixes ``"Ivo Falkirk Jr"`` -> ``("Falkirk", "Ivo", False)``
    * hyphenated ``"Sana Everly-Brightwater"`` -> one token, unambiguous

    ``is_ambiguous`` is True where the split is a GUESS — most importantly a
    bare three-token name with no particle (``"Wren Dunmoor Ashby"``), which
    is either a two-word surname or a middle name and the string cannot say
    which. A caller must treat an ambiguous split as weaker evidence rather
    than as a fact: the failure mode of a wrong split is not a missed match
    but a confident wrong one, which is worse.

    A comma form (``"Aldenshaw, Marisol"``) is still handled even though the
    live snapshot has none — it costs three lines, and the day their export
    changes order is not the day to discover this function assumed.
    """
    raw = " ".join((client_name or "").split())
    if not raw:
        return "", "", False

    if "," in raw:
        # Comma form is UNAMBIGUOUS by construction: everything before the
        # comma is the surname, whatever its token count.
        surname, _, first = raw.partition(",")
        return surname.strip(), first.strip(), False

    tokens = raw.split()
    while tokens and tokens[-1].casefold().strip(".") in {
        s.strip(".") for s in _SUFFIXES
    }:
        tokens.pop()
    if not tokens:
        return "", "", False
    if len(tokens) == 1:
        return tokens[0], "", False

    # Walk backwards over particles: "Dev van Corvallis" -> "van Corvallis".
    idx = len(tokens) - 1
    while idx > 1 and tokens[idx - 1].casefold() in _SURNAME_PARTICLES:
        idx -= 1
    surname = " ".join(tokens[idx:])
    first = " ".join(tokens[:idx])

    # Three or more tokens with no particle: the extra token is either a
    # middle name or half of a two-word surname, and the string does not
    # say which. Reported, not resolved.
    ambiguous = len(tokens) > 2 and idx == len(tokens) - 1
    return surname, first, ambiguous


def surname_matches(ledger_surname: str, client_name: str) -> tuple[bool, bool]:
    """``(matched, was_ambiguous)`` — ledger surname vs snapshot client name.

    The ledger side is used AS GIVEN: it is already structured, and
    re-deriving a surname from my own rendered claimant string would be a
    second spelling of a fact I already hold.

    When the snapshot split is ambiguous, BOTH readings are tried — the
    narrow one (final token) that :func:`split_client_name` returns by
    default, and the wide one (final two tokens) — and either hit counts,
    with the ambiguity flag returned so the caller can weight it down.
    Refusing the ambiguous case outright would drop real matches for people
    with three-part names; accepting it silently would hide that the join
    rested on a guess.
    """
    ledger = normalise_for_compare(ledger_surname)
    if not ledger:
        return False, False

    surname, _first, ambiguous = split_client_name(client_name)
    if normalise_for_compare(surname) == ledger:
        return True, ambiguous

    if ambiguous:
        # The WIDE reading: the default split above already took the NARROW
        # one (final token only), so retrying that is a no-op — which is
        # exactly what the first version of this did, testing one reading
        # twice and calling it two chances. The alternative worth trying is
        # the two-word surname: "Wren Dunmoor Ashby" read as surname
        # "Dunmoor Ashby".
        tokens = " ".join((client_name or "").split()).split()
        if len(tokens) > 2:
            wide = " ".join(tokens[-2:])
            if normalise_for_compare(wide) == ledger:
                return True, True
    return False, ambiguous


def normalise_knumber(value: str) -> str:
    """Casefolded, whitespace-free k-number. **Width is NOT assumed.**

    The live snapshot carries ``K`` + 7 digits for 157 of 159 invoices and
    ``K`` + 4 digits for two. Zero-padding to a fixed width would make those
    two outliers un-matchable, and padding in the other direction would
    invent a k-number that does not exist. So the value is compared as
    written, and the outliers are simply short.
    """
    return "".join((value or "").split()).casefold()


__all__ = [
    "normalise_for_compare",
    "normalise_knumber",
    "split_client_name",
    "surname_matches",
]
