"""PlayerContextPrimer contract pin (Phase C3a — defined for C3c, no consumer).

Pins the carry-shape + the fail-loud validity gate so C3c wires producer +
consumer against a stable contract: valid round-trips; a bad date or unknown
section id is invalid (the consumer then answers un-grounded, never on a bogus
slide).

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

from alfred.brief.narration import SEGMENT_ORDER
from alfred.brief.player_primer import KNOWN_PRIMER_SECTIONS, PlayerContextPrimer


def test_valid_primer_roundtrips() -> None:
    p = PlayerContextPrimer(brief_date="2026-08-01", section_id="day_plan")
    assert p.valid
    assert PlayerContextPrimer.from_dict(p.to_dict()) == p
    assert p.to_dict() == {"brief_date": "2026-08-01", "section_id": "day_plan"}


def test_known_sections_match_narration_order() -> None:
    """The primer vocabulary IS the narration segment vocabulary — drift-pinned
    so adding a segment without updating the primer contract can't pass silently."""
    assert KNOWN_PRIMER_SECTIONS == frozenset(SEGMENT_ORDER)


def test_every_narration_section_is_a_valid_primer_target() -> None:
    for section_id in SEGMENT_ORDER:
        assert PlayerContextPrimer(brief_date="2026-08-01", section_id=section_id).valid


def test_unknown_section_is_invalid() -> None:
    p = PlayerContextPrimer(brief_date="2026-08-01", section_id="not_a_slide")
    assert not p.valid
    assert p.context_line() == ""  # invalid → no grounding note (answer un-grounded)


def test_bad_date_is_invalid() -> None:
    assert not PlayerContextPrimer(brief_date="today", section_id="health").valid
    assert not PlayerContextPrimer(brief_date="", section_id="health").valid


def test_context_line_populated_when_valid() -> None:
    line = PlayerContextPrimer(brief_date="2026-08-01", section_id="weather").context_line()
    assert "weather" in line and "2026-08-01" in line


def test_from_dict_tolerates_missing_keys() -> None:
    p = PlayerContextPrimer.from_dict(None)
    assert not p.valid  # empty → invalid, no crash
    assert PlayerContextPrimer.from_dict({"brief_date": "2026-08-01"}).section_id == ""


def test_from_dict_tolerates_non_dict() -> None:
    """A non-dict primer (a bare string / list / int slipping past an upstream
    bound-only guard) → EMPTY invalid primer, NEVER an AttributeError — so the
    consumer answers un-grounded instead of 500-ing the turn (defense-in-depth,
    not trusting the BFF shape guard alone)."""
    for bad in ("garbage", ["day_plan"], 42, 3.14, True):
        p = PlayerContextPrimer.from_dict(bad)  # type: ignore[arg-type]
        assert not p.valid
        assert p.brief_date == "" and p.section_id == ""
        assert p.context_line() == ""
